#!/usr/bin/env python3
# final_bot1.py — Render + Flask 3.x safe, Telegram via HTTP (no async), NowPayments flow
# Updated: Added referral link generation, commission tracking, extend-with-commission, withdraw flow (SOL payouts)

import os, json, logging, sqlite3, time, requests, urllib.parse
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

# Bot username (will be fetched at startup). Fallback if fetch fails:
BOT_USERNAME = os.getenv("BOT_USERNAME", None)

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
    # track withdraw requests
    c.execute("""
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            sol_address TEXT,
            status TEXT,
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
    # update username if exists (in case it changed)
    qdb("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
    return refid

def get_user(user_id):
    return qdb("SELECT * FROM users WHERE user_id=?", (user_id,), one=True)

def get_user_by_referral_id(referral_id):
    return qdb("SELECT * FROM users WHERE referral_id=?", (referral_id,), one=True)

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

def add_commission_to_user(user_id, amount):
    qdb("UPDATE users SET commission = commission + ? WHERE user_id=?", (amount, user_id))

def increment_referrals(user_id):
    qdb("UPDATE users SET referrals = referrals + 1 WHERE user_id=?", (user_id,))

def create_withdrawal_request(user_id, amount):
    qdb("INSERT INTO withdrawals (user_id, amount, sol_address, status, created_at) VALUES (?,?,?,?,?)",
        (user_id, amount, None, "awaiting_address", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))

def get_pending_withdrawal(user_id):
    return qdb("SELECT * FROM withdrawals WHERE user_id=? AND status='awaiting_address' ORDER BY id DESC LIMIT 1", (user_id,), one=True)

def finalize_withdrawal(withdrawal_id, sol_address):
    qdb("UPDATE withdrawals SET sol_address=?, status='pending' WHERE id=?", (sol_address, withdrawal_id))

def mark_withdrawal_ready(withdrawal_id):
    qdb("UPDATE withdrawals SET status='pending' WHERE id=?", (withdrawal_id,))

def deduct_commission(user_id, amount):
    qdb("UPDATE users SET commission = commission - ? WHERE user_id=?", (amount, user_id))

# ---------------- Telegram HTTP helpers ----------------
def tg_send(method, payload):
    try:
        r = requests.post(f"{TG_API}/{method}", json=payload, timeout=15)
        if r.status_code != 200:
            logger.warning("TG %s failed: %s %s", method, r.status_code, r.text)
        # protect against non-json responses
        try:
            return r.json() if r.headers.get("content-type","").startswith("application/json") else {}
        except Exception:
            return {}
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
        # About button removed per your request
        [btn("💰 Earn", cb="earn")],
        # Help now links to the CWShelp chat
        [btn("🆘 Help", url="https://t.me/CWShelp")]
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

def get_bot_username():
    global BOT_USERNAME
    try:
        r = requests.get(f"{TG_API}/getMe", timeout=10)
        d = r.json()
        if d.get("ok") and d.get("result"):
            BOT_USERNAME = d["result"].get("username") or BOT_USERNAME
            logger.info("Bot username: %s", BOT_USERNAME)
    except Exception:
        logger.exception("Failed to fetch bot username")

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

    # handle messages (text replies including SOL addresses for withdrawals)
    if "message" in data:
        msg = data["message"]
        text = msg.get("text", "")
        cid = msg["chat"]["id"]
        uname = msg.get("from", {}).get("username", "")

        # START handling (deep-link referral)
        if text and text.startswith("/start"):
            parts = text.split()
            ref = None
            if len(parts) > 1 and parts[1].startswith("ref"):
                ref = parts[1]
            add_user(cid, uname, ref)
            send_main_menu(cid)
            return jsonify({"ok": True})

        # If user has a pending withdrawal awaiting SOL address, treat their next message as SOL address
        pending = get_pending_withdrawal(cid)
        if pending:
            # treat the message as SOL address (no heavy validation here)
            sol_address = text.strip()
            if not sol_address:
                tg_send_message(cid, "⚠️ Please send a valid SOL address.")
                return jsonify({"ok": True})
            # finalize withdrawal
            withdrawal_id = pending[0] if isinstance(pending, (list, tuple)) else None
            # pending returned as a row: (id, user_id, amount, sol_address, status, created_at)
            # Use indexing robustly:
            wid = pending[0]
            amount = pending[2]
            finalize_withdrawal(wid, sol_address)
            # deduct commission (we assume user is withdrawing full amount; we already stored amount as current commission at creation)
            deduct_commission(cid, amount)
            # prepare payout message
            u = get_user(cid)
            username_or_id = (u[1] if u and u[1] else f"{cid}")
            payout_text = f"PAYOUT REQUEST\nUser: @{username_or_id}\nAmount: ${amount:.2f}\nSOL Address: {sol_address}"
            encoded = urllib.parse.quote_plus(payout_text)
            # provide two buttons: open CWSpayout chat with text (some clients support ?text) and a share link
            keyboard = kb_inline([
                [btn("Open CWSpayout chat (with message)", url=f"https://t.me/CWSpayout?text={encoded}")],
                [btn("Share payout message", url=f"https://t.me/share/url?url=&text={encoded}")],
                [btn("🏠 Main Menu", cb="menu")]
            ])
            tg_send_message(cid, "✅ Withdrawal request prepared. Use the buttons below to send the payout request to the payout channel/team.", reply_markup=keyboard)
            # optionally notify admin (you can add webhook or alert here)
            return jsonify({"ok": True})

    # handle callback_query (button presses)
    if "callback_query" in data:
        cq = data["callback_query"]
        cid = cq["from"]["id"]
        payload = cq.get("data", "")
        uname = cq["from"].get("username", "")

        # WARROOM flow (existing)
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

        # subscription creation via callback (existing)
        if payload.startswith("sub_"):
            link, err = generate_payment_link(cid, payload)
            if link:
                tg_send_message(cid, f"✅ Pay here:\n{link}")
            else:
                tg_send_message(cid, f"❌ Error: {err}")
            return jsonify({"ok": True})

        # EARN button — show referral + commission + actions (NEW)
        if payload == "earn":
            if is_subscribed(cid):
                u = get_user(cid)
                refid = (u[7] if u else f"ref{cid}")
                comm = (u[5] if u else 0.0)
                # Build Earn UI with actions: Referral Link, Extend with commission, Withdraw (if >=100)
                rows = [
                    [btn("🔗 Referral Link", cb="ref_link")],
                    [btn("↗️ Extend with commission", cb="extend")],
                    [btn("💸 Withdraw", cb="withdraw")],
                    [btn("🏠 Main Menu", cb="menu")]
                ]
                keyboard = kb_inline(rows)
                tg_send_message(cid, f"💰 Earn Program\nYour referral id: `{refid}`\nCommission: ${comm:.2f}\n\nYou can extend your subscription using commission or withdraw when you have at least $100.",
                                reply_markup=keyboard, parse_mode="Markdown")
            else:
                tg_send_message(cid, "🔒 Earn is for subscribed users only.")
            return jsonify({"ok": True})

        # show referral link
        if payload == "ref_link":
            u = get_user(cid)
            refid = (u[7] if u else f"ref{cid}")
            # build a telegram deep-link to start with ref
            if not BOT_USERNAME:
                get_bot_username()
            bot_user = BOT_USERNAME or os.getenv("BOT_USERNAME", "")
            if bot_user:
                ref_link = f"https://t.me/{bot_user}?start={refid}"
            else:
                # fallback to general public url (less ideal)
                ref_link = f"{PUBLIC_URL.rstrip('/')}/start?ref={refid}"

            # Build a friendly share message
            share_text = f"Join the Warroom: {ref_link}"
            encoded = urllib.parse.quote_plus(share_text)

            keyboard = kb_inline([
                # direct link button (opens the link in client)
                [btn("🔗 Open Link", url=ref_link)],
                # share via Telegram (pre-filled message)
                [btn("📤 Share via Telegram", url=f"https://t.me/share/url?url=&text={encoded}")],
                [btn("🏠 Main Menu", cb="menu")]
            ])

            tg_send_message(cid, f"🔗 Your referral link:\n{ref_link}\n\nShare this with friends. When they subscribe, you'll earn commission.", reply_markup=keyboard, parse_mode="Markdown")
            return jsonify({"ok": True})

        # extend using commission - show plan choices but for extend action
        if payload == "extend":
            # list plans but callbacks have prefix extend_sub_
            plans_kb = kb_inline([
                [btn("💵 $10 / week", cb="extend_sub_10")],
                [btn("💳 $20 / month", cb="extend_sub_20")],
                [btn("💎 $50 / 3 months", cb="extend_sub_50")],
                [btn("🏠 Main Menu", cb="menu")]
            ])
            tg_send_message(cid, "Select a plan to extend using your commission:", reply_markup=plans_kb)
            return jsonify({"ok": True})

        # handle extend_sub_ callbacks
        if payload.startswith("extend_sub_"):
            plan_key = payload.replace("extend_sub_", "sub_")
            if plan_key not in PLANS:
                tg_send_message(cid, "Invalid plan selected.")
                return jsonify({"ok": True})
            plan = PLANS[plan_key]
            u = get_user(cid)
            comm = (u[5] if u else 0.0)
            if comm >= plan["amount"]:
                # deduct commission and extend
                deduct_commission(cid, plan["amount"])
                update_subscription(cid, plan["label"], plan["days"])
                tg_send_message(cid, f"✅ Subscription extended by {plan['days']} days using ${plan['amount']:.2f} commission. Enjoy!\n\nNew commission balance will reflect shortly.")
            else:
                tg_send_message(cid, f"❌ Not enough commission. You need ${plan['amount']:.2f} but have ${comm:.2f}.")
            return jsonify({"ok": True})

        # withdraw flow
        if payload == "withdraw":
            u = get_user(cid)
            comm = (u[5] if u else 0.0)
            if comm < 100:
                tg_send_message(cid, f"💸 Withdrawals are allowed when you have at least $100 in commission. Your balance: ${comm:.2f}")
                return jsonify({"ok": True})
            # create withdrawal request for full commission amount and ask for SOL address
            amount = comm
            create_withdrawal_request(cid, amount)
            tg_send_message(cid, f"🔔 You requested to withdraw ${amount:.2f}. Please reply to this chat with your SOL address. After you send it we'll prepare the payout request and provide a button to send it to the payout team.")
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

    # Important: credit subscription and also handle referral commission crediting
    if status in ("finished", "paid", "successful") and user_id:
        plan_label = PLANS.get(plan, {}).get("label", plan or "Membership")
        plan_days = PLANS.get(plan, {}).get("days", days or 0)
        plan_amount = PLANS.get(plan, {}).get("amount", 0.0)
        update_subscription(user_id, plan_label, plan_days)
        tg_send_message(
            user_id,
            f"💥 Hey upcoming billionaire! Your {plan_label} access is active!",
            reply_markup=kb_inline([[btn("➡️ Join Now", url=WARROOM_LINK)]])
        )

        # credit commission to referring user if present
        # look up this user's referred_by (value like "ref123")
        u = get_user(user_id)
        if u:
            referred_by = u[6]
            if referred_by:
                ref_user = get_user_by_referral_id(referred_by)
                if ref_user:
                    ref_user_id = ref_user[0]
                    # commission rate — set 10% of plan amount
                    commission_amt = round(float(plan_amount) * 0.10, 2)
                    if commission_amt > 0:
                        add_commission_to_user(ref_user_id, commission_amt)
                        increment_referrals(ref_user_id)
                        # notify referrer
                        tg_send_message(
                            ref_user_id,
                            f"🎉 You earned a referral commission of ${commission_amt:.2f} from user @{u[1] or user_id}!\nYour new commission balance: ${get_user(ref_user_id)[5]:.2f}\nPress Earn to manage or withdraw.",
                            reply_markup=kb_inline([[btn("💰 Earn", cb="earn")]])
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
        get_bot_username()
    except Exception:
        logger.exception("get bot username failed")
    try:
        ensure_webhook()
    except Exception:
        logger.exception("Webhook init failed")

_bootstrap()  # works with gunicorn final_bot1:app

# ---------------- DEV ----------------
if __name__ == "__main__":
    logger.info("Running on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT)
