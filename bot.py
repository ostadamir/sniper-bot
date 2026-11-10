import time, os, requests, hmac, hashlib, json
import datetime

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TRUETRADE_KEY = os.environ.get("TRUETRADE_KEY")
TRUETRADE_SECRET = os.environ.get("TRUETRADE_SECRET")

TRUETRADE_URL = "https://apiv2.thetruetrade.io"
STATE_FILE = "manager_state.json"
NO_RECORD = "هیچ پوزیشنی بسته نشده است."

PORTFOLIO_TP_PERCENT_DEFAULT = 5.0  
PORTFOLIO_TP_PERCENT_3 = 7.0
PORTFOLIO_TP_PERCENT_2 = 8.5
PORTFOLIO_TP_PERCENT_1 = 10.0
MAX_AUTO_POSITIONS = 3
AUTO_MARGIN_PERCENT = 7.0
AUTO_LEVERAGE = 13

def get_dynamic_tp_percent(num_positions):
    if num_positions == 1: return PORTFOLIO_TP_PERCENT_1
    if num_positions == 2: return PORTFOLIO_TP_PERCENT_2
    if num_positions == 3: return PORTFOLIO_TP_PERCENT_3
    return PORTFOLIO_TP_PERCENT_DEFAULT

def send_telegram(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        res = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
        return res.json().get("result", {}).get("message_id")
    except Exception as e:
        return None

def edit_telegram(message_id, text):
    if not message_id: return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
        res = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id, "text": text, "parse_mode": "HTML"}, timeout=10)
        if res.status_code == 200: return True
        if res.status_code == 400:
            if "message is not modified" in res.text: return True
            if "message to edit not found" in res.text: return False
            return False 
        return True 
    except Exception as e:
        return True

def tt_sign(secret, timestamp, method, uri):
    payload = f"{timestamp}{method.upper()}{uri}"
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()

def tt_request(method, uri, payload=None):
    if not TRUETRADE_KEY or not TRUETRADE_SECRET: return 500, {}
    timestamp = str(int(time.time() * 1000))
    signature = tt_sign(TRUETRADE_SECRET, timestamp, method, uri)
    headers = {"X-API-Key": TRUETRADE_KEY, "X-Timestamp": timestamp, "X-Signature": signature, "Content-Type": "application/json"}
    try:
        if method == "GET":
            res = requests.get(f"{TRUETRADE_URL}{uri}", headers=headers, timeout=10)
        elif method == "PATCH":
            res = requests.patch(f"{TRUETRADE_URL}{uri}", headers=headers, json=payload, timeout=10)
        elif method == "POST":
            res = requests.post(f"{TRUETRADE_URL}{uri}", headers=headers, json=payload, timeout=10)
        else: return 500, {}
        return res.status_code, res.json() if res.text else {}
    except Exception as e:
        return 500, str(e)

def patch_tpsl(pos_id, sl_value, tp_value):
    payload = {"stopLoss": str(round(sl_value, 6)), "takeProfit": str(round(tp_value, 6))}
    st, resp = tt_request("PATCH", f"/futures/positions/{pos_id}/tpsl", payload)
    return st in (200, 201)

def close_position_market(pos_id):
    st, resp = tt_request("POST", f"/futures/positions/{pos_id}/close", payload={"type": "MARKET"})
    return st in (200, 201)

# 🛑 آدرس‌های جدید برای تست
def get_futures_balance():
    endpoints = ["/v1/account/futures/summary", "/api/v1/futures/balance", "/futures/account/balance", "/account/balance"]
    for ep in endpoints:
        st, res = tt_request("GET", ep)
        if st == 200 and isinstance(res, dict):
            send_telegram(f"✅ آدرس <b>{ep}</b> جواب داد!\n<code>{json.dumps(res, ensure_ascii=False)}</code>")
            return 0.0
    send_telegram("❌ متأسفانه هیچ آدرسی جواب نداد.")
    return 0.0

def open_auto_position(symbol, side, margin_usdt, leverage):
    payload = {"symbol": f"{symbol}USDT", "side": side.upper(), "type": "MARKET", "leverage": leverage, "margin": margin_usdt}
    st, res = tt_request("POST", "/futures/orders", payload)
    if st not in (200, 201):
        send_telegram(f"⚠️ خطای باز کردن پوزیشن: Status {st} - {json.dumps(res, ensure_ascii=False)}")
    return st in (200, 201)

def new_state():
    return {"targets": {}, "pending": {}, "offset": 0, "dashboard_id": None, "auto_dashboard_id": None, "last_closed": NO_RECORD}

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f: state = json.load(f)
            for k, v in new_state().items(): state.setdefault(k, v)
            for key, t in state["targets"].items():
                t.setdefault("current_stage", 0); t.setdefault("closed", False); t.setdefault("is_auto", False); t.setdefault("limit_stage", 99); t.setdefault("fail_count", 0)
            return state
        except Exception: pass
    return new_state()

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=4)

USAGE = ("فرمت دستور:\n/new SYMBOL SIDE ENTRY SL TP1 TP2 TP3 [TP4] [RF|SAFE]\n/cancel SYMBOL SIDE")

