import telebot
from telebot import types
import sqlite3
from datetime import datetime, timedelta
import matplotlib.pyplot as plt
import io
import csv
import os
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Токен бота
TOKEN = "8508761925:AAGMzdBFXO9qHYsCl6tBUSB9BPgFlSYzVfA"

# Настройка сессии с повторными попытками
def create_session_with_retries():
    session = requests.Session()
    retry_strategy = Retry(
        total=5,  # максимум 5 попыток
        backoff_factor=1,  # пауза между попытками: 1, 2, 4, 8, 16 секунд
        status_forcelist=[429, 500, 502, 503, 504],  # коды для повторных попыток
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

# Создаем бота с кастомной сессией
bot = telebot.TeleBot(TOKEN)
bot.session = create_session_with_retries()

# ID администратора
ADMIN_ID = 1376134977

# Функция для проверки соединения с Telegram
def check_telegram_connection():
    """Проверяет доступность API Telegram"""
    try:
        # Пробуем разные DNS
        urls_to_try = [
            "https://api.telegram.org",
            "https://api.telegram.org/bot" + TOKEN[:10] + "/getMe",
            "https://api.telegram.org/bot" + TOKEN + "/getMe"
        ]
        
        for url in urls_to_try:
            try:
                response = requests.get(url, timeout=10)
                if response.status_code == 200:
                    print(f"✅ Соединение с Telegram API установлено через {url}")
                    return True
            except:
                continue
        
        # Если ничего не работает, пробуем альтернативные DNS
        print("❌ Не удалось подключиться к Telegram API")
        print("🔄 Попробуйте использовать VPN или сменить DNS")
        return False
    except Exception as e:
        print(f"❌ Ошибка при проверке соединения: {e}")
        return False

# Создание базы данных
def init_db():
    conn = sqlite3.connect('finance_bot.db')
    c = conn.cursor()
    
    # Таблица пользователей
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, username TEXT, created_at TEXT)''')
    
    # Таблица транзакций
    c.execute('''CREATE TABLE IF NOT EXISTS transactions
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  amount REAL,
                  category TEXT,
                  type TEXT,
                  description TEXT,
                  date TEXT,
                  FOREIGN KEY (user_id) REFERENCES users (user_id))''')
    
    # Таблица бюджетов
    c.execute('''CREATE TABLE IF NOT EXISTS budgets
                 (user_id INTEGER,
                  category TEXT,
                  amount REAL,
                  month TEXT,
                  PRIMARY KEY (user_id, category, month))''')
    
    # Таблица регулярных платежей
    c.execute('''CREATE TABLE IF NOT EXISTS recurring_payments
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  amount REAL,
                  category TEXT,
                  description TEXT,
                  day INTEGER,
                  last_reminded TEXT)''')
    
    conn.commit()
    conn.close()

# Инициализация базы данных
init_db()

# Функция для получения соединения с БД
def get_db():
    return sqlite3.connect('finance_bot.db')

# Функция для проверки существования пользователя
def ensure_user(user_id, username):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, username, created_at) VALUES (?, ?, ?)",
              (user_id, username, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

# Функция для проверки бюджета после добавления расхода
def check_budget_after_expense(user_id, category, expense_amount):
    """
    Проверяет состояние бюджета после добавления расхода
    Возвращает текст предупреждения или None
    """
    conn = get_db()
    c = conn.cursor()
    
    # Текущий месяц
    current_month = datetime.now().strftime("%Y-%m")
    month_start = datetime.now().replace(day=1).strftime("%Y-%m-%d 00:00:00")
    
    # Получаем бюджет на эту категорию в текущем месяце
    c.execute("""SELECT amount FROM budgets 
                 WHERE user_id = ? AND category = ? AND month = ?""",
              (user_id, category, current_month))
    budget_row = c.fetchone()
    
    if not budget_row:
        conn.close()
        return None
    
    budget_amount = budget_row[0]
    
    # Считаем все расходы по этой категории за текущий месяц (включая только что добавленный)
    c.execute("""SELECT SUM(ABS(amount)) FROM transactions 
                 WHERE user_id = ? AND category = ? AND amount < 0 
                 AND datetime(date) >= datetime(?)""",
              (user_id, category, month_start))
    total_spent = c.fetchone()[0] or 0
    
    conn.close()
    
    # Рассчитываем процент использования
    percent_used = (total_spent / budget_amount) * 100 if budget_amount > 0 else 0
    remaining = budget_amount - total_spent
    
    # Формируем предупреждение в зависимости от процента использования
    if total_spent > budget_amount:
        warning = (
            f"❌ Бюджет на категорию '{category}' ПРЕВЫШЕН!\n"
            f"Потрачено: {total_spent:,.0f} ₽ из {budget_amount:,.0f} ₽\n"
            f"Перерасход: {total_spent - budget_amount:,.0f} ₽"
        )
    elif percent_used >= 90:
        warning = (
            f"⚠️ Внимание! Бюджет на категорию '{category}' почти исчерпан!\n"
            f"Потрачено: {total_spent:,.0f} ₽ из {budget_amount:,.0f} ₽ ({percent_used:.1f}%)\n"
            f"Осталось: {remaining:,.0f} ₽"
        )
    elif percent_used >= 75:
        warning = (
            f"ℹ️ Бюджет на категорию '{category}' использован на {percent_used:.1f}%\n"
            f"Потрачено: {total_spent:,.0f} ₽ из {budget_amount:,.0f} ₽\n"
            f"Осталось: {remaining:,.0f} ₽"
        )
    else:
        return None
    
    return warning

# Функция для получения статистики пользователей
def get_user_stats():
    """Получить статистику по пользователям"""
    conn = get_db()
    c = conn.cursor()
    
    # Общее количество пользователей
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    
    # Новые пользователи за сегодня
    today = datetime.now().strftime("%Y-%m-%d")
    c.execute("SELECT COUNT(*) FROM users WHERE date(created_at) = ?", (today,))
    new_today = c.fetchone()[0]
    
    # Новые пользователи за неделю
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    c.execute("SELECT COUNT(*) FROM users WHERE date(created_at) >= ?", (week_ago,))
    new_week = c.fetchone()[0]
    
    # Новые пользователи за месяц
    month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    c.execute("SELECT COUNT(*) FROM users WHERE date(created_at) >= ?", (month_ago,))
    new_month = c.fetchone()[0]
    
    # Активные пользователи (есть транзакции за последние 7 дней)
    c.execute("""
        SELECT COUNT(DISTINCT user_id) 
        FROM transactions 
        WHERE date >= ?
    """, (week_ago,))
    active_week = c.fetchone()[0] or 0
    
    # Общее количество транзакций
    c.execute("SELECT COUNT(*) FROM transactions")
    total_transactions = c.fetchone()[0]
    
    # Топ пользователей по транзакциям
    c.execute("""
        SELECT u.user_id, u.username, COUNT(t.id) as trans_count
        FROM users u
        LEFT JOIN transactions t ON u.user_id = t.user_id
        GROUP BY u.user_id
        ORDER BY trans_count DESC
        LIMIT 5
    """)
    top_users = c.fetchall()
    
    # Пользователи без транзакций
    c.execute("""
        SELECT COUNT(*) FROM users u
        LEFT JOIN transactions t ON u.user_id = t.user_id
        WHERE t.id IS NULL
    """)
    inactive_users = c.fetchone()[0]
    
    conn.close()
    
    return {
        'total': total_users,
        'new_today': new_today,
        'new_week': new_week,
        'new_month': new_month,
        'active_week': active_week,
        'total_transactions': total_transactions,
        'top_users': top_users,
        'inactive_users': inactive_users
    }

# Функция для оптимизации базы данных
def optimize_database():
    """Оптимизация базы данных (VACUUM) и возврат статистики"""
    try:
        # Получаем размер до оптимизации
        size_before = os.path.getsize('finance_bot.db') / (1024 * 1024)
        
        # Получаем статистику до оптимизации
        conn = get_db()
        c = conn.cursor()
        
        # Информация о страницах
        c.execute("PRAGMA page_count")
        pages_before = c.fetchone()[0]
        c.execute("PRAGMA page_size")
        page_size = c.fetchone()[0]
        calc_size_before = (pages_before * page_size) / (1024 * 1024)
        
        # Количество свободных страниц
        c.execute("PRAGMA freelist_count")
        free_pages = c.fetchone()[0]
        free_space_mb = (free_pages * page_size) / (1024 * 1024)
        
        # Выполняем VACUUM
        c.execute("VACUUM")
        conn.commit()
        
        # Получаем размер после оптимизации
        c.execute("PRAGMA page_count")
        pages_after = c.fetchone()[0]
        calc_size_after = (pages_after * page_size) / (1024 * 1024)
        
        conn.close()
        
        # Реальный размер файла после оптимизации
        size_after = os.path.getsize('finance_bot.db') / (1024 * 1024)
        
        return {
            'success': True,
            'size_before': size_before,
            'size_after': size_after,
            'freed': size_before - size_after,
            'pages_before': pages_before,
            'pages_after': pages_after,
            'free_space_before': free_space_mb,
            'calc_size_before': calc_size_before,
            'calc_size_after': calc_size_after
        }
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }

# Клавиатура главного меню
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
        types.KeyboardButton("❓ Помощь")
    ]
    keyboard.add(*buttons)
    return keyboard

# Клавиатура для отчетов
def report_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        types.KeyboardButton("📅 Сегодня"),
        types.KeyboardButton("📆 Неделя"),
        types.KeyboardButton("📊 Месяц"),
        types.KeyboardButton("📈 График"),
        types.KeyboardButton("💰 Баланс"),
        types.KeyboardButton("🔮 Прогноз"),
        types.KeyboardButton("🔙 Назад")
    ]
    keyboard.add(*buttons)
    return keyboard

# Клавиатура настроек
def settings_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        types.KeyboardButton("📤 Экспорт данных"),
        types.KeyboardButton("🗑️ Очистить историю"),
        types.KeyboardButton("🔙 Главное меню")
    ]
    keyboard.add(*buttons)
    return keyboard

# Клавиатура для категорий
def categories_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    categories = [
        types.KeyboardButton("🍔 Еда"),
        types.KeyboardButton("🚗 Транспорт"),
        types.KeyboardButton("🏠 Жилье"),
        types.KeyboardButton("📱 Связь"),
        types.KeyboardButton("🎮 Развлечения"),
        types.KeyboardButton("👕 Одежда"),
        types.KeyboardButton("💊 Здоровье"),
        types.KeyboardButton("📚 Образование"),
        types.KeyboardButton("🔙 Назад")
    ]
    keyboard.add(*categories)
    return keyboard

# Клавиатура для меню регулярных платежей
def recurring_menu_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = [
        types.KeyboardButton("➕ Добавить"),
        types.KeyboardButton("📋 Список"),
        types.KeyboardButton("❌ Удалить"),
        types.KeyboardButton("🔙 Назад")
    ]
    keyboard.add(*buttons)
    return keyboard

# Клавиатура для очистки истории
def clear_history_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        types.KeyboardButton("🗑️ За месяц"),
        types.KeyboardButton("🗑️ За год"),
        types.KeyboardButton("🗑️ Всю историю"),
        types.KeyboardButton("🔙 Назад")
    ]
    keyboard.add(*buttons)
    return keyboard

# Клавиатура подтверждения
def confirm_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        types.KeyboardButton("✅ Да, удалить"),
        types.KeyboardButton("❌ Нет, отмена")
    ]
    keyboard.add(*buttons)
    return keyboard

# Словарь для временного хранения данных
user_data = {}

# Обработчик команды /start
@bot.message_handler(commands=['start'])
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

# Обработчик команды /help
@bot.message_handler(commands=['help'])
def help_command(message):
    help_text = (
        "📚 Справка по командам:\n\n"
        "💰 Доход/Расход - добавить операцию\n"
        "📊 Отчет - показать статистику (включая прогноз)\n"
        "📁 Категории - управление категориями\n"
        "🎯 Бюджеты - установить лимиты (например: 'Еда 10000')\n"
        "   После установки бюджета вы будете получать уведомления:\n"
        "   • при использовании 75% бюджета\n"
        "   • при использовании 90% бюджета\n"
        "   • при превышении бюджета\n"
        "⏰ Регулярные - настроить регулярные платежи\n"
        "⚙️ Настройки - экспорт и очистка истории\n\n"
        "🔮 Прогноз - предсказывает расходы до конца месяца\n\n"
        "Примеры ввода:\n"
        "• 'кофе 300' - расход 300р на категорию 'Еда'\n"
        "• '+5000 зарплата' - доход 5000р\n"
        "• '-1500 такси' - расход 1500р на транспорт\n\n"
        "Если ошиблись кнопкой - просто нажмите нужную кнопку еще раз"
    )
    bot.send_message(message.chat.id, help_text)

# Обработчик команды /stats (только для админа)
@bot.message_handler(commands=['stats'])
def stats_command(message):
    """Показать статистику бота (только для админа)"""
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
    
    for user_id, username, trans_count in stats['top_users']:
        name = f"@{username}" if username != "NoUsername" else f"ID:{user_id}"
        text += f"• {name}: {trans_count} транзакций\n"
    
    # Добавляем информацию о размере БД
    try:
        size_bytes = os.path.getsize('finance_bot.db')
        size_mb = size_bytes / (1024 * 1024)
        text += f"\n💾 Размер БД: {size_mb:.2f} МБ"
    except:
        pass
    
    bot.send_message(message.chat.id, text)

# Обработчик команды /users (список пользователей)
@bot.message_handler(commands=['users'])
def users_command(message):
    """Показать список последних пользователей (только для админа)"""
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "❌ У вас нет прав для этой команды")
        return
    
    conn = get_db()
    c = conn.cursor()
    
    c.execute("""
        SELECT user_id, username, created_at 
        FROM users 
        ORDER BY created_at DESC 
        LIMIT 10
    """)
    
    recent_users = c.fetchall()
    conn.close()
    
    text = "👥 Последние 10 пользователей:\n\n"
    for user_id, username, created_at in recent_users:
        name = f"@{username}" if username != "NoUsername" else "NoUsername"
        date = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
        text += f"• ID: {user_id} | {name}\n  📅 {date}\n\n"
    
    bot.send_message(message.chat.id, text)

# Обработчик команды /optimize (только для админа)
@bot.message_handler(commands=['optimize'])
def optimize_command(message):
    """Оптимизация базы данных (только для админа)"""
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "❌ У вас нет прав для этой команды")
        return
    
    # Отправляем сообщение о начале оптимизации
    msg = bot.send_message(message.chat.id, "⏳ Оптимизация базы данных...\nЭто может занять несколько секунд")
    
    # Выполняем оптимизацию
    result = optimize_database()
    
    if result['success']:
        # Формируем отчет
        text = (
            f"✅ База данных успешно оптимизирована!\n\n"
            f"📊 Размер до оптимизации: {result['size_before']:.2f} МБ\n"
            f"📊 Размер после оптимизации: {result['size_after']:.2f} МБ\n"
            f"💾 Освобождено места: {result['freed']:.2f} МБ\n"
        )
        
        if result['freed'] > 10:
            text += "\n🎉 Отличный результат! Много места освобождено"
        elif result['freed'] > 1:
            text += "\n👍 Хорошая оптимизация"
        else:
            text += "\n💡 База данных и так была оптимизирована"
            
    else:
        text = f"❌ Ошибка при оптимизации:\n{result['error']}"
    
    # Редактируем сообщение с результатом
    bot.edit_message_text(
        text,
        message.chat.id,
        msg.message_id
    )

# Обработчик текстовых сообщений
@bot.message_handler(func=lambda message: True)
def handle_message(message):
    user_id = message.from_user.id
    text = message.text
    
    # Сначала проверяем, не хочет ли пользователь выйти из текущего состояния
    # Это позволяет прервать любой процесс и вернуться в главное меню
    if text in ["🔙 Главное меню", "🔙 Назад", "❓ Помощь"]:
        user_data.pop(user_id, None)
        if text == "❓ Помощь":
            help_command(message)
        else:
            bot.send_message(user_id, "Главное меню:", reply_markup=main_keyboard())
        return
    
    # Проверяем кнопки главного меню - они всегда должны работать
    if text in ["💰 Доход", "💸 Расход", "📊 Отчет", "📁 Категории", 
                "🎯 Бюджеты", "⏰ Регулярные", "⚙️ Настройки"]:
        # Если пользователь был в каком-то процессе - сбрасываем
        if user_id in user_data:
            user_data.pop(user_id)
    
    # Если пользователь в процессе добавления транзакции
    if user_id in user_data:
        action = user_data[user_id].get('action')
        
        # Обработка ввода суммы для дохода/расхода
        if action in ['waiting_for_income', 'waiting_for_expense']:
            # Проверяем, не нажал ли пользователь кнопку главного меню
            if text in ["💰 Доход", "💸 Расход", "📊 Отчет", "📁 Категории", 
                       "🎯 Бюджеты", "⏰ Регулярные", "⚙️ Настройки", "❓ Помощь"]:
                # Сбрасываем состояние и обрабатываем новую команду
                user_data.pop(user_id)
                # Передаем управление дальше для обработки кнопки
            else:
                process_transaction_input(user_id, text, action)
                return
        
        # Обработка отчета
        elif action == 'report':
            if text in ["💰 Доход", "💸 Расход", "📊 Отчет", "📁 Категории", 
                       "🎯 Бюджеты", "⏰ Регулярные", "⚙️ Настройки", "❓ Помощь"]:
                user_data.pop(user_id)
            else:
                handle_report(user_id, text)
                return
        
        # Обработка бюджета
        elif action == 'budget':
            if text in ["💰 Доход", "💸 Расход", "📊 Отчет", "📁 Категории", 
                       "🎯 Бюджеты", "⏰ Регулярные", "⚙️ Настройки", "❓ Помощь"]:
                user_data.pop(user_id)
            else:
                handle_budget(user_id, text)
                return
        
        # Обработка регулярных платежей
        elif action == 'recurring_menu':
            if text in ["💰 Доход", "💸 Расход", "📊 Отчет", "📁 Категории", 
                       "🎯 Бюджеты", "⏰ Регулярные", "⚙️ Настройки", "❓ Помощь"]:
                user_data.pop(user_id)
            else:
                handle_recurring_menu(user_id, text)
                return
        elif action == 'add_recurring':
            if text in ["💰 Доход", "💸 Расход", "📊 Отчет", "📁 Категории", 
                       "🎯 Бюджеты", "⏰ Регулярные", "⚙️ Настройки", "❓ Помощь"]:
                user_data.pop(user_id)
            else:
                handle_add_recurring(user_id, text)
                return
        elif action == 'delete_recurring':
            if text in ["💰 Доход", "💸 Расход", "📊 Отчет", "📁 Категории", 
                       "🎯 Бюджеты", "⏰ Регулярные", "⚙️ Настройки", "❓ Помощь"]:
                user_data.pop(user_id)
            else:
                handle_delete_recurring(user_id, text)
                return
            
        # Обработка очистки истории
        elif action == 'clear_history':
            if text in ["💰 Доход", "💸 Расход", "📊 Отчет", "📁 Категории", 
                       "🎯 Бюджеты", "⏰ Регулярные", "⚙️ Настройки", "❓ Помощь"]:
                user_data.pop(user_id)
            else:
                handle_clear_history(user_id, text)
                return
    
    # Обработка кнопок главного меню
    if text == "💰 Доход":
        user_data[user_id] = {'action': 'waiting_for_income', 'type': 'income'}
        bot.send_message(user_id, "Введите доход (например: '+5000 зарплата' или '5000 зарплата')\n\nИли нажмите другую кнопку, чтобы отменить")
        
    elif text == "💸 Расход":
        user_data[user_id] = {'action': 'waiting_for_expense', 'type': 'expense'}
        bot.send_message(user_id, "Введите расход (например: '300 кофе' или '-300 кофе')\n\nИли нажмите другую кнопку, чтобы отменить")
        
    elif text == "📊 Отчет":
        user_data[user_id] = {'action': 'report'}
        bot.send_message(user_id, "Выберите период для отчета:", reply_markup=report_keyboard())
        
    elif text == "📁 Категории":
        show_categories(user_id)
        
    elif text == "🎯 Бюджеты":
        user_data[user_id] = {'action': 'budget'}
        bot.send_message(user_id, "Введите бюджет в формате 'категория сумма' (например: 'Еда 10000')\n\nИли нажмите другую кнопку, чтобы отменить")
        
    elif text == "⏰ Регулярные":
        user_data[user_id] = {'action': 'recurring_menu'}
        bot.send_message(user_id, "⏰ Регулярные платежи\n\nВыберите действие:", 
                        reply_markup=recurring_menu_keyboard())
        
    elif text == "⚙️ Настройки":
        bot.send_message(user_id, 
                        "⚙️ Настройки\n\n"
                        "Выберите действие:",
                        reply_markup=settings_keyboard())
        
    elif text == "📤 Экспорт данных":
        export_data(user_id)
        
    elif text == "🗑️ Очистить историю":
        user_data[user_id] = {'action': 'clear_history'}
        bot.send_message(user_id, 
                        "🗑️ Очистка истории\n\n"
                        "Выберите период для удаления:",
                        reply_markup=clear_history_keyboard())
        
    elif text == "❓ Помощь":
        help_command(message)
        
    elif text == "🔙 Главное меню":
        user_data.pop(user_id, None)
        bot.send_message(user_id, "Главное меню:", reply_markup=main_keyboard())
        
    elif text == "🔙 Назад":
        user_data.pop(user_id, None)
        bot.send_message(user_id, "Главное меню:", reply_markup=main_keyboard())
    
    else:
        # Если пользователь в процессе ввода, но ввел что-то не то
        if user_id in user_data:
            action = user_data[user_id].get('action')
            if action in ['waiting_for_income', 'waiting_for_expense']:
                bot.send_message(user_id, "Неверный формат. Используйте: 'сумма описание'\nНапример: 'кофе 300'\n\nИли нажмите другую кнопку меню, чтобы отменить")
            elif action == 'budget':
                bot.send_message(user_id, "Неверный формат. Используйте: 'категория сумма'\nНапример: 'Еда 10000'\n\nИли нажмите другую кнопку меню, чтобы отменить")
            elif action == 'add_recurring':
                bot.send_message(user_id, "Неверный формат. Используйте: 'категория сумма день'\nНапример: 'Интернет 500 5'\n\nИли нажмите другую кнопку меню, чтобы отменить")
            elif action == 'delete_recurring':
                bot.send_message(user_id, "Введите корректный ID платежа\n\nИли нажмите другую кнопку меню, чтобы отменить")
            else:
                bot.send_message(user_id, "Пожалуйста, используйте кнопки меню", reply_markup=main_keyboard())
        else:
            # Если не в процессе и кнопка не распознана
            bot.send_message(user_id, "Пожалуйста, используйте кнопки меню", reply_markup=main_keyboard())

# Функция для обработки отчетов
def handle_report(user_id, period):
    if period == "🔙 Назад":
        user_data.pop(user_id, None)
        bot.send_message(user_id, "Главное меню:", reply_markup=main_keyboard())
        return
    
    conn = get_db()
    c = conn.cursor()
    
    now = datetime.now()
    
    if period == "📅 Сегодня":
        # Сегодня с 00:00 до 23:59
        start_date = now.strftime("%Y-%m-%d 00:00:00")
        end_date = now.strftime("%Y-%m-%d 23:59:59")
        title = "за сегодня"
        
    elif period == "📆 Неделя":
        # Последние 7 дней (включая сегодня)
        week_ago = now - timedelta(days=7)
        start_date = week_ago.strftime("%Y-%m-%d 00:00:00")
        end_date = now.strftime("%Y-%m-%d 23:59:59")
        title = "за последние 7 дней"
        
    elif period == "📊 Месяц":
        # Последние 30 дней (включая сегодня)
        month_ago = now - timedelta(days=30)
        start_date = month_ago.strftime("%Y-%m-%d 00:00:00")
        end_date = now.strftime("%Y-%m-%d 23:59:59")
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
        forecast_text = budget_forecast(user_id)
        bot.send_message(user_id, forecast_text, reply_markup=main_keyboard())
        return
        
    else:
        bot.send_message(user_id, "Выберите период из меню")
        conn.close()
        return
    
    # Получаем доходы за период
    c.execute("""SELECT SUM(amount) FROM transactions 
                 WHERE user_id = ? AND amount > 0 AND datetime(date) BETWEEN datetime(?) AND datetime(?)""",
              (user_id, start_date, end_date))
    income = c.fetchone()[0] or 0
    
    # Получаем расходы по категориям за период
    c.execute("""SELECT category, SUM(amount) FROM transactions 
                 WHERE user_id = ? AND amount < 0 AND datetime(date) BETWEEN datetime(?) AND datetime(?)
                 GROUP BY category
                 ORDER BY amount""", (user_id, start_date, end_date))
    expenses = c.fetchall()
    
    # Получаем все транзакции за период для проверки
    c.execute("""SELECT date, amount, category, description FROM transactions 
                 WHERE user_id = ? AND datetime(date) BETWEEN datetime(?) AND datetime(?)
                 ORDER BY date DESC""", (user_id, start_date, end_date))
    all_transactions = c.fetchall()
    
    conn.close()
    
    # Формируем отчет
    text = f"📊 Отчет {title}\n\n"
    text += f"💰 Доход: {income:.0f} ₽\n"
    
    total_expense = sum(abs(row[1]) for row in expenses)
    text += f"💸 Расход: {total_expense:.0f} ₽\n"
    text += f"💎 Остаток: {income - total_expense:.0f} ₽\n\n"
    
    if expenses:
        text += "Расходы по категориям:\n"
        for category, amount in expenses:
            percent = (abs(amount) / total_expense) * 100 if total_expense > 0 else 0
            text += f"• {category}: {abs(amount):.0f} ₽ ({percent:.1f}%)\n"
    else:
        text += "Нет расходов за выбранный период\n"
    
    # Добавляем список последних транзакций для наглядности
    if all_transactions:
        text += "\nПоследние транзакции:\n"
        for date, amount, category, desc in all_transactions[:5]:  # Показываем только 5 последних
            emoji = "💰" if amount > 0 else "💸"
            date_str = datetime.strptime(date, "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
            text += f"{emoji} {date_str} | {category}: {abs(amount):.0f} ₽ | {desc}\n"
    
    user_data.pop(user_id, None)
    bot.send_message(user_id, text, reply_markup=main_keyboard())

# Функция для прогноза бюджета
def budget_forecast(user_id):
    conn = get_db()
    c = conn.cursor()
    
    now = datetime.now()
    today = now.day
    # Определяем количество дней в текущем месяце
    if now.month == 12:
        days_in_month = (datetime(now.year + 1, 1, 1) - timedelta(days=1)).day
    else:
        days_in_month = (datetime(now.year, now.month + 1, 1) - timedelta(days=1)).day
    
    # Получаем средние расходы за последние 3 месяца
    c.execute("""
        SELECT 
            category,
            AVG(monthly_total) as avg_monthly
        FROM (
            SELECT 
                category,
                strftime('%Y-%m', date) as month,
                SUM(ABS(amount)) as monthly_total
            FROM transactions 
            WHERE user_id = ? AND amount < 0 
                AND date >= date('now', '-3 months')
            GROUP BY category, strftime('%Y-%m', date)
        )
        GROUP BY category
        ORDER BY avg_monthly DESC
    """, (user_id,))
    
    averages = c.fetchall()
    
    if not averages:
        conn.close()
        return "❌ Недостаточно данных для прогноза. Добавьте больше транзакций за последние 3 месяца."
    
    # Получаем расходы за текущий месяц
    month_start = datetime.now().replace(day=1).strftime("%Y-%m-%d 00:00:00")
    c.execute("""
        SELECT category, SUM(ABS(amount)) as spent
        FROM transactions 
        WHERE user_id = ? AND amount < 0 AND datetime(date) >= datetime(?)
        GROUP BY category
    """, (user_id, month_start))
    
    current_spent = dict(c.fetchall())
    conn.close()
    
    # Формируем прогноз
    text = "🔮 Прогноз бюджета на месяц\n\n"
    text += f"📅 Сегодня: {now.strftime('%d.%m.%Y')}\n"
    text += f"⚡ Прогноз на {days_in_month} дней\n\n"
    
    total_projected = 0
    total_avg = 0
    total_spent = 0
    
    for category, avg in averages:
        spent = current_spent.get(category, 0)
        # Прогноз = (потрачено сейчас / дней прошло) * дней в месяце
        projected = (spent / today) * days_in_month if today > 0 and spent > 0 else 0
        
        total_spent += spent
        total_projected += projected
        total_avg += avg
        
        # Эмодзи для категорий
        category_emoji = {
            'Еда': '🍔',
            'Транспорт': '🚗',
            'Жилье': '🏠',
            'Связь': '📱',
            'Развлечения': '🎮',
            'Одежда': '👕',
            'Здоровье': '💊',
            'Образование': '📚',
            'Подарки': '🎁',
            'Спорт': '⚽',
            'Прочее': '📦'
        }.get(category, '📌')
        
        text += f"{category_emoji} {category}\n"
        text += f"  • Потрачено: {spent:.0f} ₽\n"
        text += f"  • Среднее за месяц: {avg:.0f} ₽\n"
        text += f"  • Прогноз: {projected:.0f} ₽\n"
        
        # Анализ
        if projected > avg * 1.2:
            text += "  ⚠️ Высокий риск перерасхода\n"
        elif projected < avg * 0.8:
            text += "  ✅ Экономия\n"
        else:
            text += "  ℹ️ В рамках нормы\n"
        text += "\n"
    
    # Общий прогноз
    text += "📊 Общий итог\n"
    text += f"  • Потрачено всего: {total_spent:.0f} ₽\n"
    text += f"  • Средний расход: {total_avg:.0f} ₽\n"
    text += f"  • Прогноз на месяц: {total_projected:.0f} ₽\n"
    
    if total_projected > total_avg * 1.2:
        text += "⚠️ Внимание! Общий прогноз превышает средние расходы"
    elif total_projected < total_avg * 0.8:
        text += "✅ Отличная экономия!"
    
    return text

# Функция для обработки ввода транзакции (ОБНОВЛЕНА)
def process_transaction_input(user_id, text, action):
    try:
        parts = text.split()
        if len(parts) < 2:
            bot.send_message(user_id, "Неверный формат. Используйте: 'сумма описание'\nНапример: 'кофе 300'\n\nИли нажмите другую кнопку меню, чтобы отменить")
            return
        
        # Определяем сумму
        amount_str = parts[0].replace('+', '').replace('-', '')
        try:
            amount = float(amount_str)
        except ValueError:
            bot.send_message(user_id, "Неверный формат суммы. Используйте число\nНапример: 300\n\nИли нажмите другую кнопку меню, чтобы отменить")
            return
        
        # Получаем тип транзакции из состояния
        trans_type = 'income' if action == 'waiting_for_income' else 'expense'

        russian_type = 'Доход' if trans_type == 'income' else 'Расход'
        
        # Если это расход, делаем сумму отрицательной
        if trans_type == 'expense':
            amount = -abs(amount)
        else:
            amount = abs(amount)
        
        # Определяем категорию
        description = ' '.join(parts[1:])
        category = categorize_transaction(description)
        
        # Сохраняем транзакцию
        save_transaction(user_id, amount, category, description, trans_type)
        
        # Очищаем состояние пользователя
        user_data.pop(user_id, None)
        
        # Отправляем подтверждение
        bot.send_message(user_id, 
                        f"✅ {russian_type} добавлен:\n"
                        f"Сумма: {abs(amount)} ₽\n"
                        f"Категория: {category}\n"
                        f"Описание: {description}",
                        reply_markup=main_keyboard())
        
        # ЕСЛИ ЭТО РАСХОД - проверяем бюджет и отправляем предупреждение
        if trans_type == 'expense':
            budget_warning = check_budget_after_expense(user_id, category, abs(amount))
            if budget_warning:
                bot.send_message(user_id, budget_warning)
        
    except Exception as e:
        bot.send_message(user_id, f"Ошибка: {str(e)}\n\nНажмите другую кнопку меню, чтобы продолжить")
        user_data.pop(user_id, None)
        bot.send_message(user_id, "Главное меню:", reply_markup=main_keyboard())

# Функция для категоризации транзакций
def categorize_transaction(description):
    description = description.lower()
    
    categories = {
        'еда': ['кофе', 'еда', 'ресторан', 'кафе', 'продукты', 'обед', 'пицца', 'бургер', 'суши', 'пиццерия'],
        'транспорт': ['такси', 'метро', 'автобус', 'бензин', 'транспорт', 'проезд', 'uber', 'яндекс.такси'],
        'жилье': ['квартплата', 'коммуналка', 'аренда', 'жкх', 'квартира', 'электричество', 'вода'],
        'связь': ['телефон', 'интернет', 'связь', 'мтс', 'билайн', 'мегафон', 'теле2'],
        'развлечения': ['кино', 'театр', 'игры', 'steam', 'развлечения', 'клуб', 'боулинг', 'караоке'],
        'одежда': ['одежда', 'обувь', 'кроссовки', 'куртка', 'джинсы', 'футболка'],
        'здоровье': ['аптека', 'лекарства', 'врач', 'больница', 'здоровье', 'стоматолог'],
        'образование': ['книги', 'курсы', 'образование', 'учеба', 'школа', 'университет'],
        'подарки': ['подарок', 'цветы', 'сувенир', 'открытка'],
        'спорт': ['спортзал', 'фитнес', 'тренировка', 'бассейн', 'йога']
    }
    
    for category, keywords in categories.items():
        for keyword in keywords:
            if keyword in description:
                return category.capitalize()
    
    return "Прочее"

# Функция для сохранения транзакции
def save_transaction(user_id, amount, category, description, trans_type):
    conn = get_db()
    c = conn.cursor()
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d %H:%M:%S")
    c.execute("""INSERT INTO transactions 
                 (user_id, amount, category, type, description, date) 
                 VALUES (?, ?, ?, ?, ?, ?)""",
              (user_id, amount, category, trans_type, description, date_str))
    conn.commit()
    conn.close()

# Функция для показа категорий
def show_categories(user_id):
    conn = get_db()
    c = conn.cursor()
    
    # Получаем статистику по категориям за последние 30 дней
    month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
    c.execute("""SELECT category, SUM(amount) as total 
                 FROM transactions 
                 WHERE user_id = ? AND amount < 0 AND datetime(date) >= datetime(?)
                 GROUP BY category
                 ORDER BY total""", (user_id, month_ago))
    
    expenses = c.fetchall()
    
    # Получаем доходы за последние 30 дней
    c.execute("""SELECT SUM(amount) FROM transactions 
                 WHERE user_id = ? AND amount > 0 AND datetime(date) >= datetime(?)""", 
              (user_id, month_ago))
    income = c.fetchone()[0] or 0
    
    conn.close()
    
    text = "📁 Ваши категории расходов за последние 30 дней:\n\n"
    if expenses:
        total_expenses = sum(abs(row[1]) for row in expenses)
        for category, amount in expenses:
            percent = (abs(amount) / total_expenses) * 100 if total_expenses > 0 else 0
            text += f"• {category}: {abs(amount):.0f} ₽ ({percent:.1f}%)\n"
        text += f"\n💰 Доход за период: {income:.0f} ₽"
        text += f"\n💸 Расход за период: {total_expenses:.0f} ₽"
        text += f"\n💎 Остаток: {income - total_expenses:.0f} ₽"
    else:
        text += "Нет расходов за последние 30 дней"
    
    bot.send_message(user_id, text, reply_markup=main_keyboard())

# Функция для отправки графика
def send_chart(user_id):
    try:
        import matplotlib
        matplotlib.use('Agg')
        
        conn = get_db()
        c = conn.cursor()
        
        month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
        c.execute("""SELECT category, SUM(amount) as total 
                     FROM transactions 
                     WHERE user_id = ? AND amount < 0 AND datetime(date) >= datetime(?)
                     GROUP BY category""", (user_id, month_ago))
        
        data = c.fetchall()
        conn.close()
        
        if not data:
            bot.send_message(user_id, "Нет данных для графика", reply_markup=main_keyboard())
            return
        
        # Создаем круговую диаграмму
        categories = [row[0] for row in data]
        amounts = [abs(row[1]) for row in data]
        
        plt.figure(figsize=(8, 8))
        plt.pie(amounts, labels=categories, autopct='%1.1f%%')
        plt.title('Расходы за последние 30 дней')
        
        # Сохраняем в буфер
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        buf.seek(0)
        
        # Отправляем фото
        bot.send_photo(user_id, buf, reply_markup=main_keyboard())
        plt.close('all')
        
    except Exception as e:
        bot.send_message(user_id, f"Ошибка при создании графика: {str(e)}", reply_markup=main_keyboard())

# Функция для показа баланса
def show_balance(user_id):
    conn = get_db()
    c = conn.cursor()
    
    c.execute("""SELECT SUM(amount) FROM transactions WHERE user_id = ?""", (user_id,))
    total_balance = c.fetchone()[0] or 0
    
    month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
    c.execute("""SELECT SUM(amount) FROM transactions 
                 WHERE user_id = ? AND amount > 0 AND datetime(date) >= datetime(?)""", (user_id, month_ago))
    month_income = c.fetchone()[0] or 0
    
    c.execute("""SELECT SUM(amount) FROM transactions 
                 WHERE user_id = ? AND amount < 0 AND datetime(date) >= datetime(?)""", (user_id, month_ago))
    month_expense = abs(c.fetchone()[0] or 0)
    
    conn.close()
    
    text = "💰 Баланс\n\n"
    text += f"Общий баланс: {total_balance:.0f} ₽\n"
    text += f"Доход за 30 дней: {month_income:.0f} ₽\n"
    text += f"Расход за 30 дней: {month_expense:.0f} ₽\n"
    text += f"Остаток за период: {month_income - month_expense:.0f} ₽"
    
    bot.send_message(user_id, text, reply_markup=main_keyboard())

# Функция для обработки бюджетов
def handle_budget(user_id, text):
    if text == "🔙 Назад":
        user_data.pop(user_id, None)
        bot.send_message(user_id, "Главное меню:", reply_markup=main_keyboard())
        return
    
    try:
        parts = text.split()
        if len(parts) != 2:
            bot.send_message(user_id, "Неверный формат. Используйте: 'категория сумма'\nНапример: 'Еда 10000'\n\nИли нажмите другую кнопку меню, чтобы отменить")
            return
        
        category = parts[0].capitalize()
        amount = float(parts[1])
        
        conn = get_db()
        c = conn.cursor()
        month = datetime.now().strftime("%Y-%m")
        
        c.execute("""INSERT OR REPLACE INTO budgets (user_id, category, amount, month)
                     VALUES (?, ?, ?, ?)""", (user_id, category, amount, month))
        conn.commit()
        conn.close()
        
        user_data.pop(user_id, None)
        
        # Показываем текущее состояние бюджета после установки
        budget_status = check_budget_after_expense(user_id, category, 0)
        if budget_status:
            bot.send_message(user_id, 
                           f"✅ Бюджет на '{category}' установлен: {amount:,.0f} ₽\n\n"
                           f"{budget_status}", 
                           reply_markup=main_keyboard())
        else:
            bot.send_message(user_id, 
                           f"✅ Бюджет на '{category}' установлен: {amount:,.0f} ₽\n"
                           f"Пока нет расходов по этой категории в этом месяце.", 
                           reply_markup=main_keyboard())
        
    except ValueError:
        bot.send_message(user_id, "Неверный формат суммы. Используйте число\nНапример: 10000\n\nИли нажмите другую кнопку меню, чтобы отменить")

# Функция для обработки меню регулярных платежей
def handle_recurring_menu(user_id, text):
    if text == "➕ Добавить":
        user_data[user_id] = {'action': 'add_recurring'}
        bot.send_message(user_id, "Введите регулярный платеж в формате:\n'категория сумма день'\n"
                         "Например: 'Интернет 500 5' (5-го числа каждого месяца)\n\n"
                         "Или нажмите другую кнопку, чтобы отменить")
    
    elif text == "📋 Список":
        show_recurring_list(user_id)
        # Не меняем состояние, остаемся в меню регулярных платежей
        
    elif text == "❌ Удалить":
        user_data[user_id] = {'action': 'delete_recurring'}
        show_recurring_list(user_id, delete_mode=True)
    
    elif text == "🔙 Назад":
        user_data.pop(user_id, None)
        bot.send_message(user_id, "Главное меню:", reply_markup=main_keyboard())
    
    else:
        bot.send_message(user_id, "Выберите действие из меню", reply_markup=recurring_menu_keyboard())

# Функция для добавления регулярного платежа
def handle_add_recurring(user_id, text):
    if text == "🔙 Назад":
        user_data[user_id] = {'action': 'recurring_menu'}
        bot.send_message(user_id, "Меню регулярных платежей:", reply_markup=recurring_menu_keyboard())
        return
    
    try:
        parts = text.split()
        if len(parts) != 3:
            bot.send_message(user_id, "Неверный формат. Используйте: 'категория сумма день'\nНапример: 'Интернет 500 5'\n\nИли нажмите другую кнопку меню, чтобы отменить")
            return
        
        category = parts[0].capitalize()
        amount = float(parts[1])
        day = int(parts[2])
        
        if day < 1 or day > 31:
            bot.send_message(user_id, "День должен быть от 1 до 31\n\nИли нажмите другую кнопку меню, чтобы отменить")
            return
        
        conn = get_db()
        c = conn.cursor()
        c.execute("""INSERT INTO recurring_payments 
                     (user_id, amount, category, description, day)
                     VALUES (?, ?, ?, ?, ?)""",
                  (user_id, amount, category, category, day))
        conn.commit()
        conn.close()
        
        user_data[user_id] = {'action': 'recurring_menu'}
        bot.send_message(user_id, f"✅ Регулярный платеж добавлен:\n"
                         f"{category}: {amount} ₽, {day}-го числа", 
                         reply_markup=recurring_menu_keyboard())
        
    except ValueError:
        bot.send_message(user_id, "Неверный формат суммы или дня. Используйте числа\nНапример: 'Интернет 500 5'\n\nИли нажмите другую кнопку меню, чтобы отменить")

# Функция для удаления регулярного платежа
def handle_delete_recurring(user_id, text):
    if text == "🔙 Назад":
        user_data[user_id] = {'action': 'recurring_menu'}
        bot.send_message(user_id, "Меню регулярных платежей:", reply_markup=recurring_menu_keyboard())
        return
    
    try:
        payment_id = int(text)
        
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM recurring_payments WHERE id = ? AND user_id = ?", 
                 (payment_id, user_id))
        conn.commit()
        
        if c.rowcount > 0:
            bot.send_message(user_id, f"✅ Регулярный платеж удален")
        else:
            bot.send_message(user_id, f"❌ Платеж с ID {payment_id} не найден")
        
        conn.close()
        
        user_data[user_id] = {'action': 'recurring_menu'}
        bot.send_message(user_id, "Меню регулярных платежей:", reply_markup=recurring_menu_keyboard())
        
    except ValueError:
        bot.send_message(user_id, "Введите корректный ID платежа (число)\n\nИли нажмите другую кнопку меню, чтобы отменить")

# Функция для показа списка регулярных платежей
def show_recurring_list(user_id, delete_mode=False):
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT id, amount, category, day FROM recurring_payments 
                 WHERE user_id = ? ORDER BY day""", (user_id,))
    payments = c.fetchall()
    conn.close()
    
    if not payments:
        bot.send_message(user_id, "У вас нет регулярных платежей")
        return
    
    text = "📋 Ваши регулярные платежи:\n\n"
    for pid, amount, category, day in payments:
        text += f"• {category}: {amount} ₽ ({day}-го числа)\n"
        if delete_mode:
            text += f"  ID: {pid}\n"
    
    if delete_mode:
        text += "\nВведите ID платежа для удаления:"
        bot.send_message(user_id, text)
    else:
        bot.send_message(user_id, text)

# Функция для экспорта данных в Excel
def export_data(user_id):
    try:
        # Проверяем наличие pandas
        try:
            import pandas as pd
        except ImportError:
            bot.send_message(user_id, "❌ Ошибка: Не установлена библиотека pandas. Установите: pip install pandas openpyxl")
            return
        
        conn = get_db()
        
        # Получаем транзакции пользователя
        df = pd.read_sql_query("""
            SELECT 
                date as 'Дата',
                CASE WHEN type = 'income' THEN 'Доход' ELSE 'Расход' END as 'Тип',
                category as 'Категория',
                ABS(amount) as 'Сумма',
                description as 'Описание'
            FROM transactions 
            WHERE user_id = ? 
            ORDER BY date DESC
        """, conn, params=(user_id,))
        
        # Получаем статистику по месяцам
        stats_df = pd.read_sql_query("""
            SELECT 
                strftime('%Y-%m', date) as 'Месяц',
                SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) as 'Доход',
                SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END) as 'Расход',
                SUM(amount) as 'Баланс'
            FROM transactions 
            WHERE user_id = ?
            GROUP BY strftime('%Y-%m', date)
            ORDER BY Месяц DESC
        """, conn, params=(user_id,))
        
        # Получаем статистику по категориям
        categories_df = pd.read_sql_query("""
            SELECT 
                category as 'Категория',
                SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END) as 'Потрачено',
                COUNT(*) as 'Количество транзакций'
            FROM transactions 
            WHERE user_id = ? AND amount < 0
            GROUP BY category
            ORDER BY Потрачено DESC
        """, conn, params=(user_id,))
        
        conn.close()
        
        if df.empty:
            bot.send_message(user_id, "❌ Нет данных для экспорта")
            return
        
        # Создаем Excel файл с несколькими листами
        filename = f"finance_report_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        
        with pd.ExcelWriter(filename, engine='openpyxl') as writer:
            # Лист с транзакциями
            df.to_excel(writer, sheet_name='Транзакции', index=False)
            
            # Лист со статистикой по месяцам
            if not stats_df.empty:
                stats_df.to_excel(writer, sheet_name='Статистика по месяцам', index=False)
            
            # Лист с категориями
            if not categories_df.empty:
                categories_df.to_excel(writer, sheet_name='Категории расходов', index=False)
            
            # Добавляем сводную информацию на отдельный лист
            if not df.empty:
                # Общая статистика
                total_income = df[df['Тип'] == 'Доход']['Сумма'].sum()
                total_expense = df[df['Тип'] == 'Расход']['Сумма'].sum()
                
                summary_data = {
                    'Показатель': ['Общий доход', 'Общий расход', 'Баланс', 'Всего транзакций'],
                    'Значение': [
                        f"{total_income:.2f} ₽",
                        f"{total_expense:.2f} ₽", 
                        f"{total_income - total_expense:.2f} ₽",
                        len(df)
                    ]
                }
                summary_df = pd.DataFrame(summary_data)
                summary_df.to_excel(writer, sheet_name='Сводка', index=False)
        
        # Отправляем файл
        with open(filename, 'rb') as f:
            bot.send_document(
                user_id, 
                f, 
                caption="📊 Финансовый отчет\n\n"
                       f"✅ Экспортировано транзакций: {len(df)}\n"
                       f"📅 Данные на: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
                       f"📁 Формат: Excel (.xlsx)"
            )
        
        # Удаляем временный файл
        os.remove(filename)
        
    except Exception as e:
        bot.send_message(user_id, f"❌ Ошибка при экспорте: {str(e)}")

# Функция для очистки истории транзакций
def clear_transactions(user_id, period="all"):
    """
    Очищает историю транзакций пользователя
    period: "all" - все, "month" - последний месяц, "year" - последний год
    """
    conn = get_db()
    c = conn.cursor()
    
    # Получаем количество до удаления
    c.execute("SELECT COUNT(*) FROM transactions WHERE user_id = ?", (user_id,))
    before_count = c.fetchone()[0]
    
    if period == "all":
        # Удаляем все транзакции пользователя
        c.execute("DELETE FROM transactions WHERE user_id = ?", (user_id,))
        period_text = "все"
    elif period == "month":
        # Удаляем транзакции за последний месяц
        month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
        c.execute("DELETE FROM transactions WHERE user_id = ? AND datetime(date) >= datetime(?)", 
                 (user_id, month_ago))
        period_text = "за последний месяц"
    elif period == "year":
        # Удаляем транзакции за последний год
        year_ago = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d 00:00:00")
        c.execute("DELETE FROM transactions WHERE user_id = ? AND datetime(date) >= datetime(?)", 
                 (user_id, year_ago))
        period_text = "за последний год"
    else:
        conn.close()
        return None, None
    
    deleted_count = c.rowcount
    conn.commit()
    conn.close()
    
    return deleted_count, period_text

# Обработчик для очистки истории
def handle_clear_history(user_id, text):
    if text == "🗑️ За месяц":
        user_data[user_id] = {'action': 'clear_history', 'period': 'month'}
        bot.send_message(user_id, 
                        "⚠️ Подтверждение удаления\n\n"
                        "Вы действительно хотите удалить ВСЕ транзакции за последний месяц?\n"
                        "Это действие нельзя отменить!\n\n"
                        "Или нажмите другую кнопку, чтобы отменить",
                        reply_markup=confirm_keyboard())
        
    elif text == "🗑️ За год":
        user_data[user_id] = {'action': 'clear_history', 'period': 'year'}
        bot.send_message(user_id, 
                        "⚠️ Подтверждение удаления\n\n"
                        "Вы действительно хотите удалить ВСЕ транзакции за последний год?\n"
                        "Это действие нельзя отменить!\n\n"
                        "Или нажмите другую кнопку, чтобы отменить",
                        reply_markup=confirm_keyboard())
        
    elif text == "🗑️ Всю историю":
        user_data[user_id] = {'action': 'clear_history', 'period': 'all'}
        bot.send_message(user_id, 
                        "⚠️ Подтверждение удаления\n\n"
                        "Вы действительно хотите удалить ВСЮ историю транзакций?\n"
                        "Это действие нельзя отменить!\n\n"
                        "Или нажмите другую кнопку, чтобы отменить",
                        reply_markup=confirm_keyboard())
        
    elif text == "✅ Да, удалить":
        if user_id in user_data and user_data[user_id].get('action') == 'clear_history':
            period = user_data[user_id].get('period', 'all')
            deleted_count, period_text = clear_transactions(user_id, period)
            
            if deleted_count and deleted_count > 0:
                bot.send_message(user_id, 
                               f"✅ История очищена!\n\n"
                               f"Удалено транзакций: {deleted_count}\n"
                               f"Период: {period_text}",
                               reply_markup=main_keyboard())
            else:
                bot.send_message(user_id, 
                               "ℹ️ Нет транзакций для удаления",
                               reply_markup=main_keyboard())
            
            user_data.pop(user_id, None)
        else:
            bot.send_message(user_id, "Не найдено действие для подтверждения", 
                           reply_markup=main_keyboard())
        
    elif text == "❌ Нет, отмена":
        user_data.pop(user_id, None)
        bot.send_message(user_id, "❌ Удаление отменено", reply_markup=main_keyboard())
        
    elif text == "🔙 Назад":
        user_data.pop(user_id, None)
        bot.send_message(user_id, "Главное меню:", reply_markup=main_keyboard())

# Проверка регулярных платежей
def check_recurring_payments():
    now = datetime.now()
    today = now.day
    
    conn = get_db()
    c = conn.cursor()
    
    c.execute("""SELECT id, user_id, amount, category, description, last_reminded 
                 FROM recurring_payments WHERE day = ?""", (today,))
    
    for pid, user_id, amount, category, description, last_reminded in c.fetchall():
        # Проверяем, не напоминали ли уже сегодня
        if last_reminded and last_reminded.startswith(now.strftime("%Y-%m-%d")):
            continue
        
        try:
            bot.send_message(user_id, 
                           f"⏰ Напоминание о регулярном платеже!\n\n"
                           f"Категория: {category}\n"
                           f"Сумма: {amount} ₽\n"
                           f"Описание: {description}\n\n"
                           f"Не забудьте оплатить сегодня!")
            
            # Обновляем дату последнего напоминания
            c.execute("""UPDATE recurring_payments 
                         SET last_reminded = ? WHERE id = ?""",
                     (now.strftime("%Y-%m-%d %H:%M:%S"), pid))
            conn.commit()
            
        except Exception as e:
            print(f"Ошибка отправки напоминания пользователю {user_id}: {e}")
    
    conn.close()

# Запуск бота
if __name__ == "__main__":
    print("Бот запущен...")
    print(f"ID администратора: {ADMIN_ID}")
    print("Команды для админа: /stats, /users, /optimize")
    
    # Проверяем соединение перед запуском
    if not check_telegram_connection():
        print("⚠️ Проблемы с соединением, но пробуем запустить...")
    
    # Запускаем проверку регулярных платежей в отдельном потоке
    import threading
    import time
    
    def recurring_checker():
        while True:
            try:
                check_recurring_payments()
            except Exception as e:
                print(f"Ошибка в проверке регулярных платежей: {e}")
            time.sleep(3600)  # Проверка каждый час
    
    thread = threading.Thread(target=recurring_checker, daemon=True)
    thread.start()
    
    # Запускаем бота с увеличенным таймаутом
    try:
        bot.polling(none_stop=True, interval=1, timeout=30, long_polling_timeout=25)
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        print("🔄 Перезапуск через 10 секунд...")
        time.sleep(10)
