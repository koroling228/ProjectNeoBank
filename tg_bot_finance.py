import io
import os
import time
import threading
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import pyodbc
import requests
import telebot
from requests.adapters import HTTPAdapter
from telebot import types
from urllib3.util.retry import Retry


TOKEN = "PASTE_NEW_TELEGRAM_BOT_TOKEN_HERE"
ADMIN_ID = 1376134977

SQL_SERVER_CONFIG = {
    "driver": "{ODBC Driver 18 for SQL Server}",
    "server": r"localhost\SQLEXPRESS",   # замени на свой сервер
    "database": "FinanceBotDB",
    "trusted_connection": "yes",
}


def build_connection_string() -> str:
    return (
        f"DRIVER={SQL_SERVER_CONFIG['driver']};"
        f"SERVER={SQL_SERVER_CONFIG['server']};"
        f"DATABASE={SQL_SERVER_CONFIG['database']};"
        f"Trusted_Connection={SQL_SERVER_CONFIG['trusted_connection']};"
        f"TrustServerCertificate=yes;"
    )


CONNECTION_STRING = build_connection_string()


def get_db():
    return pyodbc.connect(CONNECTION_STRING)


def create_session_with_retries():
    session = requests.Session()
    retry_strategy = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


bot = telebot.TeleBot(TOKEN)
bot.session = create_session_with_retries()

user_data = {}


def check_telegram_connection():
    try:
        urls_to_try = [
            "https://api.telegram.org",
            "https://api.telegram.org/bot" + TOKEN[:10] + "/getMe",
            "https://api.telegram.org/bot" + TOKEN + "/getMe",
        ]

        for url in urls_to_try:
            try:
                response = requests.get(url, timeout=10)
                if response.status_code == 200:
                    print(f"✅ Соединение с Telegram API установлено через {url}")
                    return True
            except Exception:
                continue

        print("❌ Не удалось подключиться к Telegram API")
        return False
    except Exception as e:
        print(f"❌ Ошибка при проверке соединения: {e}")
        return False


# =========================
# DB helpers
# =========================
def ensure_user(user_id, username):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        IF NOT EXISTS (SELECT 1 FROM dbo.users WHERE user_id = ?)
        BEGIN
            INSERT INTO dbo.users (user_id, username, created_at)
            VALUES (?, ?, ?)
        END
        """,
        user_id,
        user_id,
        username,
        datetime.now(),
    )
    conn.commit()
    conn.close()


def get_category_id(category_name):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        IF NOT EXISTS (SELECT 1 FROM dbo.categories WHERE name = ?)
        BEGIN
            INSERT INTO dbo.categories (name) VALUES (?)
        END
        """,
        category_name,
        category_name,
    )
    conn.commit()
    cursor.execute("SELECT id FROM dbo.categories WHERE name = ?", category_name)
    row = cursor.fetchone()
    conn.close()
    return row[0]


def get_transaction_type_id(type_name):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM dbo.transaction_types WHERE name = ?", type_name)
    row = cursor.fetchone()
    conn.close()
    if row:
        return row[0]
    raise ValueError(f"Тип транзакции не найден: {type_name}")


