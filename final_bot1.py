#!/usr/bin/env python3
# final_bot1.py — Render-ready, Flask 3.x safe (no before_first_request)

import os, json, logging, sqlite3, time, requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, redirect, session
from markupsafe import escape
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")
NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET", "")
PUBLIC_URL = os.getenv("PUBLIC_URL")
WARROOM_LINK = os.getenv("WARROOM_LINK", "https://t.me/+IM_nIsf78JI4NzI1")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
DATABASE = os.getenv("DATABASE", "db.sqlite3")
PORT = int(os.getenv("PORT", "5000"))

if not BOT_TOKEN or not NOWPAYMENTS_API_KEY or not PUBLIC_URL or not ADMIN_PASSWORD:
    raise SystemExit("Set BOT_TOKEN, NOWPAYMENTS_API_KEY, PUBLIC_URL, ADMIN_PASSWORD in env")

# ---------------- APP / LOGS / BOT ----------------
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "replace-me")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bot")
bot = Bot(BOT_TOKEN)

PLANS = {
    "sub_10": {"label": "Weekly", "amount": 10, "days": 7},
    "sub_20": {"label": "Monthly", "amount": 20, "days": 30},
    "sub_50": {"label": "3-Months", "amount": 50, "days": 90},
}
NOW_URL = "https://api.nowpayments.io/v1/invoice"

# ---------------- DB ----------------
def init_db():
    conn = sqlite3.connect(DATABASE, check_same_thread=False)
    c = conn.cursor()
    c.execute("""
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
    """)
    c.execute("""
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
    """)
    conn.commit()
    conn.close()
    logger.info("✅ DB schema ensured.")

def qdb(query, args=(), one=False):
    conn = sqlite3.connect(DATABASE, check_same_thread=False)
    c = conn.cursor()
    c.execute(query, args)
    rows = c.fetchall()
    conn.commit()
    conn.close()
    return (rows[0] if rows else None) if one else rows

def add_user(user_id, username, referred_by=None):
    refid = f"ref{user_id}"
    qdb("INSERT OR IGNORE INTO users (user_id, username, referred_by, referral_id) VALUES (?,?,?,?)",
        (user_id, username, referred_by, refid))
    return refid

def get_user(user_id):
    return qdb("SELECT * FROM users WHERE user_id=?", (user_id,), one=True)

def is_subscribed(user_id):
    u = get_user(user_id)
    if not u or not u[3]:
        return False
    try:
        return datetime.strptime(u[3], "%Y-%m-%d %H:%M:%S") > datetime.utcnow()
    except:
        return False

def update_subscription(user_id, plan_label, days):
    end = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    qdb("UPDATE users SET subscription_plan=?, subscription_end=? WHERE user_id=?", (plan_label, end, user_id))

def save_invoice(order_id, user_id, plan_key, invoice_url, amount, status="pending"):
    qdb("INSERT INTO invoices (order_id,user_id,plan_key,invoice_url,status,amount,created_at) VALUES (?,?,?,?,?,?,?)",
        (order_id, user_id, plan_key, invoice_url or "", status, amount, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))

