import os
import logging
import asyncio
import sqlite3
import random
from threading import Thread
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# ================= НАСТРОЙКИ =================
SUPER_ADMIN_ID = 8192612489  # Твой ID (главный админ)
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
    
    # Состояния игр (в памяти)
    c.execute('''CREATE TABLE IF NOT EXISTS games_state
                 (user_id INTEGER PRIMARY KEY, game_type TEXT, bet REAL, state_data TEXT)''')
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

def save_game_state(user_id, game_type, bet, state_data):
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO games_state (user_id, game_type, bet, state_data) VALUES (?, ?, ?, ?)", (user_id, game_type, bet, state_data))
    conn.commit()
    conn.close()

def get_game_state(user_id):
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute("SELECT game_type, bet, state_data FROM games_state WHERE user_id = ?", (user_id,))
    res = c.fetchone()
    conn.close()
    return res

def clear_game_state(user_id):
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute("DELETE FROM games_state WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

# --- Клавиатуры ---
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Баланс", callback_data="balance"), InlineKeyboardButton("🎮 Играть", callback_data="play_menu")],
        [InlineKeyboardButton("📋 Меню", callback_data="menu")]
    ])

def get_main_menu(user_id):
    keyboard = [
        [InlineKeyboardButton("💰 Баланс", callback_data="balance"), InlineKeyboardButton("🎮 Играть", callback_data="play_menu")],
        [InlineKeyboardButton("📋 Меню", callback_data="menu")]
    ]
    if is_admin(user_id):
        keyboard.append([InlineKeyboardButton("⚙️ Админка", callback_data="admin_panel")])
    return InlineKeyboardMarkup(keyboard)

def play_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 Телеграм игры", callback_data="tg_games"), InlineKeyboardButton("🎰 Авторские (Краш)", callback_data="orig_games")],
        [InlineKeyboardButton("💣 Мины", callback_data="mines"), InlineKeyboardButton("🏰 Башня", callback_data="tower")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu")]
    ])

def tg_games_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 Кубик", callback_data="dice_menu")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="play_menu")]
    ])

def dice_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Чет", callback_data="dice_bet_even_0.5"), InlineKeyboardButton("Нечет", callback_data="dice_bet_odd_0.5")],
        [InlineKeyboardButton("Больше 3", callback_data="dice_bet_more_0.5"), InlineKeyboardButton("Меньше 4", callback_data="dice_bet_less_0.5")],
        [InlineKeyboardButton("Центр (3,4)", callback_data="dice_bet_center_0.5"), InlineKeyboardButton("Края (1,6)", callback_data="dice_bet_edges_0.5")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="tg_games")]
    ])

def orig_games_menu():
    multipliers = ["2", "3", "4", "5", "10", "30", "50", "100"]
    keyboard = []
    row = []
    for m in multipliers:
        row.append(InlineKeyboardButton(f"x{m}", callback_data=f"orig_bet_{m}_0.5"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="play_menu")])
    return InlineKeyboardMarkup(keyboard)

def bet_menu(game_type):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("0.5$", callback_data=f"bet_{game_type}_0.5"),
         InlineKeyboardButton("1$", callback_data=f"bet_{game_type}_1.0"),
         InlineKeyboardButton("2$", callback_data=f"bet_{game_type}_2.0")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="play_menu")]
    ])

def admin_panel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Выдать $", callback_data="admin_add"), InlineKeyboardButton("➖ Забрать $", callback_data="admin_remove")],
        [InlineKeyboardButton("⚙️ Настройки подписок", callback_data="admin_subs")],
        [InlineKeyboardButton("👑 Админы", callback_data="admin_manage_admins")],
        [InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu")]
    ])

def subs_menu():
    channels_str = get_setting('channels')
    channels = [c for c in channels_str.split(',') if c]
    check_on = get_setting('sub_check') == '1'
    keyboard = []
    for ch in channels:
        keyboard.append([InlineKeyboardButton(f"❌ Удалить {ch}", callback_data=f"del_ch_{ch}")])
    keyboard.append([InlineKeyboardButton("🟢 Проверка ВКЛ" if check_on else "🔴 Проверка ВЫКЛ", callback_data="toggle_sub")])
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

    await update.message.reply_text(f"Привет! Твой баланс: {user[0]:.2f}$", reply_markup=get_main_menu(user_id))

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У тебя нет прав.")
        return
    await update.message.reply_text("Админ-панель:", reply_markup=admin_panel())

