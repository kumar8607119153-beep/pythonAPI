"""
SANJAY RANA — Delta Exchange Signal Dashboard
Signal-only dashboard. No API keys, trading, order placement, cancellation, or positions.
"""
import os, time, threading, math, logging
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template_string
import requests

try:
    import websocket
except Exception:
    websocket = None

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("signal-dashboard")

# Delta Exchange India public market data only.
REST_BASE = os.getenv("DELTA_REST_BASE", "https://api.india.delta.exchange").rstrip("/")
WS_URL = os.getenv("DELTA_PUBLIC_WS", "wss://public-socket.india.delta.exchange")
TIMEFRAMES = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
MARKET_WISH_LIST = [
    {"key": "BTC", "candidates": ["BTCUSD", "BTCUSDT"], "name": "Bitcoin", "logo": "https://assets.coingecko.com/coins/images/1/large/bitcoin.png"},
    {"key": "ETH", "candidates": ["ETHUSD", "ETHUSDT"], "name": "Ethereum", "logo": "https://assets.coingecko.com/coins/images/279/large/ethereum.png"},
    {"key": "XAUTUSDT", "candidates": ["XAUTUSDT", "XAUTUSD"], "name": "Tether Gold", "logo": "https://assets.coingecko.com/coins/images/10481/large/Tether_Gold.png"},
    {"key": "OILUSD", "candidates": ["OILUSD", "USOIL", "WTIUSD", "CLUSD"], "name": "Oil", "logo": ""},
]
state_lock = threading.RLock()
state = {
    "started": False, "connected": False, "last_ws_message": None,
    "products_checked": False, "product_error": None, "products": {},
    "prices": {}, "price_ts": {}, "signals": {}, "candle_errors": {},
    "ws_status": "Starting", "last_rest": None
}
session = requests.Session()
session.headers.update({"User-Agent": "SanjayRana-SignalDashboard/1.0"})

