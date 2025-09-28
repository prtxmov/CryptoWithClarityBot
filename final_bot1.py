import logging
import sqlite3
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from flask import Flask, request, jsonify
import threading
import requests
import json
import base64
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

# ----------------- CONFIG -----------------
BOT_TOKEN = "8467801272:AAGB5sy8q5CBp4ktLhPmTvCriF3d4t7vAbI"
DATABASE = "db.sqlite3"
MAXELPAY_API_KEY = "KU18KjYD8ajrAaEHQBnAByXFQEsJRYdp"
MAXELPAY_SECRET_KEY = "Alwq2y1565E5u5vNVzEhViwVYOcfkj0c"
WEBHOOK_URL = "https://cryptowithclaritybot.onrender.com/webhook"

# ----------------- LOGGING -----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ----------------- DATABASE -----------------
def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            subscription_plan TEXT,
            subscription_end TEXT,
            referrals INTEGER DEFAULT 0,
            commission REAL DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    user = c.fetchone()
    conn.close()
    return user

def add_user(user_id, username):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?,?)", (user_id, username))
    conn.commit()
    conn.close()

def update_subscription(user_id, plan, days):
    end_date = datetime.now() + timedelta(days=days)
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("UPDATE users SET subscription_plan=?, subscription_end=? WHERE user_id=?",
              (plan, end_date.strftime("%Y-%m-%d %H:%M:%S"), user_id))
    conn.commit()
    conn.close()

# ----------------- MAXELPAY ENCRYPTION -----------------
def encrypt_payload(payload):
    key = MAXELPAY_SECRET_KEY.encode('utf-8')
    cipher = AES.new(key[:32], AES.MODE_CBC, key[:16])
    encrypted = cipher.encrypt(pad(payload.encode('utf-8'), AES.block_size))
    return base64.b64encode(encrypted).decode('utf-8')

def generate_payment_link(user_id, plan, amount, days):
    payload = {
        "orderID": f"{user_id}_{int(datetime.now().timestamp())}",
        "amount": str(amount),
        "currency": "USD",
        "timestamp": str(int(datetime.now().timestamp())),
        "userName": str(user_id),
        "siteName": "CryptoWithClarity",
        "userEmail": f"{user_id}@example.com",
        "redirectUrl": WEBHOOK_URL,
        "websiteUrl": "https://cryptowithclarity.in",
        "cancelUrl": WEBHOOK_URL,
        "webhookUrl": f"{WEBHOOK_URL}?user_id={user_id}&plan_days={days}&plan_name={plan}"
    }
    payload_json = json.dumps(payload)
    encrypted = encrypt_payload(payload_json)
    return f"https://checkout.maxelpay.com/invoice?data={encrypted}&api_key={MAXELPAY_API_KEY}"

# ----------------- TELEGRAM BOT -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or ""
    add_user(user_id, username)

    buttons = [
        [InlineKeyboardButton("🌐 Public Community", url="https://t.me/+jUlj8kNrBRg2NGY9")],
        [InlineKeyboardButton("🛡️ Warroom", callback_data="warroom")],
        [InlineKeyboardButton("🎁 Airdrop Community", url="https://t.me/+qmz3WHjuvjcxYjM1")],
        [InlineKeyboardButton("📩 Contact Support Team", url="https://t.me/CryptoWith_Sarvesh")],
        [InlineKeyboardButton("📈 Start Trading", url="https://axiom.trade/@sarvesh")]
    ]
    keyboard = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("Welcome to CryptoWithClarity Bot!", reply_markup=keyboard)

async def warroom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user or not user[2]:  # subscription_plan
        buttons = [
            [InlineKeyboardButton("💵 $10 / week", callback_data="sub_10")],
            [InlineKeyboardButton("💳 $20 / month", callback_data="sub_20")],
            [InlineKeyboardButton("💰 $50 / 3 months", callback_data="sub_50")]
        ]
        await update.callback_query.message.reply_text(
            "Warroom is available for subscribed users only.\nChoose a plan to subscribe:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        perks = "Warroom Perks:\n- AI Prompts\n- Bot Tools for Trades\n- Exclusive Community Access"
        await update.callback_query.message.reply_text(perks)

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    if data == "sub_10":
        plan = "$10 / week"
        days = 7
        amount = 10
    elif data == "sub_20":
        plan = "$20 / month"
        days = 30
        amount = 20
    elif data == "sub_50":
        plan = "$50 / 3 months"
        days = 90
        amount = 50
    else:
        return

    payment_link = generate_payment_link(user_id, plan, amount, days)
    await query.message.reply_text(f"💳 Your payment link:\n{payment_link}\nAfter payment, your subscription will activate automatically.")

# ----------------- HANDLERS -----------------
app_bot = ApplicationBuilder().token(BOT_TOKEN).build()
app_bot.add_handler(CommandHandler("start", start))
app_bot.add_handler(CallbackQueryHandler(warroom, pattern="^warroom$"))
app_bot.add_handler(CallbackQueryHandler(subscribe, pattern="^sub_"))

# ----------------- FLASK -----------------
flask_app = Flask(__name__)

@flask_app.route("/webhook", methods=["POST", "GET"])
def webhook():
    data = request.json or request.args
    user_id = int(data.get("user_id", 0))
    plan_name = data.get("plan_name", "")
    plan_days = int(data.get("plan_days", 0))

    if user_id and plan_name and plan_days:
        update_subscription(user_id, plan_name, plan_days)
        # Here you can also notify the user in Telegram
        return jsonify({"status": "ok", "message": "Subscription activated"})
    return jsonify({"status": "error", "message": "Invalid data"})

def run_flask():
    flask_app.run(host="0.0.0.0", port=5000)

# ----------------- MAIN -----------------
if __name__ == "__main__":
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    print("🤖 Bot is running... Press Ctrl+C to stop.")
    app_bot.run_polling()
