#!/usr/bin/env python3
# final_bot1.py
"""
Env-var driven Telegram + NowPayments + Flask admin app.
Reads secrets from environment variables (safer than hard-coding).
"""

import os
import logging
import sqlite3
import threading
import requests
import json
import time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, redirect, session, escape
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

# ---------------- CONFIG (from env) ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # required
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")  # required
NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET", "")
PUBLIC_URL = os.getenv("PUBLIC_URL")  # required (e.g. https://your-app.onrender.com)
WARROOM_LINK = os.getenv("WARROOM_LINK", "https://t.me/+IM_nIsf78JI4NzI1")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")  # required
DATABASE = os.getenv("DATABASE", "db.sqlite3")
FLASK_SECRET = os.getenv("FLASK_SECRET", os.urandom(24).hex())
PORT = int(os.getenv("PORT", "5000"))
REFERRAL_RATE = float(os.getenv("REFERRAL_RATE", "0.25"))
NOWPAYMENTS_INVOICE_URL = os.getenv("NOWPAYMENTS_INVOICE_URL", "https://api.nowpayments.io/v1/invoice")

# minimal validation
if not BOT_TOKEN or not NOWPAYMENTS_API_KEY or not PUBLIC_URL or not ADMIN_PASSWORD:
    raise SystemExit("Set BOT_TOKEN, NOWPAYMENTS_API_KEY, PUBLIC_URL and ADMIN_PASSWORD in environment")

PLANS = {
    "sub_10": {"label": "Weekly", "amount": 10, "days": 7},
    "sub_20": {"label": "Monthly", "amount": 20, "days": 30},
    "sub_50": {"label": "3-Months", "amount": 50, "days": 90},
}

# ---------------- Flask + logging ----------------
app = Flask(__name__)
app.secret_key = FLASK_SECRET
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("final_bot_admin")
TELEGRAM_BOT = None  # set later

# ---------------- DB helpers ----------------
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
    logger.info("Database ready: %s", DATABASE)

def query_db(q, args=(), one=False):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute(q, args)
    rows = cur.fetchall()
    conn.commit()
    conn.close()
    return (rows[0] if rows else None) if one else rows

def add_user(user_id, username, referred_by=None):
    referral_id = f"ref{user_id}"
    query_db("INSERT OR IGNORE INTO users (user_id, username, referred_by, referral_id) VALUES (?,?,?,?)",
             (user_id, username, referred_by, referral_id))
    query_db("UPDATE users SET referral_id=? WHERE user_id=? AND (referral_id IS NULL OR referral_id='')", (referral_id, user_id))
    return referral_id

def get_user(user_id):
    return query_db("SELECT * FROM users WHERE user_id=?", (user_id,), one=True)

def update_subscription(user_id, plan_label, days):
    end_dt = datetime.utcnow() + timedelta(days=days)
    query_db("UPDATE users SET subscription_plan=?, subscription_end=? WHERE user_id=?", (plan_label, end_dt.strftime("%Y-%m-%d %H:%M:%S"), user_id))
    logger.info("Subscription updated: %s -> %s (days=%s)", user_id, plan_label, days)

def save_invoice(order_id, user_id, plan_key, invoice_url, amount, status="pending"):
    query_db("INSERT INTO invoices (order_id, user_id, plan_key, invoice_url, status, amount, created_at) VALUES (?,?,?,?,?,?,?)",
             (order_id, user_id, plan_key, invoice_url or "", status, amount, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))

def update_invoice_status(order_id, status):
    query_db("UPDATE invoices SET status=? WHERE order_id=?", (status, order_id))

def find_invoice(order_id):
    return query_db("SELECT * FROM invoices WHERE order_id=?", (order_id,), one=True)

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

# ---------------- NowPayments helpers ----------------
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
        r = requests.post(NOWPAYMENTS_INVOICE_URL, json=payload, headers=headers, timeout=20)
        data = r.json()
        logger.info("NowPayments create invoice response: %s", json.dumps(data))
        invoice_url = data.get("invoice_url") or data.get("payment_url") or data.get("url")
        save_invoice(order_id, user_id, plan_key, invoice_url, amount, status=data.get("status", "pending"))
        return invoice_url, None if invoice_url else (None, data.get("error", "Unknown"))
    except Exception as e:
        logger.exception("Error creating invoice")
        return None, str(e)

# ---------------- Telegram notify ----------------
def notify_user_subscription_activated(user_id, plan_label):
    global TELEGRAM_BOT
    try:
        logger.info("notify_user called: bot=%s user=%s plan=%s", bool(TELEGRAM_BOT), user_id, plan_label)
        if TELEGRAM_BOT:
            TELEGRAM_BOT.send_message(chat_id=user_id, text=(f"🎉 Congratulations — your *{plan_label}* membership is active!\n\nJoin the Warroom: {WARROOM_LINK}"), parse_mode="Markdown")
        else:
            logger.warning("TELEGRAM_BOT not initialized; cannot notify %s", user_id)
    except Exception:
        logger.exception("Failed to send activation message to %s", user_id)

