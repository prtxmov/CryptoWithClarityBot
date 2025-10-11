#!/usr/bin/env python3
# final_bot1.py — Render + Flask 3.x safe, Telegram via HTTP (no async), NowPayments flow

import os, json, logging, sqlite3, time, requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, redirect, session
from markupsafe import escape

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

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ---------------- APP / LOGS ----------------
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "replace-me")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bot")

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

# ---------------- Telegram HTTP helpers ----------------
def tg_send(method, payload):
    try:
        r = requests.post(f"{TG_API}/{method}", json=payload, timeout=15)
        if r.status_code != 200:
            logger.warning("TG %s failed: %s %s", method, r.status_code, r.text)
        return r.json() if r.headers.get("content-type","").startswith("application/json") else {}
    except Exception:
        logger.exception("TG call failed: %s", method)
        return {}

def tg_send_message(chat_id, text, reply_markup=None, parse_mode=None):
    data = {"chat_id": chat_id, "text": text}
    if parse_mode:
        data["parse_mode"] = parse_mode
    if reply_markup:
        data["reply_markup"] = reply_markup
    return tg_send("sendMessage", data)

def kb_inline(button_rows):
    # button_rows = [[{"text":"...", "url":"..."}, {"text":"...", "callback_data":"..."}], [...]]
    return {"inline_keyboard": button_rows}

def btn(text, url=None, cb=None):
    b = {"text": text}
    if url:
        b["url"] = url
    if cb:
        b["callback_data"] = cb
    return b

# ---------------- Helpers ----------------
def send_main_menu(chat_id):
    keyboard = kb_inline([
        [btn("🌍 Public Community", url="https://t.me/+jUlj8kNrBRg2NGY9")],
        [btn("🔥 Warroom", cb="warroom")],
        [btn("🎁 Airdrop Community", url="https://t.me/+qmz3WHjuvjcxYjM1")],
        [btn("📞 Support Team", url="https://t.me/CryptoWith_Sarvesh")],
        [btn("💹 Start Trading", url="https://axiom.trade/@sarvesh")],
        [btn("ℹ️ About", cb="about")],
        [btn("💰 Earn", cb="earn")],
        [btn("🆘 Help", cb="help")]
    ])
    tg_send_message(chat_id,
        "🚀 Welcome to *CryptoWithSarvesh Bot*!\nChoose an option below:",
        reply_markup=keyboard, parse_mode="Markdown")

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
        r = requests.post(NOW_URL, headers=headers, json=payload, timeout=20)
        d = r.json()
        invoice_url = d.get("invoice_url") or d.get("payment_url") or d.get("url")
        if invoice_url:
            save_invoice(order_id, user_id, plan_key, invoice_url, plan["amount"], d.get("status","pending"))
            return invoice_url, None
        return None, d.get("message") or d.get("error") or "Unknown error"
    except Exception as e:
        logger.exception("create invoice failed")
        return None, str(e)

def ensure_webhook():
    url = f"{PUBLIC_URL.rstrip('/')}/telegram_webhook"
    try:
        r = requests.post(f"{TG_API}/setWebhook",
                          data={"url": url, "drop_pending_updates": "true"},
                          timeout=10)
        logger.info("setWebhook -> %s %s", r.status_code, r.text)
    except Exception:
        logger.exception("setWebhook failed")

# ---------------- Routes ----------------
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
                tg_send_message(
                    cid,
                    "🎟️ You are already subscribed! Use Earn to extend.",
                    reply_markup=kb_inline([[btn("💰 Earn", cb="earn")], [btn("🏠 Main Menu", cb="menu")]])
                )
            else:
                plans = kb_inline([
                    [btn("💵 $10 / week", cb="sub_10")],
                    [btn("💳 $20 / month", cb="sub_20")],
                    [btn("💎 $50 / 3 months", cb="sub_50")],
                    [btn("🏠 Main Menu", cb="menu")]
                ])
                tg_send_message(cid, "⚡ Choose a subscription plan:", reply_markup=plans)
            return jsonify({"ok": True})

        if payload.startswith("sub_"):
            link, err = generate_payment_link(cid, payload)
            if link:
                tg_send_message(cid, f"✅ Pay here:\n{link}")
            else:
                tg_send_message(cid, f"❌ Error: {err}")
            return jsonify({"ok": True})

        if payload == "earn":
            if is_subscribed(cid):
                u = get_user(cid)
                refid = (u[7] if u else f"ref{cid}")
                comm = (u[5] if u else 0.0)
                tg_send_message(cid, f"💰 Earn Program\nYour referral id: `{refid}`\nCommission: {comm} USD",
                                parse_mode="Markdown")
            else:
                tg_send_message(cid, "🔒 Earn is for subscribed users only.")
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
        tg_send_message(
            user_id,
            f"💥 Hey upcoming billionaire! Your {plan_label} access is active!",
            reply_markup=kb_inline([[btn("➡️ Join Now", url=WARROOM_LINK)]])
        )
    return jsonify({"ok": True})

@app.get("/success")
def success():
    user_id = int(request.args.get("user_id", 0))
    plan = request.args.get("plan", "")
    if user_id:
        plan_label = PLANS.get(plan, {}).get("label", plan or "Membership")
        tg_send_message(
            user_id,
            f"💥 Hey upcoming billionaire! Your {plan_label} access is active!",
            reply_markup=kb_inline([[btn("➡️ Join Now", url=WARROOM_LINK)]])
        )
    return redirect(WARROOM_LINK)

@app.get("/cancel")
def cancel():
    user_id = int(request.args.get("user_id", 0))
    if user_id:
        tg_send_message(
            user_id,
            "❌ Payment cancelled or timed out.",
            reply_markup=kb_inline([[btn("🏠 Main Menu", cb="menu")]])
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

# ---------------- BOOTSTRAP ON IMPORT ----------------
def _bootstrap():
    try:
        init_db()
    except Exception:
        logger.exception("DB init failed")
    try:
        ensure_webhook()
    except Exception:
        logger.exception("Webhook init failed")

_bootstrap()  # works with gunicorn final_bot1:app

# ---------------- DEV ----------------
if __name__ == "__main__":
    logger.info("Running on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT)
