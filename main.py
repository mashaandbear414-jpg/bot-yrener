"""
Yrener Menu Bot — PostgreSQL edition (Railway-ready)
Зависимости: pip install pyTelegramBotAPI flask psycopg2-binary cryptography python-dotenv
"""

import os
import time
import json
import base64
import random
import string
import threading
import logging
from datetime import datetime, timedelta

import telebot
import psycopg2
import psycopg2.pool
from flask import Flask, request, jsonify, abort
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ============================================================
#   КОНФИГ (всё через переменные окружения — Railway-friendly)
# ============================================================
TOKEN      = os.environ.get("BOT_TOKEN", "8601640788:AAFmh2jGX3VrP_jVuiKnfjXE7BH6wZNetgQ")
OWNER_ID   = int(os.environ.get("OWNER_ID", "7568797437"))
DATABASE_URL = os.environ["DATABASE_URL"]     # postgres://user:pass@host:5432/db

# 32-байтный AES-ключ для шифрования ответов Android-клиента.
# Генерация: python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"
_AES_KEY_B64 = os.environ["AES_KEY_B64"]
AES_KEY: bytes = base64.b64decode(_AES_KEY_B64)
assert len(AES_KEY) == 32, "AES_KEY_B64 должен кодировать ровно 32 байта"

# Опциональный статичный API-токен для эндпоинтов (X-Api-Key header)
API_SECRET = os.environ.get("API_SECRET", "")  # оставьте пустым, чтобы отключить проверку


# ============================================================
#   ПУЛЛ ПОДКЛЮЧЕНИЙ PostgreSQL
# ============================================================
_pool: psycopg2.pool.ThreadedConnectionPool | None = None

def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(2, 10, DATABASE_URL,
                                                      connect_timeout=5)
    return _pool


class DBConn:
    """Контекстный менеджер: берёт соединение из пула, возвращает обратно."""
    def __enter__(self):
        self.conn = get_pool().getconn()
        self.conn.autocommit = False
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.conn.rollback()
        else:
            self.conn.commit()
        get_pool().putconn(self.conn)


