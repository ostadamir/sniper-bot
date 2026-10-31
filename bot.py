import time, os, requests, hmac, hashlib, json
import datetime

# ================= تنظیمات (از Secrets گیت‌هاب خوانده می‌شود) =================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

TRUETRADE_KEY = os.environ.get("TRUETRADE_KEY")
TRUETRADE_SECRET = os.environ.get("TRUETRADE_SECRET")

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
        if res.status_code == 200:
            return True
        if res.status_code == 400 and "not found" in res.text:
            return False
        print("edit failed:", res.status_code)
        return True
    except Exception as e:
        print("TG Edit Error:", e)
        return True

# ================= ترودید =================
def tt_sign(secret: str, timestamp: str, method: str, uri: str) -> str:
    payload = f"{timestamp}{method.upper()}{uri}"
    return hmac.new(secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()

def tt_request(method, uri, payload=None):
    if not TRUETRADE_KEY or not TRUETRADE_SECRET: 
        print("Missing API Keys!")
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
                    t.setdefault("limit_stage", 99) # 🌟 قابلیت جدید
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
                    if str(u["message"].get("chat", {}).get("id")) != str(TELEGRAM_CHAT_ID):
                        continue
                        
                    text = u["message"]["text"]
                    
                    if text.startswith("/cancel"):
                        parts = text.split()
                        if len(parts) == 3:
                            symbol = parts[1].upper().replace("USDT", "")
                            side = parts[2].upper()
                            
                            for k in list(state["targets"].keys()):
                                if state["targets"][k].get("symbol") == symbol and state["targets"][k].get("side") == side:
                                    del state["targets"][k]
                            
                            pos_key = f"{symbol}_{side}"
                            if pos_key in state["pending"]:
                                del state["pending"][pos_key]
                            send_telegram(f"🛑 مدیریت {pos_key} لغو شد.")

                    elif text.startswith("/new"):
                        parts = text.split()
                        
                        # 🌟 تشخیص حالت‌های محدود
                        limit_stage = 99 # پیش‌فرض: مدیریت کامل
                        if len(parts) == 10:
                            mode = parts[9].upper()
                            if mode == "RF":
                                limit_stage = 1
                            elif mode == "SAFE":
                                limit_stage = 2
                            else:
                                send_telegram(f"⚠️ حالت {mode} ناشناخته است. از RF یا SAFE استفاده کنید.")
                                continue
                            parts = parts[:9] # حذف کلمه کلیدی برای پردازش اعداد

                        if len(parts) == 9:
                            symbol = parts[1].upper().replace("USDT", "")
                            side, entry, sl, tp1, tp2, tp3, tp4 = parts[2:]
                            pos_key = f"{symbol}_{side.upper()}"
                            
                            try:
                                e = float(entry)
                                s = float(sl)
                                tps = [float(tp1), float(tp2), float(tp3), float(tp4)]
                                sd = side.upper()
                                ok = sd in ("LONG", "SHORT")
                                
                                if ok and sd == "LONG":
                                    ok = s < e < tps[0] < tps[1] < tps[2] < tps[3]
                                elif ok and sd == "SHORT":
                                    ok = s > e > tps[0] > tps[1] > tps[2] > tps[3]
                                
                                if not ok:
                                    send_telegram("❌ سیگنال نامعتبر! استاپ یا ترتیب تارگت‌ها با جهت معامله همخوانی ندارد.")
                                    continue
                            except ValueError:
                                send_telegram("❌ خطای فرمت: اعداد وارد شده معتبر نیستند.")
                                continue

                            new_sig = {
                                "symbol": symbol, "side": side.upper(),
                                "entry": e, "sl": s,
                                "tp1": tps[0], "tp2": tps[1], "tp3": tps[2], "tp4": tps[3],
                                "current_stage": 0, "closed": False,
                                "limit_stage": limit_stage
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
                                
                                msg = f"✅ سیگنال {pos_key} در اتاق انتظار ثبت شد."
                                if limit_stage == 1: msg += "\n(حالت RF: فقط ریسک‌فری)"
                                elif limit_stage == 2: msg += "\n(حالت SAFE: ریسک‌فری + قفل سود ۱۰٪)"
                                send_telegram(msg)
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
    if status != 200 or not isinstance(response, dict): 
        return
    
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
                
                matched_signal["symbol"] = symbol
                matched_signal["side"] = side
                state["targets"][pos_id_str] = matched_signal
                send_telegram(f"🔗 <b>اتصال موفق!</b>\nپوزیشن {pos_key_base} با شناسه {pos_id_str} به سیگنال متصل شد.")

        if pos_id_str not in state["targets"]: continue
        t = state["targets"][pos_id_str]
        if t.get("closed", False): continue
        
        current_price = float(pos.get("markPrice") or 0)
        if current_price <= 0: continue
            
        current_sl = float(pos.get("stopLoss") or 0)
        current_tp = float(pos.get("takeProfit") or 0)
        
        if current_tp <= 0: current_tp = t["tp4"]
        
        exchange_entry = float(pos.get("entryPrice") or 0)
        if exchange_entry > 0: t["entry"] = exchange_entry
            
        entry = t["entry"]
        sl, tp1, tp2, tp3, tp4 = t["sl"], t["tp1"], t["tp2"], t["tp3"], t["tp4"]
        is_long = (side == "LONG")
        
        pnl = float(pos.get("unrealizedPnL") or 0)
        margin = float(pos.get("initialMargin") or 0) 
        size = float(pos.get("size") or 0)
        
        if margin <= 0 or size <= 0: continue

        leverage = int(round((size * entry) / margin))
        total_pnl += pnl
        current_roi = (pnl / margin) * 100

        price_10_roi = entry
        target_pnl_10 = margin * 0.10
        price_10_roi = entry + (target_pnl_10 / size) if is_long else entry - (target_pnl_10 / size)

        # سیستم دفاعی اولیه
        if current_sl == 0:
            payload = {"stopLoss": str(sl), "takeProfit": str(tp4)}
            st, _ = tt_request("PATCH", f"/futures/positions/{pos_id_str}/tpsl", payload)
            if st in [200, 201]: current_sl = sl

        sl_tolerance = max(1e-8, current_price * 1e-5)
        if t.get("current_stage", 0) == 0 and current_sl != 0 and abs(current_sl - sl) > sl_tolerance:
            payload = {"stopLoss": str(sl), "takeProfit": str(tp4)}
            st, _ = tt_request("PATCH", f"/futures/positions/{pos_id_str}/tpsl", payload)
            if st in [200, 201]: current_sl = sl

        tp_tolerance = max(1e-8, current_price * 1e-5)
        if t.get("current_stage", 0) < 12 and abs(current_tp - tp4) > tp_tolerance:
            payload = {"stopLoss": str(current_sl), "takeProfit": str(tp4)}
            st, _ = tt_request("PATCH", f"/futures/positions/{pos_id_str}/tpsl", payload)
            if st in [200, 201]: current_tp = tp4

        dist1, dist2, dist3, dist4 = abs(tp1-entry), abs(tp2-tp1), abs(tp3-tp2), abs(tp4-tp3)
        
        if is_long:
            tp1_40 = entry + (dist1 * 0.4)
            tp1_22 = entry + (dist1 * 0.22)
            tp1_70 = entry + (dist1 * 0.7)
            tp1_80 = entry + (dist1 * 0.8)
            half2, half3, half4 = tp1+(dist2*0.5), tp2+(dist3*0.5), tp3+(dist4*0.5)
        else:
            tp1_40 = entry - (dist1 * 0.4)
            tp1_22 = entry - (dist1 * 0.22)
            tp1_70 = entry - (dist1 * 0.7)
            tp1_80 = entry - (dist1 * 0.8)
            half2, half3, half4 = tp1-(dist2*0.5), tp2-(dist3*0.5), tp3-(dist4*0.5)

        new_sl, new_tp, reason = current_sl, current_tp, ""
        new_stage = t["current_stage"]
        max_stage = t.get("limit_stage", 99) # 🌟 دریافت محدودیت مرحله

        # 🌟 فقط تا مرحله‌ای که کاربر اجازه داده پیش میره
        if new_stage < max_stage:
            if new_stage == 0 and current_roi >= 10.0:
                new_sl = entry; new_stage = 1; reason = "ریسک‌فری"
            if new_stage == 1 and current_roi >= 20.0:
                new_sl = price_10_roi; new_stage = 2; reason = "قفل سود ۱۰٪"
            if new_stage == 2 and ((is_long and current_price >= tp1_40) or (not is_long and current_price <= tp1_40)):
                new_sl = tp1_22; new_stage = 3; reason = "عبور از ۴۰٪ راه TP1"
            if new_stage == 3 and ((is_long and current_price >= tp1_70) or (not is_long and current_price <= tp1_70)):
                new_sl = tp1_40; new_stage = 4; reason = "عبور از ۷۰٪ راه TP1"
            if new_stage == 4 and ((is_long and current_price >= tp1) or (not is_long and current_price <= tp1)):
                new_sl = tp1_80; new_stage = 5; reason = "رسیدن به TP1"
            if new_stage == 5 and ((is_long and current_price >= half2) or (not is_long and current_price <= half2)):
                new_sl = tp1; new_stage = 6; reason = "عبور از ۵۰٪ راه TP2"
            if new_stage == 6 and ((is_long and current_price >= tp2) or (not is_long and current_price <= tp2)):
                new_sl = half2; new_stage = 7; reason = "رسیدن به TP2"
            if new_stage == 7 and ((is_long and current_price >= half3) or (not is_long and current_price <= half3)):
                new_sl = tp2; new_stage = 8; reason = "عبور از ۵۰٪ راه TP3"
            if new_stage == 8 and ((is_long and current_price >= tp3) or (not is_long and current_price <= tp3)):
                new_sl = half3; new_stage = 9; reason = "رسیدن به TP3"
            if new_stage == 9 and ((is_long and current_price >= half4) or (not is_long and current_price <= half4)):
                new_sl = tp3; new_stage = 10; reason = "عبور از ۵۰٪ راه TP4"
            if new_stage == 10 and ((is_long and current_price >= tp4) or (not is_long and current_price <= tp4)):
                new_sl = half4; new_stage = 11; reason = "رسیدن به TP4"
            if new_stage >= 11 and ((is_long and current_price >= current_tp) or (not is_long and current_price <= current_tp)):
                new_tp = current_tp + dist4 if is_long else current_tp - dist4
                new_sl = t["tp4"]; new_stage += 1; reason = "تمدید تارگت"

        if new_tp <= 0: new_tp = t["tp4"]

        if reason:
            if is_long:
                final_sl = max(current_sl, new_sl)
            else:
                final_sl = min(current_sl, new_sl) if current_sl > 0 else new_sl
            
            payload = {"stopLoss": str(round(final_sl, 6)), "takeProfit": str(round(new_tp, 6))}
            p_status, _ = tt_request("PATCH", f"/futures/positions/{pos_id_str}/tpsl", payload)
            if p_status in [200, 201]:
                current_sl = final_sl
                current_tp = new_tp
                t["current_stage"] = new_stage
                t["tp4"] = new_tp
            else:
                reason = ""
        
        emoji = "🟢" if current_roi >= 0 else "🔴"
        stage = t["current_stage"]
        
        # 🌟 تعیین متن وضعیت بر اساس اینکه به محدودیت رسیده یا نه
        if max_stage == 1 and stage >= 1:
            status_text = "🏁 وضعیت: ریسک‌فری شد (پایان مدیریت RF)"
            next_step_text = ""
        elif max_stage == 2 and stage >= 2:
            status_text = "🏁 وضعیت: قفل سود ۱۰٪ شد (پایان مدیریت SAFE)"
            next_step_text = ""
        else:
            if stage == 0:
                status_text = "⏳ وضعیت: منتظر ۱۰٪ سود (ریسک‌فری)"; next_step_text = ""
            elif stage == 1:
                status_text = "✅ وضعیت: ریسک‌فری شد"; next_step_text = "🚀 مرحله بعد: در مسیر رسیدن به ۲۰٪ سود"
            elif stage == 2:
                status_text = "✅ وضعیت: قفل سود ۱۰٪"; next_step_text = "🚀 مرحله بعد: در مسیر رسیدن به ۴۰٪ TP1"
            elif stage == 3:
                status_text = "✅ وضعیت: استاپ ۲۲٪ قفل شد"; next_step_text = "🚀 مرحله بعد: در مسیر رسیدن به ۷۰٪ TP1"
            elif stage == 4:
                status_text = "✅ وضعیت: استاپ ۴۰٪ قفل شد"; next_step_text = "🚀 مرحله بعد: در مسیر رسیدن به TP1"
            elif stage == 5:
                status_text = "✅ وضعیت: TP1 زده شد"; next_step_text = "🚀 مرحله بعد: در مسیر رسیدن به ۵۰٪ TP2"
            elif stage == 6:
                status_text = "✅ وضعیت: استاپ روی TP1 قفل شد"; next_step_text = "🚀 مرحله بعد: در مسیر رسیدن به TP2"
            elif stage == 7:
                status_text = "✅ وضعیت: TP2 زده شد"; next_step_text = "🚀 مرحله بعد: در مسیر رسیدن به ۵۰٪ TP3"
            elif stage == 8:
                status_text = "✅ وضعیت: استاپ روی TP2 قفل شد"; next_step_text = "🚀 مرحله بعد: در مسیر رسیدن به TP3"
            elif stage == 9:
                status_text = "✅ وضعیت: TP3 زده شد"; next_step_text = "🚀 مرحله بعد: در مسیر رسیدن به ۵۰٪ TP4"
            elif stage == 10:
                status_text = "✅ وضعیت: استاپ روی TP3 قفل شد"; next_step_text = "🚀 مرحله بعد: در مسیر رسیدن به TP4"
            elif stage >= 11:
                status_text = "✅ وضعیت: TP4 زده شد"; next_step_text = "🚀 مرحله بعد: شکار سود در روند قوی"
        
        next_target = current_tp
        if stage < 5: next_target = tp1
        elif stage < 7: next_target = tp2
        elif stage < 9: next_target = tp3
        
        prog_bar = get_progress_bar(current_price, entry, next_target)
        
        card = f"{emoji} <b>{pos_key_base}</b> ({leverage}X)\n"
        card += f"💵 سود: <code>{pnl:+.2f} USDT ({current_roi:+.1f}%)</code>\n"
        card += f"{status_text}\n"
        if next_step_text: card += f"{next_step_text}\n"
        card += f"🛑 استاپ: <code>{float(current_sl):.5f}</code>\n"
        card += f"🎯 هدف: <code>{float(next_target):.5f}</code>\n"
        card += f"📊 پیشرفت:\n<code>[{prog_bar}]</code>\n"
        
        dashboard_parts.append(card)

    for key in list(state["targets"].keys()):
        if key not in open_keys and not state["targets"][key].get("closed", False):
            matching_closed = [p for p in closed_positions if str(p.get("id")) == key]
            if not matching_closed: continue

            matching_closed.sort(key=lambda x: x.get("updatedAt", ""), reverse=True)
            last_closed = matching_closed[0]
            realized_pnl = float(last_closed.get("realizedPnL") or 0)
            closing_reason = last_closed.get("closingReason") or ""
            pnl_text = f"{realized_pnl:+.2f} USDT"
            r
