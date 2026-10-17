import time, json, os, requests, ccxt
import numpy as np, pandas as pd
from flask import Flask
import threading

# --- بخش وب‌سرور برای زنده موندن در Render ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

def run_web():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

# --- کدهای ربات تلگرام ---
# اینجا توکن رو از تنظیمات سرور (Environment Variables) می‌خونیم، نه از توی کد!
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    print("ERROR: TELEGRAM_TOKEN is not set in Environment Variables!")
    exit()

BASE = f"https://api.telegram.org/bot{TOKEN}"
ex = ccxt.kucoin()

COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "LINK", "LTC", "AVAX"]
UNIVERSE = COINS + ["DOT", "TRX", "ATOM", "NEAR", "UNI", "ETC", "BCH", "XLM", "FIL", "APT",
                    "ARB", "OP", "INJ", "AAVE", "SUI", "HBAR", "TON", "ICP"]
RISK = 0.01
PARAM_NB = 55          
PARAM_ATR_MULT = 5.0   
PARAM_ADX_MIN = 20     
PARAM_VOL_MULT = 1.2   
CHECK_INTERVAL = 180  

KB = {"keyboard": [[{"text": "🎯 سیگنال جدید"}], [{"text": "📊 وضعیت"}, {"text": "📜 آخرین سیگنال‌ها"}]], "resize_keyboard": True}
TF_KB = {"inline_keyboard": [[{"text": "✅ ۴ ساعته (استراتژی اصلی)", "callback_data": "tf_4h"}], [{"text": "⏱ ۱ ساعته (آزمایشی)", "callback_data": "tf_1h"}]]}

def tg(method, **payload):
    try:
        return requests.post(f"{BASE}/{method}", json=payload, timeout=30).json()
    except: return {}

def send(chat_id, text, markup=KB):
    tg("sendMessage", chat_id=chat_id, text=text, reply_markup=markup)

def fetch(coin, tf, limit=600):
    rows = ex.fetch_ohlcv(coin + "/USDT", tf, limit=limit)
    h = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    h['time'] = pd.to_datetime(h['time'], unit='ms')
    return h.iloc[:-1].reset_index(drop=True)

def calc_ema(series, period): return series.ewm(span=period, adjust=False).mean()
def calc_atr(high, low, close, period=14):
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()
def calc_adx(high, low, close, period=14):
    up = high - high.shift(1); down = low.shift(1) - low
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    plus_di = 100 * (pd.Series(plus_dm).rolling(window=period).mean() / atr)
    minus_di = 100 * (pd.Series(minus_dm).rolling(window=period).mean() / atr)
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100
    return dx.rolling(window=period).mean()

def analyze(coin, tf, side="LONG"):
    try:
        h_tf = fetch(coin, tf); h_1d = fetch(coin, "1d", 300)
        h_1d['ema200_daily'] = calc_ema(h_1d['close'], 200)
        h = pd.merge_asof(h_tf, h_1d[['time', 'ema200_daily']], on='time', direction='backward')
        h['ema200'] = calc_ema(h['close'], 200)
        h['adx'] = calc_adx(h['high'], h['low'], h['close'], 14)
        h['atr'] = calc_atr(h['high'], h['low'], h['close'], 14)
        h['vol_sma'] = h['volume'].rolling(window=20).mean()
        h['hh'] = h['high'].rolling(window=PARAM_NB).max().shift(1)
        h['ll'] = h['low'].rolling(window=PARAM_NB).min().shift(1)
        h = h.dropna().reset_index(drop=True)
        if len(h) < 10: return None
        last = h.iloc[-1]
        adx_ok = last['adx'] > PARAM_ADX_MIN
        vol_ok = last['volume'] > PARAM_VOL_MULT * last['vol_sma']
        
        if side == "LONG":
            breakout = last['close'] > last['hh']
            regime = (last['close'] > last['ema200']) and (last['close'] > last['ema200_daily'])
            if breakout and regime and adx_ok and vol_ok:
                sl = last['close'] - (PARAM_ATR_MULT * last['atr'])
                return {"price": last['close'], "sl": sl, "adx": last['adx']}
        else:
            breakout = last['close'] < last['ll']
            regime = (last['close'] < last['ema200']) and (last['close'] < last['ema200_daily'])
            if breakout and regime and adx_ok and vol_ok:
                sl = last['close'] + (PARAM_ATR_MULT * last['atr'])
                return {"price": last['close'], "sl": sl, "adx": last['adx']}
        return None
    except: return None

