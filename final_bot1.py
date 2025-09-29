# final_bot1_full.py
import os
import logging
import sqlite3
import json
import time
import base64
import secrets
import threading
import requests
import asyncio
from datetime import datetime, timedelta
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# ----------------- CONFIG -----------------
BOT_TOKEN = "8467801272:AAGB5sy8q5CBp4ktLhPmTvCriF3d4t7vAbI"
MAXELPAY_API_KEY = "KU18KjYD8ajrAaEHQBnAByXFQEsJRYdp"
MAXELPAY_SECRET_KEY = "Alwq2y1565E5u5vNVzEhViwVYOcfkj0c"
MAXELPAY_ENV = "prod"
MAXELPAY_ENDPOINT = f"https://api.maxelpay.com/v1/{MAXELPAY_ENV}/merchant/order/checkout"
RENDER_BASE = "https://cryptowithclaritybot.onrender.com"
MAXELPAY_WEBHOOK_PATH = "/maxelpay_webhook"
MAXELPAY_WEBHOOK_FULL = RENDER_BASE + MAXELPAY_WEBHOOK_PATH
DATABASE = "db.sqlite3"
REFERRAL_COMMISSION_RATE = 0.25
REFERRAL_PAYOUT_THRESHOLD = 100.0

# ----------------- LOGGING -----------------
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ----------------- DATABASE -----------------
def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            subscription_plan TEXT,
            subscription_end TEXT,
            referrals INTEGER DEFAULT 0,
            commission REAL DEFAULT 0,
            referral_code TEXT,
            referred_by INTEGER
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            user_id INTEGER,
            plan_name TEXT,
            amount REAL,
            days INTEGER,
            status TEXT,
            referred_by INTEGER,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def add_user_if_needed(user_id: int, username: str = ""):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO users (user_id, username, referral_code) VALUES (?,?,?)",
        (user_id, username, f"REF{user_id}")
    )
    conn.commit()
    conn.close()

def set_referred_by(new_user_id: int, ref_code: str):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE referral_code=?", (ref_code,))
    row = c.fetchone()
    if row:
        ref_id = row[0]
        c.execute("UPDATE users SET referred_by=? WHERE user_id=?", (ref_id, new_user_id))
        conn.commit()
        conn.close()
        return ref_id
    conn.close()
    return None

def get_user(user_id: int):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT user_id, username, subscription_plan, subscription_end, referrals, commission, referral_code, referred_by FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def update_subscription(user_id: int, plan_name: str, days: int):
    end_date = datetime.utcnow() + timedelta(days=days)
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("UPDATE users SET subscription_plan=?, subscription_end=? WHERE user_id=?", (plan_name, end_date.strftime("%Y-%m-%d %H:%M:%S"), user_id))
    conn.commit()
    conn.close()

