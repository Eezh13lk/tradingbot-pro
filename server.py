import json, os, time, threading, hmac, hashlib, urllib.parse
from statistics import mean, pstdev
from flask import Flask, jsonify, request, Response
import requests

CFG_PATH = "config.json"
JOURNAL = "journal.jsonl"

DEFAULT_CFG = {
    "api_key": "", "api_secret": "", "testnet": True,
    "symbol": "BTCUSDT", "timeframe": "5m", "leverage": 3,
    "risk_per_trade_pct": 0.005, "max_daily_loss_pct": 0.02,
    "max_drawdown_pct": 0.10, "poll_seconds": 15,
    "min_signal_score": 0.20,
    "use_smc": True, "use_meanrev": True, "use_reversal": True,
}

def load_cfg():
    if not os.path.exists(CFG_PATH):
        json.dump(DEFAULT_CFG, open(CFG_PATH, "w"), indent=2)
        return dict(DEFAULT_CFG)
    cfg = json.load(open(CFG_PATH))
    for k, v in DEFAULT_CFG.items(): cfg.setdefault(k, v)
    return cfg

def save_cfg(cfg): json.dump(cfg, open(CFG_PATH, "w"), indent=2)

def jlog(**rec):
    rec["ts"] = int(time.time() * 1000)
    with open(JOURNAL, "a") as f: f.write(json.dumps(rec) + "\n")

def jread(n=200):
    if not os.path.exists(JOURNAL): return []
    lines = open(JOURNAL).read().strip().split("\n")
    return [json.loads(x) for x in lines[-n:] if x.strip()]

