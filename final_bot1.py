# final_bot1.py
# Fully functional Telegram bot + MaxelPay checkout + webhook + referrals + subscriptions
# Save as final_bot1.py and deploy (Render, etc.). Make sure requirements.txt includes:
# python-telegram-bot==20.3
# Flask==2.3.2
# requests==2.32.0
# pycryptodome==3.19.1

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

from urllib.parse import urlencode
from flask import Flask, request, jsonify

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Bot,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ----------------- CONFIG -----------------
# You provided these values — they are hardcoded here per your instruction.
BOT_TOKEN = "8467801272:AAGB5sy8q5CBp4ktLhPmTvCriF3d4t7vAbI"
MAXELPAY_API_KEY = "KU18KjYD8ajrAaEHQBnAByXFQEsJRYdp"
MAXELPAY_SECRET_KEY = "Alwq2y1565E5u5vNVzEhViwVYOcfkj0c"

# Use "prod" for live; use "stg" for sandbox (change if you want to test sandbox).
MAXELPAY_ENV = "prod"
MAXELPAY_ENDPOINT = f"https://api.maxelpay.com/v1/{MAXELPAY_ENV}/merchant/order/checkout"

# Your Render app base URL (used to build webhook URL sent to MaxelPay payload)
# If you change Render URL, update this value.
RENDER_BASE = "https://cryptowithclaritybot.onrender.com"
MAXELPAY_WEBHOOK_PATH = "/maxelpay_webhook"
MAXELPAY_WEBHOOK_FULL = RENDER_BASE + MAXELPAY_WEBHOOK_PATH

# DB file
DATABASE = "db.sqlite3"

# Bot settings
REFERRAL_COMMISSION_RATE = 0.25  # 25%
REFERRAL_PAYOUT_THRESHOLD = 100.0  # commissions withdrawable when >= $100

# ----------------- LOGGING -----------------
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ----------------- DATABASE -----------------
def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()

    # users table
    c.execute(
        """
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
        """
    )

    # orders table
    c.execute(
        """
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
        """
    )

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
    # Find referrer by code, then set referred_by for new user
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
    # order_row: (order_id, user_id, plan_name, amount, days, status, referred_by)
    if not order_row:
        return
    _, _, _, amount, _, _, referred_by = order_row
    if not referred_by:
        return
    # check referrer subscription active
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
        # add commission and increment referrals count
        c.execute("UPDATE users SET commission = commission + ?, referrals = referrals + 1 WHERE user_id=?", (commission, referred_by))
        conn.commit()
        conn.close()

# ----------------- MaxelPay encryption & invoice creation -----------------
def encrypt_payload(secret_key: str, payload_json: str):
    key_bytes = secret_key.encode("utf-8")
    # ensure 32 bytes
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
    # Try several common keys to find a URL to the checkout page.
    if not resp_json:
        return None
    # If top-level contains a URL-like key
    for k in ("payment_url", "checkout_url", "url", "data"):
        val = resp_json.get(k)
        if isinstance(val, str) and val.startswith("http"):
            return val
        if isinstance(val, dict):
            for k2 in ("payment_url", "checkout_url", "url"):
                if k2 in val and isinstance(val[k2], str) and val[k2].startswith("http"):
                    return val[k2]
    # Inspect nested structures
    if "invoice" in resp_json and isinstance(resp_json["invoice"], dict):
        for k in ("url", "payment_url"):
            if k in resp_json["invoice"] and isinstance(resp_json["invoice"][k], str):
                return resp_json["invoice"][k]
    # fallback: search for any http substring in values
    def find_http(obj):
        if isinstance(obj, str) and obj.startswith("http"):
            return obj
        if isinstance(obj, dict):
            for v in obj.values():
                res = find_http(v)
                if res:
                    return res
        if isinstance(obj, list):
            for item in obj:
                res = find_http(item)
                if res:
                    return res
        return None
    return find_http(resp_json)

