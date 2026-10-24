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
        if res.status_code == 400 and "message is not modified" in res.text:
            return True
        return res.status_code == 200
    except Exception as e:
        print("TG Edit Error:", e)
        return False

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
                state.setdefault("targets", {})
                state.setdefault("pending", {})
                state.setdefault("dashboard_id", None)
                state.setdefault("last_closed", "هیچ پوزیشنی بسته نشده است.")
                for key, t in state.get("targets", {}).items():
                    t.setdefault("current_stage", 0)
                    t.setdefault("closed", False)
                return state
        except: pass
    return {"targets": {}, "pending": {}, "offset": 0, "dashboard_id": None, "last_closed": "هیچ پوزیشنی بسته نشده است."}

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
                            for k in list(state["targets"].keys()):
                                if f"{symbol}_{side}" in k:
                                    del state["targets"][k]
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
                            
                            new_sig = {
                                "entry": float(entry), "sl": float(sl),
                                "tp1": float(tp1), "tp2": float(tp2), "tp3": float(tp3), "tp4": float(tp4),
                                "current_stage": 0,
                                "closed": False
                            }

                            is_duplicate = False
                            if pos_key in state["pending"]:
                                for existing in state["pending"][pos_key]:
                                    if existing["entry"] == new_sig["entry"] and existing["sl"] == new_sig["sl"]:
                                        is_duplicate = True
                                        break
                            
                            if not is_duplicate:
                                if pos_key not in state["pending"]:
                                    state["pending"][pos_key] = []
                                state["pending"][pos_key].append(new_sig)
                                send_telegram(f"✅ سیگنال {pos_key} در اتاق انتظار ثبت شد.")
    except Exception as e:
        print("TG Check Error:", e)

# ================= ابزارک نوار پیشرفت =================
def get_progress_bar(current, entry, target):
    if entry == target: return "░░░░░░░░░░"
    if target > entry: 
        pct = max(0.0, min(1.0, (current - entry) / (target - entry)))
    else: 
        pct = max(0.0, min(1.0, (entry - current) / (entry - target)))
    filled = int(pct * 10)
    return "█" * filled + "░" * (10 - filled)

