import os
import json
import logging
import secrets
import requests
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
API_KEY = os.getenv("MAXELPAY_API_KEY", "YOUR_MAXELPAY_API_KEY")
SECRET_KEY = os.getenv("MAXELPAY_SECRET_KEY", "YOUR_MAXELPAY_SECRET_KEY")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Telegram command: start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome! Use /buy10 /buy20 /buy50 to purchase a subscription.")

# Generate payment link (dummy for testing)
def generate_payment_link(amount, user_id):
    return f"https://checkout.maxelpay.com/invoice?id=MX_INV_{amount}_{user_id}"

async def buy10(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    link = generate_payment_link(10, user_id)
    await update.message.reply_text(f"Pay $10 here: {link}")

async def buy20(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    link = generate_payment_link(20, user_id)
    await update.message.reply_text(f"Pay $20 here: {link}")

async def buy50(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    link = generate_payment_link(50, user_id)
    await update.message.reply_text(f"Pay $50 here: {link}")

# Extra commands
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/start - Welcome
/buy10 - Buy $10 plan
/buy20 - Buy $20 plan
/buy50 - Buy $50 plan
/earn - Info on earning
/about - About this bot")

async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("This is a subscription bot powered by MaxelPay.")

async def earn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("You can earn rewards by inviting friends!")

# Flask webhook
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    logger.info(f"Webhook received: {data}")
    return {"status": "ok"}

def main():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("buy10", buy10))
    application.add_handler(CommandHandler("buy20", buy20))
    application.add_handler(CommandHandler("buy50", buy50))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("about", about))
    application.add_handler(CommandHandler("earn", earn))

    # Start bot in background
    import threading
    threading.Thread(target=lambda: application.run_polling()).start()

    # Run Flask server
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

if __name__ == "__main__":
    main()
