#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram-бот с играми (Кубик, Дартс, Краш, Мины, Башня).

Стек: python-telegram-bot 20.7, SQLite, Flask (health-check для Render).

Переменные окружения:
    TELEGRAM_TOKEN - токен бота (обязательно)
    PORT           - порт для Flask (Render задаёт сам)
    DB_PATH        - путь к файлу SQLite (по умолчанию bot.db)

Все суммы хранятся в базе в ЦЕНТАХ (целые числа), чтобы не было ошибок округления.
"""

import asyncio
import html
import logging
import os
import random
import re
import sqlite3
import threading
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from flask import Flask
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =============================== НАСТРОЙКИ ===============================

BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
DB_PATH = os.environ.get("DB_PATH", "bot.db")
MAIN_ADMIN_ID = 8192612489

START_BALANCE = 200  # 2$ в центах

DICE_BETS = (50, 100, 200)        # ставки для Кубика и Дартса: 0.5$, 1$, 2$
BETS = (50, 100, 200, 500)        # ставки для Краша, Мин и Башни
CRASH_MULTS = (2, 3, 4, 5, 10, 30, 50, 100)

MINES_TOTAL = 25
MINES_COUNT = 5
MINES_SAFE = MINES_TOTAL - MINES_COUNT
MINES_STEP_TENTHS = 2             # +0.2x за каждую безопасную клетку

TOWER_LEVELS = 6
TOWER_DOORS = 5
TOWER_MULTS = (10, 12, 14, 16, 18, 20)
assert len(TOWER_MULTS) == TOWER_LEVELS

TG_GAME_DELAY = 4                 # пауза (сек) до показа результата send_dice
SUB_CACHE_TTL = 60                # кэш успешной проверки подписки (сек)

DICE_TYPES = {
    "even":   ("Чёт",          frozenset({2, 4, 6}), 2),
    "odd":    ("Нечёт",        frozenset({1, 3, 5}), 2),
    "gt3":    ("Больше 3",     frozenset({4, 5, 6}), 2),
    "lt4":    ("Меньше 4",     frozenset({1, 2, 3}), 2),
    "center": ("Центр (3,4)",  frozenset({3, 4}),    2),
    "edges":  ("Края (1,6)",   frozenset({1, 6}),    3),
}
DART_TYPES = {
    "red":    ("🔴 Красный (1,3,5)",  frozenset({1, 3, 5}),    2),
    "white":  ("⚪ Белый (2,4,6)",     frozenset({2, 4, 6}),    2),
    "bounce": ("↩️ Отскок (1)",        frozenset({1}),          6),
    "center": ("🎯 Центр (6)",         frozenset({6}),          6),
    "any":    ("✨ Любой (2,3,4,5)",   frozenset({2, 3, 4, 5}), 1.5),
}
TG_GAMES = {
    "d": {"emoji": "🎲", "title": "Кубик", "bets": DICE_BETS, "types": DICE_TYPES, "entry": "g:dice"},
    "r": {"emoji": "🎯", "title": "Дартс", "bets": DICE_BETS, "types": DART_TYPES, "entry": "g:darts"},
}

BTN_BALANCE = "💰 Баланс"
BTN_PLAY = "🎮 Играть"
BTN_MENU = "📋 Меню"
MENU_BUTTONS = {BTN_BALANCE, BTN_PLAY, BTN_MENU}

NO_SUB_CHECK_PREFIXES = ("m:o:", "m:c", "t:o:", "t:c")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logger = logging.getLogger("bot")

rng = random.SystemRandom()

mines_games = {}
tower_games = {}
_tg_busy = set()
_sub_cache = {}

# ============================== БАЗА ДАННЫХ ==============================

_db_lock = threading.RLock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row

def db_init():
    with _db_lock:
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                balance    INTEGER NOT NULL DEFAULT 200,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS channels (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL UNIQUE,
                title   TEXT,
                link    TEXT,
                enabled INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        _conn.commit()

def db_ensure_user(user_id, username=None):
    with _db_lock:
        _conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, balance) VALUES (?, ?, ?)",
            (user_id, username, START_BALANCE),
        )
        if username:
            _conn.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
        _conn.commit()

def db_user_exists(user_id):
    with _db_lock:
        return _conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone() is not None

def db_balance(user_id):
    with _db_lock:
        row = _conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row["balance"] if row else 0

def db_debit(user_id, amount):
    if amount <= 0:
        return False
    with _db_lock:
        cur = _conn.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
            (amount, user_id, amount),
        )
        _conn.commit()
        return cur.rowcount > 0

def db_credit(user_id, amount):
    if amount <= 0:
        return
    with _db_lock:
        _conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        _conn.commit()

def db_admin_change_balance(user_id, delta):
    with _db_lock:
        row = _conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        old = row["balance"]
        new = max(0, old + delta)
        _conn.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new, user_id))
        _conn.commit()
        return old, new

def db_all_user_ids():
    with _db_lock:
        return [r["user_id"] for r in _conn.execute("SELECT user_id FROM users").fetchall()]

def db_count_users():
    with _db_lock:
        return _conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]

def db_is_admin(user_id):
    with _db_lock:
        return _conn.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)).fetchone() is not None

def db_add_admin(user_id):
    with _db_lock:
        _conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
        _conn.commit()

def db_del_admin(user_id):
    with _db_lock:
        cur = _conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        _conn.commit()
        return cur.rowcount > 0

def db_list_admins():
    with _db_lock:
        return [r["user_id"] for r in _conn.execute("SELECT user_id FROM admins ORDER BY user_id").fetchall()]

def db_add_channel(chat_id, title, link):
    with _db_lock:
        _conn.execute(
            """
            INSERT INTO channels (chat_id, title, link, enabled) VALUES (?, ?, ?, 1)
            ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title, link = excluded.link, enabled = 1
            """,
            (chat_id, title, link),
        )
        _conn.commit()
    _sub_cache.clear()

def db_list_channels(only_enabled=False):
    with _db_lock:
        sql = "SELECT * FROM channels"
        if only_enabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        return _conn.execute(sql).fetchall()

def db_toggle_channel(channel_id):
    with _db_lock:
        _conn.execute("UPDATE channels SET enabled = 1 - enabled WHERE id = ?", (channel_id,))
        _conn.commit()
    _sub_cache.clear()

def db_delete_channel(channel_id):
    with _db_lock:
        _conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        _conn.commit()
    _sub_cache.clear()

# ============================== ВСПОМОГАТЕЛЬНОЕ ==============================

def is_admin(user_id):
    return user_id == MAIN_ADMIN_ID or db_is_admin(user_id)

def fmt(cents):
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(int(cents)), 100)
    if frac == 0:
        s = str(whole)
    elif frac % 10 == 0:
        s = f"{whole}.{frac // 10}"
    else:
        s = f"{whole}.{frac:02d}"
    return f"{sign}{s}$"

def fmt_mult(m):
    return f"x{m:g}"

def parse_money(text):
    s = text.strip().replace(",", ".").replace("$", "")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    if not d.is_finite():
        return None
    cents = int((d * 100).to_integral_value(rounding=ROUND_HALF_UP))
    if abs(cents) > 10 ** 12:
        return None
    return cents

def btn(text, data):
    return InlineKeyboardButton(text, callback_data=data)

def ikb(rows):
    return InlineKeyboardMarkup([list(row) for row in rows])

def chunk(items, n):
    items = list(items)
    return [items[i:i + n] for i in range(0, len(items), n)]

def main_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_BALANCE), KeyboardButton(BTN_PLAY), KeyboardButton(BTN_MENU)]],
        resize_keyboard=True,
    )

def games_menu_markup():
    return ikb([
        [btn("🎲 Телеграм игры", "g:tg")],
        [btn("🎰 Авторские (Краш)", "g:crash")],
        [btn("💣 Мины", "g:mines")],
        [btn("🏰 Башня", "g:tower")],
        [btn("⬅️ В главное меню", "g:main")],
    ])

def games_text(uid):
    return f"🎮 <b>Выберите игру</b>\n\n💰 Баланс: <b>{fmt(db_balance(uid))}</b>"

async def safe_answer(query, text=None, alert=False):
    try:
        await query.answer(text=text, show_alert=alert)
    except TelegramError as e:
        logger.debug("answer failed: %s", e)

async def safe_edit(query, text, markup=None):
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        return True
    except TelegramError as e:
        if "not modified" in str(e).lower():
            return True
        logger.warning("edit failed: %s", e)
        return False

async def send_main_menu(bot, chat_id, uid, title="📋 <b>Главное меню</b>"):
    await bot.send_message(
        chat_id,
        f"{title}\n\n💰 Баланс: <b>{fmt(db_balance(uid))}</b>",
        reply_markup=main_keyboard(),
        parse_mode=ParseMode.HTML,
    )

# ======================== ОБЯЗАТЕЛЬНАЯ ПОДПИСКА ========================

async def get_missing_channels(bot, user_id, force=False):
    channels = db_list_channels(only_enabled=True)
    if not channels:
        return []
    if not force and time.time() - _sub_cache.get(user_id, 0) < SUB_CACHE_TTL:
        return []
    missing = []
    for ch in channels:
        try:
            m = await bot.get_chat_member(ch["chat_id"], user_id)
            ok = m.status in ("member", "administrator", "creator") or (
                m.status == "restricted" and getattr(m, "is_member", False)
            )
            if not ok:
                missing.append(ch)
        except TelegramError as e:
            logger.warning("Не удалось проверить подписку на %s: %s", ch["chat_id"], e)
    if not missing:
        _sub_cache[user_id] = time.time()
    return missing

def sub_text(missing):
    lines = "\n".join(f"• {html.escape(ch['title'] or str(ch['chat_id']))}" for ch in missing)
    return f"📢 <b>Для использования бота подпишитесь на каналы:</b>\n\n{lines}\n\nПосле подписки нажмите «Проверить подписку»."

def sub_markup(missing):
    rows = []
    for ch in missing:
        if ch["link"]:
            rows.append([InlineKeyboardButton(f"📢 {(ch['title'] or 'Канал')[:40]}", url=ch["link"])])
    rows.append([btn("✅ Проверить подписку", "chk")])
    return ikb(rows)

async def ensure_subscribed(update, context):
    user = update.effective_user
    if user is None:
        return False
    if is_admin(user.id):
        return True
    missing = await get_missing_channels(context.bot, user.id)
    if not missing:
        return True
    if update.callback_query:
        await safe_answer(update.callback_query, "Сначала подпишитесь на каналы", True)
    await context.bot.send_message(
        update.effective_chat.id,
        sub_text(missing),
        reply_markup=sub_markup(missing),
        parse_mode=ParseMode.HTML,
    )
    return False

async def cb_check_subscription(update, context):
    query = update.callback_query
    user = query.from_user
    missing = [] if is_admin(user.id) else await get_missing_channels(context.bot, user.id, force=True)
    if missing:
        await safe_answer(query, "❌ Вы подписались не на все каналы", True)
        await safe_edit(query, sub_text(missing), sub_markup(missing))
        return
    await safe_answer(query, "✅ Подписка подтверждена")
    try:
        await query.message.delete()
    except TelegramError:
        pass
    await send_main_menu(context.bot, query.message.chat_id, user.id)

# ============================ ТЕЛЕГРАМ-ИГРЫ ============================

def tg_rules(types):
    return "\n".join(f"• {label} — {fmt_mult(mult)}" for label, _, mult in types.values())

async def tg_choose_bet(update, context, gkey):
    query = update.callback_query
    g = TG_GAMES[gkey]
    await safe_answer(query)
    rows = chunk([btn(fmt(b), f"{gkey}:b:{b}") for b in g["bets"]], 3)
    rows.append([btn("⬅️ Назад", "g:tg")])
    text = (
        f"{g['emoji']} <b>{g['title']}</b>\n\n{tg_rules(g['types'])}\n\n"
        f"💰 Баланс: <b>{fmt(db_balance(query.from_user.id))}</b>\n"
        f"Выберите ставку:"
    )
    await safe_edit(query, text, ikb(rows))

async def tg_choose_type(update, context, gkey, bet):
    query = update.callback_query
    g = TG_GAMES[gkey]
    if bet not in g["bets"]:
        await safe_answer(query)
        return
    await safe_answer(query)
    rows = chunk([btn(label, f"{gkey}:p:{bet}:{tkey}") for tkey, (label, _, _) in g["types"].items()], 2)
    rows.append([btn("⬅️ Назад", g["entry"])])
    text = (
        f"{g['emoji']} <b>{g['title']}</b> | Ставка: <b>{fmt(bet)}</b>\n\n"
        f"💰 Баланс: <b>{fmt(db_balance(query.from_user.id))}</b>\n"
        f"Выберите тип ставки:"
    )
    await safe_edit(query, text, ikb(rows))

async def tg_play(update, context, gkey, bet, tkey):
    query = update.callback_query
    uid = query.from_user.id
    g = TG_GAMES.get(gkey)
    if not g or bet not in g["bets"] or tkey not in g["types"]:
        await safe_answer(query)
        return
    if uid in _tg_busy:
        await safe_answer(query, "⏳ Дождитесь результата предыдущего броска", True)
        return
    if not db_debit(uid, bet):
        await safe_answer(query, "❌ Недостаточно средств", True)
        return

    _tg_busy.add(uid)
    chat_id = query.message.chat_id
    label, wins, mult = g["types"][tkey]
    try:
        await safe_answer(query)
        await safe_edit(
            query,
            f"{g['emoji']} Ставка <b>{fmt(bet)}</b> на «{html.escape(label)}» принята.\nБросаем…",
        )
        try:
            dice_msg = await context.bot.send_dice(chat_id=chat_id, emoji=g["emoji"])
        except TelegramError as e:
            logger.warning("send_dice failed: %s", e)
            db_credit(uid, bet)
            await context.bot.send_message(chat_id, "⚠️ Не удалось отправить бросок. Ставка возвращена.")
            return

        value = dice_msg.dice.value
        win = value in wins
        payout = int(round(bet * mult)) if win else 0
        if payout:
            db_credit(uid, payout)

        await asyncio.sleep(TG_GAME_DELAY)

        if win:
            text = (
                f"{g['emoji']} Выпало: <b>{value}</b>\n\n"
                f"🎉 <b>Победа!</b> «{html.escape(label)}» ({fmt_mult(mult)})\n"
                f"Выплата: <b>{fmt(payout)}</b> (чистый выигрыш {fmt(payout - bet)})"
            )
        else:
            text = (
                f"{g['emoji']} Выпало: <b>{value}</b>\n\n"
                f"😔 <b>Проигрыш.</b> «{html.escape(label)}» не сыграла.\n"
                f"Потеряно: <b>{fmt(bet)}</b>"
            )
        text += f"\n\n💰 Баланс: <b>{fmt(db_balance(uid))}</b>"
        markup = ikb([
            [btn("🔁 Повторить", f"{gkey}:p:{bet}:{tkey}")],
            [btn("💵 Другая ставка", g["entry"]), btn("⬅️ Назад", "g:tg")],
        ])
        await context.bot.send_message(chat_id, text, reply_markup=markup, parse_mode=ParseMode.HTML)
    finally:
        _tg_busy.discard(uid)

# ================================ КРАШ ================================

async def crash_choose_mult(update, context):
    query = update.callback_query
    await safe_answer(query)
    rows = chunk([btn(f"x{m}", f"c:m:{m}") for m in CRASH_MULTS], 4)
    rows.append([btn("⬅️ Назад", "play")])
    chances = "\n".join(f"• x{m} — шанс {round(100 / m, 1):g}%" for m in CRASH_MULTS)
    text = (
        f"🎰 <b>Краш</b>\n\nВыберите множитель. Если ракета долетит — ставка умножается.\n\n{chances}\n\n"
        f"💰 Баланс: <b>{fmt(db_balance(query.from_user.id))}</b>"
    )
    await safe_edit(query, text, ikb(rows))

async def crash_choose_bet(update, context, mult):
    query = update.callback_query
    if mult not in CRASH_MULTS:
        await safe_answer(query)
        return
    await safe_answer(query)
    rows = chunk([btn(fmt(b), f"c:p:{mult}:{b}") for b in BETS], 3)
    rows.append([btn("⬅️ Назад", "g:crash")])
    text = (
        f"🎰 <b>Краш x{mult}</b> (шанс {round(100 / mult, 1):g}%)\n\n"
        f"💰 Баланс: <b>{fmt(db_balance(query.from_user.id))}</b>\nВыберите ставку:"
    )
    await safe_edit(query, text, ikb(rows))

async def crash_play(update, context, mult, bet):
    query = update.callback_query
    uid = query.from_user.id
    if mult not in CRASH_MULTS or bet not in BETS:
        await safe_answer(query)
        return
    if not db_debit(uid, bet):
        await safe_answer(query, "❌ Недостаточно средств", True)
        return

    win = rng.random() < 1.0 / mult
    payout = bet * mult if win else 0
    if payout:
        db_credit(uid, payout)

    if win:
        crash_point = round(mult + rng.uniform(0, mult * 0.6), 2)
    else:
        crash_point = min(round(rng.uniform(1.0, mult), 2), round(mult - 0.01, 2))

    await safe_answer(query)
    await safe_edit(query, f"🚀 Ставка <b>{fmt(bet)}</b> на x{mult}\n\nРакета взлетает… 📈")
    await asyncio.sleep(1.5)

    if win:
        text = (
            f"🚀 Ракета долетела до <b>x{crash_point}</b>!\n\n"
            f"🎉 <b>Победа!</b> Множитель x{mult}\n"
            f"Выплата: <b>{fmt(payout)}</b> (чистый выигрыш {fmt(payout - bet)})"
        )
    else:
        text = (
            f"💥 Ракета упала на <b>x{crash_point}</b>\n\n"
            f"😔 <b>Проигрыш.</b> Нужно было x{mult}.\nПотеряно: <b>{fmt(bet)}</b>"
        )
    text += f"\n\n💰 Баланс: <b>{fmt(db_balance(uid))}</b>"
    markup = ikb([
        [btn("🔁 Повторить", f"c:p:{mult}:{bet}")],
        [btn("🎰 Другой множитель", "g:crash"), btn("⬅️ Назад", "play")],
    ])
    await safe_edit(query, text, markup)

# ================================ МИНЫ ================================

def mines_tenths(game):
    return 10 + MINES_STEP_TENTHS * len(game["opened"])

def mines_payout(game):
    return game["bet"] * mines_tenths(game) // 10

def mines_text(game):
    k = len(game["opened"])
    return (
        f"💣 <b>Мины</b>\n\n"
        f"Ставка: <b>{fmt(game['bet'])}</b>\n"
        f"Мин на поле: {MINES_COUNT}\n"
        f"Открыто: {k}/{MINES_SAFE}\n"
        f"Множитель: <b>x{mines_tenths(game) / 10:.1f}</b>\n"
        f"Забрать сейчас: <b>{fmt(mines_payout(game))}</b>"
    )

def mines_markup(game, reveal=False, hit=None):
    rows = []
    for r in range(5):
        row = []
        for c in range(5):
            idx = r * 5 + c
            if reveal:
                if idx == hit:
                    label = "💥"
                elif idx in game["mines"]:
                    label = "💣"
                elif idx in game["opened"]:
                    label = "💎"
                else:
                    label = "▫️"
                row.append(btn(label, "noop"))
            elif idx in game["opened"]:
                row.append(btn("💎", "noop"))
            else:
                row.append(btn("⬜", f"m:o:{idx}"))
        rows.append(row)
    if reveal:
        rows.append([btn("🔁 Играть снова", "g:mines"), btn("⬅️ Назад", "play")])
    else:
        rows.append([btn(f"💰 Забрать {fmt(mines_payout(game))}", "m:c")])
    return ikb(rows)

def get_active_game(store, query):
    game = store.get(query.from_user.id)
    if game and game["message_id"] == query.message.message_id and game["chat_id"] == query.message.chat_id:
        return game
    return None

async def drop_stale_keyboard(query):
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

async def resume_game(update, context, store, render):
    query = update.callback_query
    game = store[query.from_user.id]
    await safe_answer(query, "Возвращаю вашу незавершённую игру")
    old_chat, old_msg = game["chat_id"], game["message_id"]
    game["chat_id"] = query.message.chat_id
    game["message_id"] = query.message.message_id
    if (old_chat, old_msg) != (game["chat_id"], game["message_id"]):
        try:
            await context.bot.edit_message_reply_markup(chat_id=old_chat, message_id=old_msg, reply_markup=None)
        except TelegramError:
            pass
    text, markup = render(game)
    await safe_edit(query, text, markup)

def render_mines(game):
    return mines_text(game), mines_markup(game)

async def mines_entry(update, context):
    query = update.callback_query
    uid = query.from_user.id
    if uid in mines_games:
        await resume_game(update, context, mines_games, render_mines)
        return
    await safe_answer(query)
    rows = chunk([btn(fmt(b), f"m:s:{b}") for b in BETS], 3)
    rows.append([btn("⬅️ Назад", "play")])
    text = (
        f"💣 <b>Мины</b>\n\nПоле 5x5, {MINES_COUNT} мин. Каждая открытая безопасная клетка "
        f"увеличивает множитель на 0.2x. Забрать выигрыш можно в любой момент.\n\n"
        f"💰 Баланс: <b>{fmt(db_balance(uid))}</b>\nВыберите ставку:"
    )
    await safe_edit(query, text, ikb(rows))

async def mines_start(update, context, bet):
    query = update.callback_query
    uid = query.from_user.id
    if bet not in BETS:
        await safe_answer(query)
        return
    if uid in mines_games:
        await resume_game(update, context, mines_games, render_mines)
        return
    if not db_debit(uid, bet):
        await safe_answer(query, "❌ Недостаточно средств", True)
        return
    game = {
        "bet": bet,
        "mines": set(rng.sample(range(MINES_TOTAL), MINES_COUNT)),
        "opened": set(),
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
    }
    mines_games[uid] = game
    await safe_answer(query)
    ok = await safe_edit(query, mines_text(game), mines_markup(game))
    if not ok and mines_games.get(uid) is game:
        mines_games.pop(uid, None)
        db_credit(uid, bet)

async def mines_open(update, context, idx):
    query = update.callback_query
    uid = query.from_user.id
    game = get_active_game(mines_games, query)
    if game is None:
        await safe_answer(query, "Эта игра уже завершена или устарела", True)
        await drop_stale_keyboard(query)
        return
    if not 0 <= idx < MINES_TOTAL or idx in game["opened"]:
        await safe_answer(query)
        return

    if idx in game["mines"]:
        mines_games.pop(uid, None)
        await safe_answer(query, "💥 Бум!")
        text = (
            f"💥 <b>Бум! Вы попали на мину.</b>\n\n"
            f"Ставка <b>{fmt(game['bet'])}</b> проиграна.\nОткрыто клеток: {len(game['opened'])}\n\n"
            f"💰 Баланс: <b>{fmt(db_balance(uid))}</b>"
        )
        await safe_edit(query, text, mines_markup(game, reveal=True, hit=idx))
        return

    game["opened"].add(idx)
    if len(game["opened"]) >= MINES_SAFE:
        mines_games.pop(uid, None)
        payout = mines_payout(game)
        db_credit(uid, payout)
        await safe_answer(query, "🏆 Все безопасные клетки открыты!")
        text = (
            f"🏆 <b>Невероятно! Открыты все безопасные клетки.</b>\n\n"
            f"Множитель: x{mines_tenths(game) / 10:.1f}\nВыплата: <b>{fmt(payout)}</b>\n\n"
            f"💰 Баланс: <b>{fmt(db_balance(uid))}</b>"
        )
        await safe_edit(query, text, mines_markup(game, reveal=True))
        return

    await safe_answer(query)
    await safe_edit(query, mines_text(game), mines_markup(game))

async def mines_cash(update, context):
    query = update.callback_query
    uid = query.from_user.id
    game = get_active_game(mines_games, query)
    if game is None:
        await safe_answer(query, "Эта игра уже завершена или устарела", True)
        await drop_stale_keyboard(query)
        return
    mines_games.pop(uid, None)
    payout = mines_payout(game)
    db_credit(uid, payout)
    await safe_answer(query, f"Забрано {fmt(payout)}")
    text = (
        f"💰 <b>Вы забрали выигрыш!</b>\n\n"
        f"Множитель: x{mines_tenths(game) / 10:.1f}\nВыплата: <b>{fmt(payout)}</b> "
        f"(чистый результат {fmt(payout - game['bet'])})\n\n"
        f"💰 Баланс: <b>{fmt(db_balance(uid))}</b>"
    )
    await safe_edit(query, text, mines_markup(game, reveal=True))

# ================================ БАШНЯ ================================

def tower_tenths(cleared):
    return 10 if cleared <= 0 else TOWER_MULTS[cleared - 1]

def tower_payout(game):
    return game["bet"] * tower_tenths(game["level"]) // 10

def tower_ladder(game, lost=False):
    cleared = game["level"]
    lines = []
    for lvl in range(TOWER_LEVELS, 0, -1):
        if lvl <= cleared:
            mark = "✅"
        elif lvl == cleared + 1:
            mark = "💥" if lost else "➡️"
        else:
            mark = "▫️"
        lines.append(f"{mark} Уровень {lvl} — x{TOWER_MULTS[lvl - 1] / 10:.1f}")
    return "\n".join(lines)

def tower_text(game):
    return (
        f"🏰 <b>Башня</b>\n"
        f"Ставка: <b>{fmt(game['bet'])}</b>\n\n"
        f"{tower_ladder(game)}\n\n"
        f"Уровень {game['level'] + 1} из {TOWER_LEVELS}: выберите дверь "
        f"(1 из {TOWER_DOORS} — мина)\n"
        f"Забрать сейчас: <b>{fmt(tower_payout(game))}</b>"
    )

def tower_markup(game, mode="play"):
    rows = []
    if mode == "play":
        rows.append([btn("🚪", f"t:o:{i}") for i in range(TOWER_DOORS)])
        rows.append([btn(f"💰 Забрать {fmt(tower_payout(game))}", "t:c")])
    else:
        if mode == "lost":
            mine = game["mines"][game["level"]]
            rows.append([btn("💥" if i == mine else "✅", "noop") for i in range(TOWER_DOORS)])
        rows.append([btn("🔁 Играть снова", "g:tower"), btn("⬅️ Назад", "play")])
    return ikb(rows)

def render_tower(game):
    return tower_text(game), tower_markup(game)

async def tower_entry(update, context):
    query = update.callback_query
    uid = query.from_user.id
    if uid in tower_games:
        await resume_game(update, context, tower_games, render_tower)
        return
    await safe_answer(query)
    rows = chunk([btn(fmt(b), f"t:s:{b}") for b in BETS], 3)
    rows.append([btn("⬅️ Назад", "play")])
    ladder = "\n".join(
        f"• Уровень {i + 1} — x{TOWER_MULTS[i] / 10:.1f}" for i in range(TOWER_LEVELS - 1, -1, -1)
    )
    text = (
        f"🏰 <b>Башня</b>\n\n{TOWER_LEVELS} уровней, на каждом {TOWER_DOORS} дверей, одна из них — мина. "
        f"Пройдите все уровни или заберите выигрыш раньше.\n\n{ladder}\n\n"
        f"💰 Баланс: <b>{fmt(db_balance(uid))}</b>\nВыберите ставку:"
    )
    await safe_edit(query, text, ikb(rows))

async def tower_start(update, context, bet):
    query = update.callback_query
    uid = query.from_user.id
    if bet not in BETS:
        await safe_answer(query)
        return
    if uid in tower_games:
        await resume_game(update, context, tower_games, render_tower)
        return
    if not db_debit(uid, bet):
        await safe_answer(query, "❌ Недостаточно средств", True)
        return
    game = {
        "bet": bet,
        "level": 0,
        "mines": [rng.randrange(TOWER_DOORS) for _ in range(TOWER_LEVELS)],
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
    }
    tower_games[uid] = game
    await safe_answer(query)
    ok = await safe_edit(query, tower_text(game), tower_markup(game))
    if not ok and tower_games.get(uid) is game:
        tower_games.pop(uid, None)
        db_credit(uid, bet)

async def tower_open(update, context, door):
    query = update.callback_query
    uid = query.from_user.id
    game = get_active_game(tower_games, query)
    if game is None:
        await safe_answer(query, "Эта игра уже завершена или устарела", True)
        await drop_stale_keyboard(query)
        return
    if not 0 <= door < TOWER_DOORS:
        await safe_answer(query)
        return

    level = game["level"]
    if door == game["mines"][level]:
        tower_games.pop(uid, None)
        await safe_answer(query, "💥 Мина!")
        text = (
            f"🏰 <b>Башня</b>\n\n{tower_ladder(game, lost=True)}\n\n"
            f"💥 <b>Вы попали на мину на уровне {level + 1}!</b>\n"
            f"Ставка <b>{fmt(game['bet'])}</b> проиграна.\n\n"
            f"💰 Баланс: <b>{fmt(db_balance(uid))}</b>"
        )
        await safe_edit(query, text, tower_markup(game, "lost"))
        return

    game["level"] += 1
    if game["level"] >= TOWER_LEVELS:
        tower_games.pop(uid, None)
        payout = tower_payout(game)
        db_credit(uid, payout)
        await safe_answer(query, "🏆 Башня пройдена!")
        text = (
            f"🏰 <b>Башня</b>\n\n{tower_ladder(game)}\n\n"
            f"🏆 <b>Вы прошли все уровни!</b>\nМножитель: x{tower_tenths(game['level']) / 10:.1f}\n"
            f"Выплата: <b>{fmt(payout)}</b>\n\n"
            f"💰 Баланс: <b>{fmt(db_balance(uid))}</b>"
        )
        await safe_edit(query, text, tower_markup(game, "end"))
        return

    await safe_answer(query, "✅ Безопасно!")
    await safe_edit(query, tower_text(game), tower_markup(game))

async def tower_cash(update, context):
    query = update.callback_query
    uid = query.from_user.id
    game = get_active_game(tower_games, query)
    if game is None:
        await safe_answer(query, "Эта игра уже завершена или устарела", True)
        await drop_stale_keyboard(query)
        return
    tower_games.pop(uid, None)
    payout = tower_payout(game)
    db_credit(uid, payout)
    await safe_answer(query, f"Забрано {fmt(payout)}")
    text = (
        f"🏰 <b>Башня</b>\n\n{tower_ladder(game)}\n\n"
        f"💰 <b>Вы забрали выигрыш!</b>\nМножитель: x{tower_tenths(game['level']) / 10:.1f}\n"
        f"Выплата: <b>{fmt(payout)}</b> (чистый результат {fmt(payout - game['bet'])})\n\n"
        f"💰 Баланс: <b>{fmt(db_balance(uid))}</b>"
    )
    await safe_edit(query, text, tower_markup(game, "end"))

# ================================ АДМИНКА ================================

def admin_home_text():
    return (
        f"🛠 <b>Админ-панель</b>\n\n"
        f"👥 Пользователей: <b>{db_count_users()}</b>\n"
        f"📢 Активных подписок: <b>{len(db_list_channels(only_enabled=True))}</b>"
    )

def admin_home_markup(uid):
    rows = [
        [btn("💵 Баланс игрока", "adm:bal")],
        [btn("📢 Подписки", "adm:subs")],
    ]
    if uid == MAIN_ADMIN_ID:
        rows.append([btn("👮 Админы", "adm:admins")])
    rows.append([btn("📣 Рассылка", "adm:bc")])
    rows.append([btn("❌ Закрыть", "adm:close")])
    return ikb(rows)

ADMIN_BACK = ikb([[btn("⬅️ Назад", "adm:home")]])

def subs_view():
    channels = db_list_channels()
    if not channels:
        text = "📢 <b>Обязательные подписки</b>\n\nСписок каналов пуст."
    else:
        lines = [
            f"{'✅' if ch['enabled'] else '❌'} {html.escape(ch['title'] or '—')} "
            f"(<code>{ch['chat_id']}</code>)"
            for ch in channels
        ]
        text = (
            "📢 <b>Обязательные подписки</b>\n\n" + "\n".join(lines) +
            "\n\nНажмите на название, чтобы включить/выключить. 🗑 — удалить.\n"
            "Бот должен быть администратором канала, иначе подписку проверить нельзя."
        )
    rows = []
    for ch in channels:
        rows.append([
            btn(f"{'✅' if ch['enabled'] else '❌'} {(ch['title'] or str(ch['chat_id']))[:28]}", f"adm:sub_t:{ch['id']}"),
            btn("🗑", f"adm:sub_d:{ch['id']}"),
        ])
    rows.append([btn("➕ Добавить канал", "adm:sub_add")])
    rows.append([btn("⬅️ Назад", "adm:home")])
    return text, ikb(rows)

def admins_view():
    admins = db_list_admins()
    lines = [f"⭐ <code>{MAIN_ADMIN_ID}</code> (главный)"] + [f"• <code>{a}</code>" for a in admins]
    text = (
        "👮 <b>Управление админами</b>\n\n" + "\n".join(lines) +
        "\n\nОтправьте команду:\n<code>add ID</code> — добавить админа\n<code>del ID</code> — удалить админа"
    )
    return text, ADMIN_BACK

async def cmd_admin(update, context):
    user = update.effective_user
    if user is None or not is_admin(user.id):
        return
    context.user_data.pop("admin_state", None)
    await update.message.reply_text(
        admin_home_text(), reply_markup=admin_home_markup(user.id), parse_mode=ParseMode.HTML
    )

async def admin_callback(update, context):
    query = update.callback_query
    user = query.from_user
    if not is_admin(user.id):
        await safe_answer(query, "⛔ Нет доступа", True)
        return

    parts = (query.data or "").split(":")
    cmd = parts[1] if len(parts) > 1 else ""
    ud = context.user_data

    if cmd == "home":
        ud.pop("admin_state", None)
        await safe_answer(query)
        await safe_edit(query, admin_home_text(), admin_home_markup(user.id))

    elif cmd == "close":
        ud.pop("admin_state", None)
        await safe_answer(query)
        try:
            await query.message.delete()
        except TelegramError:
            pass

    elif cmd == "bal":
        ud["admin_state"] = "balance"
        await safe_answer(query)
        await safe_edit(
            query,
            "💵 <b>Баланс игрока</b>\n\nОтправьте сообщение в формате:\n<code>ID сумма</code>\n\n"
            "• <code>123456789 5</code> — выдать 5$\n"
            "• <code>123456789 -2.5</code> — забрать 2.5$\n\n"
            "Можно отправлять несколько раз подряд. Для выхода нажмите «Назад».",
            ADMIN_BACK,
        )

    elif cmd == "subs":
        ud.pop("admin_state", None)
        await safe_answer(query)
        text, markup = subs_view()
        await safe_edit(query, text, markup)

    elif cmd == "sub_add":
        ud["admin_state"] = "add_channel"
        await safe_answer(query)
        await safe_edit(
            query,
            "➕ <b>Добавление канала</b>\n\nОтправьте <code>@username</code> или числовой ID канала "
            "(например <code>-1001234567890</code>).\nДля приватного канала можно добавить ссылку-приглашение "
            "через пробел:\n<code>-1001234567890 https://t.me/+AbCdEf</code>\n\n"
            "Бот должен быть администратором канала.",
            ikb([[btn("⬅️ Назад", "adm:subs")]]),
        )

    elif cmd in ("sub_t", "sub_d"):
        try:
            channel_id = int(parts[2])
        except (IndexError, ValueError):
            await safe_answer(query)
            return
        if cmd == "sub_t":
            db_toggle_channel(channel_id)
        else:
            db_delete_channel(channel_id)
        await safe_answer(query, "Готово")
        text, markup = subs_view()
        await safe_edit(query, text, markup)

    elif cmd == "admins":
        if user.id != MAIN_ADMIN_ID:
            await safe_answer(query, "⛔ Только главный админ может управлять админами", True)
            return
        ud["admin_state"] = "admins"
        await safe_answer(query)
        text, markup = admins_view()
        await safe_edit(query, text, markup)

    elif cmd == "bc":
        ud["admin_state"] = "broadcast"
        await safe_answer(query)
        await safe_edit(
            query,
            "📣 <b>Рассылка</b>\n\nОтправьте сообщение, которое нужно разослать всем пользователям бота. "
            "После отправки режим рассылки автоматически выключится.",
            ADMIN_BACK,
        )
    else:
        await safe_answer(query)

def normalize_chat_ref(ref):
    ref = ref.strip()
    m = re.fullmatch(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,})/?", ref)
    if m:
        return "@" + m.group(1)
    if re.fullmatch(r"@[A-Za-z0-9_]{4,}", ref):
        return ref
    if re.fullmatch(r"-?\d{5,}", ref):
        return int(ref)
    return None

async def run_broadcast(bot, admin_id, user_ids, from_chat_id, message_id):
    ok = fail = 0
    for uid in user_ids:
        for attempt in range(2):
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=from_chat_id, message_id=message_id)
                ok += 1
                break
            except TelegramError as e:
                retry_after = getattr(e, "retry_after", None)
                if retry_after is not None and attempt == 0:
                    await asyncio.sleep(float(retry_after) + 1)
                    continue
                fail += 1
                break
        await asyncio.sleep(0.06)
    try:
        await bot.send_message(admin_id, f"✅ Рассылка завершена.\nДоставлено: {ok}\nОшибок: {fail}")
    except TelegramError:
        pass

async def handle_admin_input(update, context, state, text):
    msg = update.message
    uid = update.effective_user.id
    ud = context.user_data

    if not is_admin(uid):
        ud.pop("admin_state", None)
        return

    if state == "balance":
        parts = text.split()
        if len(parts) != 2:
            await msg.reply_text("❗ Формат: ID сумма\nНапример: 123456789 5", reply_markup=ADMIN_BACK)
            return
        try:
            target = int(parts[0])
        except ValueError:
            await msg.reply_text("❗ ID должен быть числом.", reply_markup=ADMIN_BACK)
            return
        amount = parse_money(parts[1])
        if amount is None or amount == 0:
            await msg.reply_text("❗ Некорректная сумма. Пример: 5 или -2.5", reply_markup=ADMIN_BACK)
            return
        res = db_admin_change_balance(target, amount)
        if res is None:
            await msg.reply_text(
                "❗ Пользователь не найден (он должен хотя бы раз запустить бота).", reply_markup=ADMIN_BACK
            )
            return
        old, new = res
        await msg.reply_text(
            f"✅ Пользователь <code>{target}</code>\n{fmt(old)} → <b>{fmt(new)}</b> ({fmt(new - old)})",
            reply_markup=ADMIN_BACK,
            parse_mode=ParseMode.HTML,
        )
        try:
            sign = "начислил" if new >= old else "списал"
            await context.bot.send_message(
                target, f"💼 Администратор {sign} {fmt(abs(new - old))}.\n💰 Баланс: {fmt(new)}"
            )
        except TelegramError:
            pass

    elif state == "add_channel":
        parts = text.split(maxsplit=1)
        ref = normalize_chat_ref(parts[0]) if parts else None
        if ref is None:
            await msg.reply_text(
                "❗ Укажите @username или числовой ID канала.", reply_markup=ikb([[btn("⬅️ Назад", "adm:subs")]])
            )
            return
        try:
            chat = await context.bot.get_chat(ref)
        except TelegramError as e:
            await msg.reply_text(
                f"❌ Не удалось найти канал: {html.escape(str(e))}", reply_markup=ikb([[btn("⬅️ Назад", "adm:subs")]]),
                parse_mode=ParseMode.HTML,
            )
            return

        link = parts[1].strip() if len(parts) > 1 else None
        if link:
            if link.startswith("t.me/"):
                link = "https://" + link
            if not link.startswith(("http://", "https://")):
                link = None
        if not link and chat.username:
            link = f"https://t.me/{chat.username}"
        if not link:
            link = getattr(chat, "invite_link", None)
        if not link:
            try:
                link = await context.bot.export_chat_invite_link(chat.id)
            except TelegramError:
                link = None

        warn = ""
        try:
            me = await context.bot.get_chat_member(chat.id, context.bot.id)
            if me.status not in ("administrator", "creator"):
                warn = "\n⚠️ Бот не является администратором канала — подписки проверяться не будут."
        except TelegramError:
            warn = "\n⚠️ Бот не имеет доступа к списку участников — сделайте его администратором канала."
        if not link:
            warn += "\n⚠️ Ссылка на канал не определена — кнопка подписки не появится."

        title = chat.title or chat.username or str(chat.id)
        db_add_channel(chat.id, title, link)
        ud.pop("admin_state", None)
        view_text, view_markup = subs_view()
        await msg.reply_text(
            f"✅ Канал «{html.escape(title)}» добавлен.{warn}\n\n{view_text}",
            reply_markup=view_markup,
            parse_mode=ParseMode.HTML,
        )

    elif state == "admins":
        if uid != MAIN_ADMIN_ID:
            ud.pop("admin_state", None)
            await msg.reply_text("⛔ Только главный админ может управлять админами.")
            return
        m = re.fullmatch(r"(?i)(add|del)\s+(\d+)", text.strip())
        if not m:
            await msg.reply_text("❗ Формат: add ID или del ID", reply_markup=ADMIN_BACK)
            return
        action, target = m.group(1).lower(), int(m.group(2))
        if target == MAIN_ADMIN_ID:
            await msg.reply_text("ℹ️ Главного админа изменить нельзя.", reply_markup=ADMIN_BACK)
            return
        if action == "add":
            db_add_admin(target)
            result = f"✅ Админ <code>{target}</code> добавлен."
        else:
            result = (
                f"✅ Админ <code>{target}</code> удалён." if db_del_admin(target)
                else f"ℹ️ <code>{target}</code> не является админом."
            )
        view_text, view_markup = admins_view()
        await msg.reply_text(f"{result}\n\n{view_text}", reply_markup=view_markup, parse_mode=ParseMode.HTML)

    elif state == "broadcast":
        ud.pop("admin_state", None)
        user_ids = db_all_user_ids()
        await msg.reply_text(f"📣 Рассылка запущена. Получателей: {len(user_ids)}.")
        context.application.create_task(
            run_broadcast(context.bot, uid, user_ids, msg.chat_id, msg.message_id)
        )

# ============================ ОБРАБОТЧИКИ ============================

async def cmd_start(update, context):
    user = update.effective_user
    if user is None or update.message is None:
        return
    db_ensure_user(user.id, user.username)
    context.user_data.pop("admin_state", None)
    if not await ensure_subscribed(update, context):
        return
    await send_main_menu(
        context.bot,
        update.effective_chat.id,
        user.id,
        title=f"👋 Привет, <b>{html.escape(user.first_name or 'игрок')}</b>!",
    )

async def on_text(update, context):
    msg = update.message
    user = update.effective_user
    if msg is None or user is None:
        return
    text = (msg.text or "").strip()
    db_ensure_user(user.id, user.username)

    state = context.user_data.get("admin_state")
    if state and is_admin(user.id):
        if text in MENU_BUTTONS:
            context.user_data.pop("admin_state", None)
        else:
            await handle_admin_input(update, context, state, text)
            return

    if not await ensure_subscribed(update, context):
        return

    if text == BTN_BALANCE:
        await msg.reply_text(
            f"💰 Ваш баланс: <b>{fmt(db_balance(user.id))}</b>",
            reply_markup=main_keyboard(),
            parse_mode=ParseMode.HTML,
        )
    elif text == BTN_PLAY:
        await msg.reply_text(games_text(user.id), reply_markup=games_menu_markup(), parse_mode=ParseMode.HTML)
    elif text == BTN_MENU:
        await send_main_menu(context.bot, msg.chat_id, user.id)
    else:
        await send_main_menu(context.bot, msg.chat_id, user.id, title="Используйте кнопки меню ниже 👇")

async def on_callback(update, context):
    query = update.callback_query
    user = query.from_user
    data = query.data or ""
    db_ensure_user(user.id, user.username)

    if query.message is None:
        await safe_answer(query)
        return

    if data.startswith("adm:"):
        await admin_callback(update, context)
        return
    if data == "noop":
        await safe_answer(query)
        return
    if data == "chk":
        await cb_check_subscription(update, context)
        return

    if not data.startswith(NO_SUB_CHECK_PREFIXES):
        if not await ensure_subscribed(update, context):
            return

    parts = data.split(":")
    action = parts[0]
    try:
        if data == "play":
            await safe_answer(query)
            await safe_edit(query, games_text(user.id), games_menu_markup())

        elif data == "g:main":
            await safe_answer(query)
            try:
                await query.message.delete()
            except TelegramError:
                pass
            await send_main_menu(context.bot, query.message.chat_id, user.id)

        elif data == "g:tg":
            await safe_answer(query)
            await safe_edit(
                query,
                "🎲 <b>Телеграм игры</b>\n\nВыберите игру:",
                ikb([
                    [btn("🎲 Кубик", "g:dice"), btn("🎯 Дартс", "g:darts")],
                    [btn("⬅️ Назад", "play")],
                ]),
            )

        elif data == "g:dice":
            await tg_choose_bet(update, context, "d")
        elif data == "g:darts":
            await tg_choose_bet(update, context, "r")
        elif data == "g:crash":
            await crash_choose_mult(update, context)
        elif data == "g:mines":
            await mines_entry(update, context)
        elif data == "g:tower":
            await tower_entry(update, context)

        elif action in ("d", "r"):
            sub = parts[1]
            if sub == "b":
                await tg_choose_type(update, context, action, int(parts[2]))
            elif sub == "p":
                await tg_play(update, context, action, int(parts[2]), parts[3])
            else:
                await safe_answer(query)

        elif action == "c":
            sub = parts[1]
            if sub == "m":
                await crash_choose_bet(update, context, int(parts[2]))
            elif sub == "p":
                await crash_play(update, context, int(parts[2]), int(parts[3]))
            else:
                await safe_answer(query)

        elif action == "m":
            sub = parts[1]
            if sub == "s":
                await mines_start(update, context, int(parts[2]))
            elif sub == "o":
                await mines_open(update, context, int(parts[2]))
            elif sub == "c":
                await mines_cash(update, context)
            else:
                await safe_answer(query)

        elif action == "t":
            sub = parts[1]
            if sub == "s":
                await tower_start(update, context, int(parts[2]))
            elif sub == "o":
                await tower_open(update, context, int(parts[2]))
            elif sub == "c":
                await tower_cash(update, context)
            else:
                await safe_answer(query)

        else:
            await safe_answer(query)

    except (ValueError, IndexError):
        logger.warning("Некорректные callback-данные: %r", data)
        await safe_answer(query)

async def on_error(update, context):
    logger.error("Необработанная ошибка", exc_info=context.error)
    if isinstance(update, Update) and update.callback_query:
        await safe_answer(update.callback_query, "⚠️ Произошла ошибка, попробуйте ещё раз", True)

# ================================ FLASK ================================

flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    return "Bot is running", 200

@flask_app.route("/health")
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ================================= MAIN =================================

def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задана переменная окружения TELEGRAM_TOKEN")

    db_init()
    threading.Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

    private = filters.ChatType.PRIVATE
    application.add_handler(CommandHandler("start", cmd_start, filters=private))
    application.add_handler(CommandHandler("admin", cmd_admin, filters=private))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    application.add_error_handler(on_error)

    logger.info("Бот запущен")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
