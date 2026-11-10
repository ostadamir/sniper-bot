import time, os, requests, hmac, hashlib, json
import datetime

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TRUETRADE_KEY = os.environ.get("TRUETRADE_KEY")
TRUETRADE_SECRET = os.environ.get("TRUETRADE_SECRET")

TRUETRADE_URL = "https://apiv2.thetruetrade.io"
STATE_FILE = "manager_state.json"
NO_RECORD = "هیچ پوزیشنی بسته نشده است."

# ===== تنظیمات سود کل پرتفوی (پویا) =====
PORTFOLIO_TP_PERCENT_DEFAULT = 5.0  
PORTFOLIO_TP_PERCENT_3 = 7.0
PORTFOLIO_TP_PERCENT_2 = 8.5
PORTFOLIO_TP_PERCENT_1 = 10.0

# ===== تنظیمات پوزیشن‌های خودکار (آدم) =====
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
        print("TG Send Error:", e)
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
        print("TG Edit Error:", e)
        return True

# ================= TrueTrade =================
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
    if st not in (200, 201):
        print(f"!!! TrueTrade API Error: Status {st} - Response: {resp}")
    return st in (200, 201)

def close_position_market(pos_id):
    st, resp = tt_request("POST", f"/futures/positions/{pos_id}/close", payload={"type": "MARKET"})
    if st not in (200, 201):
        print(f"!!! TrueTrade Close API Error: {st} - {resp}")
    return st in (200, 201)

def get_futures_balance():
    st, res = tt_request("GET", "/futures/balance")
    if st == 200 and isinstance(res, dict):
        return float(res.get("availableBalance") or 0)
    else:
        # 🛑 چاپ خطای دقیق برای دیباگ
        send_telegram(f"⚠️ API Balance Error: Status {st} - Res: {res}")
    return 0.0

def open_auto_position(symbol, side, margin_usdt, leverage):
    payload = {
        "symbol": f"{symbol}USDT",
        "side": side.upper(),
        "type": "MARKET",
        "margin": margin_usdt,
        "leverage": leverage
    }
    st, res = tt_request("POST", "/futures/positions", payload)
    if st not in (200, 201):
        # 🛑 چاپ خطای دقیق برای دیباگ
        send_telegram(f"⚠️ API Open Error: Status {st} - Res: {res}")
        print(f"!!! Error opening auto position: {st} - {res}")
    return st in (200, 201)

# ================= State =================
def new_state():
    return {"targets": {}, "pending": {}, "offset": 0, "dashboard_id": None, "auto_dashboard_id": None, "last_closed": NO_RECORD}

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            for k, v in new_state().items(): state.setdefault(k, v)
            for key, t in state["targets"].items():
                t.setdefault("current_stage", 0)
                t.setdefault("closed", False)
                t.setdefault("is_auto", False)
                t.setdefault("limit_stage", 99)
                t.setdefault("fail_count", 0)
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
            tp4 = round(tp3 + (tp3 - tp2), 8)
            parts.append(str(tp4))
        except ValueError: pass

    if len(parts) != 9:
        send_telegram("❌ فرمت اشتباه است.\n" + USAGE)
        return

    symbol = parts[1].upper().replace("USDT", "")
    sd = parts[2].upper()
    pos_key = f"{symbol}_{sd}"
    try:
        e = float(parts[3]); s = float(parts[4]); tps = [float(x) for x in parts[5:9]]
    except ValueError:
        send_telegram("❌ خطای فرمت: اعداد معتبر نیستند.")
        return
    ok = sd in ("LONG", "SHORT")
    if ok and sd == "LONG": ok = s < e < tps[0] < tps[1] < tps[2] < tps[3]
    elif ok: ok = s > e > tps[0] > tps[1] > tps[2] > tps[3]
    if not ok:
        send_telegram("❌ استاپ یا ترتیب تارگت‌ها همخوانی ندارد.")
        return

    new_sig = {"symbol": symbol, "side": sd, "entry": e, "sl": s, "tp1": tps[0], "tp2": tps[1], "tp3": tps[2], "tp4": tps[3], "current_stage": 0, "closed": False, "is_auto": False, "limit_stage": limit_stage, "fail_count": 0}
    for ex in state["pending"].get(pos_key, []):
        if ex["entry"] == e and ex["sl"] == s: return
    state["pending"].setdefault(pos_key, []).append(new_sig)
    
    msg = f"✅ سیگنال {pos_key} در اتاق انتظار ثبت شد."
    if limit_stage == 1: msg += "\n(حالت RF)"
    elif limit_stage == 2: msg += "\n(حالت SAFE)"
    send_telegram(msg)

