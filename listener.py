import os, re, json, requests
from datetime import datetime, timezone
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
import google.generativeai as genai

TG_SESSION = os.environ.get("TG_SESSION")
MAIN_BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
MAIN_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

CHANNEL_ID = -1004400244481
MAX_AGE_SEC = 7200  # 👈 فیکس شد: ۲ ساعت (تا اگر گیت‌هاب تاخیر داشت سیگنال از دست نره)

PROCESSED_FILE = "processed_signals.json"
QUEUE_FILE = "pending_signals.txt"
LAST_ID_FILE = "last_msg_id.txt"
NUM = r'([0-9]+(?:\.[0-9]+)?)'
HASHTAG_REGEX = r'#[A-Z0-9_]+'

CANCEL_KEYWORDS = ["فعال نشد", "لغو", "منقضی", "کنسل", "برداشت اردر", "پاک شد", "تایم تمام"]

def load_processed():
    if os.path.exists(PROCESSED_FILE):
        try:
            with open(PROCESSED_FILE, "r") as f: return json.load(f)
        except Exception: return []
    return []

def save_processed(signals):
    signals = signals[-50:]
    with open(PROCESSED_FILE, "w") as f: json.dump(signals, f)

def load_last_id():
    try:
        with open(LAST_ID_FILE) as f: return int(f.read().strip())
    except Exception: return None

def save_last_id(i):
    with open(LAST_ID_FILE, "w") as f: f.write(str(i))

def send_alert(msg):
    url = f"https://api.telegram.org/bot{MAIN_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": MAIN_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    try: requests.post(url, json=payload, timeout=10)
    except: pass

def send_to_main_bot(command):
    send_alert(f"📡 سیگنال جدید:\n<code>{command}</code>")
    try:
        with open(QUEUE_FILE, "a") as f: f.write(command + "\n")
        print(f"✅ Written to queue: {command}", flush=True)
    except Exception as e:
        print(f"❌ Error writing to queue: {e}", flush=True)

def fetch_messages(client):
    try: return client.get_messages(CHANNEL_ID, limit=30)
    except ValueError:
        client.get_dialogs()
        return client.get_messages(CHANNEL_ID, limit=30)

def find_tag(text_upper):
    tags = [t for t in re.findall(HASHTAG_REGEX, text_upper) if t not in ("#SIGNAL", "#FUTURES", "#CRYPTO")]
    return tags[0] if tags else None

def extract_signal_with_ai(text):
    if not GEMINI_KEY: return None
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        prompt = f"""
        You are a crypto signal parser. Extract the trade signal from the text below.
        Return ONLY a valid JSON object with the following structure:
        {{
          "is_signal": true/false,
          "symbol": "ETH",
          "side": "LONG" or "SHORT",
          "entry": 1625.31,
          "sl": 1632.73,
          "tps": [1617.85, 1610.40, 1602.99, 1595.62, 1588.27],
          "tag": "#A533" (if exists, else null),
          "is_limit": true/false
        }}
        If it's not a trading signal, return {{"is_signal": false}}.
        
        Text:
        {text}
        """
        response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        return json.loads(response.text)
    except Exception as e:
        print(f"AI Error: {e}", flush=True)
        return None

