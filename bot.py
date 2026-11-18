# -*- coding: utf-8 -*-
import os
import re
import json
import html
import time
import hmac
import hashlib
import random
import sqlite3
import asyncio
import logging
import threading
import requests

from flask import Flask, request, jsonify
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
XROCKET_TOKEN = os.environ.get("XROCKET_TOKEN")
DB_PATH = os.environ.get("DB_PATH", "casino.db")
# 1 = отклонять вебхуки с неверной подписью (включайте, когда убедитесь, что подпись сходится)
WEBHOOK_STRICT = os.environ.get("WEBHOOK_STRICT", "0") == "1"
XR_BASE = "https://pay.api.xrocket.exchange/api/v1"

START_BALANCE = 200
FEE = 0.95
RAKEBACK_PCT = 2
MIN_BET = 10
MIN_TX = 10
MAX_BET = int(os.environ.get("MAX_BET", "10000"))                  # макс. ставка, центы ($100)
MAX_PAYOUT = int(os.environ.get("MAX_PAYOUT", "100000"))           # макс. выплата за одну игру ($1000)
WD_MIN_DEPOSITED = int(os.environ.get("WD_MIN_DEPOSITED", "100"))  # вывод только после пополнений на ≥ $1
WD_TURNOVER_X = float(os.environ.get("WD_TURNOVER_X", "1"))        # оборот должен быть ≥ внесённого × X
HTML = ParseMode.HTML

RANKS = [
    (0, "Новичок", "🌱", 0), (1000, "Игрок", "🎮", 100),
    (5000, "Хайроллер", "💎", 500), (20000, "Магнат", "👑", 1000),
    (100000, "Легенда", "🏆", 5000),
]

GAMES = [
    ("dice", "🎲 Кубик"), ("darts", "🎯 Дартс"), ("football", "⚽ Футбол"),
    ("basket", "🏀 Баскетбол"), ("bowling", "🎳 Боулинг"), ("slots", "🎰 Слоты"), ("crash", "🚀 Краш"),
    ("mines", "💣 Мины"), ("tower", "🏰 Башня"),
]
GAME_TITLES = dict(GAMES)
STAT_TITLES = {**GAME_TITLES, "dice2": "🎲🎲 Два кубика", "darts2": "🎯🎯 Два броска"}

DICE = {
    "dice": {"emoji": "🎲", "n": 6, "opts": {
        "even": ("Чёт", {2, 4, 6}), "odd": ("Нечёт", {1, 3, 5}),
        "high": ("Больше (4-6)", {4, 5, 6}), "low": ("Меньше (1-3)", {1, 2, 3}),
        "center": ("Центр (3-4)", {3, 4}), "edge": ("Края (1,2,5,6)", {1, 2, 5, 6}),
    }},
    # Дартс (Telegram): 1 — мимо/отскок, 2 и 4 — красное, 3 и 5 — белое, 6 — центр. Красный x3, Белый x3
    "darts": {"emoji": "🎯", "n": 6, "opts": {
        "red": ("Красный", {2, 4}),
        "white": ("Белый", {3, 5}),
        "bounce": ("Отскок", {1}),
        "bull": ("Центр", {6}),
    }},
    "football": {"emoji": "⚽", "n": 5, "opts": {
        "goal": ("Гол", {3, 4, 5}), "miss": ("Мимо", {1, 2}), "center": ("Гол в центр", {3}),
    }},
    # Баскетбол: "Застрял" ТОЛЬКО 2,3; "Прокрут" 1,2 (x2.5)
    "basket": {"emoji": "🏀", "n": 5, "opts": {
        "goal": ("Гол", {4, 5}),
        "nogoal": ("Без гола", {1, 2, 3}),
        "bounce": ("Отскок", {1}),
        "stuck": ("Застрял", {2, 3}),
        "spin": ("Прокрут", {1, 2}),
    }},
    "bowling": {"emoji": "🎳", "n": 6, "opts": {
        "strike": ("Страйк", {6}), "miss": ("Мимо", {1}),
        "k1": ("Сбито 1/6", {2}), "k3": ("Сбито 3/6", {3}),
        "k4": ("Сбито 4/6", {4}), "k5": ("Сбито 5/6", {5}),
    }},
}

# Слоты Telegram 🎰: значение 1..64, три барабана по 4 символа (значение-1 в системе по основанию 4)
SLOT_NAMES = ["BAR", "🍇", "🍋", "7️⃣"]
SLOT_KEYS = ["b", "g", "l", "s"]


def slot_reels(v):
    n = v - 1
    return (n & 3, (n >> 2) & 3, (n >> 4) & 3)


_slot_opts = {}
for _r in range(3):
    for _i, _k in enumerate(SLOT_KEYS):
        _slot_opts[f"r{_r + 1}{_k}"] = (
            f"Слот {_r + 1}: {SLOT_NAMES[_i]}",
            {v for v in range(1, 65) if slot_reels(v)[_r] == _i},
        )
_slot_opts["uniq"] = ("Уникальные символы", {v for v in range(1, 65) if len(set(slot_reels(v))) == 3})
_slot_opts["tri"] = ("Три одинаковых", {v for v in range(1, 65) if len(set(slot_reels(v))) == 1})
_slot_opts["jp"] = ("Джекпот 777", {64})
DICE["slots"] = {"emoji": "🎰", "n": 64, "opts": _slot_opts}

_ALL2 = [(a, b) for a in range(1, 7) for b in range(1, 7)]
DICE2 = {
    "ee": ("Оба чёт", {p for p in _ALL2 if p[0] % 2 == 0 and p[1] % 2 == 0}),
    "oo": ("Оба нечёт", {p for p in _ALL2 if p[0] % 2 == 1 and p[1] % 2 == 1}),
    "hh": ("Оба больше (4-6)", {p for p in _ALL2 if p[0] >= 4 and p[1] >= 4}),
    "ll": ("Оба меньше (1-3)", {p for p in _ALL2 if p[0] <= 3 and p[1] <= 3}),
    "p18": ("Произведение > 18", {p for p in _ALL2 if p[0] * p[1] > 18}),
    "gt": ("1 > 2", {p for p in _ALL2 if p[0] > p[1]}),
    "lt": ("2 > 1", {p for p in _ALL2 if p[0] < p[1]}),
    "dbl": ("Любой дубль", {p for p in _ALL2 if p[0] == p[1]}),
}

# Дартс, 2 броска. Кольца от края к центру: 1 — отскок, 2..5 — сектора (красный/белый), 6 — центр.
# Чем больше значение, тем ближе к центру.
_RED, _WHITE = {2, 4}, {3, 5}
DARTS2 = {
    "rr": ("Оба красный", {p for p in _ALL2 if p[0] in _RED and p[1] in _RED}),
    "ww": ("Оба белый", {p for p in _ALL2 if p[0] in _WHITE and p[1] in _WHITE}),
    "cl1": ("1 ближе 2", {p for p in _ALL2 if p[0] > p[1]}),
    "cl2": ("2 ближе 1", {p for p in _ALL2 if p[1] > p[0]}),
    "sec": ("В сектора", {p for p in _ALL2 if 2 <= p[0] <= 5 and 2 <= p[1] <= 5}),
    "adj": ("Рядом", {p for p in _ALL2 if 2 <= p[0] <= 5 and 2 <= p[1] <= 5 and abs(p[0] - p[1]) == 1}),
    "eq": ("Одинаковые", {p for p in _ALL2 if p[0] == p[1]}),
    "bull2": ("Оба центр", {(6, 6)}),
    "miss2": ("Оба отскок", {(1, 1)}),
}
MULTI = {"dice": DICE2, "darts": DARTS2}

CRASH_MULTS = [2, 3, 4, 5, 10, 30, 50, 100]
# P(пройти k уровней) = 0.8^k; с учётом FEE=0.95 ожидание игрока ≈ 91-93% на любом уровне
TOWER_MULTS = [1.2, 1.5, 1.9, 2.4, 3.0, 3.7]
TOWER_LEVELS = 6
TOWER_DOORS = 5
MINES_STATE = {}
TOWER_STATE = {}
BUSY = set()

MAIN_KB = ReplyKeyboardMarkup([["💰 Баланс", "🎮 Играть", "📋 Меню"]], resize_keyboard=True)

# ───────────────────────── БАЗА ДАННЫХ ─────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
db.row_factory = sqlite3.Row


