import time, os, requests, hmac, hashlib, json
import datetime, html, re
import statistics
import traceback

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TRUETRADE_KEY = os.environ.get("TRUETRADE_KEY")
TRUETRADE_SECRET = os.environ.get("TRUETRADE_SECRET")

TRUETRADE_URL = "https://apiv2.thetruetrade.io"
STATE_FILE = "manager_state.json"
QUEUE_FILE = "pending_signals.txt"

# ===== تنظیمات مدیریت سرمایه =====
PORTFOLIO_TP_PERCENT_1 = 10.0  
PORTFOLIO_TP_PERCENT_2 = 3.2   
PORTFOLIO_TP_PERCENT_3 = 3.8   
PORTFOLIO_TP_PERCENT_4 = 4.0   
PORTFOLIO_TP_PERCENT_5 = 4.0   
PORTFOLIO_TP_PERCENT_DEFAULT = 10.0

MAX_AUTO_POSITIONS = 2
AUTO_MARGIN_PERCENT = 30.0
AUTO_LEVERAGE = 30
MAX_ENTRY_DEV = 0.01
MIN_TP_DIST = 0.003      
MIN_SL_DIST = 0.001      

MAX_DAILY_LOSS_PERCENT = 30.0
PENDING_TTL_SEC = 24 * 3600
LIMIT_ORDER_TTL_SEC = 24 * 3600
ERROR_TTL_SEC = 12 * 3600 # 👈 تغییر به ۱۲ ساعت (طبق خواسته تو)
MISSING_LIMIT = 30

USAGE = ("فرمت دستور:\n/new SYMBOL SIDE ENTRY SL TP1 TP2 TP3 TP4 [RF|SAFE]\n/newi ...\n/cancel SYMBOL SIDE\n/auto ...\n/auto_limit ... HASHTAG\n/cancel_limit HASHTAG")

def _norm(x): return str(x or "").strip().lower()
def strip_tags(t): return html.unescape(re.sub(r"<[^>]+>", "", str(t)))
def fmt_num(x): return (f"{float(x):.10f}".rstrip("0").rstrip(".")) or "0"

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        res = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
        if res.status_code == 200: return res.json().get("result", {}).get("message_id")
        if res.status_code == 400: 
            r2 = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": strip_tags(text)[:4000]}, timeout=15)
            if r2.status_code == 200: return r2.json().get("result", {}).get("message_id")
    except Exception as e: print("TG Error:", repr(e), flush=True)
    return None

def edit_telegram(message_id, text):
    if not message_id: return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
    for attempt in range(3):
        try:
            res = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id, "text": text, "parse_mode": "HTML"}, timeout=15)
            if res.status_code == 200: return True
            if res.status_code == 400:
                if "message is not modified" in res.text: return True
                if "message to edit not found" in res.text: return False
                r2 = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id, "text": strip_tags(text)[:4000]}, timeout=15)
                if r2.status_code == 200: return True
            time.sleep(2)
        except Exception: time.sleep(2)
    return True 

def notify(msg):
    print("NOTIFY:", strip_tags(msg)[:300].replace("\n", " | "), flush=True)
    return send_telegram(msg)

# ================= Check Engine (Error Logger) =================
def report_error(state, err_id, title, details, level="🔴"):
    state.setdefault("active_errors", {})[err_id] = {
        "title": title, "details": details, "level": level, 
        "time": datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M'),
        "ts": time.time()
    }

def resolve_error(state, err_id):
    if err_id in state.get("active_errors", {}):
        del state["active_errors"][err_id]

def render_check_engine(state):
    errs = state.get("active_errors", {})
    now_ts = time.time()
    for eid in list(errs.keys()):
        errs[eid].setdefault("ts", now_ts)
        if now_ts - errs[eid]["ts"] > ERROR_TTL_SEC:
            del errs[eid]

    if not errs:
        text = "✅ <b>داشبورد مانیتورینگ (Check Engine)</b>\n━━━━━━━━━━━━━━━━━━━━━\n💚 سیستم در وضعیت ایده‌آل قرار دارد.\nهیچ خطای فعالی یافت نشد."
    else:
        text = "🚨 <b>داشبورد مانیتورینگ (Check Engine)</b>\n━━━━━━━━━━━━━━━━━━━━━\n"
        for eid, e in errs.items():
            text += f"{e['level']} <b>{html.escape(str(e['title']))}</b>\n"
            text += f"📝 جزئیات: {html.escape(str(e['details']))}\n"
            text += f"🕒 زمان: {e['time']}\n"
            text += "━━━━━━━━━━━━━━━━━━━━━\n"
    
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3, minutes=30)
    text += f"\n🕒 <b>آخرین آپدیت:</b> {now.strftime('%H:%M:%S')}"

    if state.get("check_engine_id"):
        if not edit_telegram(state["check_engine_id"], text):
            state["check_engine_id"] = send_telegram(text)
    else:
        state["check_engine_id"] = send_telegram(text)

# ================= TrueTrade API =================
def exchange_error_text(res):
    if isinstance(res, dict):
        errs = res.get("errors")
        if isinstance(errs, list) and errs:
            return "؛ ".join(str(x.get("message", x)) if isinstance(x, dict) else str(x) for x in errs)
        if res.get("message"): return str(res["message"])
    return str(res)[:300]

def tt_sign(secret, timestamp, method, uri):
    payload = f"{timestamp}{method.upper()}{uri}"
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()

def tt_request(method, uri, payload=None):
    if not TRUETRADE_KEY or not TRUETRADE_SECRET: return 500, {}
    timestamp = str(int(time.time() * 1000))
    signature = tt_sign(TRUETRADE_SECRET, timestamp, method, uri)
    headers = {"X-API-Key": TRUETRADE_KEY, "X-Timestamp": timestamp, "X-Signature": signature, "Content-Type": "application/json"}
    try:
        if method == "GET": res = requests.get(f"{TRUETRADE_URL}{uri}", headers=headers, timeout=15)
        elif method == "PATCH": res = requests.patch(f"{TRUETRADE_URL}{uri}", headers=headers, json=payload, timeout=15)
        elif method == "POST": res = requests.post(f"{TRUETRADE_URL}{uri}", headers=headers, json=payload, timeout=15)
        elif method == "DELETE": res = requests.delete(f"{TRUETRADE_URL}{uri}", headers=headers, json=payload, timeout=15)
        else: return 500, {}
        return res.status_code, res.json() if res.text else {}
    except Exception as e: return 500, str(e)

