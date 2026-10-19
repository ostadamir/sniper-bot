import time, os, requests, hmac, hashlib, json
import datetime

# ================= تنظیمات =================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TRUETRADE_KEY = os.environ.get("TRUETRADE_KEY")
TRUETRADE_SECRET = os.environ.get("TRUETRADE_SECRET")

TRUETRADE_URL = "https://apiv2.thetruetrade.io"
STATE_FILE = "manager_state.json"

def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)

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
                return json.load(f)
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
                    # فرمت: /new TRX SHORT 0.3309 0.3408 0.31998 0.31749 0.31501
                    if text.startswith("/new"):
                        parts = text.split()
                        if len(parts) == 8:
                            symbol, side, entry, sl, tp1, tp2, tp3 = parts[1:]
                            pos_key = f"{symbol.upper()}_{side.upper()}"
                            state["targets"][pos_key] = {
                                "entry": float(entry), "sl": float(sl),
                                "tp1": float(tp1), "tp2": float(tp2), "tp3": float(tp3),
                                "risk_free_done": False, "tp1_done": False, 
                                "half_tp2_done": False, "tp2_done": False, "closed": False
                            }
                            send_telegram(f"✅ سیگنال {pos_key} ثبت شد.\nورود: {entry}\nاستاپ: {sl}\nTP1: {tp1}\nربات مدیریتش رو شروع کرد.")
                        else:
                            send_telegram("❌ فرمت اشتباهه. نمونه:\n/new TRX SHORT 0.33 0.34 0.32 0.31 0.30")
    except Exception as e:
        print("TG Error:", e)

# ================= منطق پله‌ای =================
def manage_positions(state):
    status, response = tt_request("GET", "/futures/positions")
    if status != 200 or not isinstance(response, dict): return
    
    open_positions = response.get("items", [])
    open_keys = set()
    
    for pos in open_positions:
        if pos.get("status") != "OPENED": continue
        
        symbol = pos["symbol"].replace("USDT", "")
        side = pos["side"]
        pos_key = f"{symbol}_{side}"
        open_keys.add(pos_key)
        
        if pos_key not in state["targets"]: continue
        t = state["targets"][pos_key]
        if t["closed"]: continue
        
        current_price = float(pos.get("markPrice", 0))
        current_sl = float(pos.get("stopLoss", 0))
        current_tp = float(pos.get("takeProfit", 0))
        is_long = (side == "LONG")
        
        entry, sl, tp1, tp2, tp3 = t["entry"], t["sl"], t["tp1"], t["tp2"], t["tp3"]
        
        # نقاط کلیدی
        half_tp1 = (entry + tp1) / 2
        three_quarter_tp1 = (entry + 3 * tp1) / 4
        half_tp2 = (tp1 + tp2) / 2
        three_quarter_tp2 = (tp1 + 3 * tp2) / 4
        
        new_sl, new_tp, reason = current_sl, current_tp, ""
        
        # 1. ریسک فری (نصف راه TP1)
        if not t["risk_free_done"]:
            if (is_long and current_price >= half_tp1) or (not is_long and current_price <= half_tp1):
                new_sl = entry
                t["risk_free_done"] = True
                reason = "ریسک‌فری (استاپ روی نقطه ورود)"
                
        # 2. سه‌چهارم راه TP1 (استاپ به نصف راه، تارگت TP2)
        if t["risk_free_done"] and not t["tp1_done"]:
            if (is_long and current_price >= three_quarter_tp1) or (not is_long and current_price <= three_quarter_tp1):
                new_sl = half_tp1
                new_tp = tp2
                t["tp1_done"] = True
                reason = "سه‌چهارم راه TP1 (استاپ روی نصف راه، تارگت TP2)"

        # 3. نصف راه TP2 (استاپ به TP1)
        if t["tp1_done"] and not t["half_tp2_done"]:
            if (is_long and current_price >= half_tp2) or (not is_long and current_price <= half_tp2):
                new_sl = tp1
                t["half_tp2_done"] = True
                reason = "نصف راه TP2 (استاپ روی TP1)"
                
        # 4. سه‌چهارم راه TP2 (استاپ به نصف راه، تارگت TP3)
        if t["half_tp2_done"] and not t["tp2_done"]:
            if (is_long and current_price >= three_quarter_tp2) or (not is_long and current_price <= three_quarter_tp2):
                new_sl = half_tp2
                new_tp = tp3
                t["tp2_done"] = True
                reason = "سه‌چهارم راه TP2 (استاپ روی نصف راه، تارگت TP3)"

        # اجرای تغییرات
        if reason:
            payload = {}
            if abs(new_sl - current_sl) > 0.00001: payload["stopLoss"] = str(round(new_sl, 6))
            if abs(new_tp - current_tp) > 0.00001: payload["takeProfit"] = str(round(new_tp, 6))
            
            if payload:
                p_status, _ = tt_request("PATCH", f"/futures/positions/{pos['id']}/tpsl", payload)
                if p_status in [200, 201]:
                    send_telegram(f"🔄 <b>مدیریت {pos_key}</b>\n{reason}\nاستاپ جدید: {new_sl}\nتارگت جدید: {new_tp}")

    # 5. پاکسازی پوزیشن‌های بسته شده (اگر ربات دیر رسید)
    for key in list(state["targets"].keys()):
        if key not in open_keys and not state["targets"][key].get("closed", False):
            state["targets"][key]["closed"] = True
            send_telegram(f"🏁 پوزیشن {key} بسته شد (TP/SL خورد یا دستی بسته شد). از لیست خارج شد.")

def run():
    print("Checking...")
    state = load_state()
    check_telegram(state)
    manage_positions(state)
    save_state(state)
    print("Done.")

if __name__ == "__main__":
    run()