def add_order(order_id: str, user_id: int, plan_name: str, amount: float, days: int, referred_by):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO orders (order_id, user_id, plan_name, amount, days, status, referred_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (order_id, user_id, plan_name, amount, days, "pending", referred_by, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

def get_order(order_id: str):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT order_id, user_id, plan_name, amount, days, status, referred_by FROM orders WHERE order_id=?", (order_id,))
    row = c.fetchone()
    conn.close()
    return row

def set_order_status(order_id: str, status: str):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("UPDATE orders SET status=? WHERE order_id=?", (status, order_id))
    conn.commit()
    conn.close()

def credit_referrer_if_applicable(order_row):
    if not order_row:
        return
    _, _, _, amount, _, _, referred_by = order_row
    if not referred_by:
        return
    ref = get_user(referred_by)
    if not ref:
        return
    _, _, ref_plan, ref_end, _, _, _, _ = ref
    active = False
    if ref_end:
        try:
            dt = datetime.strptime(ref_end, "%Y-%m-%d %H:%M:%S")
            if dt > datetime.utcnow():
                active = True
        except Exception:
            active = False
    if active:
        commission = round(float(amount) * REFERRAL_COMMISSION_RATE, 2)
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("UPDATE users SET commission = commission + ?, referrals = referrals + 1 WHERE user_id=?", (commission, referred_by))
        conn.commit()
        conn.close()

# ----------------- MaxelPay encryption -----------------
def encrypt_payload(secret_key: str, payload_json: str):
    key_bytes = secret_key.encode("utf-8")
    if len(key_bytes) < 32:
        key_bytes = key_bytes.ljust(32, b"\0")
    else:
        key_bytes = key_bytes[:32]
    iv = key_bytes[:16]
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
    padded = pad(payload_json.encode("utf-8"), AES.block_size)
    encrypted = cipher.encrypt(padded)
    return base64.b64encode(encrypted).decode("utf-8")

def extract_checkout_url_from_response(resp_json):
    if not resp_json:
        return None
    for k in ("payment_url", "checkout_url", "url", "data"):
        val = resp_json.get(k)
        if isinstance(val, str) and val.startswith("http"):
            return val
        if isinstance(val, dict):
            for k2 in ("payment_url", "checkout_url", "url"):
                if k2 in val and isinstance(val[k2], str) and val[k2].startswith("http"):
                    return val[k2]
    return None

def create_maxelpay_invoice_blocking(user_id: int, amount: float, plan_name: str, days: int, referred_by):
    order_id = f"{user_id}_{int(time.time())}_{secrets.token_hex(6)}"
    payload = {
        "orderID": order_id,
        "amount": str(amount),
        "currency": "USD",
        "timestamp": str(int(time.time())),
        "userName": str(user_id),
        "siteName": "CryptoWithClarity",
        "userEmail": f"{user_id}@example.com",
        "redirectUrl": f"{RENDER_BASE}/payment-success",
        "websiteUrl": "https://cryptowithclarity.in",
        "cancelUrl": f"{RENDER_BASE}/payment-cancel",
        "webhookUrl": MAXELPAY_WEBHOOK_FULL,
        "metadata": {"telegram_user": user_id, "plan_days": days, "ref_by": referred_by}
    }
    payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    encrypted = encrypt_payload(MAXELPAY_SECRET_KEY, payload_json)
    body = {"data": encrypted}
    headers = {"api-key": MAXELPAY_API_KEY, "Content-Type": "application/json"}
    try:
        resp = requests.post(MAXELPAY_ENDPOINT, headers=headers, json=body, timeout=20)
    except Exception as e:
        logger.exception("Error calling MaxelPay API")
        return None, f"request-error: {e}"
    try:
        resp_json = resp.json()
    except Exception:
        return None, f"invalid-response: {resp.status_code} {resp.text}"
    checkout_url = extract_checkout_url_from_response(resp_json)
    add_order(order_id, user_id, plan_name, float(amount), days, referred_by)
    if not checkout_url:
        return order_id, f"no-checkout-url-in-response: {json.dumps(resp_json)}"
    return order_id, checkout_url

async def create_invoice_async(user_id: int, amount: float, plan_name: str, days: int, referred_by):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, create_maxelpay_invoice_blocking, user_id, amount, plan_name, days, referred_by)

# ----------------- TELEGRAM HANDLERS -----------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user_if_needed(user.id, user.username or "")
    args = context.args or []
    if args:
        code = args[0]
        if code.startswith("ref_"):
            code = code[4:]
        ref_id = set_referred_by(user.id, code)
        if ref_id:
            await update.message.reply_text(f"Thanks for joining! You were referred by user {ref_id}.")
        else:
            await update.message.reply_text("Welcome! Referral code not recognized — continuing normally.")
    else:
        await update.message.reply_text("Welcome to CryptoWithClarity! Use the menu below to continue.")
    buttons = [
        [InlineKeyboardButton("🌐 Public Community", url="https://t.me/+jUlj8kNrBRg2NGY9")],
        [InlineKeyboardButton("🛡️ Warroom", callback_data="warroom")],
        [InlineKeyboardButton("🎁 Airdrop Community", url="https://t.me/+qmz3WHjuvjcxYjM1")],
        [InlineKeyboardButton("🆘 Contact Support", url="https://t.me/CryptoWith_Sarvesh")],
        [InlineKeyboardButton("💹 Start Trading", url="https://axiom.trade/@sarvesh")],
        [InlineKeyboardButton("💰 Earn / Referral", callback_data="earn")],
        [InlineKeyboardButton("❓ Help", callback_data="help")]
    ]
    await update.message.reply_text("Choose an option:", reply_markup=InlineKeyboardMarkup(buttons))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "❓ *Help*\n"
        "/start - show menu\n"
        "/earn - show your referral & commission\n"
        "/about - info & quick links\n"
        "Use the Warroom button to subscribe and get access to perks."
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = [
        [InlineKeyboardButton("📊 REFERRAL TRACKER", callback_data="about_referral")],
        [InlineKeyboardButton("📦 MY SUBSCRIPTIONS", callback_data="about_subs")],
        [InlineKeyboardButton("🌐 SOCIAL MEDIA", url="https://linktr.ee/CryptoWithClarity")]
    ]
    await update.message.reply_text("About — quick actions:", reply_markup=InlineKeyboardMarkup(buttons))

async def earn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = get_user(user.id)
    if not row or not row[2]:
        await update.message.reply_text("You must subscribe to be eligible for referring others and earning commissions.")
        return
    _, _, plan, end, referrals, commission, referral_code, _ = row
    bot_username = (await context.bot.get_me()).username
    referral_link = f"https://t.me/{bot_username}?start={referral_code}"
    text = (
        f"💰 *Your Referral Dashboard*\n\n"
        f"Referral code: `{referral_code}`\n"
        f"Referral link: {referral_link}\n\n"
        f"Referrals: {referrals}\n"
        f"Commission balance: ${commission:.2f}\n"
    )
    buttons = []
    if commission >= REFERRAL_PAYOUT_THRESHOLD:
        buttons.append([InlineKeyboardButton("💸 Request Withdrawal", callback_data="withdraw_request")])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)

