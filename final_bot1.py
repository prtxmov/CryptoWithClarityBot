#!/usr/bin/env python3
# final_bot_admin.py
"""
Ready-to-run single-file Telegram bot + NowPayments IPN + Admin backend (Flask)
HARD-CODED KEYS (as requested) — do NOT publish this file.
"""

import logging
import sqlite3
import threading
import requests
import json
import time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, redirect, session, escape
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# ----------------- CONFIG (HARD-CODED) -----------------
BOT_TOKEN = "8467801272:AAGB5sy8q5CBp4ktLhPmTvCriF3d4t7vAbI"
DATABASE = "db.sqlite3"

# NowPayments credentials
NOWPAYMENTS_API_KEY = "FG43MR3-RHPM8ZK-GCQ5VYD-SNCHJ3C"
NOWPAYMENTS_IPN_SECRET = "hCtlqRpYс7rTkK5e9eZQDbv6MimGSZkC"

# PUBLIC_URL: replace with your real server URL (example below)
# e.g. PUBLIC_URL = "https://your-app.onrender.com"
PUBLIC_URL = "https://your-deploy-domain.onrender.com"

# Admin credentials
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "prtxsarveshadmin811994"
ADMIN_SESSION_KEY = "admin_logged_in"

# Warroom link (you gave)
WARROOM_LINK = "https://t.me/+IM_nIsf78JI4NzI1"

PLANS = {
    "sub_10": {"label": "Weekly", "amount": 10, "days": 7},
    "sub_20": {"label": "Monthly", "amount": 20, "days": 30},
    "sub_50": {"label": "3-Months", "amount": 50, "days": 90},
}
REFERRAL_RATE = 0.25
NOWPAYMENTS_INVOICE_URL = "https://api.nowpayments.io/v1/invoice"

# Flask
flask_app = Flask(__name__)
flask_app.secret_key = "replace-with-a-random-secret-if-you-like"

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("final_bot_admin")

# Global bot var
TELEGRAM_BOT = None

# ----------------- DB Utilities -----------------
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
            referred_by TEXT,
            referral_id TEXT,
            last_notified TEXT
        )
    ''')
    conn.commit()
    try:
        c.execute("ALTER TABLE users ADD COLUMN referral_id TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_notified TEXT")
    except sqlite3.OperationalError:
        pass
    c.execute('''
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT,
            user_id INTEGER,
            plan_key TEXT,
            invoice_url TEXT,
            status TEXT,
            amount REAL,
            created_at TEXT
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("DB initialized")

def query_db(q, args=(), one=False):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute(q, args)
    rows = c.fetchall()
    conn.commit()
    conn.close()
    return (rows[0] if rows else None) if one else rows

def add_user(user_id, username, referred_by=None):
    referral_id = f"ref{user_id}"
    query_db("INSERT OR IGNORE INTO users (user_id, username, referred_by, referral_id) VALUES (?,?,?,?)",
             (user_id, username, referred_by, referral_id))
    query_db("UPDATE users SET referral_id=? WHERE user_id=? AND (referral_id IS NULL OR referral_id='')",
             (referral_id, user_id))
    return referral_id

def get_user(user_id):
    return query_db("SELECT * FROM users WHERE user_id=?", (user_id,), one=True)

def update_subscription(user_id, plan_label, days):
    end_date = datetime.utcnow() + timedelta(days=days)
    query_db("UPDATE users SET subscription_plan=?, subscription_end=? WHERE user_id=?",
             (plan_label, end_date.strftime("%Y-%m-%d %H:%M:%S"), user_id))
    logger.info("Subscription updated for %s -> %s (days=%s)", user_id, plan_label, days)

def save_invoice(order_id, user_id, plan_key, invoice_url, amount, status="pending"):
    query_db("INSERT INTO invoices (order_id, user_id, plan_key, invoice_url, status, amount, created_at) VALUES (?,?,?,?,?,?,?)",
             (order_id, user_id, plan_key, invoice_url or "", status, amount, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))

def find_invoice(order_id):
    return query_db("SELECT * FROM invoices WHERE order_id=?", (order_id,), one=True)

def update_invoice_status(order_id, status):
    query_db("UPDATE invoices SET status=? WHERE order_id=?", (status, order_id))

def get_last_notified(user_id):
    row = query_db("SELECT last_notified FROM users WHERE user_id=?", (user_id,), one=True)
    if row and row[0]:
        try:
            return datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
        except:
            return None
    return None

