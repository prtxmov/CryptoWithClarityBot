#!/usr/bin/env python3
# final_bot1.py — Render + Flask 3.x safe, Telegram via HTTP (no async), NowPayments flow
# Updated: Added random referral ids, 25% commission, improved referral link generation,
# and admin panel with Payouts / Subscribers / Referral tracker + activation/extend actions.

import os, json, logging, sqlite3, time, requests, urllib.parse, uuid, secrets
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

# Bot username (will be fetched at startup). Provide your bot name so links are reliably created.
# (Do NOT include the leading @) — set to your bot username to ensure link generation works.
BOT_USERNAME = os.getenv("BOT_USERNAME", "cryptowithsarvesh_bot")

# Commission rate (25% as requested)
COMMISSION_RATE = 0.25

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

def _generate_unique_referral_id():
    # generate ref_<8 hex> and ensure uniqueness
    for _ in range(5):
        rid = "ref_" + uuid.uuid4().hex[:8]
        existing = qdb("SELECT 1 FROM users WHERE referral_id=?", (rid,), one=True)
        if not existing:
            return rid
    # fallback deterministic
    return f"ref_{secrets.token_hex(4)}"

def add_user(user_id, username, referred_by=None):
    """
    Adds a user if missing. Generates a unique random referral_id for each user unless they already have one.
    - preserves existing data for current users (so code updates won't reset subscriptions).
    """
    # Check if user exists
    existing = qdb("SELECT * FROM users WHERE user_id=?", (user_id,), one=True)
    if existing:
        # update username and referred_by if present (don't overwrite referral_id)
        qdb("UPDATE users SET username=?, referred_by=? WHERE user_id=?", (username, referred_by or existing[6], user_id))
        return existing[7]  # existing referral_id
    # create new referral id
    refid = _generate_unique_referral_id()
    qdb("INSERT INTO users (user_id, username, referred_by, referral_id) VALUES (?,?,?,?)",
        (user_id, username, referred_by, refid))
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
    """
    Extends/sets subscription_end by `days` from now (UTC).
    """
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

def mark_withdrawal_processed(withdrawal_id):
    qdb("UPDATE withdrawals SET status='processed' WHERE id=?", (withdrawal_id,))

def deduct_commission(user_id, amount):
    qdb("UPDATE users SET commission = commission - ? WHERE user_id=?", (amount, user_id))

# ---------------- Telegram HTTP helpers ----------------
def tg_send(method, payload):
    try:
        r = requests.post(f"{TG_API}/{method}", json=payload, timeout=15)
        if r.status_code != 200:
            logger.warning("TG %s failed: %s %s", method, r.status_code, r.text)
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
        [btn("💰 Earn", cb="earn")],
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
            sol_address = text.strip()
            if not sol_address:
                tg_send_message(cid, "⚠️ Please send a valid SOL address.")
                return jsonify({"ok": True})
            wid = pending[0]
            amount = pending[2]
            finalize_withdrawal(wid, sol_address)
            deduct_commission(cid, amount)
            u = get_user(cid)
            username_or_id = (u[1] if u and u[1] else f"{cid}")
            payout_text = f"PAYOUT REQUEST\nUser: @{username_or_id}\nAmount: ${amount:.2f}\nSOL Address: {sol_address}"
            encoded = urllib.parse.quote_plus(payout_text)
            keyboard = kb_inline([
                [btn("Open CWSpayout chat (with message)", url=f"https://t.me/CWSpayout?text={encoded}")],
                [btn("Share payout message", url=f"https://t.me/share/url?url=&text={encoded}")],
                [btn("🏠 Main Menu", cb="menu")]
            ])
            tg_send_message(cid, "✅ Withdrawal request prepared. Use the buttons below to send the payout request to the payout channel/team.", reply_markup=keyboard)
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
                refid = (u[7] if u else _generate_unique_referral_id())
                comm = (u[5] if u else 0.0)
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
            refid = (u[7] if u else _generate_unique_referral_id())
            if not BOT_USERNAME:
                get_bot_username()
            bot_user = BOT_USERNAME or os.getenv("BOT_USERNAME", "")
            if bot_user:
                # ensure we don't include @ in the link
                bot_user_clean = bot_user.lstrip("@")
                ref_link = f"https://t.me/{bot_user_clean}?start={refid}"
            else:
                ref_link = f"{PUBLIC_URL.rstrip('/')}/start?ref={refid}"

            share_text = f"Join the Warroom: {ref_link}"
            encoded = urllib.parse.quote_plus(share_text)

            keyboard = kb_inline([
                [btn("🔗 Open Link", url=ref_link)],
                [btn("📤 Share via Telegram", url=f"https://t.me/share/url?url=&text={encoded}")],
                [btn("🏠 Main Menu", cb="menu")]
            ])

            tg_send_message(cid, f"🔗 Your referral link:\n{ref_link}\n\nShare this with friends. When they subscribe, you'll earn commission.", reply_markup=keyboard, parse_mode="Markdown")
            return jsonify({"ok": True})

        # extend using commission - show plan choices but for extend action
        if payload == "extend":
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
        u = get_user(user_id)
        if u:
            referred_by = u[6]
            if referred_by:
                ref_user = get_user_by_referral_id(referred_by)
                if ref_user:
                    ref_user_id = ref_user[0]
                    # commission rate set to COMMISSION_RATE (25%)
                    commission_amt = round(float(plan_amount) * COMMISSION_RATE, 2)
                    if commission_amt > 0:
                        add_commission_to_user(ref_user_id, commission_amt)
                        increment_referrals(ref_user_id)
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
    # dashboard links to three sections
    return """
    <html><head><title>Admin Dashboard</title></head><body>
    <h2>Admin Dashboard</h2>
    <ul>
      <li><a href="/admin/payouts">Payout Requests</a></li>
      <li><a href="/admin/subscribers">Subscribed Users</a></li>
      <li><a href="/admin/referrals">Referral Tracker</a></li>
    </ul>
    <p><a href="/admin/logout">Logout</a></p>
    </body></html>
    """

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect("/admin")

