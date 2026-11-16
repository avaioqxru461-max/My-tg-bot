# -*- coding: utf-8 -*-
"""
Telegram-бот с играми (python-telegram-bot 20.7, SQLite, Flask для Render).
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
import requests

from flask import Flask
from telegram import (
    Update,
    InlineKeyboardButton as IB,
    InlineKeyboardMarkup as IM,
    ReplyKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
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
XROCKET_TOKEN = os.environ.get("XROCKET_TOKEN")
DB_PATH = os.environ.get("DB_PATH", "casino.db")

START_BALANCE = 200
FEE = 0.95
RAKEBACK_PCT = 2
MIN_BET = 10
MIN_TX = 10
HTML = ParseMode.HTML

RANKS = [
    (0, "Новичок", "🌱", 0), (1000, "Игрок", "🎮", 100),
    (5000, "Хайроллер", "💎", 500), (20000, "Магнат", "👑", 1000),
    (100000, "Легенда", "🏆", 5000),
]

GAMES = [("dice", "🎲 Кубик"), ("darts", "🎯 Дартс"), ("football", "⚽ Футбол"), ("basket", "🏀 Баскетбол"), ("bowling", "🎳 Боулинг"), ("crash", "🎰 Краш"), ("mines", "💣 Мины"), ("tower", "🏰 Башня")]
GAME_TITLES = dict(GAMES)

DICE = {
    "dice": {"emoji": "🎲", "n": 6, "opts": {
        "even": ("Чёт", {2, 4, 6}), "odd": ("Нечёт", {1, 3, 5}),
        "high": ("Больше (4-6)", {4, 5, 6}), "low": ("Меньше (1-3)", {1, 2, 3}),
        "center": ("Центр (3-4)", {3, 4}), "edge": ("Края (1,2,5,6)", {1, 2, 5, 6}),
    }},
    "darts": {"emoji": "🎯", "n": 6, "opts": {
        "red": ("Красный", {1, 3, 5}), "white": ("Белый", {2, 4}),
        "bounce": ("Отскок", {1}), "bull": ("Центр", {6}),
        "rc": ("Красный/Центр", {1, 3, 5, 6}), "wb": ("Белый/Отскок", {1, 2, 4}),
    }},
    "football": {"emoji": "⚽", "n": 5, "opts": {
        "goal": ("Гол", {3, 4, 5}), "miss": ("Мимо", {1, 2}), "center": ("Гол в центр", {3}),
    }},
    "basket": {"emoji": "🏀", "n": 5, "opts": {
        "goal": ("Гол", {4, 5}), "nogoal": ("Без гола", {1, 2, 3}),
        "bounce": ("Отскок", {1}), "stuck": ("Застрял", {2, 3}), "spin": ("Прокрут", {1, 2}),
    }},
    "bowling": {"emoji": "🎳", "n": 6, "opts": {
        "strike": ("Страйк", {6}), "miss": ("Мимо", {1}),
        "k1": ("Сбито 1/6", {2}), "k3": ("Сбито 3/6", {3}), "k4": ("Сбито 4/6", {4}), "k5": ("Сбито 5/6", {5}),
    }},
}

CRASH_MULTS = [2, 3, 4, 5, 10, 30, 50, 100]
TOWER_MULTS = [1.0, 1.3, 1.8, 2.5, 3.5, 5.0]
TOWER_LEVELS = 6
TOWER_DOORS = 5
MINES_STATE = {}
TOWER_STATE = {}
BUSY = set()

MAIN_KB = ReplyKeyboardMarkup([["💰 Баланс", "🎮 Играть", "📋 Меню", "👤 Профиль"]], resize_keyboard=True)

# ───────────────────────── БАЗА ДАННЫХ ─────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row

def init_db():
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0, rakeback INTEGER NOT NULL DEFAULT 0, turnover INTEGER NOT NULL DEFAULT 0, rank_claimed INTEGER NOT NULL DEFAULT 0, custom_bet INTEGER NOT NULL DEFAULT 0, created INTEGER);
        CREATE TABLE IF NOT EXISTS bets(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, game TEXT, bet INTEGER, payout INTEGER, detail TEXT, ts INTEGER);
        CREATE TABLE IF NOT EXISTS deposits(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER, status TEXT, ts INTEGER);
        CREATE TABLE IF NOT EXISTS withdrawals(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER, status TEXT, ts INTEGER);
        CREATE TABLE IF NOT EXISTS channels(chat TEXT PRIMARY KEY);
    """)
    db.commit()

def get_user(uid):
    row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if row is None:
        db.execute("INSERT INTO users(id, balance, created) VALUES(?,?,?)", (uid, START_BALANCE, int(time.time())))
        db.commit()
        row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return row