def patch_tpsl(state, pos_id, sl_value, tp_value):
    payload = {"stopLoss": fmt_num(sl_value), "takeProfit": fmt_num(tp_value)}
    err_key = f"patch_{pos_id}"
    
    for attempt in range(3):
        try:
            st, res = tt_request("PATCH", f"/futures/positions/{pos_id}/tpsl", payload)
            if st in (200, 201):
                resolve_error(state, err_key)
                return True
            print(f"🚨 PATCH FAILED (Attempt {attempt+1}): {st} | {res}", flush=True)
            report_error(state, err_key, "خطای تغییر استاپ/تارگت", f"HTTP {st}: {str(res)[:100]}", "🔴")
        except Exception as e:
            print(f"🚨 PATCH ERROR: {e}", flush=True)
            report_error(state, err_key, "خطای تغییر استاپ/تارگت", str(e)[:100], "🔴")
        time.sleep(2)
    return False

def close_position_market(state, pos_id):
    st, res = tt_request("POST", f"/futures/positions/{pos_id}/close", payload={"type": "MARKET"})
    err_key = f"close_{pos_id}"
    if st not in (200, 201):
        report_error(state, err_key, "خطای بستن پوزیشن", exchange_error_text(res), "🔴")
        return False
    resolve_error(state, err_key)
    return True

def cancel_limit_order(state, order_id):
    st, res = tt_request("DELETE", f"/futures/orders/{order_id}")
    if st in (200, 201, 204):
        return True
    print(f"Cancel Failed: {st} | {res}")
    return False

def extract_list(res):
    if isinstance(res, list): return res
    if isinstance(res, dict):
        for k in ("items", "data", "assets", "result", "balances"):
            if isinstance(res.get(k), list): return res[k]
    return []

def extract_id(res, keys=("id", "positionId", "orderId")):
    if not isinstance(res, dict): return None
    for src in (res, res.get("data"), res.get("item"), res.get("position"), res.get("order")):
        if isinstance(src, dict):
            for k in keys:
                if src.get(k) is not None: return str(src[k])
    return None

def get_futures_balance(state):
    st, res = tt_request("GET", "/futures/assets")
    if st != 200: 
        report_error(state, "balance_err", "خطا در خواندن موجودی", f"HTTP {st}", "🟡")
        return None
    resolve_error(state, "balance_err")
    for a in extract_list(res):
        if isinstance(a, dict) and _norm(a.get("symbol")) == "usdt":
            try: return float(a.get("totalAssets") or 0), float(a.get("availableBalance") or 0)
            except ValueError: pass
    return None

def get_market_price(state, symbol):
    s = symbol.upper()
    tries = [
        (f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={s}-USDT", lambda j: j["data"]["price"]),
        (f"https://www.okx.com/api/v5/market/ticker?instId={s}-USDT-SWAP", lambda j: j["data"][0]["last"]),
        (f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={s}USDT", lambda j: j["result"]["list"][0]["lastPrice"]),
    ]
    for url, pick in tries:
        try:
            r = requests.get(url, timeout=8)
            if r.status_code == 200:
                p = float(pick(r.json()))
                if p > 0: 
                    resolve_error(state, f"price_{symbol}")
                    return p
        except: pass
    report_error(state, f"price_{symbol}", "خطا در دریافت قیمت", f"قیمت {symbol} خوانده نشد.", "🟡")
    return None

def check_against_market(side, price, e, sl, tps, leverage):
    tp_last, tp1 = tps[-1], tps[0]
    if side == "LONG":
        if sl >= price * (1 - MIN_SL_DIST): return "استاپ باید پایین‌تر از قیمت باشد"
        if tp_last <= price * (1 + MIN_TP_DIST): return "تارگت آخر باید بالاتر از قیمت باشد"
        if price >= tp1: return "قیمت از TP1 رد شده (سیگنال دیر شده)"
    else:
        if sl <= price * (1 + MIN_SL_DIST): return "استاپ باید بالاتر از قیمت باشد"
        if tp_last >= price * (1 - MIN_TP_DIST): return "تارگت آخر باید پایین‌تر از قیمت باشد"
        if price <= tp1: return "قیمت از TP1 رد شده (سیگنال دیر شده)"
    if abs(price - sl) / price > 0.6 / leverage: return "استاپ برای این لوریج خیلی دور است (نزدیک لیکویید)"
    return None

def valid_signal(side, e, s, tps, leverage):
    side = side.upper()
    if side == "LONG":
        if s >= e: return "استاپ باید پایین‌تر از ورود باشد", tps, ""
    elif side == "SHORT":
        if s <= e: return "استاپ باید بالاتر از ورود باشد", tps, ""
    else:
        return "side", tps, ""

    ordered = (e < tps[0] < tps[1] < tps[2] < tps[3] < tps[4]) if side == "LONG" else (e > tps[0] > tps[1] > tps[2] > tps[3] > tps[4])
    if ordered:
        if abs(e - s) / e > 0.6 / leverage: return "sl_too_far", tps, ""
        return None, tps, ""

    points = [e] + tps
    diffs = [abs(points[i+1] - points[i]) for i in range(len(points) - 1)]
    try: standard_step = statistics.median(diffs)
    except: standard_step = 0
    if standard_step <= 0: return "فاصله تارگت‌ها نامعتبر است", tps, ""

    g = 1 if side == "LONG" else -1
    fixed_tps = [float(f"{e + (g * standard_step * i):.8g}") for i in range(1, 6)]
        
    fix_msg = ""
    for i in range(5):
        if abs(fixed_tps[i] - tps[i]) > (standard_step * 0.3):
            fix_msg += f"🔧 TP{i+1} اصلاح شد از {tps[i]} به {fixed_tps[i]}\n"

    if side == "LONG" and not (e < fixed_tps[0] < fixed_tps[1] < fixed_tps[2] < fixed_tps[3] < fixed_tps[4]):
        return "ترتیب تارگت‌های اصلاح‌شده اشتباه است", tps, ""
    if side == "SHORT" and not (e > fixed_tps[0] > fixed_tps[1] > fixed_tps[2] > fixed_tps[3] > fixed_tps[4]):
        return "ترتیب تارگت‌های اصلاح‌شده اشتباه است", tps, ""

    if abs(e - s) / e > 0.6 / leverage: return "sl_too_far", tps, ""
    return None, fixed_tps, fix_msg.strip()

def get_dynamic_tp_percent(num_positions):
    if num_positions == 1: return PORTFOLIO_TP_PERCENT_1
    if num_positions == 2: return PORTFOLIO_TP_PERCENT_2
    if num_positions == 3: return PORTFOLIO_TP_PERCENT_3
    if num_positions == 4: return PORTFOLIO_TP_PERCENT_4
    if num_positions == 5: return PORTFOLIO_TP_PERCENT_5
    return PORTFOLIO_TP_PERCENT_DEFAULT

def open_auto_position(state, symbol, side, margin_usdt, leverage, sl, tp, is_limit=False, entry=None):
    payload = {
        "symbol": f"{symbol}USDT", "side": side.upper(), 
        "tradeType": "LIMIT" if is_limit else "MARKET",
        "leverage": leverage, "cost": fmt_num(round(margin_usdt, 2)), 
        "walletType": "debit", "stopLoss": fmt_num(sl), "takeProfit": fmt_num(tp)
    }
    if is_limit:
        payload["price"] = fmt_num(entry)

    st, res = tt_request("POST", "/futures/positions", payload)
    if st not in (200, 201):
        msg = exchange_error_text(res)
        notify(f"⚠️ صرافی رد کرد ({st}):\n<code>{html.escape(msg)}</code>")
        report_error(state, f"open_{symbol}", "خطای باز کردن پوزیشن", msg, "🔴")
        return None
    resolve_error(state, f"open_{symbol}")
    return res if res else {}

# ================= State & Handlers =================
def new_state(): return {
    "targets": {}, "pending": {}, "limit_orders": {}, "queue_retries": {}, "offset": 0, 
    "dashboard_id": None, "auto_dashboard_id": None, "check_engine_id": None, 
    "active_errors": {}, "day": None, "day_balance": 0.0
}

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f: state = json.load(f)
            for k, v in new_state().items(): state.setdefault(k, v)
            for key, t in state["targets"].items():
                t.setdefault("current_stage", 0); t.setdefault("closed", False); t.setdefault("is_auto", False); t.setdefault("is_isolated", False); t.setdefault("limit_stage", 99); t.setdefault("fail_count", 0); t.setdefault("missing", 0)
            return state
        except: pass
    return new_state()

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=4)

def daily_loss_exceeded(state, balance):
    today = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3, minutes=30)).strftime("%Y-%m-%d")
    if state.get("day") != today:
        state["day"] = today
        state["day_balance"] = balance
        return False
    start = state.get("day_balance") or balance
    if start <= 0: return False
    return (start - balance) / start * 100 >= MAX_DAILY_LOSS_PERCENT