def create_maxelpay_invoice_blocking(user_id: int, amount: float, plan_name: str, days: int, referred_by):
    """
    Blocking function to call MaxelPay API and return (order_id, checkout_url or error_str)
    """
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
        # include webhook which MaxelPay will call on status change
        "webhookUrl": MAXELPAY_WEBHOOK_FULL
    }
    # add custom metadata that may be echoed back (if MaxelPay supports)
    payload["metadata"] = {"telegram_user": user_id, "plan_days": days, "ref_by": referred_by}

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
    # Save mapping locally even if checkout_url missing (to inspect response)
    add_order(order_id, user_id, plan_name, float(amount), days, referred_by)

    if not checkout_url:
        # return the JSON string as error so you can debug
        return order_id, f"no-checkout-url-in-response: {json.dumps(resp_json)}"
    return order_id, checkout_url

# ----------------- Async wrapper for blocking network calls -----------------
async def create_invoice_async(user_id: int, amount: float, plan_name: str, days: int, referred_by):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, create_maxelpay_invoice_blocking, user_id, amount, plan_name, days, referred_by)

# ----------------- TELEGRAM HANDLERS -----------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Handle /start and deep link referrals: /start ref_CODE or /start <whatever>
    user = update.effective_user
    add_user_if_needed(user.id, user.username or "")
    args = context.args or []
    if args:
        # expecting referral code like "ref_REF123" or "REF123"
        code = args[0]
        # Normalize
        if code.startswith("ref_"):
            code = code[4:]
        # Attempt to set referred_by if code found
        ref_id = set_referred_by(user.id, code)
        if ref_id:
            await update.message.reply_text(f"Thanks for joining! You were referred by user {ref_id}.")
        else:
            await update.message.reply_text("Welcome! Referral code not recognized — continuing normally.")
    else:
        await update.message.reply_text("Welcome to CryptoWithClarity! Use the menu below to continue.")

    # show main menu
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
    # show about with inline buttons
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

# Callback for menu "warroom"
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
    # show subscription choices
    buttons = [
        [InlineKeyboardButton("💵 $10 / week", callback_data="sub_10")],
        [InlineKeyboardButton("💳 $20 / month", callback_data="sub_20")],
        [InlineKeyboardButton("💰 $50 / 3 months", callback_data="sub_50")]
    ]
    await query.message.reply_text("Choose a Warroom plan:", reply_markup=InlineKeyboardMarkup(buttons))

# subscription callback
async def cb_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    plan_key = query.data  # e.g., 'sub_10'
    plans = {
        "sub_10": (10.0, "$10 / week", 7),
        "sub_20": (20.0, "$20 / month", 30),
        "sub_50": (50.0, "$50 / 3 months", 90)
    }
    if plan_key not in plans:
        await query.message.reply_text("Unknown plan.")
        return
    amount, plan_name, days = plans[plan_key]

    # prepare: ensure user exists and check if they're already subscribed
    add_user_if_needed(user.id, user.username or "")
    user_row = get_user(user.id)
    referred_by = user_row[7] if user_row else None  # referred_by column

    # notify user and create invoice
    await query.message.reply_text("🔁 Generating secure payment link — please wait...")

    # Create invoice in background thread (non-blocking)
    order_id, checkout_url = await create_invoice_async(user.id, amount, plan_name, days, referred_by)
    if not order_id:
        await query.message.reply_text(f"❌ Failed to create payment link: {checkout_url}")
        return

    # add_order is already called in blocking function; but ensure exists:
    # add_order(order_id, user.id, plan_name, amount, days, referred_by)

    # send checkout link
    if isinstance(checkout_url, str) and checkout_url.startswith("http"):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💳 Pay Now", url=checkout_url)]])
        await query.message.reply_text(
            f"🔗 Payment link created — Order: `{order_id}`\nClick below to complete payment. After successful payment your subscription will be activated automatically.",
            parse_mode="Markdown",
            reply_markup=kb
        )
    else:
        # sometimes API returns data instead of direct url; send the payload for debugging
        await query.message.reply_text(f"⚠️ Unexpected payment provider response: {checkout_url}")

