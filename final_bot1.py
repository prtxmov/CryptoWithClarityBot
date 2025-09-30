import logging
import sqlite3
import threading
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# ---------------- CONFIG ----------------
BOT_TOKEN = "8467801272:AAGB5sy8q5CBp4ktLhPmTvCriF3d4t7vAbI"
DATABASE = "db.sqlite3"

# NowPayments credentials
NOWPAYMENTS_API_KEY = "FG43MR3-RHPM8ZK-GCQ5VYD-SNCHJ3C"  # api key
IPN_SECRET = "hCtlqRpYс7rTkK5e9eZQDbv6MimGSZkC"          # ipn secret

# Public URL of your Flask server (update with your domain or ngrok tunnel)
PUBLIC_URL = "https://your-server.com"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ---------------- DATABASE ----------------
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
            commission REAL DEFAULT 0,
            referred_by INTEGER
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
    c.execute("INSERT OR IGNORE INTO users (user_id, username, referred_by) VALUES (?,?,?)",
              (user_id, username, referred_by))
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

def add_commission(referrer_id, amount):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("UPDATE users SET commission = commission + ? WHERE user_id=?",
              (amount, referrer_id))
    conn.commit()
    conn.close()

# ---------------- NOWPAYMENTS ----------------
def generate_payment_link(user_id, plan, amount, days):
    url = "https://api.nowpayments.io/v1/invoice"
    headers = {
        "x-api-key": NOWPAYMENTS_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "price_amount": amount,
        "price_currency": "usd",
        "order_id": f"{user_id}_{int(datetime.now().timestamp())}",
        "order_description": f"{plan} subscription for {days} days",
        "ipn_callback_url": f"{PUBLIC_URL}/ipn?user_id={user_id}&plan={plan}&days={days}",
        "success_url": "https://t.me/cryptowithsarvesh_bot",
        "cancel_url": "https://t.me/cryptowithsarvesh_bot"
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=20)
        res_data = res.json()
        logging.info(f"NowPayments response: {res_data}")
        if "invoice_url" in res_data:
            return res_data["invoice_url"]
        else:
            return "Error generating payment link"
    except Exception as e:
        logging.error(f"Payment link exception: {e}")
        return "Error generating payment link"

# ---------------- TELEGRAM BOT ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or ""

    referred_by = None
    if context.args and context.args[0].startswith("ref"):
        try:
            referred_by = int(context.args[0].replace("ref", ""))
        except:
            pass

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
    await update.message.reply_text(
        "🚀 Welcome to *CryptoWithSarvesh Bot*!\nChoose an option below:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

async def warroom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    user = get_user(user_id)

    if not user or not user[2]:  # no subscription
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
    await update.callback_query.message.reply_text(
        "ℹ️ *About Us:*\nCryptoWithSarvesh provides AI trading prompts, signals, and premium communities.",
        parse_mode="Markdown"
    )

async def earn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    referral_link = f"https://t.me/cryptowithsarvesh_bot?start=ref{user_id}"
    await update.callback_query.message.reply_text(
        f"💰 *Earn Program:*\nInvite friends and earn referral commissions!\nYour referral link: {referral_link}",
        parse_mode="Markdown"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.message.reply_text(
        "🆘 *Help Menu:*\n- For support: @CryptoWith_Sarvesh\n- Payment Issues: Contact support\n- General Queries: Use /start",
        parse_mode="Markdown"
    )

# ---------------- FLASK ----------------
flask_app = Flask(__name__)

@flask_app.route("/ipn", methods=["POST"])
def ipn_listener():
    data = request.json
    logging.info(f"IPN Received: {data}")

    try:
        user_id = int(request.args.get("user_id"))
        plan = request.args.get("plan")
        days = int(request.args.get("days", 0))

        if data.get("payment_status") == "finished":
            update_subscription(user_id, plan, days)

            # handle referral commission
            user = get_user(user_id)
            referred_by = user[6] if user else None
            if referred_by:
                commission = 0.25 * float(data.get("price_amount", 0))
                add_commission(referred_by, commission)

        return jsonify({"status": "ok"})
    except Exception as e:
        logging.error(f"IPN error: {e}")
        return jsonify({"status": "error", "message": str(e)})

def run_flask():
    flask_app.run(host="0.0.0.0", port=5000)

# ---------------- MAIN ----------------
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
