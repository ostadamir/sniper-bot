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
                    if text.startswith("/new"):
                        parts = text.split()
                        if len(parts) == 9: # 4 Targets
                            symbol = parts[1].upper().replace("USDT", "")
                            
                            # 🌟 اصلاح باگ اول: اضافه کردن tp4 به متغیرها
                            side, entry, sl, tp1, tp2, tp3, tp4 = parts[2:]
                            
                            pos_key = f"{symbol}_{side.upper()}"
                            state["targets"][pos_key] = {
                                "entry": float(entry), "sl": float(sl),
                                "tp1": float(tp1), "tp2": float(tp2), "tp3": float(tp3), "tp4": float(tp4),
                                "risk_free_done": False, "tp1_done": False, 
                                "half_tp2_done": False, "tp2_done": False, "tp3_done": False, "closed": False
                            }
                            send_telegram(f"✅ سیگنال {pos_key} ثبت شد.\nورود: {entry}\nاستاپ: {sl}\nTP1: {tp1}\nTP4: {tp4}\nربات مدیریتش رو شروع کرد.")
                        else:
                            send_telegram("❌ فرمت اشتباهه. نمونه (با 4 تارگت):\n/new ETH LONG 2403 2331 2482 2500 2518 2536")
    except Exception as e:
        print("TG Error:", e)

