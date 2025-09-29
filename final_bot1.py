# final_bot1.py
import logging
import sqlite3
import time
import json
import os
import threading
import requests
from datetime import datetime, timedelta
from urllib.parse import urlencode
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import base64
import secrets

from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ----------------- CONFIG -----------------
BOT_TOKEN = "8467801272:AAGB5sy8q5CBp4ktLhPmTvCriF3d4t7vAbI"
DATABASE = "db.sqlite3"

# MaxelPay keys you provided
MAXELPAY_API_KEY = "KU18KjYD8ajrAaEHQBnAByXFQEsJRYdp"
MAXELPAY_SECRET_KEY = "Alwq2y1565E5u5vNVzEhViwVYOcfkj0c"

# endpoint - use "stg" for sandbox or "prod" for production
MAXELPAY_ENV = "prod"  # change to "stg" to test in sandbox
ENDPOINT = f"https://api.maxelpay.com/v1/{MAXELPAY_ENV}/merchant/order/checkout"

# webhook URL that MaxelPay will call (set this in MaxelPay dashboard)
# On Render this will be: https://<your-render-app>.onrender.com/maxelpay_webhook
# Keep only the path here for forming payload (we'll not hardcode full URL in every place)
FLASK_WEBHOOK_PATH = "/maxelpay_webhook"

