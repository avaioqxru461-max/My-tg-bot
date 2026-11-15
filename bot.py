import os
import logging
import asyncio
import sqlite3
import random
from threading import Thread
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# ================= НАСТРОЙКИ =================import os
import logging
import asyncio
import sqlite3
import random
from threading import Thread
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# ================= НАСТРОЙКИ =================
SUPER_ADMIN_ID = 8192612489
# =============================================

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

app = Flask(__name__)

@app.route('/')
def health():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# --- База данных ---
def init_db():
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, balance REAL DEFAULT 2.0, referrer_id INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)''')
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('channels', '')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('sub_check', '1')")
    c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (SUPER_ADMIN_ID,))
    conn.commit()
    conn.close()

def get_setting(key):
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    res = c.fetchone()
    conn.close()
    return res[0] if res else None

def set_setting(key, value):
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute("SELECT balance, referrer_id FROM users WHERE user_id = ?", (user_id,))
    user = c.fetchone()
    if not user:
        c.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        user = (2.0, None)
    conn.close()
    return user

def update_balance(user_id, amount):
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()

def is_admin(user_id):
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute("SELECT user_id FROM admins WHERE user_id = ?", (user_id,))
    res = c.fetchone()
    conn.close()
    return res is not None

def get_all_users():
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users = [row[0] for row in c.fetchall()]
    conn.close()
    return users

# --- Состояния игр ---
games_state = {}

# --- Клавиатуры ---
def bottom_menu():
    keyboard = [
        [KeyboardButton("💰 Баланс"), KeyboardButton("🎮 Играть"), KeyboardButton("📋 Меню")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def play_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 Телеграм игры", callback_data="tg_games"), InlineKeyboardButton("🎰 Авторские (Краш)", callback_data="orig_games")],
        [InlineKeyboardButton("💣 Мины", callback_data="mines_start"), InlineKeyboardButton("🏰 Башня", callback_data="tower_start")],
        [InlineKeyboardButton("⬅️ В главное меню", callback_data="menu")]
    ])

def tg_games_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 Кубик", callback_data="dice_menu"), InlineKeyboardButton("🎯 Дартс", callback_data="darts_menu")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="play_menu")]
    ])

def dice_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Чет", callback_data="dice_even"), InlineKeyboardButton("Нечет", callback_data="dice_odd")],
        [InlineKeyboardButton("Больше 3", callback_data="dice_more"), InlineKeyboardButton("Меньше 4", callback_data="dice_less")],
        [InlineKeyboardButton("Центр (3,4)", callback_data="dice_center"), InlineKeyboardButton("Края (1,6)", callback_data="dice_edges")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="tg_games")]
    ])

def darts_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Красный", callback_data="darts_red"), InlineKeyboardButton("Белый", callback_data="darts_white")],
        [InlineKeyboardButton("Отскок", callback_data="darts_bounce"), InlineKeyboardButton("Центр", callback_data="darts_center")],
        [InlineKeyboardButton("Любой (2-5)", callback_data="darts_any")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="tg_games")]
    ])

def orig_games_menu():
    multipliers = ["2", "3", "4", "5", "10", "30", "50", "100"]
    keyboard = []
    row = []
    for m in multipliers:
        row.append(InlineKeyboardButton(f"x{m}", callback_data=f"orig_{m}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="play_menu")])
    return InlineKeyboardMarkup(keyboard)

def bet_menu(game_type):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("0.5$", callback_data=f"bet_{game_type}_0.5"),
         InlineKeyboardButton("1$", callback_data=f"bet_{game_type}_1"),
         InlineKeyboardButton("2$", callback_data=f"bet_{game_type}_2")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="play_menu")]
    ])

def admin_panel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Выдать $", callback_data="admin_add"), InlineKeyboardButton("➖ Забрать $", callback_data="admin_remove")],
        [InlineKeyboardButton("⚙️ Подписки", callback_data="admin_subs")],
        [InlineKeyboardButton("👑 Админы", callback_data="admin_manage_admins")],
        [InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton("⬅️ Закрыть", callback_data="close")]
    ])

def subs_menu():
    channels_str = get_setting('channels')
    channels = [c for c in channels_str.split(',') if c]
    check_on = get_setting('sub_check') == '1'
    keyboard = []
    for ch in channels:
        keyboard.append([InlineKeyboardButton(f"❌ Удалить {ch}", callback_data=f"del_ch_{ch}")])
    keyboard.append([InlineKeyboardButton("🟢 ВКЛ" if check_on else "🔴 ВЫКЛ", callback_data="toggle_sub")])
    keyboard.append([InlineKeyboardButton("➕ Добавить канал", callback_data="add_channel_info")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin_panel")])
    return InlineKeyboardMarkup(keyboard)

# --- Проверка подписки ---
async def check_subscription(user_id, context):
    if get_setting('sub_check') != '1': return True
    channels_str = get_setting('channels')
    channels = [c for c in channels_str.split(',') if c]
    if not channels: return True
    for channel in channels:
        try:
            member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status not in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
                return False
        except Exception: pass
    return True

# --- Основные команды ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if args and args[0].startswith("ref_"):
        referrer_id = int(args[0].split("_")[1])
        if referrer_id != user_id:
            user = get_user(user_id)
            if user[1] is None:
                conn = sqlite3.connect('bot.db')
                c = conn.cursor()
                c.execute("UPDATE users SET referrer_id = ? WHERE user_id = ?", (referrer_id, user_id))
                conn.commit()
                conn.close()
                update_balance(referrer_id, 1.0)

    user = get_user(user_id)
    if not await check_subscription(user_id, context):
        channels_str = get_setting('channels')
        channels = [c for c in channels_str.split(',') if c]
        keyboard = [[InlineKeyboardButton(f"📢 Канал {i+1}", url=f"https://t.me/{ch.replace('@', '')}")] for i, ch in enumerate(channels)]
        keyboard.append([InlineKeyboardButton("✅ Я подписался", callback_data="check_sub")])
        await update.message.reply_text("Подпишись на каналы!", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    await update.message.reply_text(f"Привет! Твой баланс: {user[0]:.2f}$", reply_markup=bottom_menu())

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У тебя нет прав.")
        return
    await update.message.reply_text("Админ-панель:", reply_markup=admin_panel())

# --- Обработчик текстовых кнопок (Reply) ---
async def reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    user = get_user(user_id)

    if text == "💰 Баланс":
        await update.message.reply_text(f"💰 Баланс: {user[0]:.2f}$", reply_markup=bottom_menu())
    elif text == "🎮 Играть":
        await update.message.reply_text("Выбери раздел игр:", reply_markup=play_menu())
    elif text == "📋 Меню":
        await update.message.reply_text(f"Твой баланс: {user[0]:.2f}$\nВыбери действие:", reply_markup=bottom_menu())

# --- Обработчик кнопок ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    user = get_user(user_id)
    balance = user[0]

    if data == "close":
        await query.message.delete()
        return
    if data == "menu":
        await query.edit_message_text("Возврат в меню...")
        await query.message.reply_text(f"Твой баланс: {balance:.2f}$", reply_markup=bottom_menu())
        return
    
    elif data == "play_menu": await query.edit_message_text("Выбери раздел игр:", reply_markup=play_menu())
    elif data == "tg_games": await query.edit_message_text("Выбери игру Telegram:", reply_markup=tg_games_menu())
    elif data == "dice_menu": await query.edit_message_text("Выбери тип ставки на кубик:", reply_markup=dice_menu())
    elif data == "darts_menu": await query.edit_message_text("Выбери тип ставки на дартс:", reply_markup=darts_menu())
    elif data == "orig_games": await query.edit_message_text("Выбери множитель (Краш):", reply_markup=orig_games_menu())
    elif data.startswith("orig_"): await query.edit_message_text(f"Выбери ставку для Краш x{data.split('_')[1]}:", reply_markup=bet_menu(data))
    elif data.startswith("dice_") or data.startswith("darts_"): await query.edit_message_text("Выбери ставку:", reply_markup=bet_menu(data))

    elif data.startswith("bet_"):
        parts = data.split("_")
        bet = float(parts[-1])
        game_data = "_".join(parts[1:-1])
        
        if balance < bet:
            await query.edit_message_text("❌ Недостаточно средств!", reply_markup=play_menu())
            return
        
        if game_data.startswith("orig_"):
            mult = int(game_data.split("_")[1])
            update_balance(user_id, -bet)
            if random.randint(1, 100) <= (100 / mult):
                win = bet * mult
                update_balance(user_id, win)
                await query.edit_message_text(f"🎉 Выиграл x{mult}! +{win:.2f}$\n💰 Баланс: {get_user(user_id)[0]:.2f}$", reply_markup=play_menu())
            else:
                await query.edit_message_text(f"💥 Краш! Проигрыш {bet:.2f}$", reply_markup=play_menu())
        
        elif game_data.startswith("dice_"):
            update_balance(user_id, -bet)
            msg = await query.message.chat.send_dice(emoji="🎲")
            await asyncio.sleep(4)
            res = msg.dice.value
            win = 0
            if game_data == "dice_even" and res % 2 == 0: win = bet * 2
            elif game_data == "dice_odd" and res % 2 != 0: win = bet * 2
            elif game_data == "dice_more" and res > 3: win = bet * 2
            elif game_data == "dice_less" and res < 4: win = bet * 2
            elif game_data == "dice_center" and res in [3, 4]: win = bet * 2
            elif game_data == "dice_edges" and res in [1, 6]: win = bet * 3
            if win > 0:
                update_balance(user_id, win)
                await query.message.reply_text(f"🎲 Выпало: {res}\n✅ Победа! +{win:.2f}$", reply_markup=play_menu())
            else:
                await query.message.reply_text(f"🎲 Выпало: {res}\n❌ Проигрыш. -{bet:.2f}$", reply_markup=play_menu())

        elif game_data.startswith("darts_"):
            update_balance(user_id, -bet)
            msg = await query.message.chat.send_dice(emoji="🎯")
            await asyncio.sleep(4)
            res = msg.dice.value
            win = 0
            if game_data == "darts_red" and res in [1, 3, 5]: win = bet * 2
            elif game_data == "darts_white" and res in [2, 4, 6]: win = bet * 2
            elif game_data == "darts_bounce" and res == 1: win = bet * 5
            elif game_data == "darts_center" and res == 6: win = bet * 4
            elif game_data == "darts_any" and res in [2, 3, 4, 5]: win = bet * 1.5
            if win > 0:
                update_balance(user_id, win)
                await query.message.reply_text(f"🎯 Выпало: {res}\n✅ Победа! +{win:.2f}$", reply_markup=play_menu())
            else:
                await query.message.reply_text(f"🎯 Выпало: {res}\n❌ Проигрыш. -{bet:.2f}$", reply_markup=play_menu())

    # --- МИНЫ (ИСПРАВЛЕНО) ---
    elif data == "mines_start": await query.edit_message_text("Выбери ставку для Мин:", reply_markup=bet_menu("mines"))
    elif data.startswith("bet_mines_"):
        bet = float(data.split("_")[2])
        if balance < bet: 
            await query.edit_message_text("❌ Недостаточно средств!")
            return
        update_balance(user_id, -bet)
        games_state[user_id] = {"game": "mines", "bet": bet, "mines": random.sample(range(25), 5), "opened": 0}
        keyboard = [[InlineKeyboardButton("⬜", callback_data=f"m_open_{i*5+j}") for j in range(5)] for i in range(5)]
        keyboard.append([InlineKeyboardButton("💰 Забрать", callback_data="m_cashout")])
        await query.edit_message_text(f"💣 Мины. Ставка: {bet}$. Открыто: 0. Множитель: x1.0", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith("m_open_"):
        state = games_state.get(user_id)
        if not state or state["game"] != "mines": return
        idx = int(data.split("_")[2])
        if idx in state["mines"]:
            del games_state[user_id]
            await query.edit_message_text(f"💥 Бум! Ты проиграл {state['bet']:.2f}$", reply_markup=play_menu())
            return
        state["opened"] += 1
        mult = 1 + (state["opened"] * 0.2)
        # Перерисовываем клавиатуру
        keyboard = []
        for i in range(5):
            row = []
            for j in range(5):
                cur = i*5 + j
                if cur == idx: row.append(InlineKeyboardButton("💎", callback_data="ignore"))
                else: row.append(InlineKeyboardButton("⬜", callback_data=f"m_open_{cur}"))
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("💰 Забрать", callback_data="m_cashout")])
        await query.edit_message_text(f"💣 Мины. Ставка: {state['bet']}$. Открыто: {state['opened']}. Множитель: x{mult:.1f}", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data == "m_cashout":
        state = games_state.get(user_id)
        if not state or state["game"] != "mines": return
        win = state["bet"] * (1 + state["opened"] * 0.2)
        update_balance(user_id, win)
        del games_state[user_id]
        await query.edit_message_text(f"💎 Забрал {win:.2f}$", reply_markup=play_menu())

    # --- БАШНЯ (ИСПРАВЛЕНО) ---
    elif data == "tower_start": await query.edit_message_text("Выбери ставку для Башни:", reply_markup=bet_menu("tower"))
    elif data.startswith("bet_tower_"):
        bet = float(data.split("_")[2])
        if balance < bet: 
            await query.edit_message_text("❌ Недостаточно средств!")
            return
        update_balance(user_id, -bet)
        games_state[user_id] = {"game": "tower", "bet": bet, "level": 1}
        await render_tower(query, user_id)
    elif data.startswith("t_step_"):
        step = int(data.split("_")[2])
        state = games_state.get(user_id)
        if not state or state["game"] != "tower": return
        if step == random.randint(0, 4):
            del games_state[user_id]
            await query.edit_message_text(f"💀 Мина! Проигрыш {state['bet']:.2f}$", reply_markup=play_menu())
            return
        state["level"] += 1
        if state["level"] > 6:
            win = state["bet"] * 2
            update_balance(user_id, win)
            del games_state[user_id]
            await query.edit_message_text(f"🏆 Башня пройдена! Выигрыш {win:.2f}$", reply_markup=play_menu())
            return
        await render_tower(query, user_id)

    # --- АДМИНКА ---
    elif data == "admin_panel" and is_admin(user_id): await query.edit_message_text("Админ-панель:", reply_markup=admin_panel())
    elif data == "admin_subs" and is_admin(user_id): await query.edit_message_text("Управление подписками:", reply_markup=subs_menu())
    elif data == "toggle_sub" and is_admin(user_id): set_setting('sub_check', '0' if get_setting('sub_check') == '1' else '1'); await query.edit_message_text("Изменено!", reply_markup=subs_menu())
    elif data == "add_channel_info" and is_admin(user_id): await query.edit_message_text("Отправь @юзернейм канала (бот должен быть там админом).", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_subs")]]))
    elif data.startswith("del_ch_") and is_admin(user_id):
        ch = data.replace("del_ch_", ""); channels = [c for c in get_setting('channels').split(',') if c and c != ch]
        set_setting('channels', ','.join(channels)); await query.edit_message_text("Удален.", reply_markup=subs_menu())
    elif data == "admin_manage_admins" and is_admin(user_id):
        conn = sqlite3.connect('bot.db'); c = conn.cursor(); c.execute("SELECT user_id FROM admins")
        admins = [str(r[0]) for r in c.fetchall()]; conn.close()
        await query.edit_message_text("👑 Админы:\n" + "\n".join(admins) + "\n\nДобавить: `add ID`\nУдалить: `del ID`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_panel")]]))
    elif data == "admin_broadcast" and is_admin(user_id): await query.edit_message_text("Отправь текст для рассылки:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_panel")]]))

async def render_tower(query, user_id):
    state = games_state[user_id]
    text = f"🏰 Башня. Уровень: {state['level']}/6. Множитель: x{1 + state['level']*0.2:.1f}\nВыбери дверь (1 из 5 - мина):"
    keyboard = [[InlineKeyboardButton(str(i+1), callback_data=f"t_step_{i}") for i in range(5)]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

# --- Обработчик текста (Рассылка/Админы/Каналы) ---
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return
    text = update.message.text.strip()
    
    # Проверка на добавление канала
    if text.startswith('@') or text.startswith('-100'):
        try:
            bot_member = await context.bot.get_chat_member(chat_id=text, user_id=context.bot.id)
            if bot_member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
                channels = [c for c in get_setting('channels').split(',') if c]
                if text not in channels:
                    channels.append(text); set_setting('channels', ','.join(channels))
                    await update.message.reply_text(f"✅ Канал {text} добавлен!", reply_markup=subs_menu())
            else: await update.message.reply_text("❌ Бот не админ в канале.")
        except: await update.message.reply_text("❌ Ошибка канала.")
        return

    # Проверка на выдачу баланса (ID сумма)
    parts = text.split()
    if len(parts) == 2 and parts[0].isdigit():
        try:
            target_id = int(parts[0])
            amount = float(parts[1])
            update_balance(target_id, amount)
            await update.message.reply_text(f"✅ Успешно. Баланс {target_id} изменен на {amount}$.")
            return
        except: pass

    if text.startswith("add "):
        try: 
            new_admin = int(text.split()[1])
            conn = sqlite3.connect('bot.db'); c = conn.cursor(); c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (new_admin,)); conn.commit(); conn.close()
            await update.message.reply_text(f"✅ {new_admin} теперь админ.")
        except: pass
        return
    if text.startswith("del "):
        try:
            del_admin = int(text.split()[1])
            if del_admin == SUPER_ADMIN_ID: return await update.message.reply_text("Нельзя удалить главного.")
            conn = sqlite3.connect('bot.db'); c = conn.cursor(); c.execute("DELETE FROM admins WHERE user_id = ?", (del_admin,)); conn.commit(); conn.close()
            await update.message.reply_text(f"✅ {del_admin} лишен прав.")
        except: pass
        return

    # Если не распознали команду - делаем рассылку
    users = get_all_users()
    success = 0
    msg = await update.message.reply_text(f"⏳ Рассылка для {len(users)} чел...")
    for u in users:
        try:
            await context.bot.send_message(chat_id=u, text=text)
            success += 1; await asyncio.sleep(0.05)
        except: pass
    await msg.edit_text(f"✅ Рассылка завершена. Доставлено: {success}")

def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token: raise ValueError("TELEGRAM_TOKEN не задан!")
    init_db()
    Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.Regex("^(💰 Баланс|🎮 Играть|📋 Меню)$"), reply_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("Бот запущен...")
    application.run_polling()

if __name__ == '__main__':
    main()
SUPER_ADMIN_ID = 8192612489
# =============================================

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

app = Flask(__name__)

@app.route('/')
def health():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# --- База данных ---
def init_db():
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, balance REAL DEFAULT 2.0, referrer_id INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)''')
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('channels', '')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('sub_check', '1')")
    c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (SUPER_ADMIN_ID,))
    conn.commit()
    conn.close()