def update_last_notified(user_id, dt):
    query_db("UPDATE users SET last_notified=? WHERE user_id=?", (dt.strftime("%Y-%m-%d %H:%M:%S"), user_id))

# ----------------- NowPayments functions -----------------
def generate_payment_link(user_id, plan_key):
    if plan_key not in PLANS:
        return None, "Invalid plan"
    plan = PLANS[plan_key]
    amount = plan["amount"]
    days = plan["days"]
    order_id = f"{user_id}_{int(time.time())}"
    ipn_cb = f"{PUBLIC_URL.rstrip('/')}/ipn?user_id={user_id}&plan={plan_key}&days={days}"
    success_cb = f"{PUBLIC_URL.rstrip('/')}/success?order_id={order_id}&user_id={user_id}&plan={plan_key}"
    cancel_cb = f"{PUBLIC_URL.rstrip('/')}/cancel?order_id={order_id}&user_id={user_id}"
    payload = {
        "price_amount": amount,
        "price_currency": "usd",
        "order_id": order_id,
        "order_description": f"{plan['label']} subscription for user {user_id}",
        "ipn_callback_url": ipn_cb,
        "success_url": success_cb,
        "cancel_url": cancel_cb
    }
    headers = {"x-api-key": NOWPAYMENTS_API_KEY, "Content-Type": "application/json"}
    try:
        r = requests.post(NOWPAYMENTS_INVOICE_URL, headers=headers, json=payload, timeout=20)
        data = r.json()
        logger.info("NowPayments response: %s", json.dumps(data))
        invoice_url = data.get("invoice_url") or data.get("payment_url") or data.get("url")
        save_invoice(order_id, user_id, plan_key, invoice_url, amount, status=data.get("status", "pending"))
        if invoice_url:
            return invoice_url, None
        return None, data.get("error", "Unknown error")
    except Exception as e:
        logger.exception("Failed to create invoice")
        return None, str(e)

# ----------------- Notifications -----------------
def notify_user_subscription_activated(user_id, plan_label):
    global TELEGRAM_BOT
    try:
        logger.info("notify_user called: bot=%s, user=%s", bool(TELEGRAM_BOT), user_id)
        if TELEGRAM_BOT:
            TELEGRAM_BOT.send_message(chat_id=user_id,
                text=(f"🎉 Congratulations — your *{plan_label}* membership is active!\n\n"
                      f"Join the Warroom: {WARROOM_LINK}"),
                parse_mode="Markdown")
        else:
            logger.warning("TELEGRAM_BOT not initialized; cannot message user %s", user_id)
    except Exception:
        logger.exception("Failed to send activation message to %s", user_id)

def notify_user_provisional(user_id):
    global TELEGRAM_BOT
    try:
        if TELEGRAM_BOT:
            TELEGRAM_BOT.send_message(chat_id=user_id,
                text=(f"🔔 We redirected you to the Warroom: {WARROOM_LINK}\n"
                      "We're verifying your payment — your subscription will be activated automatically once payment is confirmed."),
                parse_mode="Markdown")
    except Exception:
        logger.exception("Failed to send provisional message to %s", user_id)

# ----------------- Telegram handlers -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.username or "")
    buttons = [[InlineKeyboardButton("🔥 Warroom", callback_data="warroom")]]
    await update.message.reply_text("Welcome! Use the buttons.", reply_markup=InlineKeyboardMarkup(buttons))

async def warroom_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = get_user(user_id)
    if not user or not user[2]:
        buttons = [
            [InlineKeyboardButton("💵 $10 / week", callback_data="sub_10")],
            [InlineKeyboardButton("💳 $20 / month", callback_data="sub_20")],
            [InlineKeyboardButton("💎 $50 / 3 months", callback_data="sub_50")]
        ]
        await query.message.reply_text("Warroom is for subscribed users. Choose a plan:", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await query.message.reply_text(f"You're subscribed. Join the Warroom: {WARROOM_LINK}")

async def subscribe_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    plan_key = query.data
    user_id = query.from_user.id
    pay_url, err = generate_payment_link(user_id, plan_key)
    if pay_url:
        await query.message.reply_text(f"Complete payment here:\n{pay_url}")
    else:
        await query.message.reply_text(f"Error creating payment link: {err}")

async def any_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.username or "")
    row = get_user(user.id)
    subscribed = False
    if row and row[2]:
        try:
            end_dt = datetime.strptime(row[3], "%Y-%m-%d %H:%M:%S")
            subscribed = end_dt > datetime.utcnow()
        except:
            subscribed = True
    if not subscribed:
        last = get_last_notified(user.id) if 'get_last_notified' in globals() else None
        now = datetime.utcnow()
        if (not last) or (now - last > timedelta(hours=24)):
            try:
                await update.message.reply_text(f"⚠️ Join the Warroom: {WARROOM_LINK}")
                if 'update_last_notified' in globals():
                    update_last_notified(user.id, now)
            except Exception:
                logger.exception("Failed to nudge user")

