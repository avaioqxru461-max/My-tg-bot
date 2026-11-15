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
ADMIN_ID = 8192612489  # Твой ID
# =============================================

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- Flask для Render ---
app = Flask(__name__)

@app.route('/')
def health():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# --- База данных (SQLite) ---
def init_db():
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, balance REAL DEFAULT 2.0, referrer_id INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('channels', '')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('sub_check', '1')")
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

def set_referrer(user_id, referrer_id):
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute("UPDATE users SET referrer_id = ? WHERE user_id = ? AND referrer_id IS NULL", (referrer_id, user_id))
    conn.commit()
    conn.close()

# --- Клавиатуры ---
def main_menu():
    keyboard = [
        [InlineKeyboardButton("🎁 Бонус", callback_data="bonus")],
        [InlineKeyboardButton("🎮 Игры", callback_data="games")],
        [InlineKeyboardButton("💰 Баланс", callback_data="balance")],
        [InlineKeyboardButton("👥 Рефералы", callback_data="ref")],
    ]
    return InlineKeyboardMarkup(keyboard)

def games_menu():
    keyboard = [
        [InlineKeyboardButton("🎲 Кубик (x2)", callback_data="play_dice")],
        [InlineKeyboardButton("💣 Мины (x1.5)", callback_data="play_mines")],
        [InlineKeyboardButton("🏰 Башня (x2)", callback_data="play_tower")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu")],
    ]
    return InlineKeyboardMarkup(keyboard)

def bet_menu(game_type):
    keyboard = [
        [InlineKeyboardButton("0.5$", callback_data=f"bet_{game_type}_0.5"),
         InlineKeyboardButton("1$", callback_data=f"bet_{game_type}_1.0"),
         InlineKeyboardButton("2$", callback_data=f"bet_{game_type}_2.0")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="games")],
    ]
    return InlineKeyboardMarkup(keyboard)

