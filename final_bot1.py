import logging
import sqlite3
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from flask import Flask, request, jsonify
import threading
import requests

# ----------------- CONFIG -----------------
BOT_TOKEN = "8467801272:AAGB5sy8q5CBp4ktLhPmTvCriF3d4t7vAbI"  # Your bot token
DATABASE = "db.sqlite3"
MAXELPAY_API_KEY = "KU18KjYD8ajrAaEHQBnAByXFQEsJRYdp"
# ------------------------------------------

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

def update_referral(referrer_id, commission):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("UPDATE users SET referrals=referrals+1, commission=commission+? WHERE user_id=?", (commission, referrer_id))
    conn.commit()
    conn.close()

# ----------------- TELEGRAM BOT -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or ""
    add_user(user_id, username)

    buttons = [
        [InlineKeyboardButton("🌐 Public Community", url="https://t.me/+jUlj8kNrBRg2NGY9")],
        [InlineKeyboardButton("💎 Warroom", callback_data="warroom")],
        [InlineKeyboardButton("🎁 Airdrop Community", url="https://t.me/+qmz3WHjuvjcxYjM1")],
        [InlineKeyboardButton("📩 Contact Support", url="https://t.me/CryptoWith_Sarvesh")],
        [InlineKeyboardButton("📈 Start Trading", url="https://axiom.trade/@sarvesh")],
        [InlineKeyboardButton("💰 Earn", callback_data="earn")],
        [InlineKeyboardButton("❓ Help", callback_data="help")]
    ]
    keyboard = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("👋 Welcome to CryptoWithClarity Bot! Choose an option:", reply_markup=keyboard)

# ----------------- WARROOM -----------------
async def warroom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    user = get_user(user_id)

    if not user or not user[2]:  # subscription_plan
        buttons = [
            [InlineKeyboardButton("💵 $10 / week", callback_data="sub_10")],
            [InlineKeyboardButton("💰 $20 / month", callback_data="sub_20")],
            [InlineKeyboardButton("💎 $50 / 3 months", callback_data="sub_50")]
        ]
        await query.message.reply_text(
            "Warroom is available for subscribed users only.\nChoose a plan to subscribe:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        perks = "🔥 Warroom Perks:\n- AI Prompts\n- Bot Tools for Trades\n- Exclusive Community Access"
        await query.message.reply_text(perks)

# ----------------- SUBSCRIBE -----------------
async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    if data == "sub_10":
        plan = "$10 / week"
        days = 7
    elif data == "sub_20":
        plan = "$20 / month"
        days = 30
    elif data == "sub_50":
        plan = "$50 / 3 months"
        days = 90
    else:
        return

    # Generate Maxelpay payment link
    payment_link = f"https://maxelpay.com/pay?api_key={MAXELPAY_API_KEY}&amount={plan.split()[0].replace('$','')}&user_id={user_id}"

    await query.message.reply_text(f"💳 Payment link: {payment_link}\nAfter payment, your subscription will be activated automatically.")

# ----------------- EARN -----------------
async def earn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    user = get_user(user_id)

    if not user or not user[2]:
        await query.message.reply_text("💡 You need a subscription to start earning via referral!")
        return

    ref_link = f"https://t.me/YourBotUsername?start={user_id}"
    await query.message.reply_text(f"💰 Your referral link:\n{ref_link}\nYour commission: ${user[5]:.2f}\nReferrals: {user[4]}")

# ----------------- HELP -----------------
async def help_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    help_text = "❓ Help Menu:\n\n" \
                "Use the buttons to navigate.\n" \
                "💎 Warroom: Paid subscription access.\n" \
                "🎁 Airdrop: Join our airdrop community.\n" \
                "💰 Earn: Share your referral link and earn commission.\n" \
                "📈 Start Trading: Our recommended platform.\n" \
                "📩 Contact Support: Get support anytime."
    await query.message.reply_text(help_text)

# ----------------- HANDLERS -----------------
app_bot = ApplicationBuilder().token(BOT_TOKEN).build()
app_bot.add_handler(CommandHandler("start", start))
app_bot.add_handler(CallbackQueryHandler(warroom, pattern="^warroom$"))
app_bot.add_handler(CallbackQueryHandler(subscribe, pattern="^sub_"))
app_bot.add_handler(CallbackQueryHandler(earn, pattern="^earn$"))
app_bot.add_handler(CallbackQueryHandler(help_menu, pattern="^help$"))

# ----------------- FLASK -----------------
flask_app = Flask(__name__)

@flask_app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    # Parse Maxelpay webhook and activate subscription
    user_id = data.get("user_id")
    amount = data.get("amount")
    if user_id and amount:
        if amount == 10:
            update_subscription(user_id, "$10 / week", 7)
        elif amount == 20:
            update_subscription(user_id, "$20 / month", 30)
        elif amount == 50:
            update_subscription(user_id, "$50 / 3 months", 90)
    return jsonify({"status": "ok"})

def run_flask():
    flask_app.run(host="0.0.0.0", port=5000)

# ----------------- MAIN -----------------
if __name__ == "__main__":
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    print("🤖 Bot is running... Press Ctrl+C to stop.")
    app_bot.run_polling()
