import os, json, time
import datetime as dt
import numpy as np
import pandas as pd
import requests
import ccxt

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
STATE_FILE = "paper_state.json"
VERSION = 2
UNIVERSE = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "LINK",
            "LTC", "AVAX", "DOT", "TRX", "ATOM", "NEAR", "UNI", "ETC",
            "BCH", "XLM", "FIL", "APT", "ARB", "OP", "INJ", "AAVE",
            "SUI", "HBAR", "ICP"]
ex = ccxt.kucoin()

START_BALANCE = 300.0
RISK = 0.01
MAX_POS = 3
ATR_MULT = 3.0
FEE = 0.001
SLIP = 0.0005
MAX_CHASE = 0.03
MIN_NOTIONAL = 10.0
DAY = 86400000
HEARTBEAT_HOURS = 4  # 👈 گزارش هر ۴ ساعت
COLS = ["ms", "open", "high", "low", "close", "volume"]


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def send(text):
    if not TOKEN or not CHAT_ID:
        print(text)
        return
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text,
                                     "parse_mode": "HTML"}, timeout=15)
        if not r.ok:
            print("telegram error", r.status_code)
    except Exception as e:
        print("telegram error", type(e).__name__)


def fmt(x):
    return f"{x:,.2f}" if x >= 1 else f"{x:.5g}"


def new_state():
    return {"version": VERSION, "balance": START_BALANCE, "positions": [],
            "history": [], "signals": {}, "skipped": {},
            "last_heartbeat": "", "last_error_notice": "",
            "started": False}


def load_state():
    s = None
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                s = json.load(f)
        except Exception:
            s = None
    if isinstance(s, dict) and s.get("version") == VERSION:
        for k, v in new_state().items():
            s.setdefault(k, v)
        return s
    return new_state()


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


_cache = {}


