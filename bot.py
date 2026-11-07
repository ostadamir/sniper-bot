import time, os, requests, hmac, hashlib, json
import datetime

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TRUETRADE_KEY = os.environ.get("TRUETRADE_KEY")
TRUETRADE_SECRET = os.environ.get("TRUETRADE_SECRET")

TRUETRADE_URL = "https://apiv2.thetruetrade.io"
STATE_FILE = "manager_state.json"
NO_RECORD = "هیچ پوزیشنی بسته نشده است."

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
            print("TG Edit 400 Error:", res.text)
            return False 
        print("edit failed:", res.status_code)
        return True 
    except Exception as e:
        print("TG Edit Error:", e)
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
        else: return 500, {}
        return res.status_code, res.json() if res.text else {}
    except Exception as e:
        return 500, str(e)

def patch_tpsl(pos_id, sl_value, tp_value):
    payload = {"stopLoss": str(round(sl_value, 6)), "takeProfit": str(round(tp_value, 6))}
    st, resp = tt_request("PATCH", f"/futures/positions/{pos_id}/tpsl", payload)
    if st not in (200, 201):
        print(f"!!! TrueTrade API Error for {pos_id}: Status {st} - Response: {resp}")
    return st in (200, 201)

def new_state():
    return {"targets": {}, "pending": {}, "offset": 0, "dashboard_id": None, "last_closed": NO_RECORD}

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            for k, v in new_state().items(): state.setdefault(k, v)
            for key, t in state["targets"].items():
                t.setdefault("current_stage", 0)
                t.setdefault("closed", False)
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
            tp3 = float(parts[7])
            tp2 = float(parts[6])
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

    new_sig = {"symbol": symbol, "side": sd, "entry": e, "sl": s, "tp1": tps[0], "tp2": tps[1], "tp3": tps[2], "tp4": tps[3], "current_stage": 0, "closed": False, "limit_stage": limit_stage, "fail_count": 0}
    for ex in state["pending"].get(pos_key, []):
        if ex["entry"] == e and ex["sl"] == s: return
    state["pending"].setdefault(pos_key, []).append(new_sig)
    
    msg = f"✅ سیگنال {pos_key} در اتاق انتظار ثبت شد."
    if limit_stage == 1: msg += "\n(حالت RF)"
    elif limit_stage == 2: msg += "\n(حالت SAFE)"
    send_telegram(msg)

def check_telegram(state):
    offset = state.get("offset", 0)
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", params={"offset": offset, "timeout": 5}, timeout=10).json()
        if not r.get("ok"): return
        for u in r.get("result", []):
            state["offset"] = u["update_id"] + 1
            msg = u.get("message")
            if not msg or "text" not in msg or str(msg.get("chat", {}).get("id")) != str(TELEGRAM_CHAT_ID): continue
            text = msg["text"]; parts = text.split()
            if text.startswith("/cancel"):
                if len(parts) == 3: handle_cancel(state, parts)
                else: send_telegram("❌ فرمت: /cancel SYMBOL SIDE")
            elif text.startswith("/new"): handle_new(state, parts)
    except Exception as e: print("TG Check Error:", e)

def get_progress_bar(current, entry, target):
    if entry == target: return "░░░░░░░░░░"
    if target > entry: pct = max(0.0, min(1.0, (current - entry) / (target - entry)))
    else: pct = max(0.0, min(1.0, (entry - current) / (entry - target)))
    filled = int(pct * 10)
    return "█" * filled + "░" * (10 - filled)

def get_close_status(stage, why):
    if why == "TAKEPROFIT": return "🎯 تارگت نهایی زده شد"
    if why == "MANUAL": return "🖐️ بسته شده دستی"
    status_map = {
        0: "❌ حد ضرر اولیه", 1: "⚖️ ریسک‌فری (صفر)", 2: "💰 قفل سود ۱۰٪",
        3: "🛡️ استاپ ۲۲٪ مسیر TP1", 4: "🛡️ استاپ ۴۰٪ مسیر TP1", 5: "🎯 TP1 زده شد (استاپ ۸۰٪)",
        6: "🛡️ استاپ روی TP1", 7: "🎯 TP2 زده شد (استاپ ۵۰٪)", 8: "🛡️ استاپ روی TP2",
        9: "🎯 TP3 زده شد (استاپ ۵۰٪)", 10: "🛡️ استاپ روی TP3", 11: "🎯 TP4 زده شد (استاپ ۵۰٪)",
        12: "🚀 شکار سود (استاپ روی TP4)"
    }
    return status_map.get(stage, "⚖️ بسته شده")

