import time, os, requests, hmac, hashlib, json
import datetime, html, re

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TRUETRADE_KEY = os.environ.get("TRUETRADE_KEY")
TRUETRADE_SECRET = os.environ.get("TRUETRADE_SECRET")

DRY_RUN = os.environ.get("DRY_RUN", "1") == "1"

TRUETRADE_URL = "https://apiv2.thetruetrade.io"
STATE_FILE = "manager_state.json"
QUEUE_FILE = "pending_signals.txt"

PORTFOLIO_TP_PERCENT_1 = 10.0  
PORTFOLIO_TP_PERCENT_2 = 4.0   
PORTFOLIO_TP_PERCENT_3 = 2.8   
PORTFOLIO_TP_PERCENT_4 = 3.8   
PORTFOLIO_TP_PERCENT_5 = 3.9   
PORTFOLIO_TP_PERCENT_DEFAULT = 3.9

MAX_AUTO_POSITIONS = 3
AUTO_MARGIN_PERCENT = 7.0
AUTO_LEVERAGE = 13
MAX_ENTRY_DEV = 0.01
MIN_TP_DIST = 0.003      
MIN_SL_DIST = 0.001      

USAGE = ("فرمت دستور:\n/new SYMBOL SIDE ENTRY SL TP1 TP2 TP3 TP4 [RF|SAFE]\n/cancel SYMBOL SIDE\n/auto SYMBOL SIDE ENTRY SL TP1 TP2 TP3 TP4 TP5")

def _norm(x): return str(x or "").strip().lower()

def strip_tags(t):
    return html.unescape(re.sub(r"<[^>]+>", "", str(t)))

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        res = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
        if res.status_code == 200: return res.json().get("result", {}).get("message_id")
        print(f"TG HTML Error: {res.status_code} | {res.text}", flush=True)
        if res.status_code == 400: 
            r2 = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": strip_tags(text)[:4000]}, timeout=10)
            if r2.status_code == 200: return r2.json().get("result", {}).get("message_id")
            print(f"TG Plain Error: {r2.status_code} | {r2.text}", flush=True)
    except Exception as e:
        print("TG Error:", repr(e), flush=True)
    return None

def notify(msg):
    print("NOTIFY:", strip_tags(msg)[:300].replace("\n", " | "), flush=True)
    return send_telegram(msg)

def edit_telegram(message_id, text):
    if not message_id: return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
    try:
        res = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id, "text": text, "parse_mode": "HTML"}, timeout=10)
        if res.status_code == 200: return True
        if res.status_code == 400:
            if "message is not modified" in res.text: return True
            if "message to edit not found" in res.text: return False 
            r2 = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id, "text": strip_tags(text)[:4000]}, timeout=10)
            return r2.status_code == 200
        return True
    except: return False

# ================= TrueTrade API =================
def tt_sign(secret, timestamp, method, uri):
    payload = f"{timestamp}{method.upper()}{uri}"
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()

def tt_request(method, uri, payload=None):
    if not TRUETRADE_KEY or not TRUETRADE_SECRET: 
        print("Missing TT Keys", flush=True)
        return 500, {}
    timestamp = str(int(time.time() * 1000))
    signature = tt_sign(TRUETRADE_SECRET, timestamp, method, uri)
    headers = {"X-API-Key": TRUETRADE_KEY, "X-Timestamp": timestamp, "X-Signature": signature, "Content-Type": "application/json"}
    try:
        if method == "GET": res = requests.get(f"{TRUETRADE_URL}{uri}", headers=headers, timeout=15)
        elif method == "PATCH": res = requests.patch(f"{TRUETRADE_URL}{uri}", headers=headers, json=payload, timeout=15)
        elif method == "POST": res = requests.post(f"{TRUETRADE_URL}{uri}", headers=headers, json=payload, timeout=15)
        else: return 500, {}
        return res.status_code, res.json() if res.text else {}
    except Exception as e: 
        print(f"TT Request Error: {e}", flush=True)
        return 500, str(e)

def patch_tpsl(pos_id, sl_value, tp_value):
    if DRY_RUN: return True 
    payload = {"stopLoss": str(round(sl_value, 6)), "takeProfit": str(round(tp_value, 6))}
    st, _ = tt_request("PATCH", f"/futures/positions/{pos_id}/tpsl", payload)
    return st in (200, 201)