def admin_menu():
    keyboard = [
        [InlineKeyboardButton("➕ Выдать $", callback_data="admin_add")],
        [InlineKeyboardButton("➖ Забрать $", callback_data="admin_remove")],
        [InlineKeyboardButton("⚙️ Настройки подписок", callback_data="admin_subs")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
    ]
    return InlineKeyboardMarkup(keyboard)

def subs_menu():
    channels_str = get_setting('channels')
    channels = [c for c in channels_str.split(',') if c]
    check_on = get_setting('sub_check') == '1'
    
    keyboard = []
    for ch in channels:
        keyboard.append([InlineKeyboardButton(f"❌ Удалить {ch}", callback_data=f"del_ch_{ch}")])
    
    toggle_text = "🟢 Проверка ВКЛ" if check_on else "🔴 Проверка ВЫКЛ"
    keyboard.append([InlineKeyboardButton(toggle_text, callback_data="toggle_sub")])
    keyboard.append([InlineKeyboardButton("➕ Добавить канал", callback_data="add_channel_info")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin")])
    return InlineKeyboardMarkup(keyboard)

def get_main_keyboard(user_id):
    """Возвращает главное меню с кнопкой админки, если это админ"""
    if user_id == ADMIN_ID:
        keyboard = [
            [InlineKeyboardButton("⚙️ Админка", callback_data="admin")],
            [InlineKeyboardButton("🎁 Бонус", callback_data="bonus")],
            [InlineKeyboardButton("🎮 Игры", callback_data="games")],
            [InlineKeyboardButton("💰 Баланс", callback_data="balance")],
            [InlineKeyboardButton("👥 Рефералы", callback_data="ref")],
        ]
    else:
        keyboard = [
            [InlineKeyboardButton("🎁 Бонус", callback_data="bonus")],
            [InlineKeyboardButton("🎮 Игры", callback_data="games")],
            [InlineKeyboardButton("💰 Баланс", callback_data="balance")],
            [InlineKeyboardButton("👥 Рефералы", callback_data="ref")],
        ]
    return InlineKeyboardMarkup(keyboard)

# --- Проверка подписки ---
async def check_subscription(user_id, context):
    if get_setting('sub_check') != '1':
        return True
        
    channels_str = get_setting('channels')
    channels = [c for c in channels_str.split(',') if c]
    
    if not channels:
        return True

    for channel in channels:
        try:
            member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status not in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
                return False
        except Exception:
            pass
    return True

# --- Обработчики ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    if args and args[0].startswith("ref_"):
        referrer_id = int(args[0].split("_")[1])
        if referrer_id != user_id:
            user = get_user(user_id)
            if user[1] is None:
                set_referrer(user_id, referrer_id)
                update_balance(referrer_id, 1.0)

    user = get_user(user_id)
    
    is_sub = await check_subscription(user_id, context)
    if not is_sub:
        channels_str = get_setting('channels')
        channels = [c for c in channels_str.split(',') if c]
        
        keyboard = []
        for i, ch in enumerate(channels):
            url = f"https://t.me/{ch.replace('@', '')}" if ch.startswith('@') else f"https://t.me/c/{ch.replace('-100', '')}"
            keyboard.append([InlineKeyboardButton(f"📢 Канал {i+1}", url=url)])
        
        keyboard.append([InlineKeyboardButton("✅ Я подписался", callback_data="check_sub")])
        
        await update.message.reply_text("Для использования бота подпишись на наши каналы!", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    await update.message.reply_text(
        f"Привет! Твой баланс: {user[0]:.2f}$\nВыбери действие:",
        reply_markup=get_main_keyboard(user_id)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    user = get_user(user_id)
    balance = user[0]

    if data == "check_sub":
        if await check_subscription(user_id, context):
            await query.edit_message_text(f"Спасибо за подписку!\nТвой баланс: {balance:.2f}$", reply_markup=get_main_keyboard(user_id))
        else:
            await query.edit_message_text("Ты подписался не на все каналы!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Проверить еще раз", callback_data="check_sub")]]))

    elif data == "menu":
        await query.edit_message_text(f"Твой баланс: {balance:.2f}$\nВыбери действие:", reply_markup=get_main_keyboard(user_id))

    elif data == "balance":
        await query.edit_message_text(f"💰 Твой баланс: {balance:.2f}$", reply_markup=main_menu())
    elif data == "bonus":
        if balance < 2.0:
            update_balance(user_id, 1.0)
            await query.edit_message_text(f"🎁 Бонус получен: +1.00$\n💰 Баланс: {balance + 1.0:.2f}$", reply_markup=main_menu())
        else:
            await query.edit_message_text("Бонус доступен только при балансе менее 2$", reply_markup=main_menu())
    elif data == "ref":
        bot_username = (await context.bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        await query.edit_message_text(f"👥 Приглашай друзей и получай 1$ за каждого!\n\nТвоя ссылка:\n`{ref_link}`", parse_mode="Markdown", reply_markup=main_menu())
    elif data == "games":
        await query.edit_message_text("Выбери игру:", reply_markup=games_menu())
    elif data.startswith("play_"):
        game = data.split("_")[1]
        await query.edit_message_text(f"Выбери ставку для игры ({game}):", reply_markup=bet_menu(game))

    # --- Админка ---
    elif data == "admin" and user_id == ADMIN_ID:
        await query.edit_message_text("Админ-панель:", reply_markup=admin_menu())
    elif data == "admin_subs" and user_id == ADMIN_ID:
        await query.edit_message_text("Управление обязательными подписками:", reply_markup=subs_menu())
    elif data == "toggle_sub" and user_id == ADMIN_ID:
        current = get_setting('sub_check')
        new_val = '0' if current == '1' else '1'
        set_setting('sub_check', new_val)
        await query.edit_message_text("Статус проверки изменен!", reply_markup=subs_menu())
    elif data == "add_channel_info" and user_id == ADMIN_ID:
        await query.edit_message_text("Отправь мне юзернейм канала (например, @my_channel) или его ID (например, -100123456789).\n\nВажно: бот должен быть админом в этом канале!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_subs")]]))
    elif data.startswith("del_ch_") and user_id == ADMIN_ID:
        ch_to_del = data.replace("del_ch_", "")
        channels_str = get_setting('channels')
        channels = [c for c in channels_str.split(',') if c and c != ch_to_del]
        set_setting('channels', ','.join(channels))
        await query.edit_message_text("Канал удален из списка.", reply_markup=subs_menu())
    elif data.startswith("admin_") and user_id == ADMIN_ID:
        action = data.split("_")[1]
        await query.edit_message_text(f"Отправь сообщение в формате:\n`ID сумма`\nНапример: `123456789 5.5`\n(Действие: {action})", parse_mode="Markdown")

    # --- Игры ---
    elif data.startswith("bet_"):
        parts = data.split("_")
        game = parts[1]
        bet = float(parts[2])

        if balance < bet:
            await query.edit_message_text("❌ Недостаточно средств!", reply_markup=main_menu())
            return

        if game == "dice":
            update_balance(user_id, -bet)
            await query.edit_message_text(f"🎲 Бросаю кубик на {bet}$...")
            dice_msg = await query.message.chat.send_dice(emoji="🎲")
            await asyncio.sleep(4)
            result = dice_msg.dice.value
            if result > 3:
                win = bet * 2
                update_balance(user_id, win)
                await query.message.reply_text(f"🎲 Выпало: {result}\n✅ Победа! +{win:.2f}$\n💰 Баланс: {get_user(user_id)[0]:.2f}$")
            else:
                await query.message.reply_text(f"🎲 Выпало: {result}\n❌ Проигрыш. -{bet:.2f}$\n💰 Баланс: {get_user(user_id)[0]:.2f}$")
        
        elif game == "mines":
            update_balance(user_id, -bet)
            keyboard = [[InlineKeyboardButton("1️⃣", callback_data=f"mines_win_{bet}"), InlineKeyboardButton("2️⃣", callback_data="mines_lose")],
                        [InlineKeyboardButton("3️⃣", callback_data="mines_lose")]]
            await query.edit_message_text(f"💣 Игра 'Мины'. Одна из кнопок - мина. Выбери кнопку. Ставка: {bet}$", reply_markup=InlineKeyboardMarkup(keyboard))
        
        elif game == "tower":
            update_balance(user_id, -bet)
            keyboard = [[InlineKeyboardButton("Левая 🚪", callback_data=f"tower_win_{bet}"), InlineKeyboardButton("Правая 🚪", callback_data="tower_lose")]]
            await query.edit_message_text(f"🏰 Игра 'Башня'. За одной дверью - приз. Ставка: {bet}$", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("mines_win_"):
        bet = float(data.split("_")[2])
        win = bet * 1.5
        update_balance(user_id, win)
        await query.edit_message_text(f"💥 Ты выжил! Выигрыш: {win:.2f}$\n💰 Баланс: {get_user(user_id)[0]:.2f}$", reply_markup=main_menu())
    elif data == "mines_lose":
        await query.edit_message_text(f"💀 Бум! Ты проиграл.\n💰 Баланс: {get_user(user_id)[0]:.2f}$", reply_markup=main_menu())
    elif data.startswith("tower_win_"):
        bet = float(data.split("_")[2])
        win = bet * 2
        update_balance(user_id, win)
        await query.edit_message_text(f"🏆 Ты нашел приз! Выигрыш: {win:.2f}$\n💰 Баланс: {get_user(user_id)[0]:.2f}$", reply_markup=main_menu())
    elif data == "tower_lose":
        await query.edit_message_text(f"💀 Пусто. Ты проиграл.\n💰 Баланс: {get_user(user_id)[0]:.2f}$", reply_markup=main_menu())

# --- Обработчик текста ---
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id == ADMIN_ID:
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
                        await update.message.reply_text(f"✅ Канал {text} добавлен в обязательные подписки!", reply_markup=subs_menu())
                    else:
                        await update.message.reply_text("Этот канал уже в списке.")
                else:
                    await update.message.reply_text("❌ Бот не является администратором в этом канале. Добавь бота в админы!")
            except Exception as e:
                await update.message.reply_text(f"❌ Ошибка: не удалось найти канал {text}. Проверь юзернейм/ID.")
            return

        try:
            args = text.split()
            if len(args) == 2:
                target_id = int(args[0])
                amount = float(args[1])
                update_balance(target_id, amount)
                await update.message.reply_text(f"✅ Успешно. Баланс {target_id} изменен на {amount}$.")
        except Exception:
            pass

def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_TOKEN не задан!")

    init_db()
    Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("Бот запущен...")
    application.run_polling()

if __name__ == '__main__':
    main()