def next_stage(t, is_long, entry, price, roi, price_10, cur_sl, cur_tp, max_stage):
    sl, tp1, tp2, tp3, tp4 = t["sl"], t["tp1"], t["tp2"], t["tp3"], t["tp4"]
    d1, d2, d3, d4 = abs(tp1 - entry), abs(tp2 - tp1), abs(tp3 - tp2), abs(tp4 - tp3)
    g = 1 if is_long else -1
    p40, p22 = entry + g * d1 * 0.4, entry + g * d1 * 0.22
    p70, p80 = entry + g * d1 * 0.7, entry + g * d1 * 0.8
    h2, h3, h4 = tp1 + g * d2 * 0.5, tp2 + g * d3 * 0.5, tp3 + g * d4 * 0.5

    def hit(level): return price >= level if is_long else price <= level

    chain = [
        (0, "roi", 10.0, entry, "ریسک‌فری"), (1, "roi", 20.0, price_10, "قفل سود ۱۰٪"),
        (2, "price", p40, p22, "عبور از ۴۰٪ راه TP1"), (3, "price", p70, p40, "عبور از ۷۰٪ راه TP1"),
        (4, "price", tp1, p80, "رسیدن به TP1"), (5, "price", h2, tp1, "عبور از ۵۰٪ راه TP2"),
        (6, "price", tp2, h2, "رسیدن به TP2"), (7, "price", h3, tp2, "عبور از ۵۰٪ راه TP3"),
        (8, "price", tp3, h3, "رسیدن به TP3"), (9, "price", h4, tp3, "عبور از ۵۰٪ راه TP4"),
        (10, "price", tp4, h4, "رسیدن به TP4"),
    ]
    stage, new_sl, new_tp, reason = t["current_stage"], cur_sl, cur_tp, ""
    for st, kind, level, sl_to, why in chain:
        if stage != st or stage >= max_stage: continue
        if (roi >= level) if kind == "roi" else hit(level):
            new_sl, stage, reason = sl_to, st + 1, why
    if stage >= 11 and stage < max_stage and hit(cur_tp):
        new_tp = cur_tp + d4 if is_long else cur_tp - d4
        new_sl, stage, reason = t["tp4"], stage + 1, "تمدید تارگت"
    return stage, new_sl, new_tp, reason

STAGE_TEXT = {
    0: ("⏳ وضعیت: منتظر ۱۰٪ سود (ریسک‌فری)", ""), 1: ("✅ وضعیت: ریسک‌فری شد", "🚀 مرحله بعد: در مسیر رسیدن به ۲۰٪ سود"),
    2: ("✅ وضعیت: قفل سود ۱۰٪", "🚀 مرحله بعد: در مسیر رسیدن به ۴۰٪ TP1"), 3: ("✅ وضعیت: استاپ ۲۲٪ قفل شد", "🚀 مرحله بعد: در مسیر رسیدن به ۷۰٪ TP1"),
    4: ("✅ وضعیت: استاپ ۴۰٪ قفل شد", "🚀 مرحله بعد: در مسیر رسیدن به TP1"), 5: ("✅ وضعیت: TP1 زده شد", "🚀 مرحله بعد: در مسیر رسیدن به ۵۰٪ TP2"),
    6: ("✅ وضعیت: استاپ روی TP1 قفل شد", "🚀 مرحله بعد: در مسیر رسیدن به TP2"), 7: ("✅ وضعیت: TP2 زده شد", "🚀 مرحله بعد: در مسیر رسیدن به ۵۰٪ TP3"),
    8: ("✅ وضعیت: استاپ روی TP2 قفل شد", "🚀 مرحله بعد: در مسیر رسیدن به TP3"), 9: ("✅ وضعیت: TP3 زده شد", "🚀 مرحله بعد: در مسیر رسیدن به ۵۰٪ TP4"),
    10: ("✅ وضعیت: استاپ روی TP3 قفل شد", "🚀 مرحله بعد: در مسیر رسیدن به TP4"), 11: ("✅ وضعیت: TP4 زده شد", "🚀 مرحله بعد: شکار سود در روند قوی"),
}

