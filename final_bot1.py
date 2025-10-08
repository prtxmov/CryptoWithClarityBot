#!/usr/bin/env python3
# final_bot_admin.py
"""
Telegram bot + NowPayments IPN + Admin backend (Flask)
Includes active /success verification: verifies invoice with NowPayments when payer is redirected.
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

# PUBLIC_URL must be your server domain (used to build ipn + success callbacks)
PUBLIC_URL = "https://cryptowithclaritybot.onrender.com"

# Admin credentials
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "prtxsarveshadmin811994"
ADMIN_SESSION_KEY = "admin_logged_in"

# Warroom link provided
WARROOM_LINK = "https://t.me/+IM_nIsf78JI4NzI1"

PLANS = {
    "sub_10": {"label": "Weekly", "amount": 10, "days": 7},
    "sub_20": {"label": "Monthly", "amount": 20, "days": 30},
    "sub_50": {"label": "3-Months", "amount": 50, "days": 90},
}
REFERRAL_RATE = 0.25
NOWPAYMENTS_INVOICE_URL = "https://api.nowpayments.io/v1/invoice"

# Flask app & secret
flask_app = Flask(__name__)
flask_app.secret_key = "replace-with-a-random-secret-if-you-like"

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("final_bot_admin")

# Global Telegram bot pointer
TELEGRAM_BOT = None

# ----------------- DATABASE UTILITIES -----------------
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
    # invoices table
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

def query_db(query, args=(), one=False):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute(query, args)
    rv = c.fetchall()
    conn.commit()
    conn.close()
    return (rv[0] if rv else None) if one else rv

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
    logger.info("Subscription updated: %s -> %s (%s days)", user_id, plan_label, days)

def set_subscription_end(user_id, end_datetime):
    query_db("UPDATE users SET subscription_end=? WHERE user_id=?", (end_datetime, user_id))

def add_commission_to(referral_id_or_userid, amount):
    # try by referral_id first
    res = query_db("SELECT * FROM users WHERE referral_id=?", (referral_id_or_userid,), one=True)
    if res:
        query_db("UPDATE users SET commission = commission + ? WHERE referral_id=?", (amount, referral_id_or_userid))
        return True
    # try by numeric user_id
    try:
        uid = int(referral_id_or_userid)
        query_db("UPDATE users SET commission = commission + ? WHERE user_id=?", (amount, uid))
        return True
    except Exception:
        return False

def revoke_subscription(user_id):
    query_db("UPDATE users SET subscription_plan=NULL, subscription_end=NULL WHERE user_id=?", (user_id,))

def increase_referrals(referral_id):
    query_db("UPDATE users SET referrals = referrals + 1 WHERE referral_id=?", (referral_id,))

def list_users(limit=200):
    return query_db("SELECT user_id, username, subscription_plan, subscription_end, referrals, commission, referred_by, referral_id, last_notified FROM users ORDER BY subscription_end DESC LIMIT ?", (limit,))

def update_last_notified(user_id, dt: datetime):
    query_db("UPDATE users SET last_notified=? WHERE user_id=?", (dt.strftime("%Y-%m-%d %H:%M:%S"), user_id))

def get_last_notified(user_id):
    row = query_db("SELECT last_notified FROM users WHERE user_id=?", (user_id,), one=True)
    if row and row[0]:
        try:
            return datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    return None

def save_invoice(order_id, user_id, plan_key, invoice_url, amount, status="pending"):
    query_db("INSERT INTO invoices (order_id, user_id, plan_key, invoice_url, status, amount, created_at) VALUES (?,?,?,?,?,?,?)",
             (order_id, user_id, plan_key, invoice_url or "", status, amount, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))

def find_invoice(order_id):
    return query_db("SELECT * FROM invoices WHERE order_id=?", (order_id,), one=True)

def update_invoice_status(order_id, status):
    query_db("UPDATE invoices SET status=? WHERE order_id=?", (status, order_id))

# ----------------- NOWPAYMENTS -----------------
def generate_payment_link(user_id, plan_key):
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
        "ipn_callback_url": f"{PUBLIC_URL.rstrip('/')}/ipn?user_id={user_id}&plan={plan_key}&days={days}",
        "success_url": f"{PUBLIC_URL.rstrip('/')}/success?order_id={order_id}&user_id={user_id}&plan={plan_key}",
        "cancel_url": f"{PUBLIC_URL.rstrip('/')}/cancel?order_id={order_id}&user_id={user_id}"
    }
    headers = {"x-api-key": NOWPAYMENTS_API_KEY, "Content-Type": "application/json"}
    try:
        r = requests.post(NOWPAYMENTS_INVOICE_URL, headers=headers, json=payload, timeout=20)
        data = r.json()
        logger.info("NowPayments create invoice response: %s", json.dumps(data))
        invoice_url = data.get("invoice_url") or data.get("payment_url") or data.get("url")
        save_invoice(order_id, user_id, plan_key, invoice_url or "", amount, status=data.get("status", "pending"))
        if invoice_url:
            return invoice_url, None
        return None, data.get("error", "Unknown error from NowPayments")
    except Exception as e:
        logger.exception("Error creating NowPayments invoice")
        return None, str(e)

# ----------------- TELEGRAM BOT -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or ""
    referred_by = None
    # allow /start ref{user_id}
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref"):
            referred_by = arg
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
    await update.message.reply_text("🚀 Welcome to *CryptoWithClarity Bot*!\nChoose an option below:",
                                    reply_markup=keyboard, parse_mode="Markdown")

async def warroom_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await query.message.reply_text("⚡ Warroom is for subscribed users.\nChoose a plan to subscribe:",
                                       reply_markup=InlineKeyboardMarkup(buttons))
    else:
        perks = "🔥 *Warroom Perks:*\n- AI Prompts\n- Trading Bot Tools\n- Exclusive Community Access"
        await query.message.reply_text(perks, parse_mode="Markdown")

async def subscribe_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    cb = query.data
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
    await update.callback_query.message.reply_text("ℹ️ *About Us:*\nCryptoWithClarity provides AI trading prompts, signals, and premium communities.",
                                                  parse_mode="Markdown")

async def earn_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    referral_link = f"https://t.me/cryptowithsarvesh_bot?start=ref{user_id}"
    await query.message.reply_text(f"💰 *Earn Program:*\nInvite friends and earn referral commissions!\nYour referral link: {referral_link}",
                                   parse_mode="Markdown")

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.message.reply_text("🆘 *Help Menu:*\n- For support: @CryptoWith_Sarvesh\n- Payment Issues: Contact support\n- General Queries: Use /start",
                                                  parse_mode="Markdown")

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

# ----------------- FLASK IPN -----------------
@flask_app.route("/ipn", methods=["POST"])
def ipn_listener():
    logger.info("IPN received, headers=%s", dict(request.headers))
    try:
        header_secret = request.headers.get("x-nowpayments-ipn-secret") or request.headers.get("x-nowpayments-signature")
        body = request.get_json(force=True, silent=True) or {}
        logger.info("IPN body: %s", json.dumps(body))

        # verify secret if provided
        if NOWPAYMENTS_IPN_SECRET and header_secret and header_secret != NOWPAYMENTS_IPN_SECRET:
            logger.warning("IPN header secret mismatch")
            return jsonify({"status": "error", "message": "Invalid IPN secret"}), 403

        if NOWPAYMENTS_IPN_SECRET and not header_secret:
            # fallback: check payload
            if body.get("ipn_secret") and body.get("ipn_secret") != NOWPAYMENTS_IPN_SECRET:
                logger.warning("IPN payload secret mismatch")
                return jsonify({"status": "error", "message": "Invalid IPN secret (payload)"}), 403

        # query params we set when creating invoice
        user_id_str = request.args.get("user_id")
        plan_key = request.args.get("plan")
        days = int(request.args.get("days") or 0)

        status = body.get("payment_status") or body.get("status") or body.get("invoice_status")
        price_amount = float(body.get("price_amount") or body.get("paid_amount") or 0)

        logger.info("IPN verify: user_id=%s plan=%s days=%s status=%s amount=%s", user_id_str, plan_key, days, status, price_amount)

        if status in ("finished", "successful", "paid"):
            if user_id_str:
                try:
                    user_id = int(user_id_str)
                except:
                    user_id = None

                if user_id:
                    # Determine plan label & days
                    if plan_key in PLANS:
                        plan_label = PLANS[plan_key]["label"]
                        plan_days = PLANS[plan_key]["days"]
                    else:
                        plan_label = plan_key or "Unknown"
                        plan_days = days or 0

                    update_subscription(user_id, plan_label, plan_days)

                    # referral handling
                    user_rec = get_user(user_id)
                    if user_rec:
                        referred_by = user_rec[6]  # referred_by column
                        if referred_by:
                            commission_amount = price_amount * REFERRAL_RATE
                            add_commission_to(referred_by, commission_amount)
                            increase_referrals(referred_by)

                    logger.info("Activated subscription for user %s", user_id)

                    # notify user via Telegram (best-effort)
                    try:
                        if TELEGRAM_BOT:
                            TELEGRAM_BOT.send_message(chat_id=user_id,
                                                      text=f"🎉 You are now a member of our exclusive Warroom! Join here: {WARROOM_LINK}")
                    except Exception:
                        logger.exception("Failed to send Telegram activation message")

                    return jsonify({"status": "ok"}), 200

            return jsonify({"status": "ignored", "reason": "no user_id"}), 200

        return jsonify({"status": "ok", "message": "not final status"}), 200

    except Exception as e:
        logger.exception("IPN processing error")
        return jsonify({"status": "error", "message": str(e)}), 500

# ----------------- /success (active verification) -----------------
@flask_app.route("/success")
def success_redirect():
    """
    NowPayments redirects the payer here after completing the payment flow.
    This handler will attempt to verify the order using NowPayments APIs.
    If paid -> activate + notify + redirect to WARROOM.
    If not yet confirmed -> redirect to WARROOM and send provisional msg; IPN will finalize activation.
    """
    order_id = request.args.get("order_id")
    user_id_q = request.args.get("user_id")
    plan = request.args.get("plan")

    if not order_id:
        return "Missing order_id", 400

    logger.info("Success redirect for order=%s user=%s plan=%s", order_id, user_id_q, plan)

    status = None
    headers = {"x-api-key": NOWPAYMENTS_API_KEY}

    # try invoice endpoint first
    try:
        r = requests.get(f"https://api.nowpayments.io/v1/invoice/{order_id}", headers=headers, timeout=10)
        if r.status_code == 200:
            d = r.json()
            status = d.get("status") or d.get("payment_status") or d.get("invoice_status")
    except Exception:
        logger.debug("invoice endpoint failed for order %s", order_id)

    # fallback to payment endpoint
    if not status:
        try:
            r = requests.get(f"https://api.nowpayments.io/v1/payment/{order_id}", headers=headers, timeout=10)
            if r.status_code == 200:
                d = r.json()
                status = d.get("status")
        except Exception:
            logger.debug("payment endpoint failed for order %s", order_id)

    # fallback to invoices table
    inv = find_invoice(order_id)
    if not status and inv and inv[5]:
        status = inv[5]

    logger.info("Success-check status=%s for order=%s", status, order_id)

    if status and str(status).lower() in ("finished", "paid", "successful"):
        # activate immediately
        uid = None
        if user_id_q:
            try:
                uid = int(user_id_q)
            except:
                uid = None
        if not uid and inv:
            uid = inv[2]  # invoices.user_id
        if uid:
            try:
                plan_label = PLANS[plan]["label"] if plan in PLANS else (plan or "Membership")
                plan_days = PLANS[plan]["days"] if plan in PLANS else 0
                update_subscription(uid, plan_label, plan_days)
                if order_id:
                    update_invoice_status(order_id, "paid")
                # send final activation message
                try:
                    if TELEGRAM_BOT:
                        TELEGRAM_BOT.send_message(chat_id=uid,
                                                  text=f"🎉 Your payment is confirmed — you're now a member of the Warroom! Join here: {WARROOM_LINK}")
                except Exception:
                    logger.exception("Failed to send activation message on /success")
            except Exception:
                logger.exception("Activation on /success failed")
        # redirect user to Warroom for UX
        return redirect(WARROOM_LINK)
    else:
        # not confirmed yet: redirect for UX and send provisional message to user
        try:
            uid = int(user_id_q) if user_id_q else (inv[2] if inv else None)
            if uid and TELEGRAM_BOT:
                TELEGRAM_BOT.send_message(chat_id=uid,
                                          text=(f"🔔 We've redirected you to the Warroom: {WARROOM_LINK}\n\n"
                                                "We're verifying your payment — your subscription will be activated automatically once payment is confirmed."))
        except Exception:
            logger.exception("Failed provisional notify on /success")
        return redirect(WARROOM_LINK)

# ----------------- ADMIN PAGES -----------------
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

def dashboard_page(users, msg=""):
    rows_html = ""
    for u in users:
        user_id, username, plan, sub_end, referrals, commission, referred_by, referral_id, last_notified = u
        sub_end = sub_end or "N/A"
        plan = plan or "None"
        rows_html += f"""
        <tr>
          <td>{user_id}</td>
          <td>{escape(username or '')}</td>
          <td>{escape(str(referral_id or ''))}</td>
          <td>{escape(str(referred_by or ''))}</td>
          <td>{escape(plan)}</td>
          <td>{escape(sub_end)}</td>
          <td>{referrals}</td>
          <td>{commission}</td>
          <td>
            <form method="post" action="/activate" style="display:inline;">
              <input type="hidden" name="user_id" value="{user_id}">
              Plan:
              <select name="plan_key">
                <option value="sub_10">Weekly ($10)</option>
                <option value="sub_20">Monthly ($20)</option>
                <option value="sub_50">3-Months ($50)</option>
              </select>
              <button type="submit">Activate</button>
            </form>
            <form method="post" action="/extend" style="display:inline;">
              <input type="hidden" name="user_id" value="{user_id}">
              Days: <input name="days" value="30" size="3">
              <button type="submit">Extend</button>
            </form>
            <form method="post" action="/add_commission" style="display:inline;">
              <input type="hidden" name="user_id" value="{user_id}">
              Amount: <input name="amount" value="1.0" size="5">
              <button type="submit">Add Comm</button>
            </form>
            <form method="post" action="/revoke" style="display:inline;">
              <input type="hidden" name="user_id" value="{user_id}">
              <button type="submit">Revoke</button>
            </form>
          </td>
        </tr>
        """
    return f"""
    <html><head><title>Admin Dashboard</title></head><body>
    <h2>Admin Dashboard</h2>
    <p style="color:green;">{escape(msg)}</p>
    <p><a href="/logout">Logout</a></p>
    <table border="1" cellpadding="6" cellspacing="0">
      <tr><th>user_id</th><th>username</th><th>referral_id</th><th>referred_by</th>
          <th>plan</th><th>expiry</th><th>referrals</th><th>commission</th><th>actions</th></tr>
      {rows_html}
    </table>
    </body></html>
    """

@flask_app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "GET":
        # If already logged in, redirect to dashboard
        if session.get(ADMIN_SESSION_KEY):
            return redirect("/dashboard")
        return admin_login_page()
    # POST: attempt login
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        session[ADMIN_SESSION_KEY] = True
        return redirect("/dashboard")
    return admin_login_page("Invalid credentials")

@flask_app.route("/logout")
def admin_logout():
    session.pop(ADMIN_SESSION_KEY, None)
    return redirect("/admin")

@flask_app.route("/dashboard")
def dashboard():
    if not session.get(ADMIN_SESSION_KEY):
        return redirect("/admin")
    users = list_users(500)
    return dashboard_page(users)

@flask_app.route("/activate", methods=["POST"])
def admin_activate():
    if not session.get(ADMIN_SESSION_KEY):
        return redirect("/admin")
    try:
        user_id = int(request.form.get("user_id"))
        plan_key = request.form.get("plan_key")
        if plan_key not in PLANS:
            return redirect("/dashboard")
        plan = PLANS[plan_key]
        update_subscription(user_id, plan["label"], plan["days"])
        return redirect("/dashboard")
    except Exception as e:
        logger.exception("activate error")
        return redirect("/dashboard")

@flask_app.route("/extend", methods=["POST"])
def admin_extend():
    if not session.get(ADMIN_SESSION_KEY):
        return redirect("/admin")
    try:
        user_id = int(request.form.get("user_id"))
        days = int(request.form.get("days", 30))
        # fetch current expiry
        row = get_user(user_id)
        if row and row[3]:
            # column 3 = subscription_end
            try:
                cur = datetime.strptime(row[3], "%Y-%m-%d %H:%M:%S")
            except Exception:
                cur = datetime.utcnow()
            new_end = cur + timedelta(days=days)
        else:
            new_end = datetime.utcnow() + timedelta(days=days)
        set_subscription_end(user_id, new_end.strftime("%Y-%m-%d %H:%M:%S"))
        return redirect("/dashboard")
    except Exception:
        logger.exception("extend error")
        return redirect("/dashboard")

@flask_app.route("/add_commission", methods=["POST"])
def admin_add_commission():
    if not session.get(ADMIN_SESSION_KEY):
        return redirect("/admin")
    try:
        user_id = request.form.get("user_id")
        amount = float(request.form.get("amount", "0"))
        # add commission by user_id (numeric) or referral_id
        add_commission_to(user_id, amount)
        return redirect("/dashboard")
    except Exception:
        logger.exception("add commission error")
        return redirect("/dashboard")

@flask_app.route("/revoke", methods=["POST"])
def admin_revoke():
    if not session.get(ADMIN_SESSION_KEY):
        return redirect("/admin")
    try:
        user_id = int(request.form.get("user_id"))
        revoke_subscription(user_id)
        return redirect("/dashboard")
    except Exception:
        logger.exception("revoke error")
        return redirect("/dashboard")

# ----------------- START FLASK & TELEGRAM -----------------
def run_flask():
    # In dev use debug=False; on Render or production, the platform will run Flask as main web process.
    flask_app.run(host="0.0.0.0", port=5000)

def main():
    init_db()
    # Start Flask in background for local running (Render will typically run one process, adjust accordingly)
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info("Flask (admin + IPN) server started in background thread.")

    # Build Telegram app
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(warroom_handler, pattern="^warroom$"))
    app.add_handler(CallbackQueryHandler(subscribe_handler, pattern="^sub_"))
    app.add_handler(CallbackQueryHandler(about_handler, pattern="^about$"))
    app.add_handler(CallbackQueryHandler(earn_handler, pattern="^earn$"))
    app.add_handler(CallbackQueryHandler(help_handler, pattern="^help$"))
    app.add_handler(MessageHandler(filters.ALL, any_message_handler))

    global TELEGRAM_BOT
    TELEGRAM_BOT = app.bot

    logger.info("Starting Telegram bot polling.")
    app.run_polling()

if __name__ == "__main__":
    main()
