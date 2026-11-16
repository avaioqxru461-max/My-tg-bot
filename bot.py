# -*- coding: utf-8 -*-
"""
Telegram-бот «казино» (python-telegram-bot 20.7 + SQLite + Flask для Render).

requirements.txt:
    python-telegram-bot==20.7
    Flask>=2.3

Переменные окружения:
    TELEGRAM_TOKEN  — токен бота (обязательно)
    PORT            — порт для Flask (Render задаёт сам)
    DB_PATH         — путь к файлу SQLite (по умолчанию casino.db;
                      на Render укажите путь на Persistent Disk, иначе база
                      обнулится при каждом деплое)

Все суммы хранятся в ЦЕНТАХ (int).

ЛОГИКА ИНТЕРПРЕТАЦИИ ЧИСЕЛ (значения 1-6):
  Кубик (🎲, реальное значение):
      Чёт 2,4,6 | Нечёт 1,3,5 | Больше 3 -> 4,5,6 | Меньше 4 -> 1,2,3
      Центр -> 3,4 | Края -> 1,2,5,6
  Дартс (🎯, реальное значение):
      Красный 1,3,5 | Белый 2,4,6 | Отскок 1 | Центр 6 | Любой 2,3,4,5
  Футбол (send_dice 🎲, реальное значение):
      1-2 = Гол, 3 = Гол в центр, 4-6 = Мимо.
      Ставка «Гол» выигрывает при 1,2,3 (гол в центр — тоже гол),
      «Гол в центр» только при 3, «Мимо» при 4,5,6.
  Баскетбол (send_dice 🎰 — только анимация, результат 1-6 генерируется ботом):
      1 Отскок | 2 Застрял | 3 Мимо | 4 Гол | 5 Гол | 6 Чистый гол
      «Гол» = 4,5,6 | «Без гола» = 1,2,3 | «Отскок» = 1 | «Застрял» = 2
      «Чистый гол» = 6
  Боулинг (send_dice 🎰 — только анимация, результат 1-6 генерируется ботом):
      1 Мимо | 2 Сбито 1/6 | 3 Сбито 3/6 | 4 Сбито 4/6 | 5 Сбито 5/6 | 6 Страйк
  Почему 🎰 + собственный random: у 🎰 значения 1-64, остаток от деления на 6
  давал бы смещённые шансы. Анимация — только визуал.

Выплата при выигрыше = ставка * множитель * 0.95 (округление вниз до цента).
Рейкбек 2% от каждой ставки копится на отдельном счёте.
"""

import asyncio
import logging
import os
import random
import re
import sqlite3
import threading
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ───────────────────────────── НАСТРОЙКИ ─────────────────────────────

TOKEN = os.environ.get("TELEGRAM_TOKEN")
MAIN_ADMIN_ID = 8192612489
DB_PATH = os.environ.get("DB_PATH", "casino.db")

START_BALANCE = 200            # 2$ в центах
COMMISSION = Decimal("0.05")   # комиссия казино 5%
RAKEBACK_PERCENT = 2           # рейкбек 2% от ставки
MIN_BET = 10                   # 0.10$
BET_PRESETS = [10, 25, 50, 100, 200, 500]

CRASH_MULTS = [2, 3, 4, 5, 10, 30, 50, 100]

MINES_SIZE = 25
MINES_MIN, MINES_MAX = 2, 12
MINES_MAX_MULT = 1000.0

TOWER_MULTS = [1.0, 1.2, 1.5, 1.9, 2.5, 3.5]
TOWER_DOORS = 5

BTN_BALANCE = "💰 Баланс"
BTN_PLAY = "🎮 Играть"
BTN_MENU = "📋 Меню"
MAIN_BUTTONS = (BTN_BALANCE, BTN_PLAY, BTN_MENU)

RNG = random.SystemRandom()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logger = logging.getLogger("casino")

Kb = List[List[InlineKeyboardButton]]

MAIN_KB = ReplyKeyboardMarkup(
    [[KeyboardButton(BTN_BALANCE), KeyboardButton(BTN_PLAY), KeyboardButton(BTN_MENU)]],
    resize_keyboard=True,
)

# ───────────────────────────── FLASK (для Render) ─────────────────────────────

flask_app = Flask(__name__)


@flask_app.route("/")
def index() -> str:
    return "Bot is running"


@flask_app.route("/health")
def health() -> str:
    return "ok"