def cleanup_pending(state):
    now = time.time()
    for key in list(state["pending"].keys()):
        for s in state["pending"][key]:
            s.setdefault("created", now)
        fresh = [s for s in state["pending"][key] if now - s["created"] < PENDING_TTL_SEC]
        if fresh: state["pending"][key] = fresh
        else: del state["pending"][key]

def handle_cancel(state, parts):
    if len(parts) < 3: return
    symbol = parts[1].upper().replace("USDT", ""); side = parts[2].upper()
    for k in list(state["targets"].keys()):
        tg = state["targets"][k]
        if tg.get("symbol") == symbol and tg.get("side") == side: del state["targets"][k]
    if f"{symbol}_{side}" in state["pending"]: del state["pending"][f"{symbol}_{side}"]
    notify(f"🛑 مدیریت {symbol}_{side} لغو شد.")

def handle_cancel_limit(state, parts):
    if len(parts) < 2: return
    tag = parts[1].strip()
    order_info = state.get("limit_orders", {}).get(tag)
    if not order_info:
        notify(f"ℹ️ سفارش لیمیت {tag} در لیست نبود (قبلاً فعال یا کنسل شده)."); return

    order_id = order_info.get("order_id")
    # 👇 FIX 1: اگر کنسل ناموفق بود، خطا بده تا صف دوباره تلاش کنه
    if order_id and cancel_limit_order(state, order_id):
        notify(f"🚫 سفارش لیمیت {tag} ({order_info['symbol']}) طبق پیام چنل کنسل شد.")
        del state["limit_orders"][tag]
    else:
        raise RuntimeError(f"cancel {tag} failed")

def handle_new(state, parts, is_isolated=False):
    limit_stage = 99
    mode_str = ""
    if parts[-1].upper() in ("RF", "SAFE"):
        mode = parts[-1].upper()
        limit_stage = 1 if mode == "RF" else 2
        mode_str = mode
        parts = parts[:-1]

    if len(parts) > 9:
        parts = parts[:9] 
    elif len(parts) == 8:
        try: parts.append(str(round(float(parts[7]) + (float(parts[7]) - float(parts[6])), 8)))
        except: pass

    if len(parts) != 9:
        notify("❌ فرمت اشتباه است.\n" + USAGE); return

    symbol = parts[1].upper().replace("USDT", ""); sd = parts[2].upper(); pos_key = f"{symbol}_{sd}"
    try: e = float(parts[3]); s = float(parts[4]); tps = [float(x) for x in parts[5:9]]
    except ValueError: notify("❌ اعداد معتبر نیستند."); return

    if sd not in ("LONG", "SHORT") or e <= 0:
        notify("❌ SIDE باید LONG یا SHORT باشد."); return
    g = 1 if sd == "LONG" else -1
    if (s - e) * g >= 0:
        notify("❌ استاپ در سمت اشتباه است."); return
    pts = [e] + tps
    if not all((pts[i + 1] - pts[i]) * g > 0 for i in range(4)):
        notify("❌ ترتیب تارگت‌ها اشتباه است."); return

    if abs(e - s) / e > 0.02:
        old_s = s
        s = e - (g * e * 0.02)
        notify(f"⚠️ استاپ سیگنال {abs((e-old_s)/e)*100:.2f}% بود. طبق قانون به ۲٪ محدود شد.")

    new_sig = {"symbol": symbol, "side": sd, "entry": e, "sl": s, "tp1": tps[0], "tp2": tps[1], "tp3": tps[2], "tp4": tps[3], "current_stage": 0, "closed": False, "is_auto": False, "is_isolated": is_isolated, "limit_stage": limit_stage, "fail_count": 0, "created": time.time()}
    for ex in state["pending"].get(pos_key, []):
        if ex["entry"] == e and ex["sl"] == s: return
    state["pending"].setdefault(pos_key, []).append(new_sig)
    msg_type = "آزاد 🔒" if is_isolated else "دستی"
    notify(f"✅ سیگنال {msg_type} {pos_key} ثبت شد." + (f" ({mode_str})" if mode_str else ""))