# ============================================================
#   ИНИЦИАЛИЗАЦИЯ БД
# ============================================================
def init_db() -> None:
    with DBConn() as con:
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS keys (
                key       TEXT PRIMARY KEY,
                expire    DOUBLE PRECISION NOT NULL,
                user_id   BIGINT,
                type      TEXT NOT NULL DEFAULT 'free'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_keys (
                user_id   BIGINT PRIMARY KEY,
                key       TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_last_free (
                user_id   BIGINT PRIMARY KEY,
                ts        DOUBLE PRECISION NOT NULL
            )
        """)
        # Таблица всех пользователей которые запускали бота (для рассылки)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS all_users (
                user_id   BIGINT PRIMARY KEY,
                username  TEXT,
                first_name TEXT,
                joined_at DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())
            )
        """)
        # Индекс для быстрого поиска просроченных ключей
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_keys_expire ON keys(expire)
        """)
        log.info("БД инициализирована.")


# ============================================================
#   CRUD-ХЕЛПЕРЫ
# ============================================================
def keys_get(key: str) -> dict | None:
    with DBConn() as con:
        cur = con.cursor()
        cur.execute("SELECT expire, user_id, type FROM keys WHERE key=%s", (key,))
        row = cur.fetchone()
    return {"expire": row[0], "user_id": row[1], "type": row[2]} if row else None


def keys_set(key: str, expire: float, user_id: int, ktype: str) -> None:
    with DBConn() as con:
        con.cursor().execute(
            """INSERT INTO keys(key, expire, user_id, type) VALUES(%s,%s,%s,%s)
               ON CONFLICT(key) DO UPDATE SET expire=EXCLUDED.expire,
               user_id=EXCLUDED.user_id, type=EXCLUDED.type""",
            (key, expire, user_id, ktype)
        )


def keys_del(key: str) -> None:
    with DBConn() as con:
        con.cursor().execute("DELETE FROM keys WHERE key=%s", (key,))


def keys_all() -> dict:
    with DBConn() as con:
        cur = con.cursor()
        cur.execute("SELECT key, expire, user_id, type FROM keys")
        rows = cur.fetchall()
    return {r[0]: {"expire": r[1], "user_id": r[2], "type": r[3]} for r in rows}


def user_key_get(user_id: int) -> str | None:
    with DBConn() as con:
        cur = con.cursor()
        cur.execute("SELECT key FROM user_keys WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
    return row[0] if row else None


def user_key_set(user_id: int, key: str) -> None:
    with DBConn() as con:
        con.cursor().execute(
            "INSERT INTO user_keys(user_id, key) VALUES(%s,%s) ON CONFLICT(user_id) DO UPDATE SET key=EXCLUDED.key",
            (user_id, key)
        )


def last_free_get(user_id: int) -> float | None:
    with DBConn() as con:
        cur = con.cursor()
        cur.execute("SELECT ts FROM user_last_free WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
    return row[0] if row else None


def last_free_set(user_id: int, ts: float) -> None:
    with DBConn() as con:
        con.cursor().execute(
            "INSERT INTO user_last_free(user_id, ts) VALUES(%s,%s) ON CONFLICT(user_id) DO UPDATE SET ts=EXCLUDED.ts",
            (user_id, ts)
        )


def save_user(user) -> None:
    """Сохраняет пользователя в таблицу all_users (для рассылки)."""
    with DBConn() as con:
        con.cursor().execute(
            """INSERT INTO all_users(user_id, username, first_name)
               VALUES(%s,%s,%s)
               ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username, first_name=EXCLUDED.first_name""",
            (user.id, user.username, user.first_name)
        )


def get_all_user_ids() -> list[int]:
    """Возвращает список всех user_id кто запускал бота."""
    with DBConn() as con:
        cur = con.cursor()
        cur.execute("SELECT user_id FROM all_users")
        rows = cur.fetchall()
    return [r[0] for r in rows]


# ============================================================
#   ШИФРОВАНИЕ (AES-256-GCM)
# ============================================================
def encrypt_response(payload: dict) -> dict:
    """
    Шифрует словарь в AES-256-GCM.
    Возвращает JSON-safe структуру:
        { "iv": "<base64>", "ciphertext": "<base64>" }
    Android-клиент расшифровывает тем же ключом.
    """
    aesgcm = AESGCM(AES_KEY)
    nonce = os.urandom(12)                         # 96-bit IV (GCM-стандарт)
    plaintext = json.dumps(payload, ensure_ascii=False).encode()
    ct = aesgcm.encrypt(nonce, plaintext, None)    # включает тег аутентификации
    return {
        "iv":         base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ct).decode(),
    }


# ============================================================
#   FLASK APP
# ============================================================
app = Flask(__name__)


def _check_api_secret() -> None:
    """Проверяет заголовок X-Api-Key если API_SECRET задан."""
    if API_SECRET and request.headers.get("X-Api-Key") != API_SECRET:
        abort(403)


# ── /check_key  (старый эндпоинт, обратная совместимость) ──
@app.route("/check_key", methods=["GET"])
def check_key():
    _check_api_secret()
    key = request.args.get("key", "").strip()
    data = keys_get(key)
    if data and data["expire"] > time.time():
        remaining = int(data["expire"] - time.time())
        return jsonify({"valid": True, "remaining": remaining, "type": data.get("type", "free")})
    if data:
        keys_del(key)
    return jsonify({"valid": False, "remaining": 0})


# ── /verify_access  (новый защищённый эндпоинт для Android) ──
@app.route("/verify_access", methods=["POST"])
def verify_access():
    """
    POST /verify_access
    Headers:
        Content-Type: application/json
        X-Api-Key: <API_SECRET>          ← опционально, если задан в env
    Body:
        { "key": "<license_key>" }

    Ответ (200 всегда, статус внутри зашифрованного payload):
        {
            "iv": "<base64>",
            "ciphertext": "<base64>"
        }

    Расшифрованный payload:
        {
            "status":    "ok" | "expired" | "invalid",
            "type":      "free" | "paid" | null,
            "remaining": <секунды> | 0,
            "expires_at": "<ISO-8601>" | null,
            "ts":        <unix-time запроса>
        }

    Android: AES/GCM/NoPadding, key=SHA-256(AES_KEY_B64), IV=iv поле
    """
    _check_api_secret()

    body = request.get_json(silent=True) or {}
    key  = str(body.get("key", "")).strip()

    now  = time.time()
    ts   = int(now)

    if not key:
        payload = {"status": "invalid", "type": None, "remaining": 0, "expires_at": None, "ts": ts}
        return jsonify(encrypt_response(payload)), 200

    data = keys_get(key)

    if data is None:
        payload = {"status": "invalid", "type": None, "remaining": 0, "expires_at": None, "ts": ts}

    elif data["expire"] <= now:
        keys_del(key)
        payload = {"status": "expired", "type": data["type"], "remaining": 0, "expires_at": None, "ts": ts}

    else:
        remaining = int(data["expire"] - now)
        expires_iso = datetime.utcfromtimestamp(data["expire"]).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "status":     "ok",
            "type":       data["type"],
            "remaining":  remaining,
            "expires_at": expires_iso,
            "ts":         ts,
        }

    log.info("verify_access key=%.4s… → %s", key, payload["status"])
    return jsonify(encrypt_response(payload)), 200


@app.errorhandler(403)
def forbidden(e):
    return jsonify({"error": "forbidden"}), 403

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "method not allowed"}), 405


# ============================================================
#   In-memory состояния бота
# ============================================================
waiting_support  = {}
owner_reply_to   = {}
pending_purchase = {}
owner_gen_state  = {}
owner_broadcast  = {}   # НОВОЕ: состояние рассылки


# ============================================================
#   УТИЛИТЫ
# ============================================================
def generate_free_key() -> str:
    special = random.choice(".,!?@#")
    digit   = random.choice(string.digits)
    letters = random.choices(string.ascii_uppercase, k=4)
    key_list = letters + [digit, special]
    random.shuffle(key_list)
    return "".join(key_list)

def generate_paid_key() -> str:
    specials = random.choices(".,!?@#$", k=2)
    digits   = random.choices(string.digits, k=2)
    letters  = random.choices(string.ascii_uppercase, k=3)
    key_list = letters + digits + specials
    random.shuffle(key_list)
    return "".join(key_list)

def fmt_duration(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60} мин"
    elif seconds < 86400:
        return f"{seconds // 3600} ч"
    else:
        d, h = seconds // 86400, (seconds % 86400) // 3600
        return f"{d}д {h}ч" if h else f"{d} дн"

def get_user_link(user):
    name = user.first_name or "?"
    if user.username:
        return f"[{name}](https://t.me/{user.username})", f"@{user.username}"
    return f"[{name}](tg://user?id={user.id})", "нет"

def main_kb(user_id: int):
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(telebot.types.KeyboardButton("🔑 Получить ключ"))
    kb.row(telebot.types.KeyboardButton("💎 Купить приватный ключ"))
    kb.row(telebot.types.KeyboardButton("🎲 Кубик"), telebot.types.KeyboardButton("🎰 Слоты"))
    kb.row(telebot.types.KeyboardButton("💬 Связаться с владельцем"))
    if user_id == OWNER_ID:
        kb.row(telebot.types.KeyboardButton("👑 Панель владельца"))
    return kb


# ============================================================
#   TELEGRAM-БОТ (остальные хендлеры без изменений логики)
# ============================================================
bot = telebot.TeleBot(TOKEN, parse_mode=None)

@bot.message_handler(commands=["start"])
def start(message):
    # Сохраняем пользователя для рассылки
    save_user(message.from_user)
    bot.send_message(
        message.chat.id,
        f"👋 Привет, *{message.from_user.first_name}*!\n\n"
        "🎮 Добро пожаловать в *Yrener Menu Bot*\n\n"
        "🔑 *Бесплатный ключ* — на 1 час, раз в 2 часа\n"
        "💎 *Приватный ключ* — платный, долгосрочный\n\n"
        "Выбери действие 👇",
        parse_mode="Markdown",
        reply_markup=main_kb(message.from_user.id)
    )

@bot.message_handler(func=lambda m: m.text == "🔑 Получить ключ")
def get_free_key(message):
    user_id = message.from_user.id
    now = time.time()
    key = user_key_get(user_id)
    if key:
        data = keys_get(key)
        if data and data["expire"] > now:
            remaining = int(data["expire"] - now)
            bot.send_message(
                message.chat.id,
                f"⏳ У тебя уже есть активный ключ!\n\n"
                f"🔑 Ключ: `{key}`\n"
                f"⏱ Осталось: *{remaining // 60}м {remaining % 60}с*",
                parse_mode="Markdown"
            )
            return
    last_free = last_free_get(user_id)
    if last_free:
        passed = now - last_free
        if passed < 7200:
            wait = int(7200 - passed)
            bot.send_message(
                message.chat.id,
                f"⏰ *Следующий ключ через:*\n\n*{wait // 60}м {wait % 60}с*\n\n"
                "💎 Или купи приватный ключ без ожидания!",
                parse_mode="Markdown"
            )
            return
    new_key = generate_free_key()
    expire = now + 3600
    keys_set(new_key, expire, user_id, "free")
    user_key_set(user_id, new_key)
    last_free_set(user_id, now)
    bot.send_message(
        message.chat.id,
        f"✅ *Твой бесплатный ключ:*\n\n`{new_key}`\n\n⏱ Действует *1 час*",
        parse_mode="Markdown"
    )

@bot.message_handler(func=lambda m: m.text == "🎲 Кубик")
def dice_game(message):
    msg = bot.send_dice(message.chat.id, emoji="🎲")
    value = msg.dice.value
    time.sleep(3)
    if value >= 5:
        bot.send_message(message.chat.id, f"🎉 Выпало *{value}* — выиграл! Получаешь ключ 👇", parse_mode="Markdown")
        class FakeMsg:
            chat      = message.chat
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

@bot.message_handler(func=lambda m: m.text == "💬 Связаться с владельцем")
def support_start(message):
    if message.from_user.id == OWNER_ID:
        bot.send_message(message.chat.id, "Ты и есть владелец 😄")
        return
    waiting_support[message.from_user.id] = True
    bot.send_message(message.chat.id,
        "💬 *Связь с владельцем*\n\nНапиши сообщение, владелец ответит.\n\nОтмена: /cancel",
        parse_mode="Markdown")

# ============================================================
#   ПАНЕЛЬ ВЛАДЕЛЬЦА — рассылка
# ============================================================
@bot.message_handler(func=lambda m: m.text == "👑 Панель владельца" and m.from_user.id == OWNER_ID)
def owner_panel(message):
    kb = telebot.types.InlineKeyboardMarkup()
    kb.row(
        telebot.types.InlineKeyboardButton("📢 Рассылка всем", callback_data="broadcast_start"),
        telebot.types.InlineKeyboardButton("📊 Статистика",    callback_data="owner_stats"),
    )
    kb.row(
        telebot.types.InlineKeyboardButton("🔑 Создать ключ",  callback_data="owner_genkey"),
        telebot.types.InlineKeyboardButton("🗑 Удалить ключ",  callback_data="owner_delkey"),
    )
    bot.send_message(
        message.chat.id,
        "👑 *Панель разработчика*\n\nВыбери действие:",
        parse_mode="Markdown",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data == "broadcast_start")
def broadcast_start(call):
    if call.from_user.id != OWNER_ID:
        return
    owner_broadcast[OWNER_ID] = {"step": "wait_text"}
    bot.send_message(
        OWNER_ID,
        "📢 *Рассылка*\n\nНапиши текст сообщения которое хочешь отправить всем пользователям.\n\nОтмена: /cancel",
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "broadcast_confirm")
def broadcast_confirm(call):
    if call.from_user.id != OWNER_ID:
        return
    state = owner_broadcast.get(OWNER_ID, {})
    text = state.get("text", "")
    if not text:
        bot.answer_callback_query(call.id, "Текст не найден!")
        return

    bot.answer_callback_query(call.id)
    bot.send_message(OWNER_ID, "⏳ Начинаю рассылку...")

    user_ids = get_all_user_ids()
    sent = 0
    failed = 0

    for uid in user_ids:
        if uid == OWNER_ID:
            continue
        try:
            bot.send_message(uid, text, parse_mode="Markdown")
            sent += 1
            time.sleep(0.05)  # небольшая пауза чтобы не словить флуд
        except Exception:
            failed += 1

    owner_broadcast.pop(OWNER_ID, None)
    bot.send_message(
        OWNER_ID,
        f"✅ *Рассылка завершена!*\n\n"
        f"📨 Отправлено: *{sent}*\n"
        f"❌ Не доставлено: *{failed}*\n"
        f"👥 Всего пользователей: *{len(user_ids)}*",
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda c: c.data == "broadcast_cancel")
def broadcast_cancel_cb(call):
    if call.from_user.id != OWNER_ID:
        return
    owner_broadcast.pop(OWNER_ID, None)
    bot.send_message(OWNER_ID, "❌ Рассылка отменена.")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "owner_stats")
def owner_stats(call):
    if call.from_user.id != OWNER_ID:
        return
    user_ids = get_all_user_ids()
    all_keys = keys_all()
    active = sum(1 for k in all_keys.values() if k["expire"] > time.time())
    bot.send_message(
        OWNER_ID,
        f"📊 *Статистика*\n\n"
        f"👥 Пользователей: *{len(user_ids)}*\n"
        f"🔑 Всего ключей: *{len(all_keys)}*\n"
        f"✅ Активных ключей: *{active}*",
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "owner_genkey")
def owner_genkey_cb(call):
    if call.from_user.id != OWNER_ID:
        return
    kb = telebot.types.InlineKeyboardMarkup()
    kb.row(
        telebot.types.InlineKeyboardButton("1 час",   callback_data="genkey_3600"),
        telebot.types.InlineKeyboardButton("1 день",  callback_data="genkey_86400"),
        telebot.types.InlineKeyboardButton("7 дней",  callback_data="genkey_604800"),
    )
    kb.row(
        telebot.types.InlineKeyboardButton("30 дней", callback_data="genkey_2592000"),
        telebot.types.InlineKeyboardButton("📅 Своя дата", callback_data="genkey_custom"),
    )
    bot.send_message(OWNER_ID, "🔑 Выбери срок действия ключа:", reply_markup=kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "owner_delkey")
def owner_delkey_cb(call):
    if call.from_user.id != OWNER_ID:
        return
    owner_gen_state[OWNER_ID] = {"step": "wait_delete_key"}
    bot.send_message(OWNER_ID, "🗑 Введи ключ который нужно удалить:")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("genkey_"))
def genkey_duration(call):
    if call.from_user.id != OWNER_ID:
        return
    suffix = call.data[len("genkey_"):]
    if suffix == "custom":
        owner_gen_state[OWNER_ID] = {"step": "wait_custom_date"}
        bot.send_message(OWNER_ID, "📅 Введи дату истечения ключа (формат ДД.ММ.ГГГГ):")
    else:
        seconds = int(suffix)
        key = generate_paid_key()
        keys_set(key, time.time() + seconds, OWNER_ID, "paid")
        bot.send_message(
            OWNER_ID,
            f"✅ *Ключ создан!*\n\n💎 Ключ: `{key}`\n⏱ Срок: *{fmt_duration(seconds)}*",
            parse_mode="Markdown"
        )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("givekey_"))
def givekey_callback(call):
    if call.from_user.id != OWNER_ID:
        return
    _, target_uid_str, seconds_str = call.data.split("_")
    target_uid = int(target_uid_str)
    seconds    = int(seconds_str)
    key = generate_paid_key()
    keys_set(key, time.time() + seconds, target_uid, "paid")
    user_key_set(target_uid, key)
    try:
        bot.send_message(
            target_uid,
            f"🎉 *Твой приватный ключ готов!*\n\n💎 Ключ: `{key}`\n⏱ Срок: *{fmt_duration(seconds)}*",
            parse_mode="Markdown"
        )
        bot.send_message(OWNER_ID, f"✅ Ключ выдан пользователю `{target_uid}`.", parse_mode="Markdown")
    except Exception as e:
        bot.send_message(OWNER_ID, f"❌ Ошибка при отправке ключа: {e}")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data in ("confirm_buy", "cancel_buy"))
def purchase_confirm(call):
    user_id = call.from_user.id
    if call.data == "cancel_buy":
        pending_purchase.pop(user_id, None)
        bot.send_message(call.message.chat.id, "❌ Запрос отменён.", reply_markup=main_kb(user_id))
        bot.answer_callback_query(call.id)
        return
    state = pending_purchase.get(user_id, {})
    if state.get("step") != "confirm":
        bot.answer_callback_query(call.id)
        return
    label   = state["label"]
    seconds = state["seconds"]
    pending_purchase[user_id] = {"step": "wait_price", "label": label, "seconds": seconds}
    kb = telebot.types.InlineKeyboardMarkup()
    kb.add(telebot.types.InlineKeyboardButton(
        "💰 Назначить цену",
        callback_data=f"setprice_{user_id}_{seconds}"
    ))
    link, username = get_user_link(call.from_user)
    bot.send_message(
        OWNER_ID,
        f"💎 *Запрос на приватный ключ*\n\n"
        f"👤 {link}\n🆔 `{user_id}`\n📲 {username}\n\n"
        f"⏱ Срок: *{label}* ({fmt_duration(seconds)})\n\n"
        "Нажми кнопку чтобы назначить цену:",
        parse_mode="Markdown",
        reply_markup=kb
    )
    bot.send_message(call.message.chat.id,
        "✅ *Запрос отправлен!*\n\nВладелец скоро ответит с ценой.",
        parse_mode="Markdown")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("setprice_"))
def setprice_callback(call):
    if call.from_user.id != OWNER_ID:
        return
    _, uid_str, seconds_str = call.data.split("_")
    owner_gen_state[OWNER_ID] = {
        "step":    "wait_price",
        "user_id": int(uid_str),
        "seconds": int(seconds_str),
    }
    bot.send_message(OWNER_ID, "💰 Напиши цену (например: 100р, 1$, договоримся):")
    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda m: m.text == "💎 Купить приватный ключ")
def buy_private_key(message):
    user_id = message.from_user.id
    kb = telebot.types.InlineKeyboardMarkup()
    kb.row(
        telebot.types.InlineKeyboardButton("7 дней",  callback_data="buy_604800"),
        telebot.types.InlineKeyboardButton("30 дней", callback_data="buy_2592000"),
    )
    kb.row(
        telebot.types.InlineKeyboardButton("📅 Своя дата", callback_data="buy_custom"),
    )
    bot.send_message(
        message.chat.id,
        "💎 *Приватный ключ*\n\nВыбери срок действия:",
        parse_mode="Markdown",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("buy_"))
def buy_duration(call):
    user_id = call.from_user.id
    suffix  = call.data[len("buy_"):]
    if suffix == "custom":
        pending_purchase[user_id] = {"step": "wait_date"}
        bot.send_message(call.message.chat.id,
            "📅 Введи дату до которой нужен ключ (формат *ДД.ММ.ГГГГ*):",
            parse_mode="Markdown")
    else:
        seconds = int(suffix)
        label   = fmt_duration(seconds)
        pending_purchase[user_id] = {"step": "confirm", "label": label, "seconds": seconds}
        kb = telebot.types.InlineKeyboardMarkup()
        kb.row(
            telebot.types.InlineKeyboardButton("✅ Отправить запрос", callback_data="confirm_buy"),
            telebot.types.InlineKeyboardButton("❌ Отмена",           callback_data="cancel_buy")
        )
        bot.send_message(call.message.chat.id,
            f"💎 *Подтверждение*\n\nСрок: *{label}*\n\nОтправить запрос владельцу?",
            parse_mode="Markdown", reply_markup=kb)
    bot.answer_callback_query(call.id)

@bot.message_handler(commands=["cancel"])
def cancel_cmd(message):
    uid = message.from_user.id
    waiting_support.pop(uid, None)
    pending_purchase.pop(uid, None)
    owner_broadcast.pop(uid, None)
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

@bot.message_handler(func=lambda m: True)
def handle_all(message):
    user_id = message.from_user.id
    text = message.text or ""

    # ── РАССЫЛКА: ожидаем текст от владельца ──
    if user_id == OWNER_ID and OWNER_ID in owner_broadcast:
        state = owner_broadcast[OWNER_ID]
        if state.get("step") == "wait_text":
            owner_broadcast[OWNER_ID] = {"step": "confirm", "text": text}
            user_ids = get_all_user_ids()
            count = max(0, len(user_ids) - 1)  # без самого владельца
            kb = telebot.types.InlineKeyboardMarkup()
            kb.row(
                telebot.types.InlineKeyboardButton("✅ Отправить всем", callback_data="broadcast_confirm"),
                telebot.types.InlineKeyboardButton("❌ Отмена",         callback_data="broadcast_cancel"),
            )
            bot.send_message(
                OWNER_ID,
                f"📢 *Предпросмотр рассылки*\n\n"
                f"━━━━━━━━━━━━━━━━\n{text}\n━━━━━━━━━━━━━━━━\n\n"
                f"👥 Получателей: *{count}*\n\nОтправить?",
                parse_mode="Markdown",
                reply_markup=kb
            )
            return

    if user_id == OWNER_ID and OWNER_ID in owner_gen_state:
        state = owner_gen_state[OWNER_ID]
        step  = state["step"]

        if step == "wait_price":
            target_uid = state["user_id"]
            seconds    = state["seconds"]
            del owner_gen_state[OWNER_ID]
            kb = telebot.types.InlineKeyboardMarkup()
            kb.add(telebot.types.InlineKeyboardButton(
                f"✅ Выдать ключ ({fmt_duration(seconds)})",
                callback_data=f"givekey_{target_uid}_{seconds}"
            ))
            try:
                bot.send_message(target_uid,
                    f"💬 *Ответ от владельца*\n\n"
                    f"Твой запрос на ключ *{fmt_duration(seconds)}* рассмотрен!\n\n"
                    f"💰 Цена: *{text}*\n\nПосле оплаты напиши владельцу.",
                    parse_mode="Markdown")
                bot.send_message(OWNER_ID, "✅ Цена отправлена! После оплаты нажми кнопку:", reply_markup=kb)
            except Exception as e:
                bot.send_message(OWNER_ID, f"❌ Ошибка: {e}")
            return

        if step == "wait_custom_date":
            del owner_gen_state[OWNER_ID]
            now = datetime.now()
            try:
                target = datetime.strptime(text.strip(), "%d.%m.%Y").replace(hour=23, minute=59, second=59)
                if target <= now:
                    bot.send_message(OWNER_ID, "❌ Дата уже прошла!")
                    return
                seconds = int((target - now).total_seconds())
                key = generate_paid_key()
                keys_set(key, time.time() + seconds, OWNER_ID, "paid")
                bot.send_message(OWNER_ID,
                    f"✅ *Ключ создан!*\n\n💎 Ключ: `{key}`\n📅 До: *{text.strip()}*\n⏱ {fmt_duration(seconds)}",
                    parse_mode="Markdown")
            except ValueError:
                bot.send_message(OWNER_ID, "❌ Неверный формат! Используй ДД.ММ.ГГГГ")
            return

        if step == "wait_delete_key":
            del owner_gen_state[OWNER_ID]
            k = text.strip()
            if keys_get(k):
                keys_del(k)
                bot.send_message(OWNER_ID, f"✅ Ключ `{k}` удалён.", parse_mode="Markdown")
            else:
                bot.send_message(OWNER_ID, "❌ Ключ не найден.", parse_mode="Markdown")
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
            target = datetime.strptime(text.strip(), "%d.%m.%Y").replace(hour=23, minute=59, second=59)
            if target <= now:
                bot.send_message(message.chat.id, "❌ Дата уже прошла!")
                return
            seconds = int((target - now).total_seconds())
            label = f"до {text.strip()}"
            pending_purchase[user_id] = {"step": "confirm", "label": label, "seconds": seconds}
            kb = telebot.types.InlineKeyboardMarkup()
            kb.row(
                telebot.types.InlineKeyboardButton("✅ Отправить запрос", callback_data="confirm_buy"),
                telebot.types.InlineKeyboardButton("❌ Отмена",           callback_data="cancel_buy")
            )
            bot.send_message(message.chat.id,
                f"📅 *Подтверждение*\n\nСегодня: *{now.strftime('%d.%m.%Y')}*\n"
                f"До: *{text.strip()}*\nДлительность: *{fmt_duration(seconds)}*\n\n"
                "Отправить запрос владельцу?",
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
            f"📲 Username: {username}\n🔗 tg://user?id={user_id}\n"
            f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n\n💬 *Сообщение:*\n{text}",
            parse_mode="Markdown", reply_markup=kb)
        bot.send_message(message.chat.id, "✅ Сообщение отправлено! Ожидайте.", reply_markup=main_kb(user_id))


# ============================================================
#   ЗАПУСК
# ============================================================
def run_flask() -> None:
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    init_db()
    log.info("✅ Yrener Menu Bot (PostgreSQL) запущен!")
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    bot.infinity_polling(timeout=30, long_polling_timeout=20)
