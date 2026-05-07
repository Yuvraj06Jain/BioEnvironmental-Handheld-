"""
Bio Environmental Monitoring System - Server
============================================

Install:
  pip install fastapi uvicorn motor pymongo pydantic python-multipart itsdangerous python-dotenv websockets

.env example:
  ADMINS={"admin":"password123","yuvraj":"securepass"}
  MONGO_URI=mongodb+srv://USERNAME:PASSWORD@cluster.mongodb.net/?appName=Handheld
  API_KEY=98063117
  SESSION_SECRET=some-long-random-string-change-this

Run:
  python main.py
"""

import json
import math
import os
import secrets
from datetime import datetime
from typing import Optional, Set

import uvicorn
from dotenv import load_dotenv
from fastapi import (
    FastAPI,
    Form,
    Request,
    Header,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

load_dotenv()

ADMINS = json.loads(os.getenv("ADMINS", "{}"))
MONGO_URI = os.getenv("MONGO_URI")
API_KEY = os.getenv("API_KEY")
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me-in-production")

if not ADMINS:
    raise RuntimeError("ADMINS not set in .env")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI not set in .env")
if not API_KEY:
    raise RuntimeError("API_KEY not set in .env")

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=86400)

client = AsyncIOMotorClient(MONGO_URI)
db = client["health_monitor"]
collection = db["readings"]


# ============================================================
# WebSocket Manager
# ============================================================

class ConnectionManager:
    def __init__(self):
        self.active: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def broadcast(self, message: dict):
        dead = set()
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                dead.add(ws)
        self.active -= dead


manager = ConnectionManager()


# ============================================================
# Models
# ============================================================

class SensorReading(BaseModel):
    timestamp: Optional[str] = None
    temperature: float
    humidity: float
    heart_rate: Optional[int] = None
    spo2: Optional[int] = None
    username: Optional[str] = None


class UsernameUpdate(BaseModel):
    received_at: str
    username: str


# ============================================================
# Auth
# ============================================================

