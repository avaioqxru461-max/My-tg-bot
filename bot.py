import asyncio
import logging
import math
import os
import random
import sqlite3
import threading
import time

import requests
from flask import Flask
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------
ADMIN_ID = 8192612489
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
XROCKET_TOKEN = os.environ.get("XROCKET_TOKEN")
XROCKET_URL = "https://pay.api.xrocket.exchange/api/v1/invoices"
DB_PATH = os.environ.get("DB_PATH", "bot.db")

START_BALANCE = 200  # 2$ в центах
COMMISSION = 0.05
RAKEBACK_RATE = 0.02
MIN_DEPOSIT = 10
MIN_WITHDRAW = 10
MIN_BET = 10

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("casino-bot")

# ---------------------------------------------------------------------------
# Flask (для Render)
# ---------------------------------------------------------------------------
web = Flask(__name__)


@web.route("/")
def index():
    return "Bot is running"


def run_web():
    port = int(os.environ.get("PORT", "10000"))
    web.run(host="0.0.0.0", port=port)


# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------
db_lock = threading.Lock()
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row


def init_db():
    with db_lock:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 200,
                rakeback INTEGER NOT NULL DEFAULT 0,
                turnover INTEGER NOT NULL DEFAULT 0,
                rank_claimed INTEGER NOT NULL DEFAULT 0,
                custom_bet INTEGER NOT NULL DEFAULT 0,
                created INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                game TEXT NOT NULL,
                bet INTEGER NOT NULL,
                payout INTEGER NOT NULL,
                ts INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                ts INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                invoice_id TEXT,
                link TEXT,
                ts INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat TEXT UNIQUE NOT NULL
            );
            """
        )
        conn.commit()


def db_exec(sql, params=()):
    with db_lock:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount, cur.lastrowid


def db_one(sql, params=()):
    with db_lock:
        return conn.execute(sql, params).fetchone()


def db_all(sql, params=()):
    with db_lock:
        return conn.execute(sql, params).fetchall()


def ensure_user(uid):
    db_exec(
        "INSERT OR IGNORE INTO users (id, balance, created) VALUES (?, ?, ?)",
        (uid, START_BALANCE, int(time.time())),
    )


def get_user(uid):
    ensure_user(uid)
    return db_one("SELECT * FROM users WHERE id = ?", (uid,))


def add_balance(uid, cents):
    db_exec("UPDATE users SET balance = balance + ? WHERE id = ?", (cents, uid))


def take_bet(uid, bet):
    """Списывает ставку, добавляет оборот и рейкбек. True если хватило денег."""
    rb = int(bet * RAKEBACK_RATE)
    rowcount, _ = db_exec(
        "UPDATE users SET balance = balance - ?, turnover = turnover + ?, "
        "rakeback = rakeback + ? WHERE id = ? AND balance >= ?",
        (bet, bet, rb, uid, bet),
    )
    return rowcount == 1


def refund_bet(uid, bet):
    rb = int(bet * RAKEBACK_RATE)
    db_exec(
        "UPDATE users SET balance = balance + ?, turnover = turnover - ?, "
        "rakeback = rakeback - ? WHERE id = ?",
        (bet, bet, rb, uid),
    )


def finish_bet(uid, game, bet, payout):
    if payout > 0:
        add_balance(uid, payout)
    db_exec(
        "INSERT INTO bets (user_id, game, bet, payout, ts) VALUES (?, ?, ?, ?, ?)",
        (uid, game, bet, payout, int(time.time())),
    )


def calc_payout(bet, mult):
    return int(bet * mult * (1 - COMMISSION) + 1e-9)


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
def fmt(cents):
    return f"${cents / 100:.2f}"


def parse_amount(text):
    t = text.replace("$", "").replace(",", ".").strip()
    try:
        val = float(t)
    except ValueError:
        return None
    if not math.isfinite(val):
        return None
    if val <= 0 or val > 1000000:
        return None
    return int(round(val * 100))


RANKS = [
    ("Новичок", 0, 0),
    ("Игрок", 1000, 100),
    ("Хайроллер", 5000, 500),
    ("Магнат", 20000, 1000),
    ("Легенда", 100000, 5000),
]


def rank_index(turnover):
    idx = 0
    for i, (_, threshold, _) in enumerate(RANKS):
        if turnover >= threshold:
            idx = i
    return idx


def claim_rank_bonus(uid):
    with db_lock:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if row is None:
            return 0
        cur = rank_index(row["turnover"])
        claimed = row["rank_claimed"]
        if cur <= claimed:
            return 0
        bonus = sum(RANKS[i][2] for i in range(claimed + 1, cur + 1))
        conn.execute(
            "UPDATE users SET balance = balance + ?, rank_claimed = ? WHERE id = ?",
            (bonus, cur, uid),
        )
        conn.commit()
        return bonus


def claim_rakeback(uid):
    with db_lock:
        row = conn.execute("SELECT rakeback FROM users WHERE id = ?", (uid,)).fetchone()
        if row is None or row["rakeback"] <= 0:
            return 0
        rb = row["rakeback"]
        conn.execute(
            "UPDATE users SET balance = balance + ?, rakeback = 0 WHERE id = ?",
            (rb, uid),
        )
        conn.commit()
        return rb


def profile_text(uid):
    u = get_user(uid)
    idx = rank_index(u["turnover"])
    text = (
        "👤 Профиль\n\n"
        f"ID: {uid}\n"
        f"💰 Баланс: {fmt(u['balance'])}\n"
        f"♻️ Рейкбек: {fmt(u['rakeback'])}\n"
        f"📊 Оборот: {fmt(u['turnover'])}\n"
        f"🏅 Ранг: {RANKS[idx][0]}"
    )
    if idx + 1 < len(RANKS):
        need = RANKS[idx + 1][1] - u["turnover"]
        text += f"\n⬆️ До ранга «{RANKS[idx + 1][0]}»: {fmt(need)}"
    if u["rank_claimed"] < idx:
        text += "\n🎁 Есть неполученный бонус за ранг!"
    return text


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------
MENU_BUTTONS = ["💰 Баланс", "🎮 Играть", "📋 Меню", "👤 Профиль"]
MAIN_KB = ReplyKeyboardMarkup([MENU_BUTTONS], resize_keyboard=True)

ALL_GAMES = {
    "dice": "🎲 Кубик",
    "darts": "🎯 Дартс",
    "football": "⚽ Футбол",
    "basket": "🏀 Баскетбол",
    "bowling": "🎳 Боулинг",
    "crash": "🚀 Краш",
    "mines": "💣 Мины",
    "tower": "🏰 Башня",
}
GAME_ORDER = ["dice", "darts", "football", "basket", "bowling", "crash", "mines", "tower"]

# Игры через send_dice: эмодзи, таблица множителей по dice.value, описание правил
DICE_GAMES = {
    "dice": {"emoji": "🎲", "mults": {6: 4.0, 5: 2.0}, "rules": "Выпало 6 — x4, выпало 5 — x2"},
    "darts": {"emoji": "🎯", "mults": {6: 3.0, 5: 2.0, 4: 1.0}, "rules": "Центр (6) — x3, 5 — x2, 4 — x1"},
    "football": {"emoji": "⚽", "mults": {3: 1.6, 4: 1.6, 5: 1.6}, "rules": "Гол — x1.6"},
    "basket": {"emoji": "🏀", "mults": {4: 2.4, 5: 2.4}, "rules": "Попадание — x2.4"},
    "bowling": {"emoji": "🎳", "mults": {6: 4.0, 5: 2.0}, "rules": "Страйк (6) — x4, 5 кеглей — x2"},
}

CRASH_MULTS = ["1.2", "1.5", "2", "3", "5", "10", "25", "50", "100"]
MINES_SIZE = 25
TOWER_LEVELS = 6
TOWER_DOORS = 5
TOWER_MULTS = [1.0, 1.2, 1.5, 1.9, 2.5, 3.5]

MINES = {}
TOWER = {}


def games_markup():
    buttons = [InlineKeyboardButton(ALL_GAMES[k], callback_data=f"g:{k}") for k in GAME_ORDER]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


def bet_markup(game):
    rows = []
    quick = [10, 50, 100, 200, 500, 1000]
    for i in range(0, len(quick), 3):
        rows.append(
            [InlineKeyboardButton(fmt(c), callback_data=f"bt:{game}:{c}") for c in quick[i:i + 3]]
        )
    rows.append(
        [
            InlineKeyboardButton("💼 Своя ставка", callback_data=f"bt:{game}:c"),
            InlineKeyboardButton("💰 Вся (вб)", callback_data=f"bt:{game}:a"),
        ]
    )
    rows.append([InlineKeyboardButton("⬅️ К играм", callback_data="games")])
    return InlineKeyboardMarkup(rows)


def balance_markup():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💳 Пополнить", callback_data="dep"),
                InlineKeyboardButton("💸 Вывести", callback_data="wd"),
            ]
        ]
    )


def profile_markup():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎁 Забрать бонус за ранг", callback_data="claim_rank")],
            [InlineKeyboardButton("♻️ Забрать рейкбек", callback_data="claim_rb")],
        ]
    )


def admin_markup():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Статистика", callback_data="adm:stats")],
            [InlineKeyboardButton("👤 Информация об игроке", callback_data="adm:player")],
            [InlineKeyboardButton("💸 Заявки на вывод", callback_data="adm:wd")],
            [InlineKeyboardButton("💳 Заявки на пополнение", callback_data="adm:dp")],
            [InlineKeyboardButton("📢 Настройки подписок", callback_data="adm:subs")],
            [InlineKeyboardButton("📨 Рассылка", callback_data="adm:bcast")],
        ]
    )


async def safe_edit(q, text, markup=None):
    try:
        await q.edit_message_text(text, reply_markup=markup)
    except TelegramError as e:
        logger.warning("edit_message_text failed: %s", e)


async def safe_send(context, chat_id, text, markup=None):
    try:
        return await context.bot.send_message(chat_id, text, reply_markup=markup)
    except TelegramError as e:
        logger.warning("send_message to %s failed: %s", chat_id, e)
        return None


# ---------------------------------------------------------------------------
# Обязательные подписки
# ---------------------------------------------------------------------------
async def get_missing_channels(bot, uid):
    missing = []
    for row in db_all("SELECT chat FROM channels"):
        chat = row["chat"]
        try:
            member = await bot.get_chat_member(chat, uid)
            if member.status in ("left", "kicked"):
                missing.append(chat)
        except TelegramError as e:
            logger.warning("get_chat_member %s failed: %s", chat, e)
    return missing


async def gate(update, context):
    uid = update.effective_user.id
    if uid == ADMIN_ID:
        return True
    missing = await get_missing_channels(context.bot, uid)
    if not missing:
        return True
    rows = [
        [InlineKeyboardButton(f"📢 {c}", url=f"https://t.me/{c.lstrip('@')}")]
        for c in missing
    ]
    rows.append([InlineKeyboardButton("✅ Проверить подписку", callback_data="chk_sub")])
    await safe_send(
        context,
        uid,
        "Для использования бота подпишитесь на каналы:",
        InlineKeyboardMarkup(rows),
    )
    return False


# ---------------------------------------------------------------------------
# xRocket
# ---------------------------------------------------------------------------
def create_xrocket_invoice(user_id, cents):
    """Возвращает (link, invoice_id) или None при ошибке."""
    if not XROCKET_TOKEN:
        logger.error("XROCKET_TOKEN is not set")
        return None
    headers = {
        "Authorization": f"Bearer {XROCKET_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "priceAmount": str(round(cents / 100, 2)),
        "priceCurrency": "USDT",
        "numPayments": 1,
        "description": f"Deposit for {user_id}",
    }
    try:
        resp = requests.post(XROCKET_URL, headers=headers, json=payload, timeout=20)
    except requests.RequestException as e:
        logger.error("xRocket request failed: %s", e)
        return None
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code in (200, 201) and isinstance(data, dict):
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        link = data.get("link") or inner.get("link")
        invoice_id = data.get("id") or inner.get("id")
        if link:
            return link, str(invoice_id) if invoice_id is not None else None
    logger.error("xRocket error: status=%s body=%s", resp.status_code, resp.text)
    return None


# ---------------------------------------------------------------------------
# Игры
# ---------------------------------------------------------------------------
async def show_bet_menu(context, uid, game):
    u = get_user(uid)
    text = f"{ALL_GAMES[game]}\n"
    if game in DICE_GAMES:
        text += DICE_GAMES[game]["rules"] + "\n"
    elif game == "crash":
        text += "Выберите ставку, затем множитель. Результат сразу.\n"
    elif game == "mines":
        text += "Поле 5x5, мины от 2 до 12. Откройте клетки и вовремя заберите выигрыш.\n"
    elif game == "tower":
        text += "6 уровней, 5 дверей, на каждом уровне 1 ловушка.\n"
    text += (
        f"\n💰 Баланс: {fmt(u['balance'])}\n"
        f"Выберите ставку кнопкой или отправьте сумму (мин. {fmt(MIN_BET)})."
    )
    await safe_send(context, uid, text, bet_markup(game))


async def proceed_game(context, uid, game, bet):
    if bet < MIN_BET:
        await safe_send(context, uid, f"❌ Минимальная ставка {fmt(MIN_BET)}")
        return
    u = get_user(uid)
    if u["balance"] < bet:
        await safe_send(context, uid, "❌ Недостаточно средств")
        return
    if game in DICE_GAMES:
        await play_dice(context, uid, game, bet)
    elif game == "crash":
        await show_crash_picker(context, uid, bet)
    elif game == "mines":
        if uid in MINES:
            await safe_send(context, uid, "⚠️ У вас уже идёт игра в Мины.")
            await send_mines_board(context, uid)
            return
        await show_mines_picker(context, uid, bet)
    elif game == "tower":
        await start_tower(context, uid, bet)


async def play_dice(context, uid, game, bet):
    cfg = DICE_GAMES[game]
    if not take_bet(uid, bet):
        await safe_send(context, uid, "❌ Недостаточно средств")
        return
    try:
        dice_msg = await context.bot.send_dice(chat_id=uid, emoji=cfg["emoji"])
    except TelegramError as e:
        logger.error("send_dice failed: %s", e)
        refund_bet(uid, bet)
        await safe_send(context, uid, "❌ Ошибка, ставка возвращена")
        return
    value = dice_msg.dice.value
    await asyncio.sleep(4)
    mult = cfg["mults"].get(value, 0)
    payout = calc_payout(bet, mult) if mult > 0 else 0
    finish_bet(uid, game, bet, payout)
    u = get_user(uid)
    if payout > 0:
        text = f"🎉 Выпало {value}! Выигрыш x{mult:g}: +{fmt(payout)}"
    else:
        text = f"😔 Выпало {value}. Проигрыш {fmt(bet)}"
    text += f"\n💰 Баланс: {fmt(u['balance'])}"
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"🔄 Ещё раз ({fmt(bet)})", callback_data=f"bt:{game}:{bet}")],
            [InlineKeyboardButton("🎮 Игры", callback_data="games")],
        ]
    )
    await safe_send(context, uid, text, kb)


# ----- Краш -----
async def show_crash_picker(context, uid, bet):
    rows = []
    buttons = [
        InlineKeyboardButton(f"x{m}", callback_data=f"cr:{m}:{bet}") for m in CRASH_MULTS
    ]
    for i in range(0, len(buttons), 3):
        rows.append(buttons[i:i + 3])
    rows.append([InlineKeyboardButton("⬅️ К играм", callback_data="games")])
    await safe_send(
        context,
        uid,
        f"🚀 Краш\nСтавка: {fmt(bet)}\nВыберите множитель:",
        InlineKeyboardMarkup(rows),
    )


async def play_crash(context, uid, mult, bet):
    if bet < MIN_BET or not take_bet(uid, bet):
        await safe_send(context, uid, "❌ Недостаточно средств")
        return
    r = random.random()
    crash_point = max(1.0, 0.99 / (1 - r))
    crash_point = round(min(crash_point, 1000.0), 2)
    if crash_point >= mult:
        payout = calc_payout(bet, mult)
        finish_bet(uid, "crash", bet, payout)
        text = f"✅ Выиграл x{mult:g}! (краш на x{crash_point:.2f})\n+{fmt(payout)}"
    else:
        finish_bet(uid, "crash", bet, 0)
        text = f"💥 Краш на x{crash_point:.2f}, проигрыш {fmt(bet)}"
    u = get_user(uid)
    text += f"\n💰 Баланс: {fmt(u['balance'])}"
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"🔄 Ещё раз ({fmt(bet)})", callback_data=f"bt:crash:{bet}")],
            [InlineKeyboardButton("🎮 Игры", callback_data="games")],
        ]
    )
    await safe_send(context, uid, text, kb)


# ----- Мины -----
async def show_mines_picker(context, uid, bet):
    buttons = [
        InlineKeyboardButton(f"💣 {n}", callback_data=f"mc:{n}:{bet}") for n in range(2, 13)
    ]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append([InlineKeyboardButton("⬅️ К играм", callback_data="games")])
    await safe_send(
        context,
        uid,
        f"💣 Мины\nСтавка: {fmt(bet)}\nВыберите количество мин:",
        InlineKeyboardMarkup(rows),
    )


def mines_mult(mines_count, opened):
    m = 1.0
    for i in range(opened):
        m *= (MINES_SIZE - i) / (MINES_SIZE - mines_count - i)
    return m


def mines_text(g):
    opened = len(g["opened"])
    cur = mines_mult(g["count"], opened)
    text = (
        f"💣 Мины ({g['count']}) | Ставка {fmt(g['bet'])}\n"
        f"Открыто: {opened}\n"
        f"Множитель: x{cur:.2f}\n"
        f"Забрать сейчас: {fmt(calc_payout(g['bet'], cur))}"
    )
    if opened < MINES_SIZE - g["count"]:
        nxt = mines_mult(g["count"], opened + 1)
        text += f"\nСледующий: x{nxt:.2f}"
    return text


def mines_markup(g, reveal=False):
    rows = []
    for r in range(5):
        row = []
        for c in range(5):
            i = r * 5 + c
            if reveal:
                if i in g["mines"]:
                    label = "💥" if i == g.get("hit") else "💣"
                elif i in g["opened"]:
                    label = "💎"
                else:
                    label = "▫️"
                row.append(InlineKeyboardButton(label, callback_data="noop"))
            else:
                label = "💎" if i in g["opened"] else "❓"
                row.append(InlineKeyboardButton(label, callback_data=f"mn:{i}"))
        rows.append(row)
    if not reveal:
        rows.append([InlineKeyboardButton("💰 Забрать", callback_data="mn_cash")])
    return InlineKeyboardMarkup(rows)


async def send_mines_board(context, uid):
    g = MINES.get(uid)
    if g is None:
        return
    await safe_send(context, uid, mines_text(g), mines_markup(g))


async def start_mines(context, uid, count, bet):
    if uid in MINES:
        await safe_send(context, uid, "⚠️ У вас уже идёт игра в Мины.")
        await send_mines_board(context, uid)
        return
    if count < 2 or count > 12:
        return
    if bet < MIN_BET or not take_bet(uid, bet):
        await safe_send(context, uid, "❌ Недостаточно средств")
        return
    mines = set(random.sample(range(MINES_SIZE), count))
    MINES[uid] = {"bet": bet, "count": count, "mines": mines, "opened": set()}
    await send_mines_board(context, uid)


async def finish_mines(context, q, uid, won):
    g = MINES.pop(uid, None)
    if g is None:
        return
    opened = len(g["opened"])
    mult = mines_mult(g["count"], opened)
    if won:
        payout = calc_payout(g["bet"], mult)
        finish_bet(uid, "mines", g["bet"], payout)
        text = f"✅ Вы забрали x{mult:.2f}: +{fmt(payout)}"
    else:
        finish_bet(uid, "mines", g["bet"], 0)
        text = f"💥 Мина! Проигрыш {fmt(g['bet'])}"
    u = get_user(uid)
    text += f"\n💰 Баланс: {fmt(u['balance'])}"
    await safe_edit(q, text, mines_markup(g, reveal=True))
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"🔄 Ещё раз ({fmt(g['bet'])})", callback_data=f"bt:mines:{g['bet']}")],
            [InlineKeyboardButton("🎮 Игры", callback_data="games")],
        ]
    )
    await safe_send(context, uid, "Играть дальше?", kb)


# ----- Башня -----
def tower_text(g):
    lines = [f"🏰 Башня | Ставка {fmt(g['bet'])}"]
    for i in range(TOWER_LEVELS - 1, -1, -1):
        if i < g["level"]:
            mark = "✅"
        elif i == g["level"]:
            mark = "▶️"
        else:
            mark = "⬜"
        lines.append(f"{mark} Уровень {i + 1}: x{TOWER_MULTS[i]:.1f}")
    if g["level"] > 0:
        cur = TOWER_MULTS[g["level"] - 1]
        lines.append(f"\nСейчас: x{cur:.1f} → {fmt(calc_payout(g['bet'], cur))}")
    lines.append("\nВыберите дверь (на уровне 1 ловушка из 5):")
    return "\n".join(lines)


def tower_markup(g):
    doors = [
        InlineKeyboardButton(f"🚪 {i + 1}", callback_data=f"tw:{i}") for i in range(TOWER_DOORS)
    ]
    rows = [doors]
    if g["level"] > 0:
        rows.append([InlineKeyboardButton("💰 Забрать", callback_data="tw_cash")])
    return InlineKeyboardMarkup(rows)


async def start_tower(context, uid, bet):
    if uid in TOWER:
        await safe_send(context, uid, "⚠️ У вас уже идёт игра в Башню.")
        g = TOWER[uid]
        await safe_send(context, uid, tower_text(g), tower_markup(g))
        return
    if not take_bet(uid, bet):
        await safe_send(context, uid, "❌ Недостаточно средств")
        return
    g = {
        "bet": bet,
        "level": 0,
        "traps": [random.randrange(TOWER_DOORS) for _ in range(TOWER_LEVELS)],
    }
    TOWER[uid] = g
    await safe_send(context, uid, tower_text(g), tower_markup(g))


async def finish_tower(context, q, uid, won, trap_door=None):
    g = TOWER.pop(uid, None)
    if g is None:
        return
    if won:
        mult = TOWER_MULTS[g["level"] - 1]
        payout = calc_payout(g["bet"], mult)
        finish_bet(uid, "tower", g["bet"], payout)
        text = f"✅ Вы забрали x{mult:.1f}: +{fmt(payout)}"
    else:
        finish_bet(uid, "tower", g["bet"], 0)
        text = f"💥 Ловушка за дверью {trap_door + 1}! Проигрыш {fmt(g['bet'])}"
    u = get_user(uid)
    text += f"\n💰 Баланс: {fmt(u['balance'])}"
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"🔄 Ещё раз ({fmt(g['bet'])})", callback_data=f"bt:tower:{g['bet']}")],
            [InlineKeyboardButton("🎮 Игры", callback_data="games")],
        ]
    )
    await safe_edit(q, text, kb)


# ---------------------------------------------------------------------------
# Команды и текст
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(uid)
    context.user_data.clear()
    if not await gate(update, context):
        return
    u = get_user(uid)
    await update.message.reply_text(
        f"👋 Добро пожаловать!\n💰 Ваш баланс: {fmt(u['balance'])}",
        reply_markup=MAIN_KB,
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    context.user_data.pop("state", None)
    await update.message.reply_text("🛠 Админ-панель", reply_markup=admin_markup())


async def handle_menu_button(update, context, text):
    uid = update.effective_user.id
    u = get_user(uid)
    if text == "💰 Баланс":
        await update.message.reply_text(
            f"💰 Баланс: {fmt(u['balance'])}\n♻️ Рейкбек: {fmt(u['rakeback'])}",
            reply_markup=balance_markup(),
        )
    elif text == "🎮 Играть":
        await update.message.reply_text("🎮 Выберите игру:", reply_markup=games_markup())
    elif text == "📋 Меню":
        info = (
            "📋 Информация\n\n"
            f"• Комиссия казино: {int(COMMISSION * 100)}% (входит в выплату)\n"
            f"• Рейкбек: {int(RAKEBACK_RATE * 100)}% от каждой ставки\n"
            f"• Мин. ставка / пополнение / вывод: {fmt(MIN_BET)}\n\n"
            "Быстрые ставки:\n"
            "• «ставка 1.5» — сохранить свою ставку\n"
            "• «вб» — поставить весь баланс (после выбора игры)"
        )
        await update.message.reply_text(info, reply_markup=balance_markup())
    elif text == "👤 Профиль":
        await update.message.reply_text(profile_text(uid), reply_markup=profile_markup())


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None or msg.text is None:
        return
    uid = update.effective_user.id
    text = msg.text.strip()
    ensure_user(uid)
    if not await gate(update, context):
        return

    if text in MENU_BUTTONS:
        context.user_data.pop("state", None)
        context.user_data.pop("game", None)
        await handle_menu_button(update, context, text)
        return

    state = context.user_data.get("state")
    low = text.lower()

    # Быстрые ставки
    if low == "вб":
        game = context.user_data.get("game")
        if state == "bet" and game:
            u = get_user(uid)
            await proceed_game(context, uid, game, u["balance"])
        else:
            await msg.reply_text("Сначала выберите игру в «🎮 Играть».")
        return

    parts = low.split()
    if len(parts) >= 2 and parts[0] == "ставка":
        amt = parse_amount(parts[1])
        if amt is None or amt < MIN_BET:
            await msg.reply_text(f"❌ Минимальная ставка {fmt(MIN_BET)}")
            return
        db_exec("UPDATE users SET custom_bet = ? WHERE id = ?", (amt, uid))
        await msg.reply_text(
            f"✅ Своя ставка сохранена: {fmt(amt)}\nВыберите игру:",
            reply_markup=games_markup(),
        )
        return

    # Админские состояния
    if state and state.startswith("adm_") and uid == ADMIN_ID:
        await handle_admin_state(update, context, state, text)
        return

    if state == "deposit":
        await handle_deposit_input(update, context, text)
        return

    if state == "withdraw":
        await handle_withdraw_input(update, context, text)
        return

    if state == "bet":
        game = context.user_data.get("game")
        amt = parse_amount(text)
        if game is None:
            context.user_data.pop("state", None)
            return
        if amt is None:
            await msg.reply_text("❌ Введите сумму ставки числом, например 0.5")
            return
        await proceed_game(context, uid, game, amt)
        return


async def handle_deposit_input(update, context, text):
    uid = update.effective_user.id
    amt = parse_amount(text)
    if amt is None or amt < MIN_DEPOSIT:
        await update.message.reply_text(f"❌ Минимальная сумма пополнения {fmt(MIN_DEPOSIT)}. Введите сумму:")
        return
    context.user_data.pop("state", None)
    result = await asyncio.to_thread(create_xrocket_invoice, uid, amt)
    if result is None:
        await update.message.reply_text("❌ Ошибка создания счета")
        return
    link, invoice_id = result
    db_exec(
        "INSERT INTO deposits (user_id, amount, status, invoice_id, link, ts) "
        "VALUES (?, ?, 'pending', ?, ?, ?)",
        (uid, amt, invoice_id, link, int(time.time())),
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("💳 Оплатить", url=link)]])
    await update.message.reply_text(
        f"Счёт на {fmt(amt)} создан. Нажмите кнопку для оплаты.\n"
        "Баланс пополнится после подтверждения оплаты администратором.",
        reply_markup=kb,
    )
    await safe_send(context, ADMIN_ID, f"💳 Новая заявка на пополнение: {uid}, {fmt(amt)}")


async def handle_withdraw_input(update, context, text):
    uid = update.effective_user.id
    amt = parse_amount(text)
    if amt is None or amt < MIN_WITHDRAW:
        await update.message.reply_text(f"❌ Минимальная сумма вывода {fmt(MIN_WITHDRAW)}. Введите сумму:")
        return
    rowcount, _ = db_exec(
        "UPDATE users SET balance = balance - ? WHERE id = ? AND balance >= ?",
        (amt, uid, amt),
    )
    if rowcount != 1:
        await update.message.reply_text("❌ Недостаточно средств. Введите другую сумму:")
        return
    context.user_data.pop("state", None)
    _, wid = db_exec(
        "INSERT INTO withdrawals (user_id, amount, status, ts) VALUES (?, ?, 'pending', ?)",
        (uid, amt, int(time.time())),
    )
    await update.message.reply_text(f"✅ Заявка #{wid} на вывод {fmt(amt)} создана. Ожидайте решения.")
    await safe_send(context, ADMIN_ID, f"💸 Новая заявка на вывод #{wid}: {uid}, {fmt(amt)}")


# ---------------------------------------------------------------------------
# Админка
# ---------------------------------------------------------------------------
async def handle_admin_state(update, context, state, text):
    msg = update.message
    if state == "adm_player":
        context.user_data.pop("state", None)
        try:
            pid = int(text)
        except ValueError:
            await msg.reply_text("❌ Нужен числовой ID")
            return
        row = db_one("SELECT * FROM users WHERE id = ?", (pid,))
        if row is None:
            await msg.reply_text("Игрок не найден")
            return
        bets = db_all(
            "SELECT game, bet, payout, ts FROM bets WHERE user_id = ? ORDER BY id DESC LIMIT 15",
            (pid,),
        )
        lines = [
            f"👤 Игрок {pid}",
            f"Баланс: {fmt(row['balance'])}",
            f"Оборот: {fmt(row['turnover'])}",
            f"Рейкбек: {fmt(row['rakeback'])}",
            "",
            "Последние ставки:",
        ]
        if not bets:
            lines.append("— нет —")
        for b in bets:
            when = time.strftime("%d.%m %H:%M", time.localtime(b["ts"]))
            lines.append(
                f"{when} {ALL_GAMES.get(b['game'], b['game'])}: {fmt(b['bet'])} → {fmt(b['payout'])}"
            )
        await msg.reply_text("\n".join(lines))
    elif state == "adm_addch":
        context.user_data.pop("state", None)
        chat = text.strip()
        if not chat.startswith("@"):
            await msg.reply_text("❌ Укажите канал в формате @username")
            return
        try:
            await context.bot.get_chat(chat)
        except TelegramError as e:
            await msg.reply_text(f"❌ Канал не найден: {e}")
            return
        try:
            db_exec("INSERT INTO channels (chat) VALUES (?)", (chat,))
        except sqlite3.IntegrityError:
            await msg.reply_text("Этот канал уже добавлен")
            return
        await msg.reply_text(f"✅ Канал {chat} добавлен. Бот должен быть админом канала.")
    elif state == "adm_bcast":
        context.user_data.pop("state", None)
        users = db_all("SELECT id FROM users")
        ok = 0
        fail = 0
        for row in users:
            try:
                await context.bot.send_message(row["id"], text)
                ok += 1
            except TelegramError:
                fail += 1
            await asyncio.sleep(0.05)
        await msg.reply_text(f"📨 Рассылка завершена. Доставлено: {ok}, ошибок: {fail}")


async def admin_callback(update, context, q, key, parts):
    uid = q.from_user.id
    if key == "adm":
        action = parts[1] if len(parts) > 1 else ""
        if action == "menu":
            await safe_edit(q, "🛠 Админ-панель", admin_markup())
        elif action == "stats":
            users = db_one("SELECT COUNT(*) AS n, COALESCE(SUM(balance), 0) AS b, "
                           "COALESCE(SUM(turnover), 0) AS t FROM users")
            wd = db_one("SELECT COUNT(*) AS n FROM withdrawals WHERE status = 'pending'")
            dp = db_one("SELECT COUNT(*) AS n FROM deposits WHERE status = 'pending'")
            text = (
                "📊 Статистика\n\n"
                f"Игроков: {users['n']}\n"
                f"Суммарный баланс: {fmt(users['b'])}\n"
                f"Суммарный оборот: {fmt(users['t'])}\n"
                f"Активные заявки: вывод {wd['n']}, пополнение {dp['n']}"
            )
            await safe_edit(q, text, admin_markup())
        elif action == "player":
            context.user_data["state"] = "adm_player"
            await safe_send(context, uid, "Введите ID игрока:")
        elif action == "wd":
            rows = db_all("SELECT * FROM withdrawals WHERE status = 'pending' ORDER BY id LIMIT 20")
            if not rows:
                await safe_send(context, uid, "Активных заявок на вывод нет.")
            for r in rows:
                kb = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("✅ Одобрить", callback_data=f"wd_ok:{r['id']}"),
                            InlineKeyboardButton("❌ Отклонить", callback_data=f"wd_no:{r['id']}"),
                        ]
                    ]
                )
                await safe_send(
                    context, uid, f"💸 Вывод #{r['id']}\nИгрок: {r['user_id']}\nСумма: {fmt(r['amount'])}", kb
                )
        elif action == "dp":
            rows = db_all("SELECT * FROM deposits WHERE status = 'pending' ORDER BY id LIMIT 20")
            if not rows:
                await safe_send(context, uid, "Активных заявок на пополнение нет.")
            for r in rows:
                kb = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("✅ Подтвердить", callback_data=f"dp_ok:{r['id']}"),
                            InlineKeyboardButton("❌ Отклонить", callback_data=f"dp_no:{r['id']}"),
                        ]
                    ]
                )
                await safe_send(
                    context,
                    uid,
                    f"💳 Пополнение #{r['id']}\nИгрок: {r['user_id']}\nСумма: {fmt(r['amount'])}\n"
                    f"Счёт: {r['invoice_id']}",
                    kb,
                )
        elif action == "subs":
            rows = db_all("SELECT * FROM channels ORDER BY id")
            buttons = [
                [InlineKeyboardButton(f"🗑 {r['chat']}", callback_data=f"ch_del:{r['id']}")] for r in rows
            ]
            buttons.append([InlineKeyboardButton("➕ Добавить канал", callback_data="adm:addch")])
            buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="adm:menu")])
            title = "📢 Обязательные подписки (нажмите на канал, чтобы удалить):" if rows else "📢 Каналов нет."
            await safe_edit(q, title, InlineKeyboardMarkup(buttons))
        elif action == "addch":
            context.user_data["state"] = "adm_addch"
            await safe_send(context, uid, "Отправьте @username канала (бот должен быть его админом):")
        elif action == "bcast":
            context.user_data["state"] = "adm_bcast"
            await safe_send(context, uid, "Отправьте текст рассылки:")
        return

    if key == "ch_del":
        db_exec("DELETE FROM channels WHERE id = ?", (int(parts[1]),))
        await safe_send(context, uid, "🗑 Канал удалён.")
        return

    if key in ("wd_ok", "wd_no"):
        wid = int(parts[1])
        row = db_one("SELECT * FROM withdrawals WHERE id = ?", (wid,))
        if row is None:
            return
        new_status = "approved" if key == "wd_ok" else "rejected"
        rowcount, _ = db_exec(
            "UPDATE withdrawals SET status = ? WHERE id = ? AND status = 'pending'",
            (new_status, wid),
        )
        if rowcount != 1:
            await safe_edit(q, f"Заявка #{wid} уже обработана.")
            return
        if key == "wd_ok":
            await safe_edit(q, f"✅ Вывод #{wid} одобрен ({fmt(row['amount'])}, игрок {row['user_id']})")
            await safe_send(context, row["user_id"], f"✅ Ваша заявка на вывод {fmt(row['amount'])} одобрена.")
        else:
            add_balance(row["user_id"], row["amount"])
            await safe_edit(q, f"❌ Вывод #{wid} отклонён, баланс возвращён.")
            await safe_send(
                context,
                row["user_id"],
                f"❌ Заявка на вывод {fmt(row['amount'])} отклонена, средства возвращены на баланс.",
            )
        return

    if key in ("dp_ok", "dp_no"):
        did = int(parts[1])
        row = db_one("SELECT * FROM deposits WHERE id = ?", (did,))
        if row is None:
            return
        new_status = "approved" if key == "dp_ok" else "rejected"
        rowcount, _ = db_exec(
            "UPDATE deposits SET status = ? WHERE id = ? AND status = 'pending'",
            (new_status, did),
        )
        if rowcount != 1:
            await safe_edit(q, f"Заявка #{did} уже обработана.")
            return
        if key == "dp_ok":
            add_balance(row["user_id"], row["amount"])
            await safe_edit(q, f"✅ Пополнение #{did} подтверждено ({fmt(row['amount'])}, игрок {row['user_id']})")
            await safe_send(context, row["user_id"], f"✅ Баланс пополнен на {fmt(row['amount'])}.")
        else:
            await safe_edit(q, f"❌ Пополнение #{did} отклонено.")
            await safe_send(context, row["user_id"], f"❌ Пополнение на {fmt(row['amount'])} отклонено.")
        return


# ---------------------------------------------------------------------------
# Callback-обработчик
# ---------------------------------------------------------------------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q is None:
        return
    uid = q.from_user.id
    data = q.data or ""
    try:
        await q.answer()
    except TelegramError as e:
        logger.warning("answer failed: %s", e)
    ensure_user(uid)
    parts = data.split(":")
    key = parts[0]

    if key == "noop":
        return

    if key in ("adm", "ch_del", "wd_ok", "wd_no", "dp_ok", "dp_no"):
        if uid != ADMIN_ID:
            return
        try:
            await admin_callback(update, context, q, key, parts)
        except (ValueError, IndexError) as e:
            logger.error("admin callback error: %s", e)
        return

    if key == "chk_sub":
        if await gate(update, context):
            await safe_send(context, uid, "✅ Подписка подтверждена!", MAIN_KB)
        return

    if not await gate(update, context):
        return

    try:
        if key == "games":
            context.user_data.pop("state", None)
            await safe_send(context, uid, "🎮 Выберите игру:", games_markup())

        elif key == "g":
            game = parts[1]
            if game not in ALL_GAMES:
                return
            context.user_data["state"] = "bet"
            context.user_data["game"] = game
            await show_bet_menu(context, uid, game)

        elif key == "bt":
            game = parts[1]
            val = parts[2]
            if game not in ALL_GAMES:
                return
            context.user_data["state"] = "bet"
            context.user_data["game"] = game
            u = get_user(uid)
            if val == "c":
                bet = u["custom_bet"]
                if bet <= 0:
                    await safe_send(context, uid, "Своя ставка не задана. Напишите, например: ставка 1.5")
                    return
            elif val == "a":
                bet = u["balance"]
            else:
                bet = int(val)
            await proceed_game(context, uid, game, bet)

        elif key == "cr":
            await play_crash(context, uid, float(parts[1]), int(parts[2]))

        elif key == "mc":
            await start_mines(context, uid, int(parts[1]), int(parts[2]))

        elif key == "mn":
            g = MINES.get(uid)
            if g is None:
                await safe_edit(q, "Игра не найдена или уже завершена.")
                return
            idx = int(parts[1])
            if idx < 0 or idx >= MINES_SIZE or idx in g["opened"]:
                return
            if idx in g["mines"]:
                g["hit"] = idx
                await finish_mines(context, q, uid, won=False)
                return
            g["opened"].add(idx)
            if len(g["opened"]) >= MINES_SIZE - g["count"]:
                await finish_mines(context, q, uid, won=True)
                return
            await safe_edit(q, mines_text(g), mines_markup(g))

        elif key == "mn_cash":
            g = MINES.get(uid)
            if g is None:
                await safe_edit(q, "Игра не найдена или уже завершена.")
                return
            if not g["opened"]:
                await safe_send(context, uid, "Откройте хотя бы одну клетку.")
                return
            await finish_mines(context, q, uid, won=True)

        elif key == "tw":
            g = TOWER.get(uid)
            if g is None:
                await safe_edit(q, "Игра не найдена или уже завершена.")
                return
            door = int(parts[1])
            if door < 0 or door >= TOWER_DOORS:
                return
            trap = g["traps"][g["level"]]
            if door == trap:
                await finish_tower(context, q, uid, won=False, trap_door=trap)
                return
            g["level"] += 1
            if g["level"] >= TOWER_LEVELS:
                await finish_tower(context, q, uid, won=True)
                return
            await safe_edit(q, tower_text(g), tower_markup(g))

        elif key == "tw_cash":
            g = TOWER.get(uid)
            if g is None:
                await safe_edit(q, "Игра не найдена или уже завершена.")
                return
            if g["level"] == 0:
                await safe_send(context, uid, "Пройдите хотя бы один уровень.")
                return
            await finish_tower(context, q, uid, won=True)

        elif key == "dep":
            context.user_data["state"] = "deposit"
            await safe_send(context, uid, f"💳 Введите сумму пополнения в $ (мин. {fmt(MIN_DEPOSIT)}):")

        elif key == "wd":
            context.user_data["state"] = "withdraw"
            u = get_user(uid)
            await safe_send(
                context,
                uid,
                f"💸 Введите сумму вывода в $ (мин. {fmt(MIN_WITHDRAW)}).\nДоступно: {fmt(u['balance'])}",
            )

        elif key == "claim_rank":
            bonus = claim_rank_bonus(uid)
            if bonus > 0:
                await safe_send(context, uid, f"🎁 Бонус за ранг получен: +{fmt(bonus)}")
            else:
                await safe_send(context, uid, "Нет доступных бонусов за ранг.")
            await safe_edit(q, profile_text(uid), profile_markup())

        elif key == "claim_rb":
            rb = claim_rakeback(uid)
            if rb > 0:
                await safe_send(context, uid, f"♻️ Рейкбек получен: +{fmt(rb)}")
            else:
                await safe_send(context, uid, "Рейкбек пока пуст.")
            await safe_edit(q, profile_text(uid), profile_markup())

    except (ValueError, IndexError) as e:
        logger.error("callback parse error for %r: %s", data, e)


async def on_error(update, context):
    logger.error("Unhandled error", exc_info=context.error)


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is not set")
    init_db()
    threading.Thread(target=run_web, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
