import os
import logging
import asyncio
from threading import Thread
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

app = Flask(__name__)

@app.route('/')
def health():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

balances = {}

def get_balance(user_id):
    return balances.get(user_id, 100)

def set_balance(user_id, value):
    balances[user_id] = value

def main_menu():
    keyboard = [
        [InlineKeyboardButton("🎁 Бонус", callback_data="bonus")],
        [InlineKeyboardButton("🎲 Играть", callback_data="play")],
        [InlineKeyboardButton("💰 Баланс", callback_data="balance")],
    ]
    return InlineKeyboardMarkup(keyboard)

def play_menu():
    keyboard = [
        [InlineKeyboardButton("10", callback_data="bet_10"),
         InlineKeyboardButton("50", callback_data="bet_50"),
         InlineKeyboardButton("100", callback_data="bet_100")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
    ]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    get_balance(user_id)
    await update.message.reply_text(
        "Привет! Выбери действие:",
        reply_markup=main_menu()
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "menu":
        await query.edit_message_text("Выбери действие:", reply_markup=main_menu())

    elif data == "balance":
        bal = get_balance(user_id)
        await query.edit_message_text(
            f"💰 Твой баланс: {bal} монет",
            reply_markup=main_menu()
        )

    elif data == "bonus":
        bal = get_balance(user_id)
        set_balance(user_id, bal + 50)
        await query.edit_message_text(
            f"🎁 Бонус получен: +50 монет!\n💰 Баланс: {bal + 50}",
            reply_markup=main_menu()
        )

    elif data == "play":
        bal = get_balance(user_id)
        await query.edit_message_text(
            f"💰 Баланс: {bal}\n\nВыбери ставку.\nПобеда = число > 3, выигрыш ×2.",
            reply_markup=play_menu()
        )

    elif data.startswith("bet_"):
        bet = int(data.split("_")[1])
        bal = get_balance(user_id)

        if bal < bet:
            await query.edit_message_text(
                f"❌ Недостаточно монет.\n💰 Баланс: {bal}",
                reply_markup=play_menu()
            )
            return

        set_balance(user_id, bal - bet)
        dice_msg = await query.message.chat.send_dice(emoji="🎲")
        await asyncio.sleep(4)
        result = dice_msg.dice.value

        if result > 3:
            win = bet * 2
            new_bal = get_balance(user_id) + win
            set_balance(user_id, new_bal)
            text = (
                f"🎲 Выпало: {result}\n\n"
                f"✅ Победа! +{win} монет\n"
                f"💰 Баланс: {new_bal}"
            )
        else:
            new_bal = get_balance(user_id)
            text = (
                f"🎲 Выпало: {result}\n\n"
                f"❌ Проигрыш. -{bet} монет\n"
                f"💰 Баланс: {new_bal}"
            )

        keyboard = [
            [InlineKeyboardButton("🎲 Играть снова", callback_data="play")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
        ]
        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_TOKEN не задан!")

    Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))

    print("Бот запущен...")
    application.run_polling()

if __name__ == '__main__':
    main()
