import yfinance as yf
import pandas as pd
import requests
import os
import warnings

warnings.filterwarnings("ignore")

# ================= تنظیمات =================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# دارایی‌هایی که قراره اسکن بشن
ASSETS = {
    "Gold (XAUUSD)": "GC=F",
    "Bitcoin (BTC)": "BTC-USD",
    "Ethereum (ETH)": "ETH-USD"
}

def send_telegram(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print("TG Error:", e)

def check_signals():
    print("Checking signals for 12H timeframe (EMA 20/50)...")
    
    for name, ticker in ASSETS.items():
        print(f"Analyzing {name}...")
        try:
            # گرفتن دیتای 1 ساعته برای 3 ماه گذشته (برای اینکه 50 کندل 12 ساعته کامل بشه)
            df = yf.download(tickers=ticker, period="3mo", interval="1h", progress=False)
            
            if df.empty: 
                continue
                
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
                
            df['Close'] = pd.to_numeric(df['Close'], errors='coerce')
            df = df.dropna()
            
            # تبدیل به 12 ساعته
            df_12h = df.resample('12h').agg({
                'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last'
            }).dropna()
            
            if len(df_12h) < 50:
                print(f"Not enough data for {name}")
                continue
                
            # محاسبه EMA
            df_12h['EMA20'] = df_12h['Close'].ewm(span=20, adjust=False).mean()
            df_12h['EMA50'] = df_12h['Close'].ewm(span=50, adjust=False).mean()
            
            last_row = df_12h.iloc[-1]
            prev_row = df_12h.iloc[-2]
            
            # بررسی کراس
            cross_up = (prev_row['EMA20'] <= prev_row['EMA50']) and (last_row['EMA20'] > last_row['EMA50'])
            cross_down = (prev_row['EMA20'] >= prev_row['EMA50']) and (last_row['EMA20'] < last_row['EMA50'])
            
            last_close = float(last_row['Close'])
            
            if cross_up:
                sl = last_close * 0.97 # استاپ 3%
                msg = f"🟢 <b>LONG SIGNAL: {name}</b>\n\n"
                msg += f"💰 Entry: {last_close:.2f}\n"
                msg += f"🛑 Stop Loss: {sl:.2f} (-3%)\n"
                msg += f"⏳ Timeframe: 12H (EMA 20/50)\n\n"
                msg += "⚠️ لطفا مدیریت سرمایه را رعایت کنید."
                send_telegram(msg)
                print(f"Sent BUY for {name}")
                
            elif cross_down:
                sl = last_close * 1.03 # استاپ 3%
                msg = f"🔴 <b>SHORT SIGNAL: {name}</b>\n\n"
                msg += f"💰 Entry: {last_close:.2f}\n"
                msg += f"🛑 Stop Loss: {sl:.2f} (+3%)\n"
                msg += f"⏳ Timeframe: 12H (EMA 20/50)\n\n"
                msg += "⚠️ لطفا مدیریت سرمایه را رعایت کنید."
                send_telegram(msg)
                print(f"Sent SELL for {name}")
            else:
                print(f"No crossover for {name}")
                
        except Exception as e:
            print(f"Error for {name}: {e}")

if __name__ == "__main__":
    check_signals()