# ================= منطق اصلی و داشبورد =================
def manage_positions(state):
    status, response = tt_request("GET", "/futures/positions")
    if status != 200 or not isinstance(response, dict): return
    
    items = response.get("items", [])
    open_positions = [p for p in items if p.get("status") == "OPENED"]
    closed_positions = [p for p in items if p.get("status") == "CLOSED"]
    
    open_positions.sort(key=lambda p: p.get("createdAt", ""))
    open_keys = set()
    
    dashboard_parts = []
    dashboard_parts.append("📊 <b>داشبورد مدیریت سرمایه</b>\n━━━━━━━━━━━━━━━━━━━━━")
    
    total_pnl = 0.0
    
    for pos in open_positions:
        pos_id_str = str(pos['id']) 
        symbol = pos["symbol"].replace("USDT", "")
        side = pos["side"]
        pos_key_base = f"{symbol}_{side}"
        open_keys.add(pos_id_str)
        
        if pos_id_str not in state["targets"]:
            if pos_key_base in state["pending"] and len(state["pending"][pos_key_base]) > 0:
                matched_signal = state["pending"][pos_key_base].pop(0)
                if len(state["pending"][pos_key_base]) == 0:
                    del state["pending"][pos_key_base]
                state["targets"][pos_id_str] = matched_signal
                send_telegram(f"🔗 <b>اتصال موفق!</b>\nپوزیشن {pos_key_base} با شناسه {pos_id_str} به سیگنال متصل شد.")

        if pos_id_str not in state["targets"]: continue
        t = state["targets"][pos_id_str]
        if t.get("closed", False): continue
        
        current_price = float(pos.get("markPrice") or 0)
        current_sl = float(pos.get("stopLoss") or 0)
        current_tp = float(pos.get("takeProfit") or 0)
        
        exchange_entry = float(pos.get("entryPrice") or 0)
        if exchange_entry > 0: t["entry"] = exchange_entry
            
        entry = t["entry"]
        sl, tp1, tp2, tp3, tp4 = t["sl"], t["tp1"], t["tp2"], t["tp3"], t["tp4"]
        is_long = (side == "LONG")
        
        pnl = float(pos.get("unrealizedPnL") or 0)
        margin = float(pos.get("initialMargin") or 0.01) 
        leverage = int(float(pos.get("leverage") or 1))
        
        total_pnl += pnl
        current_roi = (pnl / margin) * 100

        price_10_roi = entry
        if margin > 0:
            target_pnl_10 = margin * 0.10
            price_10_roi = entry + (target_pnl_10 / margin) if is_long else entry - (target_pnl_10 / margin)

        if current_sl == 0:
            payload = {"stopLoss": str(sl), "takeProfit": str(tp4)}
            tt_request("PATCH", f"/futures/positions/{pos_id_str}/tpsl", payload)
            current_sl = sl

        if t.get("current_stage", 0) == 0 and current_sl != 0 and abs(current_sl - sl) > 0.00001:
            payload = {"stopLoss": str(sl), "takeProfit": str(tp4)}
            tt_request("PATCH", f"/futures/positions/{pos_id_str}/tpsl", payload)
            current_sl = sl

        if t.get("current_stage", 0) < 11 and abs(current_tp - tp4) > 0.00001:
            payload = {"stopLoss": str(current_sl), "takeProfit": str(tp4)}
            tt_request("PATCH", f"/futures/positions/{pos_id_str}/tpsl", payload)
            current_tp = tp4

        dist1, dist2, dist3, dist4 = abs(tp1-entry), abs(tp2-tp1), abs(tp3-tp2), abs(tp4-tp3)
        if is_long:
            tp1_70, tp1_40, tp1_80 = entry+(dist1*0.7), entry+(dist1*0.4), entry+(dist1*0.8)
            half2, half3, half4 = tp1+(dist2*0.5), tp2+(dist3*0.5), tp3+(dist4*0.5)
        else:
            tp1_70, tp1_40, tp1_80 = entry-(dist1*0.7), entry-(dist1*0.4), entry-(dist1*0.8)
            half2, half3, half4 = tp1-(dist2*0.5), tp2-(dist3*0.5), tp3-(dist4*0.5)

        new_sl, new_tp, reason, status_text, next_step_text = current_sl, current_tp, "", "", ""
        stage = t.get("current_stage", 0)
        
        # 🌟 منطق پله‌ای با متن‌های تفکیک شده و روان
        if stage == 0:
            status_text = "⏳ وضعیت: منتظر ۱۰٪ (ریسک‌فری)"
            next_step_text = ""
            if current_roi >= 10.0:
                new_sl = entry; t["current_stage"] = 1; reason = "ریسک‌فری"
        elif stage == 1:
            status_text = "✅ وضعیت: ریسک‌فری شد"
            next_step_text = "🚀 مرحله بعد: در مسیر رسیدن به ۷۰٪ TP1"
            if current_roi >= 20.0:
                new_sl = price_10_roi; t["current_stage"] = 2; reason = "قفل سود ۱۰٪"
        elif stage == 2:
            status_text = "✅ وضعیت: قفل سود ۴۰٪"
            next_step_text = "🚀 مرحله بعد: در مسیر رسیدن به TP1"
            if (is_long and current_price >= tp1_70) or (not is_long and current_price <= tp1_70):
                new_sl = tp1_40; t["current_stage"] = 3; reason = "عبور از ۷۰٪ راه TP1"
        elif stage == 3:
            status_text = "✅ وضعیت: TP1 زده شد"
            next_step_text = "🚀 مرحله بعد: در مسیر رسیدن به ۵۰٪ TP2"
            if (is_long and current_price >= tp1) or (not is_long and current_price <= tp1):
                new_sl = tp1_80; t["current_stage"] = 4; reason = "رسیدن به TP1"
        elif stage == 4:
            status_text = "✅ وضعیت: استاپ روی TP1 قفل شد"
            next_step_text = "🚀 مرحله بعد: در مسیر رسیدن به TP2"
            if (is_long and current_price >= half2) or (not is_long and current_price <= half2):
                new_sl = tp1; t["current_stage"] = 5; reason = "عبور از ۵۰٪ راه TP2"
        elif stage == 5:
            status_text = "✅ وضعیت: TP2 زده شد"
            next_step_text = "🚀 مرحله بعد: در مسیر رسیدن به ۵۰٪ TP3"
            if (is_long and current_price >= tp2) or (not is_long and current_price <= tp2):
                new_sl = half2; t["current_stage"] = 6; reason = "رسیدن به TP2"
        elif stage == 6:
            status_text = "✅ وضعیت: استاپ روی TP2 قفل شد"
            next_step_text = "🚀 مرحله بعد: در مسیر رسیدن به TP3"
            if (is_long and current_price >= half3) or (not is_long and current_price <= half3):
                new_sl = tp2; t["current_stage"] = 7; reason = "عبور از ۵۰٪ راه TP3"
        elif stage == 7:
            status_text = "✅ وضعیت: TP3 زده شد"
            next_step_text = "🚀 مرحله بعد: در مسیر رسیدن به ۵۰٪ TP4"
            if (is_long and current_price >= tp3) or (not is_long and current_price <= tp3):
                new_sl = half3; t["current_stage"] = 8; reason = "رسیدن به TP3"
        elif stage == 8:
            status_text = "✅ وضعیت: استاپ روی TP3 قفل شد"
            next_step_text = "🚀 مرحله بعد: در مسیر رسیدن به TP4"
            if (is_long and current_price >= half4) or (not is_long and current_price <= half4):
                new_sl = tp3; t["current_stage"] = 9; reason = "عبور از ۵۰٪ راه TP4"
        elif stage == 9:
            status_text = "✅ وضعیت: TP4 زده شد"
            next_step_text = "🚀 مرحله بعد: در حال تمدید تارگت"
            if (is_long and current_price >= tp4) or (not is_long and current_price <= tp4):
                new_sl = half4; t["current_stage"] = 10; reason = "رسیدن به TP4"
        elif stage >= 10:
            status_text = "✅ وضعیت: تارگت تمدید شد"
            next_step_text = "🚀 مرحله بعد: شکار سود در روند قوی"
            if (is_long and current_price >= current_tp) or (not is_long and current_price <= current_tp):
                new_tp = current_tp + dist4 if is_long else current_tp - dist4
                new_sl = t["tp4"]; t["tp4"] = new_tp; t["current_stage"] = stage + 1; reason = "تمدید مجدد"

        if reason:
            payload = {"stopLoss": str(round(new_sl, 6)), "takeProfit": str(round(new_tp, 6))}
            tt_request("PATCH", f"/futures/positions/{pos_id_str}/tpsl", payload)
            current_sl = new_sl
            current_tp = new_tp
        
        emoji = "🟢" if current_roi >= 0 else "🔴"
        
        next_target = current_tp
        if stage < 4: next_target = tp1
        elif stage < 6: next_target = tp2
        elif stage < 8: next_target = tp3
        
        prog_bar = get_progress_bar(current_price, entry, next_target)
        
        # 🌟 ساخت کارت پوزیشن با ظاهر تمیز و بدون بهم ریختگی
        card = f"{emoji} <b>{pos_key_base}</b> | {leverage}X\n"
        card += f"💵 سود: <code>{pnl:+.2f} USDT ({current_roi:+.1f}%)</code>\n"
        card += f"{status_text}\n"
        if next_step_text:
            card += f"{next_step_text}\n"
        card += f"🛑 استاپ: <code>{current_sl}</code>\n"
        card += f"🎯 هدف: <code>{next_target}</code>\n"
        card += f"📊 پیشرفت:\n<code>[{prog_bar}]</code>\n"
        
        dashboard_parts.append(card)

    for key in list(state["targets"].keys()):
        if key not in open_keys and not state["targets"][key].get("closed", False):
            matching_closed = [p for p in closed_positions if str(p.get("id")) == key]
            pnl_text, reason_text = "نامشخص", "نامشخص"
            
            if matching_closed:
                matching_closed.sort(key=lambda x: x.get("updatedAt", ""), reverse=True)
                last_closed = matching_closed[0]
                realized_pnl = float(last_closed.get("realizedPnL") or 0)
                closing_reason = last_closed.get("closingReason") or ""
                pnl_text = f"{realized_pnl:+.2f} USDT"
                reasons = {"STOPLOSS": "حد ضرر", "TAKEPROFIT": "حد سود", "MANUAL": "دستی"}
                reason_text = reasons.get(closing_reason, closing_reason)

            state["last_closed"] = f"🔴 {key} | دلیل: {reason_text} | PnL: {pnl_text}"
            state["targets"][key]["closed"] = True
            del state["targets"][key]

    if len(open_positions) == 0:
        dashboard_parts.append("هیچ پوزیشن بازی وجود ندارد. ⏳")
    else:
        dashboard_parts.insert(1, f"📈 سود شناور کل: <code>{total_pnl:+.2f} USDT</code>\n")
    
    dashboard_parts.append("━━━━━━━━━━━━━━━━━━━━━")
    dashboard_parts.append(f"🏁 <b>آخرین بسته شده:</b>\n{state['last_closed']}")
    
    now_time = datetime.datetime.now().strftime("%H:%M")
    dashboard_parts.append(f"🕒 آپدیت: <code>{now_time}</code>")
    
    final_text = "\n".join(dashboard_parts)

    if state.get("dashboard_id"):
        success = edit_telegram(state["dashboard_id"], final_text)
        if not success:
            state["dashboard_id"] = send_telegram(final_text)
    else:
        state["dashboard_id"] = send_telegram(final_text)

def run():
    print("Checking...")
    state = load_state()
    check_telegram(state)
    manage_positions(state)
    save_state(state)
    print("Done.")

if __name__ == "__main__":
    run()
