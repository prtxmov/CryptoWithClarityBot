#!/usr/bin/env python3
"""
final_bot_live.py

Plug-and-play Telegram bot with:
- Original menu & buttons preserved
- NowPayments invoice generation
- Flask IPN listener to activate subscriptions automatically
- Referral system + commission tracking
- Auto DB schema migration to keep old data safe

Before running: set NOWPAYMENTS_API_KEY, NOWPAYMENTS_IPN_SECRET, PUBLIC_URL.
"""

import logging
import sqlite3
import threading
import requests
import json
import time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# ----------------- CONFIG - EDIT THESE -----------------
BOT_TOKEN = "8467801272:AAGB5sy8q5CBp4ktLhPmTvCriF3d4t7vAbI"
DATABASE = "db.sqlite3"

# Insert your NowPayments API key and IPN secret here (or set environment variables and load them)
NOWPAYMENTS_API_KEY = "YOUR_NOWPAYMENTS_API_KEY"
NOWPAYMENTS_IPN_SECRET = "YOUR_IPN_SECRET"

# Public URL where your Flask app is reachable (Render app URL). Example: "https://my-app.onrender.com"
PUBLIC_URL = "https://your-deployed-app.onrender.com"
# -------------------------------------------------------

# Plans mapping
PLANS = {
    "sub_10": {"label": "Weekly", "amount": 10, "days": 7},
    "sub_20": {"label": "Monthly", "amount": 20, "days": 30},
    "sub_50": {"label": "3-Months", "amount": 50, "days": 90},
}

# Referral commission (25%)
REFERRAL_RATE = 0.25

# NowPayments endpoints
NOWPAYMENTS_INVOICE_URL = "https://api.nowpayments.io/v1/invoice"

