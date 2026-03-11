import telebot
import random
import string
import time
import os
import threading
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file

TOKEN = "8601640788:AAFmh2jGX3VrP_jVuiKnfjXE7BH6wZNetgQ"
OWNER_ID = 7568797437
SO_PATH = "libluosu.so"  # положи .so рядом с bot.py

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# ============================================================
#   ХРАНИЛИЩЕ
# ============================================================
keys = {}
user_keys = {}
user_last_free = {}
waiting_support = {}
owner_reply_to = {}
pending_purchase = {}
owner_gen_state = {}
all_users = set()    # все user_id кто писал боту
broadcast_state = {} # OWNER_ID -> True

# ============================================================
#   УТИЛИТЫ
# ============================================================
def generate_key():
    """Формат: Yrener_XXXX#XXXX#XXXX"""
    def block():
        chars = string.ascii_uppercase + string.digits
        return ''.join(random.choices(chars, k=4))
    return f"Yrener_{block()}#{block()}#{block()}"

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
    if key in keys:
        data = keys[key]
        if data['expire'] > time.time():
            remaining = int(data['expire'] - time.time())
            return jsonify({
                "valid": True,
                "remaining": remaining,
                "type": data.get("type", "free")
            })
        else:
            del keys[key]
    return jsonify({"valid": False, "remaining": 0})

@app.route('/download_so', methods=['GET'])
def download_so():
    """Скачать .so файл (только с валидным ключом)"""
    key = request.args.get('key', '').strip()
    if key not in keys or keys[key]['expire'] <= time.time():
        return jsonify({"error": "Invalid key"}), 403
    if not os.path.exists(SO_PATH):
        return jsonify({"error": "File not found"}), 404
    return send_file(SO_PATH, as_attachment=True, download_name="libluosu.so")

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "ok", "time": datetime.now().strftime('%d.%m.%Y %H:%M:%S')})

# ============================================================
#   /start
# ============================================================
@bot.message_handler(commands=['start'])

def start(message):
    all_users.add(message.from_user.id)
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
#   БЕСПЛАТНЫЙ КЛЮЧ
# ============================================================
@bot.message_handler(func=lambda m: m.text == "🔑 Получить ключ")
def get_free_key(message):
    user_id = message.from_user.id
    now = time.time()

    if user_id in user_keys:
        key = user_keys[user_id]
        if key in keys and keys[key]['expire'] > now:
            remaining = int(keys[key]['expire'] - now)
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

    if user_id in user_last_free:
        cooldown = 7200
        passed = now - user_last_free[user_id]
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

    key = generate_key()
    expire = now + 3600
    keys[key] = {"expire": expire, "user_id": user_id, "type": "free"}
    user_keys[user_id] = key
    user_last_free[user_id] = now
    expire_dt = datetime.now() + timedelta(seconds=3600)

    bot.send_message(
        message.chat.id,
        f"✅ *Твой ключ готов!*\n\n"
        f"🔑 Ключ: `{key}`\n"
        f"⏱ Действует до: *{expire_dt.strftime('%H:%M:%S')}*\n"
        f"📅 Дата: *{expire_dt.strftime('%d.%m.%Y')}*\n\n"
        f"⚠️ Введи ключ в лоадере Yrener.\n"
        f"Ровно через час доступ закроется!",
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
        bot.send_message(call.message.chat.id,
            "📅 Введи дату до которой нужен ключ:\n\nФормат: *ДД.ММ.ГГГГ*\nНапример: *28.02.2026*",
            parse_mode="Markdown")
    elif data in DURATIONS:
        seconds = DURATIONS[data]
        pending_purchase[user_id] = {"step": "confirm", "label": data, "seconds": seconds}
        kb = telebot.types.InlineKeyboardMarkup()
        kb.row(
            telebot.types.InlineKeyboardButton("✅ Отправить запрос", callback_data="confirm_buy"),
            telebot.types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_buy")
        )
        bot.send_message(call.message.chat.id,
            f"💎 *Запрос на приватный ключ*\n\n⏱ Длительность: *{data}*\n\nОтправить запрос владельцу?",
            parse_mode="Markdown", reply_markup=kb)
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
    kb.add(telebot.types.InlineKeyboardButton("💬 Назначить цену", callback_data=f"setprice_{user_id}_{seconds}"))
    bot.send_message(OWNER_ID,
        f"💎 *Запрос на платный ключ*\n\n👤 Имя: {link}\n🆔 ID: `{user_id}`\n"
        f"📲 Username: {username}\n🕐 Время: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n\n⏱ Запрошено: *{label}*",
        parse_mode="Markdown", reply_markup=kb)
    bot.edit_message_text("✅ *Запрос отправлен!*\n\nОжидайте — владелец свяжется с вами.",
        call.message.chat.id, call.message.message_id, parse_mode="Markdown")
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
    bot.send_message(OWNER_ID, f"💰 Напиши цену для `{target_uid}`:\nНапример: *150 руб*", parse_mode="Markdown")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("givekey_"))
