import os, re, json, requests
from datetime import datetime, timezone
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

TG_SESSION = os.environ.get("TG_SESSION")
MAIN_BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
MAIN_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# 👇 API ها از سکرت‌ها خوانده می‌شوند
API_ID = int(os.environ.get("API_ID", 17349))
API_HASH = os.environ.get("API_HASH", '344583e45741c457fe1862106095a5eb')

CHANNEL_ID = -1004400244481

PROCESSED_FILE = "processed_signals.json"
QUEUE_FILE = "pending_signals.txt"

def load_processed():
    if os.path.exists(PROCESSED_FILE):
        try:
            with open(PROCESSED_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_processed(signals):
    signals = signals[-30:]
    with open(PROCESSED_FILE, "w") as f:
        json.dump(signals, f)

def send_alert(msg):
    url = f"https://api.telegram.org/bot{MAIN_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": MAIN_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    try: requests.post(url, json=payload, timeout=10)
    except: pass

def send_to_main_bot(command):
    send_alert(f"📡 سیگنال جدید:\n<code>{command}</code>")
    try:
        with open(QUEUE_FILE, "a") as f:
            f.write(command + "\n")
        print(f"✅ Written to queue: {command}", flush=True)
    except Exception as e:
        print(f"❌ Error writing to queue: {e}", flush=True)

def run_listener():
    if not TG_SESSION: 
        print("No TG_SESSION found", flush=True)
        return
        
    print("⏳ Connecting to Telegram...", flush=True)
    try:
        with TelegramClient(StringSession(TG_SESSION), API_ID, API_HASH) as client:
            now_utc = datetime.now(timezone.utc)
            
            try:
                messages = client.get_messages(CHANNEL_ID, limit=5)
            except Exception as e:
                print(f"⚠️ Cannot read channel: {e}", flush=True)
                send_alert("⚠️ <b>اخطار مهم:</b>\nدسترسی ربات به چنل سیگنال قطع شده است!")
                return 

            processed = load_processed()
            new_processed = False

            for msg in messages:
                # 👇 استفاده از raw_text به جای text برای جلوگیری از مشکل مارک‌داون
                if not msg.raw_text: continue
                
                if (now_utc - msg.date).total_seconds() > 420: continue 

                text = msg.raw_text
                text_upper = text.upper()

                if "منقضی شده" in text or "تاچ شد" in text or "لغو" in text or "LIMIT" in text_upper:
                    continue

                if "#SIGNAL" in text_upper and "ENTER" in text_upper and "STOP LOSS" in text_upper:
                    try:
                        symbol_match = re.search(r'([A-Z0-9]+/USDT)', text) or re.search(r'([A-Z0-9]+)USDT', text)
                        if not symbol_match: continue
                        symbol = symbol_match.group(1).replace('/', '').replace('USDT', '')
                        
                        m = re.search(r"\b(BUY|LONG|SELL|SHORT)\b", text_upper)
                        if not m: continue
                        side = "LONG" if m.group(1) in ("BUY", "LONG") else "SHORT"
                        
                        entry = re.search(r'Enter\s*:\s*([0-9.]+)', text, re.IGNORECASE).group(1)
                        sl = re.search(r'Stop Loss\s*:\s*([0-9.]+)', text, re.IGNORECASE).group(1)
                        
                        tps = re.findall(r'TP[1-5]\s*:\s*([0-9.]+)', text, re.IGNORECASE)

                        if len(tps) >= 5:
                            cmd = f"/auto {symbol} {side} {entry} {sl} {' '.join(tps[:5])}"
                            fingerprint = f"{symbol}_{side}_{entry}_{sl}"
                            
                            if fingerprint not in processed:
                                print(f"🎯 Found new signal: {fingerprint}", flush=True)
                                send_to_main_bot(cmd)
                                processed.append(fingerprint)
                                new_processed = True
                    except Exception as e:
                        print(f"Parse error: {e}", flush=True)
                        
            if new_processed:
                save_processed(processed)
                print("✅ Saved processed signals.", flush=True)
    except Exception as e:
        print(f"Listener Fatal Error: {e}", flush=True)
        send_alert(f"🚨 خطای جدی در شنونده: {e}")

if __name__ == "__main__":
    run_listener()