def fmt(x): return f"{x: .2f}" if x >= 1 else f"{x:.5g}"

def signal_text(coin, side, r, tf, extra=0):
    p, sl, adx = r["price"], r["sl"], r.get("adx", 0)
    R = abs(p - sl); d = 1 if side == "LONG" else -1
    tp = [p + d * R * k for k in (1, 2, 3)]
    size = min(100, RISK / (R / p) * 100) if R > 0 else 1
    return (f"🎯 {coin}/USDT - {side} SIGNAL ({tf})\n🔥 استراتژی: Sniper V2\n📊 قدرت روند (ADX): {adx:.1f}\n\n"
            f"🎯 Entry: {fmt(p)}\n🎯 TP1: {fmt(tp[0])}\n🎯 TP2: {fmt(tp[1])}\n🎯 TP3: {fmt(tp[2])}\n🛑 SL (5x ATR): {fmt(sl)}\n\n"
            f"💰 Risk: {RISK*100:.0f}% of portfolio = {size:.0f}% margin\n")

last_seen, in_pos = {}, {}

def auto_check(chat_id):
    for coin in UNIVERSE:
        try:
            h = fetch(coin, "4h"); ts = h["time"].iloc[-1]
            if last_seen.get(coin) == ts: continue
            first = coin not in last_seen
            last_seen[coin] = ts
            for side in ["LONG", "SHORT"]:
                r = analyze(coin, "4h", side)
                now = r is not None
                price = h["close"].iloc[-1]
                state_key = f"{coin}_{side}"
                prev = in_pos.get(state_key, False)
                in_pos[state_key] = now
                if first: continue
                if now and not prev:
                    send(chat_id, f"🚨 سیگنال جدید {side}\n\n" + signal_text(coin, side, r, "4h"))
                elif prev and not now:
                    send(chat_id, f"🔴 خروج {coin} از {side}: قیمت {fmt(price)}")
        except: pass
        time.sleep(0.2)

def handle(u, chat_id_global):
    if "callback_query" in u:
        cq = u["callback_query"]; tg("answerCallbackQuery", callback_query_id=cq["id"])
        chat_id = cq["message"]["chat"]["id"]
        send(chat_id, "دکمه‌ها فعلا در سرور غیرفعال هستند تا منابع سرور هدر نره.")
        return
    m = u.get("message")
    if not m or m["chat"]["id"] != chat_id_global: return
    text = m.get("text", "")
    if "سیگنال جدید" in text or text.startswith("/start"):
        send(chat_id_global, "✅ ربات Sniper V2 روی سرور روشن است.\nاسکن خودکار هر ۳ دقیقه انجام می‌شود.", KB)

def find_chat():
    while True:
        r = requests.get(BASE + "/getUpdates", timeout=20).json()
        if r.get("ok"):
            res = r.get("result", [])
            for u in reversed(res):
                if "message" in u:
                    return u["message"]["chat"]["id"], res[-1]["update_id"] + 1
        time.sleep(5)

def bot_loop():
    print("Starting Bot Loop...")
    time.sleep(5) 
    CHAT_ID, offset = find_chat()
    send(CHAT_ID, "✅ ربات Sniper V2 روی سرور ابری روشن شد و ۲۴ ساعته کار می‌کند.")
    t_check = time.time()
    while True:
        try:
            r = requests.get(BASE + "/getUpdates", params={"offset": offset, "timeout": 5}, timeout=10).json()
            if r.get("ok"):
                for u in r.get("result", []):
                    offset = max(offset, u["update_id"] + 1)
                    handle(u, CHAT_ID)
        except: pass
        
        if time.time() - t_check >= CHECK_INTERVAL:
            try: auto_check(CHAT_ID)
            except: pass
            t_check = time.time()
        time.sleep(1)

if __name__ == "__main__":
    threading.Thread(target=run_web).start()
    bot_loop()