def handle_auto(state, parts, is_limit=False):
    tag = None
    if is_limit:
        if len(parts) < 11:
            notify("❌ /auto_limit بدون هشتگ رد شد."); return
        tag = parts[-1]
        parts = parts[:-1]

    if len(parts) < 10:
        notify("❌ فرمت اشتباه."); return

    symbol = parts[1].upper().replace("USDT", ""); sd = parts[2].upper()
    try: e = float(parts[3]); s = float(parts[4]); tps = [float(x) for x in parts[5:10]]
    except ValueError: notify("❌ اعداد معتبر نیستند."); return

    # 👇 فیلتر استاپ ۱ درصد و بافر ۰.۲۵ درصد
    sl_dist = abs(e - s) / e
    if sl_dist > 0.01:
        notify(f"⛔ {symbol}_{sd} رد شد: استاپ سیگنال {sl_dist*100:.2f}% است (بیشتر از ۱٪ مجاز).")
        return
    
    g = 1 if sd == "LONG" else -1
    s = s - (g * e * 0.0025)

    err, fixed_tps, fix_msg = valid_signal(sd, e, s, tps, AUTO_LEVERAGE)
    if err:
        notify(f"❌ {symbol}_{sd} رد شد ({err})."); return
    if fix_msg:
        notify(f"⚠️ {symbol}_{sd}:\n{fix_msg}")
        tps = fixed_tps

    price = get_market_price(state, symbol)
    if price is None:
        notify(f"⚠️ قیمت {symbol} گرفته نشد؛ سیگنال رد شد."); return

    if is_limit:
        if (sd == "LONG" and e >= price) or (sd == "SHORT" and e <= price):
            notify(f"⛔ لیمیت {symbol}_{sd} رد شد: قیمت ورود در سمت اشتباه بازار است."); return
        if (sd == "LONG" and price <= s) or (sd == "SHORT" and price >= s):
            notify(f"⛔ لیمیت {symbol}_{sd} رد شد: قیمت بازار از استاپ رد شده."); return
        if tag in state["limit_orders"] or any(o["symbol"] == symbol and o["side"] == sd for o in state["limit_orders"].values()):
            notify(f"⚠️ برای {symbol}_{sd} لیمیت فعال وجود دارد."); return
    else:
        why = check_against_market(sd, price, e, s, tps, AUTO_LEVERAGE)
        if why: notify(f"⛔ {symbol}_{sd} رد شد: {why}"); return
        dev = abs(price - e) / e
        if dev > MAX_ENTRY_DEV:
            notify(f"⛔ {symbol}_{sd} رد شد: قیمت بازار {dev*100:.1f}٪ فاصله دارد."); return

    # 👇 FIX 2: چک کردن موجودی و سقف ضرر قبل از بستن پوزیشن قدیمی
    bal = get_futures_balance(state)
    if bal is None:
        notify("⚠️ موجودی خوانده نشد؛ سیگنال رد شد."); return
    balance, available = bal
    if daily_loss_exceeded(state, balance):
        notify(f"⛔ سقف ضرر روزانه ({MAX_DAILY_LOSS_PERCENT}٪) پر شده."); return

    target_margin = balance * AUTO_MARGIN_PERCENT / 100.0
    margin_to_use = min(target_margin, available * 0.98)
    if margin_to_use < 1.0:
        notify(f"⚠️ مارجین کم است (آزاد: {available:.2f}$)."); return

    st, resp = tt_request("GET", "/futures/positions")
    if st != 200 or not isinstance(resp, dict):
        notify("⚠️ لیست پوزیشن‌ها خوانده نشد؛ سیگنال رد شد."); return
    items = resp.get("items", [])

    for p in items:
        if p.get("status") == "OPENED" and p.get("symbol") == f"{symbol}USDT" and p.get("side") == sd:
            notify(f"⚠️ {symbol}_{sd} همین الان باز است."); return

    open_auto = [p for p in items if p.get("status") == "OPENED" and state["targets"].get(str(p["id"]), {}).get("is_auto")]

    # 👇 FIX 4: سقف پوزیشن برای مارکت و لیمیت یکسان
    total_auto = len(open_auto) + len(state["limit_orders"])
    if total_auto >= MAX_AUTO_POSITIONS:
        if open_auto:
            open_auto.sort(key=lambda x: str(x.get("createdAt", "")))
            oldest = open_auto[0]
            oldest_id = str(oldest["id"])
            pnl_old = float(oldest.get("unrealizedPnL") or 0)
            if pnl_old >= 0.01 and close_position_market(state, oldest_id):
                state["targets"].pop(oldest_id, None)
                notify(f"♻️ پوزیشن قدیمی {oldest.get('symbol')} با سود {pnl_old:+.2f}$ نقد شد.")
                time.sleep(2)
            else:
                notify(f"⚠️ سقف پر است و قدیمی‌ترین پوزیشن بسته نشد (PnL: {pnl_old:+.2f}$). سیگنال رد شد.")
                return
        else:
            notify(f"⚠️ سقف لیمیت‌ها پر است. سیگنال رد شد."); return

    res = open_auto_position(state, symbol, sd, margin_to_use, AUTO_LEVERAGE, s, tps[4], is_limit=is_limit, entry=e)
    if res is None: return

    if is_limit:
        order_id = extract_id(res, keys=("id", "orderId", "positionId"))
        if order_id:
            state["limit_orders"][tag] = {
                "order_id": order_id, "symbol": symbol, "side": sd, "entry": e,
                "sl": s, "tp1": tps[0], "tp2": tps[1], "tp3": tps[2], "tp4": tps[3], "tp5": tps[4],
                "created": time.time()
            }
            notify(f"⏳ سفارش لیمیت {symbol}_{sd} در {e} ثبت شد. هشتگ: {tag}")
        else:
            notify(f"⚠️ سفارش لیمیت ثبت شد ولی ID پیدا نشد. مدیریت دستی لازم است.")
    else:
        pos_id = extract_id(res, keys=("positionId", "id"))
        new_sig = {"symbol": symbol, "side": sd, "entry": e, "sl": s, "tp1": tps[0], "tp2": tps[1], "tp3": tps[2], "tp4": tps[3], "tp5": tps[4], "current_stage": 0, "closed": False, "is_auto": True, "is_isolated": False, "limit_stage": 99, "fail_count": 0, "created": time.time()}
        if pos_id:
            state["targets"][pos_id] = new_sig
            notify(f"🤖 پوزیشن خودکار {symbol}_{sd} باز شد.")
        else:
            state["pending"].setdefault(f"{symbol}_{sd}", []).append(new_sig)
            notify(f"🤖 پوزیشن {symbol}_{sd} باز شد (در انتظار اتصال).")