def handle_auto(state, parts):
    open_auto_count = sum(1 for t in state["targets"].values() if t.get("is_auto", False))
    if open_auto_count >= MAX_AUTO_POSITIONS:
        send_telegram(f"⚠️ سقف {MAX_AUTO_POSITIONS} پوزیشن خودکار پر شده است. سیگنال جدید نادیده گرفته شد.")
        return

    if len(parts) < 10:
        send_telegram("❌ فرمت کامند خودکار اشتباه است.")
        return

    symbol = parts[1].upper().replace("USDT", "")
    sd = parts[2].upper()
    try:
        e = float(parts[3]); s = float(parts[4]); tps = [float(x) for x in parts[5:10]]
    except ValueError:
        return

    balance = get_futures_balance()
    if balance <= 0:
        # پیام خطا در تابع get_futures_balance ارسال میشه
        return
    
    margin = balance * (AUTO_MARGIN_PERCENT / 100.0)

    if open_auto_position(symbol, sd, margin, AUTO_LEVERAGE):
        pos_key = f"{symbol}_{sd}"
        new_sig = {
            "symbol": symbol, "side": sd, "entry": e, "sl": s,
            "tp1": tps[0], "tp2": tps[1], "tp3": tps[2], "tp4": tps[3], "tp5": tps[4],
            "current_stage": 0, "closed": False, "is_auto": True, "limit_stage": 99, "fail_count": 0
        }
        state["pending"].setdefault(pos_key, []).append(new_sig)
        send_telegram(f"🤖 <b>پوزیشن خودکار باز شد!</b>\nنماد: {symbol} | مارجین: {margin:.2f}$ | لورج: {AUTO_LEVERAGE}X")

def check_telegram(state):
    offset = state.get("offset", 0)
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", params={"offset": offset, "timeout": 5}, timeout=10).json()
        if not r.get("ok"): return
        for u in r.get("result", []):
            state["offset"] = u["update_id"] + 1
            msg = u.get("message")
            if not msg or "text" not in msg or str(msg.get("chat", {}).get("id")) != str(TELEGRAM_CHAT_ID): continue
            
            text = msg["text"]
            lines = text.split('\n') 
            
            for line in lines:
                line = line.strip()
                if not line: continue
                parts = line.split()
                if line.startswith("/cancel"):
                    if len(parts) == 3: handle_cancel(state, parts)
                    else: send_telegram("❌ فرمت: /cancel SYMBOL SIDE")
                elif line.startswith("/new"): handle_new(state, parts)
                elif line.startswith("/auto"): handle_auto(state, parts)
    except Exception as e: print("TG Check Error:", e)

# ================= Stage logic =================
def get_progress_bar(current, entry, target):
    if entry == target: return "░░░░░░░░░░"
    if target > entry: pct = max(0.0, min(1.0, (current - entry) / (target - entry)))
    else: pct = max(0.0, min(1.0, (entry - current) / (entry - target)))
    filled = int(pct * 10)
    return "█" * filled + "░" * (10 - filled)