# ---------------- HELPERS ----------------
def send_main_menu(chat_id):
    kb = [
        [InlineKeyboardButton("🌍 Public Community", url="https://t.me/+jUlj8kNrBRg2NGY9")],
        [InlineKeyboardButton("🔥 Warroom", callback_data="warroom")],
        [InlineKeyboardButton("🎁 Airdrop Community", url="https://t.me/+qmz3WHjuvjcxYjM1")],
        [InlineKeyboardButton("📞 Support Team", url="https://t.me/CryptoWith_Sarvesh")],
        [InlineKeyboardButton("💹 Start Trading", url="https://axiom.trade/@sarvesh")],
        [InlineKeyboardButton("ℹ️ About", callback_data="about")],
        [InlineKeyboardButton("💰 Earn", callback_data="earn")],
        [InlineKeyboardButton("🆘 Help", callback_data="help")]
    ]
    bot.send_message(chat_id, "🚀 Welcome to *CryptoWithSarvesh Bot*!\nChoose an option below:",
                     reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

def generate_payment_link(user_id, plan_key):
    if plan_key not in PLANS:
        return None, "Invalid plan"
    plan = PLANS[plan_key]
    order_id = f"{user_id}_{int(time.time())}"
    payload = {
        "price_amount": plan["amount"],
        "price_currency": "usd",
        "order_id": order_id,
        "order_description": f"{plan['label']} subscription for user {user_id}",
        "ipn_callback_url": f"{PUBLIC_URL.rstrip('/')}/ipn?user_id={user_id}&plan={plan_key}&days={plan['days']}",
        "success_url": f"{PUBLIC_URL.rstrip('/')}/success?order_id={order_id}&user_id={user_id}&plan={plan_key}",
        "cancel_url": f"{PUBLIC_URL.rstrip('/')}/cancel?order_id={order_id}&user_id={user_id}"
    }
    headers = {"x-api-key": NOWPAYMENTS_API_KEY, "Content-Type": "application/json"}
    try:
        r = requests.post("https://api.nowpayments.io/v1/invoice", headers=headers, json=payload, timeout=20)
        d = r.json()
        invoice_url = d.get("invoice_url") or d.get("payment_url") or d.get("url")
        if invoice_url:
            save_invoice(order_id, user_id, plan_key, invoice_url, plan["amount"], d.get("status", "pending"))
            return invoice_url, None
        return None, d.get("message") or d.get("error") or "Unknown error from NowPayments"
    except Exception as e:
        logger.exception("create invoice failed")
        return None, str(e)

def ensure_webhook():
    url = f"{PUBLIC_URL.rstrip('/')}/telegram_webhook"
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                          data={"url": url, "drop_pending_updates": "true"}, timeout=10)
        logger.info("setWebhook -> %s %s", r.status_code, r.text)
    except Exception:
        logger.exception("setWebhook failed")

# ---------------- ROUTES ----------------
@app.get("/health")
def health():
    return jsonify({"ok": True, "status": "healthy"})

@app.post("/set_webhook")
def set_webhook_route():
    ensure_webhook()
    return jsonify({"ok": True})

@app.post("/telegram_webhook")
def telegram_webhook():
    data = request.get_json(force=True, silent=True) or {}
    logger.info("INCOMING TELEGRAM UPDATE: %s", json.dumps(data))

    if "message" in data:
        msg = data["message"]
        text = msg.get("text", "")
        cid = msg["chat"]["id"]
        uname = msg.get("from", {}).get("username", "")

        if text.startswith("/start"):
            # optional: parse /start refxxxx
            parts = text.split()
            ref = parts[1] if len(parts) > 1 and parts[1].startswith("ref") else None
            add_user(cid, uname, ref)
            send_main_menu(cid)
            return jsonify({"ok": True})

    if "callback_query" in data:
        cq = data["callback_query"]
        cid = cq["from"]["id"]
        payload = cq.get("data", "")

        if payload == "warroom":
            if is_subscribed(cid):
                bot.send_message(cid, "🎟️ You are already subscribed! Use Earn to extend.",
                                 reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💰 Earn", callback_data="earn")],
                                                                    [InlineKeyboardButton("🏠 Main Menu", callback_data="menu")]]))
            else:
                plans = [
                    [InlineKeyboardButton("💵 $10 / week", callback_data="sub_10")],
                    [InlineKeyboardButton("💳 $20 / month", callback_data="sub_20")],
                    [InlineKeyboardButton("💎 $50 / 3 months", callback_data="sub_50")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="menu")]
                ]
                bot.send_message(cid, "⚡ Choose a subscription plan:", reply_markup=InlineKeyboardMarkup(plans))
            return jsonify({"ok": True})

        if payload.startswith("sub_"):
            link, err = generate_payment_link(cid, payload)
            bot.send_message(cid, f"✅ Pay here:\n{link}" if link else f"❌ Error: {err}")
            return jsonify({"ok": True})

        if payload == "earn":
            if is_subscribed(cid):
                u = get_user(cid)
                refid = (u[7] if u else f"ref{cid}")
                comm = (u[5] if u else 0.0)
                bot.send_message(cid, f"💰 Earn Program\nYour referral id: `{refid}`\nCommission: {comm} USD",
                                 parse_mode="Markdown")
            else:
                bot.send_message(cid, "🔒 Earn is for subscribed users only.")
            return jsonify({"ok": True})

        if payload == "menu":
            send_main_menu(cid)
            return jsonify({"ok": True})

    return jsonify({"ok": True})