def process_queue(state):
    if not os.path.exists(QUEUE_FILE): return
    with open(QUEUE_FILE, "r") as f: lines = f.readlines()
    remaining = []
    tries = state.setdefault("queue_retries", {})
    
    for line in lines:
        line = line.strip()
        if not line.startswith("/"): continue
        try:
            parts = line.split()
            if line.startswith("/auto_limit"): handle_auto(state, parts, is_limit=True)
            elif line.startswith("/auto"): handle_auto(state, parts, is_limit=False)
            elif line.startswith("/cancel_limit"): handle_cancel_limit(state, parts)
            tries.pop(line, None) 
        except Exception as e:
            notify(f"⚠️ خطا در پردازش سیگنال: {html.escape(repr(e))}")
            tries[line] = tries.get(line, 0) + 1
            if tries[line] < 3:
                remaining.append(line)
            else:
                tries.pop(line, None)
                notify(f"🗑️ دستور بعد از ۳ بار تلاش حذف شد:\n<code>{html.escape(line)}</code>")
                
    with open(QUEUE_FILE, "w") as f:
        f.write("\n".join(remaining) + ("\n" if remaining else ""))

def check_telegram(state):
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", params={"offset": state.get("offset", 0), "timeout": 0}, timeout=15).json()
        if not r.get("ok"): return
        for u in r.get("result", []):
            state["offset"] = u["update_id"] + 1
            msg = u.get("message")
            if not msg or "text" not in msg or str(msg.get("chat", {}).get("id")) != str(TELEGRAM_CHAT_ID): continue

            for line in msg["text"].split('\n'):
                if not line.strip(): continue
                parts = line.strip().split()
                cmd = parts[0].split("@")[0].lower()
                try:
                    if cmd == "/cancel": handle_cancel(state, parts)
                    elif cmd == "/new": handle_new(state, parts, is_isolated=False)
                    elif cmd == "/newi": handle_new(state, parts, is_isolated=True)
                    elif cmd == "/auto": handle_auto(state, parts, is_limit=False)
                    # 👇 FIX 6: اضافه شدن کامندهای لیمیت به تلگرام
                    elif cmd == "/auto_limit": handle_auto(state, parts, is_limit=True)
                    elif cmd == "/cancel_limit": handle_cancel_limit(state, parts)
                    elif cmd == "/balance":
                        bal = get_futures_balance(state)
                        notify(f"💰 موجودی: {bal[0]:.2f}$ (آزاد: {bal[1]:.2f}$)" if bal else "⚠️ خطا")
                    elif cmd.startswith("/"): notify(f"❓ ناشناخته: {cmd}")
                except Exception as e: notify(f"⚠️ خطا در {cmd}: {html.escape(repr(e))}")
        if r.get("result"): requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", params={"offset": state["offset"], "limit": 1, "timeout": 0}, timeout=15)
    except Exception as e: print("TG Error:", e, flush=True)

# ================= Logic & Dashboard =================
def get_progress_bar(current, entry, target):
    if entry == target: return "░░░░░░░░░░"
    if target > entry: pct = max(0.0, min(1.0, (current - entry) / (target - entry)))
    else: pct = max(0.0, min(1.0, (entry - current) / (entry - target)))
    filled = int(pct * 10)
    return "█" * filled + "░" * (10 - filled)

def next_stage(t, is_long, entry, price, roi, price_10, cur_sl, cur_tp, max_stage, is_isolated):
    d1, d2, d3, d4 = abs(t["tp1"] - entry), abs(t["tp2"] - t["tp1"]), abs(t["tp3"] - t["tp2"]), abs(t["tp4"] - t["tp3"])
    g = 1 if is_long else -1
    p15 = entry + g * d1 * 0.15
    p40, p22, p70, p80 = entry + g*d1*0.4, entry + g*d1*0.22, entry + g*d1*0.7, entry + g*d1*0.8
    h2, h3, h4 = t["tp1"] + g*d2*0.5, t["tp2"] + g*d3*0.5, t["tp3"] + g*d4*0.5
    def hit(lvl): return price >= lvl if is_long else price <= lvl
    
    if is_isolated:
        chain = [(0, "roi", 10.0, entry, "ریسک‌فری (۱۰٪ رول)"), (1, "roi", 20.0, price_10, "قفل سود ۱۰٪"), (2, "price", p40, p22, "عبور از ۴۰٪ TP1"), (3, "price", p70, p40, "عبور از ۷۰٪ TP1"), (4, "price", t["tp1"], p80, "TP1 زده شد"), (5, "price", h2, t["tp1"], "عبور از ۵۰٪ TP2"), (6, "price", t["tp2"], h2, "TP2 زده شد"), (7, "price", h3, t["tp2"], "عبور از ۵۰٪ TP3"), (8, "price", t["tp3"], h3, "TP3 زده شد"), (9, "price", h4, t["tp3"], "عبور از ۵۰٪ TP4"), (10, "price", t["tp4"], h4, "TP4 زده شد")]
    else:
        chain = [(0, "price", p15, entry, "ریسک‌فری (۱۵٪ مسیر TP1)"), (1, "roi", 20.0, price_10, "قفل سود ۱۰٪"), (2, "price", p40, p22, "عبور از ۴۰٪ TP1"), (3, "price", p70, p40, "عبور از ۷۰٪ TP1"), (4, "price", t["tp1"], p80, "TP1 زده شد"), (5, "price", h2, t["tp1"], "عبور از ۵۰٪ TP2"), (6, "price", t["tp2"], h2, "TP2 زده شد"), (7, "price", h3, t["tp2"], "عبور از ۵۰٪ TP3"), (8, "price", t["tp3"], h3, "TP3 زده شد"), (9, "price", h4, t["tp3"], "عبور از ۵۰٪ TP4"), (10, "price", t["tp4"], h4, "TP4 زده شد")]

    stage, new_sl, new_tp, reason = t["current_stage"], cur_sl, cur_tp, ""
    for st, kind, level, sl_to, why in chain:
        if stage == st and stage < max_stage:
            if (roi >= level) if kind == "roi" else hit(level):
                new_sl, stage, reason = sl_to, st + 1, why
    if stage >= 11 and stage < max_stage and hit(cur_tp):
        new_tp, new_sl, stage = (cur_tp + d4 if is_long else cur_tp - d4), t["tp4"], stage + 1
    return stage, new_sl, new_tp, reason