def get_close_status(stage, why, is_auto=False):
    if why == "TAKEPROFIT": return "🎯 تارگت نهایی زده شد"
    if why == "MANUAL": return "🖐️ بسته شده دستی"
    if is_auto:
        status_map = {0: "منتظر TP1", 1: "TP2 (قفل ۱۰٪)", 2: "نصفه راه TP3", 3: "TP3 (قفل TP1)", 4: "TP4 (قفل TP1.5)", 5: "TP5 (کلوز)"}
        return status_map.get(stage, "بسته شده (خودکار)")
    status_map = {0: "❌ حد ضرر اولیه", 1: "⚖️ ریسک‌فری (صفر)", 2: "💰 قفل سود ۱۰٪", 3: "🛡️ استاپ ۲۲٪ مسیر TP1", 4: "🛡️ استاپ ۴۰٪ مسیر TP1", 5: "🎯 TP1 زده شد (استاپ ۸۰٪)", 6: "🛡️ استاپ روی TP1", 7: "🎯 TP2 زده شد (استاپ ۵۰٪)", 8: "🛡️ استاپ روی TP2", 9: "🎯 TP3 زده شد (استاپ ۵۰٪)", 10: "🛡️ استاپ روی TP3", 11: "🎯 TP4 زده شد (استاپ ۵۰٪)", 12: "🚀 شکار سود (استاپ روی TP4)"}
    return status_map.get(stage, "⚖️ بسته شده")

def next_stage(t, is_long, entry, price, roi, price_10, cur_sl, cur_tp, max_stage):
    sl, tp1, tp2, tp3, tp4 = t["sl"], t["tp1"], t["tp2"], t["tp3"], t["tp4"]
    d1, d2, d3, d4 = abs(tp1 - entry), abs(tp2 - tp1), abs(tp3 - tp2), abs(tp4 - tp3)
    g = 1 if is_long else -1
    p40, p22 = entry + g * d1 * 0.4, entry + g * d1 * 0.22
    p70, p80 = entry + g * d1 * 0.7, entry + g * d1 * 0.8
    h2, h3, h4 = tp1 + g * d2 * 0.5, tp2 + g * d3 * 0.5, tp3 + g * d4 * 0.5
    def hit(level): return price >= level if is_long else price <= level
    chain = [(0, "roi", 10.0, entry, "ریسک‌فری"), (1, "roi", 20.0, price_10, "قفل سود ۱۰٪"), (2, "price", p40, p22, "عبور از ۴۰٪ راه TP1"), (3, "price", p70, p40, "عبور از ۷۰٪ راه TP1"), (4, "price", tp1, p80, "رسیدن به TP1"), (5, "price", h2, tp1, "عبور از ۵۰٪ راه TP2"), (6, "price", tp2, h2, "رسیدن به TP2"), (7, "price", h3, tp2, "عبور از ۵۰٪ راه TP3"), (8, "price", tp3, h3, "رسیدن به TP3"), (9, "price", h4, tp3, "عبور از ۵۰٪ راه TP4"), (10, "price", tp4, h4, "رسیدن به TP4")]
    stage, new_sl, new_tp, reason = t["current_stage"], cur_sl, cur_tp, ""
    for st, kind, level, sl_to, why in chain:
        if stage != st or stage >= max_stage: continue
        if (roi >= level) if kind == "roi" else hit(level):
            new_sl, stage, reason = sl_to, st + 1, why
    if stage >= 11 and stage < max_stage and hit(cur_tp):
        new_tp = cur_tp + d4 if is_long else cur_tp - d4
        new_sl, stage, reason = t["tp4"], stage + 1, "تمدید تارگت"
    return stage, new_sl, new_tp, reason

def next_auto_stage(t, is_long, entry, price):
    tp1, tp2, tp3, tp4, tp5 = t["tp1"], t["tp2"], t["tp3"], t["tp4"], t["tp5"]
    g = 1 if is_long else -1
    d1 = abs(tp1 - entry)
    
    def hit(level): return price >= level if is_long else price <= level

    if t["current_stage"] == 0 and hit(tp2):
        return 1, entry + g * d1 * 0.10, "TP2 (قفل ۱۰٪)"
    elif t["current_stage"] == 1 and hit(entry + g * abs(tp3 - entry) * 0.5):
        return 2, entry + g * d1 * 0.5, "نصفه راه TP3"
    elif t["current_stage"] == 2 and hit(tp3):
        return 3, tp1, "TP3 (قفل TP1)"
    elif t["current_stage"] == 3 and hit(tp4):
        return 4, tp1 + g * abs(tp2 - tp1) * 0.5, "TP4 (قفل TP1.5)"
    elif t["current_stage"] == 4 and hit(tp5):
        return 5, tp4, "TP5 (کلوز)"
    return t["current_stage"], 0, ""