# ================= منطق پله‌ای =================
def manage_positions(state):
    status, response = tt_request("GET", "/futures/positions")
    if status != 200 or not isinstance(response, dict): return
    
    items = response.get("items", [])
    open_positions = [p for p in items if p.get("status") == "OPENED"]
    closed_positions = [p for p in items if p.get("status") == "CLOSED"]
    
    open_keys = set()
    
    for pos in open_positions:
        symbol = pos["symbol"].replace("USDT", "")
        side = pos["side"]
        pos_key = f"{symbol}_{side}"
        open_keys.add(pos_key)
        
        if pos_key not in state["targets"]: continue
        t = state["targets"][pos_key]
        if t.get("closed", False): continue
        
        current_price = float(pos.get("markPrice", 0))
        current_sl = float(pos.get("stopLoss", 0))
        current_tp = float(pos.get("takeProfit", 0))
        is_long = (side == "LONG")
        
        entry, sl, tp1, tp2, tp3 = t["entry"], t["sl"], t["tp1"], t["tp2"], t["tp3"]
        tp4 = t.get("tp4", None)
        
        # نقاط کلیدی
        half_tp1 = (entry + tp1) / 2
        three_quarter_tp1 = (entry + 3 * tp1) / 4
        half_tp2 = (tp1 + tp2) / 2
        three_quarter_tp2 = (tp1 + 3 * tp2) / 4
        half_tp3 = (tp2 + tp3) / 2
        three_quarter_tp3 = (tp2 + 3 * tp3) / 4
        
        new_sl, new_tp, reason = current_sl, current_tp, ""
        
        # 🌟 اصلاح باگ دوم: استفاده از .get() برای جلوگیری از کرش کردن پوزیشن‌های قدیمی
        # 1. ریسک فری (نصف راه TP1)
        if not t.get("risk_free_done", False):
            if (is_long and current_price >= half_tp1) or (not is_long and current_price <= half_tp1):
                new_sl = entry
                t["risk_free_done"] = True
                reason = "ریسک‌فری (استاپ روی نقطه ورود)"
                
        # 2. سه‌چهارم راه TP1 (استاپ به نصف راه، تارگت TP2)
        if t.get("risk_free_done", False) and not t.get("tp1_done", False):
            if (is_long and current_price >= three_quarter_tp1) or (not is_long and current_price <= three_quarter_tp1):
                new_sl = half_tp1
                new_tp = tp2
                t["tp1_done"] = True
                reason = "سه‌چهارم راه TP1 (استاپ روی نصف راه، تارگت TP2)"

        # 3. نصف راه TP2 (استاپ به TP1)
        if t.get("tp1_done", False) and not t.get("half_tp2_done", False):
            if (is_long and current_price >= half_tp2) or (not is_long and current_price <= half_tp2):
                new_sl = tp1
                t["half_tp2_done"] = True
                reason = "نصف راه TP2 (استاپ روی TP1)"
                
        # 4. سه‌چهارم راه TP2 (استاپ به نصف راه، تارگت TP3)
        if t.get("half_tp2_done", False) and not t.get("tp2_done", False):
            if (is_long and current_price >= three_quarter_tp2) or (not is_long and current_price <= three_quarter_tp2):
                new_sl = half_tp2
                new_tp = tp3
                t["tp2_done"] = True
                reason = "سه‌چهارم راه TP2 (استاپ روی نصف راه، تارگت TP3)"

        # 5. نصف راه TP3 (استاپ به TP2)
        if t.get("tp2_done", False) and not t.get("tp3_done", False):
            if (is_long and current_price >= half_tp3) or (not is_long and current_price <= half_tp3):
                new_sl = tp2
                t["tp3_done"] = True
                reason = "نصف راه TP3 (استاپ روی TP2)"
                
        # 6. سه‌چهارم راه TP3 (استاپ به نصف راه، تارگت TP4 - اگر وجود داشته باشد)
        if t.get("tp3_done", False) and tp4 and not t.get("tp4_done", False):
            if (is_long and current_price >= three_quarter_tp3) or (not is_long and current_price <= three_quarter_tp3):
                new_sl = half_tp3
                new_tp = tp4
                t["tp4_done"] = True
                reason = "سه‌چهارم راه TP3 (استاپ روی نصف راه، تارگت TP4)"

        # اجرای تغییرات
        if reason:
            payload = {}
            if abs(new_sl - current_sl) > 0.00001: payload["stopLoss"] = str(round(new_sl, 6))
            if abs(new_tp - current_tp) > 0.00001: payload["takeProfit"] = str(round(new_tp, 6))
            
            if payload:
                p_status, _ = tt_request("PATCH", f"/futures/positions/{pos['id']}/tpsl", payload)
                if p_status in [200, 201]:
                    send_telegram(f"🔄 <b>مدیریت {pos_key}</b>\n{reason}\nاستاپ جدید: {new_sl}\nتارگت جدید: {new_tp}")

    # 5. پاکسازی و گزارش سود/ضرر پوزیشن‌های بسته شده
    for key in list(state["targets"].keys()):
        if key not in open_keys and not state["targets"][key].get("closed", False):
            state["targets"][key]["closed"] = True
            
            symbol = key.split("_")[0] + "USDT"
            side = key.split("_")[1]
            
            matching_closed = [p for p in closed_positions if p["symbol"] == symbol and p["side"] == side]
            
            pnl_text = "نامشخص"
            reason_text = "نامشخص"
            
            if matching_closed:
                matching_closed.sort(key=lambda x: x.get("updatedAt", ""), reverse=True)
                last_closed = matching_closed[0]
                
                realized_pnl = float(last_closed.get("realizedPnL", 0))
                closing_reason = last_closed.get("closingReason", "")
                
                pnl_text = f"{realized_pnl:+.2f} USDT"
                
                reasons = {
                    "STOPLOSS": "حد ضرر (Stop Loss)",
                    "TAKEPROFIT": "حد سود (Take Profit)",
                    "MANUAL": "دستی (Manual)"
                }
                reason_text = reasons.get(closing_reason, closing_reason)
                
            send_telegram(f"🏁 <b>پوزیشن {key} بسته شد</b>\n"
                          f"دلیل: {reason_text}\n"
                          f"سود/ضرر: {pnl_text}\n"
                          f"از لیست خارج شد.")

def run():
    print("Checking...")
    state = load_state()
    check_telegram(state)
    manage_positions(state)
    save_state(state)
    print("Done.")

if __name__ == "__main__":
    run()
