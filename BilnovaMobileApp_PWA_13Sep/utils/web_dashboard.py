"""
Bilnova AI — Local Web Dashboard (Mobile View)
================================================
NEW 13-Sep-2026 (Rakesh — confirmed requested feature: "mobile pe bhi
dekhna chahiye Sale/Stock/Outstanding"). Ye ek CHHOTA local web server
hai — internet pe host NAHI hota, sirf tumhare apne WiFi/LAN [jaisa
7-PC server setup already hai] pe chalta hai. Koi bhi phone jo isi
WiFi se juda ho, browser mein http://<is-PC-ka-IP>:<port> khol ke
dekh sakta hai — read-only, kuch edit nahi kar sakta.

Security: sirf apne LAN tak seedhe expose hota hai [internet router
port-forward na karo, warna DUNIYA se access ho jayega — YE MAT
KARNA]. Ek chhota PIN-gate bhi hai [Settings mein "web_dashboard_pin"]
taaki isi WiFi pe koi aur bhi seedhe URL se data na dekh sake.

DB ko READ-ONLY mode mein kholte hain [sqlite URI mode=ro] — taaki
kabhi bhi is server ki wajah se main app ka koi lock/conflict na ho.
"""
import json
import logging
import os
import socket
import sqlite3
import threading
from datetime import datetime, date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

_server = None
_server_thread = None
_db_path = None
_pin = None


