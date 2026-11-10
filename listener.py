import os, re
from datetime import datetime, timezone
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
import requests

# ===== تنظیمات =====
TG_SESSION = os.environ.get("TG_SESSION")
MAIN_BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
MAIN_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

API_ID = 17349
API_HASH = '344583e45741c457fe1862106095a5eb'
CHANNEL_ID = -1004400244481 # آیدی چنل آدم

def send_to_main_bot(command):
    url = f"https://api.telegram.org/bot{MAIN_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": MAIN_CHAT_ID, "text": command}
    try:
        requests.post(url, json=payload, timeout=10)
        print(f"✅ Sent to bot: {command}")
    except Exception as e:
        print(f"❌ Error sending to bot: {e}")

def run_listener():
    if not TG_SESSION:
        print("❌ No TG_SESSION found in secrets!")
        return

    print("⏳ Connecting to Telegram...")
    with TelegramClient(StringSession(TG_SESSION), API_ID, API_HASH) as client:
        print("✅ Connected.")
        now_utc = datetime.now(timezone.utc)

        # گرفتن ۵ پیام آخر چنل برای بررسی
        messages = client.get_messages(CHANNEL_ID, limit=5)
        
        for msg in messages:
            if not msg.text: continue
            
            # 🛑 فیلتر ۷ دقیقه (اگر بیشتر از ۴۲۰ ثانیه گذشته باشه رد میکنه)
            msg_age = (now_utc - msg.date).total_seconds()
            if msg_age > 420:
                continue

            text = msg.text

            # 🛑 فیلتر آپدیت‌ها و منقضی شده‌ها
            if "منقضی شده" in text or "تاچ شد" in text or "لغو" in text:
                continue

            # 🛑 فقط سیگنال‌های اصلی رو قبول کن
            if "#Signal" in text and "Enter" in text and "Stop Loss" in text:
                try:
                    symbol = re.search(r'([A-Z]+/USDT)', text).group(1).replace('/', '')
                    side = "LONG" if "BUY" in text else "SHORT"
                    
                    entry = re.search(r'Enter\s*:\s*([0-9.]+)', text).group(1)
                    sl = re.search(r'Stop Loss\s*:\s*([0-9.]+)', text).group(1)
                    
                    tps = re.findall(r'TP[1-5]\s*:\s*([0-9.]+)', text)

                    if len(tps) >= 4:
                        # ساخت کامند مخصوص ربات اصلی
                        cmd = f"/auto {symbol} {side} {entry} {sl} {' '.join(tps)}"
                        send_to_main_bot(cmd)
                except Exception as e:
                    print(f"Parse error: {e}")

if __name__ == "__main__":
    run_listener()