STAGE_TEXT = {
    0: ("⏳ وضعیت: منتظر ۱۰٪ سود (ریسک‌فری)", ""), 1: ("✅ وضعیت: ریسک‌فری شد", "🚀 مرحله بعد: در مسیر رسیدن به ۲۰٪ سود"),
    2: ("✅ وضعیت: قفل سود ۱۰٪", "🚀 مرحله بعد: در مسیر رسیدن به ۴۰٪ TP1"), 3: ("✅ وضعیت: استاپ ۲۲٪ قفل شد", "🚀 مرحله بعد: در مسیر رسیدن به ۷۰٪ TP1"),
    4: ("✅ وضعیت: استاپ ۴۰٪ قفل شد", "🚀 مرحله بعد: در مسیر رسیدن به TP1"), 5: ("✅ وضعیت: TP1 زده شد", "🚀 مرحله بعد: در مسیر رسیدن به ۵۰٪ TP2"),
    6: ("✅ وضعیت: استاپ روی TP1 قفل شد", "🚀 مرحله بعد: در مسیر رسیدن به TP2"), 7: ("✅ وضعیت: TP2 زده شد", "🚀 مرحله بعد: در مسیر رسیدن به ۵۰٪ TP3"),
    8: ("✅ وضعیت: استاپ روی TP2 قفل شد", "🚀 مرحله بعد: در مسیر رسیدن به TP3"), 9: ("✅ وضعیت: TP3 زده شد", "🚀 مرحله بعد: در مسیر رسیدن به ۵۰٪ TP4"),
    10: ("✅ وضعیت: استاپ روی TP3 قفل شد", "🚀 مرحله بعد: در مسیر رسیدن به TP4"), 11: ("✅ وضعیت: TP4 زده شد", "🚀 مرحله بعد: شکار سود در روند قوی"),
}

AUTO_STAGE_TEXT = {
    0: ("منتظر TP2", ""), 1: ("TP2 (قفل ۱۰٪)", "به سمت نصفه راه TP3"), 2: ("نصفه راه TP3 (قفل ۵۰٪ TP1)", "به سمت TP3"),
    3: ("TP3 (قفل TP1)", "به سمت TP4"), 4: ("TP4 (قفل TP1.5)", "به سمت TP5"), 5: ("TP5 (کلوز)", ""),
}

def stage_texts(stage, max_stage, is_auto=False):
    if is_auto: return AUTO_STAGE_TEXT.get(stage, ("در حال مدیریت", ""))
    if max_stage == 1 and stage >= 1: return "🏁 وضعیت: ریسک‌فری شد (پایان مدیریت RF)", ""
    if max_stage == 2 and stage >= 2: return "🏁 وضعیت: قفل سود ۱۰٪ شد (پایان مدیریت SAFE)", ""
    return STAGE_TEXT[min(stage, 11)]