# ----------------- CALLBACK HANDLERS -----------------
async def cb_warroom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    row = get_user(user.id)
    subscribed = False
    if row and row[3]:
        try:
            end_dt = datetime.strptime(row[3], "%Y-%m-%d %H:%M:%S")
            if end_dt > datetime.utcnow():
                subscribed = True
        except Exception:
            subscribed = False
    if subscribed:
        text = "✅ You already have access to Warroom.\nPerks:\n- 🤖 AI Prompts\n- 🛠 Bot Tools for Trades\n- Exclusive community"
        await query.message.reply_text(text)
        return
    buttons = [
        [InlineKeyboardButton("💵 $10 / week", callback_data="sub_10")],
        [InlineKeyboardButton("💳 $20 / month", callback_data="sub_20")],
        [InlineKeyboardButton("💰 $50 / 3 months", callback_data="sub_50")]
    ]
    await query.message.reply_text("Choose a Warroom plan:", reply_markup=InlineKeyboardMarkup(buttons))

async def cb_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    plan_key = query.data
    plans = {"sub_10": (10.0, "$10 / week", 7),"sub_20": (20.0, "$20 / month", 30),"sub_50": (50.0, "$50 / 3 months", 90)}
    if plan_key not in plans:
        await query.message.reply_text("Unknown plan.")
        return
    amount, plan_name, days = plans[plan_key]
    add_user_if_needed(user.id, user.username or "")
    user_row = get_user(user.id)
    referred_by = user_row[7] if user_row else None
    await query.message.reply_text("🔁 Generating secure payment link — please wait...")
    order_id, checkout_url = await create_invoice_async(user.id, amount, plan_name, days, referred_by)
    if not order_id:
        await query.message.reply_text(f"❌ Failed to create payment link: {checkout_url}")
        return
    if isinstance(checkout_url, str) and checkout_url.startswith("http"):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💳 Pay Now", url=checkout_url)]])
        await query.message.reply_text(f"🔗 Payment link created — Order: `{order_id}`\nClick below to complete payment. After successful payment your subscription will be activated automatically.",parse_mode="Markdown", reply_markup=kb)
    else:
        await query.message.reply_text(f"⚠️ Unexpected payment provider response: {checkout_url}")

async def cb_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user
    if data == "about_referral":
        row = get_user(user.id)
        if not row:
            await query.message.reply_text("You have no referrals yet.")
            return
        _, _, _, _, referrals, commission, referral_code, _ = row
        bot_username = (await context.bot.get_me()).username
        referral_link = f"https://t.me/{bot_username}?start={referral_code}"
        text = f"Referral code: `{referral_code}`\nLink: {referral_link}\nReferrals: {referrals}\nCommission: ${commission:.2f}"
        await query.message.reply_text(text, parse_mode="Markdown")
    elif data == "about_subs":
        row = get_user(user.id)
        if not row or not row[3]:
            await query.message.reply_text("You have no active subscription.")
            return
        await query.message.reply_text(f"Your subscription: {row[2]} until {row[3]}")
    else:
        await query.message.reply_text("Unknown action.")

async def cb_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    row = get_user(user.id)
    if not row:
        await query.message.reply_text("No account found.")
        return
    commission = row[5]
    if commission < REFERRAL_PAYOUT_THRESHOLD:
        await query.message.reply_text(f"Minimum withdrawal is ${REFERRAL_PAYOUT_THRESHOLD:.2f}. Your balance: ${commission:.2f}")
        return
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("UPDATE users SET commission=0 WHERE user_id=?", (user.id,))
    conn.commit()
    conn.close()
    await query.message.reply_text("✅ Withdrawal request received. Our team will process it manually.")

# ----------------- FLASK WEBHOOK -----------------
flask_app = Flask(__name__)

@flask_app.route(MAXELPAY_WEBHOOK_PATH, methods=["POST", "GET"])
def maxelpay_webhook():
    return jsonify({"status": "ok"})

def run_flask():
    flask_app.run(host="0.0.0.0", port=5000)

# ----------------- START -----------------
async def on_startup(application):
    try:
        logger.info("Removing existing Telegram webhook (if any) ...")
        await application.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        logger.exception("Failed to delete webhook at startup (non-fatal)")

def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info("Flask server started on background thread.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    # --- HANDLERS ---
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(CommandHandler("earn", earn_command))
    app.add_handler(CallbackQueryHandler(cb_warroom, pattern="^warroom$"))
    app.add_handler(CallbackQueryHandler(cb_subscribe, pattern="^sub_"))
    app.add_handler(CallbackQueryHandler(cb_about, pattern="^about_"))
    app.add_handler(CallbackQueryHandler(cb_withdraw, pattern="^withdraw_request$"))

    asyncio.run(on_startup(app))
    logger.info("Starting Telegram bot (polling).")
    app.run_polling()

if __name__ == "__main__":
    main()
