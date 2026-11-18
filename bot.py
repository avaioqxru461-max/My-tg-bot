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
    ("basket", "🏀 Баскетбол"), ("bowling", "🎳 Боулинг"), ("crash", "🎰 Краш"),
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
    """)
    # миграция: последняя ставка игрока (чтобы не спрашивать её снова даже после перезапуска)
    try:
        db.execute("ALTER TABLE users ADD COLUMN last_bet INTEGER NOT NULL DEFAULT 0")
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


def end_rows(repeat_cb=None, game=None):
    """Кнопки после игры: повтор / другой исход с той же ставкой / ставка / игры."""
    rows = []
    if repeat_cb:
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
    u = get_user(update.effective_user.id)
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
    kb = IM([
        [IB("🎮 Играть", callback_data="menu:play"), IB("👤 Профиль", callback_data="menu:profile")],
        [IB("💳 Пополнить", callback_data="dep:start"), IB("💸 Вывести", callback_data="wd:start")],
    ])
    await respond(
        update,
        f"📋 <b>Меню</b>\n\n💰 Баланс: <b>{m(u['balance'])}</b>\n💵 Ставка: <b>{m(bet) if bet else 'не выбрана'}</b>\n\n"
        "Быстрая ставка: <code>ставка 1.5</code>\nНа весь баланс: <code>вб</code>",
        kb,
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
        [gb("bowling")],
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
    if game in DICE:
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
        await ctx.bot.send_message(chat_id, text, reply_markup=IM(end_rows(repeat_cb, game)), parse_mode=HTML)
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
    kb = IM(end_rows(f"cr:{mult}", "crash"))
    await respond(
        update,
        f"<b>🎰 Краш</b>\n🎯 Множитель: <b>x{mult}</b>\n\n{res}\n\n💰 Баланс: <b>{m(get_user(uid)['balance'])}</b>",
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
    after = end_rows(f"mc:{st['m']}", "mines")
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
    after = end_rows("tw:start", "tower")
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
    [IB("📢 Подписки", callback_data="adm:subs"), IB("✉️ Рассылка", callback_data="adm:bc")],
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
        chats = [r["chat"] for r in db.execute("SELECT chat FROM channels").fetchall()]
        rows = [[IB(f"🗑 {c}", callback_data=f"adm:delch:{c}")] for c in chats] + [[IB("➕ Добавить", callback_data="adm:addch")]]
        await respond(update, "📢 <b>Подписки</b>\n" + (html.escape("\n".join(chats)) if chats else "Нет"), IM(rows))
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


# ───────────────────────── ОБРАБОТЧИКИ ─────────────────────────
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
                try:
                    await q.message.delete()
                except Exception:
                    pass
                await ctx.bot.send_message(q.message.chat_id, "📋 <b>Главное меню</b>", reply_markup=MAIN_KB, parse_mode=HTML)
            elif sub == "profile":
                await show_profile(update, ctx)
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
    uid = update.effective_user.id
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
            db.execute("INSERT OR IGNORE INTO channels(chat) VALUES(?)", (text,))
            db.commit()
            await update.message.reply_text(f"✅ Канал {text} добавлен.")
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

        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            data = None
        if not isinstance(data, dict):
            return jsonify({"status": "bad json"}), 400
        log.info("Webhook received: %s", data)

        evt, invoice_id, status = extract_invoice(data)
        paid = status.lower() == "paid" or evt.lower() in ("invoicepay", "invoice_pay", "invoicepaid")

        if invoice_id is not None and paid:
            uid, amount, code = credit_deposit(invoice_id)
            if code == "ok":
                log.info("Автозачисление: user %s +%s (invoice %s)", uid, m(amount), invoice_id)
                tg_send(uid, f"✅ На ваш баланс зачислено {m(amount)}!")
            elif code == "not_found":
                log.error("Webhook: счёт %s не найден в deposits", invoice_id)
            else:
                log.info("Webhook: счёт %s уже обработан", invoice_id)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        log.exception("Webhook error: %s", e)
        return jsonify({"status": "error"}), 500


def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), use_reloader=False, threaded=True)


# ───────────────────────── ЗАПУСК ─────────────────────────
def main():
    if not TOKEN:
        raise SystemExit("Нет TELEGRAM_TOKEN")
    init_db()
    load_games()
    threading.Thread(target=run_flask, daemon=True).start()
    app = Application.builder().token(TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("kazna", cmd_kazna))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    log.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