# App setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ----------------- DATABASE -----------------
def init_db():
    """Create table if missing and add new columns to existing DB if needed."""
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    # Base table (safe create)
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            subscription_plan TEXT,
            subscription_end TEXT,
            referrals INTEGER DEFAULT 0,
            commission REAL DEFAULT 0,
            referred_by TEXT
        )
    ''')
    conn.commit()

    # Ensure columns exist (in case DB was created earlier with fewer columns)
    # We'll attempt to add commonly-missing columns; ignore errors if they already exist.
    extra_cols = {
        "referral_id": "TEXT",
        "referred_by": "TEXT",
        "referrals": "INTEGER DEFAULT 0",
        "commission": "REAL DEFAULT 0"
    }
    for col, col_def in extra_cols.items():
        try:
            c.execute(f"ALTER TABLE users ADD COLUMN {col} {col_def}")
            logger.info(f"Added missing column `{col}` to users table.")
        except sqlite3.OperationalError:
            # Column already exists, ignore
            pass

    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def add_user(user_id, username, referred_by=None):
    """
    Insert a user if not exists. referral_id will be "ref{user_id}" for simplicity.
    """
    referral_id = f"ref{user_id}"
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""INSERT OR IGNORE INTO users
                 (user_id, username, referral_id, referred_by)
                 VALUES (?, ?, ?, ?)""", (user_id, username, referral_id, referred_by))
    conn.commit()
    conn.close()
    return referral_id

def update_subscription(user_id, plan_label, days):
    end_date = datetime.utcnow() + timedelta(days=days)
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("UPDATE users SET subscription_plan=?, subscription_end=? WHERE user_id=?",
              (plan_label, end_date.strftime("%Y-%m-%d %H:%M:%S"), user_id))
    conn.commit()
    conn.close()
    logger.info(f"Updated subscription for user {user_id}: {plan_label} ({days} days).")

def add_commission(referral_id_or_user_id, amount):
    """
    Increase commission for the referrer. The referrer can be stored as referral_id or as user_id.
    We'll try to update by referral_id first, then by numeric user_id if not.
    """
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    # Try update by referral_id
    c.execute("UPDATE users SET commission = commission + ? WHERE referral_id=?", (amount, referral_id_or_user_id))
    if c.rowcount == 0:
        # maybe the referrer stored as user_id (string or int)
        try:
            ref_int = int(referral_id_or_user_id)
            c.execute("UPDATE users SET commission = commission + ? WHERE user_id=?", (amount, ref_int))
        except Exception:
            pass
    conn.commit()
    conn.close()
    logger.info(f"Added commission {amount} to referrer {referral_id_or_user_id}.")

# ----------------- NOWPAYMENTS -----------------
def generate_payment_link(user_id, plan_key):
    """
    Create NowPayments invoice and return invoice_url or error message.
    plan_key = one of 'sub_10', 'sub_20', 'sub_50'
    """
    if plan_key not in PLANS:
        return None, "Invalid plan"
    plan = PLANS[plan_key]
    amount = plan["amount"]
    days = plan["days"]
    order_id = f"{user_id}_{int(time.time())}"

    payload = {
        "price_amount": amount,
        "price_currency": "usd",
        "order_id": order_id,
        "order_description": f"{plan['label']} subscription for user {user_id}",
        # IPN will call PUBLIC_URL + /ipn and include query params so we know who to credit
        "ipn_callback_url": f"{PUBLIC_URL}/ipn?user_id={user_id}&plan={plan_key}&days={days}",
        "success_url": f"https://t.me/cryptowithsarvesh_bot",
        "cancel_url": f"https://t.me/cryptowithsarvesh_bot"
    }
    headers = {
        "x-api-key": NOWPAYMENTS_API_KEY,
        "Content-Type": "application/json"
    }
    try:
        res = requests.post(NOWPAYMENTS_INVOICE_URL, headers=headers, json=payload, timeout=20)
        logger.info(f"NowPayments create invoice status {res.status_code}")
        data = res.json()
        logger.info(f"NowPayments response: {json.dumps(data)}")
        if "invoice_url" in data:
            return data["invoice_url"], None
        # If NowPayments returns error message
        msg = data.get("error", data)
        return None, f"NowPayments error: {msg}"
    except Exception as e:
        logger.exception("Exception while creating NowPayments invoice")
        return None, str(e)

# ----------------- TELEGRAM BOT HANDLERS -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or ""
    # Support referral param: /start ref123 or /start ref{user_id}
    referred_by = None
    if context.args:
        arg0 = context.args[0]
        if arg0.startswith("ref"):
            # store the ref id literally, bot will check if referrer has subscription on payout
            referred_by = arg0  # e.g. "ref123"
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
        "🚀 Welcome to *CryptoWithClarity Bot*!\nChoose an option below:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

async def warroom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = get_user(user_id)

    if not user or not user[2]:
        # Not subscribed — show plans
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

async def subscribe_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    cb = query.data  # e.g. "sub_10"
    if cb not in PLANS:
        await query.message.reply_text("Invalid plan selected.")
        return
    await query.message.reply_text("⏳ Generating your payment link, please wait...")
    pay_url, err = generate_payment_link(user_id, cb)
    if pay_url:
        await query.message.reply_text(f"✅ Click below to complete payment:\n{pay_url}")
    else:
        await query.message.reply_text(f"❌ Could not create payment link: {err}")

async def about_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.message.reply_text("ℹ️ *About Us:*\nCryptoWithClarity provides AI trading prompts, signals, and premium communities.", parse_mode="Markdown")

async def earn_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    # referral link uses ref{user_id}
    referral_link = f"https://t.me/cryptowithsarvesh_bot?start=ref{user_id}"
    await query.message.reply_text(
        f"💰 *Earn Program:*\nInvite friends and earn referral commissions!\nYour referral link: {referral_link}",
        parse_mode="Markdown"
    )

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.message.reply_text("🆘 *Help Menu:*\n- For support: @CryptoWith_Sarvesh\n- Payment Issues: Contact support\n- General Queries: Use /start", parse_mode="Markdown")

# ----------------- FLASK IPN (NowPayments) -----------------
flask_app = Flask(__name__)

@flask_app.route("/ipn", methods=["POST"])
def ipn_listener():
    """
    NowPayments will POST payment data JSON here.
    We verify the IPN secret header (x-nowpayments-ipn-secret) or field in JSON if present.
    Query params from the invoice creation include user_id, plan, days.
    """
    logger.info("IPN received: headers=%s", dict(request.headers))
    try:
        # Verify secret header (NowPayments sends x-nowpayments-ipn-secret header when configured)
        header_secret = request.headers.get("x-nowpayments-ipn-secret") or request.headers.get("x-nowpayments-signature")
        body = request.get_json(force=True, silent=True) or {}
        logger.info("IPN body: %s", json.dumps(body))

        # Accept if header matches or if payload contains ipn_secret (some configs)
        if header_secret and NOWPAYMENTS_IPN_SECRET and header_secret != NOWPAYMENTS_IPN_SECRET:
            logger.warning("IPN secret header mismatch: %s", header_secret)
            return jsonify({"status": "error", "message": "Invalid IPN secret (header)"}), 403

        if not header_secret and NOWPAYMENTS_IPN_SECRET:
            # fallback: check body
            if body.get("ipn_secret") and body.get("ipn_secret") != NOWPAYMENTS_IPN_SECRET:
                logger.warning("IPN secret payload mismatch")
                return jsonify({"status": "error", "message": "Invalid IPN secret (body)"}), 403

        # Extract query params we set when creating invoice
        user_id_str = request.args.get("user_id")
        plan_key = request.args.get("plan")
        days = int(request.args.get("days") or 0)

        # Some NowPayments webhook payloads include 'payment_status' or 'status' field
        # Typical value for completed: "finished" or "successful" depending on provider
        status = body.get("payment_status") or body.get("status") or body.get("invoice_status")

        # Also price amount
        price_amount = float(body.get("price_amount") or body.get("paid_amount") or 0)

        logger.info("IPN verify: user_id=%s plan=%s days=%s status=%s amount=%s", user_id_str, plan_key, days, status, price_amount)

        # Only activate on completed status — NowPayments uses "finished" for successful invoices.
        if status in ("finished", "successful", "paid"):
            if user_id_str:
                try:
                    user_id = int(user_id_str)
                except:
                    user_id = None

                if user_id:
                    # Activate subscription
                    if plan_key in PLANS:
                        plan_label = PLANS[plan_key]["label"]
                        plan_days = PLANS[plan_key]["days"]
                    else:
                        # fallback to provided days, unknown label
                        plan_label = plan_key or "Unknown"
                        plan_days = days or 0

                    update_subscription(user_id, plan_label, plan_days)

                    # handle referral commission
                    user_rec = get_user(user_id)
                    if user_rec:
                        referred_by = user_rec[6]  # referred_by column (index 6)
                        # referred_by might be like "ref{user_id}" or numeric/store; attempt to pay commission
                        if referred_by:
                            # Determine commission amount as percentage of paid amount
                            commission_amount = price_amount * REFERRAL_RATE
                            # Add commission to the referrer (we try both referral_id and numeric id)
                            add_commission(referred_by, commission_amount)

                    logger.info("Subscription activated for user %s (plan %s)", user_id, plan_label)
                    return jsonify({"status": "ok"}), 200
            # If no user id, still return ok so provider doesn't retry excessively
            return jsonify({"status": "ignored", "reason": "no user_id"}), 200

        # Not final status; just ack
        return jsonify({"status": "ok", "message": "not final status"}), 200

    except Exception as e:
        logger.exception("Error in IPN processing")
        return jsonify({"status": "error", "message": str(e)}), 500

def run_flask():
    # Note: in production Render, Flask will be your web process. When using polling you run Flask in thread.
    flask_app.run(host="0.0.0.0", port=5000)

# ----------------- MAIN -----------------
def main():
    # Ensure DB schema
    init_db()

    # Start Flask IPN server in background thread (works for local dev & Render with web process)
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info("Flask server started on background thread (for IPN).")

    # Telegram bot
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Add handlers, exactly keeping your original flow
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(warroom, pattern="^warroom$"))
    app.add_handler(CallbackQueryHandler(subscribe_handler, pattern="^sub_"))
    app.add_handler(CallbackQueryHandler(about_handler, pattern="^about$"))
    app.add_handler(CallbackQueryHandler(earn_handler, pattern="^earn$"))
    app.add_handler(CallbackQueryHandler(help_handler, pattern="^help$"))

    logger.info("Starting Telegram bot (polling).")
    app.run_polling()

if __name__ == "__main__":
    main()
