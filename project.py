# neobank_app.py
# NeoBank — объединённая версия с SQLite, мобильным UI и функцией вкладов
# Совместимо с flet 0.80.5
# Требования: pip install flet requests matplotlib

import flet as ft
import sqlite3
import hashlib
import random
import datetime
import uuid
import re
import requests
import io
import matplotlib.pyplot as plt
from typing import Tuple, Dict, Any, List
import os

# ---------------- CONFIG ----------------
DB_PATH = "neobank_app.db"
DEFAULT_RATES = {"USD": 75.0, "EUR": 82.0, "CNY": 11.0, "RUB": 1.0}
ALLOWED_EMAIL_DOMAINS = ["gmail.com", "yandex.ru", "mail.ru", "yahoo.com", "pochta.ru"]

# ---------------- Database ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE,
        password_hash TEXT,
        full_name TEXT,
        phone TEXT,
        account TEXT,
        balance REAL DEFAULT 0.0,
        settings TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS deposits(
        id TEXT PRIMARY KEY,
        user_id TEXT,
        name TEXT,
        amount REAL,
        rate REAL,
        opened TEXT,
        ends TEXT,
        active INTEGER,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS ops(
        id TEXT PRIMARY KEY,
        user_id TEXT,
        date TEXT,
        type TEXT,
        amount REAL,
        currency TEXT,
        title TEXT,
        details TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")
    conn.commit()
    return conn

DB = init_db()

# ---------------- Helpers ----------------
def now_ts() -> str:
    return datetime.datetime.datetime.now().isoformat(sep=" ", timespec="seconds") if hasattr(datetime, "datetime") else datetime.datetime.now().isoformat(sep=" ", timespec="seconds")

# The above try/compat: ensure correct usage of datetime (some environments)
# Simpler: directly:
def now_ts():
    return datetime.datetime.now().isoformat(sep=" ", timespec="seconds")

def hash_password(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def validate_password(email: str, password: str) -> Tuple[bool, str]:
    if len(password) < 8:
        return False, "Пароль должен содержать минимум 8 символов."
    if password.lower() == email.lower():
        return False, "Пароль не должен совпадать с email."
    if not re.search(r"[A-ZА-ЯЁ]", password):
        return False, "Пароль должен содержать хотя бы одну заглавную букву."
    if not re.search(r"[a-zа-яё]", password):
        return False, "Пароль должен содержать хотя бы одну строчную букву."
    if not re.search(r"\d", password):
        return False, "Пароль должен содержать хотя бы одну цифру."
    if not re.search(r"[!@#$%^&*]", password):
        return False, "Пароль должен содержать хотя бы один специальный символ (!@#$%^&*)."
    return True, ""

def generate_account_number() -> str:
    s = "".join(str(random.randint(0, 9)) for _ in range(16))
    return " ".join([s[i:i+4] for i in range(0, 16, 4)])

# ---------------- DB operations ----------------
def user_create(email: str, password: str, full_name: str="") -> Dict[str,Any]:
    uid = str(uuid.uuid4())
    acc = generate_account_number()
    ph = hash_password(password)
    c = DB.cursor()
    c.execute("INSERT INTO users(id,email,password_hash,full_name,phone,account,balance,settings) VALUES (?,?,?,?,?,?,?,?)",
              (uid, email, ph, full_name, "", acc, 0.0, "{}"))
    DB.commit()
    return {"id": uid, "email": email, "account": acc, "full_name": full_name}

def user_get_by_email(email: str):
    c = DB.cursor()
    c.execute("SELECT id,email,password_hash,full_name,phone,account,balance FROM users WHERE email = ?", (email,))
    r = c.fetchone()
    if not r:
        return None
    # ensure balance is float
    bal = r[6]
    try:
        bal = float(bal) if bal is not None else 0.0
    except:
        bal = 0.0
    return {"id": r[0], "email": r[1], "password_hash": r[2], "full_name": r[3], "phone": r[4], "account": r[5], "balance": bal}

def user_update_balance(user_id: str, new_balance: float):
    c = DB.cursor()
    c.execute("UPDATE users SET balance = ? WHERE id = ?", (round(float(new_balance),2), user_id))
    DB.commit()

def mk_op(user_id: str, op_type: str, amount: float, currency="RUB", title="", details="") -> None:
    c = DB.cursor()
    c.execute("INSERT INTO ops(id,user_id,date,type,amount,currency,title,details) VALUES (?,?,?,?,?,?,?,?)",
              (str(uuid.uuid4()), user_id, now_ts(), op_type, round(float(amount),2), currency, title, details))
    DB.commit()

def ops_for_user(user_id: str, limit: int=1000):
    c = DB.cursor()
    c.execute("SELECT id,date,type,amount,currency,title,details FROM ops WHERE user_id = ? ORDER BY date DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    return [{"id": r[0], "date": r[1], "type": r[2], "amount": float(r[3]), "currency": r[4], "title": r[5], "details": r[6]} for r in rows]

def deposits_list_for_user(user_id: str) -> List[Dict[str,Any]]:
    c = DB.cursor()
    c.execute("SELECT id,name,amount,rate,opened,ends,active FROM deposits WHERE user_id = ?", (user_id,))
    rows = c.fetchall()
    out = []
    for r in rows:
        out.append({"id": r[0], "name": r[1], "amount": float(r[2]), "rate": float(r[3]), "opened": r[4], "ends": r[5], "active": bool(r[6])})
    return out

def create_deposit(user_id: str, name: str, amount: float, rate: float, term_months: int):
    opened = datetime.date.today()
    ends = opened + datetime.timedelta(days=term_months*30)
    did = str(uuid.uuid4())
    c = DB.cursor()
    c.execute("INSERT INTO deposits(id,user_id,name,amount,rate,opened,ends,active) VALUES (?,?,?,?,?,?,?,?)",
              (did, user_id, name, round(float(amount),2), float(rate), opened.isoformat(), ends.isoformat(), 1))
    DB.commit()
    mk_op(user_id, "deposit_open", amount, title=f"Открытие вклада: {name}", details=f"term:{term_months}")

# ---------------- Deposit products & rates ----------------
DEPOSIT_PRODUCTS = [
    {"id":"d1","name":"Накопительный","rate":0.06,"term_months":12,"min_sum":1000},
    {"id":"d2","name":"Доходный","rate":0.08,"term_months":12,"min_sum":10000},
    {"id":"d3","name":"Короткий","rate":0.045,"term_months":6,"min_sum":500}
]

def fetch_rates() -> Dict[str,float]:
    try:
        symbols = ",".join([c for c in DEFAULT_RATES.keys() if c!="RUB"])
        url = f"https://api.exchangerate.host/latest?base=RUB&symbols={symbols}"
        resp = requests.get(url, timeout=6)
        if resp.status_code != 200:
            return DEFAULT_RATES.copy()
        data = resp.json()
        rates = {"RUB":1.0}
        for cur, val in data.get("rates", {}).items():
            if val and val > 0:
                rates[cur] = round(1.0 / float(val), 4)
        for k in DEFAULT_RATES:
            if k not in rates:
                rates[k] = DEFAULT_RATES[k]
        return rates
    except:
        return DEFAULT_RATES.copy()

# ---------------- Analysis & chart ----------------
def categorize_ops(ops: List[Dict[str,Any]]):
    cats = {}
    total_income = 0.0
    total_expense = 0.0
    for o in ops:
        title = (o.get("title") or "").lower()
        details = (o.get("details") or "").lower()
        if o["type"]=="income":
            total_income += o["amount"]
        elif o["type"]=="expense":
            total_expense += o["amount"]
        cat = "Другое"
        if "кофе" in title or "кафе" in title:
            cat = "Кафе"
        elif "продукт" in title or "супермаркет" in title or "магазин" in title:
            cat = "Продукты"
        elif "такси" in title or "транспорт" in title:
            cat = "Транспорт"
        elif "зарп" in title or "з/п" in title or o["type"]=="income":
            cat = "Доход"
        cats.setdefault(cat, 0.0)
        if o["type"]=="expense":
            cats[cat] += o["amount"]
    return {"cats": cats, "total_income": total_income, "total_expense": total_expense}

def make_pie_image(data: Dict[str,float], filename: str):
    labels = []
    vals = []
    for k,v in data.items():
        labels.append(k)
        vals.append(v)
    fig = plt.figure(figsize=(3,3))
    if sum(vals) <= 0:
        plt.text(0.5,0.5,"Нет данных", horizontalalignment='center', verticalalignment='center')
    else:
        plt.pie(vals, labels=labels, autopct='%1.1f%%', textprops={'fontsize':8})
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches="tight", dpi=100)
    plt.close(fig)
    buf.seek(0)
    with open(filename, "wb") as f:
        f.write(buf.read())
    return filename

# ---------------- Seed demo ----------------
def seed_demo_if_empty():
    c = DB.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    if c.fetchone()[0] == 0:
        user_create("demo@example.com", "DemoPass1!", "Демо Пользователь")
        u = user_get_by_email("demo@example.com")
        if u:
            user_update_balance(u["id"], 150000.0)
            mk_op(u["id"], "income", 50000, title="Зарплата")
            mk_op(u["id"], "expense", 1200, title="Кафе: кофе")
            mk_op(u["id"], "expense", 4500, title="Продукты: супермаркет")
            mk_op(u["id"], "expense", 1800, title="Такси", details="транспорт")
seed_demo_if_empty()

# ---------------- Mobile shell & card ----------------
def mobile_shell(content):
    phone = ft.Container(
        content=content,
        bgcolor=ft.Colors.WHITE,
        border_radius=28,
        padding=12,
        width=360,
        height=780,
        expand=False,
        shadow=ft.BoxShadow(blur_radius=20),
    )
    # wrap in a container with black bezel
    return ft.Container(
        content=ft.Row([phone], alignment=ft.MainAxisAlignment.CENTER, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        expand=True,
        bgcolor=ft.Colors.BLACK,
        padding=12
    )

def bank_card_widget(account_str: str, balance_val: float):
    # ensure float
    try:
        bal = float(balance_val)
    except:
        bal = 0.0
    return ft.Container(
        content=ft.Column(
            [
                ft.Row([ft.Text("NeoBank", color="white", weight="bold"), ft.Icon(ft.Icons.CREDIT_CARD, color="white")], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Container(height=12),
                ft.Text(account_str, size=16, color="white"),
                ft.Container(height=6),
                ft.Text(f"{bal:,.2f} RUB", size=22, weight="bold", color="white"),
            ],
            tight=True
        ),
        padding=16,
        border_radius=16,
        bgcolor="#1e3c72",  # simple gradient replacement to avoid alignment issues
    )

# ---------------- Flet UI ----------------
def main(page: ft.Page):
    page.title = "NeoBank (mobile)"
    page.window_width = 390
    page.window_height = 844
    page.padding = 0
    page.bgcolor = ft.Colors.BLACK
    page.theme_mode = ft.ThemeMode.LIGHT

    state = {"user": None, "rates": fetch_rates()}

    def toast(text: str):
        page.snack_bar = ft.SnackBar(ft.Text(text))
        page.snack_bar.open = True
        page.update()

    # --- AUTH controls ---
    login_email = ft.TextField(label="Email", width=320)
    login_pwd = ft.TextField(label="Пароль", password=True, can_reveal_password=True, width=320)

    def do_login(e):
        em = (login_email.value or "").strip().lower()
        pwdv = login_pwd.value or ""
        if not em or not pwdv:
            toast("Введите email и пароль")
            return
        u = user_get_by_email(em)
        if not u:
            toast("Пользователь не найден")
            return
        if u["password_hash"] != hash_password(pwdv):
            toast("Неверный пароль")
            return
        state["user"] = u
        switch_to_home()

    def do_register(e):
        email_tf = ft.TextField(label="Email", width=320)
        pwd_tf = ft.TextField(label="Пароль", password=True, can_reveal_password=True, width=320)
        pwdc_tf = ft.TextField(label="Подтвердите пароль", password=True, can_reveal_password=True, width=320)
        name_tf = ft.TextField(label="ФИО (необязательно)", width=320)

        def reg_ok(ev):
            em = (email_tf.value or "").strip().lower()
            pw = pwd_tf.value or ""
            pwc = pwdc_tf.value or ""
            nm = (name_tf.value or "").strip()
            if not em or "@" not in em:
                toast("Введите корректный email")
                return
            domain = em.split("@")[-1]
            # optional domain check
            # if domain not in ALLOWED_EMAIL_DOMAINS:
            #     toast("Email-домен не поддерживается")
            #     return
            if pw != pwc:
                toast("Пароли не совпадают")
                return
            ok, msg = validate_password(em, pw)
            if not ok:
                toast(msg)
                return
            if user_get_by_email(em):
                toast("Пользователь с таким email уже существует")
                return
            user_create(em, pw, nm)
            u = user_get_by_email(em)
            if u:
                user_update_balance(u["id"], 0.0)
                mk_op(u["id"], "income", 0, title="Регистрация")
                state["user"] = u
                page.dialog.open = False
                page.update()
                switch_to_home()

        def reg_cancel(ev):
            page.dialog.open = False
            page.update()

        dialog = ft.AlertDialog(
            title=ft.Text("Регистрация"),
            content=ft.Column([email_tf, pwd_tf, pwdc_tf, name_tf], spacing=8),
            actions=[ft.Button("Отмена", on_click=reg_cancel), ft.Button("Зарегистрироваться", on_click=reg_ok)],
        )
        page.dialog = dialog
        page.dialog.open = True
        page.update()

    auth_view = ft.Column(
        [
            ft.Container(ft.Row([ft.Text("NeoBank — прототип", size=22, weight="bold")], alignment=ft.MainAxisAlignment.CENTER), height=38),
            ft.Container(height=8),
            login_email,
            login_pwd,
            ft.Row([ft.Button("Войти", on_click=do_login), ft.Button("Регистрация", on_click=do_register)], alignment=ft.MainAxisAlignment.CENTER, spacing=12),
            ft.Divider(),
            ft.Text("Для демонстрации: demo@example.com / DemoPass1!", size=12, color=ft.Colors.GREY_600)
        ],
        scroll=ft.ScrollMode.AUTO,
        spacing=12,
        expand=True
    )

    # --- Main UI controls ---
    deposits_column = ft.Column(scroll=ft.ScrollMode.AUTO)
    history_column = ft.Column(scroll=ft.ScrollMode.AUTO)
    assistant_chat = ft.Column(scroll=ft.ScrollMode.AUTO)
    assistant_input = ft.TextField(label="Вопрос помощнику", expand=True)
    conv_amount = ft.TextField(label="Сумма", width=140, value="100")
    conv_from = ft.Dropdown(label="Из", width=110, value="USD", options=[ft.dropdown.Option(k) for k in ["USD","EUR","CNY","RUB"]])
    conv_to = ft.Dropdown(label="В", width=110, value="RUB", options=[ft.dropdown.Option(k) for k in ["RUB","USD","EUR","CNY"]])
    conv_result = ft.Text()

    def refresh_user():
        u = state["user"]
        if not u:
            return
        # refresh from DB to ensure types
        state["user"] = user_get_by_email(u["email"])
        u = state["user"]
        # deposits
        deposits_column.controls.clear()
        deps = deposits_list_for_user(u["id"])
        if deps:
            for d in deps:
                status = "активен" if d["active"] else "закрыт"
                deposits_column.controls.append(ft.Card(ft.Container(ft.Column([
                    ft.Row([ft.Text(d["name"], weight="bold"), ft.Text(f"{d['rate']*100:.2f}%")], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                    ft.Text(f"Сумма: {d['amount']:.2f} RUB"),
                    ft.Text(f"Открыт: {d['opened']} — Заканчивается: {d['ends']} ({status})"),
                ]), padding=8), elevation=2))
        else:
            deposits_column.controls.append(ft.Text("Вкладов нет", italic=True))
        # history
        history_column.controls.clear()
        ops = ops_for_user(u["id"], limit=50)
        if not ops:
            history_column.controls.append(ft.Text("Операций нет.", italic=True))
        else:
            for o in ops:
                history_column.controls.append(ft.ListTile(
                    title=ft.Text(f"{o['title'] or o['type']} — {o['amount']} {o['currency']}"),
                    subtitle=ft.Text(f"{o['date']} • {o['type']}"),
                    trailing=ft.Text(o['id'][:8])
                ))
        page.update()

    # conversion
    def do_convert(e=None):
        try:
            amt = float(conv_amount.value)
        except:
            conv_result.value = "Неверная сумма"
            page.update()
            return
        frm = conv_from.value
        to = conv_to.value
        rates = state["rates"]
        if frm not in rates or to not in rates:
            conv_result.value = "Курсы недоступны"
            page.update()
            return
        amt_rub = amt * rates[frm]
        res = amt_rub / rates[to]
        conv_result.value = f"{amt:.2f} {frm} = {res:.2f} {to}"
        page.update()

    def update_rates(e=None):
        state["rates"] = fetch_rates()
        toast("Курсы обновлены")

    # open deposit dialog (integrated into previous code concept)
    def open_deposit_dialog(e):
        u = state["user"]
        if not u:
            toast("Сначала войдите")
            return
        prod_dd = ft.Dropdown(options=[ft.dropdown.Option(p["id"], text=f"{p['name']} — {p['rate']*100:.2f}% ({p['term_months']}м) min {p['min_sum']} RUB") for p in DEPOSIT_PRODUCTS], value=DEPOSIT_PRODUCTS[0]["id"])
        sum_tf = ft.TextField(label="Сумма (RUB)", value=str(DEPOSIT_PRODUCTS[0]["min_sum"]))

        def ok(ev):
            pid = prod_dd.value
            prod = next((p for p in DEPOSIT_PRODUCTS if p["id"]==pid), None)
            try:
                amount = float(sum_tf.value)
            except:
                toast("Неверная сумма")
                return
            if amount < prod["min_sum"]:
                toast(f"Мин. сумма {prod['min_sum']} RUB")
                return
            # refresh user object
            user = user_get_by_email(u["email"])
            if user["balance"] < amount:
                toast("Недостаточно средств")
                return
            # debit balance and create deposit
            user_update_balance(user["id"], user["balance"] - amount)
            create_deposit(user["id"], prod["name"], amount, prod["rate"], prod["term_months"])
            mk_op(user["id"], "transfer", -amount, title=f"Перевод на вклад {prod['name']}")
            page.dialog.open = False
            refresh_user()
            toast("Вклад открыт")

        def cancel(ev):
            page.dialog.open = False
            page.update()

        dialog = ft.AlertDialog(title=ft.Text("Открытие вклада"), content=ft.Column([prod_dd, sum_tf], spacing=8), actions=[ft.Button("Отмена", on_click=cancel), ft.Button("Открыть", on_click=ok)])
        page.dialog = dialog
        page.dialog.open = True
        page.update()

    # assistant
    def assistant_analyze(user_id: str):
        ops = ops_for_user(user_id, limit=1000)
        res = categorize_ops(ops)
        cats = res["cats"]
        total_income = res["total_income"]
        total_expense = res["total_expense"]
        lines = []
        lines.append(f"Доходы: {total_income:.2f} RUB, Расходы: {total_expense:.2f} RUB.")
        if cats:
            top = max(cats.items(), key=lambda x:x[1])
            lines.append(f"Самая большая статья расходов: {top[0]} — {top[1]:.2f} RUB.")
        if total_income > 0:
            ratio = total_expense / total_income
            if ratio > 0.7:
                lines.append("Совет: расходы >70% дохода. Сократите крупные статьи расходов.")
            else:
                lines.append("Динамика расходов в норме.")
        else:
            lines.append("Нет зарегистрированных доходов.")
        return "\n".join(lines), cats

    def assistant_send(e):
        u = state["user"]
        if not u:
            toast("Войдите для использования помощника")
            return
        q = (assistant_input.value or "").strip()
        if not q:
            return
        assistant_chat.controls.append(ft.Container(ft.Text(f"Вы: {q}"), padding=6))
        ql = q.lower()
        if "кофе" in ql or "сколько" in ql:
            ops = ops_for_user(u["id"], limit=1000)
            amount = sum(o["amount"] for o in ops if o["type"]=="expense" and ("кофе" in (o.get("title") or "").lower() or "кафе" in (o.get("title") or "").lower()))
            resp = f"Вы потратили на кофе: {amount:.2f} RUB."
            assistant_chat.controls.append(ft.Container(ft.Text(f"Помощник: {resp}"), padding=6))
        else:
            text, cats = assistant_analyze(u["id"])
            assistant_chat.controls.append(ft.Container(ft.Text(f"Помощник: {text}"), padding=6))
            if cats:
                img_path = os.path.join(".", f"pie_{u['id']}.png")
                make_pie_image(cats, img_path)
                assistant_chat.controls.append(ft.Image(src=img_path, width=220, height=220))
        assistant_input.value = ""
        page.update()

    # pages
    def build_home_view():
        u = state["user"]
        card = bank_card_widget(u.get("account",""), u.get("balance", 0.0))
        layout = ft.Column([
            card,
            ft.Container(height=10),
            ft.Row([ft.Button("Открыть вклад", on_click=open_deposit_dialog), ft.Button("Обновить курсы", on_click=update_rates)], spacing=8),
            ft.Container(height=8),
            ft.Text("Вклады:", weight="bold"),
            deposits_column,
            ft.Divider(),
            ft.Text("Конвертер валют", weight="bold"),
            ft.Row([conv_amount, conv_from, conv_to, ft.Button("Конвертировать", on_click=do_convert)], alignment=ft.MainAxisAlignment.START),
            conv_result
        ], scroll=ft.ScrollMode.AUTO, spacing=8, expand=True)
        return layout

    assistant_view = ft.Column([
        ft.Text("Цифровой помощник", weight="bold"),
        ft.Container(assistant_chat, height=300, padding=6, bgcolor=ft.Colors.GREY_100, expand=True),
        ft.Row([assistant_input, ft.Button("Отправить", on_click=assistant_send)], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ft.Text("Примеры: 'Дайджест', 'Сколько я потратил на кофе?', 'Как накопить 100000 руб за полгода?'")
    ], spacing=8, expand=True)

    history_view = ft.Column([ft.Text("История операций", weight="bold"), ft.Divider(), history_column], spacing=8, expand=True)

    profile_view = ft.Column([ft.Text("Профиль", weight="bold"), ft.Divider()], spacing=8, expand=True)

    main_container = ft.Container(content=ft.Text(""), expand=True)

    nav = ft.NavigationBar(
        height=64,
        label_behavior=ft.NavigationBarLabelBehavior.ALWAYS_SHOW,
        destinations=[
            ft.NavigationBarDestination(icon=ft.Icons.HOME, label="Главная"),
            ft.NavigationBarDestination(icon=ft.Icons.SMART_TOY, label="Помощник"),
            ft.NavigationBarDestination(icon=ft.Icons.PERSON, label="Профиль"),
            ft.NavigationBarDestination(icon=ft.Icons.LIST, label="История"),
        ],
        selected_index=0
    )

    def on_nav_change(e):
        idx = nav.selected_index
        if idx == 0:
            main_container.content = build_home_view()
            refresh_user()
        elif idx == 1:
            main_container.content = assistant_view
        elif idx == 2:
            main_container.content = profile_view
            refresh_user()
        elif idx == 3:
            main_container.content = history_view
            refresh_history = lambda: refresh_user()  # small alias to reuse
            refresh_user()
        page.update()

    nav.on_change = on_nav_change

    def switch_to_home():
        page.controls.clear()
        top_row = ft.Row([ft.Text("NeoBank", size=18, weight="bold"), ft.Icon(ft.Icons.NOTIFICATIONS_NONE)], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
        app_content = ft.Column([top_row, main_container, nav], expand=True, spacing=6)
        page.add(mobile_shell(app_content))
        nav.selected_index = 0
        on_nav_change(None)
        page.update()

    def switch_to_auth():
        page.controls.clear()
        auth_wrap = ft.Column([ft.Row([ft.Text("NeoBank", size=20, weight="bold")], alignment=ft.MainAxisAlignment.CENTER), auth_view], expand=True)
        page.add(mobile_shell(auth_wrap))
        page.update()

    # startup
    if state["user"]:
        switch_to_home()
    else:
        switch_to_auth()

# run
if __name__ == "__main__":
    ft.run(main)

