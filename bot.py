import time, os, requests, ccxt
import numpy as np, pandas as pd

# خواندن توکن و چت‌آیدی از تنظیمات گیت‌هاب
TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

if not TOKEN or not CHAT_ID:
    print("ERROR: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID is not set!")
    exit()

BASE = f"https://api.telegram.org/bot{TOKEN}"
ex = ccxt.kucoin()

UNIVERSE = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "LINK", "LTC", "AVAX",
            "DOT", "TRX", "ATOM", "NEAR", "UNI", "ETC", "BCH", "XLM", "FIL", "APT",
            "ARB", "OP", "INJ", "AAVE", "SUI", "HBAR", "TON", "ICP"]
RISK = 0.01
PARAM_NB = 55
PARAM_ATR_MULT = 5.0
PARAM_ADX_MIN = 20
PARAM_VOL_MULT = 1.2

def send(chat_id, text):
    try:
        requests.post(f"{BASE}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=10)
    except: pass

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
        h_tf = fetch(coin, tf)
        h_1d = fetch(coin, "1d", 300)
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

def signal_text(coin, side, r, tf):
    p, sl, adx = r["price"], r["sl"], r.get("adx", 0)
    R = abs(p - sl); d = 1 if side == "LONG" else -1
    tp = [p + d * R * k for k in (1, 2, 3)]
    size = min(100, RISK / (R / p) * 100) if R > 0 else 1
    return (f"🎯 {coin}/USDT - {side} SIGNAL ({tf})\n"
            f"🔥 استراتژی: Sniper V2\n"
            f"📊 قدرت روند (ADX): {adx:.1f}\n\n"
            f"🎯 Entry: {fmt(p)}\n"
            f"🎯 TP1: {fmt(tp[0])}\n"
            f"🎯 TP2: {fmt(tp[1])}\n"
            f"🎯 TP3: {fmt(tp[2])}\n"
            f"🛑 SL (5x ATR): {fmt(sl)}\n\n"
            f"💰 Risk: {RISK*100:.0f}% of portfolio = {size:.0f}% margin\n")

if __name__ == "__main__":
    print("ربات شروع به کار کرد...")
    send(CHAT_ID, "✅ ربات روی GitHub Actions اجرا شد و در حال اسکن بازار است...")
    
    found = 0
    for coin in UNIVERSE:
        r = analyze(coin, "4h", "LONG")
        if r:
            send(CHAT_ID, signal_text(coin, "LONG", r, "4h"))
            found += 1
        time.sleep(0.2)
        
    if found == 0:
        send(CHAT_ID, "ℹ️ اسکن تمام شد. هیچ سیگنال جدیدی پیدا نشد.")
    else:
        send(CHAT_ID, f"✅ اسکن تمام شد. {found} سیگنال پیدا شد.")