# Payouts: view pending withdrawals, mark processed
@app.route("/admin/payouts", methods=["GET", "POST"])
def admin_payouts():
    if not session.get("admin"):
        return admin_login_page()
    if request.method == "POST":
        # mark payout processed
        wid = int(request.form.get("withdrawal_id", 0))
        if wid:
            mark_withdrawal_processed(wid)
    rows = qdb("SELECT w.id, w.user_id, w.amount, w.sol_address, w.status, w.created_at, u.username FROM withdrawals w LEFT JOIN users u ON u.user_id = w.user_id ORDER BY w.created_at DESC")
    html = "<h2>Payout Requests</h2><table border=1 cellpadding=4><tr><th>ID</th><th>User</th><th>Amount</th><th>SOL</th><th>Status</th><th>Created</th><th>Action</th></tr>"
    for r in rows:
        html += f"<tr><td>{r[0]}</td><td>{escape(str(r[6]) or r[1])} ({r[1]})</td><td>${r[2]:.2f}</td><td>{escape(r[3] or '')}</td><td>{r[4]}</td><td>{r[5]}</td>"
        if r[4] != "processed":
            html += f"<td><form method='post'><input type='hidden' name='withdrawal_id' value='{r[0]}'><button>Mark Processed</button></form></td>"
        else:
            html += "<td>—</td>"
        html += "</tr>"
    html += "</table><p><a href='/admin'>Back</a></p>"
    return html

# Subscribers: list subscribed users and allow admin to activate/extend
@app.route("/admin/subscribers", methods=["GET", "POST"])
def admin_subscribers():
    if not session.get("admin"):
        return admin_login_page()
    message = ""
    if request.method == "POST":
        # activation form: user_id and plan_key
        uid = int(request.form.get("user_id", 0))
        plan_key = request.form.get("plan_key", "")
        if uid and plan_key in PLANS:
            plan = PLANS[plan_key]
            update_subscription(uid, plan["label"], plan["days"])
            # notify user
            try:
                tg_send_message(uid, f"🔔 Admin has activated/extended your subscription: {plan['label']} ({plan['days']} days). Enjoy!", reply_markup=kb_inline([[btn("➡️ Join Now", url=WARROOM_LINK)]]))
            except Exception:
                logger.exception("Failed to send activation message")
            message = "Subscription updated and user notified."
    # list users (all) showing subscription_end and commission
    rows = qdb("SELECT user_id, username, subscription_plan, subscription_end, commission FROM users ORDER BY subscription_end DESC NULLS LAST")
    html = "<h2>Subscribed Users / All Users</h2>"
    if message:
        html += f"<p style='color:green'>{escape(message)}</p>"
    html += "<table border=1 cellpadding=4><tr><th>User ID</th><th>Username</th><th>Plan</th><th>Ends</th><th>Commission</th><th>Activate/Extend</th></tr>"
    for r in rows:
        html += f"<tr><td>{r[0]}</td><td>{escape(r[1] or '')}</td><td>{escape(r[2] or '')}</td><td>{r[3] or '—'}</td><td>${(r[4] or 0):.2f}</td>"
        # Activation form
        html += "<td><form method='post'>"
        html += f"<input type='hidden' name='user_id' value='{r[0]}'/>"
        html += "<select name='plan_key'>"
        for pk, p in PLANS.items():
            html += f"<option value='{pk}'>{p['label']} - ${p['amount']} ({p['days']}d)</option>"
        html += "</select> <button>Activate/Extend</button></form></td></tr>"
    html += "</table><p><a href='/admin'>Back</a></p>"
    return html

# Referral tracker: shows users and referral stats
@app.route("/admin/referrals")
def admin_referrals():
    if not session.get("admin"):
        return admin_login_page()
    rows = qdb("SELECT user_id, username, referrals, commission, referral_id FROM users ORDER BY commission DESC")
    html = "<h2>Referral Tracker</h2><table border=1 cellpadding=4><tr><th>User ID</th><th>Username</th><th>Referrals</th><th>Commission</th><th>Referral ID</th></tr>"
    for r in rows:
        html += f"<tr><td>{r[0]}</td><td>{escape(r[1] or '')}</td><td>{r[2]}</td><td>${(r[3] or 0):.2f}</td><td>{escape(r[4] or '')}</td></tr>"
    html += "</table><p><a href='/admin'>Back</a></p>"
    return html

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
