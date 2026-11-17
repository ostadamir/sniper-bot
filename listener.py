import os, re, json, requests
from datetime import datetime, timezone
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

TG_SESSION = os.environ.get("TG_SESSION")
MAIN_BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
MAIN_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

CHANNEL_ID = -1004400244481
MAX_AGE_SEC = 420

PROCESSED_FILE = "processed_signals.json"
QUEUE_FILE = "pending_signals.txt"
NUM = r'([0-9]+(?:\.[0-9]+)?)'
HASHTAG_REGEX = r'#[A-Z0-9_]+'

CANCEL_KEYWORDS = ["فعال نشد", "لغو", "منقضی", "کنسل", "برداشت اردر", "پاک شد", "تایم تمام"]

def load_processed():
    if os.path.exists(PROCESSED_FILE):
        try:
            with open(PROCESSED_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_processed(signals):
    signals = signals[-50:]
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

def fetch_messages(client):
    try:
        return client.get_messages(CHANNEL_ID, limit=10)
    except ValueError:
        client.get_dialogs()
        return client.get_messages(CHANNEL_ID, limit=10)

def find_tag(text_upper):
    tags = [t for t in re.findall(HASHTAG_REGEX, text_upper) if t != "#SIGNAL"]
    return tags[0] if tags else None

def run_listener():
    if not TG_SESSION: return

    print("⏳ Connecting to Telegram...", flush=True)
    try:
        with TelegramClient(StringSession(TG_SESSION), API_ID, API_HASH) as client:
            now_utc = datetime.now(timezone.utc)
            processed = load_processed()

            try:
                messages = fetch_messages(client)
            except Exception as e:
                print(f"⚠️ Cannot read channel: {e}", flush=True)
                key = f"ACCESS_ALERT_{now_utc:%Y%m%d%H}"
                if key not in processed:
                    send_alert("⚠️ <b>اخطار مهم:</b>\nدسترسی ربات به چنل سیگنال قطع شده است!")
                    processed.append(key)
                    save_processed(processed)
                return

            new_processed = False

            # 👇 معکوس کردن ترتیب پیام‌ها (قدیمی‌ترین اول بررسی میشه)
            for msg in reversed(messages):
                if not msg.raw_text: continue
                if (now_utc - msg.date).total_seconds() > MAX_AGE_SEC: continue

                text = msg.raw_text
                text_upper = text.upper()
                current_tag = find_tag(text_upper)
                is_signal = "ENTER" in text_upper and "STOP LOSS" in text_upper

                # ۱. بررسی پیام کنسل (فقط اگر خود پیام، سیگنال نباشد)
                if current_tag and not is_signal and any(k in text for k in CANCEL_KEYWORDS):
                    if f"cancel_{current_tag}" not in processed:
                        print(f"🚫 Detected cancel for {current_tag}", flush=True)
                        send_to_main_bot(f"/cancel_limit {current_tag}")
                        processed.append(f"cancel_{current_tag}")
                        new_processed = True
                    continue

                if "منقضی شده" in text or "تاچ شد" in text or "لغو" in text:
                    continue

                # ۲. بررسی سیگنال جدید
                if "#SIGNAL" in text_upper and is_signal:
                    try:
                        symbol_match = re.search(r'([A-Z0-9]+/USDT)', text) or re.search(r'([A-Z0-9]+)USDT', text)
                        if not symbol_match: continue
                        symbol = symbol_match.group(1).replace('/', '').replace('USDT', '')

                        m = re.search(r"\b(BUY|LONG|SELL|SHORT)\b", text_upper)
                        if not m: continue
                        side = "LONG" if m.group(1) in ("BUY", "LONG") else "SHORT"

                        e_m = re.search(r'Enter(?:\s*Limit)?\s*:\s*' + NUM, text, re.IGNORECASE)
                        s_m = re.search(r'Stop Loss\s*:\s*' + NUM, text, re.IGNORECASE)
                        if not e_m or not s_m: continue
                        entry, sl = e_m.group(1), s_m.group(1)

                        if (side == "LONG" and float(sl) >= float(entry)) or (side == "SHORT" and float(sl) <= float(entry)):
                            continue

                        # 👇 استخراج هر تعداد تارگت
                        tps_raw = re.findall(r'TP[1-9]\s*:\s*' + NUM, text, re.IGNORECASE)
                        tps = [float(x) for x in tps_raw]

                        if not tps: continue
                        n_raw = len(tps) # تعداد واقعی

                        # 👇 نرمال‌سازی به ۵ تارگت
                        while len(tps) < 5:
                            if len(tps) >= 2:
                                step = abs(tps[-1] - tps[-2])
                            else:
                                step = abs(tps[0] - float(entry))
                                if step == 0: step = float(entry) * 0.01
                                
                            if side == "LONG":
                                tps.append(tps[-1] + step)
                            else:
                                tps.append(tps[-1] - step)
                                
                        tps = tps[:5]
                        # 👇 جلوگیری از تارگت صفر یا منفی
                        if any(x <= 0 for x in tps):
                            print(f"⚠️ Non-positive TP, skipped: {symbol}", flush=True)
                            continue

                        tps_str = [str(x) for x in tps]
                        fingerprint = f"{symbol}_{side}_{entry}_{sl}"
                        if fingerprint in processed: continue

                        is_limit = "LIMIT" in text_upper
                        if is_limit and not current_tag:
                            send_alert(f"⚠️ سیگنال لیمیت {symbol} هشتگ نداشت و رد شد.")
                            processed.append(fingerprint); new_processed = True
                            continue

                        if n_raw < 5:
                            send_alert(f"ℹ️ {symbol}: چنل فقط {n_raw} تارگت داد؛ بقیه به‌صورت خودکار ساخته شد.")

                        cmd_type = "/auto_limit" if is_limit else "/auto"
                        tag_suffix = f" {current_tag}" if is_limit else ""
                        cmd = f"{cmd_type} {symbol} {side} {entry} {sl} {' '.join(tps_str)}{tag_suffix}"

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
