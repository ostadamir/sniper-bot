import time, os, requests, hmac, hashlib, json
import datetime

# ================= تنظیمات (هاردکد شده) =================
TELEGRAM_TOKEN = "8865889106:AAFBVDLcTCn1kpvbzI_N6AoJd80VMXFI5zY"
TELEGRAM_CHAT_ID = "7702136864"

TRUETRADE_KEY = "VHezc6KrbqcHTlNzVRK3I0rCysxjYnyFJDw-n5pQ"
TRUETRADE_SECRET = "b496b58d14853b92219845c699b8f64367207251c2960a1e4866dd0bcb7005b3"

TRUETRADE_URL = "https://apiv2.thetruetrade.io"
STATE_FILE = "manager_state.json"

def send_telegram(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print("TG Error:", e)

# ================= ترودید =================
def tt_sign(secret: str, timestamp: str, method: str, uri: str) -> str:
    payload = f"{timestamp}{method.upper()}{uri}"
    return hmac.new(secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()

def tt_request(method, uri, payload=None):
    if not TRUETRADE_KEY or not TRUETRADE_SECRET:
        return 500, {}
        
    timestamp = str(int(time.time() * 1000))
    signature = tt_sign(TRUETRADE_SECRET, timestamp, method, uri)
    headers = {"X-API-Key": TRUETRADE_KEY, "X-Timestamp": timestamp, "X-Signature": signature, "Content-Type": "application/json"}
    try:
        if method == "GET":
            res = requests.get(f"{TRUETRADE_URL}{uri}", headers=headers, timeout=10)
        elif method == "PATCH":
            res = requests.patch(f"{TRUETRADE_URL}{uri}", headers=headers, json=payload, timeout=10)
        
        return res.status_code, res.json() if res.text else {}
    except Exception as e:
        print("TT API Exception:", e)
        return 500, str(e)

# ================= حافظه =================
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
                state.setdefault("targets", {})
                state.setdefault("pending", {}) # اتاق انتظار سیگنال‌ها
                for key, t in state.get("targets", {}).items():
                    t.setdefault("current_stage", 0)
                    t.setdefault("closed", False)
                return state
        except: pass
    return {"targets": {}, "pending": {}, "offset": 0}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)

# ================= شنود تلگرام =================
def check_telegram(state):
    offset = state.get("offset", 0)
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"offset": offset, "timeout": 5}, timeout=10).json()
        
        if r.get("ok"):
            for u in r.get("result", []):
                state["offset"] = u["update_id"] + 1
                if "message" in u and "text" in u["message"]:
                    text = u["message"]["text"]
                    
                    if text.startswith("/cancel"):
                        parts = text.split()
                        if len(parts) == 3:
                            symbol = parts[1].upper().replace("USDT", "")
                            side = parts[2].upper()
                            # لغو از لیست مدیریت شده‌ها
                            for k in list(state["targets"].keys()):
                                if f"{symbol}_{side}" in k:
                                    del state["targets"][k]
                            # لغو از اتاق انتظار
                            pos_key = f"{symbol}_{side}"
                            if pos_key in state["pending"]:
                                del state["pending"][pos_key]
                            send_telegram(f"🛑 مدیریت {pos_key} لغو شد.")

                    elif text.startswith("/new"):
                        parts = text.split()
                        if len(parts) == 9:
                            symbol = parts[1].upper().replace("USDT", "")
                            side, entry, sl, tp1, tp2, tp3, tp4 = parts[2:]
                            pos_key = f"{symbol}_{side.upper()}"
                            
                            # سیگنال جدید رو می‌فرستیم به اتاق انتظار
                            if pos_key not in state["pending"]:
                                state["pending"][pos_key] = []
                            
                            state["pending"][pos_key].append({
                                "entry": float(entry), "sl": float(sl),
                                "tp1": float(tp1), "tp2": float(tp2), "tp3": float(tp3), "tp4": float(tp4),
                                "current_stage": 0,
                                "closed": False
                            })
                            send_telegram(f"✅ سیگنال {pos_key} در اتاق انتظار ثبت شد.\nربات به محض دیدن پوزیشن باز، آن را مدیریت می‌کند.")
    except Exception as e:
        print("TG Error:", e)

