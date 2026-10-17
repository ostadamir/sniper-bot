import time, os, requests, ccxt, hmac, hashlib, json
import numpy as np, pandas as pd

# ================= تنظیمات =================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

TRUETRADE_KEY = "KnEDfEai0PDBh36LbXWWptvYru2tPTYbSEMvBcbH"
TRUETRADE_SECRET = "4605eb72c3b418ce5ca129aa75a4ec6e7aa21bcb76a537068b7195288aa2dcb8"
TRUETRADE_URL = "https://apiv2.thetruetrade.io"

UNIVERSE = ["ETH", "BNB", "NEAR", "AVAX", "LINK", "ICP", "TRX", "DOGE", "BTC", "UNI"]
ex = ccxt.kucoin()

def send_telegram(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except: pass

# ================= توابع ترو ترید =================
def tt_sign(secret: str, timestamp: str, method: str, uri: str) -> str:
    payload = f"{timestamp}{method.upper()}{uri}"
    return hmac.new(secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()

def tt_request(method, uri, payload=None):
    timestamp = str(int(time.time() * 1000))
    signature = tt_sign(TRUETRADE_SECRET, timestamp, method, uri)
    headers = {"X-API-Key": TRUETRADE_KEY, "X-Timestamp": timestamp, "X-Signature": signature, "Content-Type": "application/json"}
    try:
        if method == "GET":
            res = requests.get(f"{TRUETRADE_URL}{uri}", headers=headers, timeout=10)
        elif method == "POST":
            res = requests.post(f"{TRUETRADE_URL}{uri}", headers=headers, json=payload, timeout=15)
        elif method == "PATCH":
            res = requests.patch(f"{TRUETRADE_URL}{uri}", headers=headers, json=payload, timeout=15)
        return res.status_code, res.json() if res.text else {}
    except Exception as e:
        return 500, str(e)

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
    return h.iloc[:-1].reset_index(drop=True)

def analyze_4h(coin):
    try:
        h_tf = fetch(coin, "4h")
        h_1d = fetch(coin, "1d", 300)
        h_1d['ema200_daily'] = calc_ema(h_1d['close'], 200)
        h = pd.merge_asof(h_tf, h_1d[['time', 'ema200_daily']], on='time', direction='backward')
        h['ema200'] = calc_ema(h['close'], 200)
        h['adx'] = calc_adx(h['high'], h['low'], h['close'], 14)
        h['atr'] = calc_atr(h['high'], h['low'], h['close'], 14)
        h['vol_sma'] = h['volume'].rolling(window=20).mean()
        h['hh'] = h['high'].rolling(window=55).max().shift(1)
        h = h.dropna().reset_index(drop=True)
        
        if len(h) < 10: return None
        last = h.iloc[-1]
        
        if (last['close'] > last['hh'] and last['close'] > last['ema200'] and 
            last['close'] > last['ema200_daily'] and last['adx'] > 20 and last['volume'] > 1.2 * last['vol_sma']):
            
            sl = last['close'] - (5.0 * last['atr'])
            tp = last['close'] + ((last['close'] - sl) * 2.0)
            
            is_exceptional = (last['adx'] > 35) and (last['volume'] > 1.5 * last['vol_sma'])
            
            return {"price": last['close'], "sl": sl, "tp": tp, "adx": last['adx'], "atr": last['atr'], "is_exceptional": is_exceptional}
        return None
    except: return None

# ================= اجرای اصلی =================
def run():
    print("شروع اسکن...")
    status, response = tt_request("GET", "/futures/positions")
    
    open_positions = []
    if status == 200 and isinstance(response, dict):
        items = response.get('items', [])
        open_positions = [p for p in items if p.get('status') == 'OPENED']
    
    bot_positions = []
    open_symbols = []
    
    for pos in open_positions:
        try:
            side = pos.get('side')
            margin = float(pos.get('initialMargin', 0))
            lev = float(pos.get('leverage', 1))
            coin = pos['symbol'].replace("USDT", "")
            
            # 🛠 فیلترهای ضدگلوله برای تشخیص پوزیشن‌های ربات:
            # 1. LONG باشد
            # 2. مارجین 1، 2 یا 3 دلار باشد
            # 3. اهرم دقیقا 10 باشد (ربات همیشه 10 میذاره)
            # 4. ارز حتما جزو 10 ارز VIP باشد (WLD جزو این لیست نیست)
            is_bot = (side == 'LONG' and round(margin, 1) in [1.0, 2.0, 3.0] 
                      and lev == 10 and coin in UNIVERSE)
            
            if is_bot:
                bot_positions.append(pos)
                open_symbols.append(pos['symbol'])
        except:
            continue

    print(f"تعداد پوزیشن‌های فعال ربات: {len(bot_positions)}")
    
    # 1. مدیریت پوزیشن‌های ربات (ریسک‌فری و تریلینگ)
    for pos in bot_positions:
        symbol = pos['symbol']
        coin = symbol.replace("USDT", "")
        entry = float(pos.get('entryPrice', 0))
        current_price = float(pos.get('markPrice', 0))
        tp = float(pos.get('takeProfit', 0))
        current_sl = float(pos.get('stopLoss', 0))
        
        r = analyze_4h(coin)
        if not r: continue

        new_sl = current_sl
        is_risk_free = False

        if tp > entry and current_sl < entry:
            halfway = entry + ((tp - entry) / 2)
            if current_price >= halfway:
                new_sl = entry
                is_risk_free = True
                
        trailing_sl = current_price - (5.0 * r['atr'])
        if trailing_sl > new_sl:
            new_sl = trailing_sl
                
        if new_sl != current_sl and new_sl > 0:
            p_status, p_res = tt_request("PATCH", f"/futures/positions/{pos['id']}/tpsl", {"stopLoss": str(round(new_sl, 4))})
            if p_status in [200, 201]:
                if is_risk_free and new_sl == entry:
                    send_telegram(f"🛡 <b>پوزیشن ریسک‌فری شد!</b>\nارز: #{coin}\nحد ضرر به نقطه ورود ({entry:.4f}) منتقل شد.")
                else:
                    send_telegram(f"🔄 <b>آپدیت حد ضرر (Trailing)</b>\nارز: #{coin}\nحد ضرر جدید: {new_sl:.4f}")

    # 2. باز کردن پوزیشن‌های جدید با محدودیت 3 عدد
    found_count = 0
    
    for coin in UNIVERSE:
        symbol = f"{coin}USDT"
        if symbol in open_symbols: continue
            
        r = analyze_4h(coin)
        if r:
            # چک کردن محدودیت 3 پوزیشن
            if len(bot_positions) >= 3:
                if not r['is_exceptional']:
                    print(f"سیگنال {coin} رد شد. چون ربات ۳ پوزیشن باز دارد و این سیگنال فوق‌العاده نیست.")
                    continue
                else:
                    print(f"⚠️ استثنا! سیگنال {coin} با وجود ۳ پوزیشن باز، باز می‌شود چون فوق‌العاده قوی است.")

            if r['adx'] > 35: margin = 3.0
            elif r['adx'] > 25: margin = 2.0
            else: margin = 1.0
            
            payload = {
                "symbol": symbol, "side": "LONG", "tradeType": "MARKET", "leverage": 10,
                "cost": str(margin), "takeProfit": str(round(r['tp'], 4)), "stopLoss": str(round(r['sl'], 4)), "walletType": "debit"
            }
            o_status, o_res = tt_request("POST", "/futures/positions", payload)
            
            if o_status in [200, 201]:
                send_telegram(f"🚀 <b>پوزیشن جدید باز شد</b> 🚀\n🪙 {coin} | مارجین: {margin}$\n🎯 TP2: {r['tp']:.4f}\n🛑 SL: {r['sl']:.4f}\n📊 ADX: {r['adx']:.1f}")
                found_count += 1
                bot_positions.append({"symbol": symbol})
                open_symbols.append(symbol)
            else:
                print(f"خطا در {coin}: {o_res}")
        time.sleep(0.5)
        
    if found_count == 0:
        print("اسکن تمام شد. سیگنال جدیدی پیدا نشد یا محدودیت‌ها اجازه ندادند.")

if __name__ == "__main__":
    run()