def log_notification(user_id, payment_id, message_text):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO dbo.notifications_log
        (user_id, recurring_payment_id, sent_at, message_text)
        VALUES (?, ?, ?, ?)
        """,
        user_id,
        payment_id,
        datetime.now(),
        message_text,
    )
    conn.commit()
    conn.close()


def save_transaction(user_id, amount, category, description, trans_type):
    category_id = get_category_id(category)
    type_id = get_transaction_type_id(trans_type)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO dbo.transactions
        (user_id, amount, category_id, type_id, description, [date])
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        user_id,
        amount,
        category_id,
        type_id,
        description,
        datetime.now(),
    )
    conn.commit()
    conn.close()


# =========================
# Business logic
# =========================
def categorize_transaction(description):
    description = description.lower()

    categories = {
        "еда": ["кофе", "еда", "ресторан", "кафе", "продукты", "обед", "пицца", "бургер", "суши", "пиццерия"],
        "транспорт": ["такси", "метро", "автобус", "бензин", "транспорт", "проезд", "uber", "яндекс.такси"],
        "жилье": ["квартплата", "коммуналка", "аренда", "жкх", "квартира", "электричество", "вода"],
        "связь": ["телефон", "интернет", "связь", "мтс", "билайн", "мегафон", "теле2"],
        "развлечения": ["кино", "театр", "игры", "steam", "развлечения", "клуб", "боулинг", "караоке"],
        "одежда": ["одежда", "обувь", "кроссовки", "куртка", "джинсы", "футболка"],
        "здоровье": ["аптека", "лекарства", "врач", "больница", "здоровье", "стоматолог"],
        "образование": ["книги", "курсы", "образование", "учеба", "школа", "университет"],
        "подарки": ["подарок", "цветы", "сувенир", "открытка"],
        "спорт": ["спортзал", "фитнес", "тренировка", "бассейн", "йога"],
    }

    for category, keywords in categories.items():
        for keyword in keywords:
            if keyword in description:
                return category.capitalize()

    return "Прочее"


def check_budget_after_expense(user_id, category, expense_amount=0):
    conn = get_db()
    cursor = conn.cursor()

    current_month = datetime.now().strftime("%Y-%m")
    month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    cursor.execute(
        """
        SELECT amount
        FROM dbo.budgets
        WHERE user_id = ? AND category = ? AND [month] = ?
        """,
        user_id,
        category,
        current_month,
    )
    budget_row = cursor.fetchone()

    if not budget_row:
        conn.close()
        return None

    budget_amount = float(budget_row[0])

    cursor.execute(
        """
        SELECT SUM(ABS(t.amount))
        FROM dbo.transactions t
        JOIN dbo.categories c ON t.category_id = c.id
        JOIN dbo.transaction_types tt ON t.type_id = tt.id
        WHERE t.user_id = ?
          AND c.name = ?
          AND tt.name = 'expense'
          AND t.[date] >= ?
        """,
        user_id,
        category,
        month_start,
    )
    result = cursor.fetchone()
    total_spent = float(result[0]) if result and result[0] is not None else 0.0
    conn.close()

    percent_used = (total_spent / budget_amount) * 100 if budget_amount > 0 else 0
    remaining = budget_amount - total_spent

    if total_spent > budget_amount:
        return (
            f"❌ Бюджет на категорию '{category}' ПРЕВЫШЕН!\n"
            f"Потрачено: {total_spent:,.0f} ₽ из {budget_amount:,.0f} ₽\n"
            f"Перерасход: {total_spent - budget_amount:,.0f} ₽"
        )
    if percent_used >= 90:
        return (
            f"⚠️ Внимание! Бюджет на категорию '{category}' почти исчерпан!\n"
            f"Потрачено: {total_spent:,.0f} ₽ из {budget_amount:,.0f} ₽ ({percent_used:.1f}%)\n"
            f"Осталось: {remaining:,.0f} ₽"
        )
    if percent_used >= 75:
        return (
            f"ℹ️ Бюджет на категорию '{category}' использован на {percent_used:.1f}%\n"
            f"Потрачено: {total_spent:,.0f} ₽ из {budget_amount:,.0f} ₽\n"
            f"Осталось: {remaining:,.0f} ₽"
        )
    return None


def get_user_stats():
    conn = get_db()
    cursor = conn.cursor()

    today = datetime.now().date()
    week_ago = datetime.now() - timedelta(days=7)
    month_ago = datetime.now() - timedelta(days=30)

    cursor.execute("SELECT COUNT(*) FROM dbo.users")
    total_users = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM dbo.users WHERE CAST(created_at AS DATE) = ?", today)
    new_today = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM dbo.users WHERE created_at >= ?", week_ago)
    new_week = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM dbo.users WHERE created_at >= ?", month_ago)
    new_month = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(DISTINCT user_id) FROM dbo.transactions WHERE [date] >= ?", week_ago)
    active_week = cursor.fetchone()[0] or 0

    cursor.execute("SELECT COUNT(*) FROM dbo.transactions")
    total_transactions = cursor.fetchone()[0]

    cursor.execute(
        """
        SELECT TOP 5 u.user_id, u.username, COUNT(t.id) AS trans_count
        FROM dbo.users u
        LEFT JOIN dbo.transactions t ON u.user_id = t.user_id
        GROUP BY u.user_id, u.username
        ORDER BY trans_count DESC, u.user_id ASC
        """
    )
    top_users = cursor.fetchall()

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM dbo.users u
        LEFT JOIN dbo.transactions t ON u.user_id = t.user_id
        WHERE t.id IS NULL
        """
    )
    inactive_users = cursor.fetchone()[0]

    conn.close()

    return {
        "total": total_users,
        "new_today": new_today,
        "new_week": new_week,
        "new_month": new_month,
        "active_week": active_week,
        "total_transactions": total_transactions,
        "top_users": top_users,
        "inactive_users": inactive_users,
    }


def optimize_database():
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DBCC SHRINKDATABASE (FinanceBotDB)")
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


# =========================
# Keyboards
# =========================
def main_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        types.KeyboardButton("💰 Доход"),
        types.KeyboardButton("💸 Расход"),
        types.KeyboardButton("📊 Отчет"),
        types.KeyboardButton("📁 Категории"),
        types.KeyboardButton("🎯 Бюджеты"),
        types.KeyboardButton("⏰ Регулярные"),
        types.KeyboardButton("⚙️ Настройки"),
        types.KeyboardButton("❓ Помощь"),
    ]
    keyboard.add(*buttons)
    return keyboard


def report_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        types.KeyboardButton("📅 Сегодня"),
        types.KeyboardButton("📆 Неделя"),
        types.KeyboardButton("📊 Месяц"),
        types.KeyboardButton("📈 График"),
        types.KeyboardButton("💰 Баланс"),
        types.KeyboardButton("🔮 Прогноз"),
        types.KeyboardButton("🔙 Назад"),
    ]
    keyboard.add(*buttons)
    return keyboard


def settings_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        types.KeyboardButton("📤 Экспорт данных"),
        types.KeyboardButton("🗑️ Очистить историю"),
        types.KeyboardButton("🔙 Главное меню"),
    ]
    keyboard.add(*buttons)
    return keyboard


def recurring_menu_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = [
        types.KeyboardButton("➕ Добавить"),
        types.KeyboardButton("📋 Список"),
        types.KeyboardButton("❌ Удалить"),
        types.KeyboardButton("🔙 Назад"),
    ]
    keyboard.add(*buttons)
    return keyboard


def clear_history_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        types.KeyboardButton("🗑️ За месяц"),
        types.KeyboardButton("🗑️ За год"),
        types.KeyboardButton("🗑️ Всю историю"),
        types.KeyboardButton("🔙 Назад"),
    ]
    keyboard.add(*buttons)
    return keyboard


def confirm_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        types.KeyboardButton("✅ Да, удалить"),
        types.KeyboardButton("❌ Нет, отмена"),
    ]
    keyboard.add(*buttons)
    return keyboard