def run_listener():
    if not TG_SESSION: return

    print("⏳ Connecting to Telegram...", flush=True)
    try:
        with TelegramClient(StringSession(TG_SESSION), API_ID, API_HASH) as client:
            now_utc = datetime.now(timezone.utc)
            processed = load_processed()

            try: messages = fetch_messages(client)
            except Exception as e:
                print(f"⚠️ Cannot read channel: {e}", flush=True)
                key = f"ACCESS_ALERT_{now_utc:%Y%m%d%H}"
                if key not in processed:
                    send_alert("⚠️ <b>اخطار مهم:</b>\nدسترسی ربات به چنل سیگنال قطع شده است!")
                    processed.append(key)
                    save_processed(processed)
                return

            last_id = load_last_id()
            max_id = max((m.id for m in messages), default=last_id or 0)
            new_processed = False

            for msg in reversed(messages):
                if last_id is not None and msg.id <= last_id: continue
                if not msg.raw_text: continue
                if (now_utc - msg.date).total_seconds() > MAX_AGE_SEC: continue

                text = msg.raw_text
                text_upper = text.upper()
                current_tag = find_tag(text_upper)
                is_limit = "LIMIT" in text_upper # 👈 فیکس شد: این متغیر تعریف نشده بود
                
                # کنسل کردن لیمیت‌ها
                if current_tag and any(k in text for k in CANCEL_KEYWORDS):
                    if f"cancel_{current_tag}" not in processed:
                        print(f"🚫 Detected cancel for {current_tag}", flush=True)
                        send_to_main_bot(f"/cancel_limit {current_tag}")
                        processed.append(f"cancel_{current_tag}")
                        new_processed = True
                    continue

                if "منقضی شده" in text or "تاچ شد" in text or "لغو" in text:
                    continue

                # 👇 پردازش اصلی با هوش مصنوعی
                if any(k in text_upper for k in ["ENTER", "STOP", "TP", "#SIGNAL", "LONG", "SHORT"]):
                    ai_data = extract_signal_with_ai(text)
                    
                    if ai_data and ai_data.get("is_signal"):
                        try:
                            symbol = str(ai_data["symbol"]).upper().replace("USDT", "")
                            side = str(ai_data["side"]).upper()
                            entry = float(ai_data["entry"])
                            sl = float(ai_data["sl"])
                            tps = [float(x) for x in ai_data["tps"]]
                            ai_tag = ai_data.get("tag")
                            ai_is_limit = ai_data.get("is_limit", False)

                            if side not in ("LONG", "SHORT") or entry <= 0 or sl <= 0 or not tps:
                                raise ValueError("Invalid AI data")

                            # پر کردن تارگت‌های جا افتاده
                            while len(tps) < 5:
                                if len(tps) >= 2: step = abs(tps[-1] - tps[-2])
                                else:
                                    step = abs(tps[0] - entry)
                                    if step == 0: step = entry * 0.01
                                if side == "LONG": tps.append(tps[-1] + step)
                                else: tps.append(tps[-1] - step)
                            
                            tps = tps[:5]
                            if any(x <= 0 for x in tps): continue
                            
                            if ai_is_limit:
                                current_tag = ai_tag if ai_tag else current_tag
                                if not current_tag:
                                    send_alert(f"⚠️ سیگنال لیمیت {symbol} هشتگ نداشت و رد شد.")
                                    continue
                            else:
                                current_tag = ai_tag
                                
                            fingerprint = f"{symbol}_{side}_{entry}_{sl}"
                            if fingerprint in processed: continue

                            cmd_type = "/auto_limit" if ai_is_limit else "/auto"
                            tag_suffix = f" {current_tag}" if current_tag else ""
                            cmd = f"{cmd_type} {symbol} {side} {entry} {sl} {' '.join([str(x) for x in tps])}{tag_suffix}"

                            print(f"🧠 AI Found Signal: {fingerprint}", flush=True)
                            send_to_main_bot(cmd)
                            processed.append(fingerprint)
                            new_processed = True
                            continue
                            
                        except Exception as parse_err:
                            print(f"AI Parse Error: {parse_err}", flush=True)

                # 👇 فالبک (پشتیبان) با Regex در صورت خرابی هوش مصنوعی
                if "ENTER" in text_upper and ("STOP LOSS" in text_upper or "STOPLOSS" in text_upper or "SL:" in text_upper):
                    try:
                        symbol_match = re.search(r'([A-Z0-9]+)[/]?USDT', text_upper)
                        if not symbol_match: continue
                        symbol = symbol_match.group(1).replace('/', '')

                        m = re.search(r"\b(BUY|LONG|SELL|SHORT)\b", text_upper)
                        if not m: continue
                        side = "LONG" if m.group(1) in ("BUY", "LONG") else "SHORT"

                        e_m = re.search(r'(?:Enter|Entry)(?:\s*Limit)?\s*[:\-]?\s*' + NUM, text, re.IGNORECASE)
                        s_m = re.search(r'(?:Stop\s*Loss|StopLoss|SL|Stop)\s*[:\-]?\s*' + NUM, text, re.IGNORECASE)
                        
                        if not e_m or not s_m: continue

                        entry, sl = e_m.group(1), s_m.group(1)
                        if (side == "LONG" and float(sl) >= float(entry)) or (side == "SHORT" and float(sl) <= float(entry)): continue

                        tps_raw = re.findall(r'TP[1-9]\s*[:\-]?\s*' + NUM, text, re.IGNORECASE)
                        tps = [float(x) for x in tps_raw]
                        if not tps: continue
                        
                        while len(tps) < 5:
                            if len(tps) >= 2: step = abs(tps[-1] - tps[-2])
                            else:
                                step = abs(tps[0] - float(entry))
                                if step == 0: step = float(entry) * 0.01
                            if side == "LONG": tps.append(tps[-1] + step)
                            else: tps.append(tps[-1] - step)
                        
                        tps = tps[:5]
                        if any(x <= 0 for x in tps): continue

                        fingerprint = f"{symbol}_{side}_{entry}_{sl}"
                        if fingerprint in processed: continue

                        if is_limit and not current_tag:
                            send_alert(f"⚠️ سیگنال لیمیت {symbol} هشتگ نداشت و رد شد.")
                            processed.append(fingerprint); new_processed = True
                            continue

                        cmd_type = "/auto_limit" if is_limit else "/auto"
                        tag_suffix = f" {current_tag}" if is_limit else ""
                        cmd = f"{cmd_type} {symbol} {side} {entry} {sl} {' '.join([str(x) for x in tps])}{tag_suffix}"

                        print(f"🎯 Fallback Found Signal: {fingerprint}", flush=True)
                        send_to_main_bot(cmd)
                        processed.append(fingerprint)
                        new_processed = True
                    except Exception as e:
                        print(f"Fallback Parse error: {e}", flush=True)

            if new_processed:
                save_processed(processed)
                print("✅ Saved processed signals.", flush=True)
            
            save_last_id(max_id)

    except Exception as e:
        print(f"Listener Fatal Error: {e}", flush=True)
        send_alert(f"🚨 خطای جدی در شنونده: {e}")

if __name__ == "__main__":
    run_listener()