def next_auto_stage(t, is_long, entry, price):
    g = 1 if is_long else -1; d1 = abs(t["tp1"] - entry)
    def hit(lvl): return price >= lvl if is_long else price <= lvl
    
    if t["current_stage"] == 0 and hit(t["tp2"]): 
        risk = abs(entry - t["sl"])
        new_sl = entry - (g * risk * 0.2)
        return 1, new_sl, "TP2 (قفل ۸۰٪ مسیر ریسک)"
    elif t["current_stage"] == 1 and hit(t["tp3"]): 
        dist = abs(t["tp1"] - entry)
        new_sl = t["tp1"] - (g * dist * 0.2)
        return 2, new_sl, "TP3 (قفل ۸۰٪ مسیر TP1)"
    elif t["current_stage"] == 2 and hit(t["tp4"]): 
        return 3, t["tp2"], "TP4 (قفل TP2)"
    elif t["current_stage"] == 3 and hit(t["tp5"]): 
        return 4, t["tp4"], "TP5 (کلوز)"
    return t["current_stage"], 0, ""

STAGE_TEXT = {
    0: ("⏳ منتظر ریسک‌فری", "۱۵٪ مسیر TP1 یا ۱۰٪ رول"),
    1: ("✅ ریسک‌فری شد", "۲۰٪ سود (قفل ۱۰٪)"),
    2: ("✅ قفل سود ۱۰٪", "عبور از ۴۰٪ مسیر TP1"),
    3: ("✅ استاپ ۲۲٪ قفل شد", "عبور از ۷۰٪ مسیر TP1"),
    4: ("✅ استاپ ۴۰٪ قفل شد", "رسیدن به TP1"),
    5: ("✅ TP1 زده شد", "عبور از ۵۰٪ مسیر TP2"),
    6: ("✅ استاپ روی TP1", "رسیدن به TP2"),
    7: ("✅ TP2 زده شد", "عبور از ۵۰٪ مسیر TP3"),
    8: ("✅ استاپ روی TP2", "رسیدن به TP3"),
    9: ("✅ TP3 زده شد", "عبور از ۵۰٪ مسیر TP4"),
    10: ("✅ استاپ روی TP3", "رسیدن به TP4"),
    11: ("✅ TP4 زده شد", "شکار سود")
}

AUTO_STAGE_TEXT = {
    0: ("منتظر TP2", "TP2"),
    1: ("✅ TP2 زده شد (قفل ۸۰٪ ریسک)", "TP3"),
    2: ("✅ TP3 زده شد (قفل ۸۰٪ TP1)", "TP4"),
    3: ("✅ TP4 زده شد (قفل TP2)", "TP5 (کلوز)"),
    4: ("✅ TP5 (کلوز)", "") 
}

def get_close_status(stage, why, is_auto=False):
    if why == "TAKEPROFIT": return "🎯 تارگت نهایی زده شد"
    if why == "MANUAL": return "🖐️ بسته شده به صورت دستی"
    if why == "STOPLOSS":
        if stage == 0: return "❌ حد ضرر اولیه"
        if is_auto: return "🛡️ استاپ ترِیلینگ (قفل سود خودکار)"
        else:
            if stage == 1: return "⚖️ حد ضرر ریسک‌فری (Breakeven)"
            if stage == 2: return "💰 استاپ قفل سود ۱۰٪"
            if stage >= 3: return "🛡️ استاپ ترِیلینگ (قفل سود پله‌ای)"
    return "⚖️ بسته شده (نامشخص)"

