import logging
import random
import string
import requests
import asyncio
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

# --- Config ---
BOT_TOKEN = "8467801272:AAGB5sy8q5CBp4ktLhPmTvCriF3d4t7vAbI"

# Maxelpay API creds (replace with yours)
MAXELPAY_API_KEY = "KU18KjYD8ajrAaEHQBnAByXFQEsJRYdp"
MAXELPAY_SECRET_KEY = "Alwq2y1565E5u5vNVzEhViwVYOcfkj0c"

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Flask app
flask_app = Flask(__name__)

# --- Bot Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🚀 Start Trading", url="https://axiom.trade/@sarvesh")],
        [InlineKeyboardButton("💎 War Room", callback_data="warroom")],
        [InlineKeyboardButton("🔑 Subscribe", callback_data="subscribe")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("👋 Welcome to *Crypto With Clarity Bot* 🚀", reply_markup=reply_markup, parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        """🤖 Here are the available commands:

/start - Welcome 🎉
/about - About the bot ℹ️
/earn - Earn rewards 💰
/help - Show this help message ❓
/subscribe - Get subscription plans 🔑"""
    )

async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        """ℹ️ About CryptoWithClarity Bot:

This bot helps you explore crypto insights 🚀,
earn rewards 💰, and subscribe 🔑 for premium access.

Made to simplify crypto for everyone!"""
    )

async def earn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        """💰 Earn Menu:

👉 Invite friends and earn rewards 🎉
👉 Complete tasks and get bonuses 🔥
👉 Join our community 🚀"""
    )

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💵 $10 Plan", callback_data="sub_10")],
        [InlineKeyboardButton("💵 $20 Plan", callback_data="sub_20")],
        [InlineKeyboardButton("💵 $50 Plan", callback_data="sub_50")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🔑 Choose your subscription plan:", reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "warroom":
        await query.edit_message_text("💎 *War Room Perks:*

✅ AI Prompts
✅ Bot Tools for Trades
✅ Exclusive Market Insights 🚀", parse_mode="Markdown")

    elif query.data.startswith("sub_"):
        plan = query.data.split("_")[1]
        amount = {"10": "10", "20": "20", "50": "50"}[plan]

        # Generate fake dynamic link (replace with real API call later)
        user_id = query.from_user.id
        fake_link = f"https://checkout.maxelpay.com/invoice?id=MX_INV_{amount}_{user_id}"
        await query.edit_message_text(f"💵 Subscription Plan Selected: ${amount}

👉 [Pay Now]({fake_link})", parse_mode="Markdown")

# --- Flask Webhook ---
@flask_app.route('/webhook', methods=['POST'])
def payment_webhook():
    data = request.json
    logging.info(f"Payment Webhook: {data}")
    return {"status": "ok"}

# --- Main ---
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(CommandHandler("earn", earn_command))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Run bot + Flask together
    loop = asyncio.get_event_loop()

    async def run_bot():
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        await app.updater.idle()

    from threading import Thread
    def run_flask():
        flask_app.run(host="0.0.0.0", port=5000)

    Thread(target=run_flask).start()
    loop.run_until_complete(run_bot())

if __name__ == "__main__":
    main()