# ----------------- Flask endpoints: IPN + success -----------------
@flask_app.route("/nowpayments-debug", methods=["GET","POST"])
def nowpayments_debug():
    logger.info("DEBUG PAYLOAD: headers=%s args=%s body=%s", dict(request.headers), dict(request.args), request.get_data(as_text=True))
    return jsonify({"status":"ok"}), 200

@flask_app.route("/ipn", methods=["POST"])
def ipn_listener():
    logger.info("IPN hit: headers=%s", dict(request.headers))
    try:
        header_secret = request.headers.get("x-nowpayments-ipn-secret") or request.headers.get("x-nowpayments-signature")
        body = request.get_json(force=True, silent=True) or {}
        logger.info("IPN body: %s", json.dumps(body))

        if NOWPAYMENTS_IPN_SECRET:
            if header_secret and header_secret != NOWPAYMENTS_IPN_SECRET:
                logger.warning("IPN header secret mismatch")
                return jsonify({"status":"error","message":"invalid secret"}), 403
            if not header_secret and body.get("ipn_secret") and body.get("ipn_secret") != NOWPAYMENTS_IPN_SECRET:
                logger.warning("IPN payload secret mismatch")
                return jsonify({"status":"error","message":"invalid payload secret"}), 403

        user_id_str = request.args.get("user_id")
        plan_key = request.args.get("plan")
        days = int(request.args.get("days") or 0)
        status = body.get("payment_status") or body.get("status") or body.get("invoice_status")
        price_amount = float(body.get("price_amount") or body.get("paid_amount") or 0)
        order_id = body.get("order_id") or body.get("id")

        if not user_id_str and order_id:
            inv = find_invoice(order_id)
            if inv and inv[2]:
                user_id_str = str(inv[2])
                logger.info("Resolved user_id from invoice: %s -> %s", order_id, user_id_str)

        logger.info("IPN verify: user=%s plan=%s status=%s order=%s amount=%s", user_id_str, plan_key, status, order_id, price_amount)

        if str(status).lower() in ("finished","paid","successful"):
            if user_id_str:
                try:
                    user_id = int(user_id_str)
                except:
                    user_id = None
                if user_id:
                    if plan_key in PLANS:
                        plan_label = PLANS[plan_key]["label"]
                        plan_days = PLANS[plan_key]["days"]
                    else:
                        plan_label = plan_key or "Membership"
                        plan_days = days or 0
                    update_subscription(user_id, plan_label, plan_days)
                    try:
                        if order_id:
                            update_invoice_status(order_id, "paid")
                    except:
                        logger.exception("Failed to update invoice status")
                    try:
                        notify_user_subscription_activated(user_id, plan_label)
                    except:
                        logger.exception("Failed to notify user about activation")
                    logger.info("Activated subscription for user %s", user_id)
                    return jsonify({"status":"ok"}), 200

            return jsonify({"status":"ignored", "reason":"no user_id"}), 200

        try:
            if order_id:
                update_invoice_status(order_id, status or "pending")
        except Exception:
            logger.exception("Failed to update invoice status to non-final status")

        return jsonify({"status":"ok", "message":"not final status"}), 200

    except Exception as e:
        logger.exception("IPN processing error")
        return jsonify({"status":"error","message":str(e)}), 500