def init_db():
    try:
        db.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0, rakeback INTEGER NOT NULL DEFAULT 0, turnover INTEGER NOT NULL DEFAULT 0, rank_claimed INTEGER NOT NULL DEFAULT 0, custom_bet INTEGER NOT NULL DEFAULT 0, created INTEGER);
        CREATE TABLE IF NOT EXISTS bets(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, game TEXT, bet INTEGER, payout INTEGER, detail TEXT, ts INTEGER);
        CREATE TABLE IF NOT EXISTS deposits(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER, status TEXT, invoice_id TEXT, ts INTEGER);
        CREATE TABLE IF NOT EXISTS withdrawals(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER, status TEXT, ts INTEGER);
        CREATE TABLE IF NOT EXISTS channels(chat TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS active_games(user_id INTEGER, kind TEXT, state TEXT, PRIMARY KEY(user_id, kind));
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS bans(user_id INTEGER PRIMARY KEY, reason TEXT, ts INTEGER);
        CREATE TABLE IF NOT EXISTS admin_log(id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, action TEXT, ts INTEGER);
        CREATE TABLE IF NOT EXISTS transfers(id INTEGER PRIMARY KEY AUTOINCREMENT, from_id INTEGER, to_id INTEGER, amount INTEGER, ts INTEGER);
        CREATE TABLE IF NOT EXISTS chat_msgs(day TEXT, user_id INTEGER, n INTEGER NOT NULL DEFAULT 0, name TEXT, PRIMARY KEY(day, user_id));
        CREATE TABLE IF NOT EXISTS contests(id INTEGER PRIMARY KEY AUTOINCREMENT, chat TEXT, msg_id INTEGER, prize INTEGER, ends INTEGER, status TEXT, winner INTEGER);
        CREATE TABLE IF NOT EXISTS contest_users(contest_id INTEGER, user_id INTEGER, PRIMARY KEY(contest_id, user_id));
    """)
    # миграция: последняя ставка игрока (чтобы не спрашивать её снова даже после перезапуска)
    for ddl in (
        "ALTER TABLE users ADD COLUMN last_bet INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN referrer INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN ref_earn INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN last_daily INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN streak INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN chat_bal INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE channels ADD COLUMN kind TEXT NOT NULL DEFAULT 'channel'",
        "ALTER TABLE channels ADD COLUMN official INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE channels ADD COLUMN req INTEGER NOT NULL DEFAULT 1",
    ):
        try:
            db.execute(ddl)
        except sqlite3.OperationalError:
            pass
    db.commit()


def get_user(uid):
    row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if row is None:
        db.execute("INSERT INTO users(id, balance, created) VALUES(?,?,?)", (uid, START_BALANCE, int(time.time())))
        db.commit()
        row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return row


def place_bet(uid, bet):
    if bet <= 0 or bet > MAX_BET:
        return False
    cur = db.execute(
        "UPDATE users SET balance=balance-?, turnover=turnover+?, rakeback=rakeback+? WHERE id=? AND balance>=?",
        (bet, bet, bet * RAKEBACK_PCT // 100, uid, bet),
    )
    db.commit()
    ok = cur.rowcount == 1
    if ok:
        r = db.execute("SELECT referrer FROM users WHERE id=?", (uid,)).fetchone()
        if r and r["referrer"]:
            db.execute("UPDATE users SET ref_earn=ref_earn+? WHERE id=?", (bet * REF_BP, r["referrer"]))
            db.commit()
    return ok


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


def get_bet(ctx, uid):
    """Текущая ставка игрока: из памяти, а если её нет (перезапуск) — из БД."""
    b = ctx.user_data.get("bet")
    if not b:
        b = get_user(uid)["last_bet"]
        if b:
            ctx.user_data["bet"] = b
    return b or 0


def set_bet(ctx, uid, amt):
    ctx.user_data["bet"] = amt
    db.execute("UPDATE users SET last_bet=? WHERE id=?", (amt, uid))
    db.commit()


def save_game(kind, uid, st):
    data = dict(st)
    for k in ("mines", "opened"):
        if k in data:
            data[k] = sorted(data[k])
    db.execute("INSERT OR REPLACE INTO active_games(user_id, kind, state) VALUES(?,?,?)", (uid, kind, json.dumps(data)))
    db.commit()


def drop_game(kind, uid):
    (MINES_STATE if kind == "mines" else TOWER_STATE).pop(uid, None)
    db.execute("DELETE FROM active_games WHERE user_id=? AND kind=?", (uid, kind))
    db.commit()


def load_games():
    """Восстанавливает незавершённые Мины/Башню после перезапуска — ставки не сгорают."""
    for r in db.execute("SELECT * FROM active_games").fetchall():
        try:
            st = json.loads(r["state"])
            if r["kind"] == "mines":
                st["mines"] = set(st["mines"])
                st["opened"] = set(st["opened"])
                MINES_STATE[r["user_id"]] = st
            else:
                TOWER_STATE[r["user_id"]] = st
        except Exception:
            log.exception("Не удалось восстановить игру %s", dict(r))


def check_bet(amt, balance):
    if amt < MIN_BET:
        return f"Мин. ставка {m(MIN_BET)}"
    if amt > MAX_BET:
        return f"Макс. ставка {m(MAX_BET)}"
    if amt > balance:
        return "❌ Недостаточно средств"
    return None


# ───────────────────────── УТИЛИТЫ ─────────────────────────
def m(cents):
    return f"${cents / 100:.2f}"


def fm(x):
    return f"{round(x, 2):g}"


def calc(bet, mult):
    return min(int(bet * mult * FEE), MAX_PAYOUT)


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


def end_rows(repeat_cb=None, game=None, bet=None):
    """После игры: [меньше ставка] [повторить] [больше ставка], другой исход, ставка, игры."""
    rows = []
    if repeat_cb:
        if bet:
            lo = max(MIN_BET, bet // 2)
            hi = min(MAX_BET, bet * 2)
            row = []
            if lo < bet:
                row.append(IB(f"◀️ {m(lo)}", callback_data=f"{repeat_cb}@h"))
            row.append(IB(f"🔁 {m(bet)}", callback_data=repeat_cb))
            if hi > bet:
                row.append(IB(f"{m(hi)} ▶️", callback_data=f"{repeat_cb}@d"))
            rows.append(row)
        else:
            rows.append([IB("🔁 Повторить", callback_data=repeat_cb)])
    if game and game != "tower":
        rows.append([IB("📋 Другой исход", callback_data=f"o:{game}")])
    row = []
    if game:
        row.append(IB("💵 Ставка", callback_data=f"gs:{game}"))
    row.append(IB("🎮 Игры", callback_data="menu:play"))
    rows.append(row)
    return rows


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
    chats = [r["chat"] for r in db.execute("SELECT chat FROM channels WHERE req=1").fetchall()]
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


# ───────────────────────── XROCKET (синхронные вызовы) ─────────────────────────
def xr_headers():
    return {"Authorization": f"Bearer {XROCKET_TOKEN}", "Content-Type": "application/json"}


def _json(r):
    try:
        data = r.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _api_error(r, data):
    """Человекочитаемое описание ошибки xRocket."""
    if r.status_code == 401:
        return "401: неверный XROCKET_TOKEN"
    if r.status_code == 403:
        return "403: у API-ключа нет доступа к чекам (включите чеки/права в настройках xRocket Pay)"
    if r.status_code == 404:
        return "404: метод API не найден"
    if r.status_code == 429:
        return "429: слишком много запросов, повторите позже"
    msg = data.get("message") or data.get("error") or data.get("errors")
    if msg:
        return f"{r.status_code}: {str(msg)[:200]}"
    return f"{r.status_code}: {r.text[:200]}"


def xr_create_invoice(uid, amount_cents):
    """Возвращает (ok, link, invoice_id, error)."""
    payload = {
        "priceAmount": str(amount_cents / 100.0),
        "priceCurrency": "USDT",
        "numPayments": 1,
        "description": f"Deposit for {uid}",
    }
    try:
        r = requests.post(f"{XR_BASE}/invoices", json=payload, headers=xr_headers(), timeout=15)
    except Exception as e:
        return False, None, None, f"сеть: {e}"
    data = _json(r)
    if r.status_code in (200, 201):
        d = data.get("data") if isinstance(data.get("data"), dict) else data
        links = d.get("links") or {}
        link = links.get("telegramBotLink") or d.get("link") or links.get("telegramMiniAppLink")
        invoice_id = d.get("id")
        if link and invoice_id is not None:
            return True, link, str(invoice_id), None
        log.error("Invoice response without link/id: %s", data)
        return False, None, None, "ответ xRocket без ссылки или id счёта"
    log.error("Invoice error %s: %s", r.status_code, r.text[:500])
    return False, None, None, _api_error(r, data)


def xr_create_cheque(uid, amount_cents):
    """Возвращает (ok, link, info)."""
    if not XROCKET_TOKEN:
        return False, None, "не задан XROCKET_TOKEN"
    payload = {
        "asset": "USDT",
        "amount": str(amount_cents / 100.0),
        "description": f"Withdraw for {uid}",
        "targetType": "telegram_user_id",
        "target": str(uid),
    }
    try:
        r = requests.post(f"{XR_BASE}/cheques", json=payload, headers=xr_headers(), timeout=15)
    except Exception as e:
        return False, None, f"сеть: {e}"
    data = _json(r)
    if r.status_code in (200, 201):
        d = data.get("data") if isinstance(data.get("data"), dict) else data
        links = d.get("links") or {}
        link = links.get("telegramMiniAppLink") or links.get("telegramBotLink") or d.get("link")
        if link:
            return True, link, "Чек создан"
        log.error("Cheque response without link: %s", data)
        return False, None, "ответ xRocket без ссылки на чек"
    log.error("Cheque error %s: %s", r.status_code, r.text[:500])
    return False, None, _api_error(r, data)


def xr_balances():
    """Возвращает (status_code, список (asset, balance))."""
    r = requests.get(f"{XR_BASE}/balances", headers={"Authorization": f"Bearer {XROCKET_TOKEN}"}, timeout=15)
    data = _json(r)
    if r.status_code != 200:
        return r.status_code, []
    raw = data.get("balances")
    if raw is None and isinstance(data.get("data"), dict):
        raw = data["data"].get("balances")
    if raw is None and isinstance(data.get("data"), list):
        raw = data["data"]
    result = []
    for b in raw or []:
        name = str(b.get("asset") or b.get("currency") or "?")
        try:
            bal = float(b.get("balance", 0) or 0)
        except (TypeError, ValueError):
            bal = 0.0
        result.append((name, bal))
    return 200, result


# ───────────────────────── МЕНЮ ─────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    uid = update.effective_user.id
    if is_banned(uid) and not is_admin(uid):
        return
    existed = db.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone() is not None
    u = get_user(uid)
    if not existed and ctx.args and ctx.args[0].startswith("ref_"):
        try:
            ref = int(ctx.args[0][4:])
        except ValueError:
            ref = 0
        if ref and ref != uid and db.execute("SELECT 1 FROM users WHERE id=?", (ref,)).fetchone():
            db.execute("UPDATE users SET referrer=? WHERE id=?", (ref, uid))
            db.commit()
            try:
                await ctx.bot.send_message(ref, "👥 По вашей ссылке зарегистрировался новый игрок!")
            except Exception:
                pass
    ctx.user_data.clear()
    await update.message.reply_text(
        f"🎰 <b>Добро пожаловать!</b>\n\n💰 Баланс: <b>{m(u['balance'])}</b>",
        reply_markup=MAIN_KB, parse_mode=HTML,
    )


async def cmd_kazna(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not XROCKET_TOKEN:
        await update.effective_message.reply_text("❌ Нет токена xRocket.")
        return
    try:
        code, items = await asyncio.to_thread(xr_balances)
    except Exception as e:
        await update.effective_message.reply_text(f"❌ Ошибка: {e}")
        return
    if code != 200:
        await update.effective_message.reply_text(f"❌ Ошибка xRocket: {code}")
        return
    lines = []
    for name, bal in items:
        if bal > 0:
            txt = f"{bal:.2f}" if bal >= 0.01 else f"{bal:.8f}".rstrip("0")
            lines.append(f"• {txt} {name.lower()}")
    if not lines:
        await update.effective_message.reply_text("💰 На балансе нет средств.")
        return
    await update.effective_message.reply_text("🏦 <b>Казна xRocket:</b>\n" + "\n".join(lines), parse_mode=HTML)


async def show_balance(update, ctx):
    u = get_user(update.effective_user.id)
    kb = IM([
        [IB("💳 Пополнить", callback_data="dep:start"), IB("💸 Вывести", callback_data="wd:start")],
        [IB("🎮 Играть", callback_data="menu:play")],
    ])
    await respond(update, f"💰 <b>Баланс:</b> {m(u['balance'])}\n♻️ <b>Рейкбек:</b> {m(u['rakeback'])}", kb)


async def show_menu(update, ctx):
    uid = update.effective_user.id
    u = get_user(uid)
    bet = get_bet(ctx, uid)
    rows = [
        [IB("🎮 Играть", callback_data="menu:play"), IB("👤 Профиль", callback_data="menu:profile")],
        [IB("🎁 Ежедневный бонус", callback_data="daily:claim"), IB("👥 Рефералка", callback_data="ref:show")],
        [IB("💬 Заработок в чате", callback_data="earn:show")],
        [IB("💳 Пополнить", callback_data="dep:start"), IB("💸 Вывести", callback_data="wd:start")],
    ]
    links = []
    for r in db.execute("SELECT chat, kind FROM channels WHERE official=1 ORDER BY kind, rowid").fetchall():
        if r["chat"].startswith("@"):
            links.append(IB(("📢 Канал" if r["kind"] == "channel" else "💬 Чат"), url=f"https://t.me/{r['chat'][1:]}"))
    if links:
        rows.append(links[:4])
    await respond(
        update,
        f"📋 <b>Меню</b>\n\n💰 Баланс: <b>{m(u['balance'])}</b>\n💵 Ставка: <b>{m(bet) if bet else 'не выбрана'}</b>\n\n"
        "Быстрая ставка: <code>ставка 1.5</code> · на весь баланс: <code>вб</code>\n"
        "Перевод игроку: ответьте на его сообщение в чате: <code>нк 1.5</code>",
        IM(rows),
    )


async def show_profile(update, ctx):
    uid = update.effective_user.id
    u = get_user(uid)
    idx = rank_index(u["turnover"])
    _, name, emo, _ = RANKS[idx]
    if idx == len(RANKS) - 1:
        prog = "📈 Максимальный ранг!"
    else:
        prog = f"📈 До «{RANKS[idx+1][2]} {RANKS[idx+1][1]}»: осталось {m(RANKS[idx+1][0] - u['turnover'])}"
    text = (
        f"👤 <b>Профиль</b>\n🆔 ID: <code>{uid}</code>\n💰 Баланс: <b>{m(u['balance'])}</b>\n"
        f"♻️ Рейкбек: <b>{m(u['rakeback'])}</b>\n🎰 Оборот: <b>{m(u['turnover'])}</b>\n"
        f"🏅 Ранг: <b>{emo} {name}</b>\n{prog}"
    )
    rows = []
    if idx > u["rank_claimed"]:
        bonus = sum(RANKS[i][3] for i in range(u["rank_claimed"] + 1, idx + 1))
        rows.append([IB(f"🎁 Бонус за ранг (+{m(bonus)})", callback_data="rank:claim")])
    if u["rakeback"] > 0:
        rows.append([IB(f"♻️ Забрать рейкбек ({m(u['rakeback'])})", callback_data="rb:claim")])
    rows.append([IB("🎮 Играть", callback_data="menu:play")])
    await respond(update, text, IM(rows))


async def show_games(update, ctx):
    uid = update.effective_user.id
    if await need_sub(update, ctx, uid):
        return

    def gb(k):
        return IB(GAME_TITLES[k], callback_data=f"g:{k}")

    text = (
        "🎮 <b>Игровое меню</b>\n\n"
        "🎲 <b>Кубики и спорт</b> — быстрые исходы\n"
        "💎 <b>На риск</b> — растущий множитель\n\n"
        "💡 Ставка: <code>ставка 1.5</code>. Ставить можно текстом: "
        "<code>куб чет</code>, <code>дартс белый</code>, <code>башня</code>.\n"
        "Если ставка не задана, её выберете перед игрой.\n\n"
        "Выберите игру 👇"
    )
    rows = [
        [IB("━━ 🎲 КУБИКИ И СПОРТ ━━", callback_data="noop")],
        [gb("dice"), gb("darts")],
        [gb("football"), gb("basket")],
        [gb("bowling"), gb("slots")],
        [IB("━━ 💎 НА РИСК ━━", callback_data="noop")],
        [gb("crash"), gb("mines"), gb("tower")],
    ]
    await respond(update, text, IM(rows))


# ───────────────────────── ИГРЫ ─────────────────────────
async def show_bet_menu(update, ctx, game):
    uid = update.effective_user.id
    u = get_user(uid)
    if game == "x":
        ctx.user_data.pop("game", None)
    else:
        ctx.user_data["game"] = game
    cur = get_bet(ctx, uid)
    text = (
        f"<b>{GAME_TITLES.get(game, '💵 Выбор ставки')}</b>\n💰 Баланс: <b>{m(u['balance'])}</b>\n"
        + (f"💵 Ставка: <b>{m(cur)}</b>\n" if cur else "")
        + "\nВыберите ставку:"
    )
    rows = [
        [IB(m(a), callback_data=f"b:{game}:{a}") for a in [10, 50, 100]],
        [IB(m(a), callback_data=f"b:{game}:{a}") for a in [200, 500, 1000]],
        [IB("✍️ Ввести сумму", callback_data=f"b:{game}:in"), IB("💰 Вся сумма", callback_data=f"b:{game}:all")],
    ]
    if u["custom_bet"] > 0:
        rows.append([IB(f"⭐ Своя ставка ({m(u['custom_bet'])})", callback_data=f"b:{game}:cu")])
    rows.append([IB("⬅️ Назад", callback_data="menu:play")])
    await respond(update, text, IM(rows))


async def next_step(update, ctx):
    uid = update.effective_user.id
    game, bet = ctx.user_data.get("game"), get_bet(ctx, uid)
    if not game or not bet:
        return await show_games(update, ctx)
    head = f"<b>{GAME_TITLES[game]}</b>\n💵 Ставка: <b>{m(bet)}</b>\n\n"
    chg = [IB("💵 Сменить ставку", callback_data=f"gs:{game}"), IB("🎮 Другие игры", callback_data="menu:play")]
    if game == "slots":
        opts = DICE["slots"]["opts"]
        rows = []
        for r in (1, 2, 3):
            kk = f"r{r}b"
            rows.append([IB(f"━ Слот {r} · x{fm(64 / len(opts[kk][1]))} ━", callback_data="noop")])
            rows.append([IB(SLOT_NAMES[i], callback_data=f"p:slots:r{r}{SLOT_KEYS[i]}") for i in range(4)])
        rows.append([
            IB(f"Уникальные · x{fm(64 / len(opts['uniq'][1]))}", callback_data="p:slots:uniq"),
            IB(f"Три одинак. · x{fm(64 / len(opts['tri'][1]))}", callback_data="p:slots:tri"),
        ])
        rows.append([IB(f"Джекпот 777 · x{fm(64 / len(opts['jp'][1]))}", callback_data="p:slots:jp")])
        rows.append(chg)
        await respond(update, head + "Выберите исход:", IM(rows))
    elif game in DICE:
        cfg = DICE[game]
        multi = MULTI.get(game)
        mode = ctx.user_data.get(f"mode_{game}", 1) if multi else 1
        if multi and mode == 2:
            items = list(multi.items())
            rows = [
                [IB(f"{lab} · x{fm(36 / len(v))}", callback_data=f"q:{game}:{k}") for k, (lab, v) in items[i:i + 2]]
                for i in range(0, len(items), 2)
            ]
        else:
            items = list(cfg["opts"].items())
            rows = [
                [IB(f"{lab} · x{fm(cfg['n'] / len(v))}", callback_data=f"p:{game}:{k}") for k, (lab, v) in items[i:i + 2]]
                for i in range(0, len(items), 2)
            ]
        if multi:
            one, two = ("1 куб", "2 куба") if game == "dice" else ("1 бросок", "2 броска")
            rows.insert(0, [
                IB(("✅ " if mode == 1 else "") + one, callback_data=f"dm:{game}:1"),
                IB(("✅ " if mode == 2 else "") + two, callback_data=f"dm:{game}:2"),
            ])
        rows.append(chg)
        await respond(update, head + "Выберите исход:", IM(rows))
    elif game == "crash":
        rows = [[IB(f"x{x}", callback_data=f"cr:{x}") for x in CRASH_MULTS[i:i + 4]] for i in (0, 4)]
        rows.append(chg)
        await respond(update, head + "Выберите множитель.", IM(rows))
    elif game == "mines":
        nums = list(range(2, 13))
        rows = [[IB(f"💣 {n}", callback_data=f"mc:{n}") for n in nums[i:i + 4]] for i in range(0, len(nums), 4)]
        rows.append(chg)
        await respond(update, head + "Выберите количество мин (2–12):", IM(rows))
    elif game == "tower":
        rows = [[IB("🏰 Начать игру", callback_data="tw:start")], chg]
        await respond(update, head + "6 уровней, 5 дверей на каждом, 1 мина на уровень.", IM(rows))


async def play_dice(update, ctx, game, key, two=False):
    q = update.callback_query
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    bet = get_bet(ctx, uid)
    cfg = DICE.get(game)
    if not cfg or not bet:
        return "Ошибка ставки"
    if two:
        multi = MULTI.get(game)
        if not multi or key not in multi:
            return "Ошибка ставки"
        label, vals = multi[key]
        mult = 36 / len(vals)
        title = "🎲🎲 Два кубика" if game == "dice" else "🎯🎯 Два броска"
        log_game, rolls = f"{game}2", 2
    else:
        if key not in cfg["opts"]:
            return "Ошибка ставки"
        label, vals = cfg["opts"][key]
        mult = cfg["n"] / len(vals)
        title, log_game, rolls = GAME_TITLES[game], game, 1
    if uid in BUSY:
        return "Подождите ⏳"
    BUSY.add(uid)
    paid = False
    settled = False
    try:
        if not place_bet(uid, bet):
            return "❌ Недостаточно средств"
        paid = True
        status = (
            f"<b>{title}</b>\n💵 Ставка: <b>{m(bet)}</b>\n🎯 Исход: <b>{label}</b> (x{fm(mult)})\n\n⏳ Бросаем..."
        )
        if q:
            await q.answer()
            await q.edit_message_text(status, parse_mode=HTML)
        else:
            await update.effective_message.reply_text(status, parse_mode=HTML)
        msgs = []
        for _ in range(rolls):
            msgs.append(await ctx.bot.send_dice(chat_id=chat_id, emoji=cfg["emoji"]))
        await asyncio.sleep(4)
        values = [x.dice.value for x in msgs]
        win = (tuple(values) in vals) if two else (values[0] in vals)
        payout = calc(bet, mult) if win else 0
        if game == "slots":
            shown = " ".join(SLOT_NAMES[i] for i in slot_reels(values[0]))
        else:
            shown = " и ".join(str(v) for v in values)
        settle(uid, log_game, bet, payout, f"{label}; выпало {shown}")
        settled = True
        if win:
            res = f"🎉 <b>ПОБЕДА!</b>\n💵 Выплата: <b>+{m(payout)}</b>"
        else:
            res = f"😔 <b>Проигрыш</b>\n💸 −{m(bet)}"
        text = (
            f"<b>{title}</b>\n🎯 Ваш исход: <b>{label}</b>\n🎲 Выпало: <b>{shown}</b>\n\n{res}\n\n"
            f"💰 Баланс: <b>{m(get_user(uid)['balance'])}</b>"
        )
        repeat_cb = f"q:{game}:{key}" if two else f"p:{game}:{key}"
        await ctx.bot.send_message(chat_id, text, reply_markup=IM(end_rows(repeat_cb, game, bet=bet)), parse_mode=HTML)
    except Exception:
        if paid and not settled:
            add_balance(uid, bet)
        raise
    finally:
        BUSY.discard(uid)
    return None


async def play_crash(update, ctx, mult):
    uid = update.effective_user.id
    bet = get_bet(ctx, uid)
    if mult not in CRASH_MULTS or not bet:
        return "Ошибка"
    if not place_bet(uid, bet):
        return "❌ Недостаточно средств"
    win = random.random() < 1.0 / mult
    payout = calc(bet, mult) if win else 0
    settle(uid, "crash", bet, payout, f"x{mult}")
    if win:
        res = f"🚀 <b>Выиграл x{mult}!</b>\n💵 Выплата: <b>+{m(payout)}</b>"
    else:
        res = f"💥 <b>Краш, проигрыш</b>\n💸 −{m(bet)}"
    kb = IM(end_rows(f"cr:{mult}", "crash", bet=bet))
    await respond(
        update,
        f"<b>🚀 Краш</b>\n🎯 Множитель: <b>x{mult}</b>\n\n{res}\n\n💰 Баланс: <b>{m(get_user(uid)['balance'])}</b>",
        kb,
    )
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
    if not reveal and len(st["opened"]) > 0:
        rows.append([IB(f"💰 Забрать {m(calc(st['bet'], mines_mult(st['m'], len(st['opened']))))}", callback_data="mn:cash")])
    if extra:
        rows.extend(extra)
    return IM(rows)


def mines_text(st):
    k = len(st["opened"])
    cur = mines_mult(st["m"], k)
    return (
        f"<b>💣 Мины</b>\n💵 Ставка: <b>{m(st['bet'])}</b>\n💎 Открыто: <b>{k}</b> / {25 - st['m']}\n"
        f"📈 Множитель: <b>x{cur:.2f}</b>\n💰 Выплата: <b>{m(calc(st['bet'], cur)) if k else m(0)}</b>"
    )


async def mines_start(update, ctx, n):
    uid = update.effective_user.id
    bet = get_bet(ctx, uid)
    if uid in MINES_STATE:
        st = MINES_STATE[uid]
        await respond(update, mines_text(st), mines_kb(st))
        return "У вас уже идёт игра"
    if not bet or not 2 <= n <= 12:
        return "Ошибка ставки"
    if not place_bet(uid, bet):
        return "❌ Недостаточно средств"
    st = {"bet": bet, "m": n, "mines": set(random.sample(range(25), n)), "opened": set()}
    MINES_STATE[uid] = st
    save_game("mines", uid, st)
    await respond(update, mines_text(st), mines_kb(st))
    return None


async def mines_click(update, ctx, arg):
    uid = update.effective_user.id
    st = MINES_STATE.get(uid)
    if not st:
        return "Игра не найдена"
    after = end_rows(f"mc:{st['m']}", "mines", bet=st["bet"])
    if arg == "cash":
        if len(st["opened"]) == 0:
            return "Откройте клетку"
        drop_game("mines", uid)
        mult = mines_mult(st["m"], len(st["opened"]))
        payout = calc(st["bet"], mult)
        settle(uid, "mines", st["bet"], payout, f"x{mult:.2f}")
        await respond(update, f"🎉 <b>Выигрыш {m(payout)}!</b>", mines_kb(st, reveal=True, extra=after))
        return None
    try:
        i = int(arg)
    except ValueError:
        return "Ошибка"
    if i in st["opened"]:
        return None
    if i in st["mines"]:
        drop_game("mines", uid)
        settle(uid, "mines", st["bet"], 0)
        await respond(update, f"💥 <b>БУМ! Проигрыш {m(st['bet'])}</b>", mines_kb(st, reveal=True, hit=i, extra=after))
        return None
    st["opened"].add(i)
    save_game("mines", uid, st)
    if len(st["opened"]) == 25 - st["m"]:
        drop_game("mines", uid)
        payout = calc(st["bet"], mines_mult(st["m"], len(st["opened"])))
        settle(uid, "mines", st["bet"], payout)
        await respond(update, f"🏆 <b>Всё открыто! Выигрыш {m(payout)}</b>", mines_kb(st, reveal=True, extra=after))
        return None
    await respond(update, mines_text(st), mines_kb(st))
    return None


# ───────────────────────── БАШНЯ (сетка 6x5, все этажи видны) ─────────────────────────
def tower_text(st):
    lines = [
        "🏰 <b>Башня</b>",
        f"💵 Ставка: <b>{m(st['bet'])}</b>",
        "📈 Множители (снизу вверх): " + " → ".join(f"x{x:.1f}" for x in TOWER_MULTS),
        "🟢 твой ход · ✅ безопасно · 💣 мина · 💥 взрыв",
    ]
    if st["level"] > 0:
        cur = TOWER_MULTS[st["level"] - 1]
        lines.append(
            f"✅ Пройдено: <b>{st['level']}</b>/{TOWER_LEVELS} · сейчас <b>x{cur:.1f}</b> → <b>{m(calc(st['bet'], cur))}</b>"
        )
    else:
        lines.append("Выберите дверь на нижнем уровне 👇")
    return "\n".join(lines)


def tower_picks(st):
    """Список выбранных дверей по уровням (дополняется None для старых сохранений)."""
    picks = list(st.get("picks") or [])
    while len(picks) < st["level"]:
        picks.append(None)
    return picks


def tower_kb(st, reveal=False, extra=None):
    """Сетка: верхний ряд — уровень 6, нижний — уровень 1."""
    rows = []
    for lvl in range(TOWER_LEVELS - 1, -1, -1):
        row = []
        for d in range(TOWER_DOORS):
            trap = st["traps"][lvl]
            if lvl < st["level"]:
                # пройденный уровень: 🟢 — куда вы пошли, 💣 — обойдённая мина, 💥 — взрыв, ✅ — остальные безопасные
                picks = st.get("picks") or []
                chosen = picks[lvl] if lvl < len(picks) else None
                if trap == d and chosen == d:
                    sym = "💥"  # вы наступили — взрыв
                elif trap == d:
                    sym = "💣"  # мина, которую вы обошли
                elif chosen == d:
                    sym = "🟢"
                else:
                    sym = "✅"
                row.append(IB(sym, callback_data="noop"))
            elif reveal:
                # игра окончена: показываем оставшиеся мины
                row.append(IB("💣" if trap == d else "▫️", callback_data="noop"))
            elif lvl == st["level"]:
                row.append(IB("🚪", callback_data=f"tw:{d}"))
            else:
                row.append(IB("🔒", callback_data="noop"))
        rows.append(row)
    if not reveal and st["level"] > 0:
        cur = TOWER_MULTS[st["level"] - 1]
        rows.append([IB(f"💰 Забрать {m(calc(st['bet'], cur))}", callback_data="tw:cash")])
    if extra:
        rows.extend(extra)
    return IM(rows)


async def tower_click(update, ctx, arg):
    uid = update.effective_user.id
    after = None
    if arg == "start":
        if uid in TOWER_STATE:
            st = TOWER_STATE[uid]
            await respond(update, tower_text(st), tower_kb(st))
            return "Игра уже идёт"
        bet = get_bet(ctx, uid)
        if not bet or not place_bet(uid, bet):
            return "❌ Недостаточно средств"
        st = {"bet": bet, "level": 0, "traps": [random.randrange(TOWER_DOORS) for _ in range(TOWER_LEVELS)]}
        TOWER_STATE[uid] = st
        save_game("tower", uid, st)
        await respond(update, tower_text(st), tower_kb(st))
        return None
    st = TOWER_STATE.get(uid)
    if not st:
        return "Игра не найдена"
    after = end_rows("tw:start", "tower", bet=st["bet"])
    if arg == "cash":
        if st["level"] == 0:
            return "Пройдите уровень"
        drop_game("tower", uid)
        payout = calc(st["bet"], TOWER_MULTS[st["level"] - 1])
        settle(uid, "tower", st["bet"], payout, f"x{TOWER_MULTS[st['level'] - 1]:.1f}")
        await respond(update, f"💰 <b>Вы забрали {m(payout)}</b>", tower_kb(st, reveal=True, extra=after))
        return None
    try:
        d = int(arg)
    except ValueError:
        return "Ошибка"
    if not 0 <= d < TOWER_DOORS:
        return "Ошибка"
    if st["traps"][st["level"]] == d:
        drop_game("tower", uid)
        settle(uid, "tower", st["bet"], 0)
        # уровень для показа: считаем текущий пройденным, чтобы показать 💥 на нём
        shown = dict(st)
        shown["level"] = st["level"] + 1
        shown["picks"] = tower_picks(st) + [d]
        await respond(
            update,
            f"💥 <b>Мина на уровне {st['level'] + 1}! Проигрыш {m(st['bet'])}</b>",
            tower_kb(shown, reveal=True, extra=after),
        )
        return None
    st["picks"] = tower_picks(st) + [d]
    st["level"] += 1
    save_game("tower", uid, st)
    if st["level"] == TOWER_LEVELS:
        drop_game("tower", uid)
        payout = calc(st["bet"], TOWER_MULTS[-1])
        settle(uid, "tower", st["bet"], payout, f"x{TOWER_MULTS[-1]:.1f}")
        await respond(update, f"🏆 <b>Башня пройдена! Выигрыш {m(payout)}</b>", tower_kb(st, reveal=True, extra=after))
        return None
    await respond(update, tower_text(st), tower_kb(st))
    return None


# ───────────────────────── ПОПОЛНЕНИЕ / ВЫВОД ─────────────────────────
async def create_deposit(update, ctx, amount):
    uid = update.effective_user.id
    msg = update.effective_message
    if not XROCKET_TOKEN:
        await msg.reply_text("❌ Ошибка: нет токена.")
        return
    ok, link, invoice_id, err = await asyncio.to_thread(xr_create_invoice, uid, amount)
    if not ok:
        await msg.reply_text(f"❌ Не удалось создать счёт: {err}")
        return
    # invoice_id сохраняется ДО выдачи ссылки — по нему вебхук найдёт депозит
    db.execute(
        "INSERT INTO deposits(user_id, amount, status, invoice_id, ts) VALUES(?,?,?,?,?)",
        (uid, amount, "pending", str(invoice_id), int(time.time())),
    )
    db.commit()
    await msg.reply_text(
        f"💳 <b>Счёт на {m(amount)}</b>\nОплатите — баланс пополнится автоматически:",
        reply_markup=IM([[IB("💳 Оплатить", url=link)]]), parse_mode=HTML,
    )


async def create_withdraw(update, ctx, amount):
    uid = update.effective_user.id
    if not is_admin(uid):
        dep = db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM deposits WHERE user_id=? AND status='approved'", (uid,)
        ).fetchone()[0]
        if dep < WD_MIN_DEPOSITED:
            await update.effective_message.reply_text(
                f"❌ Вывод доступен после пополнения баланса от {m(WD_MIN_DEPOSITED)}. Стартовый бонус не выводится."
            )
            return
        need = int(dep * WD_TURNOVER_X) - get_user(uid)["turnover"]
        if need > 0:
            await update.effective_message.reply_text(
                f"❌ Сначала отыграйте внесённые средства: осталось оборота {m(need)}."
            )
            return
    c = db.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", (amount, uid, amount))
    db.commit()
    if c.rowcount != 1:
        await update.effective_message.reply_text("❌ Недостаточно средств.")
        return
    cur = db.execute(
        "INSERT INTO withdrawals(user_id, amount, status, ts) VALUES(?,?,?,?)",
        (uid, amount, "pending", int(time.time())),
    )
    db.commit()
    await update.effective_message.reply_text(
        f"💸 <b>Заявка на вывод #{cur.lastrowid} на {m(amount)}</b>\nОжидайте.", parse_mode=HTML,
    )
    try:
        await ctx.bot.send_message(
            ADMIN_ID, f"🔔 Вывод #{cur.lastrowid}: {m(amount)} от <code>{uid}</code>. /admin", parse_mode=HTML,
        )
    except Exception:
        log.exception("Не удалось уведомить админа о выводе")


# ───────────────────────── АДМИНКА ─────────────────────────
ADMIN_KB = IM([
    [IB("📊 Статистика", callback_data="adm:stats"), IB("👤 Игрок по ID", callback_data="adm:user")],
    [IB("💸 Выводы", callback_data="adm:wd"), IB("💳 Пополнения", callback_data="adm:dep")],
    [IB("📢 Подписки и чаты", callback_data="adm:subs"), IB("✉️ Рассылка", callback_data="adm:bc")],
    [IB("🚫 Бан/разбан", callback_data="adm:ban"), IB("💰 Баланс игрока", callback_data="adm:bal")],
    [IB("💬 Заработок в чате", callback_data="adm:earn"), IB("📜 Журнал", callback_data="adm:log")],
    [IB("💾 Бэкап БД", callback_data="adm:backup")],
])


def adm_kb(kind, rid):
    if kind == "wd":
        return IM([
            [IB("✅ Одобрить (чек)", callback_data=f"adm:wok:{rid}"), IB("❌ Отклонить", callback_data=f"adm:wno:{rid}")],
            [IB("✍️ Выплачено вручную", callback_data=f"adm:wman:{rid}")],
        ])
    return IM([[IB("✅ Одобрить", callback_data=f"adm:dok:{rid}"), IB("❌ Отклонить", callback_data=f"adm:dno:{rid}")]])


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    ctx.user_data.pop("await", None)
    await update.message.reply_text("🛠 <b>Админ-панель</b>", reply_markup=ADMIN_KB, parse_mode=HTML)


async def cb_admin(update, ctx, parts):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        return "Нет доступа"
    act = parts[1]
    if act == "stats":
        r = db.execute("SELECT COUNT(*) c, COALESCE(SUM(balance),0) b, COALESCE(SUM(turnover),0) t FROM users").fetchone()
        pw = db.execute("SELECT COUNT(*) FROM withdrawals WHERE status='pending'").fetchone()[0]
        pd = db.execute("SELECT COUNT(*) FROM deposits WHERE status='pending'").fetchone()[0]
        g = db.execute("SELECT COALESCE(SUM(bet),0) b, COALESCE(SUM(payout),0) p FROM bets").fetchone()
        dep_total = db.execute("SELECT COALESCE(SUM(amount),0) FROM deposits WHERE status='approved'").fetchone()[0]
        wd_total = db.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status='approved'").fetchone()[0]
        per_game = db.execute(
            "SELECT game, COUNT(*) n, SUM(bet) b, SUM(payout) p FROM bets GROUP BY game ORDER BY (SUM(bet)-SUM(payout)) DESC"
        ).fetchall()
        gl = "\n".join(
            f"• {STAT_TITLES.get(x['game'], x['game'])}: {x['n']} игр, прибыль {m(x['b'] - x['p'])}" for x in per_game
        ) or "—"
        await q.message.reply_text(
            f"📊 <b>Статистика</b>\n👥 Игроков: <b>{r['c']}</b>\n💰 Балансы игроков (долг казино): <b>{m(r['b'])}</b>\n"
            f"🎰 Оборот: <b>{m(g['b'])}</b>\n🏦 Прибыль с игр: <b>{m(g['b'] - g['p'])}</b>\n"
            f"💳 Пополнено: <b>{m(dep_total)}</b>\n💸 Выведено: <b>{m(wd_total)}</b>\n"
            f"⏳ Выводов в ожидании: <b>{pw}</b> · счетов: <b>{pd}</b>\n\n<b>По играм:</b>\n{gl}",
            parse_mode=HTML,
        )
    elif act == "user":
        ctx.user_data["await"] = "adm_user"
        await q.message.reply_text("Введите ID игрока:")
    elif act in ("wd", "dep"):
        table = "withdrawals" if act == "wd" else "deposits"
        rows = db.execute(f"SELECT * FROM {table} WHERE status='pending' ORDER BY id LIMIT 10").fetchall()
        if not rows:
            await q.message.reply_text("Нет заявок.")
        for r in rows:
            await q.message.reply_text(
                f"{'💸 Вывод' if act == 'wd' else '💳 Пополнение'} #{r['id']}\n"
                f"Игрок: {r['user_id']}\nСумма: {m(r['amount'])}",
                reply_markup=adm_kb(act, r["id"]),
            )
    elif act in ("wok", "wno", "wman", "dok", "dno"):
        return await admin_process(update, ctx, act, int(parts[2]))
    elif act == "subs":
        await show_subs(update)
    elif act == "addch":
        ctx.user_data["await"] = "adm_addch"
        ctx.user_data["ch_kind"] = parts[2] if len(parts) > 2 and parts[2] in ("channel", "chat") else "channel"
        await q.message.reply_text("Отправьте @username (для чата можно числовой ID вида -100…). Бот должен быть участником/админом.")
    elif act in ("chof", "chrq"):
        rid = int(parts[2])
        r = db.execute("SELECT rowid AS rid, * FROM channels WHERE rowid=?", (rid,)).fetchone()
        if not r:
            return "Не найдено"
        if act == "chof":
            if not r["official"]:
                cnt = db.execute("SELECT COUNT(*) FROM channels WHERE kind=? AND official=1", (r["kind"],)).fetchone()[0]
                if cnt >= 2:
                    return "Максимум 2 официальных " + ("канала" if r["kind"] == "channel" else "чата")
            db.execute("UPDATE channels SET official=? WHERE rowid=?", (0 if r["official"] else 1, rid))
        else:
            db.execute("UPDATE channels SET req=? WHERE rowid=?", (0 if r["req"] else 1, rid))
        db.commit()
        await show_subs(update)
    elif act == "delch":
        db.execute("DELETE FROM channels WHERE rowid=?", (int(parts[2]),))
        db.commit()
        await show_subs(update)
    elif act == "bc":
        ctx.user_data["await"] = "adm_bc"
        await q.message.reply_text("Текст для рассылки:")
    elif act == "home":
        await respond(update, "🛠 <b>Админ-панель</b>", ADMIN_KB)
    elif act == "ban":
        ctx.user_data["await"] = "adm_ban"
        await q.message.reply_text("Введите ID игрока (если уже заблокирован — будет разбан):")
    elif act == "bal":
        ctx.user_data["await"] = "adm_bal"
        await q.message.reply_text("Формат: ID сумма. Например «123456 +5» (начислить) или «123456 -2.5» (списать):")
    elif act == "log":
        rows = db.execute("SELECT * FROM admin_log ORDER BY id DESC LIMIT 15").fetchall()
        await q.message.reply_text(
            "📜 Журнал:\n" + "\n".join(f"{time.strftime('%m-%d %H:%M', time.gmtime(r['ts']))} {r['action']}" for r in rows)
            if rows else "Журнал пуст."
        )
    elif act == "backup":
        try:
            await maybe_backup(ctx.bot, force=True)
            return "💾 Бэкап отправлен"
        except Exception as e:
            return f"❌ {e}"[:190]
    elif act == "earn":
        await show_earn_admin(update)
    elif act == "etoggle":
        sset("earn_on", "0" if sget("earn_on") == "1" else "1")
        alog(f"Заработок в чате: {'вкл' if sget('earn_on') == '1' else 'выкл'}")
        await show_earn_admin(update)
    elif act == "ttoggle":
        new = "0" if sget("top_on") == "1" else "1"
        sset("top_on", new)
        if new == "1" and not sget("last_top_day"):
            sset("last_top_day", day_str())
        alog(f"Награда топу: {'вкл' if new == '1' else 'выкл'}")
        await show_earn_admin(update)
    elif act in ("erate", "ecap", "tsum", "tn"):
        prompts = {
            "erate": "Сколько $ платить за сообщение? Например 0.001",
            "ecap": "Лимит оплачиваемых сообщений в день на игрока (число):",
            "tsum": "Сколько $ получает каждый из топа? Например 0.5",
            "tn": "Сколько мест в топе (1–10)?",
        }
        ctx.user_data["await"] = "adm_" + act
        await q.message.reply_text(prompts[act])
    elif act == "ctnew":
        if not official_chats("chat"):
            return "❌ Нет официального чата (⭐ в «Подписки»)"
        ctx.user_data["await"] = "adm_ct_prize"
        await q.message.reply_text("Приз конкурса в $ (например 1):")
    elif act == "tpay":
        cnt = await pay_top(ctx.bot, day_str())
        db.execute("DELETE FROM chat_msgs WHERE day=?", (day_str(),))
        db.commit()
        await q.message.reply_text(f"🏆 Выплачено игрокам: {cnt}. Счётчик дня сброшен.")
    return None


async def admin_process(update, ctx, action, rid):
    q = update.callback_query
    is_wd = action.startswith("w")
    table = "withdrawals" if is_wd else "deposits"

    # атомарно «захватываем» заявку — защита от двойного нажатия и от гонки с вебхуком
    cur = db.execute(f"UPDATE {table} SET status='processing' WHERE id=? AND status='pending'", (rid,))
    db.commit()
    if cur.rowcount != 1:
        return "Уже обработано"
    r = db.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()
    uid, amount = r["user_id"], r["amount"]
    base_text = (q.message.text or "").split("\n\n⚠️")[0].split("\n\n➡️")[0]

    final_status = "approved"
    user_msg = None
    user_kb = None

    if action == "wok":
        ok, link, info = await asyncio.to_thread(xr_create_cheque, uid, amount)
        if not ok:
            # чек не создан — заявка остаётся в ожидании, баланс игрока не трогаем
            db.execute(f"UPDATE {table} SET status='pending' WHERE id=?", (rid,))
            db.commit()
            try:
                await q.edit_message_text(
                    f"{base_text}\n\n⚠️ Чек НЕ создан: {info}\n"
                    f"Заявка осталась в ожидании: повторите, отклоните (вернёт деньги) или выплатите вручную.",
                    reply_markup=adm_kb("wd", rid),
                )
            except Exception:
                pass
            return "❌ Чек не создан, см. сообщение"
        note = f"✅ Вывод #{rid} одобрен, чек создан."
        user_msg = f"💸 Вывод на {m(amount)} одобрен!\nПолучите средства:"
        user_kb = IM([[IB("🎁 Получить", url=link)]])
    elif action == "wman":
        note = f"✅ Вывод #{rid} отмечен как выплаченный вручную."
        user_msg = f"💸 Вывод #{rid} на {m(amount)} одобрен. Средства отправлены."
    elif action == "wno":
        add_balance(uid, amount)
        final_status = "rejected"
        note = f"❌ Вывод #{rid} отклонён, {m(amount)} возвращены на баланс."
        user_msg = note
    elif action == "dok":
        add_balance(uid, amount)
        note = f"✅ Пополнение #{rid} подтверждено."
        user_msg = f"✅ На ваш баланс зачислено {m(amount)}!"
    else:
        final_status = "rejected"
        note = f"❌ Пополнение #{rid} отклонено."
        user_msg = note

    db.execute(f"UPDATE {table} SET status=? WHERE id=?", (final_status, rid))
    db.commit()
    try:
        await q.edit_message_text(f"{base_text}\n\n➡️ Статус: {note}")
    except Exception:
        pass
    if user_msg:
        try:
            await ctx.bot.send_message(uid, user_msg, reply_markup=user_kb)
        except Exception:
            log.warning("Не удалось уведомить пользователя %s", uid)
    return None


# ───────────────────────── НАСТРОЙКИ В БД, БАН, ЖУРНАЛ ─────────────────────────
MICRO = 10000  # 1 цент = 10000 микро-единиц (для начислений «чуть-чуть $» за сообщения и рефералку)
SETTING_DEFAULTS = {
    "earn_on": "0", "earn_rate": "1000", "earn_cap": "200", "earn_cd": "15",
    "top_on": "0", "top_n": "3", "top_sum": "10", "last_top_day": "", "last_backup": "0",
}
EARN_LAST = {}
DAILY_STEPS = [5, 10, 15, 20, 25, 30, 50]          # центы по дням серии
REF_BP = int(os.environ.get("REF_BP", "50"))       # реферал: 50 = 0.5% оборота приглашённого
REF_MIN = 10                                       # мин. вывод рефбонуса, центы
POLL = {"until": 0}


def sget(key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else SETTING_DEFAULTS.get(key, "")


def sset(key, value):
    db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (key, str(value)))
    db.commit()


def sint(key):
    try:
        return int(float(sget(key) or 0))
    except ValueError:
        return 0


def alog(action):
    db.execute("INSERT INTO admin_log(admin_id, action, ts) VALUES(?,?,?)", (ADMIN_ID, action, int(time.time())))
    db.commit()


def is_banned(uid):
    return db.execute("SELECT 1 FROM bans WHERE user_id=?", (uid,)).fetchone() is not None


def fu(units):
    return f"${units / (MICRO * 100):.3f}"


def day_str(offset=0):
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() - offset * 86400))


def parse_dollars(s):
    try:
        v = float(s.replace("$", "").replace(",", ".").strip())
    except ValueError:
        return None
    return v if v == v and v >= 0 else None


def norm_chat(text):
    t = re.sub(r"^https?://t\.me/", "", text.strip())
    if re.fullmatch(r"-?\d+", t):
        return t
    t = t.lstrip("@")
    if re.fullmatch(r"[A-Za-z0-9_]{4,}", t):
        return "@" + t
    return None


def chat_target(chat):
    return chat if chat.startswith("@") else int(chat)


def official_chats(kind="chat"):
    return [r["chat"] for r in db.execute("SELECT chat FROM channels WHERE kind=? AND official=1 ORDER BY rowid", (kind,)).fetchall()]


def chat_is_official(chat_obj):
    for c in official_chats("chat"):
        if c.startswith("@"):
            if chat_obj.username and c.lower() == "@" + chat_obj.username.lower():
                return True
        elif str(chat_obj.id) == c.strip():
            return True
    return False


# ───────────────────────── БЭКАП ─────────────────────────
def make_backup_file():
    path = f"/tmp/casino_backup_{int(time.time())}.db"
    src = sqlite3.connect(DB_PATH, timeout=15)
    dst = sqlite3.connect(path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return path


async def maybe_backup(bot, force=False):
    if not force and time.time() - sint("last_backup") < 86400:
        return False
    path = await asyncio.to_thread(make_backup_file)
    try:
        with open(path, "rb") as f:
            await bot.send_document(ADMIN_ID, f, filename=f"casino_{day_str()}.db", caption=f"💾 Бэкап БД {day_str()}")
        sset("last_backup", int(time.time()))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return True


async def cmd_backup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        await maybe_backup(ctx.bot, force=True)
    except Exception as e:
        await update.effective_message.reply_text(f"❌ Бэкап не отправлен: {e}")


# ───────────────────────── ПРОВЕРКА ОПЛАТЫ БЕЗ ВЕБХУКА ─────────────────────────
def poll_deposits_sync():
    """Опрашивает неоплаченные счета в xRocket. Возвращает [(user_id, amount)] зачисленных."""
    if not XROCKET_TOKEN or time.time() < POLL["until"]:
        return []
    now = int(time.time())
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("UPDATE deposits SET status='expired' WHERE status='pending' AND ts<?", (now - 7 * 86400,))
        conn.commit()
        rows = conn.execute(
            "SELECT * FROM deposits WHERE status='pending' AND invoice_id IS NOT NULL AND ts<? ORDER BY id LIMIT 20",
            (now - 30,),
        ).fetchall()
    finally:
        conn.close()
    done = []
    for r in rows:
        try:
            resp = requests.get(f"{XR_BASE}/invoices/{r['invoice_id']}", headers=xr_headers(), timeout=15)
        except Exception as e:
            log.warning("poll invoice %s: %s", r["invoice_id"], e)
            continue
        if resp.status_code in (404, 405):
            log.warning("Опрос счетов: метод недоступен (%s), пауза на час; зачисление идёт через вебхук", resp.status_code)
            POLL["until"] = now + 3600
            break
        if resp.status_code != 200:
            continue
        data = _json(resp)
        d = data.get("data") if isinstance(data.get("data"), dict) else data
        if str(d.get("status") or "").lower() == "paid":
            uid, amount, code = credit_deposit(r["invoice_id"])
            if code == "ok":
                log.info("Автозачисление (опрос): user %s +%s", uid, m(amount))
                done.append((uid, amount))
    return done


# ───────────────────────── ЕЖЕДНЕВНЫЙ БОНУС, РЕФЕРАЛКА ─────────────────────────
async def cb_daily(update, ctx):
    uid = update.effective_user.id
    u = get_user(uid)
    now = int(time.time())
    last = u["last_daily"] or 0
    since = now - last
    if last and since < 86400:
        left = 86400 - since
        return f"⏳ Следующий бонус через {left // 3600}ч {(left % 3600) // 60}м"
    streak = (u["streak"] + 1) if (last and since < 172800) else 1
    amount = DAILY_STEPS[(streak - 1) % len(DAILY_STEPS)]
    db.execute("UPDATE users SET balance=balance+?, last_daily=?, streak=? WHERE id=?", (amount, now, streak, uid))
    db.commit()
    return f"🎁 Бонус {m(amount)} зачислен! Серия: {streak} дн. Завтра — больше."


async def show_ref(update, ctx):
    uid = update.effective_user.id
    u = get_user(uid)
    me = await ctx.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{uid}"
    cnt = db.execute("SELECT COUNT(*) FROM users WHERE referrer=?", (uid,)).fetchone()[0]
    cents = u["ref_earn"] // MICRO
    text = (
        "👥 <b>Рефералка</b>\n\n"
        f"Приглашайте друзей и получайте <b>{REF_BP / 100:g}%</b> от их оборота.\n\n"
        f"🔗 Ваша ссылка:\n<code>{link}</code>\n\n"
        f"👤 Приглашено: <b>{cnt}</b>\n"
        f"💰 К выводу: <b>{m(cents)}</b> (минимум {m(REF_MIN)})"
    )
    rows = []
    if cents >= REF_MIN:
        rows.append([IB(f"💸 Забрать {m(cents)}", callback_data="ref:claim")])
    rows.append([IB("⬅️ Меню", callback_data="menu:main")])
    await respond(update, text, IM(rows))


def claim_ref(uid):
    u = get_user(uid)
    cents = u["ref_earn"] // MICRO
    if cents < REF_MIN:
        return f"Минимум {m(REF_MIN)}"
    cur = db.execute(
        "UPDATE users SET balance=balance+?, ref_earn=ref_earn-? WHERE id=? AND ref_earn>=?",
        (cents, cents * MICRO, uid, cents * MICRO),
    )
    db.commit()
    return f"✅ {m(cents)} зачислено на баланс" if cur.rowcount == 1 else "Ошибка"


# ───────────────────────── ПЕРЕВОДЫ «нк» ─────────────────────────
def wager_problem(uid):
    if is_admin(uid):
        return None
    dep = db.execute("SELECT COALESCE(SUM(amount),0) FROM deposits WHERE user_id=? AND status='approved'", (uid,)).fetchone()[0]
    if dep < WD_MIN_DEPOSITED:
        return f"❌ Переводы доступны после пополнения баланса от {m(WD_MIN_DEPOSITED)}."
    need = int(dep * WD_TURNOVER_X) - get_user(uid)["turnover"]
    if need > 0:
        return f"❌ Сначала отыграйте внесённое: осталось оборота {m(need)}."
    return None


async def do_transfer(update, ctx, amt):
    msg = update.effective_message
    sender = update.effective_user
    target = msg.reply_to_message.from_user
    if target is None or target.is_bot or target.id == sender.id:
        await msg.reply_text("❌ Нельзя передать этому пользователю.")
        return
    if is_banned(target.id):
        await msg.reply_text("❌ Получатель заблокирован.")
        return
    if not db.execute("SELECT 1 FROM users WHERE id=?", (target.id,)).fetchone():
        await msg.reply_text("❌ Получатель ещё не запускал бота.")
        return
    problem = wager_problem(sender.id)
    if problem:
        await msg.reply_text(problem)
        return
    cur = db.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", (amt, sender.id, amt))
    if cur.rowcount != 1:
        db.rollback()
        await msg.reply_text("❌ Недостаточно средств.")
        return
    db.execute("UPDATE users SET balance=balance+? WHERE id=?", (amt, target.id))
    db.execute("INSERT INTO transfers(from_id, to_id, amount, ts) VALUES(?,?,?,?)", (sender.id, target.id, amt, int(time.time())))
    db.commit()
    await msg.reply_text(
        f"✅ {html.escape(sender.full_name)} передал(а) {html.escape(target.full_name)} <b>{m(amt)}</b>",
        parse_mode=HTML,
    )
    try:
        await ctx.bot.send_message(target.id, f"💸 Вам перевели {m(amt)} от {html.escape(sender.full_name)}", parse_mode=HTML)
    except Exception:
        pass


# ───────────────────────── ЗАРАБОТОК В ЧАТЕ ─────────────────────────
def top_text(day, limit=10):
    rows = db.execute(
        "SELECT user_id, n, name FROM chat_msgs WHERE day=? AND n>0 ORDER BY n DESC LIMIT ?", (day, limit)
    ).fetchall()
    if not rows:
        return "🏆 Пока никто не писал."
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else str(i + 1) + '.'} {html.escape(r['name'] or str(r['user_id']))} — {r['n']}"
        for i, r in enumerate(rows)
    ]
    return "🏆 <b>Топ по сообщениям сегодня</b>\n" + "\n".join(lines)


async def show_earn(update, ctx):
    uid = update.effective_user.id
    u = get_user(uid)
    on = sget("earn_on") == "1"
    row = db.execute("SELECT n FROM chat_msgs WHERE day=? AND user_id=?", (day_str(), uid)).fetchone()
    n = row["n"] if row else 0
    text = (
        "💬 <b>Заработок в чате</b> <i>(экспериментальная функция)</i>\n\n"
        f"{'✅ Включён' if on else '⏸ Сейчас выключен'}\n"
        f"💵 За сообщение: <b>{fu(sint('earn_rate'))}</b>\n"
        f"📊 Оплачивается до <b>{sint('earn_cap')}</b> сообщений в день, не чаще раза в {sint('earn_cd')} с\n"
        f"✍️ Ваших сообщений сегодня: <b>{n}</b>\n"
        f"💰 Накоплено: <b>{fu(u['chat_bal'])}</b>\n\n"
        f"Перевести на игровой баланс можно от {m(10)}."
    )
    if sget("top_on") == "1":
        text += f"\n🏆 Топ-{sint('top_n')} по сообщениям получает по <b>{m(sint('top_sum'))}</b> каждый день."
    rows = []
    links = [IB("💬 Открыть чат", url=f"https://t.me/{c[1:]}") for c in official_chats("chat") if c.startswith("@")]
    if links:
        rows.append(links[:2])
    rows.append([IB("💸 Перевести на баланс", callback_data="earn:cash"), IB("🏆 Топ дня", callback_data="earn:top")])
    rows.append([IB("⬅️ Меню", callback_data="menu:main")])
    await respond(update, text, IM(rows))


def cash_chat(uid):
    u = get_user(uid)
    cents = u["chat_bal"] // MICRO
    if cents < 10:
        return f"Минимум {m(10)}. Сейчас накоплено {fu(u['chat_bal'])}"
    cur = db.execute(
        "UPDATE users SET balance=balance+?, chat_bal=chat_bal-? WHERE id=? AND chat_bal>=?",
        (cents, cents * MICRO, uid, cents * MICRO),
    )
    db.commit()
    return f"✅ {m(cents)} переведено на баланс" if cur.rowcount == 1 else "Ошибка"


async def count_chat_message(update, ctx, text):
    earn_on = sget("earn_on") == "1"
    top_on = sget("top_on") == "1"
    if not (earn_on or top_on):
        return
    user, chat = update.effective_user, update.effective_chat
    if len(text) < 4 or not chat_is_official(chat):
        return
    if not db.execute("SELECT 1 FROM users WHERE id=?", (user.id,)).fetchone():
        return
    now = time.time()
    if now - EARN_LAST.get(user.id, 0) < max(1, sint("earn_cd")):
        return
    EARN_LAST[user.id] = now
    day = day_str()
    db.execute(
        "INSERT INTO chat_msgs(day, user_id, n, name) VALUES(?,?,1,?) "
        "ON CONFLICT(day, user_id) DO UPDATE SET n=n+1, name=excluded.name",
        (day, user.id, (user.first_name or user.username or "")[:20]),
    )
    n = db.execute("SELECT n FROM chat_msgs WHERE day=? AND user_id=?", (day, user.id)).fetchone()[0]
    if earn_on and n <= sint("earn_cap"):
        db.execute("UPDATE users SET chat_bal=chat_bal+? WHERE id=?", (sint("earn_rate"), user.id))
    db.commit()


async def on_group_text(update, ctx):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    text = (msg.text or "").strip()
    if not user or user.is_bot or is_banned(user.id):
        return
    low = text.lower()
    mt = re.fullmatch(r"нк\s+([\d.,]+)\$?", low)
    if mt:
        if not msg.reply_to_message:
            await msg.reply_text("Ответьте командой «нк 1.5» на сообщение игрока.")
            return
        amt = parse_amount(mt.group(1))
        if amt is None or amt < MIN_TX:
            await msg.reply_text(f"Мин. сумма перевода {m(MIN_TX)}")
            return
        await do_transfer(update, ctx, amt)
        return
    if low == "топ":
        if chat_is_official(chat):
            await msg.reply_text(top_text(day_str()), parse_mode=HTML)
        return
    await count_chat_message(update, ctx, text)


async def pay_top(bot, day):
    n = sint("top_n") or 3
    prize = sint("top_sum")
    rows = db.execute("SELECT user_id, n, name FROM chat_msgs WHERE day=? AND n>0 ORDER BY n DESC LIMIT ?", (day, n)).fetchall()
    if not rows or prize <= 0:
        return 0
    medals = ["🥇", "🥈", "🥉"]
    lines, paid = [], 0
    for i, r in enumerate(rows):
        if is_banned(r["user_id"]):
            continue
        add_balance(r["user_id"], prize)
        paid += 1
        lines.append(f"{medals[i] if i < 3 else str(i + 1) + '.'} {html.escape(r['name'] or str(r['user_id']))} — {r['n']} сообщ.")
        try:
            await bot.send_message(r["user_id"], f"🏆 Награда за активность в чате: {m(prize)}")
        except Exception:
            pass
    text = f"🏆 <b>Итоги {day}</b>\nКаждому из топа по <b>{m(prize)}</b>:\n" + "\n".join(lines)
    for c in official_chats("chat"):
        try:
            await bot.send_message(chat_target(c), text, parse_mode=HTML)
        except Exception:
            log.warning("Не удалось отправить итоги топа в %s", c)
    alog(f"Награда топу за {day}: {paid} × {m(prize)}")
    return paid


async def maybe_pay_top(bot):
    if sget("top_on") != "1":
        return
    today_s = day_str()
    last = sget("last_top_day")
    if not last:
        sset("last_top_day", today_s)
        return
    if last != today_s:
        await pay_top(bot, day_str(1))
        sset("last_top_day", today_s)


# ───────────────────────── КОНКУРС В ЧАТЕ ─────────────────────────
async def start_contest(ctx, prize, minutes):
    chats = official_chats("chat")
    if not chats:
        return "❌ Нет официального чата: добавьте чат в «Подписки» и отметьте ⭐"
    chat = chats[0]
    cur = db.execute(
        "INSERT INTO contests(chat, msg_id, prize, ends, status) VALUES(?,?,?,?,?)",
        (chat, 0, prize, int(time.time()) + minutes * 60, "active"),
    )
    db.commit()
    cid = cur.lastrowid
    try:
        sent = await ctx.bot.send_message(
            chat_target(chat),
            f"🎉 <b>Конкурс!</b>\n💰 Приз: <b>{m(prize)}</b>\n⏳ Итоги через {minutes} мин.\n\n"
            "Нажмите кнопку, чтобы участвовать (нужно один раз запустить бота).",
            reply_markup=IM([[IB("🎁 Участвовать", callback_data=f"ct:{cid}")]]), parse_mode=HTML,
        )
    except Exception as e:
        db.execute("DELETE FROM contests WHERE id=?", (cid,))
        db.commit()
        return f"❌ Не удалось отправить в чат (бот должен быть в чате): {e}"
    db.execute("UPDATE contests SET msg_id=? WHERE id=?", (sent.message_id, cid))
    db.commit()
    alog(f"Конкурс #{cid}: {m(prize)}, {minutes} мин, {chat}")
    return f"✅ Конкурс #{cid} запущен в {chat}"


async def cb_contest_join(update, ctx):
    q = update.callback_query
    uid = q.from_user.id
    try:
        cid = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        await q.answer("Ошибка")
        return
    c = db.execute("SELECT * FROM contests WHERE id=?", (cid,)).fetchone()
    if not c or c["status"] != "active" or c["ends"] <= time.time():
        await q.answer("Конкурс завершён", show_alert=True)
        return
    if not db.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
        await q.answer("Сначала запустите бота и нажмите /start", show_alert=True)
        return
    if is_banned(uid):
        await q.answer("🚫 Доступ ограничен", show_alert=True)
        return
    db.execute("INSERT OR IGNORE INTO contest_users(contest_id, user_id) VALUES(?,?)", (cid, uid))
    db.commit()
    n = db.execute("SELECT COUNT(*) FROM contest_users WHERE contest_id=?", (cid,)).fetchone()[0]
    await q.answer(f"✅ Вы участвуете! Участников: {n}", show_alert=True)


async def finish_contests(bot):
    now = int(time.time())
    for c in db.execute("SELECT * FROM contests WHERE status='active' AND ends<=?", (now,)).fetchall():
        users = [r[0] for r in db.execute("SELECT user_id FROM contest_users WHERE contest_id=?", (c["id"],)).fetchall()]
        users = [x for x in users if not is_banned(x)]
        winner = random.choice(users) if users else None
        db.execute("UPDATE contests SET status='done', winner=? WHERE id=?", (winner or 0, c["id"]))
        db.commit()
        if winner:
            add_balance(winner, c["prize"])
            text = (
                f"🏆 <b>Конкурс #{c['id']} завершён!</b>\nПобедитель: <a href=\"tg://user?id={winner}\">игрок</a>\n"
                f"💰 Приз: <b>{m(c['prize'])}</b> · участников: {len(users)}"
            )
            try:
                await bot.send_message(winner, f"🎉 Вы выиграли конкурс! +{m(c['prize'])}")
            except Exception:
                pass
        else:
            text = f"Конкурс #{c['id']} завершён без участников."
        try:
            await bot.send_message(chat_target(c["chat"]), text, parse_mode=HTML)
        except Exception:
            log.warning("Не удалось объявить итоги конкурса %s", c["id"])


# ───────────────────────── АДМИНКА: ПОДПИСКИ, ЗАРАБОТОК, БАН, БАЛАНС ─────────────────────────
async def show_subs(update):
    rows = db.execute("SELECT rowid AS rid, * FROM channels ORDER BY kind, rowid").fetchall()
    n_ch = sum(1 for r in rows if r["kind"] == "channel" and r["official"])
    n_ct = sum(1 for r in rows if r["kind"] == "chat" and r["official"])
    kb = []
    for r in rows:
        icon = "📢" if r["kind"] == "channel" else "💬"
        kb.append([
            IB(f"{icon} {r['chat'][:16]}", callback_data="noop"),
            IB("⭐" if r["official"] else "☆", callback_data=f"adm:chof:{r['rid']}"),
            IB("🔒" if r["req"] else "🔓", callback_data=f"adm:chrq:{r['rid']}"),
            IB("🗑", callback_data=f"adm:delch:{r['rid']}"),
        ])
    kb.append([IB("➕ Канал", callback_data="adm:addch:channel"), IB("➕ Чат", callback_data="adm:addch:chat")])
    kb.append([IB("⬅️ Назад", callback_data="adm:home")])
    text = (
        "📢 <b>Подписки, каналы и чаты</b>\n"
        f"⭐ официальных каналов: <b>{n_ch}/2</b> · чатов: <b>{n_ct}/2</b>\n"
        "⭐ — официальный (виден игрокам в меню), 🔒 — обязательная подписка для игры.\n"
        "Чат с ⭐ используется для заработка, топа и конкурсов."
    )
    await respond(update, text, IM(kb))


async def show_earn_admin(update):
    on = sget("earn_on") == "1"
    top = sget("top_on") == "1"
    chats = official_chats("chat")
    text = (
        "💬 <b>Заработок в чате</b> <i>(эксперимент)</i>\n\n"
        f"Статус: {'✅ включён' if on else '⏸ выключен'}\n"
        f"За сообщение: <b>{fu(sint('earn_rate'))}</b> (кулдаун {sint('earn_cd')} с)\n"
        f"Лимит оплаты: <b>{sint('earn_cap')}</b> сообщений в день на игрока\n"
        f"Награда топу: {'✅' if top else '⏸'} · топ-<b>{sint('top_n')}</b> по <b>{m(sint('top_sum'))}</b> в сутки (UTC)\n"
        f"Официальные чаты: {html.escape(', '.join(chats)) if chats else 'нет — добавьте в «Подписки» и отметьте ⭐'}"
    )
    rows = [
        [IB("⏸ Выключить заработок" if on else "▶️ Включить заработок", callback_data="adm:etoggle")],
        [IB("💵 За сообщение", callback_data="adm:erate"), IB("🔢 Лимит в день", callback_data="adm:ecap")],
        [IB("⏸ Выключить топ" if top else "▶️ Включить топ", callback_data="adm:ttoggle")],
        [IB("💰 Сумма топу", callback_data="adm:tsum"), IB("👥 Мест в топе", callback_data="adm:tn")],
        [IB("🏆 Выплатить топ сейчас", callback_data="adm:tpay")],
        [IB("🎉 Запустить конкурс в чат", callback_data="adm:ctnew")],
        [IB("⬅️ Назад", callback_data="adm:home")],
    ]
    await respond(update, text, IM(rows))


async def admin_text_extra(update, ctx, aw, text):
    msg = update.message
    if aw == "adm_ban":
        try:
            tid = int(text)
        except ValueError:
            await msg.reply_text("Нужен числовой ID.")
            return
        if tid == ADMIN_ID:
            await msg.reply_text("Админа банить нельзя.")
        elif db.execute("SELECT 1 FROM bans WHERE user_id=?", (tid,)).fetchone():
            db.execute("DELETE FROM bans WHERE user_id=?", (tid,))
            db.commit()
            alog(f"Разбан {tid}")
            await msg.reply_text(f"✅ {tid} разбанен.")
        else:
            db.execute("INSERT OR REPLACE INTO bans(user_id, reason, ts) VALUES(?,?,?)", (tid, "", int(time.time())))
            db.commit()
            alog(f"Бан {tid}")
            await msg.reply_text(f"🚫 {tid} заблокирован.")
    elif aw == "adm_bal":
        mt = re.fullmatch(r"(\d+)\s+([+-]?)([\d.,]+)", text)
        amt = parse_amount(mt.group(3)) if mt else None
        if not mt or amt is None:
            await msg.reply_text("Формат: ID сумма, например «123456 +5» или «123456 -2.5»")
            return
        tid, sign = int(mt.group(1)), mt.group(2)
        if not db.execute("SELECT 1 FROM users WHERE id=?", (tid,)).fetchone():
            await msg.reply_text("Игрок не найден.")
            return
        if sign == "-":
            db.execute("UPDATE users SET balance=MAX(0, balance-?) WHERE id=?", (amt, tid))
        else:
            db.execute("UPDATE users SET balance=balance+? WHERE id=?", (amt, tid))
        db.commit()
        alog(f"Баланс {tid}: {'-' if sign == '-' else '+'}{m(amt)}")
        await msg.reply_text(f"✅ Баланс {tid}: {get_user(tid) and m(get_user(tid)['balance'])}")
    elif aw == "adm_erate":
        v = parse_dollars(text)
        if v is None or v > 1:
            await msg.reply_text("Введите сумму в $ за сообщение, например 0.001 (макс. 1)")
            return
        sset("earn_rate", int(round(v * MICRO * 100)))
        alog(f"Оплата за сообщение: ${v}")
        await msg.reply_text(f"✅ За сообщение: {fu(sint('earn_rate'))}")
    elif aw == "adm_ecap":
        try:
            v = int(text)
        except ValueError:
            await msg.reply_text("Введите целое число.")
            return
        sset("earn_cap", max(1, min(v, 10000)))
        await msg.reply_text(f"✅ Лимит: {sint('earn_cap')} сообщений в день")
    elif aw == "adm_tsum":
        v = parse_amount(text)
        if v is None:
            await msg.reply_text("Введите сумму в $, например 0.5")
            return
        sset("top_sum", v)
        alog(f"Сумма награды топу: {m(v)}")
        await msg.reply_text(f"✅ Каждому из топа: {m(v)}")
    elif aw == "adm_tn":
        try:
            v = int(text)
        except ValueError:
            await msg.reply_text("Введите число от 1 до 10.")
            return
        sset("top_n", max(1, min(v, 10)))
        await msg.reply_text(f"✅ Мест в топе: {sint('top_n')}")
    elif aw == "adm_ct_prize":
        v = parse_amount(text)
        if v is None:
            await msg.reply_text("Введите приз в $, например 1")
            return
        ctx.user_data["ct_prize"] = v
        ctx.user_data["await"] = "adm_ct_min"
        await msg.reply_text("На сколько минут запустить конкурс? (1–1440)")
    elif aw == "adm_ct_min":
        try:
            mins = max(1, min(int(text), 1440))
        except ValueError:
            await msg.reply_text("Введите целое число минут.")
            return
        await msg.reply_text(await start_contest(ctx, ctx.user_data.pop("ct_prize", 100), mins))


# ───────────────────────── ФОНОВЫЕ ЗАДАЧИ ─────────────────────────
async def background(app):
    await asyncio.sleep(20)
    while True:
        try:
            done = await asyncio.to_thread(poll_deposits_sync)
            for uid, amount in done:
                try:
                    await app.bot.send_message(uid, f"✅ На ваш баланс зачислено {m(amount)}!")
                except Exception:
                    pass
            await finish_contests(app.bot)
            await maybe_pay_top(app.bot)
            await maybe_backup(app.bot)
        except Exception:
            log.exception("background error")
        await asyncio.sleep(60)


async def post_init(app):
    app.bot_data["bg"] = asyncio.create_task(background(app))


# ───────────────────────── ОБРАБОТЧИКИ ─────────────────────────
async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if (q.data or "").startswith("ct:"):
        await cb_contest_join(update, ctx)
        return
    get_user(uid)
    if is_banned(uid) and not is_admin(uid):
        try:
            await q.answer("🚫 Доступ ограничен", show_alert=True)
        except BadRequest:
            pass
        return
    data = q.data or ""
    mod = None
    if data.endswith("@h") or data.endswith("@d"):
        mod, data = data[-1], data[:-2]
    parts = data.split(":")
    t = parts[0]
    res = None
    try:
        if mod:
            cur_bet = get_bet(ctx, uid)
            bal = get_user(uid)["balance"]
            new_bet = max(MIN_BET, cur_bet // 2) if mod == "h" else min(MAX_BET, cur_bet * 2)
            if new_bet > bal:
                res = "❌ Недостаточно средств"
                t = "noop"
            else:
                set_bet(ctx, uid, new_bet)
        if t == "noop":
            pass
        elif t == "menu":
            sub = parts[1]
            if sub == "play":
                await show_games(update, ctx)
            elif sub == "back":
                ctx.user_data.pop("game", None)
                try:
                    await q.message.delete()
                except Exception:
                    pass
                await ctx.bot.send_message(q.message.chat_id, "📋 <b>Главное меню</b>", reply_markup=MAIN_KB, parse_mode=HTML)
            elif sub == "profile":
                await show_profile(update, ctx)
            elif sub == "main":
                await show_menu(update, ctx)
        elif t == "subs":
            if not await need_sub(update, ctx, uid):
                await show_games(update, ctx)
        elif t == "g":
            # выбор игры: если ставка уже введена — второй раз не спрашиваем
            ctx.user_data.pop("await", None)
            game = parts[1]
            if game not in GAME_TITLES and game != "x":
                res = "Ошибка"
            elif await need_sub(update, ctx, uid):
                pass
            else:
                u = get_user(uid)
                bet = get_bet(ctx, uid)
                ctx.user_data["game"] = game
                if bet and not check_bet(bet, u["balance"]):
                    await next_step(update, ctx)
                else:
                    await show_bet_menu(update, ctx, game)
        elif t == "gs":
            # принудительная смена ставки
            ctx.user_data.pop("await", None)
            game = parts[1]
            if game not in GAME_TITLES and game != "x":
                res = "Ошибка"
            elif await need_sub(update, ctx, uid):
                pass
            else:
                await show_bet_menu(update, ctx, game)
        elif t == "b":
            game, val = parts[1], parts[2]
            if game not in GAME_TITLES and game != "x":
                res = "Ошибка"
            else:
                u = get_user(uid)
                if game == "x":
                    ctx.user_data.pop("game", None)
                else:
                    ctx.user_data["game"] = game
                if val == "in":
                    ctx.user_data["await"] = "bet"
                    await respond(update, "Введите сумму ставки (например, 1.5):", IM([[IB("⬅️ Назад", callback_data=f"gs:{game}")]]))
                else:
                    if val == "all":
                        amt = min(u["balance"], MAX_BET)
                    elif val == "cu":
                        amt = u["custom_bet"]
                    else:
                        amt = int(val)
                    err = check_bet(amt, u["balance"])
                    if err:
                        res = err
                    else:
                        set_bet(ctx, uid, amt)
                        ctx.user_data.pop("await", None)
                        await next_step(update, ctx)
        elif t == "o":
            if parts[1] in GAME_TITLES:
                ctx.user_data["game"] = parts[1]
                await next_step(update, ctx)
            else:
                res = "Ошибка"
        elif t == "p":
            res = await play_dice(update, ctx, parts[1], parts[2])
        elif t == "q":
            res = await play_dice(update, ctx, parts[1], parts[2], two=True)
        elif t == "dm":
            if parts[1] in MULTI:
                ctx.user_data[f"mode_{parts[1]}"] = 2 if parts[2] == "2" else 1
                ctx.user_data["game"] = parts[1]
                await next_step(update, ctx)
            else:
                res = "Ошибка"
        elif t == "bal":
            await show_balance(update, ctx)
        elif t == "daily":
            res = await cb_daily(update, ctx)
        elif t == "ref":
            if parts[1] == "claim":
                res = claim_ref(uid)
            await show_ref(update, ctx)
        elif t == "earn":
            if parts[1] == "cash":
                res = cash_chat(uid)
                await show_earn(update, ctx)
            elif parts[1] == "top":
                await respond(update, top_text(day_str()), IM([[IB("⬅️ Назад", callback_data="earn:show")]]))
            else:
                await show_earn(update, ctx)
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
                res = "Нет бонусов"
            else:
                bonus = sum(RANKS[i][3] for i in range(u["rank_claimed"] + 1, idx + 1))
                db.execute("UPDATE users SET balance=balance+?, rank_claimed=? WHERE id=?", (bonus, idx, uid))
                db.commit()
                res = f"🎁 Бонус {m(bonus)} зачислен!"
                await show_profile(update, ctx)
        elif t == "rb":
            u = get_user(uid)
            if u["rakeback"] <= 0:
                res = "Пусто"
            else:
                db.execute("UPDATE users SET balance=balance+?, rakeback=0 WHERE id=?", (u["rakeback"], uid))
                db.commit()
                res = f"♻️ Рейкбек {m(u['rakeback'])} зачислен!"
                await show_profile(update, ctx)
        elif t == "dep":
            ctx.user_data["await"] = "dep"
            await q.message.reply_text(f"Введите сумму пополнения (мин. {m(MIN_TX)}):")
        elif t == "wd":
            ctx.user_data["await"] = "wd"
            await q.message.reply_text(f"Введите сумму вывода (мин. {m(MIN_TX)}):")
        elif t == "adm":
            res = await cb_admin(update, ctx, parts)
    except Exception:
        log.exception("callback error: %s", q.data)
        res = "Ошибка"
    try:
        await q.answer(res, show_alert=bool(res))
    except BadRequest:
        pass


def _norm(t):
    t = t.lower().replace("ё", "е")
    t = re.sub(r"\s*>\s*", ">", t)
    t = re.sub(r"[^\w/>.+\s-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _syn(d):
    out = {}
    for k, phrases in d.items():
        for ph in phrases:
            out[ph] = k
    return out


TEXT_GAMES = {
    "куб": "dice", "кубик": "dice", "кубики": "dice", "кубы": "dice", "куба": "dice",
    "дартс": "darts", "дарт": "darts", "футбол": "football",
    "баскетбол": "basket", "баскет": "basket", "боулинг": "bowling",
    "краш": "crash", "мины": "mines", "мина": "mines", "башня": "tower",
    "слот": "slots", "слоты": "slots", "казино": "slots",
}

TEXT_ONE = {
    "dice": _syn({
        "even": ["чет", "четное", "четный", "четные"],
        "odd": ["нечет", "нечетное", "нечетный", "нечетные"],
        "high": ["больше", "4-6", "больше 4-6"],
        "low": ["меньше", "1-3", "меньше 1-3"],
        "center": ["центр", "3-4"],
        "edge": ["края", "край"],
    }),
    "darts": _syn({
        "red": ["красный", "красное", "красные", "красн"],
        "white": ["белый", "белое", "белые"],
        "bounce": ["отскок", "мимо"],
        "bull": ["центр", "яблочко"],
    }),
    "football": _syn({"goal": ["гол"], "miss": ["мимо"], "center": ["гол в центр", "центр"]}),
    "basket": _syn({
        "goal": ["гол"], "nogoal": ["без гола", "нет гола"], "bounce": ["отскок"],
        "stuck": ["застрял"], "spin": ["прокрут", "прокрутка"],
    }),
    "bowling": _syn({
        "strike": ["страйк"], "miss": ["мимо"],
        "k1": ["1/6", "сбито 1/6"], "k3": ["3/6", "сбито 3/6"],
        "k4": ["4/6", "сбито 4/6"], "k5": ["5/6", "сбито 5/6"],
    }),
}

_SLOT_SYN = {
    "s": ["7", "семерка", "семерки", "семерку", "семь"], "g": ["виноград"],
    "l": ["лимон", "лемон"], "b": ["бар", "bar"],
}
_slots_text = {}
for _r in (1, 2, 3):
    for _k, _syns in _SLOT_SYN.items():
        for _sy in _syns:
            _slots_text[f"{_r} {_sy}"] = f"r{_r}{_k}"
_slots_text.update({
    "уникальные": "uniq", "уникальный": "uniq", "разные": "uniq", "уникальные символы": "uniq",
    "три": "tri", "тройка": "tri", "три одинаковых": "tri", "777": "jp", "джекпот": "jp",
})
TEXT_ONE["slots"] = _slots_text

TEXT_TWO = {
    "dice": _syn({
        "ee": ["оба чет", "оба четные", "оба четных"],
        "oo": ["оба нечет", "оба нечетные", "оба нечетных"],
        "hh": ["оба больше", "оба большие"],
        "ll": ["оба меньше", "оба меньшие"],
        "p18": ["произведение", "произведение 18", "произведение больше 18", "18", "18+", "больше 18", "произв"],
        "gt": ["1>2", "1 больше 2", "первый больше"],
        "lt": ["2>1", "2 больше 1", "второй больше"],
        "dbl": ["дубль", "дубли", "любой дубль"],
    }),
    "darts": _syn({
        "rr": ["оба красный", "оба красное", "оба красные"],
        "ww": ["оба белый", "оба белое", "оба белые"],
        "cl1": ["1 ближе 2", "1 ближе", "первый ближе"],
        "cl2": ["2 ближе 1", "2 ближе", "второй ближе"],
        "sec": ["в сектора", "сектора", "в сектор", "сектор"],
        "adj": ["рядом", "соседние", "соседние сектора"],
        "eq": ["одинаковые", "одинаково", "одинаковый", "такой же"],
        "bull2": ["оба центр", "оба центра", "оба яблочко"],
        "miss2": ["оба отскок", "оба отскока", "оба мимо"],
    }),
}


async def try_text_bet(update, ctx, text):
    """Ставки текстом: «куб оба нечет», «дартс белый», «боулинг 1/6», «башня», «мины 3», «краш 5»."""
    t = _norm(text)
    first, _, rest = t.partition(" ")
    game = TEXT_GAMES.get(first)
    if not game:
        return False
    rest = rest.strip()
    uid = update.effective_user.id
    msg = update.effective_message
    if await need_sub(update, ctx, uid):
        return True
    u = get_user(uid)
    ctx.user_data["game"] = game
    ctx.user_data.pop("await", None)

    action = None
    if game == "crash":
        mm = re.fullmatch(r"[xх]?\s*(\d+)", rest)
        if mm and int(mm.group(1)) in CRASH_MULTS:
            action = ("crash", int(mm.group(1)))
    elif game == "mines":
        mm = re.fullmatch(r"(\d+)(?:\s*(?:мин|мины|мина))?", rest)
        if mm and 2 <= int(mm.group(1)) <= 12:
            action = ("mines", int(mm.group(1)))
    elif game == "tower":
        if rest in ("", "старт", "начать"):
            action = ("tower",)
    else:
        if game in TEXT_TWO and rest in TEXT_TWO[game]:
            action = ("roll", game, TEXT_TWO[game][rest], True)
        elif rest in TEXT_ONE.get(game, {}):
            action = ("roll", game, TEXT_ONE[game][rest], False)

    bet = get_bet(ctx, uid)
    if action is None:
        if rest:
            await msg.reply_text("🤔 Не понял исход. Выберите кнопкой:")
        if bet and not check_bet(bet, u["balance"]):
            await next_step(update, ctx)
        else:
            await show_bet_menu(update, ctx, game)
        return True
    if not bet:
        await msg.reply_text("💵 Сначала задайте ставку: напишите «ставка 1.5» или выберите ниже 👇")
        await show_bet_menu(update, ctx, game)
        return True
    err = check_bet(bet, u["balance"])
    if err:
        await msg.reply_text(err)
        return True
    try:
        if action[0] == "roll":
            res = await play_dice(update, ctx, action[1], action[2], two=action[3])
        elif action[0] == "crash":
            res = await play_crash(update, ctx, action[1])
        elif action[0] == "mines":
            res = await mines_start(update, ctx, action[1])
        else:
            res = await tower_click(update, ctx, "start")
    except Exception:
        log.exception("text bet error: %s", text)
        res = "Ошибка"
    if res:
        await msg.reply_text(res)
    return True


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    chat = update.effective_chat
    if chat is not None and chat.type != "private":
        await on_group_text(update, ctx)
        return
    uid = update.effective_user.id
    if is_banned(uid) and not is_admin(uid):
        return
    text = (update.message.text or "").strip()
    low = text.lower()
    u = get_user(uid)

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

    if low == "казна":
        await cmd_kazna(update, ctx)
        return

    aw = ctx.user_data.get("await")

    if aw and aw.startswith("adm_"):
        if not is_admin(uid):
            ctx.user_data.pop("await", None)
            return
        ctx.user_data.pop("await", None)
        if aw == "adm_user":
            try:
                info = db.execute("SELECT * FROM users WHERE id=?", (int(text),)).fetchone()
            except ValueError:
                info = None
            if info:
                await update.message.reply_text(
                    f"👤 <b>Игрок</b> <code>{html.escape(text)}</code>\n💰 Баланс: {m(info['balance'])}\n🎰 Оборот: {m(info['turnover'])}",
                    parse_mode=HTML,
                )
            else:
                await update.message.reply_text("Не найден.")
        elif aw == "adm_addch":
            chat = norm_chat(text)
            if not chat:
                await update.message.reply_text("Нужен @username или числовой ID.")
            else:
                kind = ctx.user_data.pop("ch_kind", "channel")
                db.execute("INSERT OR IGNORE INTO channels(chat, kind) VALUES(?,?)", (chat, kind))
                db.commit()
                alog(f"Добавлен {kind}: {chat}")
                await update.message.reply_text(
                    f"✅ {'Канал' if kind == 'channel' else 'Чат'} {chat} добавлен. Отметьте ⭐ в «Подписки», чтобы сделать официальным."
                )
        elif aw == "adm_bc":
            ids = [r["id"] for r in db.execute("SELECT id FROM users").fetchall()]
            ok = 0
            await update.message.reply_text(f"📤 Рассылка на {len(ids)}...")
            for i in ids:
                try:
                    await ctx.bot.send_message(i, text)
                    ok += 1
                except Exception:
                    pass
                await asyncio.sleep(0.05)
            await update.message.reply_text(f"✅ Доставлено: {ok}/{len(ids)}")
        else:
            await admin_text_extra(update, ctx, aw, text)
        return

    if aw in ("bet", "dep", "wd"):
        amt = parse_amount(text)
        if amt is None:
            await update.message.reply_text("Введите число, например 1.5")
            return
        if aw == "bet":
            err = check_bet(amt, u["balance"])
            if err:
                await update.message.reply_text(err)
            else:
                set_bet(ctx, uid, amt)
                ctx.user_data.pop("await", None)
                await next_step(update, ctx)
            return
        if amt < MIN_TX:
            await update.message.reply_text(f"Мин. сумма {m(MIN_TX)}")
            return
        ctx.user_data.pop("await", None)
        if aw == "dep":
            await create_deposit(update, ctx, amt)
        else:
            await create_withdraw(update, ctx, amt)
        return

    if await try_text_bet(update, ctx, text):
        return

    mt = re.fullmatch(r"(ставка|деп|пополнить|вывод)\s+([\d.,]+)\$?", low)
    if mt:
        cmd, val = mt.group(1), mt.group(2)
        amt = parse_amount(val)
        if amt is None or amt < MIN_TX:
            await update.message.reply_text(f"Мин. сумма {m(MIN_TX)}")
            return
        if cmd == "ставка":
            if amt < MIN_BET or amt > MAX_BET:
                await update.message.reply_text(f"Ставка от {m(MIN_BET)} до {m(MAX_BET)}")
                return
            db.execute("UPDATE users SET custom_bet=? WHERE id=?", (amt, uid))
            db.commit()
            set_bet(ctx, uid, amt)
            await update.message.reply_text(f"⭐ Своя ставка: <b>{m(amt)}</b>", parse_mode=HTML)
            await show_games(update, ctx)
        elif cmd in ("деп", "пополнить"):
            await create_deposit(update, ctx, amt)
        elif cmd == "вывод":
            await create_withdraw(update, ctx, amt)
        return

    if re.fullmatch(r"[\d.,]+\$?", low):
        amt = parse_amount(low)
        if amt and MIN_BET <= amt <= MAX_BET:
            db.execute("UPDATE users SET custom_bet=? WHERE id=?", (amt, uid))
            db.commit()
            set_bet(ctx, uid, amt)
            await update.message.reply_text(f"⭐ Ставка: <b>{m(amt)}</b>", parse_mode=HTML)
            await show_games(update, ctx)
        else:
            await update.message.reply_text(f"Ставка от {m(MIN_BET)} до {m(MAX_BET)}")
        return

    if low == "вб":
        if u["balance"] < MIN_BET:
            await update.message.reply_text("❌ Недостаточно средств.")
            return
        amt = min(u["balance"], MAX_BET)
        set_bet(ctx, uid, amt)
        await update.message.reply_text(f"💰 Ва-банк: <b>{m(amt)}</b>", parse_mode=HTML)
        if ctx.user_data.get("game"):
            await next_step(update, ctx)
        else:
            await show_games(update, ctx)
        return


async def on_error(update, ctx):
    log.error("Ошибка: %s", ctx.error, exc_info=ctx.error)


# ───────────────────────── FLASK + ВЕБХУК XROCKET ─────────────────────────
flask_app = Flask(__name__)


@flask_app.route("/")
def index():
    return "Bot is running"


@flask_app.route("/health")
def health():
    return "ok"


def verify_signature(raw, header_sig):
    """Подпись xRocket: HMAC-SHA256(тело, ключ = SHA256(токен))."""
    if not XROCKET_TOKEN or not header_sig:
        return None
    key = hashlib.sha256(XROCKET_TOKEN.encode()).digest()
    expected = hmac.new(key, raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_sig.strip().lower())


def extract_invoice(data):
    """Достаёт (тип события, id счёта, статус) из разных вариантов формата вебхука."""
    evt = str(data.get("type") or data.get("event") or "")
    body = data.get("data", data)
    if isinstance(body, dict) and isinstance(body.get("invoice"), dict):
        body = body["invoice"]
    if not isinstance(body, dict):
        body = {}
    invoice_id = body.get("id") or body.get("invoiceId")
    status = str(body.get("status") or "")
    return evt, invoice_id, status


def credit_deposit(invoice_id):
    """Атомарно зачисляет депозит по invoice_id. Возвращает (user_id, amount, код)."""
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM deposits WHERE invoice_id=?", (str(invoice_id),)).fetchone()
        if not row:
            return None, None, "not_found"
        cur = conn.execute("UPDATE deposits SET status='approved' WHERE id=? AND status='pending'", (row["id"],))
        if cur.rowcount != 1:
            conn.rollback()
            return None, None, "already"
        conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (row["amount"], row["user_id"]))
        conn.commit()
        return row["user_id"], row["amount"], "ok"
    finally:
        conn.close()


def tg_send(chat_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        log.error("Не удалось отправить уведомление %s: %s", chat_id, e)


@flask_app.route("/xrocket-webhook", methods=["POST"])
def xrocket_webhook():
    try:
        raw = request.get_data()
        sig_ok = verify_signature(raw, request.headers.get("rocket-pay-signature"))
        if sig_ok is False:
            log.warning("Webhook: неверная подпись")
            if WEBHOOK_STRICT:
                return jsonify({"status": "bad signature"}), 403
        elif sig_ok is None:
            log.warning("Webhook: подписи нет или токен не задан")
            if WEBHOOK_STRICT:
                return jsonify({"status": "no signature"}), 403
