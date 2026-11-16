# -*- coding: utf-8 -*-
"""
Telegram-бот с играми (python-telegram-bot 20.7, SQLite, Flask для Render).
Все суммы хранятся в центах (int). Пополнение/вывод — имитация через заявки.

requirements.txt:
    python-telegram-bot==20.7
    Flask==3.0.3

Переменные окружения: TELEGRAM_TOKEN (обязательно), PORT (Render ставит сам), DB_PATH (необязательно).
"""
import os
import re
import html
import time
import random
import sqlite3
import asyncio
import logging
import threading

from flask import Flask
from telegram import (
    Update,
    InlineKeyboardButton as IB,
    InlineKeyboardMarkup as IM,
    ReplyKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

# ───────────────────────── НАСТРОЙКИ ─────────────────────────
ADMIN_ID = 8192612489
TOKEN = os.environ.get("TELEGRAM_TOKEN")
DB_PATH = os.environ.get("DB_PATH", "casino.db")

START_BALANCE = 200      # 2$ в центах
FEE = 0.95               # комиссия казино 5%
RAKEBACK_PCT = 2         # 2% от ставки на рейкбек-счёт
MIN_BET = 10             # 0.10$
MIN_TX = 100             # минимум для пополнения/вывода 1$
HTML = ParseMode.HTML

# Ранги: (оборот в центах, название, эмодзи, бонус в центах)
RANKS = [
    (0,       "Новичок",   "🌱", 0),
    (1000,    "Игрок",     "🎮", 100),     # 10$  -> бонус 1$
    (5000,    "Хайроллер", "💎", 500),     # 50$  -> бонус 5$
    (20000,   "Магнат",    "👑", 1000),    # 200$ -> бонус 10$
    (100000,  "Легенда",   "🏆", 5000),    # 1000$ -> бонус 50$
]

GAMES = [
    ("dice", "🎲 Кубик"), ("darts", "🎯 Дартс"),
    ("football", "⚽ Футбол"), ("basket", "🏀 Баскетбол"),
    ("bowling", "🎳 Боулинг"), ("crash", "🎰 Краш"),
    ("mines", "💣 Мины"), ("tower", "🏰 Башня"),
]
GAME_TITLES = dict(GAMES)

# Игры на send_dice: emoji, число граней n, исходы {ключ: (название, множество значений)}
# Коэффициент исхода = n / количество значений в исходе (затем выплата * 0.95)
DICE = {
    "dice": {"emoji": "🎲", "n": 6, "opts": {
        "even": ("Чёт", {2, 4, 6}),
        "odd": ("Нечёт", {1, 3, 5}),
        "high": ("Больше (4-6)", {4, 5, 6}),
        "low": ("Меньше (1-3)", {1, 2, 3}),
        "center": ("Центр (3-4)", {3, 4}),
        "edge": ("Края (1,2,5,6)", {1, 2, 5, 6}),
    }},
    "darts": {"emoji": "🎯", "n": 6, "opts": {
        "red": ("Красный", {1, 3, 5}),
        "white": ("Белый", {2, 4}),
        "bounce": ("Отскок", {1}),
        "bull": ("Центр", {6}),
        "rc": ("Красный/Центр", {1, 3, 5, 6}),
        "wb": ("Белый/Отскок", {1, 2, 4}),
    }},
    "football": {"emoji": "⚽", "n": 5, "opts": {
        "goal": ("Гол", {3, 4, 5}),
        "miss": ("Мимо", {1, 2}),
        "center": ("Гол в центр", {3}),
    }},
    "basket": {"emoji": "🏀", "n": 5, "opts": {
        "goal": ("Гол", {4, 5}),
        "nogoal": ("Без гола", {1, 2, 3}),
        "bounce": ("Отскок", {1}),
        "stuck": ("Застрял", {2, 3}),
        "clean": ("Чистый гол", {5}),
    }},
    "bowling": {"emoji": "🎳", "n": 6, "opts": {
        "strike": ("Страйк", {6}),
        "miss": ("Мимо", {1}),
        "k1": ("Сбито 1/6", {2}),
        "k3": ("Сбито 3/6", {3}),
        "k4": ("Сбито 4/6", {4}),
        "k5": ("Сбито 5/6", {5}),
    }},
}

CRASH_MULTS = [2, 3, 4, 5, 10, 30, 50, 100]
TOWER_MULTS = [1.0, 1.2, 1.5, 1.9, 2.5, 3.5]
MINES_STATE = {}   # uid -> состояние игры «Мины» (в памяти)
TOWER_STATE = {}   # uid -> состояние игры «Башня» (в памяти)
BUSY = set()       # пользователи, у которых сейчас идёт анимация dice

MAIN_KB = ReplyKeyboardMarkup(
    [["💰 Баланс", "🎮 Играть", "📋 Меню", "👤 Профиль"]], resize_keyboard=True
)

# ───────────────────────── БАЗА ДАННЫХ ─────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row


def init_db():
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 0,
            rakeback INTEGER NOT NULL DEFAULT 0,
            turnover INTEGER NOT NULL DEFAULT 0,
            rank_claimed INTEGER NOT NULL DEFAULT 0,
            custom_bet INTEGER NOT NULL DEFAULT 0,
            created INTEGER
        );
        CREATE TABLE IF NOT EXISTS bets(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, game TEXT, bet INTEGER, payout INTEGER, detail TEXT, ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS deposits(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, amount INTEGER, status TEXT, ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS withdrawals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, amount INTEGER, status TEXT, ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS channels(chat TEXT PRIMARY KEY);
        """
    )
    db.commit()


def get_user(uid):
    row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO users(id, balance, created) VALUES(?,?,?)",
            (uid, START_BALANCE, int(time.time())),
        )
        db.commit()
        row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return row


def place_bet(uid, bet):
    """Списывает ставку, увеличивает оборот и рейкбек. False — если не хватает денег."""
    cur = db.execute(
        "UPDATE users SET balance=balance-?, turnover=turnover+?, rakeback=rakeback+? "
        "WHERE id=? AND balance>=?",
        (bet, bet, bet * RAKEBACK_PCT // 100, uid, bet),
    )
    db.commit()
    return cur.rowcount == 1


def settle(uid, game, bet, payout, detail=""):
    if payout > 0:
        db.execute("UPDATE users SET balance=balance+? WHERE id=?", (payout, uid))
    db.execute(
        "INSERT INTO bets(user_id, game, bet, payout, detail, ts) VALUES(?,?,?,?,?,?)",
        (uid, game, bet, payout, detail, int(time.time())),
    )
    db.commit()


def add_balance(uid, amount):
    db.execute("UPDATE users SET balance=balance+? WHERE id=?", (amount, uid))
    db.commit()


# ───────────────────────── УТИЛИТЫ ─────────────────────────
def m(cents):
    return f"${cents / 100:.2f}"


def fm(x):
    return f"{round(x, 2):g}"


def calc(bet, mult):
    return int(bet * mult * FEE)


def parse_amount(s):
    s = s.replace("$", "").replace(",", ".").strip()
    try:
        v = float(s)
    except ValueError:
        return None
    if v != v or v <= 0 or v > 1e7:
        return None
    return int(round(v * 100))


def rank_index(turnover):
    idx = 0
    for i, r in enumerate(RANKS):
        if turnover >= r[0]:
            idx = i
    return idx


def is_admin(uid):
    return uid == ADMIN_ID


async def respond(update, text, kb=None):
    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode=HTML)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
    else:
        await update.effective_message.reply_text(text, reply_markup=kb, parse_mode=HTML)


async def unsubscribed(ctx, uid):
    chats = [r["chat"] for r in db.execute("SELECT chat FROM channels").fetchall()]
    res = []
    for chat in chats:
        try:
            target = chat if chat.startswith("@") else int(chat)
            mem = await ctx.bot.get_chat_member(target, uid)
            if mem.status in ("left", "kicked"):
                res.append(chat)
        except Exception as e:  # бот не админ в канале и т.п. — не блокируем игрока
            log.warning("sub check %s: %s", chat, e)
    return res


async def need_sub(update, ctx, uid):
    miss = await unsubscribed(ctx, uid)
    if not miss:
        return False
    rows = []
    for ch in miss:
        if ch.startswith("@"):
            rows.append([IB(f"📢 {ch}", url=f"https://t.me/{ch[1:]}")])
    rows.append([IB("✅ Проверить подписку", callback_data="subs:check")])
    text = "📢 <b>Для игры подпишитесь на каналы:</b>\n" + "\n".join(f"• {html.escape(c)}" for c in miss)
    await respond(update, text, IM(rows))
    return True


# ───────────────────────── ГЛАВНОЕ МЕНЮ / ПРОФИЛЬ ─────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id)
    ctx.user_data.clear()
    await update.message.reply_text(
        "🎰 <b>Добро пожаловать в казино!</b>\n\n"
        f"💰 Ваш баланс: <b>{m(u['balance'])}</b>\n"
        "Выберите действие в меню ниже 👇",
        reply_markup=MAIN_KB, parse_mode=HTML,
    )


async def show_balance(update, ctx):
    u = get_user(update.effective_user.id)
    await respond(
        update,
        f"💰 <b>Баланс:</b> {m(u['balance'])}\n♻️ <b>Рейкбек:</b> {m(u['rakeback'])}",
    )


async def show_menu(update, ctx):
    kb = IM([
        [IB("🎮 Играть", callback_data="menu:play"), IB("👤 Профиль", callback_data="menu:profile")],
        [IB("💳 Пополнить", callback_data="dep:start"), IB("💸 Вывести", callback_data="wd:start")],
    ])
    await respond(
        update,
        "📋 <b>Меню</b>\n\n"
        "• Быстрая ставка: <code>ставка 1.5</code> — сохранить свою ставку\n"
        "• <code>вб</code> — поставить весь баланс\n"
        f"• Комиссия казино: <b>5%</b>, рейкбек: <b>{RAKEBACK_PCT}%</b> с каждой ставки",
        kb,
    )


async def show_profile(update, ctx):
    uid = update.effective_user.id
    u = get_user(uid)
    idx = rank_index(u["turnover"])
    _, name, emo, _ = RANKS[idx]
    if idx < len(RANKS) - 1:
        need = RANKS[idx + 1][0]
        base = RANKS[idx][0]
        pct = (u["turnover"] - base) / (need - base)
        filled = max(0, min(10, int(pct * 10)))
        bar = "▰" * filled + "▱" * (10 - filled)
        prog = (f"📈 До «{RANKS[idx + 1][2]} {RANKS[idx + 1][1]}»:\n"
                f"{bar} {int(pct * 100)}% (осталось {m(need - u['turnover'])})")
    else:
        prog = "📈 Достигнут максимальный ранг!"
    text = (
        "👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"💰 Баланс: <b>{m(u['balance'])}</b>\n"
        f"♻️ Рейкбек: <b>{m(u['rakeback'])}</b>\n"
        f"🎰 Общий оборот: <b>{m(u['turnover'])}</b>\n\n"
        f"🏅 Ранг: <b>{emo} {name}</b>\n{prog}"
    )
    rows = []
    if idx > u["rank_claimed"]:
        bonus = sum(RANKS[i][3] for i in range(u["rank_claimed"] + 1, idx + 1))
        rows.append([IB(f"🎁 Забрать бонус за ранг (+{m(bonus)})", callback_data="rank:claim")])
    if u["rakeback"] > 0:
        rows.append([IB(f"♻️ Забрать рейкбек ({m(u['rakeback'])})", callback_data="rb:claim")])
    rows.append([IB("💳 Пополнить", callback_data="dep:start"), IB("💸 Вывести", callback_data="wd:start")])
    await respond(update, text, IM(rows))


async def show_games(update, ctx):
    uid = update.effective_user.id
    ctx.user_data.pop("game", None)
    ctx.user_data.pop("await", None)
    if await need_sub(update, ctx, uid):
        return
    rows = []
    for i in range(0, len(GAMES), 2):
        rows.append([IB(t, callback_data=f"g:{k}") for k, t in GAMES[i:i + 2]])
    rows.append([IB("⬅️ Назад", callback_data="menu:back")])
    await respond(update, "🎮 <b>Выберите игру:</b>", IM(rows))


# ───────────────────────── ВЫБОР СТАВКИ ─────────────────────────
async def show_bet_menu(update, ctx, game):
    uid = update.effective_user.id
    u = get_user(uid)
    ctx.user_data["game"] = game
    cur = ctx.user_data.get("bet")
    text = (
        f"<b>{GAME_TITLES[game]}</b>\n\n"
        f"💰 Баланс: <b>{m(u['balance'])}</b>\n"
        + (f"💵 Текущая ставка: <b>{m(cur)}</b>\n" if cur else "")
        + "\nВыберите ставку или введите свою суммой:"
    )
    presets = [10, 50, 100, 200, 500, 1000]
    rows = [
        [IB(m(a), callback_data=f"b:{game}:{a}") for a in presets[:3]],
        [IB(m(a), callback_data=f"b:{game}:{a}") for a in presets[3:]],
        [IB("✍️ Ввести сумму", callback_data=f"b:{game}:in"), IB("💰 Вся сумма", callback_data=f"b:{game}:all")],
    ]
    if u["custom_bet"] > 0:
        rows.append([IB(f"⭐ Использовать свою ставку ({m(u['custom_bet'])})", callback_data=f"b:{game}:cu")])
    rows.append([IB("⬅️ Назад", callback_data="menu:play")])
    await respond(update, text, IM(rows))


async def next_step(update, ctx):
    """После выбора ставки — показать следующий шаг игры."""
    game = ctx.user_data.get("game")
    bet = ctx.user_data.get("bet")
    if not game or not bet:
        await show_games(update, ctx)
        return
    head = f"<b>{GAME_TITLES[game]}</b>\n💵 Ставка: <b>{m(bet)}</b>\n\n"
    chg = [IB("💵 Сменить ставку", callback_data=f"g:{game}"), IB("⬅️ В игры", callback_data="menu:play")]
    if game in DICE:
        cfg = DICE[game]
        items = list(cfg["opts"].items())
        rows = []
        for i in range(0, len(items), 2):
            rows.append([
                IB(f"{lab} · x{fm(cfg['n'] / len(v))}", callback_data=f"p:{game}:{k}")
                for k, (lab, v) in items[i:i + 2]
            ])
        rows.append(chg)
        await respond(update, head + "Выберите исход (комиссия 5% учтена в выплате):", IM(rows))
    elif game == "crash":
        rows = [[IB(f"x{x}", callback_data=f"cr:{x}") for x in CRASH_MULTS[i:i + 4]] for i in (0, 4)]
        rows.append(chg)
        await respond(update, head + "Выберите множитель. Шанс выигрыша = 1 / множитель.", IM(rows))
    elif game == "mines":
        nums = list(range(2, 13))
        rows = [[IB(f"💣 {n}", callback_data=f"mc:{n}") for n in nums[i:i + 4]] for i in range(0, 11, 4)]
        rows.append(chg)
        await respond(update, head + "Выберите количество мин (2–12):", IM(rows))
    elif game == "tower":
        rows = [[IB("🏰 Начать игру", callback_data="tw:start")], chg]
        lad = " → ".join(f"x{fm(x)}" for x in TOWER_MULTS)
        await respond(update, head + f"6 уровней по 5 дверей, за одной из них мина.\n{lad}", IM(rows))


# ───────────────────────── DICE-ИГРЫ ─────────────────────────
async def play_dice(update, ctx, game, key):
    q = update.callback_query
    uid = q.from_user.id
    bet = ctx.user_data.get("bet")
    cfg = DICE.get(game)
    if not cfg or key not in cfg["opts"]:
        return "Неизвестный исход"
    if not bet:
        return "Сначала выберите ставку"
    if uid in BUSY:
        return "Подождите окончания броска ⏳"
    label, vals = cfg["opts"][key]
    mult = cfg["n"] / len(vals)
    if not place_bet(uid, bet):
        return "❌ Недостаточно средств"
    BUSY.add(uid)
    try:
        try:
            await q.answer()
        except BadRequest:
            pass
        await q.edit_message_text(
            f"<b>{GAME_TITLES[game]}</b>\n💵 Ставка: <b>{m(bet)}</b>\n🎯 Исход: <b>{label}</b> (x{fm(mult)})\n\n⏳ Бросаем...",
            parse_mode=HTML,
        )
        chat_id = q.message.chat_id
        dmsg = await ctx.bot.send_dice(chat_id=chat_id, emoji=cfg["emoji"])
        value = dmsg.dice.value
        await asyncio.sleep(4)
        win = value in vals
        payout = calc(bet, mult) if win else 0
        settle(uid, game, bet, payout, f"{label}; выпало {value}")
        bal = get_user(uid)["balance"]
        if win:
            res = f"🎉 <b>ПОБЕДА!</b>\n💵 Выплата: <b>+{m(payout)}</b> (x{fm(mult)}, комиссия 5%)"
        else:
            res = f"😔 <b>Проигрыш</b>\n💸 −{m(bet)}"
        text = (
            f"<b>{GAME_TITLES[game]}</b>\n"
            f"🎯 Исход: <b>{label}</b>\n🎲 Выпало: <b>{value}</b>\n\n{res}\n\n💰 Баланс: <b>{m(bal)}</b>"
        )
        kb = IM([
            [IB(f"🔁 Повторить ({m(bet)})", callback_data=f"p:{game}:{key}")],
            [IB("🔀 Другой исход", callback_data=f"o:{game}"), IB("💵 Сменить ставку", callback_data=f"g:{game}")],
            [IB("⬅️ В игры", callback_data="menu:play")],
        ])
        await ctx.bot.send_message(chat_id, text, reply_markup=kb, parse_mode=HTML)
    finally:
        BUSY.discard(uid)
    return None


# ───────────────────────── КРАШ ─────────────────────────
async def play_crash(update, ctx, mult):
    uid = update.callback_query.from_user.id
    bet = ctx.user_data.get("bet")
    if mult not in CRASH_MULTS:
        return "Неверный множитель"
    if not bet:
        return "Сначала выберите ставку"
    if not place_bet(uid, bet):
        return "❌ Недостаточно средств"
    win = random.random() < 1.0 / mult
    payout = calc(bet, mult) if win else 0
    settle(uid, "crash", bet, payout, f"x{mult}")
    bal = get_user(uid)["balance"]
    if win:
        res = f"🚀 <b>Выиграл x{mult}!</b>\n💵 Выплата: <b>+{m(payout)}</b> (комиссия 5%)"
    else:
        res = f"💥 <b>Краш, проигрыш</b>\n💸 −{m(bet)}"
    text = f"<b>🎰 Краш</b>\n🎯 Множитель: <b>x{mult}</b>\n\n{res}\n\n💰 Баланс: <b>{m(bal)}</b>"
    kb = IM([
        [IB(f"🔁 Повторить x{mult} ({m(bet)})", callback_data=f"cr:{mult}")],
        [IB("🔀 Другой множитель", callback_data="o:crash"), IB("💵 Сменить ставку", callback_data="g:crash")],
        [IB("⬅️ В игры", callback_data="menu:play")],
    ])
    await respond(update, text, kb)
    return None


# ───────────────────────── МИНЫ ─────────────────────────
def mines_mult(mines, opened):
    mult = 1.0
    for i in range(opened):
        mult *= (25 - i) / (25 - mines - i)
    return mult


def mines_kb(st, reveal=False, hit=None, extra=None):
    rows = []
    for r in range(5):
        row = []
        for c in range(5):
            i = r * 5 + c
            if reveal:
                if i == hit:
                    t = "💥"
                elif i in st["mines"]:
                    t = "💣"
                elif i in st["opened"]:
                    t = "💎"
                else:
                    t = "▫️"
                row.append(IB(t, callback_data="noop"))
            elif i in st["opened"]:
                row.append(IB("💎", callback_data="noop"))
            else:
                row.append(IB("⬜", callback_data=f"mn:{i}"))
        rows.append(row)
    if not reveal:
        k = len(st["opened"])
        if k > 0:
            pay = calc(st["bet"], mines_mult(st["m"], k))
            rows.append([IB(f"💰 Забрать {m(pay)}", callback_data="mn:cash")])
        else:
            rows.append([IB("Откройте клетку 👆", callback_data="noop")])
    if extra:
        rows.extend(extra)
    return IM(rows)


def mines_text(st):
    k = len(st["opened"])
    cur = mines_mult(st["m"], k)
    nxt = mines_mult(st["m"], k + 1) if k < 25 - st["m"] else cur
    return (
        "<b>💣 Мины</b>\n"
        f"💵 Ставка: <b>{m(st['bet'])}</b> · 💣 Мин: <b>{st['m']}</b>\n"
        f"💎 Открыто: <b>{k}</b> / {25 - st['m']}\n"
        f"📈 Множитель: <b>x{cur:.2f}</b> · следующий: x{nxt:.2f}\n"
        f"💰 Выплата сейчас: <b>{m(calc(st['bet'], cur)) if k else m(0)}</b>"
    )


AFTER_MINES = [[IB("🔁 Играть снова", callback_data="g:mines"), IB("⬅️ В игры", callback_data="menu:play")]]


async def mines_start(update, ctx, n):
    uid = update.callback_query.from_user.id
    if uid in MINES_STATE:
        st = MINES_STATE[uid]
        await respond(update, mines_text(st), mines_kb(st))
        return "У вас уже идёт игра"
    bet = ctx.user_data.get("bet")
    if not bet:
        return "Сначала выберите ставку"
    if not 2 <= n <= 12:
        return "Неверное число мин"
    if not place_bet(uid, bet):
        return "❌ Недостаточно средств"
    st = {"bet": bet, "m": n, "mines": set(random.sample(range(25), n)), "opened": set()}
    MINES_STATE[uid] = st
    await respond(update, mines_text(st), mines_kb(st))
    return None


async def mines_click(update, ctx, arg):
    uid = update.callback_query.from_user.id
    st = MINES_STATE.get(uid)
    if not st:
        return "Игра не найдена"
    if arg == "cash":
        k = len(st["opened"])
        if k == 0:
            return "Сначала откройте клетку"
        del MINES_STATE[uid]
        return await mines_finish_win(update, st)
    try:
        i = int(arg)
    except ValueError:
        return "Ошибка"
    if not 0 <= i < 25 or i in st["opened"]:
        return None
    if i in st["mines"]:
        del MINES_STATE[uid]
        settle(uid, "mines", st["bet"], 0, f"{st['m']} мин, открыто {len(st['opened'])}")
        bal = get_user(uid)["balance"]
        text = (f"<b>💣 Мины</b>\n\n💥 <b>БУМ! Вы подорвались.</b>\n💸 −{m(st['bet'])}\n"
                f"💰 Баланс: <b>{m(bal)}</b>")
        await respond(update, text, mines_kb(st, reveal=True, hit=i, extra=AFTER_MINES))
        return None
    st["opened"].add(i)
    if len(st["opened"]) == 25 - st["m"]:
        del MINES_STATE[uid]
        return await mines_finish_win(update, st)
    await respond(update, mines_text(st), mines_kb(st))
    return None


async def mines_finish_win(update, st):
    uid = update.callback_query.from_user.id
    k = len(st["opened"])
    mult = mines_mult(st["m"], k)
    payout = calc(st["bet"], mult)
    settle(uid, "mines", st["bet"], payout, f"{st['m']} мин, открыто {k}, x{mult:.2f}")
    bal = get_user(uid)["balance"]
    text = (f"<b>💣 Мины</b>\n\n🎉 <b>Вы забрали выигрыш!</b>\n📈 x{mult:.2f}\n"
            f"💵 Выплата: <b>+{m(payout)}</b> (комиссия 5%)\n💰 Баланс: <b>{m(bal)}</b>")
    await respond(update, text, mines_kb(st, reveal=True, extra=AFTER_MINES))
    return None


# ───────────────────────── БАШНЯ ─────────────────────────
AFTER_TOWER = [[IB("🔁 Играть снова", callback_data="g:tower"), IB("⬅️ В игры", callback_data="menu:play")]]


def tower_text(st):
    lines = []
    for lvl in range(5, -1, -1):
        mark = "✅" if lvl < st["level"] else ("▶️" if lvl == st["level"] else "🔒")
        lines.append(f"{mark} Уровень {lvl + 1} — x{fm(TOWER_MULTS[lvl])}")
    cash = calc(st["bet"], TOWER_MULTS[st["level"] - 1]) if st["level"] > 0 else 0
    return (
        "<b>🏰 Башня</b>\n"
        f"💵 Ставка: <b>{m(st['bet'])}</b>\n\n" + "\n".join(lines) +
        f"\n\n💰 Можно забрать: <b>{m(cash)}</b>"
    )


def tower_kb(st, reveal=False, extra=None):
    rows = []
    for lvl in range(5, -1, -1):
        row = []
        for d in range(5):
            if lvl < st["level"]:
                row.append(IB("✅" if st["picks"][lvl] == d else "⬛", callback_data="noop"))
            elif lvl == st["level"] and not reveal:
                row.append(IB("🚪", callback_data=f"tw:{d}"))
            elif lvl == st["level"] and reveal:
                row.append(IB("💥" if st["bombs"][lvl] == d else "⬛", callback_data="noop"))
            else:
                row.append(IB("▫️", callback_data="noop"))
        rows.append(row)
    if not reveal and st["level"] > 0:
        pay = calc(st["bet"], TOWER_MULTS[st["level"] - 1])
        rows.append([IB(f"💰 Забрать {m(pay)}", callback_data="tw:cash")])
    if extra:
        rows.extend(extra)
    return IM(rows)


async def tower_click(update, ctx, arg):
    uid = update.callback_query.from_user.id
    if arg == "start":
        if uid in TOWER_STATE:
            st = TOWER_STATE[uid]
            await respond(update, tower_text(st), tower_kb(st))
            return "У вас уже идёт игра"
        bet = ctx.user_data.get("bet")
        if not bet:
            return "Сначала выберите ставку"
        if not place_bet(uid, bet):
            return "❌ Недостаточно средств"
        st = {"bet": bet, "level": 0, "bombs": [random.randrange(5) for _ in range(6)], "picks": []}
        TOWER_STATE[uid] = st
        await respond(update, tower_text(st), tower_kb(st))
        return None
    st = TOWER_STATE.get(uid)
    if not st:
        return "Игра не найдена"
    if arg == "cash":
        if st["level"] == 0:
            return "Сначала пройдите уровень"
        del TOWER_STATE[uid]
        return await tower_win(update, st)
    try:
        d = int(arg)
    except ValueError:
        return "Ошибка"
    if not 0 <= d < 5:
        return None
    lvl = st["level"]
    st["picks"].append(d)
    if st["bombs"][lvl] == d:
        del TOWER_STATE[uid]
        settle(uid, "tower", st["bet"], 0, f"упал на уровне {lvl + 1}")
        bal = get_user(uid)["balance"]
        text = (f"<b>🏰 Башня</b>\n\n💥 <b>Мина на уровне {lvl + 1}!</b>\n💸 −{m(st['bet'])}\n"
                f"💰 Баланс: <b>{m(bal)}</b>")
        await respond(update, text, tower_kb(st, reveal=True, extra=AFTER_TOWER))
        return None
    st["level"] += 1
    if st["level"] == 6:
        del TOWER_STATE[uid]
        return await tower_win(update, st)
    await respond(update, tower_text(st), tower_kb(st))
    return None


async def tower_win(update, st):
    uid = update.callback_query.from_user.id
    mult = TOWER_MULTS[st["level"] - 1]
    payout = calc(st["bet"], mult)
    settle(uid, "tower", st["bet"], payout, f"уровней {st['level']}, x{fm(mult)}")
    bal = get_user(uid)["balance"]
    text = (f"<b>🏰 Башня</b>\n\n🎉 <b>Вы забрали выигрыш!</b>\n📈 x{fm(mult)}\n"
            f"💵 Выплата: <b>+{m(payout)}</b> (комиссия 5%)\n💰 Баланс: <b>{m(bal)}</b>")
    await respond(update, text, tower_kb(st, reveal=True, extra=AFTER_TOWER) if st["level"] < 6
                  else IM(AFTER_TOWER))
    return None


# ───────────────────────── ПОПОЛНЕНИЕ / ВЫВОД ─────────────────────────
async def create_deposit(update, ctx, amount):
    uid = update.effective_user.id
    cur = db.execute("INSERT INTO deposits(user_id, amount, status, ts) VALUES(?,?,?,?)",
                     (uid, amount, "pending", int(time.time())))
    db.commit()
    await update.message.reply_text(
        f"💳 <b>Заявка на пополнение #{cur.lastrowid}</b>\nСумма: <b>{m(amount)}</b>\n"
        "Статус: ⏳ ожидает подтверждения администратором.",
        parse_mode=HTML,
    )
    try:
        await ctx.bot.send_message(ADMIN_ID, f"🔔 Новая заявка на пополнение #{cur.lastrowid}: {m(amount)} от <code>{uid}</code>. /admin",
                                   parse_mode=HTML)
    except Exception:
        pass


async def create_withdraw(update, ctx, amount):
    uid = update.effective_user.id
    c = db.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", (amount, uid, amount))
    db.commit()
    if c.rowcount != 1:
        await update.message.reply_text("❌ Недостаточно средств на балансе.")
        return
    cur = db.execute("INSERT INTO withdrawals(user_id, amount, status, ts) VALUES(?,?,?,?)",
                     (uid, amount, "pending", int(time.time())))
    db.commit()
    await update.message.reply_text(
        f"💸 <b>Заявка на вывод #{cur.lastrowid}</b>\nСумма: <b>{m(amount)}</b> (списана с баланса)\n"
        "Статус: ⏳ ожидает решения администратора.",
        parse_mode=HTML,
    )
    try:
        await ctx.bot.send_message(ADMIN_ID, f"🔔 Новая заявка на вывод #{cur.lastrowid}: {m(amount)} от <code>{uid}</code>. /admin",
                                   parse_mode=HTML)
    except Exception:
        pass


# ───────────────────────── АДМИНКА ─────────────────────────
ADMIN_KB = IM([
    [IB("📊 Статистика", callback_data="adm:stats"), IB("👤 Игрок по ID", callback_data="adm:user")],
    [IB("💸 Заявки на вывод", callback_data="adm:wd"), IB("💳 Заявки на пополнение", callback_data="adm:dep")],
    [IB("📢 Настройки подписок", callback_data="adm:subs"), IB("✉️ Рассылка", callback_data="adm:bc")],
])


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    ctx.user_data.pop("await", None)
    await update.message.reply_text("🛠 <b>Админ-панель</b>", reply_markup=ADMIN_KB, parse_mode=HTML)


def user_info_text(uid):
    u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        return None
    idx = rank_index(u["turnover"])
    out = [
        f"👤 <b>Игрок</b> <code>{uid}</code>",
        f"💰 Баланс: {m(u['balance'])} · ♻️ Рейкбек: {m(u['rakeback'])}",
        f"🎰 Оборот: {m(u['turnover'])} · 🏅 {RANKS[idx][2]} {RANKS[idx][1]}",
        "",
        "<b>Последние ставки:</b>",
    ]
    bets = db.execute("SELECT * FROM bets WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
    out += [f"#{b['id']} {GAME_TITLES.get(b['game'], b['game'])}: {m(b['bet'])} → {m(b['payout'])}" for b in bets] or ["—"]
    out.append("\n<b>Пополнения:</b>")
    ds = db.execute("SELECT * FROM deposits WHERE user_id=? ORDER BY id DESC LIMIT 5", (uid,)).fetchall()
    out += [f"#{d['id']} {m(d['amount'])} — {d['status']}" for d in ds] or ["—"]
    out.append("\n<b>Выводы:</b>")
    ws = db.execute("SELECT * FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 5", (uid,)).fetchall()
    out += [f"#{w['id']} {m(w['amount'])} — {w['status']}" for w in ws] or ["—"]
    return "\n".join(out)


async def admin_list(update, ctx, kind):
    table = "withdrawals" if kind == "wd" else "deposits"
    rows = db.execute(f"SELECT * FROM {table} WHERE status='pending' ORDER BY id LIMIT 10").fetchall()
    q = update.callback_query
    if not rows:
        await q.message.reply_text("Активных заявок нет ✅")
        return
    for r in rows:
        if kind == "wd":
            kb = IM([[IB("✅ Одобрить", callback_data=f"adm:wok:{r['id']}"),
                      IB("❌ Отклонить", callback_data=f"adm:wno:{r['id']}")]])
            title = "💸 Вывод"
        else:
            kb = IM([[IB("✅ Подтвердить пополнение", callback_data=f"adm:dok:{r['id']}"),
                      IB("❌ Отклонить", callback_data=f"adm:dno:{r['id']}")]])
            title = "💳 Пополнение"
        await q.message.reply_text(
            f"{title} <b>#{r['id']}</b>\nИгрок: <code>{r['user_id']}</code>\nСумма: <b>{m(r['amount'])}</b>",
            reply_markup=kb, parse_mode=HTML,
        )


async def admin_process(update, ctx, action, rid):
    q = update.callback_query
    table = "withdrawals" if action.startswith("w") else "deposits"
    r = db.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()
    if not r or r["status"] != "pending":
        return "Заявка уже обработана"
    uid, amount = r["user_id"], r["amount"]
    if action == "wok":
        new, note = "approved", f"✅ Ваш вывод #{rid} на {m(amount)} одобрен."
    elif action == "wno":
        new, note = "rejected", f"❌ Вывод #{rid} отклонён, {m(amount)} возвращены на баланс."
        add_balance(uid, amount)
    elif action == "dok":
        new, note = "approved", f"✅ Пополнение #{rid} на {m(amount)} зачислено!"
        add_balance(uid, amount)
    else:  # dno
        new, note = "rejected", f"❌ Заявка на пополнение #{rid} отклонена."
    db.execute(f"UPDATE {table} SET status=? WHERE id=?", (new, rid))
    db.commit()
    try:
        await q.edit_message_text(f"{q.message.text}\n\n➡️ Статус: {new}")
    except BadRequest:
        pass
    try:
        await ctx.bot.send_message(uid, note)
    except Exception:
        pass
    return None


async def show_subs_admin(update, ctx):
    chats = [r["chat"] for r in db.execute("SELECT chat FROM channels").fetchall()]
    rows = [[IB(f"🗑 {c}", callback_data=f"adm:delch:{c}")] for c in chats]
    rows.append([IB("➕ Добавить канал", callback_data="adm:addch")])
    text = "📢 <b>Обязательные подписки</b>\n" + ("\n".join(f"• {html.escape(c)}" for c in chats) if chats else "Каналов нет.")
    text += "\n\nБот должен быть админом в канале для проверки подписки."
    await respond(update, text, IM(rows))


async def cb_admin(update, ctx, parts):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        return "Нет доступа"
    act = parts[1]
    if act == "stats":
        r = db.execute("SELECT COUNT(*) c, COALESCE(SUM(balance),0) b, COALESCE(SUM(turnover),0) t FROM users").fetchone()
        w = db.execute("SELECT COUNT(*) c FROM withdrawals WHERE status='pending'").fetchone()["c"]
        d = db.execute("SELECT COUNT(*) c FROM deposits WHERE status='pending'").fetchone()["c"]
        await q.message.reply_text(
            "📊 <b>Статистика</b>\n"
            f"👥 Игроков: <b>{r['c']}</b>\n💰 Суммарный баланс: <b>{m(r['b'])}</b>\n"
            f"🎰 Суммарный оборот: <b>{m(r['t'])}</b>\n"
            f"💸 Активных заявок на вывод: <b>{w}</b>\n💳 Активных заявок на пополнение: <b>{d}</b>",
            parse_mode=HTML,
        )
    elif act == "user":
        ctx.user_data["await"] = "adm_user"
        await q.message.reply_text("Введите ID игрока:")
    elif act in ("wd", "dep"):
        await admin_list(update, ctx, act)
    elif act in ("wok", "wno", "dok", "dno"):
        return await admin_process(update, ctx, act, int(parts[2]))
    elif act == "subs":
        await show_subs_admin(update, ctx)
    elif act == "addch":
        ctx.user_data["await"] = "adm_addch"
        await q.message.reply_text("Отправьте @username канала (или числовой ID, например -1001234567890):")
    elif act == "delch":
        db.execute("DELETE FROM channels WHERE chat=?", (":".join(parts[2:]),))
        db.commit()
        await show_subs_admin(update, ctx)
    elif act == "bc":
        ctx.user_data["await"] = "adm_bc"
        await q.message.reply_text("Отправьте текст рассылки для всех пользователей:")
    return None


# ───────────────────────── ОБРАБОТКА CALLBACK ─────────────────────────
async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    get_user(uid)
    parts = (q.data or "").split(":")
    t = parts[0]
    res = None
    try:
        if t == "noop":
            pass
        elif t == "menu":
            sub = parts[1]
            if sub == "play":
                await show_games(update, ctx)
            elif sub == "back":
                ctx.user_data.pop("game", None)
                await respond(update, "📋 <b>Главное меню</b> — используйте кнопки внизу 👇")
            elif sub == "profile":
                await show_profile(update, ctx)
        elif t == "subs":
            if not await need_sub(update, ctx, uid):
                await show_games(update, ctx)
        elif t == "g":                      # выбор игры
            game = parts[1]
            if game not in GAME_TITLES:
                res = "Неизвестная игра"
            elif await need_sub(update, ctx, uid):
                pass
            elif game == "mines" and uid in MINES_STATE:
                await respond(update, mines_text(MINES_STATE[uid]), mines_kb(MINES_STATE[uid]))
            elif game == "tower" and uid in TOWER_STATE:
                await respond(update, tower_text(TOWER_STATE[uid]), tower_kb(TOWER_STATE[uid]))
            else:
                ctx.user_data.pop("await", None)
                await show_bet_menu(update, ctx, game)
        elif t == "b":                      # выбор ставки
            game, val = parts[1], parts[2]
            u = get_user(uid)
            ctx.user_data["game"] = game
            if val == "in":
                ctx.user_data["await"] = "bet"
                await respond(update, f"<b>{GAME_TITLES[game]}</b>\n\n✍️ Отправьте сумму ставки в долларах (например <code>1.5</code>):",
                              IM([[IB("⬅️ Назад", callback_data=f"g:{game}")]]))
            else:
                if val == "all":
                    amt = u["balance"]
                elif val == "cu":
                    amt = u["custom_bet"]
                else:
                    amt = int(val)
                if amt < MIN_BET:
                    res = f"Минимальная ставка {m(MIN_BET)}"
                elif amt > u["balance"]:
                    res = "❌ Недостаточно средств"
                else:
                    ctx.user_data["bet"] = amt
                    ctx.user_data.pop("await", None)
                    await next_step(update, ctx)
        elif t == "o":                      # вернуться к выбору исхода
            ctx.user_data["game"] = parts[1]
            if not ctx.user_data.get("bet"):
                await show_bet_menu(update, ctx, parts[1])
            else:
                await next_step(update, ctx)
        elif t == "p":
            res = await play_dice(update, ctx, parts[1], parts[2])
        elif t == "cr":
            res = await play_crash(update, ctx, int(parts[1]))
        elif t == "mc":
            res = await mines_start(update, ctx, int(parts[1]))
        elif t == "mn":
            res = await mines_click(update, ctx, parts[1])
        elif t == "tw":
            res = await tower_click(update, ctx, parts[1])
        elif t == "rank":
            u = get_user(uid)
            idx = rank_index(u["turnover"])
            if idx <= u["rank_claimed"]:
                res = "Нет доступных бонусов"
            else:
                bonus = sum(RANKS[i][3] for i in range(u["rank_claimed"] + 1, idx + 1))
                db.execute("UPDATE users SET balance=balance+?, rank_claimed=? WHERE id=?", (bonus, idx, uid))
                db.commit()
                res = f"🎁 Бонус {m(bonus)} зачислен!"
                await show_profile(update, ctx)
        elif t == "rb":
            u = get_user(uid)
            if u["rakeback"] <= 0:
                res = "Рейкбек пуст"
            else:
                amt = u["rakeback"]
                db.execute("UPDATE users SET balance=balance+?, rakeback=0 WHERE id=?", (amt, uid))
                db.commit()
                res = f"♻️ Рейкбек {m(amt)} зачислен на баланс!"
                await show_profile(update, ctx)
        elif t == "dep":
            ctx.user_data["await"] = "dep"
            await q.message.reply_text(f"💳 Введите сумму пополнения в долларах (мин. {m(MIN_TX)}):")
        elif t == "wd":
            ctx.user_data["await"] = "wd"
            await q.message.reply_text(f"💸 Введите сумму вывода в долларах (мин. {m(MIN_TX)}):")
        elif t == "adm":
            res = await cb_admin(update, ctx, parts)
    except Exception:
        log.exception("callback error: %s", q.data)
        res = "Произошла ошибка, попробуйте ещё раз"
    try:
        await q.answer(res, show_alert=bool(res))
    except BadRequest:
        pass


# ───────────────────────── ОБРАБОТКА ТЕКСТА ─────────────────────────
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    low = text.lower()
    u = get_user(uid)

    # кнопки главного меню
    if text in ("💰 Баланс", "🎮 Играть", "📋 Меню", "👤 Профиль"):
        ctx.user_data.pop("await", None)
        if text == "💰 Баланс":
            await show_balance(update, ctx)
        elif text == "🎮 Играть":
            await show_games(update, ctx)
        elif text == "📋 Меню":
            await show_menu(update, ctx)
        else:
            await show_profile(update, ctx)
        return

    aw = ctx.user_data.get("await")

    # ввод админа
    if aw and aw.startswith("adm_"):
        if not is_admin(uid):
            ctx.user_data.pop("await", None)
            return
        ctx.user_data.pop("await", None)
        if aw == "adm_user":
            try:
                info = user_info_text(int(text))
            except ValueError:
                info = None
            await update.message.reply_text(info or "Игрок не найден", parse_mode=HTML)
        elif aw == "adm_addch":
            chat = text.strip()
            if not (chat.startswith("@") or re.fullmatch(r"-?\d+", chat)):
                await update.message.reply_text("Нужен @username или числовой ID.")
                return
            db.execute("INSERT OR IGNORE INTO channels(chat) VALUES(?)", (chat,))
            db.commit()
            await update.message.reply_text(f"✅ Канал {chat} добавлен.")
        elif aw == "adm_bc":
            ids = [r["id"] for r in db.execute("SELECT id FROM users").fetchall()]
            ok = 0
            await update.message.reply_text(f"📤 Рассылка на {len(ids)} пользователей...")
            for i in ids:
                try:
                    await ctx.bot.send_message(i, text)
                    ok += 1
                except Exception:
                    pass
                await asyncio.sleep(0.05)
            await update.message.reply_text(f"✅ Доставлено: {ok} из {len(ids)}")
        return

    # ввод суммы ставки / пополнения / вывода
    if aw in ("bet", "dep", "wd"):
        amt = parse_amount(text)
        if amt is None:
            await update.message.reply_text("Введите число, например <code>1.5</code>", parse_mode=HTML)
            return
        if aw == "bet":
            if amt < MIN_BET:
                await update.message.reply_text(f"Минимальная ставка {m(MIN_BET)}")
            elif amt > u["balance"]:
                await update.message.reply_text(f"❌ Недостаточно средств. Баланс: {m(u['balance'])}")
            else:
                ctx.user_data["bet"] = amt
                ctx.user_data.pop("await", None)
                await next_step(update, ctx)
            return
        if amt < MIN_TX:
            await update.message.reply_text(f"Минимальная сумма {m(MIN_TX)}")
            return
        ctx.user_data.pop("await", None)
        if aw == "dep":
            await create_deposit(update, ctx, amt)
        else:
            await create_withdraw(update, ctx, amt)
        return

    # быстрые ставки
    mt = re.fullmatch(r"ставка\s+([\d.,]+)\$?", low)
    if mt:
        amt = parse_amount(mt.group(1))
        if amt is None or amt < MIN_BET:
            await update.message.reply_text(f"Минимальная ставка {m(MIN_BET)}")
            return
        db.execute("UPDATE users SET custom_bet=? WHERE id=?", (amt, uid))
        db.commit()
        ctx.user_data["bet"] = amt
        await update.message.reply_text(f"⭐ Своя ставка сохранена: <b>{m(amt)}</b>", parse_mode=HTML)
        await show_games(update, ctx)
        return
    if low == "вб":
        if u["balance"] < MIN_BET:
            await update.message.reply_text("❌ Недостаточно средств.")
            return
        ctx.user_data["bet"] = u["balance"]
        game = ctx.user_data.get("game")
        await update.message.reply_text(f"💰 Ставка: весь баланс — <b>{m(u['balance'])}</b>", parse_mode=HTML)
        if game:
            await next_step(update, ctx)
        else:
            await show_games(update, ctx)
        return


async def on_error(update, ctx):
    log.error("Ошибка: %s", ctx.error, exc_info=ctx.error)


# ───────────────────────── FLASK (для Render) ─────────────────────────
flask_app = Flask(__name__)


@flask_app.route("/")
def index():
    return "Bot is running"


@flask_app.route("/health")
def health():
    return "ok"


def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), use_reloader=False)


# ───────────────────────── ЗАПУСК ─────────────────────────
def main():
    if not TOKEN:
        raise SystemExit("Не задана переменная окружения TELEGRAM_TOKEN")
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    log.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