def run_flask() -> None:
    port = int(os.environ.get("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)


# ───────────────────────────── БАЗА ДАННЫХ ─────────────────────────────

_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row
_db_lock = threading.RLock()


def db_init() -> None:
    with _db_lock:
        _db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                balance INTEGER NOT NULL DEFAULT 0,
                rakeback INTEGER NOT NULL DEFAULT 0,
                custom_bet INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS admins (id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS channels (
                chat_id TEXT PRIMARY KEY,
                title TEXT,
                link TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS active_bets (
                user_id INTEGER,
                game TEXT,
                bet INTEGER,
                PRIMARY KEY (user_id, game)
            );
            """
        )
        _db.commit()


def db_exec(sql: str, params: Tuple[Any, ...] = ()) -> int:
    with _db_lock:
        cur = _db.execute(sql, params)
        _db.commit()
        return cur.rowcount


def db_one(sql: str, params: Tuple[Any, ...] = ()) -> Optional[sqlite3.Row]:
    with _db_lock:
        return _db.execute(sql, params).fetchone()


def db_all(sql: str, params: Tuple[Any, ...] = ()) -> List[sqlite3.Row]:
    with _db_lock:
        return _db.execute(sql, params).fetchall()


# ── пользователи ──

def ensure_user(uid: int, username: Optional[str]) -> None:
    with _db_lock:
        row = db_one("SELECT username FROM users WHERE id=?", (uid,))
        if row is None:
            db_exec(
                "INSERT INTO users (id, username, balance) VALUES (?,?,?)",
                (uid, username, START_BALANCE),
            )
        elif row["username"] != username:
            db_exec("UPDATE users SET username=? WHERE id=?", (username, uid))


def get_user(uid: int) -> sqlite3.Row:
    row = db_one("SELECT * FROM users WHERE id=?", (uid,))
    if row is None:
        ensure_user(uid, None)
        row = db_one("SELECT * FROM users WHERE id=?", (uid,))
    assert row is not None
    return row


def get_balance(uid: int) -> int:
    return int(get_user(uid)["balance"])


def rake_of(bet: int) -> int:
    return (bet * RAKEBACK_PERCENT + 50) // 100


def try_debit(uid: int, bet: int) -> bool:
    """Атомарно списывает ставку и начисляет рейкбек."""
    n = db_exec(
        "UPDATE users SET balance = balance - ?, rakeback = rakeback + ? "
        "WHERE id=? AND balance >= ?",
        (bet, rake_of(bet), uid, bet),
    )
    return n == 1


def credit(uid: int, amount: int) -> None:
    if amount > 0:
        db_exec("UPDATE users SET balance = balance + ? WHERE id=?", (amount, uid))


def claim_rakeback(uid: int) -> int:
    with _db_lock:
        row = db_one("SELECT rakeback FROM users WHERE id=?", (uid,))
        amount = int(row["rakeback"]) if row else 0
        if amount > 0:
            db_exec(
                "UPDATE users SET balance = balance + ?, rakeback = 0 WHERE id=?",
                (amount, uid),
            )
        return amount


def set_custom_bet(uid: int, cents: int) -> None:
    db_exec("UPDATE users SET custom_bet=? WHERE id=?", (cents, uid))


def admin_change_balance(uid: int, delta: int) -> Optional[int]:
    with _db_lock:
        row = db_one("SELECT balance FROM users WHERE id=?", (uid,))
        if row is None:
            return None
        new = max(0, int(row["balance"]) + delta)
        db_exec("UPDATE users SET balance=? WHERE id=?", (new, uid))
        return new


# ── админы / каналы / настройки ──

def is_admin(uid: int) -> bool:
    if uid == MAIN_ADMIN_ID:
        return True
    return db_one("SELECT 1 FROM admins WHERE id=?", (uid,)) is not None


def list_admins() -> List[int]:
    return [int(r["id"]) for r in db_all("SELECT id FROM admins ORDER BY id")]


def list_channels() -> List[sqlite3.Row]:
    return db_all("SELECT * FROM channels ORDER BY rowid")


def get_setting(key: str, default: str) -> str:
    row = db_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    db_exec(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


# ── «журнал» активных ставок (защита от потери денег при перезапуске) ──

def bet_open(uid: int, game: str, bet: int) -> None:
    db_exec(
        "INSERT OR REPLACE INTO active_bets (user_id, game, bet) VALUES (?,?,?)",
        (uid, game, bet),
    )


def bet_close(uid: int, game: str) -> None:
    db_exec("DELETE FROM active_bets WHERE user_id=? AND game=?", (uid, game))


def refund_open_bets() -> List[Tuple[int, str, int]]:
    """Состояние Мин/Башни живёт в памяти. Если бот перезапустился —
    возвращаем ставки незавершённых игр."""
    with _db_lock:
        rows = db_all("SELECT user_id, game, bet FROM active_bets")
        result: List[Tuple[int, str, int]] = []
        for r in rows:
            credit(int(r["user_id"]), int(r["bet"]))
            result.append((int(r["user_id"]), str(r["game"]), int(r["bet"])))
        db_exec("DELETE FROM active_bets")
        return result


# ───────────────────────────── УТИЛИТЫ ─────────────────────────────

def fmt(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}{cents // 100}.{cents % 100:02d}$"


def parse_money(s: str, allow_negative: bool = False) -> Optional[int]:
    s = s.strip().replace(",", ".").replace("$", "")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    if not d.is_finite():
        return None
    if d < 0 and not allow_negative:
        return None
    cents = int((d * 100).to_integral_value(rounding=ROUND_DOWN))
    if abs(cents) > 10 ** 12:
        return None
    return cents


def calc_payout(bet: int, mult: float) -> int:
    """Выплата = ставка * множитель * (1 - комиссия), округление вниз."""
    value = Decimal(bet) * Decimal(str(mult)) * (Decimal(1) - COMMISSION)
    return int(value.to_integral_value(rounding=ROUND_DOWN))


_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def user_lock(uid: int) -> asyncio.Lock:
    return _locks[uid]


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def chunk(buttons: List[InlineKeyboardButton], n: int) -> Kb:
    return [buttons[i:i + n] for i in range(0, len(buttons), n)]


async def safe_edit(query: Any, text: str, markup: Optional[InlineKeyboardMarkup] = None) -> None:
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning("edit failed: %s", e)
    except TelegramError as e:
        logger.warning("edit failed: %s", e)


# ───────────────────────────── ОПИСАНИЕ ИГР ─────────────────────────────

GAMES: Dict[str, Dict[str, str]] = {
    "dice": {"title": "🎲 Кубик", "kind": "dice"},
    "darts": {"title": "🎯 Дартс", "kind": "dice"},
    "football": {"title": "⚽ Футбол", "kind": "dice"},
    "basket": {"title": "🏀 Баскетбол", "kind": "dice"},
    "bowl": {"title": "🎳 Боулинг", "kind": "dice"},
    "crash": {"title": "🚀 Краш", "kind": "crash"},
    "mines": {"title": "💣 Мины", "kind": "mines"},
    "tower": {"title": "🏰 Башня", "kind": "tower"},
}

# options: (ключ, название, выигрышные значения, множитель)
# множители «честные» (1 / вероятность); казино берёт 5% с выплаты.
DICE_RULES: Dict[str, Dict[str, Any]] = {
    "dice": {
        "title": "🎲 Кубик",
        "emoji": "🎲",
        "real": True,
        "options": [
            ("even", "Чёт", {2, 4, 6}, 2.0),
            ("odd", "Нечёт", {1, 3, 5}, 2.0),
            ("high", "Больше 3", {4, 5, 6}, 2.0),
            ("low", "Меньше 4", {1, 2, 3}, 2.0),
            ("center", "Центр (3,4)", {3, 4}, 3.0),
            ("edges", "Края (1,2,5,6)", {1, 2, 5, 6}, 1.5),
        ],
        "texts": {v: f"🎲 Выпало: {v}" for v in range(1, 7)},
    },
    "darts": {
        "title": "🎯 Дартс",
        "emoji": "🎯",
        "real": True,
        "options": [
            ("red", "🔴 Красный (1,3,5)", {1, 3, 5}, 2.0),
            ("white", "⚪ Белый (2,4,6)", {2, 4, 6}, 2.0),
            ("bounce", "Отскок (1)", {1}, 6.0),
            ("center", "Центр (6)", {6}, 6.0),
            ("any", "Любой (2-5)", {2, 3, 4, 5}, 1.5),
        ],
        "texts": {
            1: "🎯 1 — 🔴 отскок",
            2: "🎯 2 — ⚪ белый сектор",
            3: "🎯 3 — 🔴 красный сектор",
            4: "🎯 4 — ⚪ белый сектор",
            5: "🎯 5 — 🔴 красный сектор",
            6: "🎯 6 — ⚪ центр, яблочко!",
        },
    },
    "football": {
        "title": "⚽ Футбол",
        "emoji": "🎲",  # анимации футбола нет -> кубик, число = событие
        "real": True,
        "options": [
            ("goal", "Гол", {1, 2, 3}, 2.0),
            ("miss", "Мимо", {4, 5, 6}, 2.0),
            ("center", "Гол в центр", {3}, 6.0),
        ],
        "texts": {
            1: "⚽ 1 — гол!",
            2: "⚽ 2 — гол!",
            3: "⚽ 3 — гол в центр ворот!",
            4: "⚽ 4 — мимо ворот",
            5: "⚽ 5 — мимо ворот",
            6: "⚽ 6 — мимо ворот",
        },
    },
    "basket": {
        "title": "🏀 Баскетбол",
        "emoji": "🎰",  # только визуал
        "real": False,
        "options": [
            ("goal", "Гол", {4, 5, 6}, 2.0),
            ("nogoal", "Без гола", {1, 2, 3}, 2.0),
            ("bounce", "Отскок", {1}, 6.0),
            ("stuck", "Застрял", {2}, 6.0),
            ("clean", "Чистый гол", {6}, 6.0),
        ],
        "texts": {
            1: "🏀 1 — мяч отскочил от кольца",
            2: "🏀 2 — мяч застрял в кольце",
            3: "🏀 3 — мимо кольца",
            4: "🏀 4 — гол!",
            5: "🏀 5 — гол!",
            6: "🏀 6 — чистый гол (swish)!",
        },
    },
    "bowl": {
        "title": "🎳 Боулинг",
        "emoji": "🎰",  # только визуал
        "real": False,
        "options": [
            ("strike", "Страйк", {6}, 6.0),
            ("miss", "Мимо", {1}, 6.0),
            ("p1", "Сбито 1/6", {2}, 6.0),
            ("p3", "Сбито 3/6", {3}, 6.0),
            ("p4", "Сбито 4/6", {4}, 6.0),
            ("p5", "Сбито 5/6", {5}, 6.0),
        ],
        "texts": {
            1: "🎳 1 — мимо, ни одной кегли",
            2: "🎳 2 — сбито 1/6",
            3: "🎳 3 — сбито 3/6",
            4: "🎳 4 — сбито 4/6",
            5: "🎳 5 — сбито 5/6",
            6: "🎳 6 — СТРАЙК!",
        },
    },
}

# ───────────────────────────── ПОДПИСКИ ─────────────────────────────

async def get_missing_channels(bot: Any, uid: int) -> List[sqlite3.Row]:
    if get_setting("sub_required", "1") != "1":
        return []
    missing: List[sqlite3.Row] = []
    for ch in list_channels():
        try:
            member = await bot.get_chat_member(int(ch["chat_id"]), uid)
        except (TelegramError, ValueError) as e:
            logger.warning("Не могу проверить подписку на %s: %s", ch["chat_id"], e)
            continue
        status = member.status
        if status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
            missing.append(ch)
        elif status == ChatMemberStatus.RESTRICTED and not getattr(member, "is_member", True):
            missing.append(ch)
    return missing


async def ensure_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return False
    if is_admin(user.id):
        return True
    missing = await get_missing_channels(context.bot, user.id)
    if not missing:
        return True
    rows: Kb = []
    lines = ["📢 Чтобы пользоваться ботом, подпишитесь на каналы:"]
    for ch in missing:
        lines.append(f"• {ch['title']}")
        if ch["link"]:
            rows.append([InlineKeyboardButton(f"📢 {ch['title']}", url=ch["link"])])
    rows.append([btn("✅ Проверить подписку", "chk_sub")])
    await context.bot.send_message(
        chat.id, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows)
    )
    return False


# ───────────────────────────── ГЛАВНЫЕ ЭКРАНЫ ─────────────────────────────

def games_text(uid: int) -> str:
    u = get_user(uid)
    text = f"🎮 Выберите игру\n\n💰 Баланс: {fmt(u['balance'])}"
    if u["custom_bet"]:
        text += f"\n⚙️ Своя ставка: {fmt(u['custom_bet'])}"
    text += "\n\nБыстро: «ставка 1.5» — своя ставка, «вб» — весь баланс"
    return text


def games_markup() -> InlineKeyboardMarkup:
    buttons = [btn(g["title"], f"g:{key}") for key, g in GAMES.items()]
    return InlineKeyboardMarkup(chunk(buttons, 2))


def balance_view(uid: int) -> Tuple[str, InlineKeyboardMarkup]:
    u = get_user(uid)
    text = (
        f"💰 Баланс: {fmt(u['balance'])}\n"
        f"🎁 Рейкбек: {fmt(u['rakeback'])} ({RAKEBACK_PERCENT}% с каждой ставки)"
    )
    if u["custom_bet"]:
        text += f"\n⚙️ Своя ставка: {fmt(u['custom_bet'])}"
    rows: Kb = []
    if u["rakeback"] > 0:
        rows.append([btn(f"🎁 Забрать рейкбек ({fmt(u['rakeback'])})", "rb:claim")])
    rows.append([btn("🎮 Играть", "menu:games")])
    return text, InlineKeyboardMarkup(rows)


def main_menu_view(uid: int) -> Tuple[str, InlineKeyboardMarkup]:
    text = "📋 Меню"
    rows: Kb = [
        [btn("🎮 Играть", "menu:games"), btn("💰 Баланс", "menu:balance")],
        [btn("ℹ️ Правила", "menu:rules")],
    ]
    if is_admin(uid):
        rows.append([btn("🛠 Админка", "ad:main")])
    return text, InlineKeyboardMarkup(rows)


RULES_TEXT = (
    "ℹ️ Правила\n\n"
    f"• Старт: {fmt(START_BALANCE)}, минимальная ставка {fmt(MIN_BET)}\n"
    "• Комиссия казино 5%: выплата = ставка × множитель × 0.95\n"
    f"• Рейкбек {RAKEBACK_PERCENT}% от каждой ставки копится отдельно "
    "(Баланс → «Забрать рейкбек»)\n"
    "• «ставка 1.5» — запомнить свою ставку, «вб» — поставить весь баланс\n"
    "• Мины и Башня: выигрыш можно забрать в любой момент\n"
    "• Футбол/Баскетбол/Боулинг — результат определяется числом 1–6"
)


# ───────────────────────────── ВЫБОР СТАВКИ / ИСХОДА ─────────────────────────────

def bet_menu(uid: int, game: str) -> Tuple[str, InlineKeyboardMarkup]:
    u = get_user(uid)
    text = (
        f"{GAMES[game]['title']}\n\n"
        f"💰 Баланс: {fmt(u['balance'])}\n"
        "Выберите ставку:"
    )
    rows = chunk([btn(fmt(p), f"b:{game}:{p}") for p in BET_PRESETS], 3)
    rows.append([btn("🔥 Ва-банк", f"ba:{game}")])
    label = "✏️ Использовать свою ставку"
    if u["custom_bet"]:
        label += f" ({fmt(u['custom_bet'])})"
    rows.append([btn(label, f"bc:{game}")])
    rows.append([btn("◀️ Назад", "menu:games")])
    return text, InlineKeyboardMarkup(rows)


def options_menu(game: str, bet: int) -> Tuple[str, InlineKeyboardMarkup]:
    kind = GAMES[game]["kind"]
    title = GAMES[game]["title"]
    if kind == "dice":
        rules = DICE_RULES[game]
        buttons = [
            btn(f"{label} x{mult:g}", f"t:{game}:{bet}:{key}")
            for key, label, _w, mult in rules["options"]
        ]
        rows = chunk(buttons, 2)
        text = f"{title}\nСтавка: {fmt(bet)}\n\nВыберите исход:"
    elif kind == "crash":
        buttons = [
            btn(f"x{m} · {100 / m:.1f}%", f"t:{game}:{bet}:{m}") for m in CRASH_MULTS
        ]
        rows = chunk(buttons, 3)
        text = (
            f"{title}\nСтавка: {fmt(bet)}\n\n"
            "Выберите множитель. Шанс выигрыша = 100 / множитель."
        )
    else:  # mines
        buttons = [
            btn(f"{n} 💣", f"t:{game}:{bet}:{n}") for n in range(MINES_MIN, MINES_MAX + 1)
        ]
        rows = chunk(buttons, 4)
        text = (
            f"{title}\nСтавка: {fmt(bet)}\n\n"
            "Выберите количество мин (поле 5x5). Чем больше мин — тем выше множитель."
        )
    rows.append([btn("◀️ Назад", f"g:{game}")])
    return text, InlineKeyboardMarkup(rows)


def again_markup(game: str, bet: int, opt: Optional[str]) -> InlineKeyboardMarkup:
    again_cb = f"t:{game}:{bet}:{opt}" if opt is not None else f"b:{game}:{bet}"
    return InlineKeyboardMarkup(
        [
            [btn("🔁 Повторить", again_cb)],
            [btn("💵 Другая ставка", f"g:{game}"), btn("🎮 Игры", "menu:games")],
        ]
    )


# ───────────────────────────── ИГРЫ: КУБИК / ДАРТС / ФУТБОЛ / БАСКЕТБОЛ / БОУЛИНГ ─────────────────────────────

async def play_dice_game(
    query: Any, context: ContextTypes.DEFAULT_TYPE, game: str, bet: int, opt: str
) -> None:
    uid = query.from_user.id
    rules = DICE_RULES[game]
    option = next((o for o in rules["options"] if o[0] == opt), None)
    if option is None:
        await query.answer("Неизвестный исход", show_alert=True)
        return
    _key, label, wins, mult = option

    if not try_debit(uid, bet):
        await query.answer("Недостаточно средств", show_alert=True)
        return
    await query.answer()

    chat_id = query.message.chat_id
    await safe_edit(
        query, f"{rules['title']}\nСтавка: {fmt(bet)} · {label}\n\nБросаем…"
    )
    try:
        dice_msg = await context.bot.send_dice(chat_id=chat_id, emoji=rules["emoji"])
    except TelegramError as e:
        logger.error("send_dice failed: %s", e)
        credit(uid, bet)
        await context.bot.send_message(chat_id, "⚠️ Ошибка Telegram, ставка возвращена.")
        return

    await asyncio.sleep(4)  # ждём окончания анимации

    if rules["real"] and dice_msg.dice is not None:
        value = int(dice_msg.dice.value)
    else:
        value = RNG.randint(1, 6)

    win = value in wins
    payout = calc_payout(bet, mult) if win else 0
    if win:
        credit(uid, payout)

    lines = [
        rules["texts"].get(value, f"Выпало: {value}"),
        "",
        f"Ставка: {fmt(bet)} на «{label}» (x{mult:g})",
    ]
    if win:
        lines.append(f"✅ Победа! Выплата {fmt(payout)} (чистыми {fmt(payout - bet)})")
    else:
        lines.append(f"❌ Проигрыш −{fmt(bet)}")
    lines.append(f"💰 Баланс: {fmt(get_balance(uid))}")
    await context.bot.send_message(
        chat_id, "\n".join(lines), reply_markup=again_markup(game, bet, opt)
    )


# ───────────────────────────── ИГРА: КРАШ ─────────────────────────────

async def play_crash(
    query: Any, context: ContextTypes.DEFAULT_TYPE, bet: int, mult: int
) -> None:
    uid = query.from_user.id
    if mult not in CRASH_MULTS:
        await query.answer("Неизвестный множитель", show_alert=True)
        return
    if not try_debit(uid, bet):
        await query.answer("Недостаточно средств", show_alert=True)
        return
    await query.answer()

    win = RNG.random() * 100 < 100.0 / mult
    if win:
        crash_at = round(mult * RNG.uniform(1.0, 1.4), 2)
    else:
        crash_at = round(RNG.uniform(1.0, mult - 0.01), 2)

    title = GAMES["crash"]["title"]
    head = f"{title}\nСтавка: {fmt(bet)} · цель x{mult}\n\n"
    for frac in (0.25, 0.6, 0.9):
        cur = 1.0 + (crash_at - 1.0) * frac
        await safe_edit(query, head + f"🚀 Ракета летит… x{cur:.2f}")
        await asyncio.sleep(0.8)

    if win:
        payout = calc_payout(bet, mult)
        credit(uid, payout)
        result = (
            f"💥 Ракета взорвалась на x{crash_at:.2f}\n"
            f"✅ Вы успели забрать x{mult}! Выплата {fmt(payout)}"
        )
    else:
        result = (
            f"💥 Ракета взорвалась на x{crash_at:.2f} — раньше цели x{mult}\n"
            f"❌ Проигрыш −{fmt(bet)}"
        )
    text = head + result + f"\n\n💰 Баланс: {fmt(get_balance(uid))}"
    await safe_edit(query, text, again_markup("crash", bet, str(mult)))


# ───────────────────────────── ИГРА: МИНЫ ─────────────────────────────

MINES: Dict[int, Dict[str, Any]] = {}


def mines_mult(mines: int, opened: int) -> float:
    m = 1.0
    for i in range(opened):
        m *= (MINES_SIZE - i) / (MINES_SIZE - mines - i)
    return min(m, MINES_MAX_MULT)


def mines_text(g: Dict[str, Any]) -> str:
    k = len(g["opened"])
    safe = MINES_SIZE - g["count"]
    cur = mines_mult(g["count"], k)
    nxt = mines_mult(g["count"], k + 1) if k < safe else cur
    text = (
        f"💣 Мины · мин на поле: {g['count']}\n"
        f"Ставка: {fmt(g['bet'])}\n"
        f"Открыто: {k}/{safe}\n"
    )
    if k:
        text += f"Сейчас: x{cur:.2f} → {fmt(calc_payout(g['bet'], cur))}\n"
    text += f"Следующая клетка: x{nxt:.2f}"
    return text


def mines_board(g: Dict[str, Any], reveal: bool = False, hit: Optional[int] = None) -> Kb:
    rows: Kb = []
    for r in range(5):
        row: List[InlineKeyboardButton] = []
        for c in range(5):
            i = r * 5 + c
            if i in g["opened"]:
                row.append(btn("💎", "noop"))
            elif reveal and i in g["mines"]:
                row.append(btn("💥" if i == hit else "💣", "noop"))
            elif reveal:
                row.append(btn("▫️", "noop"))
            else:
                row.append(btn("❓", f"mc:{i}"))
        rows.append(row)
    return rows


def mines_markup(g: Dict[str, Any]) -> InlineKeyboardMarkup:
    rows = mines_board(g)
    k = len(g["opened"])
    if k >= 1:
        payout = calc_payout(g["bet"], mines_mult(g["count"], k))
        rows.append([btn(f"💰 Забрать {fmt(payout)}", "mcash")])
    return InlineKeyboardMarkup(rows)


def mines_end_markup(g: Dict[str, Any], hit: Optional[int]) -> InlineKeyboardMarkup:
    rows = mines_board(g, reveal=True, hit=hit)
    rows.append([btn("🔁 Играть снова", f"t:mines:{g['bet']}:{g['count']}")])
    rows.append([btn("🎮 Игры", "menu:games")])
    return InlineKeyboardMarkup(rows)


async def start_mines(
    query: Any, context: ContextTypes.DEFAULT_TYPE, bet: int, count: int
) -> None:
    uid = query.from_user.id
    if not MINES_MIN <= count <= MINES_MAX:
        await query.answer("Неверное количество мин", show_alert=True)
        return
    async with user_lock(uid):
        if uid in MINES:
            await query.answer("У вас уже идёт игра в Мины — продолжайте!", show_alert=True)
            g = MINES[uid]
            await context.bot.send_message(
                query.message.chat_id, mines_text(g), reply_markup=mines_markup(g)
            )
            return
        if not try_debit(uid, bet):
            await query.answer("Недостаточно средств", show_alert=True)
            return
        bet_open(uid, "mines", bet)
        g = {
            "bet": bet,
            "count": count,
            "mines": set(RNG.sample(range(MINES_SIZE), count)),
            "opened": set(),
        }
        MINES[uid] = g
    await query.answer()
    await safe_edit(query, mines_text(g), mines_markup(g))


async def _mines_win(query: Any, uid: int, g: Dict[str, Any], auto: bool) -> None:
    """Вызывать под user_lock."""
    k = len(g["opened"])
    mult = mines_mult(g["count"], k)
    payout = calc_payout(g["bet"], mult)
    credit(uid, payout)
    MINES.pop(uid, None)
    bet_close(uid, "mines")
    head = "🏆 Все безопасные клетки открыты!" if auto else "✅ Выигрыш забран!"
    text = (
        f"💣 Мины · мин: {g['count']}\n{head}\n"
        f"x{mult:.2f} · выплата {fmt(payout)} (комиссия 5% учтена)\n\n"
        f"💰 Баланс: {fmt(get_balance(uid))}"
    )
    await safe_edit(query, text, mines_end_markup(g, None))


async def mines_click(query: Any, idx: int) -> None:
    uid = query.from_user.id
    async with user_lock(uid):
        g = MINES.get(uid)
        if g is None:
            await query.answer("Игра уже завершена", show_alert=True)
            await safe_edit(query, "Эта игра уже завершена.", InlineKeyboardMarkup(
                [[btn("🎮 Игры", "menu:games")]]))
            return
        if idx in g["opened"]:
            await query.answer()
            return
        if idx in g["mines"]:
            MINES.pop(uid, None)
            bet_close(uid, "mines")
            text = (
                f"💥 Бабах! Вы попали на мину.\n"
                f"Ставка {fmt(g['bet'])} проиграна.\n\n"
                f"💰 Баланс: {fmt(get_balance(uid))}"
            )
            await query.answer()
            await safe_edit(query, text, mines_end_markup(g, idx))
            return
        g["opened"].add(idx)
        k = len(g["opened"])
        safe = MINES_SIZE - g["count"]
        await query.answer()
        if k >= safe or mines_mult(g["count"], k) >= MINES_MAX_MULT:
            await _mines_win(query, uid, g, auto=True)
        else:
            await safe_edit(query, mines_text(g), mines_markup(g))


async def mines_cashout(query: Any) -> None:
    uid = query.from_user.id
    async with user_lock(uid):
        g = MINES.get(uid)
        if g is None:
            await query.answer("Игра уже завершена", show_alert=True)
            return
        if not g["opened"]:
            await query.answer("Сначала откройте хотя бы одну клетку", show_alert=True)
            return
        await query.answer()
        await _mines_win(query, uid, g, auto=False)


# ───────────────────────────── ИГРА: БАШНЯ ─────────────────────────────

TOWER: Dict[int, Dict[str, Any]] = {}


def tower_text(g: Dict[str, Any], reveal: bool = False, lost: bool = False) -> str:
    lines = [f"🏰 Башня · ставка {fmt(g['bet'])}", ""]
    for lvl in range(len(TOWER_MULTS) - 1, -1, -1):
        mult = TOWER_MULTS[lvl]
        cells = ["🚪"] * TOWER_DOORS
        if lvl < g["level"]:
            cells[g["picks"][lvl]] = "✅"
            if reveal:
                cells[g["mines"][lvl]] = "💣"
            mark = "✅"
        elif lvl == g["level"]:
            mark = "👉"
            if lost:
                cells[g["mines"][lvl]] = "💥"
                mark = "💥"
            elif reveal:
                cells[g["mines"][lvl]] = "💣"
        else:
            cells = ["🔒"] * TOWER_DOORS
            mark = "🔒"
        lines.append(f"{mark} {lvl + 1}  x{mult:.1f}  {' '.join(cells)}")
    if g["level"] >= 1 and not reveal:
        m = TOWER_MULTS[g["level"] - 1]
        lines.append("")
        lines.append(f"Сейчас: x{m:.1f} → {fmt(calc_payout(g['bet'], m))}")
    return "\n".join(lines)


def tower_markup(g: Dict[str, Any]) -> InlineKeyboardMarkup:
    rows: Kb = [[btn(f"🚪 {d + 1}", f"td:{d}") for d in range(TOWER_DOORS)]]
    if g["level"] >= 1:
        m = TOWER_MULTS[g["level"] - 1]
        rows.append([btn(f"💰 Забрать {fmt(calc_payout(g['bet'], m))} (x{m:.1f})", "tcash")])
    return InlineKeyboardMarkup(rows)


def tower_end_markup(g: Dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn("🔁 Играть снова", f"b:tower:{g['bet']}")],
            [btn("🎮 Игры", "menu:games")],
        ]
    )


async def start_tower(query: Any, context: ContextTypes.DEFAULT_TYPE, bet: int) -> None:
    uid = query.from_user.id
    async with user_lock(uid):
        if uid in TOWER:
            await query.answer("У вас уже идёт игра в Башню — продолжайте!", show_alert=True)
            g = TOWER[uid]
            await context.bot.send_message(
                query.message.chat_id, tower_text(g), reply_markup=tower_markup(g)
            )
            return
        if not try_debit(uid, bet):
            await query.answer("Недостаточно средств", show_alert=True)
            return
        bet_open(uid, "tower", bet)
        g = {
            "bet": bet,
            "level": 0,
            "picks": [],
            "mines": [RNG.randrange(TOWER_DOORS) for _ in TOWER_MULTS],
        }
        TOWER[uid] = g
    await query.answer()
    await safe_edit(query, tower_text(g), tower_markup(g))


async def _tower_win(query: Any, uid: int, g: Dict[str, Any], complete: bool) -> None:
    """Вызывать под user_lock."""
    mult = TOWER_MULTS[g["level"] - 1]
    payout = calc_payout(g["bet"], mult)
    credit(uid, payout)
    TOWER.pop(uid, None)
    bet_close(uid, "tower")
    head = "🏆 Башня пройдена!" if complete else "✅ Выигрыш забран!"
    text = (
        tower_text(g, reveal=True)
        + f"\n\n{head}\nx{mult:.1f} · выплата {fmt(payout)} (комиссия 5% учтена)"
        + f"\n💰 Баланс: {fmt(get_balance(uid))}"
    )
    await safe_edit(query, text, tower_end_markup(g))


async def tower_click(query: Any, door: int) -> None:
    uid = query.from_user.id
    async with user_lock(uid):
        g = TOWER.get(uid)
        if g is None:
            await query.answer("Игра уже завершена", show_alert=True)
            await safe_edit(query, "Эта игра уже завершена.", InlineKeyboardMarkup(
                [[btn("🎮 Игры", "menu:games")]]))
            return
        if not 0 <= door < TOWER_DOORS:
            await query.answer()
            return
        lvl = g["level"]
        await query.answer()
        if door == g["mines"][lvl]:
            TOWER.pop(uid, None)
            bet_close(uid, "tower")
            text = (
                tower_text(g, reveal=True, lost=True)
                + f"\n\n💥 Там была мина! Ставка {fmt(g['bet'])} проиграна."
                + f"\n💰 Баланс: {fmt(get_balance(uid))}"
            )
            await safe_edit(query, text, tower_end_markup(g))
            return
        g["picks"].append(door)
        g["level"] += 1
        if g["level"] >= len(TOWER_MULTS):
            await _tower_win(query, uid, g, complete=True)
        else:
            await safe_edit(query, tower_text(g), tower_markup(g))


async def tower_cashout(query: Any) -> None:
    uid = query.from_user.id
    async with user_lock(uid):
        g = TOWER.get(uid)
        if g is None:
            await query.answer("Игра уже завершена", show_alert=True)
            return
        if g["level"] < 1:
            await query.answer("Сначала пройдите хотя бы один уровень", show_alert=True)
            return
        await query.answer()
        await _tower_win(query, uid, g, complete=False)


# ───────────────────────────── АДМИНКА ─────────────────────────────

def admin_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn("💸 Баланс игрока", "ad:bal"), btn("📢 Подписки", "ad:subs")],
            [btn("👮 Админы", "ad:adm"), btn("📨 Рассылка", "ad:bc")],
            [btn("📊 Статистика", "ad:stat")],
        ]
    )


def cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn("✖️ Отмена", "ad:cancel")]])


def subs_view() -> Tuple[str, InlineKeyboardMarkup]:
    on = get_setting("sub_required", "1") == "1"
    channels = list_channels()
    lines = [f"📢 Обязательные подписки: {'ВКЛ ✅' if on else 'ВЫКЛ ⛔'}", ""]
    if channels:
        for ch in channels:
            lines.append(f"• {ch['title']} ({ch['chat_id']})")
    else:
        lines.append("Каналов нет.")
    lines.append("")
    lines.append("Бот должен быть администратором канала.")
    rows: Kb = [[btn("➕ Добавить канал", "sub:add")]]
    for ch in channels:
        rows.append([btn(f"🗑 Удалить {ch['title']}", f"sub:del:{ch['chat_id']}")])
    rows.append([btn("⛔ Выключить" if on else "✅ Включить", "sub:tog")])
    rows.append([btn("◀️ Назад", "ad:main")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None or not is_admin(user.id):
        return
    context.user_data.pop("action", None)
    await update.message.reply_text("🛠 Админ-панель", reply_markup=admin_markup())


async def admin_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
) -> None:
    query = update.callback_query
    assert query is not None
    uid = query.from_user.id
    if not is_admin(uid):
        await query.answer("Нет доступа", show_alert=True)
        return
    await query.answer()
    ud = context.user_data

    if data == "ad:main" or data == "ad:cancel":
        ud.pop("action", None)
        await safe_edit(query, "🛠 Админ-панель", admin_markup())
    elif data == "ad:bal":
        ud["action"] = "bal"
        await safe_edit(
            query,
            "💸 Отправьте: ID сумма\n"
            "Пример выдачи: 123456789 5\n"
            "Пример изъятия: 123456789 -2.5",
            cancel_markup(),
        )
    elif data == "ad:subs":
        ud.pop("action", None)
        text, markup = subs_view()
        await safe_edit(query, text, markup)
    elif data == "sub:add":
        ud["action"] = "sub_add"
        await safe_edit(
            query,
            "Отправьте @username канала или его числовой ID (-100…).\n"
            "Бот должен быть администратором этого канала.",
            cancel_markup(),
        )
    elif data == "sub:tog":
        on = get_setting("sub_required", "1") == "1"
        set_setting("sub_required", "0" if on else "1")
        text, markup = subs_view()
        await safe_edit(query, text, markup)
    elif data.startswith("sub:del:"):
        db_exec("DELETE FROM channels WHERE chat_id=?", (data[len("sub:del:"):],))
        text, markup = subs_view()
        await safe_edit(query, text, markup)
    elif data == "ad:adm":
        if uid != MAIN_ADMIN_ID:
            await query.answer("Только главный админ", show_alert=True)
            return
        ud["action"] = "adm"
        admins = list_admins()
        listing = "\n".join(f"• {a}" for a in admins) if admins else "—"
        await safe_edit(
            query,
            f"👮 Админы (кроме главного {MAIN_ADMIN_ID}):\n{listing}\n\n"
            "Отправьте: add ID — добавить, del ID — удалить",
            cancel_markup(),
        )
    elif data == "ad:bc":
        ud["action"] = "bc"
        await safe_edit(
            query,
            "📨 Отправьте сообщение для рассылки (текст, фото, видео — что угодно).",
            cancel_markup(),
        )
    elif data == "ad:stat":
        row = db_one("SELECT COUNT(*) AS n, COALESCE(SUM(balance),0) AS b, "
                     "COALESCE(SUM(rakeback),0) AS r FROM users")
        assert row is not None
        text = (
            f"📊 Игроков: {row['n']}\n"
            f"💰 Сумма балансов: {fmt(row['b'])}\n"
            f"🎁 Сумма рейкбека: {fmt(row['r'])}\n"
            f"👮 Админов: {len(list_admins()) + 1}\n"
            f"📢 Каналов: {len(list_channels())}"
        )
        await safe_edit(query, text, admin_markup())


async def do_broadcast(
    context: ContextTypes.DEFAULT_TYPE, from_chat_id: int, message_id: int
) -> Tuple[int, int]:
    ids = [int(r["id"]) for r in db_all("SELECT id FROM users")]
    ok = fail = 0
    for target in ids:
        try:
            await context.bot.copy_message(
                chat_id=target, from_chat_id=from_chat_id, message_id=message_id
            )
            ok += 1
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await context.bot.copy_message(
                    chat_id=target, from_chat_id=from_chat_id, message_id=message_id
                )
                ok += 1
            except TelegramError:
                fail += 1
        except TelegramError:
            fail += 1
        await asyncio.sleep(0.05)
    return ok, fail


async def handle_admin_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, action: str
) -> None:
    msg = update.effective_message
    user = update.effective_user
    assert msg is not None and user is not None
    text = (msg.text or "").strip()

    if action == "bal":
        parts = text.split()
        if len(parts) != 2 or not parts[0].lstrip("-").isdigit():
            await msg.reply_text("Формат: ID сумма (например 123456789 5 или 123456789 -2.5)",
                                 reply_markup=cancel_markup())
            return
        target = int(parts[0])
        delta = parse_money(parts[1], allow_negative=True)
        if delta is None or delta == 0:
            await msg.reply_text("Неверная сумма.", reply_markup=cancel_markup())
            return
        new = admin_change_balance(target, delta)
        if new is None:
            await msg.reply_text("Пользователь не найден (он должен нажать /start).",
                                 reply_markup=cancel_markup())
            return
        context.user_data.pop("action", None)
        sign = "+" if delta > 0 else "−"
        await msg.reply_text(
            f"✅ Баланс {target}: {fmt(new)} ({sign}{fmt(abs(delta))})",
            reply_markup=admin_markup(),
        )
        try:
            await context.bot.send_message(
                target, f"💼 Администратор изменил ваш баланс: {sign}{fmt(abs(delta))}\n"
                        f"💰 Баланс: {fmt(new)}")
        except TelegramError:
            pass

    elif action == "sub_add":
        raw = text.strip()
        chat_ref: Any = int(raw) if raw.lstrip("-").isdigit() else raw
        if isinstance(chat_ref, str) and not chat_ref.startswith("@"):
            chat_ref = "@" + chat_ref
        try:
            chat = await context.bot.get_chat(chat_ref)
            me = await context.bot.get_chat_member(chat.id, context.bot.id)
        except TelegramError as e:
            await msg.reply_text(f"❌ Не удалось получить канал: {e}\n"
                                 "Проверьте имя и что бот добавлен в канал.",
                                 reply_markup=cancel_markup())
            return
        if me.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            await msg.reply_text("❌ Сделайте бота администратором канала и повторите.",
                                 reply_markup=cancel_markup())
            return
        link: Optional[str] = None
        if chat.username:
            link = f"https://t.me/{chat.username}"
        else:
            link = chat.invite_link
            if not link:
                try:
                    link = await context.bot.export_chat_invite_link(chat.id)
                except TelegramError:
                    link = None
        db_exec(
            "INSERT OR REPLACE INTO channels (chat_id, title, link) VALUES (?,?,?)",
            (str(chat.id), chat.title or str(chat.id), link),
        )
        context.user_data.pop("action", None)
        text_out, markup = subs_view()
        await msg.reply_text("✅ Канал добавлен.\n\n" + text_out, reply_markup=markup)

    elif action == "adm":
        if user.id != MAIN_ADMIN_ID:
            context.user_data.pop("action", None)
            return
        m = re.match(r"^(add|del)\s+(\d+)$", text, re.I)
        if not m:
            await msg.reply_text("Формат: add ID или del ID", reply_markup=cancel_markup())
            return
        cmd, target = m.group(1).lower(), int(m.group(2))
        if target == MAIN_ADMIN_ID:
            await msg.reply_text("Главного админа менять нельзя.", reply_markup=cancel_markup())
            return
        if cmd == "add":
            db_exec("INSERT OR IGNORE INTO admins (id) VALUES (?)", (target,))
            out = f"✅ {target} теперь админ."
        else:
            db_exec("DELETE FROM admins WHERE id=?", (target,))
            out = f"✅ {target} больше не админ."
        context.user_data.pop("action", None)
        await msg.reply_text(out, reply_markup=admin_markup())

    elif action == "bc":
        context.user_data.pop("action", None)
        await msg.reply_text("📨 Рассылка началась…")
        ok, fail = await do_broadcast(context, msg.chat_id, msg.message_id)
        await msg.reply_text(f"📨 Готово. Доставлено: {ok}, не доставлено: {fail}",
                             reply_markup=admin_markup())


# ───────────────────────────── ХЕНДЛЕРЫ ─────────────────────────────

BET_RE = re.compile(r"^\s*ставка\s+(-?\d+(?:[.,]\d+)?)\s*\$?\s*$", re.I)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return
    ensure_user(user.id, user.username)
    context.user_data.pop("action", None)
    if not await ensure_sub(update, context):
        return
    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!\n\n"
        f"💰 Баланс: {fmt(get_balance(user.id))}\n"
        "Играйте кнопками внизу. Быстрые ставки: «ставка 1.5» и «вб».",
        reply_markup=MAIN_KB,
    )


async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return
    uid = user.id
    ensure_user(uid, user.username)
    text = (msg.text or "").strip()

    # ввод админа (в т.ч. нетекстовые сообщения для рассылки)
    action = context.user_data.get("action")
    if action and is_admin(uid):
        if text in MAIN_BUTTONS:
            context.user_data.pop("action", None)
        else:
            await handle_admin_input(update, context, action)
            return

    if not text:
        return
    if not await ensure_sub(update, context):
        return

    low = text.lower()

    if text == BTN_BALANCE:
        t, m = balance_view(uid)
        await msg.reply_text(t, reply_markup=m)
        return
    if text == BTN_PLAY:
        await msg.reply_text(games_text(uid), reply_markup=games_markup())
        return
    if text == BTN_MENU:
        t, m = main_menu_view(uid)
        await msg.reply_text(t, reply_markup=m)
        return

    # быстрая ставка: «ставка 1.5»
    m = BET_RE.match(text)
    if m:
        cents = parse_money(m.group(1))
        balance = get_balance(uid)
        if cents is None or cents < MIN_BET:
            await msg.reply_text(f"Минимальная ставка — {fmt(MIN_BET)}.")
            return
        if cents > balance:
            await msg.reply_text(f"Недостаточно средств. Ваш баланс: {fmt(balance)}.")
            return
        set_custom_bet(uid, cents)
        await msg.reply_text(f"✅ Своя ставка сохранена: {fmt(cents)}")
        await msg.reply_text(games_text(uid), reply_markup=games_markup())
        return

    # ва-банк: «вб»
    if low == "вб":
        balance = get_balance(uid)
        if balance < MIN_BET:
            await msg.reply_text("Баланс слишком мал для ставки.")
            return
        set_custom_bet(uid, balance)
        await msg.reply_text(f"🔥 Ва-банк! Ставка: {fmt(balance)}")
        await msg.reply_text(games_text(uid), reply_markup=games_markup())
        return


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    user = query.from_user
    uid = user.id
    ensure_user(uid, user.username)
    data = query.data or ""
    parts = data.split(":")
    cmd = parts[0]

    try:
        if data == "noop":
            await query.answer()
            return

        # ── админка (свои проверки доступа) ──
        if cmd in ("ad", "sub"):
            await admin_callback(update, context, data)
            return

        # ── действия внутри уже идущих игр: без проверки подписки,
        #    чтобы игрок всегда мог забрать выигрыш ──
        if cmd == "mc":
            await mines_click(query, int(parts[1]))
            return
        if cmd == "mcash":
            await mines_cashout(query)
            return
        if cmd == "td":
            await tower_click(query, int(parts[1]))
            return
        if cmd == "tcash":
            await tower_cashout(query)
            return

        # ── проверка подписки ──
        if cmd == "chk_sub":
            missing = await get_missing_channels(context.bot, uid)
            if missing:
                await query.answer("Вы подписаны не на все каналы", show_alert=True)
                return
            await query.answer("Готово ✅")
            try:
                await query.message.delete()
            except TelegramError:
                pass
            await context.bot.send_message(
                query.message.chat_id,
                f"✅ Спасибо за подписку!\n💰 Баланс: {fmt(get_balance(uid))}",
                reply_markup=MAIN_KB,
            )
            return

        if not is_admin(uid):
            missing = await get_missing_channels(context.bot, uid)
            if missing:
                await query.answer()
                await ensure_sub(update, context)
                return

        # ── меню ──
        if cmd == "menu":
            await query.answer()
            where = parts[1]
            if where == "games":
                await safe_edit(query, games_text(uid), games_markup())
            elif where == "balance":
                t, m = balance_view(uid)
                await safe_edit(query, t, m)
            elif where == "rules":
                await safe_edit(
                    query, RULES_TEXT,
                    InlineKeyboardMarkup([[btn("◀️ Назад", "menu:main")]]),
                )
            else:
                t, m = main_menu_view(uid)
                await safe_edit(query, t, m)
            return

        if cmd == "rb":
            amount = claim_rakeback(uid)
            if amount <= 0:
                await query.answer("Рейкбека пока нет", show_alert=True)
                return
            await query.answer(f"Зачислено {fmt(amount)}")
            t, m = balance_view(uid)
            await safe_edit(query, t, m)
            return

        # ── выбор игры ──
        if cmd == "g":
            game = parts[1]
            if game not in GAMES:
                await query.answer()
                return
            await query.answer()
            t, m = bet_menu(uid, game)
            await safe_edit(query, t, m)
            return

        # ── выбор ставки: b (пресет), ba (ва-банк), bc (своя) ──
        if cmd in ("b", "ba", "bc"):
            game = parts[1]
            if game not in GAMES:
                await query.answer()
                return
            if cmd == "b":
                bet = int(parts[2])
            elif cmd == "ba":
                bet = get_balance(uid)
            else:
                bet = int(get_user(uid)["custom_bet"])
                if bet <= 0:
                    await query.answer(
                        "Своя ставка не задана. Напишите в чат: ставка 1.5",
                        show_alert=True,
                    )
                    return
            if bet < MIN_BET:
                await query.answer(f"Минимальная ставка — {fmt(MIN_BET)}", show_alert=True)
                return
            if bet > get_balance(uid):
                await query.answer("Недостаточно средств", show_alert=True)
                return
            if GAMES[game]["kind"] == "tower":
                await start_tower(query, context, bet)
            else:
                await query.answer()
                t, m = options_menu(game, bet)
                await safe_edit(query, t, m)
            return

        # ── запуск игры: t:{game}:{bet}:{opt} ──
        if cmd == "t":
            game, bet, opt = parts[1], int(parts[2]), parts[3]
            if game not in GAMES or bet < MIN_BET:
                await query.answer()
                return
            kind = GAMES[game]["kind"]
            if kind == "dice":
                await play_dice_game(query, context, game, bet, opt)
            elif kind == "crash":
                await play_crash(query, context, bet, int(opt))
            elif kind == "mines":
                await start_mines(query, context, bet, int(opt))
            else:
                await query.answer()
            return

        await query.answer()
    except (ValueError, IndexError):
        logger.warning("Некорректный callback: %s", data)
        try:
            await query.answer()
        except TelegramError:
            pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Ошибка в обработчике", exc_info=context.error)


async def post_init(application: Application) -> None:
    refunded = refund_open_bets()
    for uid, game, bet in refunded:
        logger.info("Возврат ставки %s игроку %s (%s)", fmt(bet), uid, game)
        try:
            await application.bot.send_message(
                uid,
                f"♻️ Бот был перезапущен. Ставка {fmt(bet)} в незавершённой игре "
                f"({GAMES.get(game, {}).get('title', game)}) возвращена на баланс.",
            )
        except TelegramError:
            pass


def main() -> None:
    if not TOKEN:
        raise SystemExit("Не задана переменная окружения TELEGRAM_TOKEN")
    db_init()
    threading.Thread(target=run_flask, daemon=True).start()

    application = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )
    private = filters.ChatType.PRIVATE
    application.add_handler(CommandHandler("start", start_cmd, filters=private))
    application.add_handler(CommandHandler("admin", admin_cmd, filters=private))
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(MessageHandler(private & ~filters.COMMAND, message_router))
    application.add_error_handler(error_handler)

    logger.info("Бот запущен")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
