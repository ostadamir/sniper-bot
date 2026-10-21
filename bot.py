import time, os, requests, hmac, hashlib, json
import datetime

# ================= تنظیمات =================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TRUETRADE_KEY = os.environ.get("TRUETRADE_KEY")
TRUETRADE_SECRET = os.environ.get("TRUETRADE_SECRET")

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
    if not TRUETRADE_KEY or not TRUETRADE_SECRET: return 500, {}
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
        return 500, str(e)

# ================= حافظه =================
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
                # 🌟 اصلاح باگ: اضافه کردن current_stage به پوزیشن‌های قدیمی
                for key, t in state.get("targets", {}).items():
                    t.setdefault("current_stage", 0)
                    t.setdefault("closed", False)
                return state
        except: pass
    return {"targets": {}, "offset": 0}

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
                            pos_key = f"{symbol}_{side}"
                            if pos_key in state["targets"]:
                                del state["targets"][pos_key]
                                send_telegram(f"🛑 مدیریت {pos_key} لغو شد.")

                    elif text.startswith("/new"):
                        parts = text.split()
                        if len(parts) == 9:
                            symbol = parts[1].upper().replace("USDT", "")
                            side, entry, sl, tp1, tp2, tp3, tp4 = parts[2:]
                            pos_key = f"{symbol}_{side.upper()}"
                            
                            state["targets"][pos_key] = {
                                "entry": float(entry), "sl": float(sl),
                                "tp1": float(tp1), "tp2": float(tp2), "tp3": float(tp3), "tp4": float(tp4),
                                "current_stage": 0,
                                "closed": False
                            }
                            send_telegram(f"✅ سیگنال {pos_key} ثبت شد.\nورود: {entry}\nاستاپ: {sl}\nربات مدیریتش رو شروع کرد.")
    except Exception as e:
        print("TG Error:", e)