# --- Обработчик кнопок ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    user = get_user(user_id)
    balance = user[0]

    if data == "menu":
        await query.edit_message_text(f"Твой баланс: {balance:.2f}$", reply_markup=get_main_menu(user_id))
    elif data == "balance":
        await query.edit_message_text(f"💰 Баланс: {balance:.2f}$", reply_markup=get_main_menu(user_id))
    
    # --- ИГРЫ ---
    elif data == "play_menu":
        await query.edit_message_text("Выбери раздел игр:", reply_markup=play_menu())
    elif data == "tg_games":
        await query.edit_message_text("Выбери игру Telegram:", reply_markup=tg_games_menu())
    elif data == "dice_menu":
        await query.edit_message_text("Выбери тип ставки на кубик:", reply_markup=dice_menu())
    elif data == "orig_games":
        await query.edit_message_text("Выбери множитель (Краш). Если повезет - умножишь ставку!", reply_markup=orig_games_menu())
    
    # --- АВТОРСКИЕ ИГРЫ (КРАШ) ---
    elif data.startswith("orig_bet_"):
        parts = data.split("_")
        mult = int(parts[2])
        bet = float(parts[3])
        if balance < bet:
            await query.edit_message_text("❌ Недостаточно средств!", reply_markup=play_menu())
            return
        
        update_balance(user_id, -bet)
        # Шанс выигрыша = 100 / множитель (например, для x2 - 50%)
        chance = 100 / mult
        if random.randint(1, 100) <= chance:
            win = bet * mult
            update_balance(user_id, win)
            await query.edit_message_text(f"🎉 Ты выиграл x{mult}! +{win:.2f}$\n💰 Баланс: {get_user(user_id)[0]:.2f}$", reply_markup=play_menu())
        else:
            await query.edit_message_text(f"💥 Краш! Ты проиграл {bet:.2f}$\n💰 Баланс: {get_user(user_id)[0]:.2f}$", reply_markup=play_menu())

    # --- КУБИК (СТАВКИ) ---
    elif data.startswith("dice_bet_"):
        parts = data.split("_")
        bet_type = parts[2]
        bet = float(parts[3])
        if balance < bet:
            await query.edit_message_text("❌ Недостаточно средств!", reply_markup=play_menu())
            return
        update_balance(user_id, -bet)
        
        msg = await query.message.chat.send_dice(emoji="🎲")
        await asyncio.sleep(4)
        res = msg.dice.value
        
        win = 0
        win_condition = False
        
        if bet_type == "even" and res % 2 == 0: win_condition = True; win = bet * 2
        elif bet_type == "odd" and res % 2 != 0: win_condition = True; win = bet * 2
        elif bet_type == "more" and res > 3: win_condition = True; win = bet * 2
        elif bet_type == "less" and res < 4: win_condition = True; win = bet * 2
        elif bet_type == "center" and res in [3, 4]: win_condition = True; win = bet * 2
        elif bet_type == "edges" and res in [1, 6]: win_condition = True; win = bet * 3
        
        if win_condition:
            update_balance(user_id, win)
            await query.message.reply_text(f"🎲 Выпало: {res}\n✅ Победа! +{win:.2f}$", reply_markup=play_menu())
        else:
            await query.message.reply_text(f"🎲 Выпало: {res}\n❌ Проигрыш. -{bet:.2f}$", reply_markup=play_menu())

    # --- МИНЫ ---
    elif data.startswith("mines_bet_"):
        bet = float(data.split("_")[2])
        if balance < bet:
            await query.edit_message_text("❌ Недостаточно средств!", reply_markup=play_menu())
            return
        update_balance(user_id, -bet)
        
        # Генерируем поле 5x5 (25 клеток). Пусть будет 5 мин.
        mines = random.sample(range(25), 5)
        save_game_state(user_id, "mines", bet, f"mines:{','.join(map(str, mines))}:0") # state: тип:мины:открыто
        
        # Рисуем поле 5x5
        keyboard = []
        for i in range(5):
            row = []
            for j in range(5):
                idx = i*5 + j
                row.append(InlineKeyboardButton("⬜", callback_data=f"mines_open_{idx}"))
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("💰 Забрать выигрыш", callback_data="mines_cashout")])
        
        await query.edit_message_text(f"💣 Игра Мины. Ставка: {bet}$. Открывай клетки, но не попади на мину!\nТекущий множитель: x1.0", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("mines_open_"):
        state = get_game_state(user_id)
        if not state or state[0] != "mines": return
        
        idx = int(data.split("_")[2])
        parts = state[2].split(":")
        mines = list(map(int, parts[1].split(",")))
        opened = int(parts[2])
        bet = state[1]
        
        if idx in mines:
            clear_game_state(user_id)
            await query.edit_message_text(f"💥 Бум! Ты попал на мину и проиграл {bet:.2f}$", reply_markup=play_menu())
            return
        
        opened += 1
        mult = 1 + (opened * 0.2) # +0.2x за каждую открытую клетку
        save_game_state(user_id, "mines", bet, f"mines:{','.join(map(str, mines))}:{opened}")
        
        # Обновляем клавиатуру
        keyboard = []
        for i in range(5):
            row = []
            for j in range(5):
                cur_idx = i*5 + j
                if cur_idx == idx:
                    row.append(InlineKeyboardButton("💎", callback_data="ignore"))
                else:
                    row.append(InlineKeyboardButton("⬜", callback_data=f"mines_open_{cur_idx}"))
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("💰 Забрать выигрыш", callback_data="mines_cashout")])
        
        await query.edit_message_text(f"💣 Игра Мины. Ставка: {bet}$.\nОткрыто: {opened}. Множитель: x{mult:.1f}", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "mines_cashout":
        state = get_game_state(user_id)
        if not state or state[0] != "mines": return
        parts = state[2].split(":")
        opened = int(parts[2])
        bet = state[1]
        mult = 1 + (opened * 0.2)
        win = bet * mult
        update_balance(user_id, win)
        clear_game_state(user_id)
        await query.edit_message_text(f"💎 Ты забрал выигрыш: {win:.2f}$ (x{mult:.1f})\n💰 Баланс: {get_user(user_id)[0]:.2f}$", reply_markup=play_menu())

    # --- БАШНЯ ---
    elif data.startswith("tower_bet_"):
        bet = float(data.split("_")[2])
        if balance < bet:
            await query.edit_message_text("❌ Недостаточно средств!", reply_markup=play_menu())
            return
        update_balance(user_id, -bet)
        save_game_state(user_id, "tower", bet, "tower:1") # уровень 1
        await render_tower(query, user_id, bet, 1, None)

    elif data.startswith("tower_step_"):
        step = int(data.split("_")[2])
        state = get_game_state(user_id)
        if not state or state[0] != "tower": return
        level = int(state[2].split(":")[1])
        bet = state[1]
        
        # 5 дверей, 1 мина
        mine_pos = random.randint(0, 4)
        if step == mine_pos:
            clear_game_state(user_id)
            await query.edit_message_text(f"💀 Ты нашел мину на уровне {level} и проиграл {bet:.2f}$", reply_markup=play_menu())
            return
        
        level += 1
        if level > 6:
            win = bet * 2 # Максимальный множитель
            update_balance(user_id, win)
            clear_game_state(user_id)
            await query.edit_message_text(f"🏆 Ты прошел Башню до конца! Выигрыш: {win:.2f}$", reply_markup=play_menu())
            return
            
        mult = 1 + (level * 0.2)
        save_game_state(user_id, "tower", bet, f"tower:{level}")
        await render_tower(query, user_id, bet, level, mult)

    # --- АДМИНКА ---
    elif data == "admin_panel" and is_admin(user_id):
        await query.edit_message_text("Админ-панель:", reply_markup=admin_panel())
    elif data == "admin_subs" and is_admin(user_id):
        await query.edit_message_text("Управление подписками:", reply_markup=subs_menu())
    elif data == "toggle_sub" and is_admin(user_id):
        current = get_setting('sub_check')
        set_setting('sub_check', '0' if current == '1' else '1')
        await query.edit_message_text("Статус изменен!", reply_markup=subs_menu())
    elif data == "add_channel_info" and is_admin(user_id):
        await query.edit_message_text("Отправь мне @юзернейм или ID канала (бот должен быть админом там).", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_subs")]]))
    elif data.startswith("del_ch_") and is_admin(user_id):
        ch_to_del = data.replace("del_ch_", "")
        channels_str = get_setting('channels')
        channels = [c for c in channels_str.split(',') if c and c != ch_to_del]
        set_setting('channels', ','.join(channels))
        await query.edit_message_text("Канал удален.", reply_markup=subs_menu())
    
    elif data == "admin_manage_admins" and is_admin(user_id):
        conn = sqlite3.connect('bot.db')
        c = conn.cursor()
        c.execute("SELECT user_id FROM admins")
        admins = [row[0] for row in c.fetchall()]
        conn.close()
        text = "👑 Список админов:\n" + "\n".join([str(a) for a in admins])
        text += "\n\nЧтобы добавить/удалить, отправь: `add ID` или `del ID`"
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_panel")]]))
    
    elif data == "admin_broadcast" and is_admin(user_id):
        await query.edit_message_text("Отправь текст для рассылки (следующим сообщением):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_panel")]]))
    
    elif data.startswith("admin_") and is_admin(user_id):
        action = data.split("_")[1]
        await query.edit_message_text(f"Отправь: `ID сумма` (Действие: {action})", parse_mode="Markdown")