# =========================
# Commands
# =========================
@bot.message_handler(commands=["start"])
def start(message):
    user_id = message.from_user.id
    username = message.from_user.username or "NoUsername"
    ensure_user(user_id, username)

    welcome_text = (
        "👋 Добро пожаловать в Finance Bot!\n\n"
        "Я помогу вам управлять личными финансами:\n"
        "💰 Учитывать доходы и расходы\n"
        "📊 Анализировать траты\n"
        "🎯 Устанавливать бюджеты\n"
        "🔮 Прогнозировать расходы\n"
        "⏰ Напоминать о регулярных платежах\n\n"
        "Выберите действие в меню:"
    )
    bot.send_message(message.chat.id, welcome_text, reply_markup=main_keyboard())


@bot.message_handler(commands=["help"])
def help_command(message):
    help_text = (
        "📚 Справка по командам:\n\n"
        "💰 Доход/Расход - добавить операцию\n"
        "📊 Отчет - показать статистику\n"
        "📁 Категории - показать статистику по категориям\n"
        "🎯 Бюджеты - установить лимиты\n"
        "⏰ Регулярные - настроить регулярные платежи\n"
        "⚙️ Настройки - экспорт и очистка истории\n\n"
        "Примеры:\n"
        "• +5000 зарплата\n"
        "• 300 кофе\n"
        "• -1500 такси\n"
    )
    bot.send_message(message.chat.id, help_text)


@bot.message_handler(commands=["stats"])
def stats_command(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "❌ У вас нет прав для этой команды")
        return

    stats = get_user_stats()
    text = (
        "📊 Статистика бота\n\n"
        f"👥 Всего пользователей: {stats['total']}\n"
        f"🆕 Новых за сегодня: {stats['new_today']}\n"
        f"📅 Новых за неделю: {stats['new_week']}\n"
        f"📆 Новых за месяц: {stats['new_month']}\n"
        f"⚡ Активных за неделю: {stats['active_week']}\n"
        f"📝 Всего транзакций: {stats['total_transactions']}\n"
        f"😴 Без транзакций: {stats['inactive_users']}\n\n"
        "🏆 Топ пользователей:\n"
    )

    for user_id, username, trans_count in stats["top_users"]:
        name = f"@{username}" if username != "NoUsername" else f"ID:{user_id}"
        text += f"• {name}: {trans_count} транзакций\n"

    bot.send_message(message.chat.id, text)


@bot.message_handler(commands=["optimize"])
def optimize_command(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "❌ У вас нет прав для этой команды")
        return

    msg = bot.send_message(message.chat.id, "⏳ Оптимизация базы данных...")
    result = optimize_database()
    text = "✅ Обслуживание БД выполнено" if result["success"] else f"❌ Ошибка при оптимизации:\n{result['error']}"
    bot.edit_message_text(text, message.chat.id, msg.message_id)