# ================= منطق پله‌ای و اصلاح خودکار =================
def manage_positions(state):
    status, response = tt_request("GET", "/futures/positions")
    if status != 200 or not isinstance(response, dict): return
    
    items = response.get("items", [])
    open_positions = [p for p in items if p.get("status") == "OPENED"]
    
    open_keys = set()
    
    for pos in open_positions:
        symbol = pos["symbol"].replace("USDT", "")
        side = pos["side"]
        pos_key = f"{symbol}_{side}"
        open_keys.add(pos_key)
        
        if pos_key not in state["targets"]: continue
        t = state["targets"][pos_key]
        if t.get("closed", False): continue
        
        current_price = float(pos.get("markPrice") or 0)
        current_sl = float(pos.get("stopLoss") or 0)
        current_tp = float(pos.get("takeProfit") or 0)
        is_long = (side == "LONG")
        
        entry, sl, tp1 = t["entry"], t["sl"], t["tp1"]
        tp2, tp3, tp4 = t["tp2"], t["tp3"], t["tp4"]
        
        # 🌟 بخش اصلاح تی‌پی دستی شما
        if abs(current_tp - tp4) > 0.00001:
            payload_fix = {"takeProfit": str(round(tp4, 6))}
            fix_status, _ = tt_request("PATCH", f"/futures/positions/{pos['id']}/tpsl", payload_fix)
            if fix_status in [200, 201]:
                send_telegram(f"🛠️ <b>اصلاح تی‌پی {pos_key}</b>\nتی‌پی دستی شما شناسایی شد. ربات آن را به تارگت نهایی ({tp4}) تغییر داد تا مدیریت پله‌ای به هم نریزد.")
                current_tp = tp4

        # --- محاسبات درصدی ---
        dist_to_tp1 = abs(tp1 - entry)
        half_tp1 = entry + (dist_to_tp1 * 0.5) if is_long else entry - (dist_to_tp1 * 0.5)
        trigger_80 = entry + (dist_to_tp1 * 0.8) if is_long else entry - (dist_to_tp1 * 0.8)
        sl_40 = entry + (dist_to_tp1 * 0.4) if is_long else entry - (dist_to_tp1 * 0.4)
        
        new_sl, new_tp, reason = current_sl, current_tp, ""
        
        # مرحله 0: ۵۰ درصد راه -> ریسک فری
        if t.get("current_stage", 0) == 0:
            if (is_long and current_price >= half_tp1) or (not is_long and current_price <= half_tp1):
                new_sl = entry
                t["current_stage"] = 1
                reason = "ریسک‌فری (استاپ روی نقطه ورود)"

        # مرحله 1: ۸۰ درصد راه -> استاپ روی ۴۰ درصد
        elif t.get("current_stage", 0) == 1:
            if (is_long and current_price >= trigger_80) or (not is_long and current_price <= trigger_80):
                new_sl = sl_40
                t["current_stage"] = 2
                reason = "رسیدن به 80% راه TP1 (استاپ روی 40% راه)"

        # مرحله 2: رسیدن به TP1
        elif t.get("current_stage", 0) == 2:
            if (is_long and current_price >= tp1) or (not is_long and current_price <= tp1):
                new_sl = trigger_80
                new_tp = tp2
                t["current_stage"] = 3
                reason = "رسیدن به TP1 (استاپ روی 80% راه، تارگت TP2)"

        # مرحله 3: رسیدن به TP2
        elif t.get("current_stage", 0) == 3:
            if (is_long and current_price >= tp2) or (not is_long and current_price <= tp2):
                new_sl = tp1
                new_tp = tp3
                t["current_stage"] = 4
                reason = "رسیدن به TP2 (استاپ روی TP1، تارگت TP3)"

        # مرحله 4: رسیدن به TP3
        elif t.get("current_stage", 0) == 4:
            if (is_long and current_price >= tp3) or (not is_long and current_price <= tp3):
                new_sl = tp2
                new_tp = tp4
                t["current_stage"] = 5
                reason = "رسیدن به TP3 (استاپ روی TP2، تارگت TP4)"

        # مرحله 5: رسیدن به TP4 (تمدید تارگت)
        elif t.get("current_stage", 0) == 5:
            if (is_long and current_price >= tp4) or (not is_long and current_price <= tp4):
                dist_tp3_tp4 = abs(tp4 - tp3)
                extended_tp = tp4 + dist_tp3_tp4 if is_long else tp4 - dist_tp3_tp4
                
                new_sl = tp3
                new_tp = extended_tp
                
                t["current_stage"] = 6
                t["tp4"] = extended_tp
                reason = f"🚀 تمدید تارگت! استاپ روی TP3، تارگت جدید: {extended_tp:.4f}"

        # مرحله 6: تمدیدهای بعدی
        elif t.get("current_stage", 0) >= 6:
            if (is_long and current_price >= new_tp) or (not is_long and current_price <= new_tp):
                dist_tp3_tp4 = abs(tp4 - tp3)
                extended_tp = new_tp + dist_tp3_tp4 if is_long else new_tp - dist_tp3_tp4
                
                new_sl = current_tp
                new_tp = extended_tp
                t["tp4"] = extended_tp
                reason = f"🚀 تمدید مجدد تارگت! استاپ روی {current_tp:.4f}، تارگت جدید: {extended_tp:.4f}"

        # اجرای تغییرات در صرافی
        if reason:
            payload = {}
            if abs(new_sl - current_sl) > 0.00001: payload["stopLoss"] = str(round(new_sl, 6))
            if abs(new_tp - current_tp) > 0.00001: payload["takeProfit"] = str(round(new_tp, 6))
            
            if payload:
                p_status, _ = tt_request("PATCH", f"/futures/positions/{pos['id']}/tpsl", payload)
                if p_status in [200, 201]:
                    send_telegram(f"🔄 <b>مدیریت {pos_key}</b>\n{reason}\nاستاپ جدید: {new_sl:.4f}\nتارگت جدید: {new_tp:.4f}")

    # پاکسازی پوزیشن‌های بسته شده
    for key in list(state["targets"].keys()):
        if key not in open_keys and not state["targets"][key].get("closed", False):
            state["targets"][key]["closed"] = True
            send_telegram(f"🏁 <b>پوزیشن {key} بسته شد</b>\nاز لیست خارج شد.")

def run():
    print("Checking...")
    state = load_state()
    check_telegram(state)
    manage_positions(state)
    save_state(state)
    print("Done.")

if __name__ == "__main__":
    run()
