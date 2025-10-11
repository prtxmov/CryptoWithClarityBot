#!/usr/bin/env python3
# final_bot1.py — Fully working version (Render + NowPayments + Telegram)

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
    raise SystemExit("❌ Set BOT_TOKEN, NOWPAYMENTS_API_KEY, PUBLIC_URL, ADMIN_PASSWORD in environment")

# ---------------- FLASK & LOGGING ----------------
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


# ---------------- DATABASE ----------------
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
    logger.info("✅ Database initialized.")


def qdb(query, args=(), one=False):
    conn = sqlite3.connect(DATABASE, check_same_thread=False)
    c = conn.cursor()
    c.execute(query, args)
    rv = c.fetchall()
    conn.commit()
    conn.close()
    return (rv[0] if rv else None) if one else rv


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
        (order_id, user_id, plan_key, invoice_url, status, amount, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))


# ---------------- HELPERS ----------------
def send_main_menu(chat_id):
    keyboard = [
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
                     reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


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
        "ipn_callback_url": f"{PUBLIC_URL}/ipn?user_id={user_id}&plan={plan_key}&days={plan['days']}",
        "success_url": f"{PUBLIC_URL}/success?order_id={order_id}&user_id={user_id}&plan={plan_key}",
        "cancel_url": f"{PUBLIC_URL}/cancel?order_id={order_id}&user_id={user_id}"
    }
    headers = {"x-api-key": NOWPAYMENTS_API_KEY, "Content-Type": "application/json"}
    try:
        r = requests.post(NOW_URL, headers=headers, json=payload, timeout=20)
        d = r.json()
        invoice_url = d.get("invoice_url")
        if invoice_url:
            save_invoice(order_id, user_id, plan_key, invoice_url, plan["amount"])
            return invoice_url, None
        return None, d.get("message", "Error creating invoice")
    except Exception as e:
        return None, str(e)


# ---------------- TELEGRAM WEBHOOK ----------------
@app.route("/telegram_webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json(force=True, silent=True) or {}
    logger.info("INCOMING TELEGRAM UPDATE: %s", json.dumps(data))

    if "message" in data:
        msg = data["message"]
        text = msg.get("text", "")
        cid = msg["chat"]["id"]
        uname = msg.get("from", {}).get("username", "")

        if text.startswith("/start"):
            add_user(cid, uname)
            send_main_menu(cid)
            return jsonify({"ok": True})

    if "callback_query" in data:
        cq = data["callback_query"]
        cid = cq["from"]["id"]
        data_cb = cq.get("data", "")

        if data_cb == "warroom":
            if is_subscribed(cid):
                bot.send_message(cid, "🎟️ You are already subscribed! Use Earn to extend.",
                                 reply_markup=InlineKeyboardMarkup(
                                     [[InlineKeyboardButton("💰 Earn", callback_data="earn")]]))
            else:
                plans = [
                    [InlineKeyboardButton("💵 $10 / week", callback_data="sub_10")],
                    [InlineKeyboardButton("💳 $20 / month", callback_data="sub_20")],
                    [InlineKeyboardButton("💎 $50 / 3 months", callback_data="sub_50")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="menu")]
                ]
                bot.send_message(cid, "⚡ Choose a subscription plan:", reply_markup=InlineKeyboardMarkup(plans))
            return jsonify({"ok": True})

        if data_cb.startswith("sub_"):
            link, err = generate_payment_link(cid, data_cb)
            if link:
                bot.send_message(cid, f"✅ Click below to pay:\n{link}")
            else:
                bot.send_message(cid, f"❌ Error: {err}")
            return jsonify({"ok": True})

        if data_cb == "earn":
            if is_subscribed(cid):
                bot.send_message(cid, "💰 Earn program: Invite friends to get rewards!")
            else:
                bot.send_message(cid, "🔒 Earn is available only for Warroom members.")
            return jsonify({"ok": True})

        if data_cb == "menu":
            send_main_menu(cid)
            return jsonify({"ok": True})

    return jsonify({"ok": True})


# ---------------- IPN HANDLER ----------------
@app.route("/ipn", methods=["POST"])
def ipn():
    body = request.get_json(force=True, silent=True) or {}
    status = body.get("payment_status") or body.get("status")
    user_id = int(request.args.get("user_id", 0))
    plan = request.args.get("plan", "")
    days = int(request.args.get("days", 0))

    if status and status.lower() in ("finished", "paid", "successful"):
        if user_id:
            plan_label = PLANS.get(plan, {}).get("label", plan)
            update_subscription(user_id, plan_label, days)
            bot.send_message(user_id,
                             f"💥 Hey upcoming billionaire! Your {plan_label} access is active!",
                             reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Join Now", url=WARROOM_LINK)]]))
    return jsonify({"ok": True})


@app.route("/success")
def success():
    user_id = int(request.args.get("user_id", 0))
    plan = request.args.get("plan", "")
    if user_id:
        plan_label = PLANS.get(plan, {}).get("label", plan)
        bot.send_message(user_id,
                         f"💥 Hey upcoming billionaire! Your {plan_label} access is active!",
                         reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Join Now", url=WARROOM_LINK)]]))
    return redirect(WARROOM_LINK)


@app.route("/cancel")
def cancel():
    user_id = int(request.args.get("user_id", 0))
    if user_id:
        bot.send_message(user_id,
                         "❌ Payment cancelled or timed out.",
                         reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="menu")]]))
    return "<h2>Payment Cancelled</h2>", 200


# ---------------- ADMIN ----------------
@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        if request.form.get("username") == ADMIN_USERNAME and request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect("/admin")
        return "Invalid credentials", 401
    if not session.get("admin"):
        return '<form method="POST"><input name="username"><input name="password" type="password"><button>Login</button></form>'
    return "<h2>Admin logged in</h2>"


# ---------------- HEALTH + WEBHOOK ----------------
@app.route("/health")
def health():
    return jsonify({"ok": True, "status": "healthy"})


def ensure_webhook():
    url = f"{PUBLIC_URL}/telegram_webhook"
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                      data={"url": url, "drop_pending_updates": "true"})
    logger.info("setWebhook -> %s %s", r.status_code, r.text)


@app.before_first_request
def setup():
    init_db()
    ensure_webhook()


# ---------------- RUN ----------------
if __name__ != "__main__":
    init_db()
    ensure_webhook()

if __name__ == "__main__":
    init_db()
    ensure_webhook()
    logger.info(f"🚀 Running on port {PORT}")
    app.run(host="0.0.0.0", port=PORT)
