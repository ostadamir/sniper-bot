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
        print("🚨 ERROR: TrueTrade API Keys are missing!")
        return 500, {}
        
    timestamp = str(int(time.time() * 1000))
    signature = tt_sign(TRUETRADE_SECRET, timestamp, method, uri)
    headers = {"X-API-Key": TRUETRADE_KEY, "X-Timestamp": timestamp, "X-Signature": signature, "Content-Type": "application/json"}
    try:
        if method == "GET":
            res = requests.get(f"{TRUETRADE_URL}{uri}", headers=headers, timeout=10)
        elif method == "PATCH":
            res = requests.patch(f"{TRUETRADE_URL}{uri}", headers=headers, json=payload, timeout=10)
        
        print(f"TT API [{method}] {uri} -> Status: {res.status_code}")
        if res.status_code not in [200, 201]:
            print(f"TT API Error Response: {res.text}")
            
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
        print("Telegram Response:", r)
        
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
                            send_telegram(f"✅ سیگنال {pos_key} ثبت شد.\nربات با منطق جدید (خواندن مستقیم ROI) شروع کرد.")
    except Exception as e:
        print("TG Error:", e)

# ================= منطق پله‌ای =================
def manage_positions(state):
    status, response = tt_request("GET", "/futures/positions")
    if status != 200 or not isinstance(response, dict): 
        print("Failed to get positions from TrueTrade. Check API keys.")
        return
    
    items = response.get("items", [])
    open_positions = [p for p in items if p.get("status") == "OPENED"]
    
    print(f"Found {len(open_positions)} open positions on exchange.")
    
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
        
        exchange_entry = float(pos.get("entryPrice") or 0)
        if exchange_entry > 0:
            t["entry"] = exchange_entry
            
        entry = t["entry"]
        sl, tp1 = t["sl"], t["tp1"]
        tp2, tp3, tp4 = t["tp2"], t["tp3"], t["tp4"]
        is_long = (side == "LONG")
        
        # 🌟 سیستم خواندن مستقیم ROI (دقیقاً مثل صفحه صرافی)
        # اول چک می‌کنیم ببینیم صرافی فیلد roi یا roe رو میده یا نه
        direct_roi = pos.get("roi") or pos.get("roe")
        
        if direct_roi is not None:
            current_roi = float(direct_roi)
            # گاهی صرافی درصد رو به صورت اعشار (0.11) میده، گاهی عدد صحیح (11.0)
            if current_roi < 1.5 and current_roi > -1.5 and current_roi != 0: 
                current_roi = current_roi * 100
        else:
            # اگه صرافی فیلد مستقیم نداد، خودمون با سود دلاری و مارجین حساب می‌کنیم
            pnl = float(pos.get("unrealizedPnl") or 0)
            margin = float(pos.get("margin") or 0.01) # جلوگیری از تقسیم بر صفر
            current_roi = (pnl / margin) * 100

        # قیمتی که معادل ۱۰ درصد رول است
        price_10_roi = entry
        if current_price != entry:
            pnl_total = float(pos.get("unrealizedPnl") or 0)
            qty = abs(pnl_total / (current_price - entry)) if (current_price - entry) != 0 else 0
            if qty > 0:
                target_pnl = float(pos.get("margin") or 1) * 0.10
                price_10_roi = entry + (target_pnl / qty) if is_long else entry - (target_pnl / qty)
        
        # چاپ تمام اطلاعات مهم برای دیباگ در گیت‌هاب
        print(f"--- {pos_key} ---")
        print(f"ROI Calculated: {current_roi:.2f}% | PnL: {pos.get('unrealizedPnl')} | Margin: {pos.get('margin')}")
        print(f"Stage: {t.get('current_stage', 0)} | Price: {current_price} | SL: {current_sl} | TP: {current_tp}")

        # 🛡️ سیستم دفاعی 1: برگرداندن استاپ لاس در صورت پاک شدن
        if current_sl == 0:
            payload = {"stopLoss": str(sl), "takeProfit": str(tp4)}
            tt_request("PATCH", f"/futures/positions/{pos['id']}/tpsl", payload)
            current_sl = sl
            send_telegram(f"🛡️ <b>نجات {pos_key}!</b>\nاستاپ لاس پاک شده بود، ربات آن را به {sl} برگرداند.")

        # 🔄 سیستم همگام‌سازی
        if t.get("current_stage", 0) == 0 and current_sl != 0 and abs(current_sl - sl) > 0.00001:
            payload = {"stopLoss": str(sl), "takeProfit": str(tp4)}
            fix_status, _ = tt_request("PATCH", f"/futures/positions/{pos['id']}/tpsl", payload)
            if fix_status in [200, 201]:
                send_telegram(f"🔄 <b>همگام‌سازی {pos_key}</b>\nاستاپ لاس جدید ({sl}) از کامند شما اعمال شد.")
            current_sl = sl

        # 🛠️ اصلاح تی‌پی دستی
        if t.get("current_stage", 0) < 11 and abs(current_tp - tp4) > 0.00001:
            payload = {"stopLoss": str(current_sl), "takeProfit": str(tp4)}
            fix_status, _ = tt_request("PATCH", f"/futures/positions/{pos['id']}/tpsl", payload)
            if fix_status in [200, 201]:
                send_telegram(f"🛠️ <b>اصلاح تی‌پی {pos_key}</b>\nتی‌پی دستی شما شناسایی و به تارگت نهایی ({tp4}) تغییر کرد.")
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

        # --- منطق جدید پله‌ای ---
        if stage == 0:
            if current_roi >= 10.0:
                new_sl = entry
                t["current_stage"] = 1
                reason = f"۱۰٪ رول (ریسک‌فری / استاپ روی نقطه ورود) | ROI: {current_roi:.2f}%"

        elif stage == 1:
            if current_roi >= 20.0:
                new_sl = price_10_roi
                t["current_stage"] = 2
                reason = f"۲۰٪ رول (استاپ روی قیمت ۱۰٪ رول) | ROI: {current_roi:.2f}%"

        elif stage == 2:
            if (is_long and current_price >= tp1_70) or (not is_long and current_price <= tp1_70):
                new_sl = tp1_40
                t["current_stage"] = 3
                reason = "۷۰٪ راه TP1 (استاپ روی ۴۰٪ راه TP1)"

        elif stage == 3:
            if (is_long and current_price >= tp1) or (not is_long and current_price <= tp1):
                new_sl = tp1_80
                t["current_stage"] = 4
                reason = "رسیدن به TP1 (استاپ روی ۸۰٪ راه TP1)"

        elif stage == 4:
            if (is_long and current_price >= half2) or (not is_long and current_price <= half2):
                new_sl = tp1
                t["current_stage"] = 5
                reason = "۵۰٪ راه TP2 (استاپ روی TP1)"

        elif stage == 5:
            if (is_long and current_price >= tp2) or (not is_long and current_price <= tp2):
                new_sl = half2
                t["current_stage"] = 6
                reason = "رسیدن به TP2 (استاپ روی ۵۰٪ راه TP2)"

        elif stage == 6:
            if (is_long and current_price >= half3) or (not is_long and current_price <= half3):
                new_sl = tp2
                t["current_stage"] = 7
                reason = "۵۰٪ راه TP3 (استاپ روی TP2)"

        elif stage == 7:
            if (is_long and current_price >= tp3) or (not is_long and current_price <= tp3):
                new_sl = half3
                t["current_stage"] = 8
                reason = "رسیدن به TP3 (استاپ روی ۵۰٪ راه TP3)"

        elif stage == 8:
            if (is_long and current_price >= half4) or (not is_long and current_price <= half4):
                new_sl = tp3
                t["current_stage"] = 9
                reason = "۵۰٪ راه TP4 (استاپ روی TP3)"

        elif stage == 9:
            if (is_long and current_price >= tp4) or (not is_long and current_price <= tp4):
                new_sl = half4
                t["current_stage"] = 10
                reason = "رسیدن به TP4 (استاپ روی ۵۰٪ راه TP4)"

        elif stage == 10:
            if (is_long and current_price >= current_tp) or (not is_long and current_price <= current_tp):
                dist_ext = abs(tp4 - tp3)
                new_tp = current_tp + dist_ext if is_long else current_tp - dist_ext
                new_sl = tp3
                t["tp4"] = new_tp
                t["current_stage"] = 11
                reason = f"🚀 تمدید تارگت! استاپ روی TP3، تارگت جدید: {new_tp:.4f}"

        elif stage >= 11:
            if (is_long and current_price >= current_tp) or (not is_long and current_price <= current_tp):
                dist_ext = abs(tp4 - tp3)
                new_tp = current_tp + dist_ext if is_long else current_tp - dist_ext
                new_sl = t["tp4"]
                t["tp4"] = new_tp
                t["current_stage"] = stage + 1
                reason = f"🚀 تمدید مجدد تارگت! استاپ روی {new_sl:.4f}، تارگت جدید: {new_tp:.4f}"

        # 🌟 اجرای تغییرات در صرافی
        if reason:
            payload = {
                "stopLoss": str(round(new_sl, 6)),
                "takeProfit": str(round(new_tp, 6))
            }
            print(f"Executing Stage Change: {reason} | New SL: {new_sl} | New TP: {new_tp}")
            
            p_status, _ = tt_request("PATCH", f"/futures/positions/{pos['id']}/tpsl", payload)
            if p_status in [200, 201]:
                send_telegram(f"🔄 <b>مدیریت {pos_key}</b>\n{reason}\nاستاپ: {new_sl:.4f}\nتارگت: {new_tp:.4f}")

    # پاکسازی پوزیشن‌های بسته شده
    for key in list(state["targets"].keys()):
        if key not in open_keys and not state["targets"][key].get("closed", False):
            state["targets"][key]["closed"] = True
            send_telegram(f"🏁 <b>پوزیشن {key} بسته شد</b>")

def run():
    print("Checking...")
    state = load_state()
    check_telegram(state)
    manage_positions(state)
    save_state(state)
    print("Done.")

if __name__ == "__main__":
    run()