# ================= منطق پله‌ای =================
def manage_positions(state):
    status, response = tt_request("GET", "/futures/positions")
    if status != 200 or not isinstance(response, dict): 
        return
    
    items = response.get("items", [])
    open_positions = [p for p in items if p.get("status") == "OPENED"]
    
    print(f"Found {len(open_positions)} open positions on exchange.")
    
    open_keys = set()
    
    for pos in open_positions:
        pos_id_str = str(pos['id']) # استفاده از شناسه یکتای صرافی
        symbol = pos["symbol"].replace("USDT", "")
        side = pos["side"]
        pos_key_base = f"{symbol}_{side}"
        
        open_keys.add(pos_id_str)
        
        # 🌟 جادوی جدید: اگر پوزیشن جدیدی باز شده که تو لیست ما نیست، از اتاق انتظار برش دار
        if pos_id_str not in state["targets"]:
            if pos_key_base in state["pending"] and len(state["pending"][pos_key_base]) > 0:
                exchange_entry = float(pos.get("entryPrice") or 0)
                # پیدا کردن نزدیک‌ترین سیگنال از نظر قیمت ورود
                best_match_idx = 0
                min_diff = float('inf')
                for idx, sig in enumerate(state["pending"][pos_key_base]):
                    diff = abs(sig["entry"] - exchange_entry)
                    if diff < min_diff:
                        min_diff = diff
                        best_match_idx = idx
                
                # انتقال از اتاق انتظار به لیست مدیریت
                matched_signal = state["pending"][pos_key_base].pop(best_match_idx)
                if len(state["pending"][pos_key_base]) == 0:
                    del state["pending"][pos_key_base]
                    
                state["targets"][pos_id_str] = matched_signal
                send_telegram(f"🔗 <b>اتصال موفق!</b>\nپوزیشن {pos_key_base} با شناسه {pos_id_str} به سیگنال شما متصل شد و مدیریت آغاز شد.")

        if pos_id_str not in state["targets"]: continue
        t = state["targets"][pos_id_str]
        if t.get("closed", False): continue
        
        current_price = float(pos.get("markPrice") or 0)
        current_sl = float(pos.get("stopLoss") or 0)
        current_tp = float(pos.get("takeProfit") or 0)
        
        exchange_entry = float(pos.get("entryPrice") or 0)
        if exchange_entry > 0:
            t["entry"] = exchange_entry
            
        entry = t["entry"]
        sl, tp1 = t["sl"], t["tp1"]
        tp2, tp3, tp4 = t["tp2"], t["tp3"], t["tp4"]
        is_long = (side == "LONG")
        
        pnl = float(pos.get("unrealizedPnL") or 0)
        margin = float(pos.get("initialMargin") or 0.01) 
        size = float(pos.get("size") or 0)
        
        current_roi = (pnl / margin) * 100

        price_10_roi = entry
        if size > 0:
            target_pnl_10 = margin * 0.10
            price_10_roi = entry + (target_pnl_10 / size) if is_long else entry - (target_pnl_10 / size)
        
        print(f"Checking {pos_key_base} ({pos_id_str}) | Stage: {t.get('current_stage', 0)} | ROI: {current_roi:.2f}%")

        # 🛡️ سیستم دفاعی 1: برگرداندن استاپ لاس در صورت پاک شدن
        if current_sl == 0:
            payload = {"stopLoss": str(sl), "takeProfit": str(tp4)}
            tt_request("PATCH", f"/futures/positions/{pos_id_str}/tpsl", payload)
            current_sl = sl
            send_telegram(f"🛡️ <b>نجات {pos_key_base}!</b>\nاستاپ لاس پاک شده بود، ربات آن را برگرداند.")

        # 🔄 سیستم همگام‌سازی
        if t.get("current_stage", 0) == 0 and current_sl != 0 and abs(current_sl - sl) > 0.00001:
            payload = {"stopLoss": str(sl), "takeProfit": str(tp4)}
            fix_status, _ = tt_request("PATCH", f"/futures/positions/{pos_id_str}/tpsl", payload)
            if fix_status in [200, 201]:
                send_telegram(f"🔄 <b>همگام‌سازی {pos_key_base}</b>\nاستاپ لاس جدید از کامند اعمال شد.")
            current_sl = sl

        # 🛠️ اصلاح تی‌پی دستی
        if t.get("current_stage", 0) < 11 and abs(current_tp - tp4) > 0.00001:
            payload = {"stopLoss": str(current_sl), "takeProfit": str(tp4)}
            fix_status, _ = tt_request("PATCH", f"/futures/positions/{pos_id_str}/tpsl", payload)
            if fix_status in [200, 201]:
                send_telegram(f"🛠️ <b>اصلاح تی‌پی {pos_key_base}</b>\nتی‌پی به تارگت نهایی تغییر کرد.")
            current_tp = tp4

        # 🌟 محاسبات فواصل قیمتی
        dist1 = abs(tp1 - entry)
        dist2 = abs(tp2 - tp1)
        dist3 = abs(tp3 - tp2)
        dist4 = abs(tp4 - tp3)

        if is_long:
            tp1_70 = entry + (dist1 * 0.70)
            tp1_40 = entry + (dist1 * 0.40)
            tp1_80 = entry + (dist1 * 0.80)
            half2 = tp1 + (dist2 * 0.5)
            half3 = tp2 + (dist3 * 0.5)
            half4 = tp3 + (dist4 * 0.5)
        else:
            tp1_70 = entry - (dist1 * 0.70)
            tp1_40 = entry - (dist1 * 0.40)
            tp1_80 = entry - (dist1 * 0.80)
            half2 = tp1 - (dist2 * 0.5)
            half3 = tp2 - (dist3 * 0.5)
            half4 = tp3 - (dist4 * 0.5)

        new_sl, new_tp, reason = current_sl, current_tp, "" 
        stage = t.get("current_stage", 0)

        # منطق پله‌ای
        if stage == 0:
            if current_roi >= 10.0:
                new_sl = entry
                t["current_stage"] = 1
                reason = f"۱۰٪ رول (ریسک‌فری)"
        elif stage == 1:
            if current_roi >= 20.0:
                new_sl = price_10_roi
                t["current_stage"] = 2
                reason = f"۲۰٪ رول (قفل سود ۱۰٪)"
        elif stage == 2:
            if (is_long and current_price >= tp1_70) or (not is_long and current_price <= tp1_70):
                new_sl = tp1_40
                t["current_stage"] = 3
                reason = "۷۰٪ راه TP1"
        elif stage == 3:
            if (is_long and current_price >= tp1) or (not is_long and current_price <= tp1):
                new_sl = tp1_80
                t["current_stage"] = 4
                reason = "رسیدن به TP1"
        elif stage == 4:
            if (is_long and current_price >= half2) or (not is_long and current_price <= half2):
                new_sl = tp1
                t["current_stage"] = 5
                reason = "۵۰٪ راه TP2"
        elif stage == 5:
            if (is_long and current_price >= tp2) or (not is_long and current_price <= tp2):
                new_sl = half2
                t["current_stage"] = 6
                reason = "رسیدن به TP2"
        elif stage == 6:
            if (is_long and current_price >= half3) or (not is_long and current_price <= half3):
                new_sl = tp2
                t["current_stage"] = 7
                reason = "۵۰٪ راه TP3"
        elif stage == 7:
            if (is_long and current_price >= tp3) or (not is_long and current_price <= tp3):
                new_sl = half3
                t["current_stage"] = 8
                reason = "رسیدن به TP3"
        elif stage == 8:
            if (is_long and current_price >= half4) or (not is_long and current_price <= half4):
                new_sl = tp3
                t["current_stage"] = 9
                reason = "۵۰٪ راه TP4"
        elif stage == 9:
            if (is_long and current_price >= tp4) or (not is_long and current_price <= tp4):
                new_sl = half4
                t["current_stage"] = 10
                reason = "رسیدن به TP4"
        elif stage == 10:
            if (is_long and current_price >= current_tp) or (not is_long and current_price <= current_tp):
                dist_ext = abs(tp4 - tp3)
                new_tp = current_tp + dist_ext if is_long else current_tp - dist_ext
                new_sl = tp3
                t["tp4"] = new_tp
                t["current_stage"] = 11
                reason = f"تمدید تارگت! تارگت جدید: {new_tp:.4f}"
        elif stage >= 11:
            if (is_long and current_price >= current_tp) or (not is_long and current_price <= current_tp):
                dist_ext = abs(tp4 - tp3)
                new_tp = current_tp + dist_ext if is_long else current_tp - dist_ext
                new_sl = t["tp4"]
                t["tp4"] = new_tp
                t["current_stage"] = stage + 1
                reason = f"تمدید مجدد تارگت! تارگت جدید: {new_tp:.4f}"

        if reason:
            payload = {"stopLoss": str(round(new_sl, 6)), "takeProfit": str(round(new_tp, 6))}
            p_status, _ = tt_request("PATCH", f"/futures/positions/{pos_id_str}/tpsl", payload)
            if p_status in [200, 201]:
                send_telegram(f"🔄 <b>مدیریت {pos_key_base}</b>\n{reason}\nاستاپ: {new_sl:.4f}\nتارگت: {new_tp:.4f}")

    # پاکسازی پوزیشن‌های بسته شده
    for key in list(state["targets"].keys()):
        if key not in open_keys and not state["targets"][key].get("closed", False):
            state["targets"][key]["closed"] = True
            send_telegram(f"🏁 <b>پوزیشن با شناسه {key} بسته شد</b>")

def run():
    print("Checking...")
    state = load_state()
    check_telegram(state)
    manage_positions(state)
    save_state(state)
    print("Done.")

if __name__ == "__main__":
    run()