# =========================
# Reports
# =========================
def handle_report(user_id, period):
    now = datetime.now()

    if period == "🔙 Назад":
        user_data.pop(user_id, None)
        bot.send_message(user_id, "Главное меню:", reply_markup=main_keyboard())
        return

    if period == "📅 Сегодня":
        start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = now.replace(hour=23, minute=59, second=59, microsecond=0)
        title = "за сегодня"
    elif period == "📆 Неделя":
        start_date = now - timedelta(days=7)
        end_date = now.replace(hour=23, minute=59, second=59, microsecond=0)
        title = "за последние 7 дней"
    elif period == "📊 Месяц":
        start_date = now - timedelta(days=30)
        end_date = now.replace(hour=23, minute=59, second=59, microsecond=0)
        title = "за последние 30 дней"
    elif period == "📈 График":
        user_data.pop(user_id, None)
        send_chart(user_id)
        return
    elif period == "💰 Баланс":
        user_data.pop(user_id, None)
        show_balance(user_id)
        return
    elif period == "🔮 Прогноз":
        user_data.pop(user_id, None)
        bot.send_message(user_id, budget_forecast(user_id), reply_markup=main_keyboard())
        return
    else:
        bot.send_message(user_id, "Выберите период из меню")
        return

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT SUM(t.amount)
        FROM dbo.transactions t
        JOIN dbo.transaction_types tt ON t.type_id = tt.id
        WHERE t.user_id = ?
          AND tt.name = 'income'
          AND t.[date] BETWEEN ? AND ?
        """,
        user_id,
        start_date,
        end_date,
    )
    income_result = cursor.fetchone()
    income = float(income_result[0]) if income_result and income_result[0] is not None else 0.0

    cursor.execute(
        """
        SELECT c.name, SUM(t.amount)
        FROM dbo.transactions t
        JOIN dbo.categories c ON t.category_id = c.id
        JOIN dbo.transaction_types tt ON t.type_id = tt.id
        WHERE t.user_id = ?
          AND tt.name = 'expense'
          AND t.[date] BETWEEN ? AND ?
        GROUP BY c.name
        ORDER BY SUM(t.amount)
        """,
        user_id,
        start_date,
        end_date,
    )
    expenses = cursor.fetchall()

    cursor.execute(
        """
        SELECT TOP 5 t.[date], t.amount, c.name, t.description, tt.name
        FROM dbo.transactions t
        JOIN dbo.categories c ON t.category_id = c.id
        JOIN dbo.transaction_types tt ON t.type_id = tt.id
        WHERE t.user_id = ?
          AND t.[date] BETWEEN ? AND ?
        ORDER BY t.[date] DESC
        """,
        user_id,
        start_date,
        end_date,
    )
    all_transactions = cursor.fetchall()
    conn.close()

    text = f"📊 Отчет {title}\n\n"
    text += f"💰 Доход: {income:.0f} ₽\n"

    total_expense = sum(abs(float(row[1])) for row in expenses)
    text += f"💸 Расход: {total_expense:.0f} ₽\n"
    text += f"💎 Остаток: {income - total_expense:.0f} ₽\n\n"

    if expenses:
        text += "Расходы по категориям:\n"
        for category, amount in expenses:
            amount = abs(float(amount))
            percent = (amount / total_expense) * 100 if total_expense > 0 else 0
            text += f"• {category}: {amount:.0f} ₽ ({percent:.1f}%)\n"
    else:
        text += "Нет расходов за выбранный период\n"

    if all_transactions:
        text += "\nПоследние транзакции:\n"
        for date_value, amount, category, desc, type_name in all_transactions:
            emoji = "💰" if type_name == "income" else "💸"
            text += f"{emoji} {date_value.strftime('%d.%m %H:%M')} | {category}: {abs(float(amount)):.0f} ₽ | {desc}\n"

    user_data.pop(user_id, None)
    bot.send_message(user_id, text, reply_markup=main_keyboard())


def budget_forecast(user_id):
    conn = get_db()
    cursor = conn.cursor()

    now = datetime.now()
    today = now.day
    if now.month == 12:
        days_in_month = (datetime(now.year + 1, 1, 1) - timedelta(days=1)).day
    else:
        days_in_month = (datetime(now.year, now.month + 1, 1) - timedelta(days=1)).day

    start_3_months = now - timedelta(days=90)

    cursor.execute(
        """
        WITH monthly AS (
            SELECT
                c.name AS category_name,
                FORMAT(t.[date], 'yyyy-MM') AS month_key,
                SUM(ABS(t.amount)) AS monthly_total
            FROM dbo.transactions t
            JOIN dbo.categories c ON t.category_id = c.id
            JOIN dbo.transaction_types tt ON t.type_id = tt.id
            WHERE t.user_id = ?
              AND tt.name = 'expense'
              AND t.[date] >= ?
            GROUP BY c.name, FORMAT(t.[date], 'yyyy-MM')
        )
        SELECT category_name, AVG(monthly_total) AS avg_monthly
        FROM monthly
        GROUP BY category_name
        ORDER BY AVG(monthly_total) DESC
        """,
        user_id,
        start_3_months,
    )
    averages = cursor.fetchall()

    if not averages:
        conn.close()
        return "❌ Недостаточно данных для прогноза. Добавьте больше транзакций за последние 3 месяца."

    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    cursor.execute(
        """
        SELECT c.name, SUM(ABS(t.amount)) AS spent
        FROM dbo.transactions t
        JOIN dbo.categories c ON t.category_id = c.id
        JOIN dbo.transaction_types tt ON t.type_id = tt.id
        WHERE t.user_id = ?
          AND tt.name = 'expense'
          AND t.[date] >= ?
        GROUP BY c.name
        """,
        user_id,
        month_start,
    )
    current_spent = {row[0]: float(row[1]) for row in cursor.fetchall()}
    conn.close()

    text = "🔮 Прогноз бюджета на месяц\n\n"
    text += f"📅 Сегодня: {now.strftime('%d.%m.%Y')}\n"
    text += f"⚡️ Прогноз на {days_in_month} дней\n\n"

    total_projected = 0
    total_avg = 0
    total_spent = 0

    category_emoji = {
        "Еда": "🍔",
        "Транспорт": "🚗",
        "Жилье": "🏠",
        "Связь": "📱",
        "Развлечения": "🎮",
        "Одежда": "👕",
        "Здоровье": "💊",
        "Образование": "📚",
        "Подарки": "🎁",
        "Спорт": "⚽️",
        "Прочее": "📦",
    }

    for category, avg in averages:
        avg = float(avg)
        spent = current_spent.get(category, 0.0)
        projected = (spent / today) * days_in_month if today > 0 and spent > 0 else 0.0

        total_spent += spent
        total_projected += projected
        total_avg += avg

        text += f"{category_emoji.get(category, '📌')} {category}\n"
        text += f"  • Потрачено: {spent:.0f} ₽\n"
        text += f"  • Среднее за месяц: {avg:.0f} ₽\n"
        text += f"  • Прогноз: {projected:.0f} ₽\n"

        if projected > avg * 1.2:
            text += "  ⚠️ Высокий риск перерасхода\n\n"
        elif projected < avg * 0.8:
            text += "  ✅ Экономия\n\n"
        else:
            text += "  ℹ️ В рамках нормы\n\n"

    text += "📊 Общий итог\n"
    text += f"  • Потрачено всего: {total_spent:.0f} ₽\n"
    text += f"  • Средний расход: {total_avg:.0f} ₽\n"
    text += f"  • Прогноз на месяц: {total_projected:.0f} ₽\n"

    if total_projected > total_avg * 1.2:
        text += "⚠️ Внимание! Общий прогноз превышает средние расходы"
    elif total_projected < total_avg * 0.8:
        text += "✅ Отличная экономия!"

    return text


def process_transaction_input(user_id, text, action):
    try:
        parts = text.split()
        if len(parts) < 2:
            bot.send_message(user_id, "Неверный формат. Используйте: 'сумма описание'")
            return

        amount_str = parts[0].replace("+", "").replace("-", "")
        amount = float(amount_str)
        trans_type = "income" if action == "waiting_for_income" else "expense"
        russian_type = "Доход" if trans_type == "income" else "Расход"

        amount = abs(amount) if trans_type == "income" else -abs(amount)
        description = " ".join(parts[1:])
        category = categorize_transaction(description)

        save_transaction(user_id, amount, category, description, trans_type)
        user_data.pop(user_id, None)

        bot.send_message(
            user_id,
            f"✅ {russian_type} добавлен:\n"
            f"Сумма: {abs(amount)} ₽\n"
            f"Категория: {category}\n"
            f"Описание: {description}",
            reply_markup=main_keyboard(),
        )

        if trans_type == "expense":
            budget_warning = check_budget_after_expense(user_id, category, abs(amount))
            if budget_warning:
                bot.send_message(user_id, budget_warning)

    except ValueError:
        bot.send_message(user_id, "Неверный формат суммы. Используйте число")
    except Exception as e:
        bot.send_message(user_id, f"Ошибка: {str(e)}", reply_markup=main_keyboard())
        user_data.pop(user_id, None)


def show_categories(user_id):
    conn = get_db()
    cursor = conn.cursor()
    month_ago = datetime.now() - timedelta(days=30)

    cursor.execute(
        """
        SELECT c.name, SUM(t.amount) AS total
        FROM dbo.transactions t
        JOIN dbo.categories c ON t.category_id = c.id
        JOIN dbo.transaction_types tt ON t.type_id = tt.id
        WHERE t.user_id = ?
          AND tt.name = 'expense'
          AND t.[date] >= ?
        GROUP BY c.name
        ORDER BY SUM(t.amount)
        """,
        user_id,
        month_ago,
    )
    expenses = cursor.fetchall()

    cursor.execute(
        """
        SELECT SUM(t.amount)
        FROM dbo.transactions t
        JOIN dbo.transaction_types tt ON t.type_id = tt.id
        WHERE t.user_id = ?
          AND tt.name = 'income'
          AND t.[date] >= ?
        """,
        user_id,
        month_ago,
    )
    income_result = cursor.fetchone()
    income = float(income_result[0]) if income_result and income_result[0] is not None else 0.0
    conn.close()

    text = "📁 Ваши категории расходов за последние 30 дней:\n\n"
    if expenses:
        total_expenses = sum(abs(float(row[1])) for row in expenses)
        for category, amount in expenses:
            amount = abs(float(amount))
            percent = (amount / total_expenses) * 100 if total_expenses > 0 else 0
            text += f"• {category}: {amount:.0f} ₽ ({percent:.1f}%)\n"
        text += f"\n💰 Доход за период: {income:.0f} ₽"
        text += f"\n💸 Расход за период: {total_expenses:.0f} ₽"
        text += f"\n💎 Остаток: {income - total_expenses:.0f} ₽"
    else:
        text += "Нет расходов за последние 30 дней"

    bot.send_message(user_id, text, reply_markup=main_keyboard())


def send_chart(user_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        month_ago = datetime.now() - timedelta(days=30)

        cursor.execute(
            """
            SELECT c.name, SUM(t.amount) AS total
            FROM dbo.transactions t
            JOIN dbo.categories c ON t.category_id = c.id
            JOIN dbo.transaction_types tt ON t.type_id = tt.id
            WHERE t.user_id = ?
              AND tt.name = 'expense'
              AND t.[date] >= ?
            GROUP BY c.name
            """,
            user_id,
            month_ago,
        )
        data = cursor.fetchall()
        conn.close()

        if not data:
            bot.send_message(user_id, "Нет данных для графика", reply_markup=main_keyboard())
            return

        categories = [row[0] for row in data]
        amounts = [abs(float(row[1])) for row in data]

        plt.figure(figsize=(8, 8))
        plt.pie(amounts, labels=categories, autopct="%1.1f%%")
        plt.title("Расходы за последние 30 дней")

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100, bbox_inches="tight")
        buf.seek(0)
        bot.send_photo(user_id, buf, reply_markup=main_keyboard())
        plt.close("all")

    except Exception as e:
        bot.send_message(user_id, f"Ошибка при создании графика: {str(e)}", reply_markup=main_keyboard())


def show_balance(user_id):
    conn = get_db()
    cursor = conn.cursor()
    month_ago = datetime.now() - timedelta(days=30)

    cursor.execute("SELECT SUM(amount) FROM dbo.transactions WHERE user_id = ?", user_id)
    total_balance_result = cursor.fetchone()
    total_balance = float(total_balance_result[0]) if total_balance_result and total_balance_result[0] is not None else 0.0

    cursor.execute(
        """
        SELECT SUM(t.amount)
        FROM dbo.transactions t
        JOIN dbo.transaction_types tt ON t.type_id = tt.id
        WHERE t.user_id = ? AND tt.name = 'income' AND t.[date] >= ?
        """,
        user_id,
        month_ago,
    )
    month_income_result = cursor.fetchone()
    month_income = float(month_income_result[0]) if month_income_result and month_income_result[0] is not None else 0.0

    cursor.execute(
        """
        SELECT SUM(ABS(t.amount))
        FROM dbo.transactions t
        JOIN dbo.transaction_types tt ON t.type_id = tt.id
        WHERE t.user_id = ? AND tt.name = 'expense' AND t.[date] >= ?
        """,
        user_id,
        month_ago,
    )
    month_expense_result = cursor.fetchone()
    month_expense = float(month_expense_result[0]) if month_expense_result and month_expense_result[0] is not None else 0.0
    conn.close()

    text = "💰 Баланс\n\n"
    text += f"Общий баланс: {total_balance:.0f} ₽\n"
    text += f"Доход за 30 дней: {month_income:.0f} ₽\n"
    text += f"Расход за 30 дней: {month_expense:.0f} ₽\n"
    text += f"Остаток за период: {month_income - month_expense:.0f} ₽"
    bot.send_message(user_id, text, reply_markup=main_keyboard())


def handle_budget(user_id, text):
    if text == "🔙 Назад":
        user_data.pop(user_id, None)
        bot.send_message(user_id, "Главное меню:", reply_markup=main_keyboard())
        return

    try:
        parts = text.split()
        if len(parts) != 2:
            bot.send_message(user_id, "Неверный формат. Используйте: 'категория сумма'")
            return

        category = parts[0].capitalize()
        amount = float(parts[1])
        month = datetime.now().strftime("%Y-%m")

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """
            MERGE dbo.budgets AS target
            USING (SELECT ? AS user_id, ? AS category, ? AS [month]) AS source
            ON target.user_id = source.user_id
               AND target.category = source.category
               AND target.[month] = source.[month]
            WHEN MATCHED THEN
                UPDATE SET amount = ?
            WHEN NOT MATCHED THEN
                INSERT (user_id, category, amount, [month])
                VALUES (?, ?, ?, ?);
            """,
            user_id,
            category,
            month,
            amount,
            user_id,
            category,
            amount,
            month,
        )
        conn.commit()
        conn.close()

        user_data.pop(user_id, None)
        budget_status = check_budget_after_expense(user_id, category, 0)

        if budget_status:
            msg = f"✅ Бюджет на '{category}' установлен: {amount:,.0f} ₽\n\n{budget_status}"
        else:
            msg = f"✅ Бюджет на '{category}' установлен: {amount:,.0f} ₽\nПока нет расходов по этой категории в этом месяце."

        bot.send_message(user_id, msg, reply_markup=main_keyboard())

    except ValueError:
        bot.send_message(user_id, "Неверный формат суммы. Используйте число")


def handle_recurring_menu(user_id, text):
    if text == "➕ Добавить":
        user_data[user_id] = {"action": "add_recurring"}
        bot.send_message(user_id, "Введите регулярный платеж в формате: 'категория сумма день'")
    elif text == "📋 Список":
        show_recurring_list(user_id)
    elif text == "❌ Удалить":
        user_data[user_id] = {"action": "delete_recurring"}
        show_recurring_list(user_id, delete_mode=True)
    elif text == "🔙 Назад":
        user_data.pop(user_id, None)
        bot.send_message(user_id, "Главное меню:", reply_markup=main_keyboard())
    else:
        bot.send_message(user_id, "Выберите действие из меню", reply_markup=recurring_menu_keyboard())


def handle_add_recurring(user_id, text):
    if text == "🔙 Назад":
        user_data[user_id] = {"action": "recurring_menu"}
        bot.send_message(user_id, "Меню регулярных платежей:", reply_markup=recurring_menu_keyboard())
        return

    try:
        parts = text.split()
        if len(parts) != 3:
            bot.send_message(user_id, "Неверный формат. Используйте: 'категория сумма день'")
            return

        category = parts[0].capitalize()
        amount = float(parts[1])
        day = int(parts[2])

        if not 1 <= day <= 31:
            bot.send_message(user_id, "День должен быть от 1 до 31")
            return

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO dbo.recurring_payments (user_id, category, amount, description, [day])
            VALUES (?, ?, ?, ?, ?)
            """,
            user_id,
            category,
            amount,
            category,
            day,
        )
        conn.commit()
        conn.close()

        user_data[user_id] = {"action": "recurring_menu"}
        bot.send_message(
            user_id,
            f"✅ Регулярный платеж добавлен:\n{category}: {amount} ₽, {day}-го числа",
            reply_markup=recurring_menu_keyboard(),
        )

    except ValueError:
        bot.send_message(user_id, "Неверный формат суммы или дня")