def get_setting(key):
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    res = c.fetchone()
    conn.close()
    return res[0] if res else None

def set_setting(key, value):
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute("SELECT balance, referrer_id FROM users WHERE user_id = ?", (user_id,))
    user = c.fetchone()
    if not user:
        c.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        user = (2.0, None)
    conn.close()
    return user

def update_balance(user_id, amount):
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()

def is_admin(user_id):
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute("SELECT user_id FROM admins WHERE user_id = ?", (user_id,))
    res = c.fetchone()
    conn.close()
    return res is not None

def get_all_users():
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users = [row[0] for row in c.fetchall()]
    conn.close()
    return users

# --- Состояния игр ---
games_state = {}

# --- Клавиатуры ---
def bottom_menu():
    keyboard = [
        [KeyboardButton("💰 Баланс"), KeyboardButton("🎮 Играть"), KeyboardButton("📋 Меню")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def play_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 Телеграм игры", callback_data="tg_games"), InlineKeyboardButton("🎰 Авторские (Краш)", callback_data="orig_games")],
        [InlineKeyboardButton("💣 Мины", callback_data="mines_start"), InlineKeyboardButton("🏰 Башня", callback_data="tower_start")],
        [InlineKeyboardButton("⬅️ В главное меню", callback_data="menu")]
    ])