def give_paid_key(call):
    if call.from_user.id != OWNER_ID:
        return
    parts = call.data.split("_")
    target_uid = int(parts[1])
    seconds = int(parts[2])
    key = generate_key()
    expire = time.time() + seconds
    expire_dt = datetime.fromtimestamp(expire)
    keys[key] = {"expire": expire, "user_id": target_uid, "type": "paid"}
    user_keys[target_uid] = key
    try:
        bot.send_message(target_uid,
            f"🎉 *Твой платный ключ!*\n\n💎 Ключ: `{key}`\n⏱ Длительность: *{fmt_duration(seconds)}*\n"
            f"📅 Истекает: *{expire_dt.strftime('%d.%m.%Y %H:%M')}*\n\nВведи ключ в лоадере Yrener!",
            parse_mode="Markdown")
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
    active_free = sum(1 for v in keys.values() if v['expire'] > time.time() and v['type'] == 'free')
    active_paid = sum(1 for v in keys.values() if v['expire'] > time.time() and v['type'] == 'paid')
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        telebot.types.InlineKeyboardButton("🔑 Все ключи", callback_data="owner_all_keys"),
        telebot.types.InlineKeyboardButton("➕ Создать ключ", callback_data="owner_create_key"),
        telebot.types.InlineKeyboardButton("🗑 Удалить ключ", callback_data="owner_delete_key"),
        telebot.types.InlineKeyboardButton("👥 Пользователи", callback_data="owner_users"),
    )
    bot.send_message(message.chat.id,
        f"👑 *Панель владельца*\n\n👥 Пользователей: *{len(user_keys)}*\n"
        f"🔑 Бесплатных: *{active_free}*\n💎 Платных: *{active_paid}*\n"
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}",
        parse_mode="Markdown", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "owner_all_keys")
def owner_all_keys(call):
    if call.from_user.id != OWNER_ID:
        return
    active = [(k, v) for k, v in keys.items() if v['expire'] > time.time()]
    if not active:
        bot.answer_callback_query(call.id, "Нет активных ключей")
        return
    text = "🔑 *Активные ключи:*\n\n"
    for k, v in active:
        mins = int((v['expire'] - time.time()) // 60)
        emoji = "💎" if v.get('type') == 'paid' else "🔑"
        text += f"{emoji} `{k}` — {mins}м\n"
    bot.send_message(call.message.chat.id, text, parse_mode="Markdown")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "owner_users")
def owner_users(call):
    if call.from_user.id != OWNER_ID:
        return
    if not user_keys:
        bot.answer_callback_query(call.id, "Нет пользователей")
        return
    text = "👥 *Пользователи:*\n\n"
    for uid, key in list(user_keys.items())[:30]:
        active = key in keys and keys[key]['expire'] > time.time()
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
    bot.send_message(call.message.chat.id, "➕ *Создать ключ*\n\nВыбери длительность:",
        parse_mode="Markdown", reply_markup=kb)
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
        key = generate_key()
        expire = time.time() + secs
        expire_dt = datetime.fromtimestamp(expire)
        keys[key] = {"expire": expire, "user_id": OWNER_ID, "type": "paid"}
        bot.send_message(call.message.chat.id,
            f"✅ *Ключ создан!*\n\n💎 Ключ: `{key}`\n⏱ {fmt_duration(secs)}\n📅 До: *{expire_dt.strftime('%d.%m.%Y %H:%M')}*",
            parse_mode="Markdown")
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
    bot.send_message(message.chat.id,
        "💬 *Связь с владельцем*\n\nНапиши сообщение, владелец ответит.\n\nОтмена: /cancel",
        parse_mode="Markdown")