# ================= Main logic =================
def manage_positions(state):
    status, response = tt_request("GET", "/futures/positions")
    if status != 200 or not isinstance(response, dict): return
    
    items = response.get("items", [])
    open_positions = [p for p in items if p.get("status") == "OPENED"]
    closed_positions = [p for p in items if p.get("status") == "CLOSED"]
    open_positions.sort(key=lambda p: p.get("createdAt", ""))
    open_keys = set()
    
    manual_parts = ["📊 <b>داشبورد مدیریت دستی</b>\n━━━━━━━━━━━━━━━━━━━━━"]
    auto_parts = ["🤖 <b>داشبورد خودکار (آدم)</b>\n━━━━━━━━━━━━━━━━━━━━━"]
    
    manual_pnl = 0.0
    manual_margin = 0.0 
    
    MAX_LEN = 3300 
    manual_len = sum(len(s) for s in manual_parts)
    auto_len = sum(len(s) for s in auto_parts)
    manual_unmanaged = []
    auto_unmanaged = []

    for pos in open_positions:
        pos_id = str(pos["id"])
        symbol = pos["symbol"].replace("USDT", "")
        side = pos["side"]
        key_base = f"{symbol}_{side}"
        open_keys.add(pos_id)

        if pos_id not in state["targets"]:
            queue = state["pending"].get(key_base)
            if queue:
                sig = queue.pop(0)
                if not queue: del state["pending"][key_base]
                sig["symbol"], sig["side"] = symbol, side
                state["targets"][pos_id] = sig
                is_auto = sig.get("is_auto", False)
                msg = "🔗 <b>اتصال موفق!</b>"
                if is_auto: msg = "🤖 <b>اتصال خودکار!</b>"
                send_telegram(f"{msg}\nپوزیشن {key_base} به سیگنال متصل شد.")
            else:
                manual_unmanaged.append(key_base)
                continue

        if pos_id not in state["targets"]: continue
        t = state["targets"][pos_id]
        if t.get("closed", False): continue
        
        is_auto = t.get("is_auto", False)
        
        price = float(pos.get("markPrice") or 0)
        if price <= 0: continue
        cur_sl = float(pos.get("stopLoss") or 0)
        cur_tp = float(pos.get("takeProfit") or 0)
        pnl = float(pos.get("unrealizedPnL") or 0)
        margin = float(pos.get("initialMargin") or 0)
        size = float(pos.get("size") or 0)
        if margin <= 0 or size <= 0: continue
        is_long = (side == "LONG")
        ex_entry = float(pos.get("entryPrice") or 0)
        if ex_entry <= 0: ex_entry = price - pnl / size if is_long else price + pnl / size
        if ex_entry > 0: t["entry"] = ex_entry
        entry = t["entry"]
        leverage = int(round((size * entry) / margin))
        roi = (pnl / margin) * 100
        tol = max(1e-8, price * 1e-5)

        # منطق مخصوص پوزیشن‌های خودکار
        if is_auto:
            stage, new_sl, reason = next_auto_stage(t, is_long, entry, price)
            if stage > t["current_stage"]:
                if stage == 5:
                    send_telegram(f"🏁 <b>TP5 برای {key_base} زده شد! کلوز پوزیشن.</b>")
                    close_position_market(pos_id)
                else:
                    if reason:
                        payload_sl = max(cur_sl, new_sl) if is_long else min(cur_sl, new_sl) if cur_sl > 0 else new_sl
                        if patch_tpsl(pos_id, payload_sl, t["tp5"]):
                            cur_sl = payload_sl
                            t["current_stage"] = stage
                            send_telegram(f"✅ <b>{key_base}:</b> {reason} | استاپ روی {payload_sl:.5f}")
            
            stage = t["current_stage"]
            status_text, next_text = stage_texts(stage, 99, is_auto=True)
            bar = get_progress_bar(price, entry, t["tp5"])
            emoji = "🟢" if roi >= 0 else "🔴"
            
            card = f"{emoji} <b>{key_base}</b> ({leverage}X) | 🤖 خودکار\n"
            card += f"💵 سود: <code>{pnl:+.2f} USDT ({roi:+.1f}%)</code>\n"
            card += f"وضعیت: {status_text}\n"
            if next_text: card += f"بعدی: {next_text}\n"
            card += f"🛑 استاپ: <code>{float(cur_sl):.5f}</code>\n"
            
            if auto_len + len(card) < MAX_LEN:
                auto_parts.append(card)
                auto_len += len(card)
            continue

        # منطق مخصوص پوزیشن‌های دستی
        manual_pnl += pnl
        manual_margin += margin
        d10 = (margin * 0.10) / size
        price_10 = entry + d10 if is_long else entry - d10

        if cur_sl > 0:
            adopt_tol = max(tol, price * 5e-4)
            def at_or_better(level): return (cur_sl >= level - adopt_tol) if is_long else (cur_sl <= level + adopt_tol)
            adopted = t["current_stage"]
            if adopted < 1 and at_or_better(entry): adopted = 1
            if adopted < 2 and at_or_better(price_10): adopted = 2
            adopted = min(adopted, t.get("limit_stage", 99))
            if adopted > t["current_stage"]:
                t["current_stage"] = adopted
                send_telegram(f"🔎 <b>{key_base}:</b> استاپ دستی شما شناسایی شد (مرحله {adopted}).")

        if t["current_stage"] >= 1 and cur_sl > 0:
            is_worse = (cur_sl > entry + tol) if not is_long else (cur_sl < entry - tol)
            if is_worse:
                if abs(price - entry) > (price * 0.0015):
                    if patch_tpsl(pos_id, entry, cur_tp): cur_sl = entry

        sl0 = t["sl"]
        looser = (cur_sl < sl0 - tol) if is_long else (cur_sl > sl0 + tol)
        if cur_sl == 0 or (t["current_stage"] == 0 and looser):
            restore = sl0
            if cur_sl == 0 and t["current_stage"] >= 2: restore = price_10
            elif cur_sl == 0 and t["current_stage"] == 1: restore = entry
            ok_side = (restore < price) if is_long else (restore > price)
            if not ok_side: restore = sl0
            if patch_tpsl(pos_id, restore, t["tp4"]): cur_sl = restore

        if cur_tp <= 0: cur_tp = t["tp4"]
        if t["current_stage"] < 12 and abs(cur_tp - t["tp4"]) > tol:
            if patch_tpsl(pos_id, cur_sl, t["tp4"]): cur_tp = t["tp4"]

        max_stage = t.get("limit_stage", 99)
        stage, new_sl, new_tp, reason = next_stage(t, is_long, entry, price, roi, price_10, cur_sl, cur_tp, max_stage)
        if new_tp <= 0: new_tp = t["tp4"]

        if reason:
            if is_long: final_sl = max(cur_sl, new_sl)
            else: final_sl = min(cur_sl, new_sl) if cur_sl > 0 else new_sl
            same_tp = abs(new_tp - cur_tp) <= tol
            sl_ok = cur_sl > 0 and ((cur_sl >= new_sl - tol) if is_long else (cur_sl <= new_sl + tol))
            if sl_ok and same_tp:
                t["current_stage"] = stage
                t["fail_count"] = 0
            else:
                is_sl_valid = False
                tolerance = price * 0.001
                if is_long:
                    if final_sl < price: is_sl_valid = True
                    elif (final_sl - price) <= tolerance: final_sl = price * 0.9995; is_sl_valid = True
                else:
                    if final_sl > price: is_sl_valid = True
                    elif (price - final_sl) <= tolerance: final_sl = price * 1.0005; is_sl_valid = True
                ok = False
                if is_sl_valid:
                    if is_long: final_sl = max(final_sl, cur_sl)
                    elif cur_sl > 0: final_sl = min(final_sl, cur_sl)
                    ok = patch_tpsl(pos_id, final_sl, new_tp)
                if ok:
                    cur_sl, cur_tp = final_sl, new_tp
                    t["current_stage"] = stage
                    t["tp4"] = new_tp
                    t["fail_count"] = 0
                else:
                    t["fail_count"] = t.get("fail_count", 0) + 1
                    if t["fail_count"] == 3:
                        send_telegram(f"⚠️ <b>{key_base}:</b> ۳ بار تغییر استاپ رد شد.")

        stage = t["current_stage"]
        status_text, next_text = stage_texts(stage, max_stage, is_auto=False)
        target = cur_tp
        if stage < 5: target = t["tp1"]
        elif stage < 7: target = t["tp2"]
        elif stage < 9: target = t["tp3"]
        bar = get_progress_bar(price, entry, target)
        emoji = "🟢" if roi >= 0 else "🔴"
        mode_val = t.get("limit_stage", 99)
        if mode_val == 1: mode_txt = "🔒 RF"
        elif mode_val == 2: mode_txt = "🛡️ SAFE"
        else: mode_txt = "⚙️ عادی"
        
        card = f"{emoji} <b>{key_base}</b> ({leverage}X) | {mode_txt}\n"
        card += f"💵 سود: <code>{pnl:+.2f} USDT ({roi:+.1f}%)</code>\n"
        card += f"{status_text}\n"
        if next_text: card += f"{next_text}\n"
        card += f"🛑 استاپ: <code>{float(cur_sl):.5f}</code>\n"
        card += f"🎯 هدف: <code>{float(target):.5f}</code>\n"
        card += f"📊 پیشرفت:\n<code>[{bar}]</code>\n"
        
        if manual_len + len(card) < MAX_LEN:
            manual_parts.append(card)
            manual_len += len(card)

    for key in list(state["targets"].keys()):
        if key in open_keys or state["targets"][key].get("closed", False): continue
        match = [p for p in closed_positions if str(p.get("id")) == key]
        if not match: continue
        match.sort(key=lambda x: x.get("updatedAt", ""), reverse=True)
        last = match[0]
        realized = float(last.get("realizedPnL") or 0)
        why = last.get("closingReason") or ""
        t = state["targets"][key]
        is_auto = t.get("is_auto", False)
        stage = t.get("current_stage", 0) 
        reasons = {"STOPLOSS": "حد ضرر", "TAKEPROFIT": "حد سود", "MANUAL": "دستی"}
        reason_text = reasons.get(why, why)
        status_msg = get_close_status(stage, why, is_auto=is_auto)
        sym = last.get("symbol", "").replace("USDT", "")
        sd = last.get("side", "")
        name = f"{sym}_{sd}" if sym and sd else key
        pnl_text = f"{realized:+.2f} USDT"
        
        state["last_closed"] = f"🔴 {name} | {status_msg}\nدلیل API: {reason_text} | PnL: {pnl_text}"
        prefix = "🤖" if is_auto else "🏁"
        send_telegram(f"{prefix} <b>پوزیشن {name} بسته شد!</b>\nوضعیت: {status_msg}\nسود/ضرر: {pnl_text}")
        del state["targets"][key]

    if manual_margin > 0:
        active_tp_percent = get_dynamic_tp_percent(len([t for t in state["targets"].values() if not t.get("is_auto", False)]))
        target_profit = manual_margin * (active_tp_percent / 100.0)
        if manual_pnl >= target_profit:
            send_telegram(f"🚀 <b>رسیدن به هدف سود کل (دستی)!</b>\nدر حال بستن همه...")
            for pos in open_positions:
                if not state["targets"].get(str(pos["id"]), {}).get("is_auto", False):
                    close_position_market(str(pos["id"]))
            return 

    if not [p for p in open_positions if not state["targets"].get(str(p["id"]), {}).get("is_auto", False)]: 
        manual_parts.append("هیچ پوزیشن دستی بازی وجود ندارد. ⏳")
    else: 
        manual_parts.insert(1, f"📈 سود شناور کل: <code>{manual_pnl:+.2f} USDT</code>\n")
    
    if manual_unmanaged: manual_parts.append(f"⚠️ <i>بدون سیگنال: {', '.join(manual_unmanaged)}</i>")
    manual_parts.append("━━━━━━━━━━━━━━━━━━━━━")
    manual_parts.append(f"🏁 <b>آخرین بسته شده:</b>\n{state['last_closed']}")
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3, minutes=30)
    manual_parts.append(f"🕒 آپدیت: <code>{now.strftime('%H:%M')}</code>")
    final_manual_text = "\n".join(manual_parts)

    if not [p for p in open_positions if state["targets"].get(str(p["id"]), {}).get("is_auto", False)]:
        auto_parts.append("هیچ پوزیشن خودکاری وجود ندارد. ⏳")
    if auto_unmanaged: auto_parts.append(f"⚠️ <i>بدون سیگنال: {', '.join(auto_unmanaged)}</i>")
    auto_parts.append(f"🕒 آپدیت: <code>{now.strftime('%H:%M')}</code>")
    final_auto_text = "\n".join(auto_parts)

    if state.get("dashboard_id"):
        if not edit_telegram(state["dashboard_id"], final_manual_text):
            state["dashboard_id"] = send_telegram(final_manual_text)
    else:
        state["dashboard_id"] = send_telegram(final_manual_text)

    if state.get("auto_dashboard_id"):
        if not edit_telegram(state["auto_dashboard_id"], final_auto_text):
            state["auto_dashboard_id"] = send_telegram(final_auto_text)
    else:
        if len(auto_parts) > 3:
            state["auto_dashboard_id"] = send_telegram(final_auto_text)

def run():
    print("Checking...")
    state = load_state()
    check_telegram(state)
    manage_positions(state)
    save_state(state)
    print("Done.")

if __name__ == "__main__":
    run()