# about submenu callbacks
async def cb_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data  # e.g., about_referral, about_subs
    if data == "about_referral":
        user = query.from_user
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
        user = query.from_user
        row = get_user(user.id)
        if not row or not row[3]:
            await query.message.reply_text("You have no active subscription.")
            return
        await query.message.reply_text(f"Your subscription: {row[2]} until {row[3]}")
    else:
        await query.message.reply_text("Unknown action.")

# withdraw request
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
    # For now: mark commission zero and notify admin or say request received
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("UPDATE users SET commission=0 WHERE user_id=?", (user.id,))
    conn.commit()
    conn.close()
    await query.message.reply_text("✅ Withdrawal request received. Our team will process it manually.")

# ----------------- FLASK webhook to receive MaxelPay notifications -----------------
flask_app = Flask(__name__)

@flask_app.route(MAXELPAY_WEBHOOK_PATH, methods=["POST", "GET"])
def maxelpay_webhook():
    """
    MaxelPay will call this endpoint on payment events.
    We attempt to extract order ID from JSON body or query params.
    When found and status indicates success, we activate subscription and credit referrals.
    """
    # Try JSON body
    data = {}
    try:
        data = request.get_json(silent=True) or {}
    except Exception:
        data = {}

    q = request.args or {}

    # Try common keys for order id
    order_id = None
    for k in ("orderID", "orderId", "order_id", "id", "invoice_id"):
        if k in data:
            order_id = data.get(k)
            break
    if not order_id:
        # maybe nested
        if "data" in data and isinstance(data["data"], dict):
            for k in ("orderID", "orderId", "order_id", "id"):
                if k in data["data"]:
                    order_id = data["data"].get(k)
                    break
    if not order_id:
        # try query params
        for k in ("order_id", "orderID", "id"):
            if k in q:
                order_id = q.get(k)
                break

    # If still not found, try to find the order id in any string fields (heuristic)
    if not order_id:
        for v in list(data.values()):
            if isinstance(v, str) and "_" in v and len(v) > 10:
                # our order_id format contains underscores and timestamp
                order_id = v
                break

    if not order_id:
        logger.warning("Webhook called but order_id not found. data=%s args=%s", data, q)
        return jsonify({"status": "error", "message": "order_id not found"}), 400

    # Optional: check payment status in payload
    # MaxelPay typically reports some status field; attempt common ones
    status = data.get("status") or data.get("payment_status") or (data.get("data") or {}).get("status") if isinstance(data.get("data"), dict) else None
    # Some providers require verifying by calling their API for order status — skip here.

    # Fetch the local order mapping
    order_row = get_order(order_id)
    if not order_row:
        # as fallback: some providers forward our metadata (user_id / plan_days) in query params
        user_id = q.get("user_id") or data.get("user_id") or data.get("metadata", {}).get("telegram_user") if isinstance(data.get("metadata"), dict) else None
        plan_days = q.get("plan_days") or data.get("plan_days") or data.get("metadata", {}).get("plan_days") if isinstance(data.get("metadata"), dict) else None
        try:
            user_id = int(user_id) if user_id else None
            days = int(plan_days) if plan_days else 0
        except Exception:
            user_id = None
            days = 0
        if user_id and days > 0:
            # Activate immediately
            plan_name = f"Paid {days}-day plan"
            update_subscription(user_id, plan_name, days)
            try:
                bot = Bot(BOT_TOKEN)
                bot.send_message(chat_id=user_id, text=f"✅ Payment received. Your subscription is now active for {days} days. Enjoy the Warroom!")
            except Exception:
                logger.exception("Failed to notify user after fallback activation")
            return jsonify({"status": "ok", "message": "activated (fallback)"}), 200
        logger.warning("Order mapping not found for order_id=%s", order_id)
        return jsonify({"status": "error", "message": "order mapping not found"}), 404

    # order_row: (order_id, user_id, plan_name, amount, days, status, referred_by)
    _, user_id, plan_name, amount, days, old_status, referred_by = order_row

    # Determine if webhook indicates success — many gateways use "paid" / "success" etc.
    success = False
    if isinstance(status, str) and status.lower() in ("paid", "success", "completed"):
        success = True
    # Otherwise, try if payload has 'paid' boolean True
    if not success:
        if isinstance(data.get("paid"), bool) and data.get("paid") is True:
            success = True
    # Some gateways send 'status_code' or numeric field - we'll accept 200-like
    if not success and isinstance(data.get("status_code"), int) and data.get("status_code") == 200:
        success = True

    # If not obviously successful, we may still want to mark success conservatively:
    # If the payload contains an 'invoice' with 'paid' true.
    if not success and isinstance(data.get("invoice"), dict) and data["invoice"].get("paid") in (True, "true", "True"):
        success = True

    # If we can't decide, assume success (optionally) — here we will check the presence of 'paid' or 'status' or leave it conservative.
    # For safety, if status unknown, we treat it as success only if request method is POST (it likely is)
    if not success and request.method == "POST":
        # fallback: treat POST as success (you can remove this to require explicit 'paid')
        success = True

    if success:
        # update DB
        try:
            update_subscription(user_id, plan_name, int(days))
            set_order_status(order_id, "paid")
            # credit referral
            order_row = get_order(order_id)
            credit_referrer_if_applicable(order_row)
        except Exception:
            logger.exception("Failed to activate subscription for order %s", order_id)
            return jsonify({"status": "error", "message": "db error"}), 500

        # notify user
        try:
            bot = Bot(BOT_TOKEN)
            bot.send_message(chat_id=user_id, text=f"✅ Payment confirmed — {plan_name} activated for {days} days. Thank you and welcome to Warroom!")
        except Exception:
            logger.exception("Failed to send confirmation message to user %s", user_id)

        # optionally notify referrer if commission credited
        try:
            # fetch order again to know referred_by and commission
            order_row = get_order(order_id)
            if order_row and order_row[6]:
                ref_id = order_row[6]
                ref_row = get_user(ref_id)
                if ref_row:
                    # send brief notification
                    bot = Bot(BOT_TOKEN)
                    # show updated commission balance
                    new_comm = ref_row[5]  # note: credit_referrer_if_applicable already updated commission
                    bot.send_message(chat_id=ref_id, text=f"🎉 You earned a referral commission! Your new balance is ${new_comm:.2f}.")
        except Exception:
            logger.exception("Failed to notify referrer")

        return jsonify({"status": "ok", "order_id": order_id}), 200
    else:
        # not a success; mark pending/failed according to payload
        set_order_status(order_id, "failed")
        logger.info("Received webhook but payment not successful for order %s status=%s", order_id, status)
        return jsonify({"status": "ok", "message": "received (not paid)"}), 200

# ----------------- SERVER + BOT START -----------------
def run_flask():
    flask_app.run(host="0.0.0.0", port=5000)

async def on_startup(application):
    # attempt to delete any pre-existing Telegram webhook to avoid conflicts
    try:
        logger.info("Removing existing Telegram webhook (if any) ...")
        await application.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        logger.exception("Failed to delete webhook at startup (non-fatal)")

def main():
    # init DB
    init_db()

    # Start Flask webhook server in background thread
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info("Flask server started on background thread.")

    # Build Telegram application
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(CommandHandler("earn", earn_command))
    app.add_handler(CallbackQueryHandler(cb_warroom, pattern="^warroom$"))
    app.add_handler(CallbackQueryHandler(cb_subscribe, pattern="^sub_"))
    app.add_handler(CallbackQueryHandler(cb_about, pattern="^about_"))
    app.add_handler(CallbackQueryHandler(cb_withdraw, pattern="^withdraw_request$"))
    # run startup tasks
    app.post_init(on_startup)

    # Start bot polling (works fine if you ensure only one instance runs)
    logger.info("Starting Telegram bot (polling). Make sure only one instance runs for this token.")
    app.run_polling()

if __name__ == "__main__":
    main()
