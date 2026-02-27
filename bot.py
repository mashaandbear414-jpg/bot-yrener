import telebot
import random
import string
import time
import threading
import sqlite3
import os
from datetime import datetime, timedelta
from flask import Flask, request, jsonify

TOKEN = "8601640788:AAFmh2jGX3VrP_jVuiKnfjXE7BH6wZNetgQ"
OWNER_ID = 7568797437

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# ============================================================
#   SQLITE БАЗА ДАННЫХ (сохраняется при перезапуске)
# ============================================================
DB_PATH = "/app/data/yrener.db" if os.path.exists("/app/data") else "yrener.db"

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True) if os.path.dirname(DB_PATH) else None
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS keys (
        key TEXT PRIMARY KEY,
        expire REAL,
        user_id INTEGER,
        type TEXT
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS user_keys (
        user_id INTEGER PRIMARY KEY,
        key TEXT
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS user_last_free (
        user_id INTEGER PRIMARY KEY,
        ts REAL
    )''')
    con.commit()
    con.close()

init_db()

def db():
    return sqlite3.connect(DB_PATH)

def keys_get(key):
    con = db()
    row = con.execute("SELECT expire, user_id, type FROM keys WHERE key=?", (key,)).fetchone()
    con.close()
    if row:
        return {"expire": row[0], "user_id": row[1], "type": row[2]}
    return None

def keys_set(key, expire, user_id, ktype):
    con = db()
    con.execute("INSERT OR REPLACE INTO keys VALUES (?,?,?,?)", (key, expire, user_id, ktype))
    con.commit()
    con.close()

def keys_del(key):
    con = db()
    con.execute("DELETE FROM keys WHERE key=?", (key,))
    con.commit()
    con.close()

def keys_all():
    con = db()
    rows = con.execute("SELECT key, expire, user_id, type FROM keys").fetchall()
    con.close()
    return {r[0]: {"expire": r[1], "user_id": r[2], "type": r[3]} for r in rows}

def user_key_get(user_id):
    con = db()
    row = con.execute("SELECT key FROM user_keys WHERE user_id=?", (user_id,)).fetchone()
    con.close()
    return row[0] if row else None

def user_key_set(user_id, key):
    con = db()
    con.execute("INSERT OR REPLACE INTO user_keys VALUES (?,?)", (user_id, key))
    con.commit()
    con.close()

def last_free_get(user_id):
    con = db()
    row = con.execute("SELECT ts FROM user_last_free WHERE user_id=?", (user_id,)).fetchone()
    con.close()
    return row[0] if row else None

def last_free_set(user_id, ts):
    con = db()
    con.execute("INSERT OR REPLACE INTO user_last_free VALUES (?,?)", (user_id, ts))
    con.commit()
    con.close()

# In-memory (не нужна персистентность)
waiting_support = {}
owner_reply_to = {}
pending_purchase = {}
owner_gen_state = {}

# ============================================================
#   УТИЛИТЫ
# ============================================================
def generate_free_key():
    special = random.choice('.,!?@#')
    digit = random.choice(string.digits)
    letters = random.choices(string.ascii_uppercase, k=4)
    key_list = letters + [digit, special]
    random.shuffle(key_list)
    return ''.join(key_list)

def generate_paid_key():
    specials = random.choices('.,!?@#$', k=2)
    digits = random.choices(string.digits, k=2)
    letters = random.choices(string.ascii_uppercase, k=3)
    key_list = letters + digits + specials
    random.shuffle(key_list)
    return ''.join(key_list)

def fmt_duration(seconds):
    if seconds < 3600:
        return f"{seconds // 60} мин"
    elif seconds < 86400:
        h = seconds // 3600
        return f"{h} ч"
    else:
        d = seconds // 86400
        h = (seconds % 86400) // 3600
        return f"{d}д {h}ч" if h else f"{d} дн"

def get_user_link(user):
    name = user.first_name or "?"
    if user.username:
        return f"[{name}](https://t.me/{user.username})", f"@{user.username}"
    return f"[{name}](tg://user?id={user.id})", "нет"

def main_kb(user_id):
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(telebot.types.KeyboardButton("🔑 Получить ключ"))
    kb.row(telebot.types.KeyboardButton("💎 Купить приватный ключ"))
    kb.row(telebot.types.KeyboardButton("🎲 Кубик"), telebot.types.KeyboardButton("🎰 Слоты"))
    kb.row(telebot.types.KeyboardButton("💬 Связаться с владельцем"))
    if user_id == OWNER_ID:
        kb.row(telebot.types.KeyboardButton("👑 Панель владельца"))
    return kb

# ============================================================
#   FLASK API
# ============================================================
@app.route('/check_key', methods=['GET'])
def check_key():
    key = request.args.get('key', '').strip()
    data = keys_get(key)
    if data:
        if data['expire'] > time.time():
            remaining = int(data['expire'] - time.time())
            return jsonify({"valid": True, "remaining": remaining, "type": data.get("type", "free")})
        else:
            keys_del(key)
    return jsonify({"valid": False, "remaining": 0})

# ============================================================
#   /start
# ============================================================
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(
        message.chat.id,
        f"👋 Привет, *{message.from_user.first_name}*!\n\n"
        f"🎮 Добро пожаловать в *Yrener Menu Bot*\n\n"
        f"🔑 *Бесплатный ключ* — на 1 час, раз в 2 часа\n"
        f"💎 *Приватный ключ* — платный, долгосрочный\n\n"
        f"Выбери действие 👇",
        parse_mode="Markdown",
        reply_markup=main_kb(message.from_user.id)
    )

# ============================================================
#   БЕСПЛАТНЫЙ КЛЮЧ (ровно 1 час, кулдаун 2 часа)
# ============================================================
@bot.message_handler(func=lambda m: m.text == "🔑 Получить ключ")
def get_free_key(message):
    user_id = message.from_user.id
    now = time.time()

    key = user_key_get(user_id)
    if key:
        data = keys_get(key)
        if data and data['expire'] > now:
            remaining = int(data['expire'] - now)
            mins = remaining // 60
            secs = remaining % 60
            bot.send_message(
                message.chat.id,
                f"⏳ У тебя уже есть активный ключ!\n\n"
                f"🔑 Ключ: `{key}`\n"
                f"⏱ Осталось: *{mins}м {secs}с*",
                parse_mode="Markdown"
            )
            return

    last_free = last_free_get(user_id)
    if last_free:
        cooldown = 7200
        passed = now - last_free
        if passed < cooldown:
            wait = int(cooldown - passed)
            mins = wait // 60
            secs = wait % 60
            bot.send_message(
                message.chat.id,
                f"⏰ *Следующий ключ через:*\n\n"
                f"*{mins}м {secs}с*\n\n"
                f"💎 Или купи приватный ключ без ожидания!",
                parse_mode="Markdown"
            )
            return

    key = generate_free_key()
    expire = now + 3600
    keys_set(key, expire, user_id, "free")
    user_key_set(user_id, key)
    last_free_set(user_id, now)
    expire_dt = datetime.now() + timedelta(seconds=3600)

    bot.send_message(
        message.chat.id,
        f"✅ *Ключ создан!*\n\n"
        f"🔑 Ключ: `{key}`\n"
        f"⏱ {fmt_duration(3600)}\n"
        f"📅 До: *{expire_dt.strftime('%d.%m.%Y %H:%M')}*\n\n"
        f"⚠️ Введи ключ в приложении Yrener.\n"
        f"Ровно через час игра закроется!",
        parse_mode="Markdown"
    )

# ============================================================
#   ПОКУПКА ПРИВАТНОГО КЛЮЧА
# ============================================================
DURATIONS = {
    "1ч": 3600, "6ч": 21600, "12ч": 43200,
    "1 день": 86400, "2 дня": 172800, "7 дней": 604800
}

@bot.message_handler(func=lambda m: m.text == "💎 Купить приватный ключ")
def buy_key(message):
    kb = telebot.types.InlineKeyboardMarkup(row_width=3)
    buttons = [telebot.types.InlineKeyboardButton(label, callback_data=f"buy_{label}") for label in DURATIONS]
    kb.add(*buttons)
    kb.add(telebot.types.InlineKeyboardButton("📅 Выбрать дату", callback_data="buy_custom"))
    bot.send_message(
        message.chat.id,
        "💎 *Приватный ключ*\n\nВыбери на сколько нужен ключ:",
        parse_mode="Markdown",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("buy_"))
def handle_buy(call):
    user_id = call.from_user.id
    data = call.data[4:]

    if data == "custom":
        pending_purchase[user_id] = {"step": "wait_date"}
        bot.send_message(
            call.message.chat.id,
            "📅 Введи дату до которой нужен ключ:\n\n"
            "Формат: *ДД.ММ.ГГГГ*\nНапример: *28.02.2026*",
            parse_mode="Markdown"
        )
    elif data in DURATIONS:
        seconds = DURATIONS[data]
        pending_purchase[user_id] = {"step": "confirm", "label": data, "seconds": seconds}
        kb = telebot.types.InlineKeyboardMarkup()
        kb.row(
            telebot.types.InlineKeyboardButton("✅ Отправить запрос", callback_data="confirm_buy"),
            telebot.types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_buy")
        )
        bot.send_message(
            call.message.chat.id,
            f"💎 *Запрос на приватный ключ*\n\n"
            f"⏱ Длительность: *{data}*\n\n"
            f"Отправить запрос владельцу?\nВладелец назначит цену и выдаст ключ.",
            parse_mode="Markdown",
            reply_markup=kb
        )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "confirm_buy")
def confirm_buy(call):
    user_id = call.from_user.id
    if user_id not in pending_purchase:
        bot.answer_callback_query(call.id, "Запрос устарел")
        return
    purchase = pending_purchase.pop(user_id)
    label = purchase.get("label", "?")
    seconds = purchase.get("seconds", 0)

    user = call.from_user
    link, username = get_user_link(user)

    kb = telebot.types.InlineKeyboardMarkup()
    kb.add(telebot.types.InlineKeyboardButton(
        "💬 Назначить цену", callback_data=f"setprice_{user_id}_{seconds}"
    ))

    bot.send_message(
        OWNER_ID,
        f"💎 *Запрос на платный ключ*\n\n"
        f"👤 Имя: {link}\n"
        f"🆔 ID: `{user_id}`\n"
        f"📲 Username: {username}\n"
        f"🔗 Профиль: tg://user?id={user_id}\n"
        f"🕐 Время: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n\n"
        f"⏱ Запрошено: *{label}*",
        parse_mode="Markdown",
        reply_markup=kb
    )

    bot.edit_message_text(
        "✅ *Запрос отправлен!*\n\n"
        "Ожидайте — владелец свяжется с вами и назначит цену.",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "cancel_buy")
def cancel_buy(call):
    pending_purchase.pop(call.from_user.id, None)
    bot.edit_message_text("❌ Отменено.", call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("setprice_"))
def set_price(call):
    if call.from_user.id != OWNER_ID:
        return
    parts = call.data.split("_")
    target_uid = int(parts[1])
    seconds = int(parts[2])
    owner_gen_state[OWNER_ID] = {"step": "wait_price", "user_id": target_uid, "seconds": seconds}
    bot.send_message(OWNER_ID, f"💰 Напиши цену для пользователя `{target_uid}`:\nНапример: *150 руб*", parse_mode="Markdown")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("givekey_"))
def give_paid_key(call):
    if call.from_user.id != OWNER_ID:
        return
    parts = call.data.split("_")
    target_uid = int(parts[1])
    seconds = int(parts[2])

    key = generate_paid_key()
    expire = time.time() + seconds
    expire_dt = datetime.fromtimestamp(expire)
    keys_set(key, expire, target_uid, "paid")
    user_key_set(target_uid, key)

    try:
        bot.send_message(
            target_uid,
            f"🎉 *Твой платный ключ!*\n\n"
            f"💎 Ключ: `{key}`\n"
            f"⏱ Длительность: *{fmt_duration(seconds)}*\n"
            f"📅 Истекает: *{expire_dt.strftime('%d.%m.%Y %H:%M')}*\n\n"
            f"Введи ключ в меню чита!",
            parse_mode="Markdown"
        )
        bot.send_message(OWNER_ID, f"✅ Ключ `{key}` выдан пользователю `{target_uid}`", parse_mode="Markdown")
    except Exception as e:
        bot.send_message(OWNER_ID, f"❌ Ошибка: {e}")
    bot.answer_callback_query(call.id)

# ============================================================
#   ПАНЕЛЬ ВЛАДЕЛЬЦА
# ============================================================
@bot.message_handler(func=lambda m: m.text == "👑 Панель владельца")
def owner_panel(message):
    if message.from_user.id != OWNER_ID:
        return
    all_keys = keys_all()
    now = time.time()
    active_free = sum(1 for v in all_keys.values() if v['expire'] > now and v['type'] == 'free')
    active_paid = sum(1 for v in all_keys.values() if v['expire'] > now and v['type'] == 'paid')
    con = db(); total_users = con.execute("SELECT COUNT(*) FROM user_keys").fetchone()[0]; con.close()

    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        telebot.types.InlineKeyboardButton("🔑 Все ключи", callback_data="owner_all_keys"),
        telebot.types.InlineKeyboardButton("➕ Создать ключ", callback_data="owner_create_key"),
        telebot.types.InlineKeyboardButton("🗑 Удалить ключ", callback_data="owner_delete_key"),
        telebot.types.InlineKeyboardButton("👥 Пользователи", callback_data="owner_users"),
    )
    bot.send_message(
        message.chat.id,
        f"👑 *Панель владельца*\n\n"
        f"👥 Пользователей: *{total_users}*\n"
        f"🔑 Бесплатных ключей: *{active_free}*\n"
        f"💎 Платных ключей: *{active_paid}*\n"
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}",
        parse_mode="Markdown",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data == "owner_all_keys")
def owner_all_keys(call):
    if call.from_user.id != OWNER_ID:
        return
    active = [(k, v) for k, v in keys_all().items() if v['expire'] > time.time()]
    if not active:
        bot.answer_callback_query(call.id, "Нет активных ключей")
        return
    text = "🔑 *Активные ключи:*\n\n"
    for k, v in active:
        mins = int((v['expire'] - time.time()) // 60)
        emoji = "💎" if v.get('type') == 'paid' else "🔑"
        text += f"{emoji} `{k}` — {mins}м | uid:`{v.get('user_id','?')}`\n"
    bot.send_message(call.message.chat.id, text, parse_mode="Markdown")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "owner_users")
def owner_users(call):
    if call.from_user.id != OWNER_ID:
        return
    con = db(); rows = con.execute("SELECT user_id, key FROM user_keys LIMIT 30").fetchall(); con.close()
    if not rows:
        bot.answer_callback_query(call.id, "Нет пользователей")
        return
    text = "👥 *Пользователи:*\n\n"
    for uid, key in rows:
        data = keys_get(key)
        active = data is not None and data["expire"] > time.time()
        status = "🟢" if active else "🔴"
        text += f"{status} `{uid}` — `{key}`\n"
    bot.send_message(call.message.chat.id, text, parse_mode="Markdown")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "owner_create_key")
def owner_create_key_menu(call):
    if call.from_user.id != OWNER_ID:
        return
    kb = telebot.types.InlineKeyboardMarkup(row_width=3)
    durations = [("1 час", 3600), ("6 часов", 21600), ("12 часов", 43200),
                 ("1 день", 86400), ("2 дня", 172800), ("7 дней", 604800)]
    for label, secs in durations:
        kb.add(telebot.types.InlineKeyboardButton(label, callback_data=f"owngen_{secs}"))
    kb.add(telebot.types.InlineKeyboardButton("📅 Своя дата", callback_data="owngen_custom"))
    bot.send_message(call.message.chat.id, "➕ *Создать платный ключ*\n\nВыбери длительность:", parse_mode="Markdown", reply_markup=kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("owngen_"))
def owner_gen_duration(call):
    if call.from_user.id != OWNER_ID:
        return
    val = call.data[7:]
    if val == "custom":
        owner_gen_state[OWNER_ID] = {"step": "wait_custom_date"}
        bot.send_message(call.message.chat.id, "📅 Введи дату:\nФормат: *ДД.ММ.ГГГГ*", parse_mode="Markdown")
    else:
        secs = int(val)
        key = generate_paid_key()
        expire = time.time() + secs
        expire_dt = datetime.fromtimestamp(expire)
        keys_set(key, expire, OWNER_ID, "paid")
        bot.send_message(
            call.message.chat.id,
            f"✅ *Ключ создан!*\n\n💎 Ключ: `{key}`\n⏱ {fmt_duration(secs)}\n📅 До: *{expire_dt.strftime('%d.%m.%Y %H:%M')}*",
            parse_mode="Markdown"
        )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "owner_delete_key")
def owner_delete_menu(call):
    if call.from_user.id != OWNER_ID:
        return
    owner_gen_state[OWNER_ID] = {"step": "wait_delete_key"}
    bot.send_message(call.message.chat.id, "🗑 Введи ключ который нужно удалить:")
    bot.answer_callback_query(call.id)

# ============================================================
#   КУБИК / СЛОТЫ
# ============================================================
@bot.message_handler(func=lambda m: m.text == "🎲 Кубик")
def dice_game(message):
    msg = bot.send_dice(message.chat.id, emoji="🎲")
    value = msg.dice.value
    time.sleep(3)
    if value >= 5:
        bot.send_message(message.chat.id, f"🎉 Выпало *{value}* — выиграл! Получаешь бесплатный ключ 👇", parse_mode="Markdown")

        class FakeMsg:
            chat = message.chat
            from_user = message.from_user
        get_free_key(FakeMsg())
    else:
        bot.send_message(message.chat.id, f"😢 Выпало *{value}* — не повезло!", parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text == "🎰 Слоты")
def slots_game(message):
    msg = bot.send_dice(message.chat.id, emoji="🎰")
    value = msg.dice.value
    time.sleep(2)
    if value == 64:
        bot.send_message(message.chat.id, "🏆 ДЖЕКПОТ! Напиши владельцу за наградой!")
    elif value in [1, 22, 43]:
        bot.send_message(message.chat.id, "🎉 Три одинаковых! Небольшой выигрыш!")
    else:
        bot.send_message(message.chat.id, "😔 Не повезло! Попробуй ещё раз!")

# ============================================================
#   ПОДДЕРЖКА
# ============================================================
@bot.message_handler(func=lambda m: m.text == "💬 Связаться с владельцем")
def support_start(message):
    if message.from_user.id == OWNER_ID:
        bot.send_message(message.chat.id, "Ты и есть владелец 😄")
        return
    waiting_support[message.from_user.id] = True
    bot.send_message(
        message.chat.id,
        "💬 *Связь с владельцем*\n\nНапиши сообщение, владелец ответит.\n\nОтмена: /cancel",
        parse_mode="Markdown"
    )

@bot.message_handler(commands=['cancel'])
def cancel_cmd(message):
    uid = message.from_user.id
    waiting_support.pop(uid, None)
    pending_purchase.pop(uid, None)
    if uid == OWNER_ID:
        owner_gen_state.pop(OWNER_ID, None)
        owner_reply_to.pop(OWNER_ID, None)
    bot.send_message(message.chat.id, "❌ Отменено.", reply_markup=main_kb(uid))

@bot.callback_query_handler(func=lambda c: c.data.startswith("reply_"))
def reply_to_user(call):
    if call.from_user.id != OWNER_ID:
        return
    target_id = int(call.data.split("_")[1])
    owner_reply_to[OWNER_ID] = target_id
    bot.send_message(OWNER_ID, f"✏️ Напиши ответ пользователю `{target_id}`:", parse_mode="Markdown")
    bot.answer_callback_query(call.id)

# ============================================================
#   ОБРАБОТКА ВСЕХ СООБЩЕНИЙ
# ============================================================
@bot.message_handler(func=lambda m: True)
def handle_all(message):
    user_id = message.from_user.id
    text = message.text or ""

    # ── Владелец в состоянии ввода ──
    if user_id == OWNER_ID and OWNER_ID in owner_gen_state:
        state = owner_gen_state[OWNER_ID]
        step = state["step"]

        if step == "wait_price":
            target_uid = state["user_id"]
            seconds = state["seconds"]
            del owner_gen_state[OWNER_ID]
            kb = telebot.types.InlineKeyboardMarkup()
            kb.add(telebot.types.InlineKeyboardButton(
                f"✅ Выдать ключ ({fmt_duration(seconds)})",
                callback_data=f"givekey_{target_uid}_{seconds}"
            ))
            try:
                bot.send_message(
                    target_uid,
                    f"💬 *Ответ от владельца*\n\n"
                    f"Твой запрос на ключ *{fmt_duration(seconds)}* рассмотрен!\n\n"
                    f"💰 Цена: *{text}*\n\nПосле оплаты напиши владельцу.",
                    parse_mode="Markdown"
                )
                bot.send_message(OWNER_ID, f"✅ Цена отправлена!\nПосле оплаты нажми кнопку:", reply_markup=kb)
            except Exception as e:
                bot.send_message(OWNER_ID, f"❌ Ошибка: {e}")
            return

        if step == "wait_custom_date":
            del owner_gen_state[OWNER_ID]
            now = datetime.now()
            try:
                target = datetime.strptime(text.strip(), "%d.%m.%Y")
                target = target.replace(hour=23, minute=59, second=59)
                if target <= now:
                    bot.send_message(OWNER_ID, "❌ Дата уже прошла!")
                    return
                seconds = int((target - now).total_seconds())
                key = generate_paid_key()
                expire = time.time() + seconds
                keys_set(key, expire, OWNER_ID, "paid")
                bot.send_message(
                    OWNER_ID,
                    f"✅ *Ключ создан!*\n\n💎 Ключ: `{key}`\n📅 До: *{text.strip()}*\n⏱ {fmt_duration(seconds)}",
                    parse_mode="Markdown"
                )
            except ValueError:
                bot.send_message(OWNER_ID, "❌ Неверный формат! Используй ДД.ММ.ГГГГ")
            return

        if step == "wait_delete_key":
            del owner_gen_state[OWNER_ID]
            key = text.strip()
            if keys_get(key):
                keys_del(key)
                bot.send_message(OWNER_ID, f"✅ Ключ `{key}` удалён.", parse_mode="Markdown")
            else:
                bot.send_message(OWNER_ID, f"❌ Ключ не найден.", parse_mode="Markdown")
            return

    # ── Владелец отвечает пользователю ──
    if user_id == OWNER_ID and OWNER_ID in owner_reply_to:
        target_id = owner_reply_to.pop(OWNER_ID)
        try:
            bot.send_message(target_id, f"📨 *Ответ от владельца:*\n\n{text}", parse_mode="Markdown")
            bot.send_message(OWNER_ID, "✅ Ответ отправлен!")
        except Exception as e:
            bot.send_message(OWNER_ID, f"❌ Ошибка: {e}")
        return

    # ── Пользователь вводит кастомную дату покупки ──
    if user_id in pending_purchase and pending_purchase[user_id].get("step") == "wait_date":
        now = datetime.now()
        try:
            target = datetime.strptime(text.strip(), "%d.%m.%Y")
            target = target.replace(hour=23, minute=59, second=59)
            if target <= now:
                bot.send_message(message.chat.id, "❌ Дата уже прошла!")
                return
            seconds = int((target - now).total_seconds())
            label = f"до {text.strip()}"
            pending_purchase[user_id] = {"step": "confirm", "label": label, "seconds": seconds}
            kb = telebot.types.InlineKeyboardMarkup()
            kb.row(
                telebot.types.InlineKeyboardButton("✅ Отправить запрос", callback_data="confirm_buy"),
                telebot.types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_buy")
            )
            bot.send_message(
                message.chat.id,
                f"📅 *Подтверждение*\n\n"
                f"Сегодня: *{now.strftime('%d.%m.%Y')}*\n"
                f"До: *{text.strip()}*\n"
                f"Длительность: *{fmt_duration(seconds)}*\n\n"
                f"Отправить запрос владельцу?",
                parse_mode="Markdown",
                reply_markup=kb
            )
        except ValueError:
            bot.send_message(message.chat.id, "❌ Неверный формат! Используй *ДД.ММ.ГГГГ*", parse_mode="Markdown")
        return

    # ── Пользователь пишет в поддержку ──
    if user_id in waiting_support:
        waiting_support.pop(user_id)
        link, username = get_user_link(message.from_user)
        kb = telebot.types.InlineKeyboardMarkup()
        kb.add(telebot.types.InlineKeyboardButton("💬 Ответить", callback_data=f"reply_{user_id}"))
        bot.send_message(
            OWNER_ID,
            f"📩 *Сообщение в поддержку*\n\n"
            f"👤 Имя: {link}\n"
            f"🆔 ID: `{user_id}`\n"
            f"📲 Username: {username}\n"
            f"🔗 Профиль: tg://user?id={user_id}\n"
            f"🕐 Время: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n\n"
            f"💬 *Сообщение:*\n{text}",
            parse_mode="Markdown",
            reply_markup=kb
        )
        bot.send_message(message.chat.id, "✅ Сообщение отправлено! Ожидайте.", reply_markup=main_kb(user_id))

# ============================================================
#   ЗАПУСК
# ============================================================
def run_flask():
    app.run(host='0.0.0.0', port=8080, debug=False)

if __name__ == "__main__":
    print("✅ Yrener Menu Bot запущен!")
    print("🌐 Flask API на порту 8080")
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    bot.infinity_polling()