def stage_texts(stage, max_stage):
    if max_stage == 1 and stage >= 1: return "🏁 وضعیت: ریسک‌فری شد (پایان مدیریت RF)", ""
    if max_stage == 2 and stage >= 2: return "🏁 وضعیت: قفل سود ۱۰٪ شد (پایان مدیریت SAFE)", ""
    return STAGE_TEXT[min(stage, 11)]

def manage_positions(state):
    status, response = tt_request("GET", "/futures/positions")
    if status != 200 or not isinstance(response, dict): return

    items = response.get("items", [])
    open_positions = [p for p in items if p.get("status") == "OPENED"]
    closed_positions = [p for p in items if p.get("status") == "CLOSED"]
    open_positions.sort(key=lambda p: p.get("createdAt", ""))
    open_keys = set()

    parts = ["📊 <b>داشبورد مدیریت سرمایه</b>\n━━━━━━━━━━━━━━━━━━━━━"]
    total_pnl = 0.0
    MAX_LEN = 3300 
    current_len = sum(len(s) for s in parts)
    hidden_count = 0
    unmanaged = []

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
                send_telegram(f"🔗 <b>اتصال موفق!</b>\nپوزیشن {key_base} به سیگنال متصل شد.")
            else:
                unmanaged.append(key_base)
                continue

        if pos_id not in state["targets"]: continue
        t = state["targets"][pos_id]
        if t.get("closed", False): continue

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
        total_pnl += pnl
        roi = (pnl / margin) * 100
        d10 = (margin * 0.10) / size
        price_10 = entry + d10 if is_long else entry - d10

        tol = max(1e-8, price * 1e-5)

        # 🛑 ۱. تشخیص استاپ دستی
        if cur_sl > 0:
            adopt_tol = max(tol, price * 5e-4)
            def at_or_better(level):
                return (cur_sl >= level - adopt_tol) if is_long else (cur_sl <= level + adopt_tol)
            adopted = t["current_stage"]
            if adopted < 1 and at_or_better(entry): adopted = 1
            if adopted < 2 and at_or_better(price_10): adopted = 2
            adopted = min(adopted, t.get("limit_stage", 99))
            if adopted > t["current_stage"]:
                t["current_stage"] = adopted
                send_telegram(f"🔎 <b>{key_base}:</b> استاپ دستی شما شناسایی شد (مرحله {adopted}).")

        # 🛑 ۲. تلاش سایلنت برای صفر کردن استاپ (ایده جدید!)
        if t["current_stage"] >= 1 and cur_sl > 0:
            # آیا استاپ فعلی، از نقطه ورود بدتره؟ (برای شورت یعنی بالاتر، برای لانگ یعنی پایین‌تر)
            is_worse = (cur_sl > entry + tol) if not is_long else (cur_sl < entry - tol)
            if is_worse:
                # برای اینکه صرافی ارور نده، فقط وقتی تلاش میکنیم که قیمت به اندازه کافی از نقطه ورود فاصله گرفته باشه
                distance_to_entry = abs(price - entry)
                if distance_to_entry > (price * 0.0015): # حداقل 0.15% فاصله
                    if patch_tpsl(pos_id, entry, cur_tp):
                        cur_sl = entry
                        print(f"SILENT SUCCESS: Adjusted SL to exact entry for {key_base}")

        # ۳. محافظت از استاپ (اگه استاپ صفر شد یا شل شد)
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
                    elif (final_sl - price) <= tolerance:
                        final_sl = price * 0.9995
                        is_sl_valid = True
                else:
                    if final_sl > price: is_sl_valid = True
                    elif (price - final_sl) <= tolerance:
                        final_sl = price * 1.0005
                        is_sl_valid = True

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
                    print(f"Stage update failed {key_base}: final_sl={final_sl} price={price} entry={entry} cur_sl={cur_sl}")
                    t["fail_count"] = t.get("fail_count", 0) + 1
                    if t["fail_count"] == 3:
                        send_telegram(f"⚠️ <b>{key_base}:</b> ۳ بار تغییر استاپ رد شد (جزئیات تو لاگ).")

        stage = t["current_stage"]
        status_text, next_text = stage_texts(stage, max_stage)
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

        if current_len > (MAX_LEN - 500):
            card = f"{emoji} <b>{key_base}</b> ({leverage}X) | {mode_txt}\n"
            card += f"💵 سود: <code>{pnl:+.2f} USDT ({roi:+.1f}%)</code>\n"
        else:
            card = f"{emoji} <b>{key_base}</b> ({leverage}X) | {mode_txt}\n"
            card += f"💵 سود: <code>{pnl:+.2f} USDT ({roi:+.1f}%)</code>\n"
            card += f"{status_text}\n"
            if next_text: card += f"{next_text}\n"
            card += f"🛑 استاپ: <code>{float(cur_sl):.5f}</code>\n"
            card += f"🎯 هدف: <code>{float(target):.5f}</code>\n"
            card += f"📊 پیشرفت:\n<code>[{bar}]</code>\n"

        if current_len + len(card) > MAX_LEN:
            hidden_count += 1
            continue

        parts.append(card)
        current_len += len(card)

    for key in list(state["targets"].keys()):
        if key in open_keys or state["targets"][key].get("closed", False): continue
        match = [p for p in closed_positions if str(p.get("id")) == key]
        if not match: continue
        match.sort(key=lambda x: x.get("updatedAt", ""), reverse=True)
        last = match[0]
        realized = float(last.get("realizedPnL") or 0)
        why = last.get("closingReason") or ""
        
        t = state["targets"][key]
        stage = t.get("current_stage", 0) 
        reasons = {"STOPLOSS": "حد ضرر", "TAKEPROFIT": "حد سود", "MANUAL": "دستی"}
        reason_text = reasons.get(why, why)
        status_msg = get_close_status(stage, why)

        sym = last.get("symbol", "").replace("USDT", "")
        sd = last.get("side", "")
        name = f"{sym}_{sd}" if sym and sd else key
        pnl_text = f"{realized:+.2f} USDT"
        
        state["last_closed"] = f"🔴 {name} | {status_msg}\nدلیل API: {reason_text} | PnL: {pnl_text}"
        send_telegram(f"🏁 <b>پوزیشن {name} بسته شد!</b>\nوضعیت: {status_msg}\nدلیل صرافی: {reason_text}\nسود/ضرر: {pnl_text}")
        
        del state["targets"][key]

    if not open_positions: parts.append("هیچ پوزیشن بازی وجود ندارد. ⏳")
    else: parts.insert(1, f"📈 سود شناور کل: <code>{total_pnl:+.2f} USDT</code>\n")

    if hidden_count > 0:
        parts.append(f"\n⚠️ <i>{hidden_count} پوزیشن دیگر به دلیل محدودیت طول پیام نمایش داده نشد.</i>")
    
    if unmanaged:
        parts.append(f"⚠️ <i>بدون سیگنال: {', '.join(unmanaged)}</i>")
    
    parts.append("━━━━━━━━━━━━━━━━━━━━━")
    parts.append(f"🏁 <b>آخرین بسته شده:</b>\n{state['last_closed']}")
    now = datetime.datetime.now(datetime.timezone.utc)
    iran = now + datetime.timedelta(hours=3, minutes=30)
    parts.append(f"🕒 آپدیت: <code>{iran.strftime('%H:%M')}</code>")
        final_text = "\n".join(parts)

        if len(final_text) > 4096:
            final_text = final_text[:3900] + "\n\n⚠️ <i>پیام به دلیل محدودیت تلگرام کوتاه شد.</i>"

        if state.get("dashboard_id"):
            if not edit_telegram(state["dashboard_id"], final_text):
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