class Binance:
    MAIN = "https://fapi.binance.com"
    TEST = "https://testnet.binancefuture.com"
    def __init__(self, key, secret, testnet=True):
        self.key = key; self.secret = secret.encode()
        self.base = self.TEST if testnet else self.MAIN
    def _sign(self, p):
        qs = urllib.parse.urlencode(p)
        return qs + "&signature=" + hmac.new(self.secret, qs.encode(), hashlib.sha256).hexdigest()
    def _h(self): return {"X-MBX-APIKEY": self.key}
    def get(self, path, params=None, signed=False):
        params = params or {}
        if signed:
            params["timestamp"] = int(time.time() * 1000); params["recvWindow"] = 5000
            url = f"{self.base}{path}?{self._sign(params)}"
        else:
            url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        r = requests.get(url, headers=self._h(), timeout=10); r.raise_for_status(); return r.json()
    def post(self, path, params):
        params["timestamp"] = int(time.time() * 1000); params["recvWindow"] = 5000
        url = f"{self.base}{path}?{self._sign(params)}"
        r = requests.post(url, headers=self._h(), timeout=10); r.raise_for_status(); return r.json()
    def klines(self, symbol, interval, limit=200):
        d = self.get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
        return [{"o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]), "v": float(k[5])} for k in d]
    def price(self, symbol): return float(self.get("/fapi/v1/ticker/price", {"symbol": symbol})["price"])
    def set_leverage(self, symbol, lev): return self.post("/fapi/v1/leverage", {"symbol": symbol, "leverage": lev})
    def balance(self):
        d = self.get("/fapi/v2/balance", signed=True)
        for a in d:
            if a["asset"] == "USDT": return float(a["availableBalance"])
        return 0.0
    def position(self, symbol):
        d = self.get("/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        for p in d:
            amt = float(p["positionAmt"])
            if amt != 0:
                return {"side": "long" if amt > 0 else "short", "qty": abs(amt),
                        "entry": float(p["entryPrice"]), "pnl": float(p["unRealizedProfit"])}
        return None
    def market_order(self, symbol, side, qty, reduce_only=False):
        return self.post("/fapi/v1/order", {"symbol": symbol, "side": side, "type": "MARKET",
            "quantity": qty, "reduceOnly": "true" if reduce_only else "false"})
    def stop_order(self, symbol, side, stop_price, qty):
        return self.post("/fapi/v1/order", {"symbol": symbol, "side": side, "type": "STOP_MARKET",
            "stopPrice": round(stop_price, 2), "quantity": qty, "reduceOnly": "true",
            "workingType": "MARK_PRICE"})
    def cancel_all(self, symbol):
        try: self.post("/fapi/v1/allOpenOrders", {"symbol": symbol})
        except Exception: pass

def swings(bars, lb=3):
    highs, lows = [], []
    for i in range(lb, len(bars) - lb):
        h, l = bars[i]["h"], bars[i]["l"]
        if all(h >= bars[j]["h"] for j in range(i - lb, i + lb + 1) if j != i): highs.append(i)
        if all(l <= bars[j]["l"] for j in range(i - lb, i + lb + 1) if j != i): lows.append(i)
    return highs, lows

def atr(bars, n=14):
    if len(bars) < n + 1: return 0.0
    trs = [max(bars[i]["h"] - bars[i]["l"], abs(bars[i]["h"] - bars[i-1]["c"]),
               abs(bars[i]["l"] - bars[i-1]["c"])) for i in range(1, len(bars))]
    return mean(trs[-n:])

def rsi(bars, n=14):
    if len(bars) < n + 1: return 50.0
    g, l = [], []
    for i in range(1, len(bars)):
        d = bars[i]["c"] - bars[i-1]["c"]
        g.append(max(d, 0)); l.append(max(-d, 0))
    gg, ll = mean(g[-n:]), mean(l[-n:]) or 1e-9
    return 100 - 100 / (1 + gg / ll)

def sig_smc(bars):
    if len(bars) < 40: return None
    highs, lows = swings(bars)
    if not highs or not lows: return None
    lh = bars[highs[-1]]["h"]; ll = bars[lows[-1]]["l"]; c = bars[-1]["c"]
    if c > lh:
        risk = c - ll
        if risk > 0: return {"side": "long", "strategy": "SMC", "strength": 0.9, "stop": ll, "target": c + 2*risk}
    if c < ll:
        risk = lh - c
        if risk > 0: return {"side": "short", "strategy": "SMC", "strength": 0.9, "stop": lh, "target": c - 2*risk}
    return None

def sig_meanrev(bars, n=40, z=2.0):
    if len(bars) < n: return None
    closes = [b["c"] for b in bars[-n:]]
    m = mean(closes); sd = pstdev(closes) or 1e-9
    zz = (bars[-1]["c"] - m) / sd
    if zz <= -z: return {"side": "long", "strategy": "MEANREV", "strength": min(1.0, abs(zz)/3), "stop": m - 2*sd, "target": m}
    if zz >= z:  return {"side": "short", "strategy": "MEANREV", "strength": min(1.0, abs(zz)/3), "stop": m + 2*sd, "target": m}
    return None

def sig_reversal(bars):
    if len(bars) < 20: return None
    r = rsi(bars, 14); b = bars[-1]
    rng = b["h"] - b["l"] or 1e-9
    if r < 30 and (b["c"] - b["l"])/rng > 0.6:
        return {"side": "long", "strategy": "REVERSAL", "strength": (30-r)/30, "stop": b["l"], "target": b["c"] + 2*(b["c"]-b["l"])}
    if r > 70 and (b["c"] - b["l"])/rng < 0.4:
        return {"side": "short", "strategy": "REVERSAL", "strength": (r-70)/30, "stop": b["h"], "target": b["c"] - 2*(b["h"]-b["c"])}
    return None

def ensemble(sigs, cfg):
    if not sigs: return None
    score = sum((1 if s["side"] == "long" else -1) * s["strength"] for s in sigs)
    if abs(score) < cfg["min_signal_score"]: return None
    side = "long" if score > 0 else "short"
    members = [s for s in sigs if s["side"] == side]
    stops = [s["stop"] for s in members if s.get("stop")]
    if not stops: return None
    stop = min(stops) if side == "long" else max(stops)
    return {"side": side, "stop": stop, "score": abs(score), "members": [s["strategy"] for s in members]}

class Risk:
    def __init__(self, cfg, eq):
        self.cfg = cfg; self.eq = eq; self.peak = eq
        self.day = time.strftime("%Y-%m-%d"); self.day_start = eq
        self.halted = False; self.reason = ""
    def update(self, eq):
        self.eq = eq; self.peak = max(self.peak, eq)
        d = time.strftime("%Y-%m-%d")
        if d != self.day: self.day = d; self.day_start = eq; self.halted = False; self.reason = ""
        if (eq - self.day_start)/max(self.day_start, 1e-9) <= -self.cfg["max_daily_loss_pct"]:
            self.halted = True; self.reason = "daily_loss_cap"
        if (eq - self.peak)/max(self.peak, 1e-9) <= -self.cfg["max_drawdown_pct"]:
            self.halted = True; self.reason = "max_drawdown"
    def size(self, entry, stop):
        risk_usd = self.eq * self.cfg["risk_per_trade_pct"]
        per_unit = abs(entry - stop)
        return 0 if per_unit <= 0 else risk_usd / per_unit

class Bot:
    def __init__(self):
        self.cfg = load_cfg()
        self.log = []
        self.running = False
        self.thread = None
        self.risk = None
        self.client = None
        self.state = {"equity": 0, "position": None, "loops": 0, "last_sig": None}
    def _say(self, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self.log.append(line); self.log = self.log[-300:]
        print(line); jlog(event="log", msg=msg)
    def start(self):
        if self.running: return
        self.cfg = load_cfg()
        if not self.cfg["api_key"]:
            self._say("no api_key — set it in Settings first"); return
        self.client = Binance(self.cfg["api_key"], self.cfg["api_secret"], self.cfg["testnet"])
        self.risk = Risk(self.cfg, 0)
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True); self.thread.start()
        self._say(f"started on {self.cfg['symbol']} {self.cfg['timeframe']} {'TESTNET' if self.cfg['testnet'] else 'LIVE'}")
    def stop(self):
        self.running = False; self._say("stopping...")
    def _loop(self):
        cfg = self.cfg; sym = cfg["symbol"]
        try:
            self.client.set_leverage(sym, cfg["leverage"])
            self._say(f"leverage {cfg['leverage']}x set")
        except Exception as e: self._say(f"leverage error: {e}")
        try:
            bal = self.client.balance()
            self.risk.eq = bal; self.risk.peak = bal; self.risk.day_start = bal
        except Exception as e:
            self._say(f"balance error: {e}"); self.running = False; return
        while self.running:
            try:
                bars = self.client.klines(sym, cfg["timeframe"], 200)
                sigs = []
                if cfg["use_smc"]:      s = sig_smc(bars);      sigs += [s] if s else []
                if cfg["use_meanrev"]:  s = sig_meanrev(bars);  sigs += [s] if s else []
                if cfg["use_reversal"]: s = sig_reversal(bars); sigs += [s] if s else []
                combo = ensemble(sigs, cfg)
                pos = self.client.position(sym)
                eq = self.client.balance() + (pos["pnl"] if pos else 0)
                self.risk.update(eq)
                self.state.update({"equity": eq, "position": pos, "loops": self.state["loops"] + 1,
                                   "last_sig": None if not combo else combo["side"]})
                self._say(f"eq={eq:.2f} pos={'flat' if not pos else pos['side']} sig={self.state['last_sig']}")
                if pos is None and combo and not self.risk.halted:
                    entry = bars[-1]["c"]; stop = combo["stop"]
                    qty = round(self.risk.size(entry, stop), 3)
                    if qty > 0:
                        side = "BUY" if combo["side"] == "long" else "SELL"
                        try:
                            self.client.cancel_all(sym)
                            self.client.market_order(sym, side, qty)
                            self.client.stop_order(sym, "SELL" if combo["side"] == "long" else "BUY", stop, qty)
                            self._say(f"OPEN {combo['side']} {qty} @ {entry:.2f} stop={stop:.2f}")
                            jlog(event="open", side=combo["side"], qty=qty, entry=entry, stop=stop,
                                 members=combo["members"], score=combo["score"])
                        except Exception as e: self._say(f"order error: {e}")
                elif self.risk.halted:
                    self._say(f"HALTED: {self.risk.reason}")
                time.sleep(cfg["poll_seconds"])
            except Exception as e:
                self._say(f"loop error: {e}"); time.sleep(5)

BOT = Bot()
app = Flask(__name__)

HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=theme-color content="#0b1220">
<meta name=apple-mobile-web-app-capable content=yes>
<title>TradingBot Pro</title>
<style>:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;font-family:system-ui,sans-serif;background:#0b1220;color:#e8eefc;padding:12px;max-width:820px;margin:0 auto;padding-top:env(safe-area-inset-top);padding-bottom:env(safe-area-inset-bottom)}
header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
h1{font-size:1.1rem;margin:0}
.badge{font-size:.7rem;padding:3px 8px;border-radius:20px;background:#1e293b;color:#94a3b8}
.badge.on{background:#052e16;color:#86efac}.badge.off{background:#450a0a;color:#fca5a5}
.card{background:#141c2e;border:1px solid #243047;border-radius:14px;padding:14px;margin-bottom:12px}
.card h2{font-size:.9rem;margin:0 0 10px;color:#94a3b8;text-transform:uppercase;letter-spacing:.05em}
.row{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #1e293b;font-size:.9rem}
.row:last-child{border-bottom:0}
.pos{color:#86efac}.neg{color:#fca5a5}.muted{color:#64748b}
button{width:100%;border:0;border-radius:12px;padding:14px;font-size:1rem;font-weight:700;cursor:pointer;margin-top:8px}
.btn-start{background:#16a34a;color:#fff}.btn-stop{background:#dc2626;color:#fff}.btn-save{background:#3b82f6;color:#fff}
input,select{width:100%;padding:10px 12px;border-radius:10px;border:1px solid #334155;background:#0b1220;color:#e8eefc;font-size:1rem;margin-top:4px}
label{display:block;font-size:.78rem;color:#94a3b8;margin-top:10px}
pre{white-space:pre-wrap;font-size:.72rem;color:#94a3b8;margin:0;max-height:280px;overflow:auto;font-family:ui-monospace,monospace;line-height:1.4}
.eq{font-size:2rem;font-weight:800;margin:4px 0}
.tabs{display:flex;gap:6px;margin-bottom:12px}
.tab{flex:1;padding:10px;text-align:center;border-radius:10px;background:#141c2e;border:1px solid #243047;font-size:.85rem;cursor:pointer;color:#94a3b8}
.tab.active{background:#1e293b;color:#e8eefc;border-color:#3b82f6}
.hidden{display:none}
.switch{display:flex;align-items:center;justify-content:space-between;padding:8px 0}
.switch input{width:auto;margin:0}</style></head><body>
<header><h1>TradingBot Pro</h1><span id=status class="badge off">offline</span></header>
<div class=tabs>
<div class="tab active" data-tab=dash>Dashboard</div>
<div class=tab data-tab=cfg>Settings</div>
<div class=tab data-tab=jr>Journal</div></div>
<div id=tab-dash>
<div class=card><h2>Equity</h2><div class=eq id=equity>$—</div>
<div class=row><span class=muted>Loops</span><span id=loops>0</span></div>
<div class=row><span class=muted>Halted</span><span id=halted>—</span></div>
<div class=row><span class=muted>Last signal</span><span id=lastsig>—</span></div></div>
<div class=card><h2>Position</h2><div id=position class=muted>flat</div></div>
<div class=card><button id=btn-start class=btn-start>START</button><button id=btn-stop class=btn-stop>STOP</button></div>
<div class=card><h2>Log</h2><pre id=log>waiting…</pre></div></div>
<div id=tab-cfg class=hidden>
<div class=card><h2>Binance API</h2>
<label>API Key<input id=api_key placeholder="paste testnet key"></label>
<label>API Secret<input id=api_secret type=password placeholder="paste secret"></label>
<div class=switch><span>Testnet mode</span><input id=testnet type=checkbox></div>
<p class=muted style="font-size:.75rem;margin-top:8px">Get testnet keys at <b>testnet.binancefuture.com</b>. Never paste live keys.</p></div>
<div class=card><h2>Trading</h2>
<label>Symbol<input id=symbol placeholder=BTCUSDT></label>
<label>Timeframe<select id=timeframe><option>1m</option><option>5m</option><option selected>15m</option><option>1h</option><option>4h</option></select></label>
<label>Leverage<input id=leverage type=number min=1 max=20></label>
<label>Risk % per trade<input id=risk type=number step=0.1 min=0.1 max=5></label>
<label>Max daily loss %<input id=daily type=number step=0.1 min=0.5 max=20></label>
<label>Max drawdown %<input id=dd type=number step=0.5 min=1 max=50></label></div>
<div class=card><h2>Strategies</h2>
<div class=switch><span>SMC</span><input id=use_smc type=checkbox></div>
<div class=switch><span>Mean reversion</span><input id=use_meanrev type=checkbox></div>
<div class=switch><span>Reversal</span><input id=use_reversal type=checkbox></div></div>
<button id=btn-save class=btn-save>SAVE</button></div>
<div id=tab-jr class=hidden><div class=card><h2>Recent trades</h2><div id=journal class=muted>no trades yet</div></div></div>
<script>
async function get(p){const r=await fetch(p);if(!r.ok)throw 0;return r.json()}
async function post(p,b){return(await fetch(p,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b||{})})).json()}
async function refresh(){try{const s=await get("/api/state");
document.getElementById("status").textContent=s.running?"running":"stopped";
document.getElementById("status").className="badge "+(s.running?"on":"off");
document.getElementById("equity").textContent="$"+(s.equity||0).toFixed(2);
document.getElementById("loops").textContent=s.loops;
document.getElementById("halted").innerHTML=s.halted?`<span class=neg>${s.halt_reason||"yes"}</span>`:"no";
document.getElementById("lastsig").textContent=s.last_sig||"—";
const p=s.position;
document.getElementById("position").innerHTML=!p?'<span class=muted>flat</span>':`<div class=row><span>${p.side.toUpperCase()}</span><span>qty ${p.qty}</span></div><div class=row><span class=muted>entry</span><span>${p.entry}</span></div><div class=row><span class=muted>unrealized</span><span class="${p.pnl>=0?'pos':'neg'}">${p.pnl.toFixed(2)}</span></div>`;
document.getElementById("log").textContent=(s.log||[]).join("\\n")||"waiting…";
const c=s.cfg;const f=document.getElementById("symbol");
if(c&&!f.value){f.value=c.symbol||"";document.getElementById("timeframe").value=c.timeframe||"15m";document.getElementById("leverage").value=c.leverage||3;document.getElementById("risk").value=((c.risk_per_trade_pct||0.005)*100).toFixed(2);document.getElementById("daily").value=((c.max_daily_loss_pct||0.02)*100).toFixed(2);document.getElementById("dd").value=((c.max_drawdown_pct||0.10)*100).toFixed(2);document.getElementById("testnet").checked=c.testnet;document.getElementById("use_smc").checked=c.use_smc;document.getElementById("use_meanrev").checked=c.use_meanrev;document.getElementById("use_reversal").checked=c.use_reversal;}}
catch(e){document.getElementById("status").textContent="offline";document.getElementById("status").className="badge off"}}
async function save(){const b={symbol:document.getElementById("symbol").value.trim().toUpperCase(),timeframe:document.getElementById("timeframe").value,leverage:parseInt(document.getElementById("leverage").value,10),risk_per_trade_pct:parseFloat(document.getElementById("risk").value)/100,max_daily_loss_pct:parseFloat(document.getElementById("daily").value)/100,max_drawdown_pct:parseFloat(document.getElementById("dd").value)/100,testnet:document.getElementById("testnet").checked,use_smc:document.getElementById("use_smc").checked,use_meanrev:document.getElementById("use_meanrev").checked,use_reversal:document.getElementById("use_reversal").checked};const k=document.getElementById("api_key").value.trim();const s=document.getElementById("api_secret").value.trim();if(k)b.api_key=k;if(s)b.api_secret=s;await post("/api/cfg",b);alert("Saved.")}
document.querySelectorAll(".tab").forEach(t=>{t.onclick=()=>{document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));t.classList.add("active");["dash","cfg","jr"].forEach(id=>{document.getElementById("tab-"+id).classList.toggle("hidden",id!==t.dataset.tab)});if(t.dataset.tab==="jr")loadJournal()}});
async function loadJournal(){try{const rows=await get("/api/journal");const el=document.getElementById("journal");if(!rows.length){el.textContent="no trades yet";return}el.innerHTML=rows.reverse().map(r=>`<div class=row><span>${new Date(r.ts).toLocaleTimeString()} <b>${r.event}</b> ${r.side||""}</span><span class="${(r.pnl||0)>=0?'pos':'neg'}">${r.pnl!=null?r.pnl.toFixed(2):""}</span></div>`).join("")}catch(e){el.textContent="error"}}
document.getElementById("btn-start").onclick=()=>post("/api/start").then(refresh);
document.getElementById("btn-stop").onclick=()=>post("/api/stop").then(refresh);
document.getElementById("btn-save").onclick=save;
refresh();setInterval(refresh,5000);
</script></body></html>"""

@app.route("/")
def root(): return Response(HTML, mimetype="text/html")

@app.route("/api/state")
def api_state():
    return jsonify({"running": BOT.running, "equity": BOT.state["equity"],
                    "position": BOT.state["position"], "loops": BOT.state["loops"],
                    "last_sig": BOT.state["last_sig"],
                    "halted": BOT.risk.halted if BOT.risk else False,
                    "halt_reason": BOT.risk.reason if BOT.risk else "",
                    "log": BOT.log[-60:],
                    "cfg": {k: v for k, v in BOT.cfg.items() if k not in ("api_key", "api_secret")}})

@app.route("/api/start", methods=["POST"])
def api_start(): BOT.start(); return jsonify({"ok": True})

@app.route("/api/stop", methods=["POST"])
def api_stop(): BOT.stop(); return jsonify({"ok": True})

@app.route("/api/cfg", methods=["POST"])
def api_cfg():
    new = request.json or {}
    cfg = load_cfg()
    for k in ("api_key", "api_secret", "testnet", "symbol", "timeframe", "leverage",
              "risk_per_trade_pct", "max_daily_loss_pct", "max_drawdown_pct",
              "poll_seconds", "min_signal_score", "use_smc", "use_meanrev", "use_reversal"):
        if k in new: cfg[k] = new[k]
    save_cfg(cfg); BOT.cfg = cfg
    return jsonify({"ok": True})

@app.route("/api/journal")
def api_journal(): return jsonify(jread(200))

if __name__ == "__main__":
    print("=" * 60)
    print(" TradingBot Pro — Codespaces mode")
    print(" In Ports tab, make port 8000 PUBLIC, then open that URL")
    print("=" * 60)
    app.run(host="0.0.0.0", port=8000, debug=False, use_reloader=False)