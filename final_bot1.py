#!/usr/bin/env python3
# Compact final_bot_admin.py - webhook mode
import os, sqlite3, json, logging, requests, time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, redirect, session, escape
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

# Config from env
BOT_TOKEN = os.getenv("BOT_TOKEN")
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")
NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET", "")
PUBLIC_URL = os.getenv("PUBLIC_URL")
WARROOM_LINK = os.getenv("WARROOM_LINK", "https://t.me/+IM_nIsf78JI4NzI1")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME","admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
DATABASE = os.getenv("DATABASE","db.sqlite3")
PORT = int(os.getenv("PORT","5000"))

if not BOT_TOKEN or not NOWPAYMENTS_API_KEY or not PUBLIC_URL or not ADMIN_PASSWORD:
    raise SystemExit("Set BOT_TOKEN, NOWPAYMENTS_API_KEY, PUBLIC_URL, ADMIN_PASSWORD in env")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET","change-me")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")
bot = Bot(BOT_TOKEN)

PLANS = {
  "sub_10": {"label":"Weekly","amount":10,"days":7},
  "sub_20": {"label":"Monthly","amount":20,"days":30},
  "sub_50": {"label":"3-Months","amount":50,"days":90},
}
NOW_URL = "https://api.nowpayments.io/v1/invoice"