def handle_delete_recurring(user_id, text):
    if text == "🔙 Назад":
        user_data[user_id] = {"action": "recurring_menu"}
        bot.send_message(user_id, "Меню регулярных платежей:", reply_markup=recurring_menu_keyboard())
        return

    try:
        payment_id = int(text)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM dbo.recurring_payments WHERE id = ? AND user_id = ?", payment_id, user_id)
        conn.commit()
        deleted = cursor.rowcount
        conn.close()

        if deleted > 0:
            bot.send_message(user_id, "✅ Регулярный платеж удален")
        else:
            bot.send_message(user_id, f"❌ Платеж с ID {payment_id} не найден")

        user_data[user_id] = {"action": "recurring_menu"}
        bot.send_message(user_id, "Меню регулярных платежей:", reply_markup=recurring_menu_keyboard())

    except ValueError:
        bot.send_message(user_id, "Введите корректный ID платежа")


def show_recurring_list(user_id, delete_mode=False):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, amount, category, [day] FROM dbo.recurring_payments WHERE user_id = ? ORDER BY [day]",
        user_id,
    )
    payments = cursor.fetchall()
    conn.close()

    if not payments:
        bot.send_message(user_id, "У вас нет регулярных платежей")
        return

    text = "📋 Ваши регулярные платежи:\n\n"
    for pid, amount, category, day in payments:
        text += f"• {category}: {float(amount)} ₽ ({day}-го числа)\n"
        if delete_mode:
            text += f"  ID: {pid}\n"

    if delete_mode:
        text += "\nВведите ID платежа для удаления:"

    bot.send_message(user_id, text)