def place_bet(uid, bet):
    cur = db.execute("UPDATE users SET balance=balance-?, turnover=turnover+?, rakeback=rakeback+? WHERE id=? AND balance>=?", (bet, bet, bet * RAKEBACK_PCT // 100, uid, bet))
    db.commit()
    return cur.rowcount == 1

def settle(uid, game, bet, payout, detail=""):
    if payout > 0:
        db.execute("UPDATE users SET balance=balance+? WHERE id=?", (payout, uid))
    db.execute("INSERT INTO bets(user_id, game, bet, payout, detail, ts) VALUES(?,?,?,?,?,?)", (uid, game, bet, payout, detail, int(time.time())))
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

async def need_sub(update, ctx, uid):
    chats = [r["chat"] for r in db.execute("SELECT chat FROM channels").fetchall()]
    miss = []
    for chat in chats:
        try:
            target = chat if chat.startswith("@") else int(chat)
            mem = await ctx.bot.get_chat_member(target, uid)
            if mem.status in ("left", "kicked"):
                miss.append(chat)
        except Exception:
            pass
    if not miss:
        return False
    rows = [[IB(f"📢 {ch}", url=f"https://t.me/{ch[1:]}")] for ch in miss if ch.startswith("@")]
    rows.append([IB("✅ Проверить подписку", callback_data="subs:check")])
    await respond(update, "📢 <b>Подпишитесь на каналы:</b>\n" + "\n".join(f"• {html.escape(c)}" for c in miss), IM(rows))
    return True

# ───────────────────────── МЕНЮ ─────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id)
    ctx.user_data.clear()
    await update.message.reply_text(f"🎰 <b>Добро пожаловать!</b>\n\n💰 Баланс: <b>{m(u['balance'])}</b>", reply_markup=MAIN_KB, parse_mode=HTML)

async def cmd_kazna(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not XROCKET_TOKEN:
        await update.message.reply_text("❌ Нет токена xRocket.")
        return
    url = "https://pay.api.xrocket.exchange/api/v1/balances"
    headers = {"Authorization": f"Bearer {XROCKET_TOKEN}"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            balances = data.get("balances", [])
            if not balances:
                await update.message.reply_text("💰 Казна пуста.")
                return
            text = "🏦 <b>Казна xRocket:</b>\n"
            for b in balances:
                text += f"• {b.get('asset')}: {b.get('amount')}\n"
            await update.message.reply_text(text, parse_mode=HTML)
        else:
            await update.message.reply_text(f"❌ Ошибка: {r.status_code}")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def show_balance(update, ctx):
    u = get_user(update.effective_user.id)
    kb = IM([[IB("💳 Пополнить", callback_data="dep:start"), IB("💸 Вывести", callback_data="wd:start")]])
    await respond(update, f"💰 <b>Баланс:</b> {m(u['balance'])}\n♻️ <b>Рейкбек:</b> {m(u['rakeback'])}", kb)

async def show_menu(update, ctx):
    kb = IM([[IB("🎮 Играть", callback_data="menu:play"), IB("👤 Профиль", callback_data="menu:profile")]])
    await respond(update, "📋 <b>Меню</b>\n\nБыстрая ставка: <code>ставка 1.5</code> или <code>вб</code>", kb)

async def show_profile(update, ctx):
    uid = update.effective_user.id
    u = get_user(uid)
    idx = rank_index(u["turnover"])
    _, name, emo, _ = RANKS[idx]
    prog = "📈 Максимальный ранг!" if idx == len(RANKS) - 1 else f"📈 До «{RANKS[idx+1][2]} {RANKS[idx+1][1]}»: осталось {m(RANKS[idx+1][0] - u['turnover'])}"
    text = f"👤 <b>Профиль</b>\n🆔 ID: <code>{uid}</code>\n💰 Баланс: <b>{m(u['balance'])}</b>\n♻️ Рейкбек: <b>{m(u['rakeback'])}</b>\n🎰 Оборот: <b>{m(u['turnover'])}</b>\n🏅 Ранг: <b>{emo} {name}</b>\n{prog}"
    rows = []
    if idx > u["rank_claimed"]:
        bonus = sum(RANKS[i][3] for i in range(u["rank_claimed"] + 1, idx + 1))
        rows.append([IB(f"🎁 Бонус за ранг (+{m(bonus)})", callback_data="rank:claim")])
    if u["rakeback"] > 0:
        rows.append([IB(f"♻️ Забрать рейкбек ({m(u['rakeback'])})", callback_data="rb:claim")])
    await respond(update, text, IM(rows))

async def show_games(update, ctx):
    if await need_sub(update, ctx, update.effective_user.id):
        return
    rows = [[IB(t, callback_data=f"g:{k}") for k, t in GAMES[i:i+2]] for i in range(0, len(GAMES), 2)]
    rows.append([IB("⬅️ Назад", callback_data="menu:back")])
    await respond(update, "🎮 <b>Выберите игру:</b>", IM(rows))

# ───────────────────────── ИГРЫ ─────────────────────────
async def show_bet_menu(update, ctx, game):
    u = get_user(update.effective_user.id)
    ctx.user_data["game"] = game
    cur = ctx.user_data.get("bet")
    text = f"<b>{GAME_TITLES[game]}</b>\n💰 Баланс: <b>{m(u['balance'])}</b>\n" + (f"💵 Ставка: <b>{m(cur)}</b>\n" if cur else "") + "\nВыберите ставку:"
    rows = [[IB(m(a), callback_data=f"b:{game}:{a}") for a in [10, 50, 100]], [IB(m(a), callback_data=f"b:{game}:{a}") for a in [200, 500, 1000]], [IB("✍️ Ввести сумму", callback_data=f"b:{game}:in"), IB("💰 Вся сумма", callback_data=f"b:{game}:all")]]
    if u["custom_bet"] > 0:
        rows.append([IB(f"⭐ Своя ставка ({m(u['custom_bet'])})", callback_data=f"b:{game}:cu")])
    rows.append([IB("⬅️ Назад", callback_data="menu:play")])
    await respond(update, text, IM(rows))

async def next_step(update, ctx):
    game, bet = ctx.user_data.get("game"), ctx.user_data.get("bet")
    if not game or not bet:
        return await show_games(update, ctx)
    head = f"<b>{GAME_TITLES[game]}</b>\n💵 Ставка: <b>{m(bet)}</b>\n\n"
    chg = [IB("💵 Сменить ставку", callback_data=f"g:{game}"), IB("🎮 Другие игры", callback_data="menu:play")]
    if game in DICE:
        cfg = DICE[game]
        items = list(cfg["opts"].items())
        rows = [[IB(f"{lab} · x{fm(cfg['n'] / len(v))}", callback_data=f"p:{game}:{k}") for k, (lab, v) in items[i:i+2]] for i in range(0, len(items), 2)]
        rows.append(chg)
        await respond(update, head + "Выберите исход:", IM(rows))
    elif game == "crash":
        rows = [[IB(f"x{x}", callback_data=f"cr:{x}") for x in CRASH_MULTS[i:i+4]] for i in (0, 4)]
        rows.append(chg)
        await respond(update, head + "Выберите множитель.", IM(rows))
    elif game == "mines":
        rows = [[IB(f"💣 {n}", callback_data=f"mc:{n}") for n in range(2, 13)[i:i+4]] for i in range(0, 11, 4)]
        rows.append(chg)
        await respond(update, head + "Выберите количество мин (2–12):", IM(rows))
    elif game == "tower":
        rows = [[IB("🏰 Начать игру", callback_data="tw:start")], chg]
        await respond(update, head + "6 уровней, 5 дверей, 1 мина.", IM(rows))

async def play_dice(update, ctx, game, key):
    q = update.callback_query
    uid, bet, cfg = q.from_user.id, ctx.user_data.get("bet"), DICE.get(game)
    if not cfg or key not in cfg["opts"] or not bet:
        return "Ошибка ставки"
    if uid in BUSY:
        return "Подождите ⏳"
    label, vals = cfg["opts"][key]
    mult = cfg["n"] / len(vals)
    if not place_bet(uid, bet):
        return "❌ Недостаточно средств"
    BUSY.add(uid)
    try:
        await q.answer()
        await q.edit_message_text(f"<b>{GAME_TITLES[game]}</b>\n💵 Ставка: <b>{m(bet)}</b>\n🎯 Исход: <b>{label}</b> (x{fm(mult)})\n\n⏳ Бросаем...", parse_mode=HTML)
        dmsg = await ctx.bot.send_dice(chat_id=q.message.chat_id, emoji=cfg["emoji"])
        await asyncio.sleep(4)
        win = dmsg.dice.value in vals
        payout = calc(bet, mult) if win else 0
        settle(uid, game, bet, payout, f"{label}; выпало {dmsg.dice.value}")
        res = f"🎉 <b>ПОБЕДА!</b>\n💵 Выплата: <b>+{m(payout)}</b>" if win else f"😔 <b>Проигрыш</b>\n💸 −{m(bet)}"
        text = f"<b>{GAME_TITLES[game]}</b>\n🎲 Выпало: <b>{dmsg.dice.value}</b>\n\n{res}\n\n💰 Баланс: <b>{m(get_user(uid)['balance'])}</b>"
        kb = IM([[IB("🔁 Повторить", callback_data=f"p:{game}:{key}")], [IB("🎮 Другие игры", callback_data="menu:play")]])
        await ctx.bot.send_message(q.message.chat_id, text, reply_markup=kb, parse_mode=HTML)
    finally:
        BUSY.discard(uid)
    return None

async def play_crash(update, ctx, mult):
    uid, bet = update.callback_query.from_user.id, ctx.user_data.get("bet")
    if mult not in CRASH_MULTS or not bet:
        return "Ошибка"
    if not place_bet(uid, bet):
        return "❌ Недостаточно средств"
    win = random.random() < 1.0 / mult
    payout = calc(bet, mult) if win else 0
    settle(uid, "crash", bet, payout, f"x{mult}")
    res = f"🚀 <b>Выиграл x{mult}!</b>\n💵 Выплата: <b>+{m(payout)}</b>" if win else f"💥 <b>Краш, проигрыш</b>\n💸 −{m(bet)}"
    kb = IM([[IB("🔁 Повторить", callback_data=f"cr:{mult}")], [IB("🎮 Другие игры", callback_data="menu:play")]])
    await respond(update, f"<b>🎰 Краш</b>\n🎯 Множитель: <b>x{mult}</b>\n\n{res}\n\n💰 Баланс: <b>{m(get_user(uid)['balance'])}</b>", kb)
    return None

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
                if i == hit: t = "💥"
                elif i in st["mines"]: t = "💣"
                elif i in st["opened"]: t = "💎"
                else: t = "▫️"
                row.append(IB(t, callback_data="noop"))
            elif i in st["opened"]:
                row.append(IB("💎", callback_data="noop"))
            else:
                row.append(IB("⬜", callback_data=f"mn:{i}"))
        rows.append(row)
    if not reveal and len(st["opened"]) > 0:
        rows.append([IB(f"💰 Забрать {m(calc(st['bet'], mines_mult(st['m'], len(st['opened']))))}", callback_data="mn:cash")])
    if extra:
        rows.extend(extra)
    return IM(rows)

def mines_text(st):
    k = len(st["opened"])
    cur = mines_mult(st["m"], k)
    return f"<b>💣 Мины</b>\n💵 Ставка: <b>{m(st['bet'])}</b>\n💎 Открыто: <b>{k}</b> / {25 - st['m']}\n📈 Множитель: <b>x{cur:.2f}</b>\n💰 Выплата: <b>{m(calc(st['bet'], cur)) if k else m(0)}</b>"

async def mines_start(update, ctx, n):
    uid, bet = update.callback_query.from_user.id, ctx.user_data.get("bet")
    if uid in MINES_STATE: return "У вас уже идёт игра"
    if not bet or not 2 <= n <= 12: return "Ошибка ставки"
    if not place_bet(uid, bet): return "❌ Недостаточно средств"
    st = {"bet": bet, "m": n, "mines": set(random.sample(range(25), n)), "opened": set()}
    MINES_STATE[uid] = st
    await respond(update, mines_text(st), mines_kb(st))
    return None

async def mines_click(update, ctx, arg):
    uid = update.callback_query.from_user.id
    st = MINES_STATE.get(uid)
    if not st: return "Игра не найдена"
    if arg == "cash":
        if len(st["opened"]) == 0: return "Откройте клетку"
        del MINES_STATE[uid]
        payout = calc(st["bet"], mines_mult(st["m"], len(st["opened"])))
        settle(uid, "mines", st["bet"], payout, f"x{mines_mult(st['m'], len(st['opened'])):.2f}")
        await respond(update, f"🎉 <b>Выигрыш {m(payout)}!</b>", mines_kb(st, reveal=True, extra=[[IB("🎮 Другие игры", callback_data="menu:play")]]))
        return None
    try: i = int(arg)
    except ValueError: return "Ошибка"
    if i in st["opened"]: return None
    if i in st["mines"]:
        del MINES_STATE[uid]
        settle(uid, "mines", st["bet"], 0)
        await respond(update, f"💥 <b>БУМ! Проигрыш {m(st['bet'])}</b>", mines_kb(st, reveal=True, hit=i, extra=[[IB("🎮 Другие игры", callback_data="menu:play")]]))
        return None
    st["opened"].add(i)
    if len(st["opened"]) == 25 - st["m"]:
        del MINES_STATE[uid]
        payout = calc(st["bet"], mines_mult(st["m"], len(st["opened"])))
        settle(uid, "mines", st["bet"], payout)
        await respond(update, f"🏆 <b>Всё открыто! Выигрыш {m(payout)}</b>", mines_kb(st, reveal=True, extra=[[IB("🎮 Другие игры", callback_data="menu:play")]]))
        return None
    await respond(update, mines_text(st), mines_kb(st))
    return None

# ───────────────────────── БАШНЯ ─────────────────────────
def tower_text(st):
    lines = [f"🏰 Башня | Ставка {m(st['bet'])}\n"]
    for lvl in range(TOWER_LEVELS - 1, -1, -1):
        row = []
        for d in range(TOWER_DOORS):
            if lvl < st["level"]:
                row.append("💥" if st["traps"][lvl] == d else "✅")
            elif lvl == st["level"]:
                row.append("🚪")
            else:
                row.append("🔒")
        lines.append(f"Уровень {lvl + 1} (x{TOWER_MULTS[lvl]:.1f}): " + " ".join(row))
    if st["level"] > 0:
        cur = TOWER_MULTS[st["level"] - 1]
        lines.append(f"\nСейчас: x{cur:.1f} → {m(calc(st['bet'], cur))}")
    return "\n".join(lines)

def tower_kb(st):
    rows = []
    if st["level"] < TOWER_LEVELS:
        rows.append([IB(f"🚪 Дверь {d + 1}", callback_data=f"tw:{d}") for d in range(TOWER_DOORS)])
    if st["level"] > 0:
        rows.append([IB("💰 Забрать", callback_data="tw:cash")])
    return IM(rows)

async def tower_click(update, ctx, arg):
    uid, bet = update.callback_query.from_user.id, ctx.user_data.get("bet")
    if arg == "start":
        if uid in TOWER_STATE: return "Игра уже идёт"
        if not bet or not place_bet(uid, bet): return "❌ Недостаточно средств"
        st = {"bet": bet, "level": 0, "traps": [random.randrange(5) for _ in range(TOWER_LEVELS)]}
        TOWER_STATE[uid] = st
        await respond(update, tower_text(st), tower_kb(st))
        return None
    st = TOWER_STATE.get(uid)
    if not st: return "Игра не найдена"
    if arg == "cash":
        if st["level"] == 0: return "Пройдите уровень"
        del TOWER_STATE[uid]
        payout = calc(st["bet"], TOWER_MULTS[st["level"] - 1])
        settle(uid, "tower", st["bet"], payout)
        await respond(update, f"💰 <b>Вы забрали {m(payout)}</b>", IM([[IB("🎮 Другие игры", callback_data="menu:play")]]))
        return None
    try: d = int(arg)
    except ValueError: return "Ошибка"
    if st["traps"][st["level"]] == d:
        del TOWER_STATE[uid]
        settle(uid, "tower", st["bet"], 0)
        await respond(update, f"💥 <b>Мина на уровне {st['level'] + 1}! Проигрыш {m(st['bet'])}</b>", IM([[IB("🎮 Другие игры", callback_data="menu:play")]]))
        return None
    st["level"] += 1
    if st["level"] == TOWER_LEVELS:
        del TOWER_STATE[uid]
        payout = calc(st["bet"], TOWER_MULTS[-1])
        settle(uid, "tower", st["bet"], payout)
        await respond(update, f"🏆 <b>Башня пройдена! Выигрыш {m(payout)}</b>", IM([[IB("🎮 Другие игры", callback_data="menu:play")]]))
        return None
    await respond(update, tower_text(st), tower_kb(st))
    return None

# ───────────────────────── XROCKET ─────────────────────────
async def create_deposit(update, ctx, amount):
    uid = update.effective_user.id
    if not XROCKET_TOKEN:
        await update.message.reply_text("❌ Ошибка: нет токена.")
        return
    amount_usdt = amount / 100.0 
    url = "https://pay.api.xrocket.exchange/api/v1/invoices"
    headers = {"Authorization": f"Bearer {XROCKET_TOKEN}", "Content-Type": "application/json"}
    payload = {"priceAmount": str(amount_usdt), "priceCurrency": "USDT", "numPayments": 1, "description": f"Deposit for {uid}"}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        if r.status_code in (200, 201):
            data = r.json()
            link = data.get("links", {}).get("telegramBotLink")
            if link:
                cur = db.execute("INSERT INTO deposits(user_id, amount, status, ts) VALUES(?,?,?,?)", (uid, amount, "pending", int(time.time())))
                db.commit()
                await update.message.reply_text(f"💳 <b>Счет на {m(amount)}</b>\nОплатите:", reply_markup=IM([[IB("💳 Оплатить", url=link)]]), parse_mode=HTML)
                return
        await update.message.reply_text(f"❌ Ошибка xRocket: {r.status_code}")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

async def create_withdraw(update, ctx, amount):
    uid = update.effective_user.id
    c = db.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", (amount, uid, amount))
    db.commit()
    if c.rowcount != 1:
        await update.message.reply_text("❌ Недостаточно средств.")
        return
    cur = db.execute("INSERT INTO withdrawals(user_id, amount, status, ts) VALUES(?,?,?,?)", (uid, amount, "pending", int(time.time())))
    db.commit()
    await update.message.reply_text(f"💸 <b>Заявка на вывод #{cur.lastrowid} на {m(amount)}</b>\nОжидайте.", parse_mode=HTML)
    await ctx.bot.send_message(ADMIN_ID, f"🔔 Вывод #{cur.lastrowid}: {m(amount)} от <code>{uid}</code>. /admin", parse_mode=HTML)

# ───────────────────────── АДМИНКА ─────────────────────────
ADMIN_KB = IM([[IB("📊 Статистика", callback_data="adm:stats"), IB("👤 Игрок по ID", callback_data="adm:user")], [IB("💸 Выводы", callback_data="adm:wd"), IB("💳 Пополнения", callback_data="adm:dep")], [IB("📢 Подписки", callback_data="adm:subs"), IB("✉️ Рассылка", callback_data="adm:bc")]])

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    ctx.user_data.pop("await", None)
    await update.message.reply_text("🛠 <b>Админ-панель</b>", reply_markup=ADMIN_KB, parse_mode=HTML)

async def cb_admin(update, ctx, parts):
    q = update.callback_query
    if not is_admin(q.from_user.id): return "Нет доступа"
    act = parts[1]
    if act == "stats":
        r = db.execute("SELECT COUNT(*) c, COALESCE(SUM(balance),0) b, COALESCE(SUM(turnover),0) t FROM users").fetchone()
        await q.message.reply_text(f"📊 <b>Статистика</b>\n👥 Игроков: <b>{r['c']}</b>\n💰 Балансы: <b>{m(r['b'])}</b>\n🎰 Оборот: <b>{m(r['t'])}</b>", parse_mode=HTML)
    elif act == "user":
        ctx.user_data["await"] = "adm_user"
        await q.message.reply_text("Введите ID игрока:")
    elif act in ("wd", "dep"):
        table = "withdrawals" if act == "wd" else "deposits"
        rows = db.execute(f"SELECT * FROM {table} WHERE status='pending' ORDER BY id LIMIT 10").fetchall()
        if not rows: await q.message.reply_text("Нет заявок.")
        for r in rows:
            kb = IM([[IB("✅ Одобрить", callback_data=f"adm:{'wok' if act=='wd' else 'dok'}:{r['id']}"), IB("❌ Отклонить", callback_data=f"adm:{'wno' if act=='wd' else 'dno'}:{r['id']}")]])
            await q.message.reply_text(f"{'💸 Вывод' if act=='wd' else '💳 Пополнение'} #{r['id']}\nИгрок: <code>{r['user_id']}</code>\nСумма: <b>{m(r['amount'])}</b>", reply_markup=kb, parse_mode=HTML)
    elif act in ("wok", "wno", "dok", "dno"):
        return await admin_process(update, ctx, act, int(parts[2]))
    elif act == "subs":
        chats = [r["chat"] for r in db.execute("SELECT chat FROM channels").fetchall()]
        rows = [[IB(f"🗑 {c}", callback_data=f"adm:delch:{c}")] for c in chats] + [[IB("➕ Добавить", callback_data="adm:addch")]]
        await respond(update, "📢 <b>Подписки</b>\n" + ("\n".join(chats) if chats else "Нет"), IM(rows))
    elif act == "addch":
        ctx.user_data["await"] = "adm_addch"
        await q.message.reply_text("Отправьте @username канала:")
    elif act == "delch":
        db.execute("DELETE FROM channels WHERE chat=?", (":".join(parts[2:]),))
        db.commit()
        await q.message.reply_text("Удалено.")
    elif act == "bc":
        ctx.user_data["await"] = "adm_bc"
        await q.message.reply_text("Текст для рассылки:")
    return None

async def admin_process(update, ctx, action, rid):
    q = update.callback_query
    table = "withdrawals" if action.startswith("w") else "deposits"
    r = db.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()
    if not r or r["status"] != "pending": return "Уже обработано"
    uid, amount = r["user_id"], r["amount"]
    
    if action == "wok":
        # Создаем чек через xRocket для вывода
        if not XROCKET_TOKEN:
            note = f"✅ Вывод #{rid} одобрен (но нет токена для чека)."
        else:
            url = "https://pay.api.xrocket.exchange/api/v1/cheques"
            headers = {"Authorization": f"Bearer {XROCKET_TOKEN}", "Content-Type": "application/json"}
            payload = {
                "asset": "USDT",
                "amount": str(amount / 100.0),
                "description": f"Withdraw for {uid}",
                "targetType": "telegram_user_id",
                "target": str(uid)
            }
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=10)
                if resp.status_code in (200, 201):
                    data = resp.json()
                    cheque_link = data.get("links", {}).get("telegramMiniAppLink")
                    if cheque_link:
                        note = f"✅ Вывод #{rid} одобрен! Чек создан."
                        try:
                            await ctx.bot.send_message(uid, f"💸 Ваш вывод на {m(amount)} одобрен!\nПолучите средства по ссылке:", reply_markup=IM([[IB("🎁 Получить", url=cheque_link)]]))
                        except Exception: pass
                    else:
                        note = f"✅ Вывод #{rid} одобрен, но ссылка на чек не получена."
                else:
                    note = f"⚠️ Вывод #{rid} одобрен, но ошибка создания чека: {resp.status_code}"
            except Exception as e:
                note = f"⚠️ Вывод #{rid} одобрен, но ошибка: {e}"
    elif action == "wno":
        add_balance(uid, amount)
        note = f"❌ Вывод #{rid} отклонён, {m(amount)} возвращены."
    elif action == "dok":
        add_balance(uid, amount)
        note = f"✅ На ваш баланс зачислено {m(amount)}!"
    else:
        note = f"❌ Пополнение #{rid} отклонено."
        
    db.execute(f"UPDATE {table} SET status=? WHERE id=?", ("approved" if "ok" in action else "rejected", rid))
    db.commit()
    try: await q.edit_message_text(f"{q.message.text}\n\n➡️ Статус: {note}")
    except Exception: pass
    try: await ctx.bot.send_message(uid, note)
    except Exception: pass
    return None

# ───────────────────────── ОБРАБОТЧИКИ ─────────────────────────
async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    get_user(uid)
    parts = (q.data or "").split(":")
    t = parts[0]
    res = None
    try:
        if t == "noop": pass
        elif t == "menu":
            sub = parts[1]
            if sub == "play": await show_games(update, ctx)
            elif sub == "back": ctx.user_data.pop("game", None); await respond(update, "📋 <b>Главное меню</b>", MAIN_KB)
            elif sub == "profile": await show_profile(update, ctx)
        elif t == "subs":
            if not await need_sub(update, ctx, uid): await show_games(update, ctx)
        elif t == "g":
            ctx.user_data.pop("await", None)
            game = parts[1]
            if await need_sub(update, ctx, uid): pass
            else: await show_bet_menu(update, ctx, game)
        elif t == "b":
            game, val = parts[1], parts[2]
            u = get_user(uid)
            ctx.user_data["game"] = game
            if val == "in":
                ctx.user_data["await"] = "bet"
                await respond(update, f"Введите сумму ставки (например, 1.5):", IM([[IB("⬅️ Назад", callback_data=f"g:{game}")]]))
            else:
                amt = u["balance"] if val == "all" else (u["custom_bet"] if val == "cu" else int(val))
                if amt < MIN_BET: res = f"Мин. ставка {m(MIN_BET)}"
                elif amt > u["balance"]: res = "❌ Недостаточно средств"
                else:
                    ctx.user_data["bet"] = amt
                    ctx.user_data.pop("await", None)
                    await next_step(update, ctx)
        elif t == "o":
            ctx.user_data["game"] = parts[1]
            await next_step(update, ctx)
        elif t == "p": res = await play_dice(update, ctx, parts[1], parts[2])
        elif t == "cr": res = await play_crash(update, ctx, int(parts[1]))
        elif t == "mc": res = await mines_start(update, ctx, int(parts[1]))
        elif t == "mn": res = await mines_click(update, ctx, parts[1])
        elif t == "tw": res = await tower_click(update, ctx, parts[1])
        elif t == "rank":
            u = get_user(uid); idx = rank_index(u["turnover"])
            if idx <= u["rank_claimed"]: res = "Нет бонусов"
            else:
                bonus = sum(RANKS[i][3] for i in range(u["rank_claimed"] + 1, idx + 1))
                db.execute("UPDATE users SET balance=balance+?, rank_claimed=? WHERE id=?", (bonus, idx, uid))
                db.commit(); res = f"🎁 Бонус {m(bonus)} зачислен!"; await show_profile(update, ctx)
        elif t == "rb":
            u = get_user(uid)
            if u["rakeback"] <= 0: res = "Пусто"
            else:
                db.execute("UPDATE users SET balance=balance+?, rakeback=0 WHERE id=?", (u["rakeback"], uid))
                db.commit(); res = f"♻️ Рейкбек {m(u['rakeback'])} зачислен!"; await show_profile(update, ctx)
        elif t == "dep":
            ctx.user_data["await"] = "dep"
            await q.message.reply_text(f"Введите сумму пополнения (мин. {m(MIN_TX)}):")
        elif t == "wd":
            ctx.user_data["await"] = "wd"
            await q.message.reply_text(f"Введите сумму вывода (мин. {m(MIN_TX)}):")
        elif t == "adm": res = await cb_admin(update, ctx, parts)
    except Exception:
        log.exception("callback error: %s", q.data); res = "Ошибка"
    try: await q.answer(res, show_alert=bool(res))
    except BadRequest: pass

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    low = text.lower()
    u = get_user(uid)

    if text in ("💰 Баланс", "🎮 Играть", "📋 Меню", "👤 Профиль"):
        ctx.user_data.pop("await", None)
        if text == "💰 Баланс": await show_balance(update, ctx)
        elif text == "🎮 Играть": await show_games(update, ctx)
        elif text == "📋 Меню": await show_menu(update, ctx)
        else: await show_profile(update, ctx)
        return

    if low == "казна":
        await cmd_kazna(update, ctx)
        return

    aw = ctx.user_data.get("await")

    if aw and aw.startswith("adm_"):
        if not is_admin(uid): ctx.user_data.pop("await", None); return
        ctx.user_data.pop("await", None)
        if aw == "adm_user":
            try: info = db.execute("SELECT * FROM users WHERE id=?", (int(text),)).fetchone()
            except ValueError: info = None
            if info: await update.message.reply_text(f"👤 <b>Игрок</b> <code>{text}</code>\n💰 Баланс: {m(info['balance'])}\n🎰 Оборот: {m(info['turnover'])}", parse_mode=HTML)
            else: await update.message.reply_text("Не найден.")
        elif aw == "adm_addch":
            db.execute("INSERT OR IGNORE INTO channels(chat) VALUES(?)", (text,)); db.commit()
            await update.message.reply_text(f"✅ Канал {text} добавлен.")
        elif aw == "adm_bc":
            ids = [r["id"] for r in db.execute("SELECT id FROM users").fetchall()]; ok = 0
            await update.message.reply_text(f"📤 Рассылка на {len(ids)}...")
            for i in ids:
                try: await ctx.bot.send_message(i, text); ok += 1
                except Exception: pass
                await asyncio.sleep(0.05)
            await update.message.reply_text(f"✅ Доставлено: {ok}/{len(ids)}")
        return

    if aw in ("bet", "dep", "wd"):
        amt = parse_amount(text)
        if amt is None: await update.message.reply_text("Введите число, например 1.5"); return
        if aw == "bet":
            if amt < MIN_BET: await update.message.reply_text(f"Мин. ставка {m(MIN_BET)}")
            elif amt > u["balance"]: await update.message.reply_text(f"❌ Недостаточно средств. Баланс: {m(u['balance'])}")
            else:
                ctx.user_data["bet"] = amt; ctx.user_data.pop("await", None); await next_step(update, ctx)
            return
        if amt < MIN_TX: await update.message.reply_text(f"Мин. сумма {m(MIN_TX)}"); return
        ctx.user_data.pop("await", None)
        if aw == "dep": await create_deposit(update, ctx, amt)
        else: await create_withdraw(update, ctx, amt)
        return

    mt = re.fullmatch(r"(ставка|деп|пополнить|вывод)\s+([\d.,]+)\$?", low)
    if mt:
        cmd, val = mt.group(1), mt.group(2)
        amt = parse_amount(val)
        if amt is None or amt < MIN_TX: await update.message.reply_text(f"Мин. сумма {m(MIN_TX)}"); return
        
        if cmd == "ставка":
            if amt < MIN_BET: await update.message.reply_text(f"Мин. ставка {m(MIN_BET)}"); return
            db.execute("UPDATE users SET custom_bet=? WHERE id=?", (amt, uid)); db.commit()
            ctx.user_data["bet"] = amt
            await update.message.reply_text(f"⭐ Своя ставка: <b>{m(amt)}</b>", parse_mode=HTML)
            await show_games(update, ctx)
        elif cmd in ("деп", "пополнить"):
            await create_deposit(update, ctx, amt)
        elif cmd == "вывод":
            await create_withdraw(update, ctx, amt)
        return

    if re.fullmatch(r"[\d.,]+\$?", low):
        amt = parse_amount(low)
        if amt and amt >= MIN_BET:
            db.execute("UPDATE users SET custom_bet=? WHERE id=?", (amt, uid)); db.commit()
            ctx.user_data["bet"] = amt
            await update.message.reply_text(f"⭐ Ставка: <b>{m(amt)}</b>", parse_mode=HTML)
            await show_games(update, ctx)
        else:
            await update.message.reply_text(f"Мин. ставка {m(MIN_BET)}")
        return

    if low == "вб":
        if u["balance"] < MIN_BET: await update.message.reply_text("❌ Недостаточно средств."); return
        ctx.user_data["bet"] = u["balance"]
        await update.message.reply_text(f"💰 Весь баланс: <b>{m(u['balance'])}</b>", parse_mode=HTML)
        if ctx.user_data.get("game"): await next_step(update, ctx)
        else: await show_games(update, ctx)
        return

async def on_error(update, ctx):
    log.error("Ошибка: %s", ctx.error, exc_info=ctx.error)

# ───────────────────────── FLASK ─────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/")
def index(): return "Bot is running"

@flask_app.route("/health")
def health(): return "ok"

def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), use_reloader=False)

# ───────────────────────── ЗАПУСК ─────────────────────────
def main():
    if not TOKEN: raise SystemExit("Нет TELEGRAM_TOKEN")
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