def notify_user_provisional(user_id):
    global TELEGRAM_BOT
    try:
        if TELEGRAM_BOT:
            TELEGRAM_BOT.send_message(chat_id=user_id, text=(f"🔔 We've redirected you to the Warroom: {WARROOM_LINK}\n\nWe're verifying your payment — activation will happen automatically once payment is confirmed."), parse_mode="Markdown")
    except Exception:
        logger.exception("Failed provisional notify to %s", user_id)

# ---------------- Telegram handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.username or "")
    buttons = [
        [InlineKeyboardButton("🔥 Warroom", callback_data="warroom")],
        [InlineKeyboardButton("💰 Earn", callback_data="earn")],
    ]
    await update.message.reply_text("Welcome — choose an option:", reply_markup=InlineKeyboardMarkup(buttons))

async def warroom_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    user = get_user(uid)
    if not user or not user[2]:
        buttons = [
            [InlineKeyboardButton("💵 $10 / week", callback_data="sub_10")],
            [InlineKeyboardButton("💳 $20 / month", callback_data="sub_20")],
        ]
        await query.message.reply_text("Warroom is for subscribers. Pick a plan:", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await query.message.reply_text(f"You are subscribed — join the Warroom: {WARROOM_LINK}")

async def subscribe_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    plan_key = query.data
    uid = query.from_user.id
    url, err = generate_payment_link(uid, plan_key)
    if url:
        await query.message.reply_text(f"Complete payment here:\n{url}")
    else:
        await query.message.reply_text(f"Failed to create payment link: {err}")

async def any_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        try:
            last = get_last_notified(user.id)
        except Exception:
            last = None
        now = datetime.utcnow()
        if (not last) or (now - last > timedelta(hours=24)):
            try:
                await update.message.reply_text(f"⚠️ Join the Warroom: {WARROOM_LINK}")
                update_last_notified(user.id, now)
            except Exception:
                logger.exception("Failed to send nudge")

# ---------------- Flask endpoints: /ipn and /success ----------------
@app.route("/ipn", methods=["POST"])
def ipn_listener():
    logger.info("IPN received, headers=%s", dict(request.headers))
    try:
        header_secret = request.headers.get("x-nowpayments-ipn-secret") or request.headers.get("x-nowpayments-signature")
        body = request.get_json(force=True, silent=True) or {}
        logger.info("IPN body: %s", json.dumps(body))

        # verify secret if configured
        if NOWPAYMENTS_IPN_SECRET:
            if header_secret and header_secret != NOWPAYMENTS_IPN_SECRET:
                logger.warning("IPN header secret mismatch")
                return jsonify({"status":"error","message":"invalid secret"}), 403
            if not header_secret and body.get("ipn_secret") and body.get("ipn_secret") != NOWPAYMENTS_IPN_SECRET:
                logger.warning("IPN payload secret mismatch")
                return jsonify({"status":"error","message":"invalid payload secret"}), 403

        user_id_q = request.args.get("user_id")
        plan = request.args.get("plan")
        days = int(request.args.get("days") or 0)
        status = body.get("payment_status") or body.get("status") or body.get("invoice_status")
        amount = float(body.get("price_amount") or body.get("paid_amount") or 0)
        order_id = body.get("order_id") or body.get("id")

        # fallback: resolve user via invoices table
        if not user_id_q and order_id:
            inv = find_invoice(order_id)
            if inv and inv[2]:
                user_id_q = str(inv[2])
                logger.info("Resolved user_id from invoice: %s -> %s", order_id, user_id_q)

        logger.info("IPN verify: user=%s plan=%s status=%s order=%s amount=%s", user_id_q, plan, status, order_id, amount)

        if str(status).lower() in ("finished", "paid", "successful"):
            if user_id_q:
                try:
                    uid = int(user_id_q)
                except:
                    uid = None
                if uid:
                    plan_label = PLANS[plan]["label"] if plan in PLANS else (plan or "Membership")
                    plan_days = PLANS[plan]["days"] if plan in PLANS else (days or 0)
                    update_subscription(uid, plan_label, plan_days)
                    try:
                        if order_id:
                            update_invoice_status(order_id, "paid")
                    except:
                        logger.exception("update invoice failed")
                    try:
                        notify_user_subscription_activated(uid, plan_label)
                    except:
                        logger.exception("notify failed")
                    return jsonify({"status":"ok"}), 200
            return jsonify({"status":"ignored","reason":"no user"}), 200

        # non-final: update invoice status
        if order_id:
            try:
                update_invoice_status(order_id, status or "pending")
            except:
                logger.exception("invoice update failed")
        return jsonify({"status":"ok","message":"non-final"}), 200
    except Exception:
        logger.exception("IPN handler error")
        return jsonify({"status":"error","message":"server error"}), 500

@app.route("/success")
def success_redirect():
    order_id = request.args.get("order_id")
    user_id_q = request.args.get("user_id")
    plan = request.args.get("plan")
    if not order_id:
        return "Missing order_id", 400

    logger.info("Success redirect order=%s user=%s plan=%s", order_id, user_id_q, plan)
    status = None
    headers = {"x-api-key": NOWPAYMENTS_API_KEY}

    try:
        r = requests.get(f"https://api.nowpayments.io/v1/invoice/{order_id}", headers=headers, timeout=10)
        if r.status_code == 200:
            d = r.json()
            status = d.get("status") or d.get("payment_status") or d.get("invoice_status")
    except Exception:
        logger.debug("invoice endpoint failed for %s", order_id)

    if not status:
        try:
            r = requests.get(f"https://api.nowpayments.io/v1/payment/{order_id}", headers=headers, timeout=10)
            if r.status_code == 200:
                d = r.json()
                status = d.get("status")
        except Exception:
            logger.debug("payment endpoint failed for %s", order_id)

    inv = find_invoice(order_id)
    if not status and inv and inv[5]:
        status = inv[5]

    logger.info("Success-check status=%s for order=%s", status, order_id)

    if status and str(status).lower() in ("finished", "paid", "successful"):
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
            except:
                logger.exception("activation on success failed")
        return redirect(WARROOM_LINK)
    else:
        try:
            uid = int(user_id_q) if user_id_q else (inv[2] if inv else None)
            if uid:
                notify_user_provisional(uid)
        except:
            logger.exception("provisional notify failed")
        return redirect(WARROOM_LINK)

# ---------------- Admin (simple) ----------------
def admin_login_page(msg=""):
    return f"""
    <html><head><title>Admin Login</title></head><body>
    <h2>Admin login</h2><p style="color:red;">{escape(msg)}</p>
    <form method="post" action="/admin">Username: <input name="username"><br>Password: <input name="password" type="password"><br><button type="submit">Login</button></form>
    </body></html>
    """

def dashboard_page():
    rows = query_db("SELECT user_id, username, subscription_plan, subscription_end, referrals, commission, referred_by, referral_id, last_notified FROM users ORDER BY subscription_end DESC", ())
    table_rows = ""
    for r in rows:
        table_rows += "<tr>" + "".join([f"<td>{escape(str(x))}</td>" for x in r]) + f"<td><form method='post' action='/resend_activation'><input type='hidden' name='user_id' value='{r[0]}'><button>Resend</button></form></td></tr>"
    return f"<html><body><h2>Admin</h2><a href='/logout'>Logout</a><table border=1><tr><th>user_id</th><th>username</th><th>plan</th><th>expiry</th><th>referrals</th><th>commission</th><th>referred_by</th><th>refid</th><th>last_notified</th><th>actions</th></tr>{table_rows}</table></body></html>"

@app.route("/admin", methods=["GET","POST"])
def admin():
    if request.method == "GET":
        if session.get("admin_logged_in"):
            return dashboard_page()
        return admin_login_page()
    username = request.form.get("username","")
    password = request.form.get("password","")
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        session["admin_logged_in"] = True
        return redirect("/admin")
    return admin_login_page("Invalid credentials")

@app.route("/logout")
def logout():
    session.pop("admin_logged_in", None)
    return redirect("/admin")

@app.route("/resend_activation", methods=["POST"])
def resend_activation():
    if not session.get("admin_logged_in"):
        return redirect("/admin")
    try:
        uid = int(request.form.get("user_id"))
        row = get_user(uid)
        if row and row[2]:
            notify_user_subscription_activated(uid, row[2])
    except:
        logger.exception("resend failed")
    return redirect("/admin")

# ---------------- Start Flask + Telegram ----------------
def run_flask():
    app.run(host="0.0.0.0", port=PORT)

def main():
    global TELEGRAM_BOT
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info("Flask started on port %s", PORT)

    tg_app = ApplicationBuilder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CallbackQueryHandler(warroom_cb, pattern="^warroom$"))
    tg_app.add_handler(CallbackQueryHandler(subscribe_cb, pattern="^sub_"))
    tg_app.add_handler(MessageHandler(filters.ALL, any_msg))
    TELEGRAM_BOT = tg_app.bot
    logger.info("Starting Telegram polling")
    tg_app.run_polling()

if __name__ == "__main__":
    main()