def export_data(user_id):
    try:
        conn = get_db()

        df = pd.read_sql(
            """
            SELECT
                t.[date] AS [Дата],
                CASE WHEN tt.name = 'income' THEN N'Доход' ELSE N'Расход' END AS [Тип],
                c.name AS [Категория],
                ABS(t.amount) AS [Сумма],
                t.description AS [Описание]
            FROM dbo.transactions t
            JOIN dbo.categories c ON t.category_id = c.id
            JOIN dbo.transaction_types tt ON t.type_id = tt.id
            WHERE t.user_id = ?
            ORDER BY t.[date] DESC
            """,
            conn,
            params=[user_id],
        )

        stats_df = pd.read_sql(
            """
            SELECT
                FORMAT(t.[date], 'yyyy-MM') AS [Месяц],
                SUM(CASE WHEN tt.name = 'income' THEN t.amount ELSE 0 END) AS [Доход],
                SUM(CASE WHEN tt.name = 'expense' THEN ABS(t.amount) ELSE 0 END) AS [Расход],
                SUM(t.amount) AS [Баланс]
            FROM dbo.transactions t
            JOIN dbo.transaction_types tt ON t.type_id = tt.id
            WHERE t.user_id = ?
            GROUP BY FORMAT(t.[date], 'yyyy-MM')
            ORDER BY [Месяц] DESC
            """,
            conn,
            params=[user_id],
        )

        categories_df = pd.read_sql(
            """
            SELECT
                c.name AS [Категория],
                SUM(CASE WHEN tt.name = 'expense' THEN ABS(t.amount) ELSE 0 END) AS [Потрачено],
                COUNT(*) AS [Количество транзакций]
            FROM dbo.transactions t
            JOIN dbo.categories c ON t.category_id = c.id
            JOIN dbo.transaction_types tt ON t.type_id = tt.id
            WHERE t.user_id = ?
            GROUP BY c.name
            ORDER BY [Потрачено] DESC
            """,
            conn,
            params=[user_id],
        )
        conn.close()

        if df.empty:
            bot.send_message(user_id, "❌ Нет данных для экспорта")
            return

        filename = f"finance_report_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

        with pd.ExcelWriter(filename, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Транзакции", index=False)
            if not stats_df.empty:
                stats_df.to_excel(writer, sheet_name="Статистика по месяцам", index=False)
            if not categories_df.empty:
                categories_df.to_excel(writer, sheet_name="Категории расходов", index=False)

            total_income = df[df["Тип"] == "Доход"]["Сумма"].sum()
            total_expense = df[df["Тип"] == "Расход"]["Сумма"].sum()

            summary_df = pd.DataFrame({
                "Показатель": ["Общий доход", "Общий расход", "Баланс", "Всего транзакций"],
                "Значение": [
                    f"{total_income:.2f} ₽",
                    f"{total_expense:.2f} ₽",
                    f"{total_income - total_expense:.2f} ₽",
                    len(df),
                ],
            })
            summary_df.to_excel(writer, sheet_name="Сводка", index=False)

        with open(filename, "rb") as f:
            bot.send_document(
                user_id,
                f,
                caption=(
                    "📊 Финансовый отчет\n\n"
                    f"✅ Экспортировано транзакций: {len(df)}\n"
                    f"📅 Данные на: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
                    "📁 Формат: Excel (.xlsx)"
                ),
            )

        os.remove(filename)

    except Exception as e:
        bot.send_message(user_id, f"❌ Ошибка при экспорте: {str(e)}")


def clear_transactions(user_id, period="all"):
    conn = get_db()
    cursor = conn.cursor()

    if period == "all":
        cursor.execute("DELETE FROM dbo.transactions WHERE user_id = ?", user_id)
        period_text = "все"
    elif period == "month":
        month_ago = datetime.now() - timedelta(days=30)
        cursor.execute("DELETE FROM dbo.transactions WHERE user_id = ? AND [date] >= ?", user_id, month_ago)
        period_text = "за последний месяц"
    elif period == "year":
        year_ago = datetime.now() - timedelta(days=365)
        cursor.execute("DELETE FROM dbo.transactions WHERE user_id = ? AND [date] >= ?", user_id, year_ago)
        period_text = "за последний год"
    else:
        conn.close()
        return None, None

    deleted_count = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted_count, period_text


def handle_clear_history(user_id, text):
    if text == "🗑️ За месяц":
        user_data[user_id] = {"action": "clear_history", "period": "month"}
        bot.send_message(user_id, "⚠️ Вы действительно хотите удалить ВСЕ транзакции за последний месяц?", reply_markup=confirm_keyboard())
    elif text == "🗑️ За год":
        user_data[user_id] = {"action": "clear_history", "period": "year"}
        bot.send_message(user_id, "⚠️ Вы действительно хотите удалить ВСЕ транзакции за последний год?", reply_markup=confirm_keyboard())
    elif text == "🗑️ Всю историю":
        user_data[user_id] = {"action": "clear_history", "period": "all"}
        bot.send_message(user_id, "⚠️ Вы действительно хотите удалить ВСЮ историю транзакций?", reply_markup=confirm_keyboard())
    elif text == "✅ Да, удалить":
        if user_id in user_data and user_data[user_id].get("action") == "clear_history":
            period = user_data[user_id].get("period", "all")
            deleted_count, period_text = clear_transactions(user_id, period)
            if deleted_count and deleted_count > 0:
                bot.send_message(
                    user_id,
                    f"✅ История очищена!\n\nУдалено транзакций: {deleted_count}\nПериод: {period_text}",
                    reply_markup=main_keyboard(),
                )
            else:
                bot.send_message(user_id, "ℹ️ Нет транзакций для удаления", reply_markup=main_keyboard())
            user_data.pop(user_id, None)
    elif text == "❌ Нет, отмена":
        user_data.pop(user_id, None)
        bot.send_message(user_id, "❌ Удаление отменено", reply_markup=main_keyboard())
    elif text == "🔙 Назад":
        user_data.pop(user_id, None)
        bot.send_message(user_id, "Главное меню:", reply_markup=main_keyboard())


def check_recurring_payments():
    now = datetime.now()
    today = now.day

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, user_id, amount, category, description, last_reminded
        FROM dbo.recurring_payments
        WHERE [day] = ?
        """,
        today,
    )
    rows = cursor.fetchall()

    for pid, user_id, amount, category, description, last_reminded in rows:
        if last_reminded and getattr(last_reminded, "date", lambda: None)() == now.date():
            continue

        try:
            message_text = (
                f"⏰ Напоминание о регулярном платеже!\n\n"
                f"Категория: {category}\n"
                f"Сумма: {float(amount)} ₽\n"
                f"Описание: {description}\n\n"
                f"Не забудьте оплатить сегодня!"
            )

            bot.send_message(user_id, message_text)

            cursor.execute(
                "UPDATE dbo.recurring_payments SET last_reminded = ? WHERE id = ?",
                now,
                pid,
            )
            conn.commit()

            log_notification(user_id, pid, message_text)

        except Exception as e:
            print(f"Ошибка отправки напоминания пользователю {user_id}: {e}")

    conn.close()


# =========================
# Router
# =========================
@bot.message_handler(func=lambda message: True)
def handle_message(message):
    user_id = message.from_user.id
    username = message.from_user.username or "NoUsername"
    text = message.text

    ensure_user(user_id, username)

    if text in ["🔙 Главное меню", "🔙 Назад", "❓ Помощь"]:
        user_data.pop(user_id, None)
        if text == "❓ Помощь":
            help_command(message)
        else:
            bot.send_message(user_id, "Главное меню:", reply_markup=main_keyboard())
        return

    if text in ["💰 Доход", "💸 Расход", "📊 Отчет", "📁 Категории", "🎯 Бюджеты", "⏰ Регулярные", "⚙️ Настройки"]:
        user_data.pop(user_id, None)

    if user_id in user_data:
        action = user_data[user_id].get("action")
        if action in ["waiting_for_income", "waiting_for_expense"]:
            process_transaction_input(user_id, text, action)
            return
        if action == "report":
            handle_report(user_id, text)
            return
        if action == "budget":
            handle_budget(user_id, text)
            return
        if action == "recurring_menu":
            handle_recurring_menu(user_id, text)
            return
        if action == "add_recurring":
            handle_add_recurring(user_id, text)
            return
        if action == "delete_recurring":
            handle_delete_recurring(user_id, text)
            return
        if action == "clear_history":
            handle_clear_history(user_id, text)
            return

    if text == "💰 Доход":
        user_data[user_id] = {"action": "waiting_for_income", "type": "income"}
        bot.send_message(user_id, "Введите доход (например: '+5000 зарплата')")
    elif text == "💸 Расход":
        user_data[user_id] = {"action": "waiting_for_expense", "type": "expense"}
        bot.send_message(user_id, "Введите расход (например: '300 кофе')")
    elif text == "📊 Отчет":
        user_data[user_id] = {"action": "report"}
        bot.send_message(user_id, "Выберите период для отчета:", reply_markup=report_keyboard())
    elif text == "📁 Категории":
        show_categories(user_id)
    elif text == "🎯 Бюджеты":
        user_data[user_id] = {"action": "budget"}
        bot.send_message(user_id, "Введите бюджет в формате 'категория сумма' (например: 'Еда 10000')")
    elif text == "⏰ Регулярные":
        user_data[user_id] = {"action": "recurring_menu"}
        bot.send_message(user_id, "⏰ Регулярные платежи\n\nВыберите действие:", reply_markup=recurring_menu_keyboard())
    elif text == "⚙️ Настройки":
        bot.send_message(user_id, "⚙️ Настройки\n\nВыберите действие:", reply_markup=settings_keyboard())
    elif text == "📤 Экспорт данных":
        export_data(user_id)
    elif text == "🗑️ Очистить историю":
        user_data[user_id] = {"action": "clear_history"}
        bot.send_message(user_id, "🗑️ Очистка истории\n\nВыберите период для удаления:", reply_markup=clear_history_keyboard())
    else:
        bot.send_message(user_id, "Пожалуйста, используйте кнопки меню", reply_markup=main_keyboard())


if __name__ == "__main__":
    print("Бот запущен...")
    print(f"ID администратора: {ADMIN_ID}")

    if not check_telegram_connection():
        print("⚠️ Проблемы с соединением, но пробуем запустить...")

    def recurring_checker():
        while True:
            try:
                check_recurring_payments()
            except Exception as e:
                print(f"Ошибка в проверке регулярных платежей: {e}")
            time.sleep(3600)

    thread = threading.Thread(target=recurring_checker, daemon=True)
    thread.start()

    try:
        bot.polling(none_stop=True, interval=1, timeout=30, long_polling_timeout=25)
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        print("🔄 Перезапуск через 10 секунд...")
        time.sleep(10)