def tg_games_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 Кубик", callback_data="dice_menu"), InlineKeyboardButton("🎯 Дартс", callback_data="darts_menu")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="play_menu")]
    ])

def dice_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Чет", callback_data="dice_even"), InlineKeyboardButton("Нечет", callback_data="dice_odd")],
        [InlineKeyboardButton("Больше 3", callback_data="dice_more"), InlineKeyboardButton("Меньше 4", callback_data="dice_less")],
        [InlineKeyboardButton("Центр (3,4)", callback_data="dice_center"), InlineKeyboardButton("Края (1,6)", callback_data="dice_edges")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="tg_games")]
    ])

def darts_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Красный", callback_data="darts_red"), InlineKeyboardButton("Белый", callback_data="darts_white")],
        [InlineKeyboardButton("Отскок", callback_data="darts_bounce"), InlineKeyboardButton("Центр", callback_data="darts_center")],
        [InlineKeyboardButton("Любой (2-5)", callback_data="darts_any")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="tg_games")]
    ])

def orig_games_menu():
    multipliers = ["2", "3", "4", "5", "10", "30", "50", "100"]
    keyboard = []
    row = []
    for m in multipliers:
        row.append(InlineKeyboardButton(f"x{m}", callback_data=f"orig_{m}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="play_menu")])
    return InlineKeyboardMarkup(keyboard)

def bet_menu(game_type):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("0.5$", callback_data=f"bet_{game_type}_0.5"),
         InlineKeyboardButton("1$", callback_data=f"bet_{game_type}_1"),
         InlineKeyboardButton("2$", callback_data=f"bet_{game_type}_2")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="play_menu")]
    ])