def init_db():
    c = sqlite3.connect(DATABASE)
    cur = c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, subscription_plan TEXT, subscription_end TEXT,
        referrals INTEGER DEFAULT 0, commission REAL DEFAULT 0, referred_by TEXT, referral_id TEXT, last_notified TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, user_id INTEGER, plan_key TEXT,
        invoice_url TEXT, status TEXT, amount REAL, created_at TEXT
    )""")
    c.commit(); c.close()

def qdb(q, args=(), one=False):
    c = sqlite3.connect(DATABASE); cur = c.cursor(); cur.execute(q, args); r = cur.fetchall(); c.commit(); c.close()
    return (r[0] if r else None) if one else r

def add_user(uid, username, referred_by=None):
    refid = f"ref{uid}"
    qdb("INSERT OR IGNORE INTO users (user_id, username, referred_by, referral_id) VALUES (?,?,?,?)", (uid, username, referred_by, refid))
    qdb("UPDATE users SET referral_id=? WHERE user_id=? AND (referral_id IS NULL OR referral_id='')", (refid, uid))
    return refid

def get_user(uid): return qdb("SELECT * FROM users WHERE user_id=?", (uid,), one=True)
def is_subscribed(uid):
    r = get_user(uid); 
    if not r or not r[3]: return False
    try:
        return datetime.strptime(r[3], "%Y-%m-%d %H:%M:%S") > datetime.utcnow()
    except: return False
def update_subscription(uid, plan_label, days):
    end = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    qdb("UPDATE users SET subscription_plan=?, subscription_end=? WHERE user_id=?", (plan_label, end, uid))
def save_invoice(order_id, uid, plan_key, invoice_url, amount, status="pending"):
    qdb("INSERT INTO invoices (order_id,user_id,plan_key,invoice_url,status,amount,created_at) VALUES (?,?,?,?,?,?,?)",
         (order_id, uid, plan_key, invoice_url or "", status, amount, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))
def find_invoice(order_id): return qdb("SELECT * FROM invoices WHERE order_id=?", (order_id,), one=True)
def update_invoice_status(order_id, status): qdb("UPDATE invoices SET status=? WHERE order_id=?", (status, order_id))

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
    try: bot.send_message(chat_id=chat_id, text="🚀 Welcome! Choose an option:", reply_markup=InlineKeyboardMarkup(kb))
    except: logger.exception("send menu")

def generate_payment_link(user_id, plan_key):
    if plan_key not in PLANS: return None, "invalid plan"
    plan = PLANS[plan_key]; amount = plan["amount"]; days = plan["days"]
    order_id = f"{user_id}_{int(time.time())}"
    payload = {"price_amount": amount, "price_currency":"usd", "order_id":order_id,
               "order_description": f"{plan['label']} subscription for user {user_id}",
               "ipn_callback_url": f"{PUBLIC_URL.rstrip('/')}/ipn?user_id={user_id}&plan={plan_key}&days={days}",
               "success_url": f"{PUBLIC_URL.rstrip('/')}/success?order_id={order_id}&user_id={user_id}&plan={plan_key}",
               "cancel_url": f"{PUBLIC_URL.rstrip('/')}/cancel?order_id={order_id}&user_id={user_id}"}
    headers = {"x-api-key": NOWPAYMENTS_API_KEY, "Content-Type":"application/json"}
    try:
        r = requests.post(NOW_URL, headers=headers, json=payload, timeout=20); d = r.json()
        invoice_url = d.get("invoice_url") or d.get("payment_url") or d.get("url")
        save_invoice(order_id, user_id, plan_key, invoice_url or "", amount, status=d.get("status","pending"))
        return invoice_url, None if invoice_url else (None, d.get("error","no url"))
    except Exception as e:
        logger.exception("create invoice")
        return None, str(e)

@app.route("/telegram_webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json(force=True)
    if "message" in data:
        m = data["message"]; text = m.get("text",""); cid = m["chat"]["id"]; uname = m.get("from",{}).get("username","")
        if text and text.startswith("/start"):
            parts = text.split(); ref = parts[1] if len(parts)>1 and parts[1].startswith("ref") else None
            add_user(cid, uname, ref); send_main_menu(cid); return jsonify({"ok":True})
        add_user(cid, uname); send_main_menu(cid); return jsonify({"ok":True})
    if "callback_query" in data:
        cq = data["callback_query"]; cid = cq["from"]["id"]; payload = cq.get("data","")
        try: bot.answer_callback_query(callback_query_id=cq.get("id"))
        except: pass
        if payload=="warroom":
            if is_subscribed(cid):
                bot.send_message(chat_id=cid, text="🎟️ You are already subscribed. Use Main Menu to extend.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]))
            else:
                kb = [[InlineKeyboardButton("💵 $10 / week", callback_data="sub_10")],
                      [InlineKeyboardButton("💳 $20 / month", callback_data="sub_20")],
                      [InlineKeyboardButton("💎 $50 / 3 months", callback_data="sub_50")],
                      [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]
                bot.send_message(chat_id=cid, text="Choose a plan:", reply_markup=InlineKeyboardMarkup(kb))
            return jsonify({"ok":True})
        if payload.startswith("sub_"):
            link, err = generate_payment_link(cid, payload)
            if link: bot.send_message(chat_id=cid, text=f"Pay here:\n{link}")
            else: bot.send_message(chat_id=cid, text=f"Error: {err}")
            return jsonify({"ok":True})
        if payload=="main_menu": send_main_menu(cid); return jsonify({"ok":True})
        if payload=="earn":
            if is_subscribed(cid):
                u = get_user(cid); refid = (u[7] if u else f"ref{cid}"); comm = (u[5] if u else 0.0)
                bot.send_message(chat_id=cid, text=f"💰 Your referral id: `{refid}`\nCommission: {comm} USD", parse_mode="Markdown")
            else:
                bot.send_message(chat_id=cid, text="🔒 Earn is for subscribed users. Please subscribe.")
            return jsonify({"ok":True})
    return jsonify({"ok":True})

@app.route("/ipn", methods=["POST"])
def ipn():
    hdr = request.headers; body = request.get_json(force=True, silent=True) or {}
    header_secret = hdr.get("x-nowpayments-ipn-secret") or hdr.get("x-nowpayments-signature")
    if NOWPAYMENTS_IPN_SECRET and header_secret and header_secret!=NOWPAYMENTS_IPN_SECRET:
        logger.warning("IPN secret mismatch"); return jsonify({"error":"invalid"}),403
    status = body.get("payment_status") or body.get("status") or body.get("invoice_status"); order_id = body.get("order_id") or body.get("id")
    user_id_q = request.args.get("user_id")
    if not user_id_q and order_id:
        inv = find_invoice(order_id); user_id_q = str(inv[2]) if inv else None
    if str(status).lower() in ("finished","paid","successful") and user_id_q:
        try:
            uid = int(user_id_q); plan = request.args.get("plan"); days = int(request.args.get("days") or 0)
            label = PLANS.get(plan,{}).get("label", plan or "Membership"); d = PLANS.get(plan,{}).get("days", days or 0)
            update_subscription(uid, label, d); update_invoice_status(order_id, "paid")
            bot.send_message(chat_id=uid, text=f"💥 Hey — upcoming billionaire! Your {label} membership is active.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Join Now", url=WARROOM_LINK)]]))
        except Exception:
            logger.exception("activate ipn")
        return jsonify({"ok":True})
    if order_id: update_invoice_status(order_id, status or "pending")
    return jsonify({"ok":True})

@app.route("/success")
def success():
    order_id = request.args.get("order_id"); user_id_q = request.args.get("user_id"); plan = request.args.get("plan")
    if not order_id: return "Missing order_id", 400
    status = None; headers = {"x-api-key": NOWPAYMENTS_API_KEY}
    try:
        r = requests.get(f"https://api.nowpayments.io/v1/invoice/{order_id}", headers=headers, timeout=10)
        if r.status_code==200: status = r.json().get("status")
    except: pass
    inv = find_invoice(order_id)
    if status and str(status).lower() in ("finished","paid","successful"):
        uid = int(user_id_q) if user_id_q else (inv[2] if inv else None)
        if uid:
            label = PLANS.get(plan,{}).get("label", plan or "Membership"); days = PLANS.get(plan,{}).get("days",0)
            update_subscription(uid, label, days); update_invoice_status(order_id, "paid")
            bot.send_message(chat_id=uid, text=f"💥 Hey — upcoming billionaire! Your {label} membership is active.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Join Now", url=WARROOM_LINK)]]))
        return redirect(WARROOM_LINK)
    if inv: update_invoice_status(order_id, "cancelled")
    uid = int(user_id_q) if user_id_q else (inv[2] if inv else None)
    if uid: bot.send_message(chat_id=uid, text="❌ Payment cancelled or timed out. Use Main Menu to try again.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]))
    return "<html><body><h2>Payment not completed</h2><p>Check your Telegram.</p></body></html>",200

@app.route("/cancel")
def cancel():
    order_id = request.args.get("order_id"); user_id_q = request.args.get("user_id")
    if order_id: update_invoice_status(order_id, "cancelled")
    inv = find_invoice(order_id) if order_id else None
    uid = int(user_id_q) if user_id_q else (inv[2] if inv else None)
    if uid: bot.send_message(chat_id=uid, text="❌ Payment cancelled. Use Main Menu to try again.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]))
    return "<html><body><h2>Payment cancelled</h2></body></html>",200

@app.route("/admin", methods=["GET","POST"])
def admin():
    if request.method=="GET":
        if session.get("admin"): return "<html><body><h2>Admin</h2><a href=/logout>Logout</a></body></html>"
        return "<form method=post><input name=username placeholder=admin><input name=password type=password placeholder=password><button>Login</button></form>"
    if request.form.get("username")==ADMIN_USERNAME and request.form.get("password")==ADMIN_PASSWORD:
        session["admin"]=True; return redirect("/admin")
    return "invalid",403

@app.route("/logout")
def logout():
    session.pop("admin", None); return redirect("/admin")

if __name__ == "__main__":
    init_db(); app.run(host="0.0.0.0", port=PORT)
