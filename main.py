"""
ReCred Backend - Simple Flask server for Railway
=====================================================
Deploy to Railway:
  1. Push these 4 files to a GitHub repo (main.py, requirements.txt,
     runtime.txt, Procfile)
  2. Railway -> New Project -> Deploy from GitHub repo -> pick repo
  3. Wait ~2 min for build
  4. Settings -> Networking -> Generate Domain
  5. Test: visit https://YOUR.up.railway.app/  -> should say "ReCred OK"
  6. Copy that URL into recred_pi.py as SERVER_URL

Endpoints:
  GET  /                -> health check
  GET  /health          -> health check (for Railway probe)
  POST /api/session     -> create session, body: {bottles, points}
                           returns: {token, url}
  GET  /r/<token>       -> redemption page (user scans QR to get here)
  POST /r/<token>/claim -> claim an offer
"""

import os
import sqlite3
import secrets
import threading
import time
from flask import Flask, request, jsonify, render_template_string, abort, redirect
from werkzeug.exceptions import HTTPException

app = Flask(__name__)

# Use /tmp on Railway unless DB_PATH or a Railway volume mount is provided.
# Local Windows runs use a file next to this app because C:\tmp is often locked.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if os.environ.get("DB_PATH"):
    DB_PATH = os.environ["DB_PATH"]
elif os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"):
    DB_PATH = os.path.join(os.environ["RAILWAY_VOLUME_MOUNT_PATH"], "recred.db")
elif os.name == "nt":
    DB_PATH = os.path.join(BASE_DIR, "recred.db")
else:
    DB_PATH = "/tmp/recred.db"
SESSION_TTL = 24 * 3600   # 24 hours
_db_initialized = False
_db_init_lock = threading.Lock()

# ===================== DATABASE =====================
def ensure_db_dir():
    if DB_PATH == ":memory:":
        return
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(db_dir, exist_ok=True)