def handle_cancel(state, parts):
    symbol = parts[1].upper().replace("USDT", "")
    side = parts[2].upper()
    for k in list(state["targets"].keys()):
        tg = state["targets"][k]
        if tg.get("symbol") == symbol and tg.get("side") == side: del state["targets"][k]
    pos_key = f"{symbol}_{side}"
    if pos_key in state["pending"]: del state["pending"][pos_key]
    send_telegram(f"🛑 مدیریت {pos_key} لغو شد.")

def handle_new(state, parts):
    limit_stage = 99
    if len(parts) > 0 and parts[-1].upper() in ("RF", "SAFE"):
        mode = parts[-1].upper()
        if mode == "RF": limit_stage = 1
        elif mode == "SAFE": limit_stage = 2
        parts = parts[:-1]

    if len(parts) == 8:
        try:
            tp3 = float(parts[7]); tp2 = float(parts[6])
            tp4 = round(tp3 + (tp3 - tp2), 8); parts.append(str(tp4))
        except ValueError: pass

    if len(parts) != 9:
        send_telegram("❌ فرمت اشتباه است.\n" + USAGE); return

    symbol = parts[1].upper().replace("USDT", ""); sd = parts[2].upper(); pos_key = f"{symbol}_{sd}"
    try: e = float(parts[3]); s = float(parts[4]); tps = [float(x) for x in parts[5:9]]
    except ValueError: return

    new_sig = {"symbol": symbol, "side": sd, "entry": e, "sl": s, "tp1": tps[0], "tp2": tps[1], "tp3": tps[2], "tp4": tps[3], "current_stage": 0, "closed": False, "is_auto": False, "limit_stage": limit_stage, "fail_count": 0}
    for ex in state["pending"].get(pos_key, []):
        if ex["entry"] == e and ex["sl"] == s: return
    state["pending"].setdefault(pos_key, []).append(new_sig)
    send_telegram(f"✅ سیگنال {pos_key} ثبت شد.")

def handle_auto(state, parts):
    open_auto_count = sum(1 for t in state["targets"].values() if t.get("is_auto", False))
    if open_auto_count >= MAX_AUTO_POSITIONS:
        send_telegram(f"⚠️ سقف {MAX_AUTO_POSITIONS} پوزیشن خودکار پر است."); return

    if len(parts) < 10: return

    symbol = parts[1].upper().replace("USDT", ""); sd = parts[2].upper()
    try: e = float(parts[3]); s = float(parts[4]); tps = [float(x) for x in parts[5:10]]
    except ValueError: return

    balance = get_futures_balance()
    if balance <= 0: return
    
    margin = balance * (AUTO_MARGIN_PERCENT / 100.0)
    if open_auto_position(symbol, sd, margin, AUTO_LEVERAGE):
        pos_key = f"{symbol}_{sd}"
        new_sig = {"symbol": symbol, "side": sd, "entry": e, "sl": s, "tp1": tps[0], "tp2": tps[1], "tp3": tps[2], "tp4": tps[3], "tp5": tps[4], "current_stage": 0, "closed": False, "is_auto": True, "limit_stage": 99, "fail_count": 0}
        state["pending"].setdefault(pos_key, []).append(new_sig)
        send_telegram(f"🤖 پوزیشن خودکار {symbol} باز شد.")

def check_telegram(state):
    offset = state.get("offset", 0)
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", params={"offset": offset, "timeout": 5}, timeout=10).json()
        if not r.get("ok"): return
        for u in r.get("result", []):
            state["offset"] = u["update_id"] + 1
            msg = u.get("message")
            if not msg or "text" not in msg or str(msg.get("chat", {}).get("id")) != str(TELEGRAM_CHAT_ID): continue
            for line in msg["text"].split('\n'):
                line = line.strip()
                if not line: continue
                parts = line.split()
                if line.startswith("/cancel"): handle_cancel(state, parts)
                elif line.startswith("/new"): handle_new(state, parts)
                elif line.startswith("/auto"): handle_auto(state, parts)
    except Exception as e: print("TG Check Error:", e)

def get_progress_bar(current, entry, target):
    if entry == target: return "░░░░░░░░░░"
    if target > entry: pct = max(0.0, min(1.0, (current - entry) / (target - entry)))
    else: pct = max(0.0, min(1.0, (entry - current) / (entry - target)))
    filled = int(pct * 10)
    return "█" * filled + "░" * (10 - filled)

def next_auto_stage(t, is_long, entry, price):
    tp1, tp2, tp3, tp4, tp5 = t["tp1"], t["tp2"], t["tp3"], t["tp4"], t["tp5"]
    g = 1 if is_long else -1; d1 = abs(tp1 - entry)
    def hit(level): return price >= level if is_long else price <= level
    if t["current_stage"] == 0 and hit(tp2): return 1, entry + g * d1 * 0.10, "TP2 (قفل ۱۰٪)"
    elif t["current_stage"] == 1 and hit(entry + g * abs(tp3 - entry) * 0.5): return 2, entry + g * d1 * 0.5, "نصفه راه TP3"
    elif t["current_stage"] == 2 and hit(tp3): return 3, tp1, "TP3 (قفل TP1)"
    elif t["current_stage"] == 3 and hit(tp4): return 4, tp1 + g * abs(tp2 - tp1) * 0.5, "TP4 (قفل TP1.5)"
    elif t["current_stage"] == 4 and hit(tp5): return 5, tp4, "TP5 (کلوز)"
    return t["current_stage"], 0, ""