def admin_panel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Выдать $", callback_data="admin_add"), InlineKeyboardButton("➖ Забрать $", callback_data="admin_remove")],
        [InlineKeyboardButton("⚙️ Подписки", callback_data="admin_subs")],
        [InlineKeyboardButton("👑 Админы", callback_data="admin_manage_admins")],
        [InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton("⬅️ Закрыть", callback_data="close")]
    ])

def subs_menu():
    channels_str = get_setting('channels')
    channels = [c for c in channels_str.split(',') if c]
    check_on = get_setting('sub_check') == '1'
    keyboard = []
    for ch in channels:
        keyboard.append([InlineKeyboardButton(f"❌ Удалить {ch}", callback_data=f"del_ch_{ch}")])
    keyboard.append([InlineKeyboardButton("🟢 ВКЛ" if check_on else "🔴 ВЫКЛ", callback_data="toggle_sub")])
    keyboard.append([InlineKeyboardButton("➕ Добавить канал", callback_data="add_channel_info")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin_panel")])
    return InlineKeyboardMarkup(keyboard)

# --- Проверка подписки ---
async def check_subscription(user_id, context):
    if get_setting('sub_check') != '1': return True
    channels_str = get_setting('channels')
    channels = [c for c in channels_str.split(',') if c]
    if not channels: return True
    for channel in channels:
        try:
            member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status not in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
                return False
        except Exception: pass
    return True

# --- Основные команды ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if args and args[0].startswith("ref_"):
        referrer_id = int(args[0].split("_")[1])
        if referrer_id != user_id:
            user = get_user(user_id)
            if user[1] is None:
                conn = sqlite3.connect('bot.db')
                c = conn.cursor()
                c.execute("UPDATE users SET referrer_id = ? WHERE user_id = ?", (referrer_id, user_id))
                conn.commit()
                conn.close()
                update_balance(referrer_id, 1.0)

    user = get_user(user_id)
    if not await check_subscription(user_id, context):
        channels_str = get_setting('channels')
        channels = [c for c in channels_str.split(',') if c]
        keyboard = [[InlineKeyboardButton(f"📢 Канал {i+1}", url=f"https://t.me/{ch.replace('@', '')}")] for i, ch in enumerate(channels)]
        keyboard.append([InlineKeyboardButton("✅ Я подписался", callback_data="check_sub")])
        await update.message.reply_text("Подпишись на каналы!", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    await update.message.reply_text(f"Привет! Твой баланс: {user[0]:.2f}$", reply_markup=bottom_menu())

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У тебя нет прав.")
        return
    await update.message.reply_text("Админ-панель:", reply_markup=admin_panel())

# --- Обработчик текстовых кнопок (Reply) ---
async def reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    user = get_user(user_id)

    if text == "💰 Баланс":
        await update.message.reply_text(f"💰 Баланс: {user[0]:.2f}$", reply_markup=bottom_menu())
    elif text == "🎮 Играть":
        await update.message.reply_text("Выбери раздел игр:", reply_markup=play_menu())
    elif text == "📋 Меню":
        await update.message.reply_text(f"Твой баланс: {user[0]:.2f}$\nВыбери действие:", reply_markup=bottom_menu())

# --- Обработчик кнопок ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    user = get_user(user_id)
    balance = user[0]

    if data == "close":
        await query.message.delete()
        return
    if data == "menu":
        await query.edit_message_text("Возврат в меню...")
        await query.message.reply_text(f"Твой баланс: {balance:.2f}$", reply_markup=bottom_menu())
        return
    
    elif data == "play_menu": await query.edit_message_text("Выбери раздел игр:", reply_markup=play_menu())
    elif data == "tg_games": await query.edit_message_text("Выбери игру Telegram:", reply_markup=tg_games_menu())
    elif data == "dice_menu": await query.edit_message_text("Выбери тип ставки на кубик:", reply_markup=dice_menu())
    elif data == "darts_menu": await query.edit_message_text("Выбери тип ставки на дартс:", reply_markup=darts_menu())
    elif data == "orig_games": await query.edit_message_text("Выбери множитель (Краш):", reply_markup=orig_games_menu())
    elif data.startswith("orig_"): await query.edit_message_text(f"Выбери ставку для Краш x{data.split('_')[1]}:", reply_markup=bet_menu(data))
    elif data.startswith("dice_") or data.startswith("darts_"): await query.edit_message_text("Выбери ставку:", reply_markup=bet_menu(data))

    elif data.startswith("bet_"):
        parts = data.split("_")
        bet = float(parts[-1])
        game_data = "_".join(parts[1:-1])
        
        if balance < bet:
            await query.edit_message_text("❌ Недостаточно средств!", reply_markup=play_menu())
            return
        
        if game_data.startswith("orig_"):
            mult = int(game_data.split("_")[1])
            update_balance(user_id, -bet)
            if random.randint(1, 100) <= (100 / mult):
                win = bet * mult
                update_balance(user_id, win)
                await query.edit_message_text(f"🎉 Выиграл x{mult}! +{win:.2f}$\n💰 Баланс: {get_user(user_id)[0]:.2f}$", reply_markup=play_menu())
            else:
                await query.edit_message_text(f"💥 Краш! Проигрыш {bet:.2f}$", reply_markup=play_menu())
        
        elif game_data.startswith("dice_"):
            update_balance(user_id, -bet)
            msg = await query.message.chat.send_dice(emoji="🎲")
            await asyncio.sleep(4)
            res = msg.dice.value
            win = 0
            if game_data == "dice_even" and res % 2 == 0: win = bet * 2
            elif game_data == "dice_odd" and res % 2 != 0: win = bet * 2
            elif game_data == "dice_more" and res > 3: win = bet * 2
            elif game_data == "dice_less" and res < 4: win = bet * 2
            elif game_data == "dice_center" and res in [3, 4]: win = bet * 2
            elif game_data == "dice_edges" and res in [1, 6]: win = bet * 3
            if win > 0:
                update_balance(user_id, win)
                await query.message.reply_text(f"🎲 Выпало: {res}\n✅ Победа! +{win:.2f}$", reply_markup=play_menu())
            else:
                await query.message.reply_text(f"🎲 Выпало: {res}\n❌ Проигрыш. -{bet:.2f}$", reply_markup=play_menu())

        elif game_data.startswith("darts_"):
            update_balance(user_id, -bet)
            msg = await query.message.chat.send_dice(emoji="🎯")
            await asyncio.sleep(4)
            res = msg.dice.value
            win = 0
            if game_data == "darts_red" and res in [1, 3, 5]: win = bet * 2
            elif game_data == "darts_white" and res in [2, 4, 6]: win = bet * 2
            elif game_data == "darts_bounce" and res == 1: win = bet * 5
            elif game_data == "darts_center" and res == 6: win = bet * 4
            elif game_data == "darts_any" and res in [2, 3, 4, 5]: win = bet * 1.5
            if win > 0:
                update_balance(user_id, win)
                await query.message.reply_text(f"🎯 Выпало: {res}\n✅ Победа! +{win:.2f}$", reply_markup=play_menu())
            else:
                await query.message.reply_text(f"🎯 Выпало: {res}\n❌ Проигрыш. -{bet:.2f}$", reply_markup=play_menu())

    # --- МИНЫ (ИСПРАВЛЕНО) ---
    elif data == "mines_start": await query.edit_message_text("Выбери ставку для Мин:", reply_markup=bet_menu("mines"))
    elif data.startswith("bet_mines_"):
        bet = float(data.split("_")[2])
        if balance < bet: 
            await query.edit_message_text("❌ Недостаточно средств!")
            return
        update_balance(user_id, -bet)
        games_state[user_id] = {"game": "mines", "bet": bet, "mines": random.sample(range(25), 5), "opened": 0}
        keyboard = [[InlineKeyboardButton("⬜", callback_data=f"m_open_{i*5+j}") for j in range(5)] for i in range(5)]
        keyboard.append([InlineKeyboardButton("💰 Забрать", callback_data="m_cashout")])
        await query.edit_message_text(f"💣 Мины. Ставка: {bet}$. Открыто: 0. Множитель: x1.0", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith("m_open_"):
        state = games_state.get(user_id)
        if not state or state["game"] != "mines": return
        idx = int(data.split("_")[2])
        if idx in state["mines"]:
            del games_state[user_id]
            await query.edit_message_text(f"💥 Бум! Ты проиграл {state['bet']:.2f}$", reply_markup=play_menu())
            return
        state["opened"] += 1
        mult = 1 + (state["opened"] * 0.2)
        # Перерисовываем клавиатуру
        keyboard = []
        for i in range(5):
            row = []
            for j in range(5):
                cur = i*5 + j
                if cur == idx: row.append(InlineKeyboardButton("💎", callback_data="ignore"))
                else: row.append(InlineKeyboardButton("⬜", callback_data=f"m_open_{cur}"))
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("💰 Забрать", callback_data="m_cashout")])
        await query.edit_message_text(f"💣 Мины. Ставка: {state['bet']}$. Открыто: {state['opened']}. Множитель: x{mult:.1f}", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data == "m_cashout":
        state = games_state.get(user_id)
        if not state or state["game"] != "mines": return
        win = state["bet"] * (1 + state["opened"] * 0.2)
        update_balance(user_id, win)
        del games_state[user_id]
        await query.edit_message_text(f"💎 Забрал {win:.2f}$", reply_markup=play_menu())

    # --- БАШНЯ (ИСПРАВЛЕНО) ---
    elif data == "tower_start": await query.edit_message_text("Выбери ставку для Башни:", reply_markup=bet_menu("tower"))
    elif data.startswith("bet_tower_"):
        bet = float(data.split("_")[2])
        if balance < bet: 
            await query.edit_message_text("❌ Недостаточно средств!")
            return
        update_balance(user_id, -bet)
        games_state[user_id] = {"game": "tower", "bet": bet, "level": 1}
        await render_tower(query, user_id)
    elif data.startswith("t_step_"):
        step = int(data.split("_")[2])
        state = games_state.get(user_id)
        if not state or state["game"] != "tower": return
        if step == random.randint(0, 4):
            del games_state[user_id]
            await query.edit_message_text(f"💀 Мина! Проигрыш {state['bet']:.2f}$", reply_markup=play_menu())
            return
        state["level"] += 1
        if state["level"] > 6:
            win = state["bet"] * 2
            update_balance(user_id, win)
            del games_state[user_id]
            await query.edit_message_text(f"🏆 Башня пройдена! Выигрыш {win:.2f}$", reply_markup=play_menu())
            return
        await render_tower(query, user_id)

    # --- АДМИНКА ---
    elif data == "admin_panel" and is_admin(user_id): await query.edit_message_text("Админ-панель:", reply_markup=admin_panel())
    elif data == "admin_subs" and is_admin(user_id): await query.edit_message_text("Управление подписками:", reply_markup=subs_menu())
    elif data == "toggle_sub" and is_admin(user_id): set_setting('sub_check', '0' if get_setting('sub_check') == '1' else '1'); await query.edit_message_text("Изменено!", reply_markup=subs_menu())
    elif data == "add_channel_info" and is_admin(user_id): await query.edit_message_text("Отправь @юзернейм канала (бот должен быть там админом).", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_subs")]]))
    elif data.startswith("del_ch_") and is_admin(user_id):
        ch = data.replace("del_ch_", ""); channels = [c for c in get_setting('channels').split(',') if c and c != ch]
        set_setting('channels', ','.join(channels)); await query.edit_message_text("Удален.", reply_markup=subs_menu())
    elif data == "admin_manage_admins" and is_admin(user_id):
        conn = sqlite3.connect('bot.db'); c = conn.cursor(); c.execute("SELECT user_id FROM admins")
        admins = [str(r[0]) for r in c.fetchall()]; conn.close()
        await query.edit_message_text("👑 Админы:\n" + "\n".join(admins) + "\n\nДобавить: `add ID`\nУдалить: `del ID`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_panel")]]))
    elif data == "admin_broadcast" and is_admin(user_id): await query.edit_message_text("Отправь текст для рассылки:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_panel")]]))

