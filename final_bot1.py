import logging
import sqlite3
import threading
import requests
import json
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# ----------------- CONFIG -----------------
BOT_TOKEN = "8467801272:AAGB5sy8q5CBp4ktLhPmTvCriF3d4t7vAbI"
DATABASE = "db.sqlite3"
NOWPAYMENTS_API_KEY = "FG43MR3-RHPM8ZK-GCQ5VYD-SNCHJ3C"
NOWPAYMENTS_IPN_SECRET = "hCtlqRpYс7rTkK5e9eZQDbv6MimGSZkC"
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
            referral_id TEXT,
            referred_by TEXT,
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

def add_user(user_id, username, referred_by=None):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    referral_id = f"ref{user_id}"
    c.execute("INSERT OR IGNORE INTO users (user_id, username, referral_id, referred_by) VALUES (?,?,?,?)",
              (user_id, username, referral_id, referred_by))
    conn.commit()
    conn.close()
    return referral_id

def update_subscription(user_id, plan, days):
    end_date = datetime.now() + timedelta(days=days)
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("UPDATE users SET subscription_plan=?, subscription_end=? WHERE user_id=?",
              (plan, end_date.strftime("%Y-%m-%d %H:%M:%S"), user_id))
    conn.commit()
    conn.close()

def add_commission(referral_id, amount):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("UPDATE users SET commission = commission + ? WHERE referral_id=?", (amount, referral_id))
    c.execute("UPDATE users SET referrals = referrals + 1 WHERE referral_id=?", (referral_id,))
    conn.commit()
    conn.close()

# ----------------- NOWPAYMENTS -----------------
def generate_payment_link(user_id, plan, amount, days):
    payload = {
        "price_amount": amount,
        "price_currency": "usd",
        "pay_currency": "usd",
        "order_id": f"{user_id}_{int(datetime.now().timestamp())}",
        "ipn_callback_url": f"https://cryptowithsarveshbot.onrender.com/webhook?user_id={user_id}&plan={plan}&days={days}"
    }
    headers = {"x-api-key": NOWPAYMENTS_API_KEY, "Content-Type": "application/json"}
    try:
        res = requests.post("https://api.nowpayments.io/v1/invoice", headers=headers, json=payload)
        data = res.json()
        if "invoice_url" in data:
            return data["invoice_url"]
        return "Error generating payment link"
    except Exception as e:
        logging.error(f"NowPayments error: {e}")
        return "Error generating payment link"

# ----------------- TELEGRAM BOT -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or ""
    referred_by = None
    if update.message.text and "ref" in update.message.text:
        referred_by = update.message.text.split("ref")[1]
    add_user(user_id, username, referred_by)

    buttons = [
        [InlineKeyboardButton("🌍 Public Community", url="https://t.me/+jUlj8kNrBRg2NGY9")],
        [InlineKeyboardButton("🔥 Warroom", callback_data="warroom")],
        [InlineKeyboardButton("🎁 Airdrop Community", url="https://t.me/+qmz3WHjuvjcxYjM1")],
        [InlineKeyboardButton("📞 Support Team", url="https://t.me/CryptoWith_Sarvesh")],
        [InlineKeyboardButton("💹 Start Trading", url="https://axiom.trade/@sarvesh")],
        [InlineKeyboardButton("ℹ️ About", callback_data="about")],
        [InlineKeyboardButton("💰 Earn", callback_data="earn")],
        [InlineKeyboardButton("🆘 Help", callback_data="help")]
    ]
    keyboard = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("🚀 Welcome to *CryptoWithClarity Bot*!\nChoose an option below:", reply_markup=keyboard, parse_mode="Markdown")

async def warroom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    user = get_user(user_id)

    if not user or not user[2]:
        buttons = [
            [InlineKeyboardButton("💵 $10 / week", callback_data="sub_10")],
            [InlineKeyboardButton("💳 $20 / month", callback_data="sub_20")],
            [InlineKeyboardButton("💎 $50 / 3 months", callback_data="sub_50")]
        ]
        await query.message.reply_text(
            "⚡ Warroom is for subscribed users.\nChoose a plan to subscribe:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        perks = "🔥 *Warroom Perks:*\n- AI Prompts\n- Trading Bot Tools\n- Exclusive Community Access"
        await query.message.reply_text(perks, parse_mode="Markdown")

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    if data == "sub_10":
        plan, amount, days = "Weekly", 10, 7
    elif data == "sub_20":
        plan, amount, days = "Monthly", 20, 30
    elif data == "sub_50":
        plan, amount, days = "3-Months", 50, 90
    else:
        return

    await query.message.reply_text("⏳ Generating your payment link, please wait...")
    payment_link = generate_payment_link(user_id, plan, amount, days)
    await query.message.reply_text(f"✅ Click below to complete payment:\n{payment_link}")

async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.message.reply_text("ℹ️ *About Us:*\nCryptoWithClarity provides AI trading prompts, signals, and premium communities.", parse_mode="Markdown")

async def earn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    user = get_user(user_id)
    referral_id = user[4] if user else f"ref{user_id}"
    await update.callback_query.message.reply_text(
        f"💰 *Earn Program:*\nInvite friends and earn referral commissions!\nYour referral link: https://t.me/cryptowithsarvesh_bot?start={referral_id}",
        parse_mode="Markdown"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.message.reply_text("🆘 *Help Menu:*\n- For support: @CryptoWith_Sarvesh\n- Payment Issues: Contact support\n- General Queries: Use /start", parse_mode="Markdown")

# ----------------- FLASK -----------------
flask_app = Flask(__name__)

@flask_app.route("/webhook", methods=["POST"])
def webhook():
    ipn_secret = request.headers.get("x-nowpayments-ipn-secret")
    if ipn_secret != NOWPAYMENTS_IPN_SECRET:
        return jsonify({"status": "error", "message": "Invalid IPN secret"}), 403

    data = request.json
    user_id = request.args.get("user_id")
    plan = request.args.get("plan")
    days = int(request.args.get("days", 0))
    logging.info(f"IPN received for user {user_id}, plan {plan}, days {days}")

    if user_id and plan and days > 0:
        update_subscription(user_id, plan, days)
        # Add referral commission if user was referred
        user = get_user(int(user_id))
        if user and user[5]:
            add_commission(user[5], 0.25 * days)
        return jsonify({"status": "success", "message": "Subscription updated"})
    return jsonify({"status": "error", "message": "Invalid data"})

def run_flask():
    flask_app.run(host="0.0.0.0", port=5000)

# ----------------- MAIN -----------------
def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    logging.info("Flask server started on background thread.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(warroom, pattern="^warroom$"))
    app.add_handler(CallbackQueryHandler(subscribe, pattern="^sub_"))
    app.add_handler(CallbackQueryHandler(about, pattern="^about$"))
    app.add_handler(CallbackQueryHandler(earn, pattern="^earn$"))
    app.add_handler(CallbackQueryHandler(help_command, pattern="^help$"))

    logging.info("Starting Telegram bot (polling).")
    app.run_polling()

if __name__ == "__main__":
    main()