@bot.message_handler(commands=['cancel'])
def cancel_cmd(message):
    uid = message.from_user.id
    waiting_support.pop(uid, None)
    pending_purchase.pop(uid, None)
    if uid == OWNER_ID:
        owner_gen_state.pop(OWNER_ID, None)
        owner_reply_to.pop(OWNER_ID, None)
        broadcast_state.pop(OWNER_ID, None)
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
    all_users.add(user_id)

    # ── Рассылка ──
    if user_id == OWNER_ID and broadcast_state.get(OWNER_ID):
        del broadcast_state[OWNER_ID]
        count = 0
        failed = 0
        for uid in list(all_users):
            if uid == OWNER_ID:
                continue
            try:
                bot.send_message(uid, "📢 *Объявление от Yrener:*\n\n" + text, parse_mode="Markdown")
                count += 1
            except:
                failed += 1
        bot.send_message(OWNER_ID,
            f"✅ Рассылка завершена!\n📨 Отправлено: *{count}*\n❌ Не доставлено: *{failed}*",
            parse_mode="Markdown")
        return

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
                bot.send_message(target_uid,
                    f"💬 *Ответ от владельца*\n\nТвой запрос рассмотрен!\n\n💰 Цена: *{text}*\n\nПосле оплаты напиши владельцу.",
                    parse_mode="Markdown")
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
                key = generate_key()
                expire = time.time() + seconds
                keys[key] = {"expire": expire, "user_id": OWNER_ID, "type": "paid"}
                bot.send_message(OWNER_ID,
                    f"✅ *Ключ создан!*\n\n💎 Ключ: `{key}`\n📅 До: *{text.strip()}*\n⏱ {fmt_duration(seconds)}",
                    parse_mode="Markdown")
            except ValueError:
                bot.send_message(OWNER_ID, "❌ Неверный формат! Используй ДД.ММ.ГГГГ")
            return

        if step == "wait_delete_key":
            del owner_gen_state[OWNER_ID]
            key = text.strip()
            if key in keys:
                del keys[key]
                bot.send_message(OWNER_ID, f"✅ Ключ `{key}` удалён.", parse_mode="Markdown")
            else:
                bot.send_message(OWNER_ID, f"❌ Ключ не найден.", parse_mode="Markdown")
            return

    if user_id == OWNER_ID and OWNER_ID in owner_reply_to:
        target_id = owner_reply_to.pop(OWNER_ID)
        try:
            bot.send_message(target_id, f"📨 *Ответ от владельца:*\n\n{text}", parse_mode="Markdown")
            bot.send_message(OWNER_ID, "✅ Ответ отправлен!")
        except Exception as e:
            bot.send_message(OWNER_ID, f"❌ Ошибка: {e}")
        return

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
            bot.send_message(message.chat.id,
                f"📅 *Подтверждение*\n\nДо: *{text.strip()}*\nДлительность: *{fmt_duration(seconds)}*\n\nОтправить запрос?",
                parse_mode="Markdown", reply_markup=kb)
        except ValueError:
            bot.send_message(message.chat.id, "❌ Неверный формат! Используй *ДД.ММ.ГГГГ*", parse_mode="Markdown")
        return

    if user_id in waiting_support:
        waiting_support.pop(user_id)
        link, username = get_user_link(message.from_user)
        kb = telebot.types.InlineKeyboardMarkup()
        kb.add(telebot.types.InlineKeyboardButton("💬 Ответить", callback_data=f"reply_{user_id}"))
        bot.send_message(OWNER_ID,
            f"📩 *Сообщение в поддержку*\n\n👤 Имя: {link}\n🆔 ID: `{user_id}`\n"
            f"📲 Username: {username}\n🕐 Время: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n\n💬 *Сообщение:*\n{text}",
            parse_mode="Markdown", reply_markup=kb)
        bot.send_message(message.chat.id, "✅ Сообщение отправлено! Ожидайте.", reply_markup=main_kb(user_id))

# ============================================================
#   ЗАПУСК
# ============================================================
@bot.callback_query_handler(func=lambda c: c.data == "owner_broadcast")
def owner_broadcast(call):
    if call.from_user.id != OWNER_ID:
        return
    broadcast_state[OWNER_ID] = True
    count = len(all_users) - 1
    bot.send_message(OWNER_ID,
        f"📢 *Рассылка*\n\nВсего получателей: *{count}*\n\nНапиши текст объявления — отправлю всем пользователям.\n\nОтмена: /cancel",
        parse_mode="Markdown")
    bot.answer_callback_query(call.id)

def run_flask():
    app.run(host='0.0.0.0', port=8080, debug=False)

if __name__ == "__main__":
    print("✅ Yrener Menu Bot запущен!")
    print("🌐 Flask API на порту 8080")
    print(f"📡 Эндпоинты:")
    print(f"   GET /check_key?key=Yrener_XXXX#XXXX#XXXX")
    print(f"   GET /download_so?key=Yrener_XXXX#XXXX#XXXX")
    print(f"   GET /ping")
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    bot.infinity_polling()