# For redirect/cancel pages (optional)
REDIRECT_URL = "https://cryptowithclarity.in/payment-success"
CANCEL_URL = "https://cryptowithclarity.in/payment-cancel"

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
            commission REAL DEFAULT 0
        )
        """
    )
    # orders table to map orderID -> user_id and plan
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            user_id INTEGER,
            plan_name TEXT,
            amount REAL,
            days INTEGER,
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()

def save_order(order_id, user_id, plan_name, amount, days):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO orders (order_id, user_id, plan_name, amount, days, created_at) VALUES (?,?,?,?,?,?)",
        (order_id, user_id, plan_name, amount, days, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

def get_order(order_id):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT order_id, user_id, plan_name, amount, days FROM orders WHERE order_id=?", (order_id,))
    row = c.fetchone()
    conn.close()
    return row

def add_user_if_not_exists(user_id, username):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?,?)", (user_id, username))
    conn.commit()
    conn.close()

def update_subscription(user_id, plan_name, days):
    end_date = datetime.now() + timedelta(days=days)
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute(
        "UPDATE users SET subscription_plan=?, subscription_end=? WHERE user_id=?",
        (plan_name, end_date.strftime("%Y-%m-%d %H:%M:%S"), user_id),
    )
    conn.commit()
    conn.close()

# ----------------- MaxelPay encryption & API -----------------
def encrypt_payload_aes_cbc(secret_key: str, payload_json: str) -> str:
    """
    AES-256-CBC with PKCS7 padding, then base64 encode — matches MaxelPay example.
    secret_key: the secret string (use bytes UTF-8). IV = first 16 bytes of secret_key.
    """
    key_bytes = secret_key.encode("utf-8")
    # Ensure key length is 32 bytes for AES-256. If shorter, pad with zeros (or derive properly).
    # MaxelPay docs assumed secret_key length >= 32; here we trim or pad to 32 bytes.
    if len(key_bytes) < 32:
        key_bytes = key_bytes.ljust(32, b"\0")
    else:
        key_bytes = key_bytes[:32]
    iv = key_bytes[:16]
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
    padded = pad(payload_json.encode("utf-8"), AES.block_size)
    encrypted = cipher.encrypt(padded)
    return base64.b64encode(encrypted).decode("utf-8")

def create_maxelpay_invoice(user_id: int, amount: float, plan_name: str, days: int):
    """
    Create a MaxelPay checkout by encrypting payload and POSTing to the API.
    Returns (order_id, checkout_url) on success, or (None, error_message) on failure.
    """
    # Unique order id
    ts = int(time.time())
    order_id = f"{user_id}_{ts}_{secrets.token_hex(6)}"

    payload = {
        "orderID": order_id,
        "amount": str(amount),
        "currency": "USD",
        "timestamp": str(int(time.time())),
        "userName": str(user_id),
        "siteName": "CryptoWithClarity",
        "userEmail": f"{user_id}@example.com",
        "redirectUrl": REDIRECT_URL,
        "websiteUrl": "https://cryptowithclarity.in",
        "cancelUrl": CANCEL_URL,
        # Pass webhook URL where MaxelPay will notify - use full path
        "webhookUrl": f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME', '')}{FLASK_WEBHOOK_PATH}" if os.getenv('RENDER_EXTERNAL_HOSTNAME') else None
    }

    # If the RENDER_EXTERNAL_HOSTNAME env var not set (e.g., local run), let MaxelPay call full URL later.
    # But we will always include webhook as environment variable when deploying (Render has a public hostname).
    # To be safe, also include webhookUrl as placeholder if not set (many providers allow setting webhook in dashboard)
    if not payload["webhookUrl"]:
        payload["webhookUrl"] = "https://cryptowithclaritybot.onrender.com" + FLASK_WEBHOOK_PATH

    payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    encrypted_data = encrypt_payload_aes_cbc(MAXELPAY_SECRET_KEY, payload_json)

    body = {"data": encrypted_data}
    headers = {"api-key": MAXELPAY_API_KEY, "Content-Type": "application/json"}

    try:
        resp = requests.post(ENDPOINT, headers=headers, json=body, timeout=15)
    except Exception as e:
        logger.exception("Error calling MaxelPay API")
        return None, f"request-error: {str(e)}"

    if resp.status_code not in (200, 201):
        logger.error("MaxelPay API returned error: %s %s", resp.status_code, resp.text)
        return None, f"api-error: {resp.status_code} {resp.text}"

    try:
        data = resp.json()
    except Exception:
        logger.exception("Invalid JSON from MaxelPay")
        return None, "invalid-json-response"

    # The exact response structure can vary. Try common keys:
    checkout_url = None
    # Inspect 'checkout_url', 'payment_url', 'url', 'data' etc.
    for key in ("checkout_url", "payment_url", "url", "checkoutUrl", "data"):
        if key in data:
            v = data.get(key)
            # if data key contains nested structure
            if isinstance(v, dict):
                # try to find url inside
                for k2 in ("url", "checkout_url", "payment_url"):
                    if k2 in v:
                        checkout_url = v[k2]
                        break
            else:
                checkout_url = v
            if checkout_url:
                break

    # Some APIs return the url inside data['data'] as JSON or inside 'data' -> 'url'
    if not checkout_url and isinstance(data.get("data"), dict):
        for k in ("url", "payment_url", "checkout_url"):
            if k in data["data"]:
                checkout_url = data["data"][k]
                break

    # Fallback: if response contains 'invoice' with 'url'
    if not checkout_url and "invoice" in data and isinstance(data["invoice"], dict):
        for k in ("url", "payment_url"):
            if k in data["invoice"]:
                checkout_url = data["invoice"][k]
                break

    if not checkout_url:
        # If still not found, maybe MaxelPay includes direct link in top-level 'message' or similar.
        # Return whole JSON string so operator can inspect.
        logger.info("MaxelPay response (unrecognized) : %s", json.dumps(data))
        return order_id, json.dumps(data)

    # Save order mapping to DB
    save_order(order_id, user_id, plan_name, amount, days)
    return order_id, checkout_url

# ----------------- TELEGRAM BOT HANDLERS -----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user_if_not_exists(user.id, user.username or "")
    buttons = [
        [InlineKeyboardButton("🌐 Public Community", url="https://t.me/+jUlj8kNrBRg2NGY9")],
        [InlineKeyboardButton("🛡️ Warroom", callback_data="warroom")],
        [InlineKeyboardButton("🎁 Airdrop Community", url="https://t.me/+qmz3WHjuvjcxYjM1")],
        [InlineKeyboardButton("📩 Contact Support", url="https://t.me/CryptoWith_Sarvesh")],
        [InlineKeyboardButton("📈 Start Trading", url="https://axiom.trade/@sarvesh")],
    ]
    keyboard = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("👋 Welcome to CryptoWithClarity — choose an option:", reply_markup=keyboard)

async def cb_warroom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    urow = None
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT subscription_plan, subscription_end FROM users WHERE user_id=?", (user.id,))
    urow = c.fetchone()
    conn.close()
    if not urow or not urow[0]:
        # not subscribed
        buttons = [
            [InlineKeyboardButton("💵 $10 / week", callback_data="sub_10")],
            [InlineKeyboardButton("💳 $20 / month", callback_data="sub_20")],
            [InlineKeyboardButton("💰 $50 / 3 months", callback_data="sub_50")],
        ]
        await query.message.reply_text(
            "🔒 Warroom access requires subscription. Choose a plan:", reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        plan, end = urow
        await query.message.reply_text(f"✅ You are subscribed ({plan}) until {end}.\n\nWarroom Perks:\n- 🤖 AI prompts\n- 🛠 trading tools\n- Exclusive access")

async def cb_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    plan_key = query.data  # "sub_10" etc

    plans = {
        "sub_10": (10.0, "$10 / week", 7),
        "sub_20": (20.0, "$20 / month", 30),
        "sub_50": (50.0, "$50 / 3 months", 90),
    }
    if plan_key not in plans:
        await query.message.reply_text("Plan not recognized.")
        return

    amount, plan_name, days = plans[plan_key]
    # Create invoice via MaxelPay
    await query.message.reply_text("🔁 Generating secure payment link... please wait.")
    order_id, checkout = create_maxelpay_invoice(user.id, amount, plan_name, days)
    if not order_id:
        await query.message.reply_text(f"❌ Failed to create payment link: {checkout}")
        return

    # If checkout is JSON string because API returned unexpected structure, show it
    if checkout.startswith("{") and "http" not in checkout:
        await query.message.reply_text("⚠️ Received unexpected response from payment provider. Contact support.")
        logger.info("Invoice response: %s", checkout)
        return

    # Send the checkout link to the user (inline button)
    btn = InlineKeyboardMarkup([[InlineKeyboardButton("💳 Pay Now", url=checkout)]])
    # also include the order_id so user can reference it
    await query.message.reply_text(
        f"🔗 Payment link created — Order: `{order_id}`\nClick below to pay. After successful payment your subscription will be activated automatically.",
        parse_mode="Markdown",
        reply_markup=btn,
    )

# ----------------- FLASK app for receiving MaxelPay webhooks -----------------
flask_app = Flask(__name__)

@flask_app.route(FLASK_WEBHOOK_PATH, methods=["POST", "GET"])
def maxelpay_webhook():
    """
    This endpoint handles MaxelPay webhook callbacks.
    MaxelPay may POST JSON; sometimes it will call with query params.
    We try to find a recognizable order_id in the incoming payload or query params.
    """
    # Try JSON body first
    data = {}
    try:
        data = request.get_json(silent=True) or {}
    except Exception:
        data = {}
    # Query params
    q = request.args or {}

    # Attempt to find order_id in JSON or query params
    order_id = None
    candidate_keys = ["orderID", "orderId", "order_id", "orderIDReceived", "id", "invoice_id"]
    for k in candidate_keys:
        if k in data:
            order_id = data.get(k)
            break
    if not order_id:
        # check nested 'data' if present and JSON-parsed
        if "data" in data and isinstance(data["data"], dict):
            for k in candidate_keys:
                if k in data["data"]:
                    order_id = data["data"].get(k)
                    break
    if not order_id:
        # check query params
        for k in ("order_id", "orderID", "id"):
            if k in q:
                order_id = q.get(k)
                break

    # As fallback: look for uuid-like value in JSON values (if created as such)
    if not order_id and isinstance(data, dict):
        for v in data.values():
            if isinstance(v, str) and "_" in v and len(v) > 10:
                # simple heuristic: our order_id contains user_ts_random -> contains underscores
                order_id = v
                break

    if not order_id:
        logger.warning("Webhook received without order_id. raw data: %s args: %s", data, q)
        return jsonify({"status": "error", "message": "order_id not found"}), 400

    # find order entry in DB
    row = get_order(order_id)
    if not row:
        # maybe webhook included user_id & plan directly
        user_id = data.get("user_id") or q.get("user_id")
        days = data.get("plan_days") or q.get("plan_days") or data.get("days") or q.get("days")
        try:
            if user_id:
                user_id = int(user_id)
            if days:
                days = int(days)
        except Exception:
            user_id = None
            days = 0

        if user_id and days:
            # activate
            update_subscription(user_id, f"Paid {days}-day plan", days)
            # notify user
            try:
                bot = Bot(BOT_TOKEN)
                bot.send_message(user_id, f"✅ Payment received. Your subscription is active for {days} days. Enjoy the Warroom!")
            except Exception:
                logger.exception("Failed to notify user after direct-params activation")
            return jsonify({"status": "ok", "message": "activated (direct params)"})

        logger.warning("Order not found for order_id=%s", order_id)
        return jsonify({"status": "error", "message": "order not found"}), 404

    # We have order mapping
    _, user_id, plan_name, amount, days = row
    try:
        # Activate subscription
        update_subscription(user_id, plan_name, int(days))
    except Exception as e:
        logger.exception("Failed to update subscription for user %s", user_id)
        return jsonify({"status": "error", "message": "db error"}), 500

    # Notify user via Telegram bot
    try:
        bot = Bot(BOT_TOKEN)
        bot.send_message(
            chat_id=user_id,
            text=f"✅ Payment confirmed — {plan_name} activated for {days} days. Thank you! 🎉",
        )
    except Exception:
        logger.exception("Failed to send Telegram confirmation to user %s", user_id)

    return jsonify({"status": "ok", "order_id": order_id})

def run_flask():
    # Use port 5000 - Render will map externally
    flask_app.run(host="0.0.0.0", port=5000)

# ----------------- MAIN / START -----------------
def main():
    init_db()

    # Start Flask in background thread
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info("Flask webhook server started in background thread.")

    # Build Telegram app
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_warroom, pattern="^warroom$"))
    app.add_handler(CallbackQueryHandler(cb_subscribe, pattern="^sub_"))

    # Delete any existing webhook (prevents conflict between webhook/polling)
    try:
        logger.info("Deleting any existing Telegram webhook before starting polling.")
        app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        logger.exception("Failed to delete webhook (this is okay if running in webhook mode).")

    # Start polling (safe if only one instance runs). If deploying to Render it's better to use webhook mode,
    # but polling works on Render too (just make sure only one instance is running).
    logger.info("Starting bot (long-polling). Make sure no other instance is running for this token.")
    app.run_polling()

if __name__ == "__main__":
    main()