@flask_app.route("/success")
def success_redirect():
    order_id = request.args.get("order_id")
    user_id_q = request.args.get("user_id")
    plan = request.args.get("plan")

    if not order_id:
        return "Missing order_id", 400

    logger.info("Success redirect for order=%s user=%s plan=%s", order_id, user_id_q, plan)

    status = None
    headers = {"x-api-key": NOWPAYMENTS_API_KEY}

    try:
        r = requests.get(f"https://api.nowpayments.io/v1/invoice/{order_id}", headers=headers, timeout=10)
        if r.status_code == 200:
            d = r.json()
            status = d.get("status") or d.get("payment_status") or d.get("invoice_status")
    except Exception:
        logger.debug("invoice endpoint failed for order %s", order_id)

    if not status:
        try:
            r = requests.get(f"https://api.nowpayments.io/v1/payment/{order_id}", headers=headers, timeout=10)
            if r.status_code == 200:
                d = r.json()
                status = d.get("status")
        except Exception:
            logger.debug("payment endpoint failed for order %s", order_id)

    inv = find_invoice(order_id)
    if not status and inv and inv[5]:
        status = inv[5]

    logger.info("Success-check status=%s", status)

    if status and str(status).lower() in ("finished","paid","successful"):
        uid = None
        if user_id_q:
            try:
                uid = int(user_id_q)
            except:
                uid = None
        if not uid and inv:
            uid = inv[2]
        if uid:
            try:
                plan_label = PLANS[plan]["label"] if plan in PLANS else (plan or "Membership")
                plan_days = PLANS[plan]["days"] if plan in PLANS else 0
                update_subscription(uid, plan_label, plan_days)
                update_invoice_status(order_id, "paid")
                notify_user_subscription_activated(uid, plan_label)
            except Exception:
                logger.exception("activation on success redirect failed")
        return redirect(WARROOM_LINK)
    else:
        try:
            uid = int(user_id_q) if user_id_q else (inv[2] if inv else None)
            if uid:
                notify_user_provisional(uid)
        except Exception:
            logger.exception("provisional notify failed")
        return redirect(WARROOM_LINK)

# ----------------- Simple admin (same as before) -----------------
def admin_login_page(msg=""):
    return f"""
    <html><head><title>Admin Login</title></head><body>
    <h2>Admin login</h2>
    <p style="color:red;">{escape(msg)}</p>
    <form method="post" action="/admin">
      Username: <input name="username"><br>
      Password: <input name="password" type="password"><br>
      <button type="submit">Login</button>
    </form>
    </body></html>
    """

def dashboard_page():
    rows = query_db("SELECT user_id, username, subscription_plan, subscription_end, referrals, commission, referred_by, referral_id, last_notified FROM users ORDER BY subscription_end DESC", ())
    table_rows = ""
    for r in rows:
        table_rows += "<tr>" + "".join([f"<td>{escape(str(x))}</td>" for x in r]) + f"<td><form method='post' action='/resend_activation'><input type='hidden' name='user_id' value='{r[0]}'><button>Resend Activation</button></form></td></tr>"
    return f"<html><body><h2>Admin Dashboard</h2><a href='/logout'>Logout</a><table border=1><tr><th>user_id</th><th>username</th><th>plan</th><th>expiry</th><th>referrals</th><th>commission</th><th>referred_by</th><th>refid</th><th>last_notified</th><th>actions</th></tr>{table_rows}</table></body></html>"

@flask_app.route("/admin", methods=["GET","POST"])
def admin_login():
    if request.method == "GET":
        if session.get(ADMIN_SESSION_KEY):
            return dashboard_page()
        return admin_login_page()
    username = request.form.get("username","")
    password = request.form.get("password","")
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        session[ADMIN_SESSION_KEY] = True
        return redirect("/admin")
    return admin_login_page("Invalid credentials")

@flask_app.route("/logout")
def admin_logout():
    session.pop(ADMIN_SESSION_KEY, None)
    return redirect("/admin")

@flask_app.route("/resend_activation", methods=["POST"])
def resend_activation():
    if not session.get(ADMIN_SESSION_KEY):
        return redirect("/admin")
    try:
        user_id = int(request.form.get("user_id"))
        row = get_user(user_id)
        if row and row[2]:
            notify_user_subscription_activated(user_id, row[2])
    except Exception:
        logger.exception("resend failed")
    return redirect("/admin")

# ----------------- Start Flask & Telegram -----------------
def run_flask():
    flask_app.run(host="0.0.0.0", port=5000)

def main():
    global TELEGRAM_BOT
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info("Flask started on port 5000 (background thread).")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(warroom_cb, pattern="^warroom$"))
    app.add_handler(CallbackQueryHandler(subscribe_handler, pattern="^sub_"))
    app.add_handler(CallbackQueryHandler(lambda u,c: None, pattern="^earn$"))  # placeholder
    app.add_handler(CallbackQueryHandler(lambda u,c: None, pattern="^help$"))  # placeholder
    app.add_handler(MessageHandler(filters.ALL, any_message_handler))

    TELEGRAM_BOT = app.bot

    logger.info("Starting Telegram bot polling.")
    app.run_polling()

if __name__ == "__main__":
    main()