def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def api_get(path, params=None, timeout=12):
    response = session.get(REST_BASE + path, params=params, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(str(data.get("error") or data))
    return data

def get_products():
    # Public products endpoint; only show instruments actually returned by Delta.
    data = api_get("/v2/products")
    result = data.get("result", []) if isinstance(data, dict) else []
    if not isinstance(result, list):
        result = []
    active = {}
    for p in result:
        sym = str(p.get("symbol", "")).upper()
        status = str(p.get("state", p.get("status", "live"))).lower()
        if sym and status in ("live", "active", "online", ""):
            active[sym] = p
    selected = {}
    for item in MARKET_WISH_LIST:
        found = next((active[s.upper()] for s in item["candidates"] if s.upper() in active), None)
        if found:
            selected[item["key"]] = {
                **item,
                "symbol": str(found.get("symbol", "")).upper(),
                "product_id": found.get("id"),
                "contract_type": found.get("contract_type", ""),
                "status": "Available on Delta"
            }
        else:
            selected[item["key"]] = {**item, "symbol": None, "product_id": None,
                                     "status": "Exact symbol not found on Delta Exchange"}
    with state_lock:
        state["products"] = selected
        state["products_checked"] = True
        state["product_error"] = None
    return selected

def fetch_candles(symbol, resolution, count=150):
    seconds = TIMEFRAMES[resolution]
    end = int(time.time())
    start = end - seconds * count
    data = api_get("/history/candles", {
        "symbol": symbol, "resolution": resolution, "start": start, "end": end
    })
    rows = data.get("result", []) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise RuntimeError("Unexpected candle response")
    candles = []
    for r in rows:
        try:
            candles.append({
                "time": int(r.get("time", r.get("t", r.get("timestamp", 0)))),
                "open": float(r.get("open", r.get("o"))),
                "high": float(r.get("high", r.get("h"))),
                "low": float(r.get("low", r.get("l"))),
                "close": float(r.get("close", r.get("c"))),
            })
        except (TypeError, ValueError):
            continue
    candles.sort(key=lambda x: x["time"])
    # Delta may return candle timestamps in milliseconds in some feeds.
    for c in candles:
        if c["time"] > 10_000_000_000:
            c["time"] //= 1000
    if len(candles) < 15:
        raise RuntimeError(f"Not enough candles returned ({len(candles)})")
    return candles

def supertrend(candles, period=10, multiplier=2.0):
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    trs = []
    for i in range(len(candles)):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    atr = [None] * len(candles)
    if len(candles) < period:
        return []
    atr[period-1] = sum(trs[:period]) / period
    for i in range(period, len(candles)):
        atr[i] = ((atr[i-1] * (period-1)) + trs[i]) / period
    final_upper = [None] * len(candles)
    final_lower = [None] * len(candles)
    st = [None] * len(candles)
    direction = [None] * len(candles)
    for i in range(period-1, len(candles)):
        mid = (highs[i] + lows[i]) / 2
        basic_upper = mid + multiplier * atr[i]
        basic_lower = mid - multiplier * atr[i]
        if i == period-1:
            final_upper[i], final_lower[i] = basic_upper, basic_lower
            direction[i] = "SELL" if closes[i] < mid else "BUY"
            st[i] = final_upper[i] if direction[i] == "SELL" else final_lower[i]
            continue
        final_upper[i] = basic_upper if (basic_upper < final_upper[i-1] or closes[i-1] > final_upper[i-1]) else final_upper[i-1]
        final_lower[i] = basic_lower if (basic_lower > final_lower[i-1] or closes[i-1] < final_lower[i-1]) else final_lower[i-1]
        if direction[i-1] == "SELL":
            direction[i] = "BUY" if closes[i] > final_upper[i] else "SELL"
        else:
            direction[i] = "SELL" if closes[i] < final_lower[i] else "BUY"
        st[i] = final_lower[i] if direction[i] == "BUY" else final_upper[i]
    output = []
    for i in range(period, len(candles)):
        output.append({
            "candle_time": candles[i]["time"],
            "direction": direction[i],
            "signal_price": closes[i] if direction[i] != direction[i-1] else None,
            "tsl_price": st[i],
            "close": closes[i],
            "previous_direction": direction[i-1],
        })
    return output

def update_signal_snapshot():
    while True:
        try:
            with state_lock:
                products = dict(state["products"])
            if not products:
                products = get_products()
            snapshot = {}
            errors = {}
            for key, product in products.items():
                symbol = product.get("symbol")
                if not symbol:
                    continue
                for tf in TIMEFRAMES:
                    ident = f"{key}|{tf}"
                    try:
                        candles = fetch_candles(symbol, tf)
                        calc = supertrend(candles)
                        if not calc:
                            raise RuntimeError("Supertrend could not be calculated")
                        last = calc[-1]
                        # Signal price = close of most recent confirmed direction-change candle.
                        signal = None
                        for item in reversed(calc):
                            if item["signal_price"] is not None:
                                signal = item
                                break
                        current = {
                            "market": key, "symbol": symbol, "timeframe": tf,
                            "direction": last["direction"],
                            "signal_price": signal["signal_price"] if signal else None,
                            "signal_time": signal["candle_time"] if signal else None,
                            "tsl_price": last["tsl_price"],
                            "candle_close": last["close"],
                            "last_candle_time": last["candle_time"],
                            "updated_at": utc_now(),
                            "status": "LIVE DATA"
                        }
                        snapshot[ident] = current
                        errors.pop(ident, None)
                    except Exception as e:
                        errors[ident] = str(e)[:180]
            with state_lock:
                state["signals"].update(snapshot)
                state["candle_errors"] = errors
                state["last_rest"] = utc_now()
        except Exception as e:
            log.exception("Signal refresh failed")
            with state_lock:
                state["product_error"] = str(e)[:200]
        time.sleep(30)

def on_ws_open(ws):
    with state_lock:
        products = dict(state["products"])
    symbols = sorted({p["symbol"] for p in products.values() if p.get("symbol")})
    if not symbols:
        try:
            products = get_products()
            symbols = sorted({p["symbol"] for p in products.values() if p.get("symbol")})
        except Exception as e:
            log.warning("Product lookup for websocket failed: %s", e)
    if not symbols:
        with state_lock: state["ws_status"] = "No matching Delta symbols"
        return
    payload = {"type": "subscribe", "payload": {"channels": [{"name": "trades", "symbols": symbols}]}}
    ws.send(__import__("json").dumps(payload))
    with state_lock:
        state["connected"] = True
        state["ws_status"] = "Connected to public trade feed"

def on_ws_message(ws, message):
    import json
    try:
        obj = json.loads(message)
        with state_lock:
            state["last_ws_message"] = utc_now()
        # Trade messages normally include symbol and price. Tolerate field-name variants.
        events = obj if isinstance(obj, list) else [obj]
        for event in events:
            if not isinstance(event, dict):
                continue
            symbol = str(event.get("sy", event.get("symbol", event.get("product_symbol", "")))).upper()
            raw_price = event.get("p", event.get("price", event.get("last_price")))
            if not symbol or raw_price is None:
                continue
            try:
                price = float(raw_price)
            except (TypeError, ValueError):
                continue
            with state_lock:
                state["prices"][symbol] = price
                state["price_ts"][symbol] = utc_now()
    except Exception:
        log.debug("Ignoring non-JSON/heartbeat WS frame")

def on_ws_error(ws, error):
    with state_lock:
        state["connected"] = False
        state["ws_status"] = "WebSocket error; reconnecting"
    log.warning("WebSocket error: %s", error)

def on_ws_close(ws, code, msg):
    with state_lock:
        state["connected"] = False
        state["ws_status"] = "Disconnected; reconnecting"

def websocket_loop():
    if websocket is None:
        with state_lock: state["ws_status"] = "websocket-client dependency missing"
        return
    while True:
        try:
            if not state["products_checked"]:
                try: get_products()
                except Exception as e:
                    with state_lock:
                        state["product_error"] = str(e)[:200]
                        state["ws_status"] = "Cannot load Delta products"
                    time.sleep(10)
                    continue
            app_ws = websocket.WebSocketApp(WS_URL, on_open=on_ws_open,
                                            on_message=on_ws_message,
                                            on_error=on_ws_error,
                                            on_close=on_ws_close)
            app_ws.run_forever(ping_interval=25, ping_timeout=8)
        except Exception as e:
            log.warning("WebSocket loop error: %s", e)
        with state_lock:
            state["connected"] = False
        time.sleep(5)

def start_workers():
    with state_lock:
        if state["started"]:
            return
        state["started"] = True
    threading.Thread(target=update_signal_snapshot, daemon=True, name="signal-refresh").start()
    threading.Thread(target=websocket_loop, daemon=True, name="public-trades").start()

@app.route("/")
def home():
    start_workers()
    return render_template_string(HTML)

@app.route("/api/state")
def api_state():
    start_workers()
    with state_lock:
        payload = {
            "connected": state["connected"], "ws_status": state["ws_status"],
            "last_ws_message": state["last_ws_message"], "last_rest": state["last_rest"],
            "product_error": state["product_error"],
            "products": state["products"], "prices": state["prices"],
            "price_ts": state["price_ts"], "signals": state["signals"],
            "candle_errors": state["candle_errors"], "timeframes": list(TIMEFRAMES.keys()),
            "server_time": utc_now(), "indicator": "Supertrend ATR 10 / Multiplier 2.0",
            "trading_enabled": False
        }
    return jsonify(payload)

HTML = r"""
<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sanjay Rana — Live Signals</title>
<style>
:root{color-scheme:dark;--bg:#08111f;--panel:#111d30;--line:#24364e;--muted:#9db0c9;--text:#edf4ff;--green:#20c997;--red:#ff667a;--gold:#f4c95d}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top,#152640 0,#08111f 55%);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1440px;margin:auto;padding:18px}.top{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.brand{display:flex;gap:12px;align-items:center}.mark{width:44px;height:44px;border-radius:13px;display:grid;place-items:center;background:linear-gradient(145deg,#f4c95d,#d58a1c);color:#101827;font-weight:900;font-size:18px;box-shadow:0 8px 25px #d58a1c33}
h1{font-size:clamp(19px,3vw,27px);margin:0;letter-spacing:.3px}.sub{color:var(--muted);font-size:12px;margin-top:3px}
.pill{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--line);background:#101d30;border-radius:999px;padding:8px 11px;color:var(--muted);font-size:12px}.dot{width:7px;height:7px;border-radius:50%;background:#8190a7}.dot.live{background:var(--green);box-shadow:0 0 12px #20c99788}
.banner{border:1px solid #694f1d;background:#322713;color:#ffe6a2;padding:10px 13px;border-radius:12px;margin-bottom:14px;font-size:12px}
.controls{display:flex;gap:7px;flex-wrap:wrap;margin:14px 0}.tf{border:1px solid var(--line);background:#101d30;color:#c4d3e7;border-radius:9px;padding:9px 15px;font-weight:700;cursor:pointer}.tf.active{background:#f4c95d;color:#111827;border-color:#f4c95d}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:13px}.card{min-width:0;background:linear-gradient(145deg,#142239,#0f1a2b);border:1px solid var(--line);border-radius:16px;padding:15px;box-shadow:0 10px 30px #0002}
.cardhead{display:flex;align-items:center;justify-content:space-between;gap:8px}.asset{display:flex;align-items:center;gap:10px;min-width:0}.logo{width:38px;height:38px;border-radius:50%;background:#fff;object-fit:contain;padding:4px;flex-shrink:0}.oilLogo{display:grid;place-items:center;background:#d5e2ef;font-size:21px}.assetname{font-size:16px;font-weight:800}.symbol{font-size:11px;color:var(--muted)}.tag{font-size:10px;letter-spacing:.6px;font-weight:800;border-radius:6px;padding:5px 7px;background:#24344b;color:#b9c9df;white-space:nowrap}
.direction{font-size:12px;font-weight:900;letter-spacing:.8px;padding:6px 9px;border-radius:7px;background:#25344b;color:#c5d3e5}.direction.BUY{background:#123c38;color:#62f0c4}.direction.SELL{background:#47232e;color:#ff91a0}
.price{font-size:clamp(23px,4vw,32px);font-weight:800;letter-spacing:-.6px;margin:15px 0 3px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.priceLabel{font-size:10px;letter-spacing:1px;color:var(--muted);font-weight:700}.stats{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:14px}.stat{padding:10px;background:#09142277;border:1px solid #20314a;border-radius:10px;min-width:0}.stat .label{font-size:10px;color:var(--muted);letter-spacing:.7px}.stat .val{font-size:15px;font-weight:750;margin-top:4px;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}.foot{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;color:var(--muted);font-size:10px;margin-top:12px}.notice{color:var(--gold);font-size:11px;margin-top:10px}.bottom{color:var(--muted);font-size:11px;text-align:center;margin:20px 0 8px}
@media(max-width:680px){.wrap{padding:12px}.grid{grid-template-columns:1fr}.card{padding:13px}.top{align-items:flex-start}.pill{padding:7px 9px}}
</style></head><body><main class="wrap">
<header class="top"><div class="brand"><div class="mark">SR</div><div><h1>SANJAY RANA <span style="color:#f4c95d">LIVE SIGNALS</span></h1><div class="sub">DELTA EXCHANGE PUBLIC DATA · SUPERTREND 10 / 2.0</div></div></div>
<div class="pill"><span id="dot" class="dot"></span><span id="connection">Connecting to Delta public feed…</span></div></header>
<div class="banner"><b>SIGNAL ONLY — NO TRADING.</b> This dashboard has no order, account, or position functions. Prices and signals appear only when the exact market is available from Delta Exchange.</div>
<div class="controls" id="controls"></div><section class="grid" id="grid"></section>
<div class="bottom">Public market data only · Price updates arrive on exchange trade ticks · Signals refresh from candles · <span id="clock">—</span></div>
</main>
<script>
const tfs=['1m','5m','15m','1h']; let selectedTf='1m'; let latest=null;
const $=id=>document.getElementById(id);
function fmt(v){if(v===null||v===undefined||!Number.isFinite(Number(v)))return '—';return Number(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:8});}
function time(v){if(!v)return '—';try{return new Date(v).toLocaleTimeString([], {hour12:false})}catch(e){return '—'}}
function timeEpoch(v){if(!v)return '—';return new Date(Number(v)*1000).toLocaleString([], {month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})}
function buildControls(){ $('controls').innerHTML=tfs.map(tf=>`<button class="tf ${tf===selectedTf?'active':''}" data-tf="${tf}">${tf}</button>`).join('');document.querySelectorAll('.tf').forEach(b=>b.onclick=()=>{selectedTf=b.dataset.tf;buildControls();render()})}
function render(){
 if(!latest)return;
 const p=latest.products||{}, signals=latest.signals||{}, prices=latest.prices||{}, pts=latest.price_ts||{};
 $('grid').innerHTML=Object.entries(p).map(([key,item])=>{
   const sym=item.symbol, s=sym?signals[`${key}|${selectedTf}`]:null, price=sym?prices[sym]:null;
   const liveStatus=sym?(s?'Signal ready':'Waiting for candle data'):item.status;
   const logo=item.logo?`<img class="logo" src="${item.logo}" alt="${key} logo" onerror="this.style.display='none'">`:`<div class="logo oilLogo" aria-label="Oil icon">🛢️</div>`;
   const dir=s?.direction||'—';
   return `<article class="card"><div class="cardhead"><div class="asset">${logo}<div><div class="assetname">${item.name}</div><div class="symbol">${sym||key+' · Not listed'}</div></div></div><span class="direction ${dir}">${dir}</span></div>
   <div class="priceLabel" style="margin-top:17px">LIVE TRADED PRICE</div><div class="price">${fmt(price)}</div>
   <div class="stats"><div class="stat"><div class="label">SIGNAL PRICE · LAST FLIP</div><div class="val">${fmt(s?.signal_price)}</div><div class="sub">${s?.signal_time?timeEpoch(s.signal_time):'No confirmed flip in fetched history'}</div></div>
   <div class="stat"><div class="label">TSL PRICE · SUPERTREND LINE</div><div class="val">${fmt(s?.tsl_price)}</div><div class="sub">ATR 10 · Multiplier 2.0</div></div></div>
   <div class="foot"><span>TF: ${selectedTf} · ${liveStatus}</span><span>Tick: ${time(pts[sym])}</span></div>
   ${!sym?`<div class="notice">Exact instrument not found in Delta product list. No substitute price is shown.</div>`:(!s?`<div class="notice">${(latest.candle_errors||{})[key+'|'+selectedTf]||'Waiting for Delta candle data.'}</div>`:'')}
   </article>`;
 }).join('');
 $('dot').className='dot '+(latest.connected?'live':'');
 $('connection').textContent=latest.connected?'Delta public feed connected':(latest.ws_status||'Reconnecting…');
 $('clock').textContent='Server time '+time(latest.server_time);
 if(latest.product_error) $('connection').textContent='Delta data error: '+latest.product_error;
}
async function poll(){try{const r=await fetch('/api/state',{cache:'no-store'});latest=await r.json();render()}catch(e){$('connection').textContent='Dashboard reconnecting…';$('dot').className='dot'} }
buildControls();poll();setInterval(poll,1000);
</script></body></html>
"""

if __name__ == "__main__":
    from werkzeug.serving import run_simple
    port = int(os.environ.get("PORT", "10000"))
    start_workers()
    run_simple("0.0.0.0", port, app, threaded=True)