def close_position_market(pos_id):
    if DRY_RUN: return True
    st, _ = tt_request("POST", f"/futures/positions/{pos_id}/close", payload={"type": "MARKET"})
    return st in (200, 201)

def extract_list(res):
    if isinstance(res, list): return res
    if isinstance(res, dict):
        for k in ("items", "data", "assets", "result", "balances"):
            if isinstance(res.get(k), list): return res[k]
    return []

def extract_id(res):
    if not isinstance(res, dict): return None
    for src in (res, res.get("data"), res.get("item"), res.get("position")):
        if isinstance(src, dict):
            for k in ("id", "positionId"):
                if src.get(k) is not None: return str(src[k])
    return None

def fmt_num(x): return (f"{float(x):.10f}".rstrip("0").rstrip(".")) or "0"

def get_futures_balance():
    st, res = tt_request("GET", "/futures/assets")
    if st != 200: return None
    for a in extract_list(res):
        if isinstance(a, dict) and _norm(a.get("symbol")) == "usdt":
            try: return float(a.get("totalAssets") or 0), float(a.get("availableBalance") or 0)
            except ValueError: pass
    return None

def get_market_price(symbol):
    s = symbol.upper()
    tries = [
        (f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={s}-USDT", lambda j: j["data"]["price"]),
        (f"https://www.okx.com/api/v5/market/ticker?instId={s}-USDT-SWAP", lambda j: j["data"][0]["last"]),
        (f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={s}USDT", lambda j: j["result"]["list"][0]["lastPrice"]),
    ]
    for url, pick in tries:
        try:
            r = requests.get(url, timeout=8)
            if r.status_code == 200:
                p = float(pick(r.json()))
                if p > 0: return p
        except: pass
    return None

def check_against_market(side, price, e, sl, tps, leverage):
    tp_last, tp1 = tps[-1], tps[0]
    if side == "LONG":
        if sl >= price * (1 - MIN_SL_DIST): return "استاپ باید پایین‌تر از قیمت باشد"
        if tp_last <= price * (1 + MIN_TP_DIST): return "تارگت آخر باید بالاتر از قیمت باشد"
        if price >= tp1: return "قیمت از TP1 رد شده (سیگنال دیر شده)"
    else:
        if sl <= price * (1 + MIN_SL_DIST): return "استاپ باید بالاتر از قیمت باشد"
        if tp_last >= price * (1 - MIN_TP_DIST): return "تارگت آخر باید پایین‌تر از قیمت باشد"
        if price <= tp1: return "قیمت از TP1 رد شده (سیگنال دیر شده)"
    if abs(price - sl) / price > 0.6 / leverage: return "استاپ برای این لوریج خیلی دور است"
    return None

def valid_signal(side, e, s, tps, leverage):
    if side == "LONG": ok = s < e < tps[0] and all(tps[i] < tps[i + 1] for i in range(len(tps) - 1))
    elif side == "SHORT": ok = s > e > tps[0] and all(tps[i] > tps[i + 1] for i in range(len(tps) - 1))
    else: return "side"
    if not ok: return "order"
    if abs(e - s) / e > 0.6 / leverage: return "sl_too_far"
    return None

def get_dynamic_tp_percent(num_positions):
    if num_positions == 1: return PORTFOLIO_TP_PERCENT_1
    if num_positions == 2: return PORTFOLIO_TP_PERCENT_2
    if num_positions == 3: return PORTFOLIO_TP_PERCENT_3
    if num_positions == 4: return PORTFOLIO_TP_PERCENT_4
    if num_positions == 5: return PORTFOLIO_TP_PERCENT_5
    return PORTFOLIO_TP_PERCENT_DEFAULT

def open_auto_position(symbol, side, margin_usdt, leverage, sl, tp):
    if DRY_RUN:
        notify(f"🧪 DRY-RUN: می‌خواستم {symbol} {side} با مارجین {margin_usdt:.2f}$ باز کنم.")
        return None
        
    payload = {"symbol": f"{symbol}USDT", "side": side.upper(), "tradeType": "MARKET", "leverage": leverage, "cost": fmt_num(round(margin_usdt, 2)), "walletType": "debit", "stopLoss": fmt_num(sl), "takeProfit": fmt_num(tp)}
    st, res = tt_request("POST", "/futures/positions", payload)
    if st not in (200, 201):
        errs = res.get("errors", [])
        msg = errs[0].get("message") if errs else str(res)
        notify(f"⚠️ صرافی رد کرد ({st}):\n<code>{html.escape(msg)}</code>")
        return None
    return res if res else {}

# ================= State & Handlers =================
def new_state(): return {"targets": {}, "pending": {}, "offset": 0, "dashboard_id": None, "auto_dashboard_id": None}

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f: state = json.load(f)
            for k, v in new_state().items(): state.setdefault(k, v)
            for key, t in state["targets"].items():
                t.setdefault("current_stage", 0); t.setdefault("closed", False); t.setdefault("is_auto", False); t.setdefault("limit_stage", 99); t.setdefault("fail_count", 0)
            return state
        except: pass
    return new_state()

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=4)

def handle_cancel(state, parts):
    if len(parts) < 3: return
    symbol = parts[1].upper().replace("USDT", ""); side = parts[2].upper()
    for k in list(state["targets"].keys()):
        tg = state["targets"][k]
        if tg.get("symbol") == symbol and tg.get("side") == side: del state["targets"][k]
    if f"{symbol}_{side}" in state["pending"]: del state["pending"][f"{symbol}_{side}"]
    notify(f"🛑 مدیریت {symbol}_{side} لغو شد.")

def handle_new(state, parts):
    limit_stage = 99
    if parts[-1].upper() in ("RF", "SAFE"):
        limit_stage = 1 if parts[-1].upper() == "RF" else 2
        parts = parts[:-1]

    if len(parts) == 8:
        try: parts.append(str(round(float(parts[7]) + (float(parts[7]) - float(parts[6])), 8)))
        except: pass

    if len(parts) != 9:
        notify("❌ فرمت اشتباه است.\n" + USAGE); return

    symbol = parts[1].upper().replace("USDT", ""); sd = parts[2].upper(); pos_key = f"{symbol}_{sd}"
    try: e = float(parts[3]); s = float(parts[4]); tps = [float(x) for x in parts[5:9]]
    except ValueError: return

    new_sig = {"symbol": symbol, "side": sd, "entry": e, "sl": s, "tp1": tps[0], "tp2": tps[1], "tp3": tps[2], "tp4": tps[3], "current_stage": 0, "closed": False, "is_auto": False, "limit_stage": limit_stage, "fail_count": 0}
    for ex in state["pending"].get(pos_key, []):
        if ex["entry"] == e and ex["sl"] == s: return
    state["pending"].setdefault(pos_key, []).append(new_sig)
    notify(f"✅ سیگنال دستی {pos_key} ثبت شد." + (f" ({parts[-1]})" if limit_stage < 99 else ""))

def handle_auto(state, parts):
    if len(parts) < 10:
        notify("❌ فرمت: /auto SYMBOL SIDE ENTRY SL TP1 TP2 TP3 TP4 TP5"); return
        
    if sum(1 for t in state["targets"].values() if t.get("is_auto")) >= MAX_AUTO_POSITIONS:
        notify(f"⚠️ سقف {MAX_AUTO_POSITIONS} پوزیشن خودکار پر است."); return

    symbol = parts[1].upper().replace("USDT", ""); sd = parts[2].upper()
    try: e = float(parts[3]); s = float(parts[4]); tps = [float(x) for x in parts[5:10]]
    except ValueError: notify("❌ اعداد معتبر نیستند."); return
        
    err = valid_signal(sd, e, s, tps, AUTO_LEVERAGE)
    if err: notify(f"❌ /auto {symbol}_{sd} رد شد ({err})."); return

    st, resp = tt_request("GET", "/futures/positions")
    if st != 200 or not isinstance(resp, dict): notify("⚠️ لیست پوزیشن‌ها خوانده نشد."); return
    
    for p in resp.get("items", []):
        if p.get("status") == "OPENED" and p.get("symbol") == f"{symbol}USDT" and p.get("side") == sd:
            notify(f"⚠️ {symbol}_{sd} همین الان باز است."); return

    price = get_market_price(symbol)
    if price is None: notify(f"⚠️ قیمت بازار {symbol} گرفته نشد."); return
    
    why = check_against_market(sd, price, e, s, tps, AUTO_LEVERAGE)
    if why: notify(f"⛔ /auto {symbol}_{sd} رد شد: {why}"); return

    bal = get_futures_balance()
    if bal is None: notify("⚠️ خطا: موجودی فیوچرز خوانده نشد."); return
    
    balance, available = bal
    target_margin = balance * AUTO_MARGIN_PERCENT / 100.0
    if available < target_margin or target_margin < 1.0:
        notify(f"⚠️ مارجین ناکافی (نیاز {target_margin:.2f}$, آزاد {available:.2f}$)."); return

    res = open_auto_position(symbol, sd, target_margin, AUTO_LEVERAGE, s, tps[4])
    if res is None: return
    
    new_sig = {"symbol": symbol, "side": sd, "entry": e, "sl": s, "tp1": tps[0], "tp2": tps[1], "tp3": tps[2], "tp4": tps[3], "tp5": tps[4], "current_stage": 0, "closed": False, "is_auto": True, "limit_stage": 99, "fail_count": 0}
    pos_id = extract_id(res)
    if pos_id:
        state["targets"][pos_id] = new_sig
        notify(f"🤖 پوزیشن خودکار {symbol}_{sd} باز شد (margin {target_margin:.2f}$).")
    else:
        state["pending"].setdefault(f"{symbol}_{sd}", []).append(new_sig)

def process_queue(state):
    if not os.path.exists(QUEUE_FILE): return
    with open(QUEUE_FILE, "r") as f: lines = f.readlines()
    os.remove(QUEUE_FILE)
    print(f"process_queue: {len(lines)} line(s)", flush=True)
    for line in lines:
        if line.strip().startswith("/auto"):
            try: handle_auto(state, line.strip().split())
            except Exception as e: notify(f"⚠️ خطا در پردازش سیگنال: {html.escape(repr(e))}")

def check_telegram(state):
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", params={"offset": state.get("offset", 0), "timeout": 5}, timeout=10).json()
        print(f"📩 TG Updates Received: {len(r.get('result', []))}", flush=True)
        if not r.get("ok"): return
        for u in r.get("result", []):
            state["offset"] = u["update_id"] + 1
            msg = u.get("message")
            if not msg or "text" not in msg or str(msg.get("chat", {}).get("id")) != str(TELEGRAM_CHAT_ID): continue
            
            for line in msg["text"].split('\n'):
                if not line.strip(): continue
                parts = line.strip().split()
                cmd = parts[0].split("@")[0].lower()
                try:
                    if cmd == "/cancel": handle_cancel(state, parts)
                    elif cmd == "/new": handle_new(state, parts)
                    elif cmd == "/auto": handle_auto(state, parts)
                    elif cmd == "/balance":
                        bal = get_futures_balance()
                        notify(f"💰 موجودی: {bal[0]:.2f}$ (آزاد: {bal[1]:.2f}$)" if bal else "⚠️ خطای خواندن موجودی")
                    elif cmd.startswith("/"): notify(f"❓ ناشناخته: {cmd}")
                except Exception as e: notify(f"⚠️ خطا در {cmd}: {html.escape(repr(e))}")
        if r.get("result"): requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", params={"offset": state["offset"], "limit": 1, "timeout": 0})
    except Exception as e: print("TG Error:", e, flush=True)

# ================= Logic & Dashboard =================
def next_stage(t, is_long, entry, price, roi, price_10, cur_sl, cur_tp, max_stage):
    d1, d2, d3, d4 = abs(t["tp1"] - entry), abs(t["tp2"] - t["tp1"]), abs(t["tp3"] - t["tp2"]), abs(t["tp4"] - t["tp3"])
    g = 1 if is_long else -1
    p40, p22, p70, p80 = entry + g*d1*0.4, entry + g*d1*0.22, entry + g*d1*0.7, entry + g*d1*0.8
    h2, h3, h4 = t["tp1"] + g*d2*0.5, t["tp2"] + g*d3*0.5, t["tp3"] + g*d4*0.5
    def hit(lvl): return price >= lvl if is_long else price <= lvl
    chain = [(0, "roi", 10.0, entry, ""), (1, "roi", 20.0, price_10, ""), (2, "price", p40, p22, ""), (3, "price", p70, p40, ""), (4, "price", t["tp1"], p80, ""), (5, "price", h2, t["tp1"], ""), (6, "price", t["tp2"], h2, ""), (7, "price", h3, t["tp2"], ""), (8, "price", t["tp3"], h3, ""), (9, "price", h4, t["tp3"], ""), (10, "price", t["tp4"], h4, "")]
    stage, new_sl, new_tp = t["current_stage"], cur_sl, cur_tp
    for st, kind, level, sl_to, _ in chain:
        if stage == st and stage < max_stage:
            if (roi >= level) if kind == "roi" else hit(level):
                new_sl, stage = sl_to, st + 1
    if stage >= 11 and stage < max_stage and hit(cur_tp):
        new_tp, new_sl, stage = (cur_tp + d4 if is_long else cur_tp - d4), t["tp4"], stage + 1
    return stage, new_sl, new_tp

def next_auto_stage(t, is_long, entry, price):
    g = 1 if is_long else -1; d1 = abs(t["tp1"] - entry)
    def hit(lvl): return price >= lvl if is_long else price <= lvl
    if t["current_stage"] == 0 and hit(t["tp2"]): return 1, entry + g * d1 * 0.05
    elif t["current_stage"] == 1 and hit(entry + g * abs(t["tp3"] - entry) * 0.5): return 2, entry + g * d1 * 0.5
    elif t["current_stage"] == 2 and hit(t["tp3"]): return 3, t["tp1"]
    elif t["current_stage"] == 3 and hit(t["tp4"]): return 4, t["tp1"] + g * abs(t["tp2"] - t["tp1"]) * 0.5
    elif t["current_stage"] == 4 and hit(t["tp5"]): return 5, t["tp4"]
    return t["current_stage"], 0

def manage_positions(state):
    status, response = tt_request("GET", "/futures/positions")
    if status != 200 or not isinstance(response, dict):
        notify(f"⚠️ خطا در دریافت لیست پوزیشن‌ها ({status}): {str(response)[:100]}")
        return
    
    items = response.get("items", [])
    print(f"📊 TT Positions Fetched: {len(items)}", flush=True)
    
    open_positions = [p for p in items if p.get("status") == "OPENED"]
    closed_positions = [p for p in items if p.get("status") == "CLOSED"]
    open_keys = set()
    
    manual_parts = ["📊 <b>داشبورد دستی</b>"]
    auto_parts = ["🤖 <b>داشبورد خودکار</b>"]
    manual_pnl, manual_margin = 0.0, 0.0 
    manual_unmanaged = []

    for pos in open_positions:
        pos_id = str(pos["id"]); symbol = pos["symbol"].replace("USDT", ""); side = pos["side"]; key_base = f"{symbol}_{side}"
        open_keys.add(pos_id)

        if pos_id not in state["targets"]:
            queue = state["pending"].get(key_base)
            if queue:
                sig = queue.pop(0)
                if not queue: del state["pending"][key_base]
                sig["symbol"], sig["side"] = symbol, side
                state["targets"][pos_id] = sig
                notify(f"🔗 اتصال موفق!\nپوزیشن {key_base} متصل شد.")
            else: manual_unmanaged.append(key_base); continue

        t = state["targets"].get(pos_id)
        if not t or t.get("closed", False): continue
        
        is_auto = t.get("is_auto", False)
        price = float(pos.get("markPrice") or 0)
        cur_sl = float(pos.get("stopLoss") or 0)
        pnl = float(pos.get("unrealizedPnL") or 0)
        margin = float(pos.get("initialMargin") or 0)
        size = float(pos.get("size") or 0)
        
        if price <= 0: continue
        if margin <= 0 or size <= 0: continue
            
        is_long = (side == "LONG")
        entry = float(pos.get("entryPrice") or 0)
        if entry <= 0: entry = price - pnl / size if is_long else price + pnl / size
        if entry > 0: t["entry"] = entry
        entry = t["entry"]
        roi = (pnl / margin) * 100
        tol = max(1e-8, price * 1e-5)

        if is_auto:
            stage, new_sl = next_auto_stage(t, is_long, entry, price)
            if stage > t["current_stage"]:
                if stage == 5: close_position_market(pos_id)
                elif new_sl > 0:
                    payload_sl = max(cur_sl, new_sl) if is_long else min(cur_sl, new_sl) if cur_sl > 0 else new_sl
                    if patch_tpsl(pos_id, payload_sl, t["tp5"]): cur_sl = payload_sl; t["current_stage"] = stage
            emoji = "🟢" if roi >= 0 else "🔴"
            card = f"{emoji} <b>{key_base}</b> | 🤖 (سود: {roi:+.1f}%) | استاپ: {cur_sl:.4f}"
            if len("\n".join(auto_parts)) + len(card) < 3500: auto_parts.append(card)
            continue

        manual_pnl += pnl; manual_margin += margin
        price_10 = entry + ((margin * 0.10) / size if is_long else -(margin * 0.10) / size)
        
        sl0 = t["sl"]
        if cur_sl == 0 or (t["current_stage"] == 0 and (cur_sl < sl0 - tol if is_long else cur_sl > sl0 + tol)):
            restore = sl0 if t["current_stage"] == 0 else (price_10 if t["current_stage"] >= 2 else entry)
            if patch_tpsl(pos_id, restore, t["tp4"]): cur_sl = restore

        stage, new_sl, new_tp = next_stage(t, is_long, entry, price, roi, price_10, cur_sl, t.get("limit_stage", 99))
        if new_sl != cur_sl or new_tp != t.get("cur_tp", 0):
            final_sl = max(cur_sl, new_sl) if is_long else min(cur_sl, new_sl) if cur_sl > 0 else new_sl
            if patch_tpsl(pos_id, final_sl, new_tp): 
                cur_sl = final_sl; t["current_stage"] = stage; t["cur_tp"] = new_tp

        emoji = "🟢" if roi >= 0 else "🔴"
        card = f"{emoji} <b>{key_base}</b> | ⚙️ (سود: {roi:+.1f}%) | استاپ: {cur_sl:.4f}"
        if len("\n".join(manual_parts)) + len(card) < 3500: manual_parts.append(card)

    for key in list(state["targets"].keys()):
        if key in open_keys or state["targets"][key].get("closed"): continue
        match = [p for p in closed_positions if str(p.get("id")) == key]
        if match:
            prefix = "🤖" if state["targets"][key].get("is_auto") else "🏁"
            notify(f"{prefix} بسته شد: {match[0].get('symbol','').replace('USDT','')} (PnL: {float(match[0].get('realizedPnL') or 0):+.2f}$)")
            del state["targets"][key]

    if manual_margin > 0:
        target_pct = get_dynamic_tp_percent(len([t for t in state["targets"].values() if not t.get("is_auto")]))
        if manual_pnl >= (manual_margin * target_pct / 100.0):
            notify(f"🚀 هدف سود کل (دستی) پر شد ({target_pct}%). در حال بستن...")
            for pos in open_positions:
                if not state["targets"].get(str(pos["id"]), {}).get("is_auto"): close_position_market(str(pos["id"]))
            return 

    if not manual_parts[1:]: manual_parts.append("پوزیشن دستی باز نیست.")
    else: manual_parts.insert(1, f"📈 سود کل: {manual_pnl:+.2f} USDT")
    
    if manual_unmanaged: 
        unmanaged_str = ', '.join(manual_unmanaged[:5])
        if len(manual_unmanaged) > 5: unmanaged_str += f" + {len(manual_unmanaged)-5} more"
        manual_parts.append(f"⚠️ بدون سیگنال: {unmanaged_str}")
        
    if not auto_parts[1:]: auto_parts.append("پوزیشن خودکار باز نیست.")

    if state.get("dashboard_id"):
        if not edit_telegram(state["dashboard_id"], "\n".join(manual_parts)):
            state["dashboard_id"] = send_telegram("\n".join(manual_parts))
    else:
        state["dashboard_id"] = send_telegram("\n".join(manual_parts))

    if state.get("auto_dashboard_id"):
        if not edit_telegram(state["auto_dashboard_id"], "\n".join(auto_parts)):
            state["auto_dashboard_id"] = send_telegram("\n".join(auto_parts))
    else:
        state["auto_dashboard_id"] = send_telegram("\n".join(auto_parts))

def run():
    print("Checking...", flush=True)
    state = load_state()
    process_queue(state)
    check_telegram(state)
    manage_positions(state)
    save_state(state)
    print("Done.", flush=True)

if __name__ == "__main__":
    run()