async def render_tower(query, user_id, bet, level, mult):
    text = f"🏰 Башня. Ставка: {bet}$. Уровень: {level}/6.\n"
    if mult: text += f"Текущий множитель: x{mult:.1f}\n"
    text += "Выбери дверь. В одной из пяти - мина!"
    
    keyboard = [[
        InlineKeyboardButton("1", callback_data=f"tower_step_0"),
        InlineKeyboardButton("2", callback_data=f"tower_step_1"),
        InlineKeyboardButton("3", callback_data=f"tower_step_2"),
        InlineKeyboardButton("4", callback_data=f"tower_step_3"),
        InlineKeyboardButton("5", callback_data=f"tower_step_4"),
    ]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

# --- Обработчик текста ---
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return
    
    text = update.message.text.strip()
    
    if text.startswith('@') or text.startswith('-100'):
        try:
            bot_member = await context.bot.get_chat_member(chat_id=text, user_id=context.bot.id)
            if bot_member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
                channels_str = get_setting('channels')
                channels = [c for c in channels_str.split(',') if c]
                if text not in channels:
                    channels.append(text)
                    set_setting('channels', ','.join(channels))
                    await update.message.reply_text(f"✅ Канал {text} добавлен!", reply_markup=subs_menu())
                else:
                    await update.message.reply_text("Уже в списке.")
            else:
                await update.message.reply_text("❌ Бот не админ в этом канале.")
        except Exception:
            await update.message.reply_text("❌ Не удалось найти канал.")
        return

    if text.startswith("add "):
        try:
            new_admin = int(text.split()[1])
            conn = sqlite3.connect('bot.db')
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (new_admin,))
            conn.commit()
            conn.close()
            await update.message.reply_text(f"✅ Пользователь {new_admin} теперь админ.")
        except: pass
        return
    
    if text.startswith("del "):
        try:
            del_admin = int(text.split()[1])
            if del_admin == SUPER_ADMIN_ID:
                await update.message.reply_text("Нельзя удалить главного админа.")
                return
            conn = sqlite3.connect('bot.db')
            c = conn.cursor()
            c.execute("DELETE FROM admins WHERE user_id = ?", (del_admin,))
            conn.commit()
            conn.close()
            await update.message.reply_text(f"✅ Пользователь {del_admin} лишен прав.")
        except: pass
        return

    # Рассылка
    users = get_all_users()
    success = 0
    await update.message.reply_text(f"Начинаю рассылку для {len(users)} чел...")
    for u in users:
        try:
            await context.bot.send_message(chat_id=u, text=text)
            success += 1
            await asyncio.sleep(0.05) # чтобы не забанили
        except: pass
    await update.message.reply_text(f"✅ Рассылка завершена. Доставлено: {success}")
    return

def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token: raise ValueError("TELEGRAM_TOKEN не задан!")

    init_db()
    Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(token).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("Бот запущен...")
    application.run_polling()

if __name__ == '__main__':
    main()