def get_daily(coin):
    if coin in _cache:
        return _cache[coin]
    rows = ex.fetch_ohlcv(coin + "/USDT", "1d", limit=500)
    d = pd.DataFrame(rows, columns=COLS)
    d = d[d["ms"] + DAY <= ex.milliseconds()].reset_index(drop=True)
    if len(d) < 260:
        raise ValueError("not enough candles")
    h, l, c = d["high"], d["low"], d["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(),
                    (l - c.shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.rolling(14).mean()
    d["sma200"] = c.rolling(200).mean()
    d["hh"] = h.rolling(55).max().shift(1)
    _cache[coin] = d
    return d


def check_signal(d):
    last = d.iloc[-1]
    if last[["atr", "sma200", "hh"]].isna().any():
        return None
    close = float(last["close"])
    if close > last["hh"] and close > last["sma200"]:
        atr = float(last["atr"])
        return {"candle_ms": int(last["ms"]), "close": close, "atr": atr,
                "strength": (close - float(last["hh"])) / atr}
    return None


def live_price(sym, fallback):
    try:
        return float(ex.fetch_ticker(sym)["last"])
    except Exception:
        return fallback


def do_exit(state, pos, reason):
    d = get_daily(pos["symbol"].split("/")[0])
    px = live_price(pos["symbol"], float(d["close"].iloc[-1]))
    px = px * (1 - SLIP)
    value = px * pos["qty"]
    fee = value * FEE
    state["balance"] += value - fee
    net = value - fee - pos["invested"] - pos["fee_entry"]
    pct = net / pos["invested"] * 100
    state["positions"].remove(pos)
    state["history"].append({"symbol": pos["symbol"], "entry": pos["entry"],
                             "exit": px, "reason": reason,
                             "net": round(net, 4),
                             "time": now_utc().strftime("%Y-%m-%d %H:%M")})
    icon = "🟢" if net > 0 else "🔴"
    send(f"{icon} <b>PAPER SELL</b> · {pos['symbol']}\n"
         f"<pre>Reason   {reason}\n"
         f"Entry    {fmt(pos['entry'])}\n"
         f"Exit     {fmt(px)}\n"
         f"Result   {net:+.2f}$ ({pct:+.1f}%)\n"
         f"Balance  {state['balance']:.2f}$</pre>")


def manage_positions(state, errors):
    for pos in list(state["positions"]):
        coin = pos["symbol"].split("/")[0]
        try:
            d = get_daily(coin)
        except Exception as e:
            errors.append(f"{coin}: {type(e).__name__} {str(e)[:100]}")
            continue
        new = d[d["ms"] > pos["last_ms"]]
        for _, row in new.iterrows():
            pos["last_ms"] = int(row["ms"])
            pos["peak"] = max(pos["peak"], float(row["close"]))
            if pd.isna(row["atr"]):
                continue
            stop = pos["peak"] - ATR_MULT * float(row["atr"])
            pos["stop"] = stop
            if float(row["close"]) < stop:
                do_exit(state, pos, "Trailing stop (3 ATR)")
                break


def try_open(state, errors):
    """برمی‌گردونه: (تعداد سیگنال پیدا شده, تعداد پوزیشن باز شده)"""
    open_syms = {p["symbol"] for p in state["positions"]}
    cands = []
    for coin in UNIVERSE:
        time.sleep(0.3)
        sym = coin + "/USDT"
        if sym in open_syms:
            continue
        try:
            d = get_daily(coin)
        except Exception as e:
            errors.append(f"{coin}: {type(e).__name__} {str(e)[:100]}")
            continue
        r = check_signal(d)
        if r is None or state["signals"].get(sym) == r["candle_ms"]:
            continue
        r["symbol"] = sym
        cands.append(r)
    cands.sort(key=lambda r: r["strength"], reverse=True)

    signals_found = len(cands)
    opened_count = 0
    skips = []
    for r in cands:
        sym = r["symbol"]
        invested = sum(p["invested"] for p in state["positions"])
        equity = state["balance"] + invested
        skip = None
        live = live_price(sym, r["close"])
        if len(state["positions"]) >= MAX_POS:
            skip = "max positions reached"
        elif live > r["close"] * (1 + MAX_CHASE):
            skip = "price already ran more than 3%"
        if skip is None:
            entry = live * (1 + SLIP)
            stop_dist = ATR_MULT * r["atr"] / entry
            notional = min(equity * RISK / stop_dist, equity / MAX_POS)
            notional = min(notional, state["balance"] / (1 + FEE))
            if notional < MIN_NOTIONAL:
                skip = "position size too small or no free balance"
        if skip:
            last = state["skipped"].get(sym)
            if last is None or r["candle_ms"] - last >= 7 * DAY:
                state["skipped"][sym] = r["candle_ms"]
                skips.append(f"{sym}: {skip}")
            continue
        fee_entry = notional * FEE
        stop0 = r["close"] - ATR_MULT * r["atr"]
        state["positions"].append({
            "symbol": sym, "entry": entry, "qty": notional / entry,
            "invested": notional, "fee_entry": fee_entry,
            "peak": r["close"], "last_ms": r["candle_ms"], "stop": stop0,
            "time": now_utc().strftime("%Y-%m-%d %H:%M")})
        state["balance"] -= notional + fee_entry
        state["signals"][sym] = r["candle_ms"]
        opened_count += 1
        risk_usd = notional * stop_dist
        send(f"🟢 <b>PAPER BUY</b> · {sym}\n"
             f"<pre>Entry    {fmt(entry)}\n"
             f"Stop     {fmt(stop0)} (trailing)\n"
             f"Size     {notional:.2f}$\n"
             f"Risk     {risk_usd:.2f}$ ({risk_usd / equity * 100:.1f}%)\n"
             f"Balance  {state['balance']:.2f}$</pre>")
    if skips:
        send("⏭ <b>Signals skipped</b>\n<pre>" + "\n".join(skips) + "</pre>")
    return signals_found, opened_count


def stats(hist):
    wins = [x for x in hist if x["net"] > 0]
    gain = sum(x["net"] for x in wins)
    loss = -sum(x["net"] for x in hist if x["net"] <= 0)
    pf = f"{gain / loss:.2f}" if loss > 0 else "n/a"
    wr = f"{len(wins) / len(hist) * 100:.0f}%" if hist else "n/a"
    return wr, pf


def heartbeat(state, signals_found_this_run):
    """گزارش هر ۴ ساعت یکبار"""
    now = now_utc()
    last = state.get("last_heartbeat", "")
    if last:
        try:
            last_dt = dt.datetime.fromisoformat(last)
            elapsed = (now - last_dt).total_seconds()
            if elapsed < HEARTBEAT_HOURS * 3600 - 120:
                return
        except Exception:
            pass

    state["last_heartbeat"] = now.isoformat()

    unreal, lines = 0.0, []
    for p in state["positions"]:
        live = live_price(p["symbol"], p["entry"])
        u = live * p["qty"] - p["invested"]
        unreal += u
        lines.append(f"{p['symbol']:<10}{u:+8.2f}$  stop {fmt(p['stop'])}")
    invested = sum(p["invested"] for p in state["positions"])
    equity = state["balance"] + invested + unreal
    wr, pf = stats(state["history"])

    # 📊 وضعیت اسکن در این دوره
    if signals_found_this_run > 0:
        scan_status = f"✅ {signals_found_this_run} سیگنال جدید پیدا شد"
    else:
        scan_status = "ℹ️ هیچ سیگنال جدیدی در این دوره نبود"

    text = (f"⏰ <b>گزارش ۴ ساعته</b>\n"
            f"<pre>Equity   {equity:.2f}$\n"
            f"Free     {state['balance']:.2f}$\n"
            f"Open     {len(state['positions'])}\n"
            f"Trades   {len(state['history'])}\n"
            f"Winrate  {wr}\n"
            f"ProfitF  {pf}</pre>\n"
            f"<b>اسکن اخیر:</b> {scan_status}")
    if lines:
        text += "\n<pre>" + "\n".join(lines) + "</pre>"
    else:
        text += "\n<i>هیچ پوزیشن بازی در حال حاضر وجود ندارد.</i>"
    send(text)


def startup_message():
    send("✅ <b>Bot started</b> · PAPER TRADING (no real money)\n"
         f"<pre>Balance   {START_BALANCE:.0f}$ (virtual)\n"
         "Entry     55d breakout + above SMA200\n"
         "Exit      3 ATR trailing stop\n"
         f"Risk      {RISK * 100:.0f}% per trade\n"
         f"Max pos   {MAX_POS}\n"
         "Leverage  none (1x)\n"
         f"Coins     {len(UNIVERSE)}\n"
         "Checks    every 30 minutes\n"
         "Reports   every 4 hours</pre>\n"
         "I will message you on every buy, sell, skipped signal, "
         "error, and every 4 hours with a status report.")


def notify_errors(state, errors):
    if not errors:
        return
    print("errors:", errors[:5])
    today = now_utc().strftime("%Y-%m-%d")
    if len(errors) >= 5 and state["last_error_notice"] != today:
        state["last_error_notice"] = today
        send(f"⚠️ <b>{len(errors)} coins failed to load</b>\n"
             f"<pre>{errors[0]}</pre>")


def run():
    print("scan start")
    state = load_state()
    errors = []
    if not state["started"]:
        startup_message()
        state["started"] = True
    try:
        manage_positions(state, errors)
        signals_found, opened_count = try_open(state, errors)
        print(f"signals found: {signals_found}, opened: {opened_count}")
        heartbeat(state, signals_found)
    except Exception as e:
        send(f"⚠️ <b>Unexpected error</b>\n"
             f"<pre>{type(e).__name__}: {str(e)[:150]}</pre>")
    notify_errors(state, errors)
    save_state(state)
    print("done")


if __name__ == "__main__":
    run()