def get_db():
    ensure_db_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    global _db_initialized
    if _db_initialized:
        return

    with _db_init_lock:
        if _db_initialized:
            return

        with get_db() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS sessions(
                token TEXT PRIMARY KEY,
                bottles INTEGER NOT NULL,
                points INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                claimed_offer TEXT
            )""")
            c.commit()
        _db_initialized = True
        app.logger.info("[DB] initialized at %s", DB_PATH)

@app.before_request
def initialize_database():
    init_db()

# ===================== OFFERS =====================
OFFERS = [
    {"id": "ola",        "brand": "Ola",        "title": "Rs.50 off next ride",     "cost": 50,  "code": "RECRED50"},
    {"id": "uber",       "brand": "Uber",       "title": "Rs.50 off next ride",     "cost": 50,  "code": "UBER50RC"},
    {"id": "rapido",     "brand": "Rapido",     "title": "Rs.20 off any ride",      "cost": 20,  "code": "RAPIDO20"},
    {"id": "zomato",     "brand": "Zomato",     "title": "Rs.75 off orders Rs.199+","cost": 75,  "code": "ZOMATO75"},
    {"id": "burgerking", "brand": "Burger King","title": "Free Whopper with meal",  "cost": 100, "code": "BKWHOP"},
    {"id": "pvr",        "brand": "PVR",        "title": "Rs.100 off movie tickets","cost": 100, "code": "PVR100"},
]

# ===================== TEMPLATE =====================
REDEEM_PAGE = """
<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ReCred Rewards</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,system-ui,Segoe UI,Roboto,sans-serif;background:#0a0a0a;color:#fff;padding:20px;min-height:100vh}
  .wrap{max-width:520px;margin:0 auto}
  .logo{font-size:34px;font-weight:800;margin:12px 0 24px}
  .logo .a{color:#54b91d}.logo .b{color:#3dd9fd}
  .card{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:16px;padding:22px;margin-bottom:16px}
  .stat{display:flex;justify-content:space-between;align-items:center;padding:12px 0;border-bottom:1px solid #2a2a2a}
  .stat:last-child{border:none}
  .stat .l{color:#888;font-size:14px}
  .stat .v{font-size:28px;font-weight:700}
  .stat .v.g{color:#54b91d}.stat .v.y{color:#3dd9fd}
  h2{font-size:17px;margin:20px 0 10px;color:#ccc}
  .offer{background:#151515;border:1px solid #2a2a2a;border-radius:12px;padding:14px;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center;gap:12px}
  .offer.locked{opacity:.4}
  .offer.claimed{border-color:#54b91d;background:#0f1f0a}
  .offer .brand{font-weight:700;font-size:16px}
  .offer .title{font-size:13px;color:#aaa;margin-top:2px}
  .offer .cost{font-size:12px;color:#3dd9fd;margin-top:4px}
  .btn{background:#54b91d;color:#000;border:none;padding:10px 18px;border-radius:8px;font-weight:700;cursor:pointer;font-size:14px;white-space:nowrap}
  .btn:disabled{background:#333;color:#666;cursor:not-allowed}
  .code{background:#000;color:#3dd9fd;padding:14px;border-radius:8px;font-family:monospace;font-size:22px;text-align:center;letter-spacing:3px;margin-top:10px;font-weight:700}
  .expired{text-align:center;padding:40px 20px;color:#888}
  .done{text-align:center;padding:14px;color:#54b91d;font-weight:700;font-size:16px}
</style></head><body>
<div class="wrap">
  <div class="logo"><span class="a">Re</span><span class="b">Cred</span></div>

  {% if expired %}
    <div class="card expired">
      <h2 style="color:#ff6464">Session expired</h2>
      <p style="margin-top:12px">Recycle more bottles to get a fresh code.</p>
    </div>
  {% else %}
    <div class="card">
      <div class="stat"><span class="l">Bottles recycled</span><span class="v g">{{ bottles }}</span></div>
      <div class="stat"><span class="l">Points earned</span><span class="v y">{{ points }}</span></div>
    </div>

    {% if claimed %}
      <div class="card">
        <div class="done">&#10003; Offer claimed</div>
        <div style="text-align:center;color:#aaa;margin-top:8px">{{ claimed.brand }} &mdash; {{ claimed.title }}</div>
        <div class="code">{{ claimed.code }}</div>
        <p style="text-align:center;color:#666;font-size:12px;margin-top:12px">
          Use this code in the {{ claimed.brand }} app at checkout.
        </p>
      </div>
    {% else %}
      <h2>Redeem your points</h2>
      {% for o in offers %}
        <div class="offer {% if o.cost > points %}locked{% endif %}">
          <div>
            <div class="brand">{{ o.brand }}</div>
            <div class="title">{{ o.title }}</div>
            <div class="cost">{{ o.cost }} pts</div>
          </div>
          <form method="POST" action="/r/{{ token }}/claim" style="margin:0">
            <input type="hidden" name="offer" value="{{ o.id }}">
            <button class="btn" {% if o.cost > points %}disabled{% endif %}>
              {% if o.cost > points %}Locked{% else %}Claim{% endif %}
            </button>
          </form>
        </div>
      {% endfor %}
      <p style="text-align:center;color:#555;font-size:11px;margin-top:18px">
        One offer per session &bull; Session expires in 24h
      </p>
    {% endif %}
  {% endif %}
</div>
</body></html>
"""

# ===================== ROUTES =====================
@app.route("/")
def home():
    return "ReCred OK", 200

@app.route("/health")
def health():
    return "OK", 200

@app.route("/api/session", methods=["POST"])
def create_session():
    data = request.get_json(silent=True) or {}
    try:
        bottles = int(data.get("bottles", 0))
        points  = int(data.get("points",  0))
    except (TypeError, ValueError):
        return jsonify(error="bad input"), 400
    if bottles < 0 or points < 0:
        return jsonify(error="negative values"), 400

    token = secrets.token_urlsafe(8)
    try:
        with get_db() as c:
            c.execute(
                "INSERT INTO sessions(token,bottles,points,created_at) VALUES(?,?,?,?)",
                (token, bottles, points, int(time.time()))
            )
            c.commit()
    except Exception as e:
        return jsonify(error=f"db error: {e}"), 500

    base = request.host_url.rstrip("/")
    return jsonify(token=token, url=f"{base}/r/{token}")

@app.route("/r/<token>")
def redeem(token):
    try:
        with get_db() as c:
            row = c.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
    except HTTPException:
        raise
    except Exception as e:
        return f"DB error: {e}", 500

    if not row:
        abort(404)

    if int(time.time()) - row["created_at"] > SESSION_TTL:
        return render_template_string(REDEEM_PAGE, expired=True,
                                      token=token, bottles=0, points=0,
                                      offers=[], claimed=None)

    claimed = None
    if row["claimed_offer"]:
        claimed = next((o for o in OFFERS if o["id"] == row["claimed_offer"]), None)

    return render_template_string(REDEEM_PAGE,
                                  expired=False,
                                  token=token,
                                  bottles=row["bottles"],
                                  points=row["points"],
                                  offers=OFFERS,
                                  claimed=claimed)

@app.route("/r/<token>/claim", methods=["POST"])
def claim(token):
    offer_id = request.form.get("offer", "")
    offer = next((o for o in OFFERS if o["id"] == offer_id), None)
    if not offer:
        abort(400)
    try:
        with get_db() as c:
            row = c.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
            if not row:
                abort(404)
            if row["claimed_offer"]:
                return redirect(f"/r/{token}")
            if row["points"] < offer["cost"]:
                abort(403)
            c.execute("UPDATE sessions SET claimed_offer=? WHERE token=?",
                      (offer_id, token))
            c.commit()
    except HTTPException:
        raise
    except Exception as e:
        return f"DB error: {e}", 500
    return redirect(f"/r/{token}")

# Railway / local dev entry point
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