def is_logged_in(request: Request) -> bool:
    return request.session.get("authenticated") is True


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    if is_logged_in(request):
        return RedirectResponse(url="/", status_code=303)

    error_html = ""
    if error:
        error_html = '<div class="error-msg">&#9888; Incorrect username or password.</div>'

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>BioEnv Monitor — Login</title>
  <style>
    body {{
      font-family: Arial, sans-serif;
      background: #eef2f7;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .card {{
      background: white;
      padding: 36px;
      border-radius: 14px;
      width: 360px;
      box-shadow: 0 4px 20px rgba(0,0,0,.12);
    }}
    h1 {{
      color: #1a4e8c;
      font-size: 1.2rem;
      margin-bottom: 6px;
    }}
    p {{
      color: #64748b;
      font-size: .85rem;
      margin-bottom: 22px;
    }}
    label {{
      display: block;
      margin-top: 14px;
      font-size: .8rem;
      font-weight: bold;
      color: #475569;
    }}
    input {{
      width: 100%;
      padding: 10px;
      margin-top: 5px;
      border: 1px solid #cbd5e1;
      border-radius: 7px;
    }}
    button {{
      width: 100%;
      margin-top: 22px;
      padding: 11px;
      background: #1a4e8c;
      color: white;
      border: none;
      border-radius: 7px;
      font-weight: bold;
      cursor: pointer;
    }}
    .error-msg {{
      background: #fee2e2;
      color: #991b1b;
      padding: 10px;
      border-radius: 6px;
      font-size: .85rem;
      margin-bottom: 12px;
    }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Bio Environmental Monitor</h1>
    <p>Secure dashboard login</p>
    {error_html}
    <form method="POST" action="/login">
      <label>Username</label>
      <input type="text" name="username" autocomplete="username" autofocus>
      <label>Password</label>
      <input type="password" name="password" autocomplete="current-password">
      <button type="submit">Sign In</button>
    </form>
  </div>
</body>
</html>""")


@app.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    stored_pass = ADMINS.get(username)

    if stored_pass and secrets.compare_digest(password, stored_pass):
        request.session["authenticated"] = True
        request.session["admin_name"] = username
        return RedirectResponse(url="/", status_code=303)

    return RedirectResponse(url="/login?error=1", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


# ============================================================
# Helper Functions
# ============================================================

def split_ts(ts: str):
    if not ts:
        return "-", "-"
    clean = ts.replace(" (server)", "").strip()
    if "T" in clean:
        parts = clean.split("T")
        return parts[0], parts[1][:8]
    if " " in clean:
        parts = clean.split(" ", 1)
        return parts[0], parts[1][:8]
    return clean, "-"


def make_badge(value, low, high, unit=""):
    if value is None:
        return '<span class="na">&#8212;</span>'
    try:
        v = float(value)
        cls = "ok" if low <= v <= high else "bad"
        return f'<span class="badge {cls}">{v:.1f}{unit}</span>'
    except Exception:
        return f'<span class="na">{value}</span>'


def estimate_wet_bulb_c(temp_c: float, rh: float) -> float:
    """
    Stull approximation for wet-bulb temperature.
    Used here for dashboard-level heat-risk estimation.
    """
    return (
        temp_c * math.atan(0.151977 * math.sqrt(rh + 8.313659))
        + math.atan(temp_c + rh)
        - math.atan(rh - 1.676331)
        + 0.00391838 * (rh ** 1.5) * math.atan(0.023101 * rh)
        - 4.686035
    )


def estimate_wbgt_c(temp_c: Optional[float], rh: Optional[float]) -> Optional[float]:
    if temp_c is None or rh is None:
        return None
    if rh < 0 or rh > 100:
        return None

    wet_bulb = estimate_wet_bulb_c(temp_c, rh)

    # Approximate shaded/indoor WBGT.
    # True WBGT also needs globe temperature and wind effects.
    return 0.7 * wet_bulb + 0.3 * temp_c


def heat_risk_from_wbgt(wbgt: Optional[float]) -> str:
    if wbgt is None:
        return "Unknown"
    if wbgt < 26:
        return "Normal"
    elif wbgt < 28:
        return "Caution"
    elif wbgt < 30:
        return "High Caution"
    elif wbgt < 32:
        return "Danger"
    else:
        return "Extreme Danger"


def wbgt_badge(value):
    if value is None:
        return '<span class="na">&#8212;</span>'

    risk = heat_risk_from_wbgt(value)
    cls = "ok" if risk in ["Normal", "Caution"] else "bad"
    return f'<span class="badge {cls}">{value:.1f} &deg;C<br><small>{risk}</small></span>'


def heart_context_message(hr: Optional[int], wbgt: Optional[float]) -> str:
    if hr is None:
        return "-"

    risk = heat_risk_from_wbgt(wbgt)

    if 60 <= hr <= 100:
        return "Normal HR"

    if hr > 100:
        if risk in ["Danger", "Extreme Danger", "High Caution"]:
            return "Elevated HR; heat stress possible"
        return "Elevated HR; monitor"

    if hr < 60:
        return "Low HR; monitor"

    return "-"


def context_badge(hr: Optional[int], wbgt: Optional[float]):
    msg = heart_context_message(hr, wbgt)

    if msg == "-":
        return '<span class="na">&#8212;</span>'

    bad = "Elevated" in msg or "Low" in msg
    cls = "bad" if bad else "ok"
    return f'<span class="badge {cls}">{msg}</span>'


# ============================================================
# API Routes
# ============================================================

@app.post("/data", status_code=201)
async def receive_data(reading: SensorReading, x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    doc = reading.dict()

    if not doc.get("timestamp") or doc["timestamp"] == "OFFLINE":
        doc["timestamp"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S") + " (server)"

    doc["received_at"] = datetime.now().isoformat()

    wbgt = estimate_wbgt_c(doc.get("temperature"), doc.get("humidity"))
    doc["wbgt"] = round(wbgt, 1) if wbgt is not None else None
    doc["heat_risk"] = heat_risk_from_wbgt(wbgt)

    await collection.insert_one(doc)
    print(f"[{doc['received_at']}] Saved: {doc}")

    date_s, time_s = split_ts(doc.get("timestamp", ""))
    hr_val = doc.get("heart_rate")
    sp_val = doc.get("spo2")
    wbgt_val = doc.get("wbgt")

    is_hr = hr_val is not None

    if is_hr:
        abnormal = (
            (hr_val is not None and (hr_val < 60 or hr_val > 100))
            or (sp_val is not None and sp_val < 95)
            or (doc["heat_risk"] in ["Danger", "Extreme Danger"])
        )

        row_cls = ' class="alert-row new-row"' if abnormal else ' class="new-row"'

        row_html = f"""<tr{row_cls} data-received-at="{doc['received_at']}">
          <td class="mono">{date_s}</td>
          <td class="mono">{time_s}</td>
          <td>{make_badge(hr_val, 60, 100, " bpm")}</td>
          <td>{make_badge(sp_val, 95, 100, "%")}</td>
          <td>{wbgt_badge(wbgt_val)}</td>
          <td>{context_badge(hr_val, wbgt_val)}</td>
          <td class="user-cell"><span class="no-user">&#8212;</span></td>
        </tr>"""

    else:
        row_html = f"""<tr class="new-row">
          <td class="mono">{date_s}</td>
          <td class="mono">{time_s}</td>
          <td>{make_badge(doc.get("temperature"), 15, 40, " &deg;C")}</td>
          <td>{make_badge(doc.get("humidity"), 20, 80, "%")}</td>
          <td>{wbgt_badge(wbgt_val)}</td>
        </tr>"""

    await manager.broadcast({
        "type": "hr" if is_hr else "env",
        "row_html": row_html,
        "received_at": doc["received_at"],
    })

    return {"status": "ok"}


@app.post("/set-username")
async def set_username(request: Request, update: UsernameUpdate):
    if not is_logged_in(request):
        raise HTTPException(status_code=401, detail="Not authenticated")

    result = await collection.update_one(
        {"received_at": update.received_at},
        {"$set": {"username": update.username}}
    )

    return {"status": "ok", "modified": result.modified_count}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    if not ws.session.get("authenticated"):
        await ws.close(code=1008)
        return

    await manager.connect(ws)

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)


@app.get("/health")
async def health():
    return {"status": "ok"}


# ============================================================
# Dashboard
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    admin_name = request.session.get("admin_name", "Admin")
    docs = await collection.find({}, {"_id": 0}).sort("received_at", -1).to_list(1000)

    env_docs = [d for d in docs if d.get("heart_rate") is None]
    hr_docs = [d for d in docs if d.get("heart_rate") is not None]

    pending = [{"received_at": d["received_at"]} for d in hr_docs if not d.get("username")]
    pending_json = json.dumps(pending)

    env_rows = ""
    for d in env_docs:
        date_s, time_s = split_ts(d.get("timestamp", ""))

        wbgt = d.get("wbgt")
        if wbgt is None:
            wbgt_calc = estimate_wbgt_c(d.get("temperature"), d.get("humidity"))
            wbgt = round(wbgt_calc, 1) if wbgt_calc is not None else None

        env_rows += f"""<tr>
          <td class="mono">{date_s}</td>
          <td class="mono">{time_s}</td>
          <td>{make_badge(d.get("temperature"), 15, 40, " &deg;C")}</td>
          <td>{make_badge(d.get("humidity"), 20, 80, "%")}</td>
          <td>{wbgt_badge(wbgt)}</td>
        </tr>"""

    if not env_rows:
        env_rows = "<tr id='env-empty'><td colspan='5' class='empty'>No environmental readings yet.</td></tr>"

    hr_rows = ""
    for d in hr_docs:
        date_s, time_s = split_ts(d.get("timestamp", ""))
        hr_val = d.get("heart_rate")
        sp_val = d.get("spo2")
        uname = d.get("username") or '<span class="no-user">&#8212;</span>'

        wbgt = d.get("wbgt")
        if wbgt is None:
            wbgt_calc = estimate_wbgt_c(d.get("temperature"), d.get("humidity"))
            wbgt = round(wbgt_calc, 1) if wbgt_calc is not None else None

        heat_risk = heat_risk_from_wbgt(wbgt)

        abnormal = (
            (hr_val is not None and (hr_val < 60 or hr_val > 100))
            or (sp_val is not None and sp_val < 95)
            or (heat_risk in ["Danger", "Extreme Danger"])
        )

        row_cls = ' class="alert-row"' if abnormal else ""

        hr_rows += f"""<tr{row_cls} data-received-at="{d['received_at']}">
          <td class="mono">{date_s}</td>
          <td class="mono">{time_s}</td>
          <td>{make_badge(hr_val, 60, 100, " bpm")}</td>
          <td>{make_badge(sp_val, 95, 100, "%")}</td>
          <td>{wbgt_badge(wbgt)}</td>
          <td>{context_badge(hr_val, wbgt)}</td>
          <td class="user-cell">{uname}</td>
        </tr>"""

    if not hr_rows:
        hr_rows = "<tr id='hr-empty'><td colspan='7' class='empty'>No heart rate readings yet.</td></tr>"

    total = len(docs)
    env_cnt = len(env_docs)
    hr_cnt = len(hr_docs)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>BioEnv Monitor</title>
  <style>
    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }}

    body {{
      font-family: Arial, sans-serif;
      background: #f0f4f8;
      color: #1e293b;
      padding-bottom: 60px;
    }}

    .topbar {{
      background: #1a4e8c;
      color: white;
      padding: 14px 28px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}

    .topbar-logo {{
      font-weight: bold;
      font-size: 1rem;
    }}

    .topbar-sub {{
      font-size: .72rem;
      opacity: .75;
      margin-top: 2px;
    }}

    .topbar-right {{
      display: flex;
      align-items: center;
      gap: 12px;
      font-size: .85rem;
    }}

    .btn-nav {{
      background: rgba(255,255,255,.15);
      color: white;
      padding: 6px 12px;
      border-radius: 6px;
      text-decoration: none;
      border: 1px solid rgba(255,255,255,.25);
    }}

    .live-pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: rgba(255,255,255,.15);
      padding: 5px 12px;
      border-radius: 18px;
    }}

    .live-dot {{
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: #4ade80;
      box-shadow: 0 0 6px #4ade80;
    }}

    .live-dot.disconnected {{
      background: #f87171;
      box-shadow: 0 0 6px #f87171;
    }}

    .page {{
      padding: 28px 20px;
    }}

    .stats {{
      max-width: 1200px;
      margin: 0 auto 22px;
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
    }}

    .stat {{
      background: white;
      border: 1px solid #dde3ec;
      border-radius: 8px;
      padding: 10px 20px;
      font-size: .8rem;
      color: #64748b;
    }}

    .stat strong {{
      display: block;
      color: #1e293b;
      font-size: 1.25rem;
    }}

    .tabs {{
      max-width: 1200px;
      margin: 0 auto;
      border-bottom: 2px solid #dde3ec;
    }}

    .tab-btn {{
      padding: 11px 24px;
      border: none;
      background: none;
      font-weight: bold;
      cursor: pointer;
      color: #64748b;
      border-bottom: 3px solid transparent;
      margin-bottom: -2px;
    }}

    .tab-btn.active {{
      color: #1a4e8c;
      border-bottom-color: #1a4e8c;
    }}

    .hr-tab.active {{
      color: #0e7490;
      border-bottom-color: #0e7490;
    }}

    .panel {{
      display: none;
    }}

    .panel.active {{
      display: block;
    }}

    .section-header {{
      max-width: 1200px;
      margin: 20px auto 14px;
    }}

    .section-title {{
      font-weight: bold;
      font-size: 1rem;
    }}

    .section-sub {{
      color: #64748b;
      font-size: .78rem;
      margin-top: 3px;
    }}

    .wrap {{
      max-width: 1200px;
      margin: 0 auto;
      background: white;
      border: 1px solid #dde3ec;
      border-radius: 10px;
      overflow: hidden;
      box-shadow: 0 1px 4px rgba(0,0,0,.06);
    }}

    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: .86rem;
    }}

    thead tr {{
      background: #1a4e8c;
      color: white;
    }}

    .hr-panel thead tr {{
      background: #0e7490;
    }}

    th, td {{
      padding: 10px 14px;
      text-align: left;
      border-bottom: 1px solid #dde3ec;
      vertical-align: middle;
    }}

    th {{
      font-size: .72rem;
      text-transform: uppercase;
      letter-spacing: .05em;
    }}

    .mono {{
      font-family: monospace;
      color: #64748b;
      font-size: .78rem;
    }}

    .badge {{
      display: inline-block;
      padding: 4px 9px;
      border-radius: 5px;
      font-weight: bold;
      font-size: .8rem;
      line-height: 1.25;
    }}

    .badge.ok {{
      background: #dcfce7;
      color: #166534;
    }}

    .badge.bad {{
      background: #fee2e2;
      color: #991b1b;
    }}

    .alert-row {{
      background: #fff8f8;
    }}

    .new-row {{
      animation: flash 1.8s ease forwards;
    }}

    @keyframes flash {{
      0% {{ background: #d1fae5; }}
      100% {{ background: transparent; }}
    }}

    .empty {{
      text-align: center;
      padding: 34px;
      color: #94a3b8;
    }}

    .na, .no-user {{
      color: #cbd5e1;
    }}

    .legend {{
      max-width: 1200px;
      margin: 12px auto 0;
      font-size: .76rem;
      color: #64748b;
    }}

    #modal-overlay {{
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(15,23,42,.45);
      z-index: 100;
      align-items: center;
      justify-content: center;
    }}

    #modal-overlay.show {{
      display: flex;
    }}

    #modal {{
      background: white;
      padding: 30px;
      border-radius: 12px;
      max-width: 420px;
      width: 100%;
    }}

    #username-input {{
      width: 100%;
      margin-top: 12px;
      padding: 10px;
      border: 1px solid #cbd5e1;
      border-radius: 7px;
    }}

    .modal-actions {{
      margin-top: 16px;
      display: flex;
      justify-content: flex-end;
      gap: 10px;
    }}

    .btn-primary {{
      background: #0e7490;
      color: white;
      border: none;
      padding: 8px 18px;
      border-radius: 6px;
      cursor: pointer;
    }}

    .btn-skip {{
      background: white;
      color: #64748b;
      border: 1px solid #cbd5e1;
      padding: 8px 18px;
      border-radius: 6px;
      cursor: pointer;
    }}
  </style>
</head>

<body>
  <div class="topbar">
    <div>
      <div class="topbar-logo">&#127807; Bio Environmental Monitor</div>
      <div class="topbar-sub">ESP32 &middot; DHT11 &middot; MAX30102 &middot; Heat Context</div>
    </div>
    <div class="topbar-right">
      <div class="live-pill">
        <div class="live-dot" id="live-dot"></div>
        <span id="live-label">Live</span>
      </div>
      <span>Signed in as <strong>{admin_name}</strong></span>
      <a class="btn-nav" href="/logout">Sign Out</a>
    </div>
  </div>

  <div id="modal-overlay">
    <div id="modal">
      <h2>Heart Rate Reading Received</h2>
      <p>Enter the name of the person who performed this scan.</p>
      <div id="modal-time"></div>
      <input type="text" id="username-input" placeholder="e.g. Yuvraj" maxlength="40">
      <div class="modal-actions">
        <button class="btn-skip" onclick="skipCurrent()">Skip</button>
        <button class="btn-primary" onclick="saveCurrent()">Save Name</button>
      </div>
    </div>
  </div>

  <div class="page">
    <div class="stats">
      <div class="stat"><strong id="stat-total">{total}</strong>Total Readings</div>
      <div class="stat"><strong id="stat-env">{env_cnt}</strong>Environmental</div>
      <div class="stat"><strong id="stat-hr">{hr_cnt}</strong>HR Scans</div>
    </div>

    <div class="tabs">
      <button class="tab-btn active" id="tab-env" onclick="showTab('env')">Environmental</button>
      <button class="tab-btn hr-tab" id="tab-hr" onclick="showTab('hr')">Heart Rate</button>
    </div>

    <div id="panel-env" class="panel active">
      <div class="section-header">
        <div class="section-title">Temperature, Humidity & Heat Risk</div>
        <div class="section-sub">DHT11 readings used to estimate environmental heat stress</div>
      </div>
      <div class="wrap">
        <table>
          <thead>
            <tr>
              <th>Date</th>
              <th>Time</th>
              <th>Temperature</th>
              <th>Humidity</th>
              <th>Heat Risk</th>
            </tr>
          </thead>
          <tbody id="env-tbody">{env_rows}</tbody>
        </table>
      </div>
    </div>

    <div id="panel-hr" class="panel hr-panel">
      <div class="section-header">
        <div class="section-title">Heart Rate, SpO&#8322; & Environmental Context</div>
        <div class="section-sub">Heart-rate danger is interpreted with DHT11-based heat-risk context</div>
      </div>
      <div class="wrap">
        <table>
          <thead>
            <tr>
              <th>Date</th>
              <th>Time</th>
              <th>Heart Rate</th>
              <th>SpO&#8322;</th>
              <th>Heat Risk</th>
              <th>HR Context</th>
              <th>User</th>
            </tr>
          </thead>
          <tbody id="hr-tbody">{hr_rows}</tbody>
        </table>
      </div>
      <div class="legend">
        HR: 60–100 bpm normal range. SpO&#8322; below 95% is marked abnormal. Heat-risk is estimated from DHT11 readings.
      </div>
    </div>
  </div>

  <script>
    function showTab(name) {{
      document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.getElementById('panel-' + name).classList.add('active');
      document.getElementById('tab-' + name).classList.add('active');
      location.hash = name === 'hr' ? 'hr' : '';
    }}

    if (location.hash === '#hr') showTab('hr');

    let totalCount = {total};
    let envCount = {env_cnt};
    let hrCount = {hr_cnt};

    function updateStats(type) {{
      totalCount++;
      if (type === 'hr') hrCount++;
      else envCount++;

      document.getElementById('stat-total').textContent = totalCount;
      document.getElementById('stat-env').textContent = envCount;
      document.getElementById('stat-hr').textContent = hrCount;
    }}

    const pending = {pending_json};
    let currentIdx = 0;

    const dot = document.getElementById('live-dot');
    const label = document.getElementById('live-label');

    function connectWS() {{
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const ws = new WebSocket(`${{proto}}://${{location.host}}/ws`);

      ws.onopen = () => {{
        dot.classList.remove('disconnected');
        label.textContent = 'Live';
      }};

      ws.onmessage = (event) => {{
        const msg = JSON.parse(event.data);
        const tbody = document.getElementById(msg.type === 'hr' ? 'hr-tbody' : 'env-tbody');

        const emptyRow = tbody.querySelector('tr[id$="-empty"]');
        if (emptyRow) emptyRow.remove();

        tbody.insertAdjacentHTML('afterbegin', msg.row_html);
        updateStats(msg.type);

        if (msg.type === 'hr') {{
          pending.push({{ received_at: msg.received_at }});
          showTab('hr');
          setTimeout(openNext, 400);
        }}
      }};

      ws.onclose = () => {{
        dot.classList.add('disconnected');
        label.textContent = 'Reconnecting...';
        setTimeout(connectWS, 3000);
      }};

      ws.onerror = () => ws.close();
    }}

    connectWS();

    function openNext() {{
      if (currentIdx >= pending.length) return;
      const item = pending[currentIdx];
      document.getElementById('modal-time').textContent = item.received_at;
      document.getElementById('username-input').value = '';
      document.getElementById('modal-overlay').classList.add('show');
      setTimeout(() => document.getElementById('username-input').focus(), 100);
    }}

    function closeModal() {{
      document.getElementById('modal-overlay').classList.remove('show');
    }}

    function skipCurrent() {{
      currentIdx++;
      closeModal();
      if (currentIdx < pending.length) setTimeout(openNext, 300);
    }}

    async function saveCurrent() {{
      const name = document.getElementById('username-input').value.trim();
      if (!name) return;

      const item = pending[currentIdx];

      try {{
        await fetch('/set-username', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ received_at: item.received_at, username: name }})
        }});

        const targetRow = document.querySelector(`#hr-tbody tr[data-received-at="${{item.received_at}}"]`);
        if (targetRow) {{
          targetRow.querySelector('td.user-cell').innerHTML = name;
        }}

        currentIdx++;
        closeModal();

        if (currentIdx < pending.length) setTimeout(openNext, 300);
      }} catch (e) {{
        alert('Failed to save name.');
      }}
    }}

    document.getElementById('username-input').addEventListener('keydown', e => {{
      if (e.key === 'Enter') saveCurrent();
      if (e.key === 'Escape') skipCurrent();
    }});

    if (pending.length > 0) {{
      showTab('hr');
      setTimeout(openNext, 600);
    }}
  </script>
</body>
</html>"""

    return HTMLResponse(html)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)