def manage_positions(state):
    now_ts = time.time()

    status, response = tt_request("GET", "/futures/positions")
    if status != 200 or not isinstance(response, dict):
        report_error(state, "fetch_pos", "خطا در دریافت پوزیشن‌ها", "اتصال برقرار نشد.", "🟡")
        return
    resolve_error(state, "fetch_pos")

    items = response.get("items", [])
    open_positions = [p for p in items if p.get("status") == "OPENED"]
    closed_positions = [p for p in items if p.get("status") == "CLOSED"]
    open_keys = {str(p["id"]) for p in open_positions}

    for tag in list(state.get("limit_orders", {}).keys()):
        order = state["limit_orders"][tag]
        matched_pos = next((p for p in open_positions if str(p["id"]) == str(order["order_id"])), None) \
            or next((p for p in open_positions if str(p["id"]) not in state["targets"]
                     and p["symbol"] == f"{order['symbol']}USDT" and p["side"] == order["side"]), None)
        if matched_pos:
            pos_id = str(matched_pos["id"])
            state["targets"][pos_id] = {
                "symbol": order["symbol"], "side": order["side"], "entry": order["entry"],
                "sl": order["sl"], "tp1": order["tp1"], "tp2": order["tp2"], "tp3": order["tp3"], "tp4": order["tp4"], "tp5": order["tp5"],
                "current_stage": 0, "closed": False, "is_auto": True, "is_isolated": False, "limit_stage": 99, "fail_count": 0, "created": time.time()
            }
            del state["limit_orders"][tag]
            notify(f"✅ سفارش لیمیت {tag} فعال شد! پوزیشن {order['symbol']}_{order['side']} وارد بازار شد و مدیریت آن آغاز شد.")
            open_keys.add(pos_id)

    closed_ids = {str(p.get("id")) for p in closed_positions}
    for tag in list(state.get("limit_orders", {}).keys()):
        order = state["limit_orders"][tag]
        if str(order.get("order_id")) in closed_ids:
            notify(f"ℹ️ لیمیت {tag} ({order['symbol']}_{order['side']}) پر شد و قبل از شروع مدیریت بسته شد.")
            del state["limit_orders"][tag]

    for tag in list(state.get("limit_orders", {}).keys()):
        order = state["limit_orders"][tag]
        if now_ts - order["created"] > LIMIT_ORDER_TTL_SEC:
            if cancel_limit_order(state, order["order_id"]):
                notify(f"⏳ زمان سفارش لیمیت {tag} ({order['symbol']}) تمام شد و کنسل شد.")
                del state["limit_orders"][tag]
            else:
                order["cancel_fail"] = order.get("cancel_fail", 0) + 1
                if order["cancel_fail"] >= 3:
                    notify(f"⚠️ کنسل خودکار {tag} سه بار ناموفق بود. از لیست حذف شد؛ در صرافی بررسی کن.")
                    del state["limit_orders"][tag]

    manual_parts = ["📊 <b>داشبورد دستی</b>"]
    auto_parts = ["🤖 <b>داشبورد خودکار</b>"]
    manual_pnl, manual_margin = 0.0, 0.0 
    manual_unmanaged = []

    for pos in open_positions:
        pos_id = str(pos["id"]); symbol = pos["symbol"].replace("USDT", ""); side = pos["side"]; key_base = f"{symbol}_{side}"

        if pos_id not in state["targets"]:
            queue = state["pending"].get(key_base)
            if queue:
                sig = queue.pop(0)
                if not queue: del state["pending"][key_base]
                sig["symbol"], sig["side"] = symbol, side
                state["targets"][pos_id] = sig
                notify(f"🔗 اتصال موفق!\nپوزیشن {key_base} متصل شد.")
            else: manual_unmanaged.append(key_base); continue

        t = state["targets"].get(pos_id)
        if not t or t.get("closed", False): continue
        t["missing"] = 0
        
        is_auto = t.get("is_auto", False)
        is_isolated = t.get("is_isolated", False)
        price = float(pos.get("markPrice") or 0)
        cur_sl = float(pos.get("stopLoss") or 0)
        cur_tp = float(pos.get("takeProfit") or 0)
        pnl = float(pos.get("unrealizedPnL") or 0)
        margin = float(pos.get("initialMargin") or 0)
        size = float(pos.get("size") or 0)
        
        if price <= 0 or margin <= 0 or size <= 0: continue
        if cur_tp <= 0: cur_tp = t.get("tp4", t.get("tp5", 0))
            
        is_long = (side == "LONG")
        entry = float(pos.get("entryPrice") or 0)
        if entry <= 0: entry = price - pnl / size if is_long else price + pnl / size
        if entry > 0: t["entry"] = entry
        entry = t["entry"]
        roi = (pnl / margin) * 100

        # ===== FIX: محاسبه قیمت واقعی از روی PnL =====
        lev = 0.0
        try: lev = float(pos.get("leverage") or 0)
        except (TypeError, ValueError): lev = 0.0
        if lev <= 0 and is_auto: lev = float(AUTO_LEVERAGE)
        if lev > 0 and entry > 0:
            move = pnl / (margin * lev)
            implied = entry * (1 + move) if is_long else entry * (1 - move)
            if implied > 0 and abs(price - implied) / implied > 0.005:
                report_error(state, f"mark_{pos_id}", "مغایرت قیمت مارک",
                             f"{key_base}: صرافی {price} | محاسبه‌شده {implied:.8g}", "🟡")
                price = implied
            else:
                resolve_error(state, f"mark_{pos_id}")
        tol = max(1e-8, price * 1e-5)

        if is_auto:
            closed_now = False
            while True:
                stage, new_sl, reason = next_auto_stage(t, is_long, entry, price)
                if stage <= t["current_stage"]: break
                if stage == 4:
                    if close_position_market(state, pos_id):
                        notify(f"🎯 تارگت نهایی {key_base} زده شد و پوزیشن بسته شد.")
                        state["targets"].pop(pos_id, None)
                        closed_now = True
                    break
                if not reason: break
                payload_sl = max(cur_sl, new_sl) if is_long else (min(cur_sl, new_sl) if cur_sl > 0 else new_sl)
                if abs(cur_sl - payload_sl) > 1e-6:
                    if patch_tpsl(state, pos_id, payload_sl, t.get("tp5", cur_tp)):
                        cur_sl = payload_sl
                        t["current_stage"] = stage
                    else:
                        break 
                else:
                    t["current_stage"] = stage

            if closed_now: continue

            stage_txt, path_txt = AUTO_STAGE_TEXT.get(t["current_stage"], ("در حال مدیریت", ""))
            target = t.get("tp5", cur_tp)
            if t["current_stage"] == 0: target = t.get("tp2", cur_tp)
            elif t["current_stage"] == 1: target = t.get("tp3", cur_tp)
            elif t["current_stage"] == 2: target = t.get("tp4", cur_tp)
            
            bar = get_progress_bar(price, entry, target)
            emoji = "🟢" if roi >= 0 else "🔴"
            card = f"{emoji} <b>{key_base}</b> | 🤖 خودکار\n"
            card += f"💵 سود: <code>{pnl:+.2f} USDT ({roi:+.1f}%)</code>\n"
            card += f"🛑 استاپ: <code>{cur_sl:.4f}</code>\n"
            card += f"📌 وضعیت: {stage_txt}\n"
            if path_txt: card += f"⏳ در مسیر: {path_txt}\n"
            card += f"📊 پیشرفت: <code>[{bar}]</code>\n"
            if len("\n".join(auto_parts)) + len(card) < 3500: auto_parts.append(card)
            continue

        if not is_isolated:
            manual_pnl += pnl; manual_margin += margin
            
        price_10 = entry + ((margin * 0.10) / size if is_long else -(margin * 0.10) / size)
        
        sl0 = t["sl"]
        if cur_sl == 0 or (t["current_stage"] == 0 and (cur_sl < sl0 - tol if is_long else cur_sl > sl0 + tol)):
            restore = sl0 if t["current_stage"] == 0 else (price_10 if t["current_stage"] >= 2 else entry)
            if patch_tpsl(state, pos_id, restore, cur_tp): cur_sl = restore

        stage, new_sl, new_tp, reason = next_stage(t, is_long, entry, price, roi, price_10, cur_sl, cur_tp, t.get("limit_stage", 99), is_isolated)
        if reason:
            final_sl = max(cur_sl, new_sl) if is_long else min(cur_sl, new_sl) if cur_sl > 0 else new_sl
            is_behind = (is_long and final_sl >= price) or (not is_long and final_sl <= price)
            if not is_behind:
                if abs(final_sl - cur_sl) > 1e-6 or abs(new_tp - cur_tp) > 1e-6:
                    if patch_tpsl(state, pos_id, final_sl, new_tp): 
                        cur_sl = final_sl; t["current_stage"] = stage; t["cur_tp"] = new_tp
                else:
                    t["current_stage"] = stage
            else:
                # 👇 اضافه شدن لاگ برای شرط is_behind
                report_error(state, f"behind_{pos_id}", "استاپ پشت قیمت", f"{key_base}: قیمت {price} از استاپ جدید {final_sl} رد شده.", "🟡")

        stage_txt, path_txt = STAGE_TEXT.get(t["current_stage"], ("در حال مدیریت", ""))
        target = t.get("tp4", cur_tp)
        if t["current_stage"] < 5: target = t.get("tp1", cur_tp)
        elif t["current_stage"] < 7: target = t.get("tp2", cur_tp)
        elif t["current_stage"] < 9: target = t.get("tp3", cur_tp)
        
        bar = get_progress_bar(price, entry, target)
        emoji = "🟢" if roi >= 0 else "🔴"
        
        icon = "🔒" if is_isolated else "⚙️"
        card = f"{emoji} <b>{key_base}</b> | {icon}\n"
        card += f"💵 سود: <code>{pnl:+.2f} USDT ({roi:+.1f}%)</code>\n"
        card += f"🛑 استاپ: <code>{cur_sl:.4f}</code>\n"
        card += f"📌 وضعیت: {stage_txt}\n"
        if path_txt: card += f"⏳ در مسیر: {path_txt}\n"
        card += f"📊 پیشرفت: <code>[{bar}]</code>\n"
        if len("\n".join(manual_parts)) + len(card) < 3500: manual_parts.append(card)

    for key in list(state["targets"].keys()):
        if key in open_keys or state["targets"][key].get("closed"): continue
        match = [p for p in closed_positions if str(p.get("id")) == key]
        
        if not match:
            tg = state["targets"][key]
            tg["missing"] = tg.get("missing", 0) + 1
            if tg["missing"] >= MISSING_LIMIT:
                notify(f"🧹 پوزیشن {tg.get('symbol')}_{tg.get('side')} مدت‌هاست در صرافی پیدا نمی‌شود؛ از مدیریت حذف شد.")
                # 👇 FIX 5: پاک کردن خطاهای مربوطه
                resolve_error(state, f"patch_{key}"); resolve_error(state, f"close_{key}")
                resolve_error(state, f"mark_{key}"); resolve_error(state, f"behind_{key}")
                del state["targets"][key]
            continue
            
        last = match[0]
        t = state["targets"][key]
        is_auto = t.get("is_auto", False)
        stage = t.get("current_stage", 0)
        why = last.get("closingReason") or "MANUAL"
        
        status_msg = get_close_status(stage, why, is_auto)
        prefix = "🤖" if is_auto else ("🔒" if t.get("is_isolated") else "🏁")
        sym = last.get("symbol", "").replace("USDT", "")
        sd = last.get("side", "")
        pnl = float(last.get("realizedPnL") or 0)
        emoji = "🟢" if pnl >= 0 else "🔴"
        
        msg = f"{prefix} <b>پوزیشن {sym}_{sd} بسته شد!</b>\n"
        msg += f"وضعیت: {status_msg}\n"
        msg += f"سود/ضرر: <code>{pnl:+.2f} USDT</code> {emoji}"
        
        notify(msg)
        # 👇 FIX 5: پاک کردن خطاهای مربوطه
        resolve_error(state, f"patch_{key}"); resolve_error(state, f"close_{key}")
        resolve_error(state, f"mark_{key}"); resolve_error(state, f"behind_{key}")
        del state["targets"][key]

    if manual_margin > 0:
        non_isolated_positions = [t for t in state["targets"].values() if not t.get("is_auto") and not t.get("is_isolated")]
        target_pct = get_dynamic_tp_percent(len(non_isolated_positions))
        
        if manual_pnl >= (manual_margin * target_pct / 100.0):
            notify(f"🚀 هدف سود کل (دستی) پر شد ({target_pct}%). در حال بستن...")
            for pos in open_positions:
                pid = str(pos["id"])
                p_t = state["targets"].get(pid)
                if not p_t: continue
                if not p_t.get("is_auto") and not p_t.get("is_isolated"):
                    if close_position_market(state, pid):
                        notify(f"🏁 {pos['symbol'].replace('USDT', '')}_{pos['side']} بسته شد (هدف سبد): <code>{float(pos.get('unrealizedPnL') or 0):+.2f} USDT</code>")
                        # 👇 FIX 5: پاک کردن خطاهای مربوطه
                        resolve_error(state, f"patch_{pid}"); resolve_error(state, f"close_{pid}")
                        resolve_error(state, f"mark_{pid}"); resolve_error(state, f"behind_{pid}")
                        state["targets"].pop(pid, None)
            return 

    if not manual_parts[1:]: manual_parts.append("پوزیشن دستی باز نیست.")
    else: manual_parts.insert(1, f"📈 سود کل (سبد): <code>{manual_pnl:+.2f} USDT</code>")
    
    if manual_unmanaged: 
        unmanaged_str = ', '.join(manual_unmanaged[:5])
        if len(manual_unmanaged) > 5: unmanaged_str += f" + {len(manual_unmanaged)-5} more"
        manual_parts.append(f"⚠️ بدون سیگنال: {unmanaged_str}")

    if state.get("limit_orders"):
        for tag, o in state["limit_orders"].items():
            auto_parts.append(f"⏳ لیمیت در انتظار {o['symbol']} | {tag}\nقیمت: {o['entry']}")
        
    if not auto_parts[1:]: auto_parts.append("پوزیشن خودکار باز نیست.")

    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3, minutes=30)
    time_str = now.strftime('%H:%M:%S')
    manual_parts.append(f"\n🕒 <b>آخرین آپدیت:</b> {time_str}")
    auto_parts.append(f"\n🕒 <b>آخرین آپدیت:</b> {time_str}")

    m_text = "\n".join(manual_parts)
    if state.get("dashboard_id"):
        if not edit_telegram(state["dashboard_id"], m_text):
            state["dashboard_id"] = send_telegram(m_text)
    else:
        state["dashboard_id"] = send_telegram(m_text)

    a_text = "\n".join(auto_parts)
    if state.get("auto_dashboard_id"):
        if not edit_telegram(state["auto_dashboard_id"], a_text):
            state["auto_dashboard_id"] = send_telegram(a_text)
    else:
        state["auto_dashboard_id"] = send_telegram(a_text)
        
    render_check_engine(state)

def run():
    print("Checking...", flush=True)
    state = load_state()
    try:
        cleanup_pending(state)
        bal = get_futures_balance(state)
        if bal: daily_loss_exceeded(state, bal[0])
        process_queue(state)
        check_telegram(state)
        manage_positions(state)
    except Exception as e:
        print("RUN ERROR:", repr(e))
        traceback.print_exc()
        notify(f"🚨 خطای اجرای کلی: <code>{html.escape(repr(e))[:300]}</code>")
    finally:
        save_state(state)

if __name__ == "__main__":
    run()