@app.post("/ipn")
def ipn():
    hdr = dict(request.headers)
    body = request.get_json(force=True, silent=True) or {}
    logger.info("IPN headers=%s body=%s", hdr, json.dumps(body))

    header_secret = hdr.get("x-nowpayments-ipn-secret") or hdr.get("x-nowpayments-signature")
    if NOWPAYMENTS_IPN_SECRET and header_secret and header_secret != NOWPAYMENTS_IPN_SECRET:
        logger.warning("IPN secret mismatch")
        return jsonify({"error": "invalid secret"}), 403

    status = (body.get("payment_status") or body.get("status") or body.get("invoice_status") or "").lower()
    user_id = int(request.args.get("user_id", 0))
    plan = request.args.get("plan", "")
    days = int(request.args.get("days", 0))

    if status in ("finished", "paid", "successful") and user_id:
        plan_label = PLANS.get(plan, {}).get("label", plan or "Membership")
        plan_days = PLANS.get(plan, {}).get("days", days or 0)
        update_subscription(user_id, plan_label, plan_days)
        bot.send_message(
            user_id,
            f"💥 Hey upcoming billionaire! Your {plan_label} access is active!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Join Now", url=WARROOM_LINK)]])
        )
    return jsonify({"ok": True})

@app.get("/success")
def success():
    user_id = int(request.args.get("user_id", 0))
    plan = request.args.get("plan", "")
    if user_id:
        plan_label = PLANS.get(plan, {}).get("label", plan or "Membership")
        bot.send_message(
            user_id,
            f"💥 Hey upcoming billionaire! Your {plan_label} access is active!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Join Now", url=WARROOM_LINK)]])
        )
    return redirect(WARROOM_LINK)

@app.get("/cancel")
def cancel():
    user_id = int(request.args.get("user_id", 0))
    if user_id:
        bot.send_message(
            user_id,
            "❌ Payment cancelled or timed out.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="menu")]])
        )
    return "<h2>Payment Cancelled</h2>", 200

# ---------------- ADMIN ----------------
def admin_login_page(msg=""):
    return f"""
    <html><head><title>Admin Login</title></head><body>
    <h2>Admin login</h2><p style="color:red;">{escape(msg)}</p>
    <form method="post" action="/admin">
      Username: <input name="username"><br>
      Password: <input type="password" name="password"><br>
      <button>Login</button>
    </form>
    </body></html>
    """

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        if request.form.get("username") == ADMIN_USERNAME and request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect("/admin")
        return admin_login_page("Invalid credentials")
    if not session.get("admin"):
        return admin_login_page()
    return "<h2>Admin logged in</h2>"

# ---------------- IMPORT-TIME BOOTSTRAP (Flask 3 safe) ----------------
def _bootstrap():
    try:
        init_db()
    except Exception:
        logger.exception("DB init failed")
    try:
        ensure_webhook()
    except Exception:
        logger.exception("Webhook init failed")

_bootstrap()  # runs on import (works with gunicorn final_bot1:app)

# ---------------- LOCAL DEV ----------------
if __name__ == "__main__":
    logger.info("Running dev server on %s", PORT)
    app.run(host="0.0.0.0", port=PORT)