def get_local_ip() -> str:
    """Is PC ka LAN IP nikalta hai [jaise 192.168.1.5] — internet
    connect na bhi ho tab bhi kaam karta hai [socket trick, koi
    real packet nahi bhejta]."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def _ro_connect():
    """Read-only connection — WRITE query yahan se kabhi possible nahi
    [SQLite khud block karega], is server ki wajah se DB corrupt/lock
    hone ka risk zero hai."""
    uri = f"file:{_db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


class _RODb:
    """Chhota wrapper — sqlite3 raw connection ko DatabaseManager jaisa
    interface deta hai [.execute/.fetchone/.fetchall], taaki
    services/ledger_service.py ke functions [jo db.fetchall() jaisi
    calls karte hain] isi read-only connection ke saath bhi kaam karein
    bina unhe chhue. FIX 13-Sep-2026 (khud test karte waqt pakda):
    pehle raw sqlite3.Connection seedha pass kar diya tha — usme
    .fetchall() method hai hi nahi, is wajah se get_trial_balance()
    andar se crash hoke Outstanding hamesha chupke se ₹0 dikhata
    [try/except ne error nigal liya tha]."""
    def __init__(self, conn):
        self.conn = conn
    def execute(self, q, params=()):
        return self.conn.execute(q, params)
    def fetchone(self, q, params=()):
        return self.conn.execute(q, params).fetchone()
    def fetchall(self, q, params=()):
        return self.conn.execute(q, params).fetchall()


def _get_summary():
    today = date.today().strftime("%Y-%m-%d")
    conn = _ro_connect()
    try:
        def one(q, params=()):
            r = conn.execute(q, params).fetchone()
            return float(r[0]) if r and r[0] is not None else 0.0

        sale = one("SELECT COALESCE(SUM(total_amount),0) FROM vouchers "
                    "WHERE type='SALE' AND date=? AND is_cancelled=0", (today,))
        purchase = one("SELECT COALESCE(SUM(total_amount),0) FROM vouchers "
                        "WHERE type='PURCHASE' AND date=? AND is_cancelled=0", (today,))
        # Outstanding — Sundry Debtors Dr minus Sundry Creditors Cr,
        # yehi Dashboard [desktop] bhi dikhata hai.
        try:
            from services.ledger_service import get_trial_balance
            tb = get_trial_balance(_RODb(conn))
        except Exception as ex:
            logger.error(f"[WebDash] trial balance: {ex}")
            tb = []
        outstanding = 0.0
        for a in tb:
            if "debtor" in (a.get("group") or "").lower():
                outstanding += max(0.0, a.get("net", 0))
        items = int(conn.execute(
            "SELECT COUNT(*) FROM items WHERE is_active=1").fetchone()[0] or 0)
        parties = int(conn.execute(
            "SELECT COUNT(*) FROM accounts WHERE is_active=1").fetchone()[0] or 0)

        # Stock Value — same formula jo Dashboard [desktop] use karta
        # hai [Opening+Purchase-Sale+CN/DN+StockJournal, Sale Price se].
        stock_value = 0.0
        try:
            srows = conn.execute("""
                SELECT COALESCE(i.opening_stock,0), COALESCE(i.sale_price,0),
                       COALESCE(SUM(CASE WHEN v.type='SALE' THEN vi.qty ELSE 0 END),0),
                       COALESCE(SUM(CASE WHEN v.type='PURCHASE' THEN vi.qty ELSE 0 END),0),
                       COALESCE(SUM(CASE WHEN v.type='CREDIT_NOTE' THEN vi.qty ELSE 0 END),0),
                       COALESCE(SUM(CASE WHEN v.type='DEBIT_NOTE' THEN vi.qty ELSE 0 END),0)
                FROM items i
                LEFT JOIN voucher_items vi ON vi.item_name=i.name
                LEFT JOIN vouchers v ON v.id=vi.voucher_id AND v.is_cancelled=0
                WHERE i.is_active=1 AND i.item_type='Product'
                GROUP BY i.id""").fetchall()
            for opening, rate, sold, purchased, cn, dn in srows:
                current = float(opening) + (float(purchased) - float(dn)) - (float(sold) - float(cn))
                stock_value += current * float(rate)
        except Exception as ex:
            logger.error(f"[WebDash] stock_value: {ex}")

        return {"today_sale": sale, "today_purchase": purchase,
                "outstanding": round(outstanding, 2), "items": items,
                "parties": parties, "stock_value": round(stock_value, 2),
                "as_of": datetime.now().strftime("%d-%m-%Y %H:%M")}
    finally:
        conn.close()


def _get_recent(limit=15):
    conn = _ro_connect()
    try:
        rows = conn.execute(
            "SELECT date, type, party_name, total_amount, "
            "CASE is_cancelled WHEN 1 THEN 'Cancelled' ELSE 'Saved' END "
            "FROM vouchers ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"date": r[0], "type": r[1], "party": r[2] or "—",
                  "amount": float(r[3] or 0), "status": r[4]} for r in rows]
    finally:
        conn.close()


_PAGE_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bilnova AI — Mobile Dashboard</title>
<style>
  body{font-family:'Segoe UI',Arial,sans-serif;background:#eceff1;margin:0;padding:12px;color:#212121}
  h1{color:#1a237e;font-size:20px;margin:8px 0}
  .sub{color:#757575;font-size:12px;margin-bottom:16px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .card{background:#fff;border-radius:10px;padding:14px;box-shadow:0 1px 3px rgba(0,0,0,.15)}
  .card .lbl{color:#757575;font-size:12px}
  .card .val{font-size:20px;font-weight:bold;margin-top:4px}
  .green{color:#1b5e20}.red{color:#c62828}.blue{color:#0d47a1}.orange{color:#e65100}
  table{width:100%;border-collapse:collapse;margin-top:16px;background:#fff;border-radius:8px;overflow:hidden}
  th{background:#1a237e;color:#fff;padding:8px;font-size:12px;text-align:left}
  td{padding:8px;font-size:12px;border-bottom:1px solid #eee}
  #pinbox{max-width:300px;margin:60px auto;text-align:center}
  #pinbox input{font-size:22px;text-align:center;letter-spacing:6px;padding:10px;width:100%;
                box-sizing:border-box;border:2px solid #1a237e;border-radius:8px;margin-top:12px}
  #pinbox button{margin-top:10px;padding:10px 20px;background:#1a237e;color:#fff;border:none;
                  border-radius:8px;font-size:16px}
  #err{color:#c62828;font-size:13px;margin-top:8px;min-height:18px}
</style></head>
<body>
<div id="pinbox">
  <h1>⚡ Bilnova AI</h1>
  <div class="sub">Mobile Dashboard — PIN daalo</div>
  <input id="pin" type="password" inputmode="numeric" maxlength="8" placeholder="PIN">
  <button onclick="tryPin()">Dekho</button>
  <div id="err"></div>
</div>
<div id="dash" style="display:none">
  <h1>⚡ Bilnova AI — Dashboard</h1>
  <div class="sub" id="asof"></div>
  <div class="grid">
    <div class="card"><div class="lbl">Today's Sale</div><div class="val green" id="s_sale">—</div></div>
    <div class="card"><div class="lbl">Today's Purchase</div><div class="val blue" id="s_purch">—</div></div>
    <div class="card"><div class="lbl">Outstanding</div><div class="val orange" id="s_out">—</div></div>
    <div class="card"><div class="lbl">Stock Value</div><div class="val" id="s_stock">—</div></div>
    <div class="card"><div class="lbl">Total Items</div><div class="val" id="s_items">—</div></div>
    <div class="card"><div class="lbl">Total Parties</div><div class="val" id="s_parties">—</div></div>
  </div>
  <table><thead><tr><th>Date</th><th>Type</th><th>Party</th><th>Amount</th></tr></thead>
  <tbody id="recent"></tbody></table>
</div>
<script>
let PIN = "";
function tryPin(){
  PIN = document.getElementById('pin').value;
  fetch('/api/summary?pin=' + encodeURIComponent(PIN)).then(r=>{
    if(r.status===403){ document.getElementById('err').innerText='Galat PIN'; return null; }
    return r.json();
  }).then(d=>{ if(d){ document.getElementById('pinbox').style.display='none';
                       document.getElementById('dash').style.display='block';
                       localStorage.setItem('bilnova_pin', PIN);
                       refresh(); setInterval(refresh, 30000); } });
}
function fmt(n){ return '₹' + Number(n).toLocaleString('en-IN', {maximumFractionDigits:0}); }
function refresh(){
  fetch('/api/summary?pin=' + encodeURIComponent(PIN)).then(r=>r.json()).then(d=>{
    document.getElementById('s_sale').innerText = fmt(d.today_sale);
    document.getElementById('s_purch').innerText = fmt(d.today_purchase);
    document.getElementById('s_out').innerText = fmt(d.outstanding);
    document.getElementById('s_stock').innerText = fmt(d.stock_value);
    document.getElementById('s_items').innerText = d.items;
    document.getElementById('s_parties').innerText = d.parties;
    document.getElementById('asof').innerText = 'Last updated: ' + d.as_of;
  });
  fetch('/api/recent?pin=' + encodeURIComponent(PIN)).then(r=>r.json()).then(rows=>{
    const tb = document.getElementById('recent');
    tb.innerHTML = rows.map(r =>
      `<tr><td>${r.date}</td><td>${r.type}</td><td>${r.party}</td><td>${fmt(r.amount)}</td></tr>`
    ).join('');
  });
}
// Saved PIN se auto-login (agar isi phone se pehle bhi khola tha)
const saved = localStorage.getItem('bilnova_pin');
if(saved){ document.getElementById('pin').value = saved; tryPin(); }
</script>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # console spam mat karo

    def _cors_headers(self):
        # NEW 13-Sep-2026 (Rakesh — confirmed requested: "Play Store pe
        # nahi, direct client ko APK di jaaye" — PWA ab GitHub Pages
        # [alag origin] pe hosted hai, is local server se baat karti
        # hai. Bina in headers ke, browser/WebView har fetch() ko
        # CORS policy se CHUPKE SE block kar deta — "Connect" screen
        # kabhi kaam hi nahi karta, koi error bhi saaf na dikhta.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        # Browser CORS "preflight" request — kuch browsers GET se pehle
        # ye bhejte hain, isko bhi sahi jawab dena zaroori hai.
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def _check_pin(self, qs):
        if not _pin:
            return True  # PIN set nahi hai to open access (user ki apni choice)
        return qs.get("pin", [""])[0] == _pin

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                body = _PAGE_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self._cors_headers()
                self.end_headers()
                self.wfile.write(body)
            elif parsed.path == "/api/summary":
                if not self._check_pin(qs):
                    self.send_response(403); self._cors_headers(); self.end_headers(); return
                data = json.dumps(_get_summary()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self._cors_headers()
                self.end_headers()
                self.wfile.write(data)
            elif parsed.path == "/api/recent":
                if not self._check_pin(qs):
                    self.send_response(403); self._cors_headers(); self.end_headers(); return
                data = json.dumps(_get_recent()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self._cors_headers()
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(404); self._cors_headers(); self.end_headers()
        except Exception as ex:
            logger.error(f"[WebDash] request failed: {ex}")
            try:
                self.send_response(500); self._cors_headers(); self.end_headers()
            except Exception:
                pass


def is_running() -> bool:
    return _server is not None


def start(db_path: str, pin: str = "", port: int = 8899) -> dict:
    """Server start karta hai [agar already chal raha hai to same info
    wapas kar deta hai — dobara start nahi karta].
    Return: {"ok","url","port","error"}"""
    global _server, _server_thread, _db_path, _pin
    if _server is not None:
        return {"ok": True, "url": f"http://{get_local_ip()}:{_server.server_port}",
                "port": _server.server_port, "error": None}
    _db_path = db_path
    _pin = (pin or "").strip()
    for try_port in range(port, port + 10):
        try:
            _server = ThreadingHTTPServer(("0.0.0.0", try_port), _Handler)
            break
        except OSError:
            continue
    if _server is None:
        return {"ok": False, "url": None, "port": None,
                "error": f"Port {port}-{port+9} sab busy hain — koi doosra "
                         f"app band karke try karo."}
    _server_thread = threading.Thread(target=_server.serve_forever, daemon=True)
    _server_thread.start()
    ip = get_local_ip()
    return {"ok": True, "url": f"http://{ip}:{_server.server_port}",
            "port": _server.server_port, "error": None}


def stop():
    global _server, _server_thread
    if _server:
        try:
            _server.shutdown()
            _server.server_close()
        except Exception:
            pass
        _server = None
        _server_thread = None