def manage_positions(state):
    status, response = tt_request("GET", "/futures/positions")
    if status != 200 or not isinstance(response, dict): return
    
    items = response.get("items", [])
    open_positions = [p for p in items if p.get("status") == "OPENED"]
    closed_positions = [p for p in items if p.get("status") == "CLOSED"]
    open_positions.sort(key=lambda p: p.get("createdAt", ""))
    open_keys = set()
    
    manual_parts = ["📊 <b>داشبورد مدیریت دستی</b>"]
    auto_parts = ["🤖 <b>داشبورد خودکار (آدم)</b>"]
    manual_pnl = 0.0; manual_margin = 0.0 
    manual_unmanaged = []; auto_unmanaged = []

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
            else:
                manual_unmanaged.append(key_base); continue

        t = state["targets"].get(pos_id)
        if not t or t.get("closed", False): continue
        is_auto = t.get("is_auto", False)
        price = float(pos.get("markPrice") or 0)
        if price <= 0: continue
        cur_sl = float(pos.get("stopLoss") or 0); cur_tp = float(pos.get("takeProfit") or 0)
        pnl = float(pos.get("unrealizedPnL") or 0); margin = float(pos.get("initialMargin") or 0); size = float(pos.get("size") or 0)
        if margin <= 0 or size <= 0: continue
        is_long = (side == "LONG"); entry = t["entry"]; leverage = int(round((size * entry) / margin)); roi = (pnl / margin) * 100

        if is_auto:
            stage, new_sl, reason = next_auto_stage(t, is_long, entry, price)
            if stage > t["current_stage"]:
                if stage == 5: close_position_market(pos_id)
                elif reason:
                    payload_sl = max(cur_sl, new_sl) if is_long else min(cur_sl, new_sl) if cur_sl > 0 else new_sl
                    if patch_tpsl(pos_id, payload_sl, t["tp5"]):
                        cur_sl = payload_sl; t["current_stage"] = stage
            card = f"🤖 <b>{key_base}</b> | سود: {pnl:+.2f}$ | استاپ: {cur_sl:.5f}"
            auto_parts.append(card)
            continue

        manual_pnl += pnl; manual_margin += margin
        card = f"👤 <b>{key_base}</b> | سود: {pnl:+.2f}$ | استاپ: {cur_sl:.5f}"
        manual_parts.append(card)

    for key in list(state["targets"].keys()):
        if key in open_keys or state["targets"][key].get("closed", False): continue
        match = [p for p in closed_positions if str(p.get("id")) == key]
        if not match: continue
        del state["targets"][key]
        send_telegram(f"🏁 پوزیشن {key} بسته شد.")

    if manual_margin > 0:
        target_profit = manual_margin * (get_dynamic_tp_percent(len([t for t in state["targets"].values() if not t.get("is_auto", False)])) / 100.0)
        if manual_pnl >= target_profit:
            send_telegram("🚀 هدف سود کل (دستی) پر شد. در حال بستن...")
            for pos in open_positions:
                if not state["targets"].get(str(pos["id"]), {}).get("is_auto", False): close_position_market(str(pos["id"]))
            return 

    if not [p for p in open_positions if not state["targets"].get(str(p["id"]), {}).get("is_auto", False)]: manual_parts.append("پوزیشن دستی باز نیست. ⏳")
    else: manual_parts.insert(1, f"📈 سود کل: {manual_pnl:+.2f} USDT")
    if manual_unmanaged: manual_parts.append(f"⚠️ بدون سیگنال: {', '.join(manual_unmanaged)}")
    
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3, minutes=30)
    manual_parts.append(f"🕒 آپدیت: {now.strftime('%H:%M')}")
    
    if not [p for p in open_positions if state["targets"].get(str(p["id"]), {}).get("is_auto", False)]: auto_parts.append("پوزیشن خودکار باز نیست. ⏳")
    if auto_unmanaged: auto_parts.append(f"⚠️ بدون سیگنال: {', '.join(auto_unmanaged)}")
    auto_parts.append(f"🕒 آپدیت: {now.strftime('%H:%M')}")

    if state.get("dashboard_id"): edit_telegram(state["dashboard_id"], "\n".join(manual_parts))
    else: state["dashboard_id"] = send_telegram("\n".join(manual_parts))

    if state.get("auto_dashboard_id"): edit_telegram(state["auto_dashboard_id"], "\n".join(auto_parts))
    elif len(auto_parts) > 3: state["auto_dashboard_id"] = send_telegram("\n".join(auto_parts))

def run():
    print("Checking...")
    state = load_state()
    check_telegram(state)
    manage_positions(state)
    save_state(state)
    print("Done.")

if __name__ == "__main__":
    run()