async def render_tower(query, user_id):
    state = games_state[user_id]
    text = f"🏰 Башня. Уровень: {state['level']}/6. Множитель: x{1 + state['level']*0.2:.1f}\nВыбери дверь (1 из 5 - мина):"
    keyboard = [[InlineKeyboardButton(str(i+1), callback_data=f"t_step_{i}") for i in range(5)]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

# --- Обработчик текста (Рассылка/Админы/Каналы) ---
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return
    text = update.message.text.strip()
    
    # Проверка на добавление канала
    if text.startswith('@') or text.startswith('-100'):
        try:
            bot_member = await context.bot.get_chat_member(chat_id=text, user_id=context.bot.id)
            if bot_member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
                channels = [c for c in get_setting('channels').split(',') if c]
                if text not in channels:
                    channels.append(text); set_setting('channels', ','.join(channels))
                    await update.message.reply_text(f"✅ Канал {text} добавлен!", reply_markup=subs_menu())
            else: await update.message.reply_text("❌ Бот не админ в канале.")
        except: await update.message.reply_text("❌ Ошибка канала.")
        return

    # Проверка на выдачу баланса (ID сумма)
    parts = text.split()
    if len(parts) == 2 and parts[0].isdigit():
        try:
            target_id = int(parts[0])
            amount = float(parts[1])
            update_balance(target_id, amount)
            await update.message.reply_text(f"✅ Успешно. Баланс {target_id} изменен на {amount}$.")
            return
        except: pass

    if text.startswith("add "):
        try: 
            new_admin = int(text.split()[1])
            conn = sqlite3.connect('bot.db'); c = conn.cursor(); c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (new_admin,)); conn.commit(); conn.close()
            await update.message.reply_text(f"✅ {new_admin} теперь админ.")
        except: pass
        return
    if text.startswith("del "):
        try:
            del_admin = int(text.split()[1])
            if del_admin == SUPER_ADMIN_ID: return await update.message.reply_text("Нельзя удалить главного.")
            conn = sqlite3.connect('bot.db'); c = conn.cursor(); c.execute("DELETE FROM admins WHERE user_id = ?", (del_admin,)); conn.commit(); conn.close()
            await update.message.reply_text(f"✅ {del_admin} лишен прав.")
        except: pass
        return

    # Если не распознали команду - делаем рассылку
    users = get_all_users()
    success = 0
    msg = await update.message.reply_text(f"⏳ Рассылка для {len(users)} чел...")
    for u in users:
        try:
            await context.bot.send_message(chat_id=u, text=text)
            success += 1; await asyncio.sleep(0.05)
        except: pass
    await msg.edit_text(f"✅ Рассылка завершена. Доставлено: {success}")

def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token: raise ValueError("TELEGRAM_TOKEN не задан!")
    init_db()
    Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.Regex("^(💰 Баланс|🎮 Играть|📋 Меню)$"), reply_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("Бот запущен...")
    application.run_polling()

if __name__ == '__main__':
    main()
