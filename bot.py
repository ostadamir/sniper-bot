import time, os, requests, ccxt, json
import numpy as np, pandas as pd
import datetime

# ================= تنظیمات =================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

STATE_FILE = "paper_state.json"
UNIVERSE = ["ETH", "BNB", "NEAR", "AVAX", "LINK", "ICP", "TRX", "DOGE", "BTC", "UNI"]
ex = ccxt.kucoin()

def send_telegram(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except: pass

# ================= توابع مدیریت State =================
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"balance": 300.0, "positions": []}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)

# ================= محاسبات استراتژی =================
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

def fetch(coin, tf, limit=600):
    rows = ex.fetch_ohlcv(coin + "/USDT", tf, limit=limit)
    h = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    h['time'] = pd.to_datetime(h['time'], unit='ms')
    return h

def analyze_daily(coin):
    try:
        h = fetch(coin, "1d", 500) 
        
        h['ema200'] = calc_ema(h['close'], 200)
        h['adx'] = calc_adx(h['high'], h['low'], h['close'], 14)
        h['atr'] = calc_atr(h['high'], h['low'], h['close'], 14)
        h['vol_sma'] = h['volume'].rolling(window=20).mean()
        h['hh55'] = h['high'].rolling(window=55).max().shift(1)
        h['ll55'] = h['low'].rolling(window=55).min().shift(1)
        
        h_calc = h.iloc[:-1].dropna().reset_index(drop=True) 
        if len(h_calc) < 10: return None
        last = h_calc.iloc[-1]
        
        vol_ratio = last['volume'] / last['vol_sma']
        
        if (last['close'] > last['hh55'] and last['close'] > last['ema200'] and 
            last['adx'] > 25 and vol_ratio > 1.5):
            
            swing_range = last['hh55'] - last['ll55']
            if swing_range <= 0: return None
            
            sl = last['hh55'] - (swing_range * 0.618)
            tp = last['hh55'] + (swing_range * 1.618)
            
            if last['close'] <= sl: return None
            
            # تعیین کیفیت سیگنال
            if last['adx'] > 35 and vol_ratio > 2.0:
                quality = "🌟 عالی"
                margin_pct = 0.03 # 3%
            elif last['adx'] > 30 and vol_ratio > 1.8:
                quality = "🔥 خیلی خوب"
                margin_pct = 0.02 # 2%
            else:
                quality = "✅ خوب"
                margin_pct = 0.01 # 1%
                
            return {"price": last['close'], "sl": sl, "tp": tp, "adx": last['adx'], "atr": last['atr'], "quality": quality, "margin_pct": margin_pct}
        return None
    except: return None

# ================= اجرای اصلی =================
def run():
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] شروع اسکن Paper Trading (300$)...")
    state = load_state()
    
    # محاسبه کل سرمایه (Equity = Free Balance + Locked Margin)
    total_equity = state['balance'] + sum(p['margin'] for p in state['positions'] if not p.get('closed'))
    
    # 1. مدیریت پوزیشن‌های فیک باز
    for pos in state['positions'][:]:
        if pos.get('closed'): continue
        
        h = fetch(pos['symbol'], "1d", 10)
        if h.empty: continue
        current_candle = h.iloc[-2] # کندل بسته شده امروز
        current_price = current_candle['close']
        
        if current_price > pos['highest']: pos['highest'] = current_price
        
        # ریسک فری (1.5 R)
        if not pos['breakeven'] and current_price >= (pos['entry'] + (pos['initial_risk'] * 1.5)):
            pos['sl'] = pos['entry']
            pos['breakeven'] = True
            
        # تریلینگ استاپ ۵ ATR
        trailing_sl = pos['highest'] - (5.0 * pos['atr'])
        if trailing_sl > pos['sl']: pos['sl'] = trailing_sl
            
        exit_price = 0
        reason = ""
        
        if current_candle['low'] <= pos['sl']:
            exit_price = pos['sl']
            reason = "Trailing SL" if pos['sl'] > pos['sl_initial'] else "Hard SL"
            if pos['breakeven'] and pos['sl'] == pos['entry']: reason = "Breakeven"
        elif current_candle['high'] >= pos['tp']:
            exit_price = pos['tp']
            reason = "TP (Fib 1.618)"
            
        if exit_price > 0:
            qty = (pos['margin'] * 10) / pos['entry']
            pnl = (exit_price - pos['entry']) * qty
            state['balance'] += pos['margin'] + pnl
            pos['closed'] = True
            
            send_telegram(f"🔴 <b>خروج از پوزیشن (فیک)</b>\n"
                          f"🪙 {pos['symbol']} | دلیل: {reason}\n"
                          f"💰 سود/ضرر: ${pnl:.2f}\n"
                          f"💵 موجودی جدید: ${state['balance']:.2f}")

    # پاک کردن پوزیشن‌های بسته شده
    state['positions'] = [p for p in state['positions'] if not p.get('closed')]
    
    # 2. باز کردن پوزیشن‌های جدید
    open_symbols = [p['symbol'] for p in state['positions']]
    
    if len(open_symbols) < 3:
        for coin in UNIVERSE:
            symbol = f"{coin}/USDT"
            if symbol in open_symbols: continue
                
            r = analyze_daily(coin)
            if r:
                margin_amount = total_equity * r['margin_pct']
                margin_amount = max(1.0, round(margin_amount, 2)) # حداقل 1 دلار
                
                if state['balance'] < margin_amount:
                    continue
                    
                state['positions'].append({
                    "symbol": symbol, "entry": r['price'], "sl": r['sl'], "sl_initial": r['sl'],
                    "tp": r['tp'], "atr": r['atr'], "margin": margin_amount, "highest": r['price'],
                    "initial_risk": r['price'] - r['sl'], "quality": r['quality'],
                    "breakeven": False, "closed": False
                })
                state['balance'] -= margin_amount
                
                send_telegram(f"🟢 <b>پوزیشن جدید باز شد (فیک)</b>\n"
                              f"🪙 {symbol} | مارجین: ${margin_amount}\n"
                              f"💎 کیفیت: {r['quality']}\n"
                              f"🎯 TP: {r['tp']:.4f}\n"
                              f"🛑 SL: {r['sl']:.4f}\n"
                              f"💵 موجودی آزاد: ${state['balance']:.2f}")
                break 

    save_state(state)
    print("پایان اسکن. فایل state ذخیره شد.")

if __name__ == "__main__":
    run()
