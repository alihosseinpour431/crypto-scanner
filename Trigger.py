# Trigger.py
# ✅ نسخه نهایی بهینه‌شده برای GitHub Actions - منطق سه فیلتر
# فیلتر ۱: روزانه - EMA30 > EMA50
# فیلتر ۲: ساعتی - RSI(30) > EMA50(RSI)
# فیلتر ۳: کنترل اشباع - 30 < RSI_MA < 70

import os
import time
import requests
import ccxt
import pandas as pd
import pytz
import jdatetime
import numpy as np
from datetime import datetime
from html import escape
from tqdm.auto import tqdm

# ================= CONFIG =================
EXCHANGE_ID = 'xt'
SCAN_SPOT = True
SCAN_FUTURES = True

# تایم‌فریم‌ها و حداقل کندل
DAILY_TF = '1d'
DAILY_LIMIT = 300
HOURLY_TF = '1h'
HOURLY_LIMIT = 300
MIN_BARS_REQUIRED = 250

# پارامترهای اندیکاتور
RSI_LENGTH = 30
RSI_EMA_LENGTH = 50

# ================= ENV & SECURITY =================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("❌ TELEGRAM_BOT_TOKEN is not set in environment variables!")

TELEGRAM_CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "487817626").split(",") if cid.strip()]
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
COINGECKO_API = "https://api.coingecko.com/api/v3"

# ================= EXCHANGE INIT =================
try:
    exchange = getattr(ccxt, EXCHANGE_ID)({
        'enableRateLimit': True,
        'timeout': 30000
    })
    exchange_markets = exchange.load_markets()
except Exception as e:
    print(f"❌ Critical Error initializing exchange: {e}")
    exit(1)

# ================= CACHE =================
market_cap_cache = {}

# ================= TELEGRAM =================
def send_telegram_message(text, chat_id=None):
    targets = [chat_id] if chat_id else TELEGRAM_CHAT_IDS
    for cid in targets:
        cid = cid.strip()
        if not cid: continue
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            'chat_id': cid,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True
        }
        try:
            r = requests.post(url, json=payload, timeout=30)
            if DEBUG_MODE and r.status_code != 200:
                print(f"⚠️ Telegram ({cid}): {r.status_code} | {r.text[:100]}")
        except Exception as e:
            print(f"❌ Telegram Error ({cid}): {e}")

# ================= COINGECKO =================
def load_market_caps():
    if market_cap_cache: return
    print("📥 Loading Market Cap from CoinGecko ...")
    session = requests.Session()
    for page in range(1, 11):
        try:
            url = f"{COINGECKO_API}/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page={page}&sparkline=false"
            r = session.get(url, timeout=20)
            data = r.json()
            if not isinstance(data, list) or len(data) == 0: break
            for coin in data:
                sym = str(coin.get('symbol', '')).upper().strip()
                mc = coin.get('market_cap')
                if sym and isinstance(mc, (int, float)):
                    market_cap_cache[sym] = mc
            if len(data) < 250: break
            time.sleep(0.1)
        except: break
    print(f"✅ Market Cap loaded: {len(market_cap_cache)} symbols")

def get_market_cap(symbol):
    base = symbol.split('/')[0].upper()
    return market_cap_cache.get(base)

def fmt_mc(value):
    if value is None or (isinstance(value, float) and np.isnan(value)): return "🔸 N/A"
    v = float(value)
    if v >= 1e12: return f"💎 ${v/1e12:.2f}T"
    if v >= 1e9: return f"💎 ${v/1e9:.2f}B"
    if v >= 1e6: return f"💎 ${v/1e6:.2f}M"
    return f"💎 ${v:,.0f}"

# ================= DEDUPLICATION =================
def get_filtered_pairs():
    symbol_map = {}
    for symbol, info in exchange_markets.items():
        if not info.get('active') or info.get('quote') != 'USDT': continue
        is_spot = info.get('spot', False)
        is_future = info.get('future', False) or info.get('swap', False)
        if (SCAN_SPOT and is_spot) or (SCAN_FUTURES and is_future):
            base = symbol.split('/')[0].upper()
            if base not in symbol_map:
                symbol_map[base] = (symbol, info, is_spot)
            elif is_spot and not symbol_map[base][2]:
                symbol_map[base] = (symbol, info, True)
    return [(sym, inf) for sym, inf, _ in symbol_map.values()]

# ================= DATA FETCH =================
def fetch_ohlcv(symbol, timeframe, limit):
    try:
        data = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if len(data) < MIN_BARS_REQUIRED: return None
        df = pd.DataFrame(data, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
        return df
    except Exception as e:
        if DEBUG_MODE: print(f"⚠️ Fetch error {symbol}: {e}")
        return None

# ================= INDICATOR 1: DAILY EMA =================
def calc_daily_ema(df):
    """
    فیلتر اول - تایم روزانه
    محاسبه EMA30 و EMA50 روی قیمت بسته شدن
    شرط: EMA30 > EMA50 (روند صعودی کوتاه‌مدت)
    """
    df = df.copy()
    df['ema30'] = df['c'].ewm(span=30, adjust=False).mean()
    df['ema50'] = df['c'].ewm(span=50, adjust=False).mean()
    return df

# ================= INDICATOR 2: HOURLY RSI + RSI_MA =================
def calc_hourly_rsi(df):
    """
    فیلتر دوم - تایم ساعتی
    1. محاسبه RSI(30)
    2. محاسبه EMA50 روی RSI (به نام RSI_MA)
    شرط: RSI > RSI_MA (مومنتوم فعلی قوی‌تر از میانگین مومنتوم)
    """
    df = df.copy()
    
    # --- محاسبه RSI(30) ---
    delta = df['c'].diff()
    gain = delta.where(delta > 0, 0).rolling(RSI_LENGTH).mean()
    loss = -delta.where(delta < 0, 0).rolling(RSI_LENGTH).mean()
    
    rs = gain / loss.replace(0, np.nan)
    df['rsi'] = 100 - (100 / (1 + rs))
    df['rsi'] = df['rsi'].fillna(100)  # وقتی loss=0 باشد، RSI=100
    
    # --- محاسبه RSI_MA = EMA50(RSI) ---
    df['rsi_ma'] = df['rsi'].ewm(span=RSI_EMA_LENGTH, adjust=False).mean()
    
    return df

# ================= SCAN MAIN LOGIC =================
def scan_with_three_filters(pairs):
    """
    منطق اصلی اسکن با سه فیلتر:
    1️⃣ روزانه: EMA30 > EMA50
    2️⃣ ساعتی: RSI > RSI_MA
    3️⃣ کنترل اشباع: 30 < RSI_MA < 70
    """
    results = []
    total = len(pairs)
    print(f"🔍 شروع اسکن با ۳ فیلتر روی {total} نماد...")
    
    for idx, (symbol, info) in enumerate(tqdm(pairs, desc="Scanning", total=total), 1):
        try:
            # 🔹 فیلتر ۱: تایم روزانه - EMA30 > EMA50
            df_d = fetch_ohlcv(symbol, DAILY_TF, DAILY_LIMIT)
            if df_d is None: continue
            df_d = calc_daily_ema(df_d)
            last_d = df_d.iloc[-1]
            
            cond_daily = (
                pd.notna(last_d['ema30']) and 
                pd.notna(last_d['ema50']) and 
                last_d['ema30'] > last_d['ema50']
            )
            if not cond_daily: continue  # ❌ رد شد از فیلتر روزانه
            
            # 🔹 فیلتر ۲ و ۳: تایم ساعتی - RSI و RSI_MA
            df_h = fetch_ohlcv(symbol, HOURLY_TF, HOURLY_LIMIT)
            if df_h is None: continue
            df_h = calc_hourly_rsi(df_h)
            last_h = df_h.iloc[-1]
            
            # فیلتر ۲: RSI > RSI_MA (قدرت گرفتن مومنتوم)
            cond_rsi = (
                pd.notna(last_h['rsi']) and 
                pd.notna(last_h['rsi_ma']) and 
                last_h['rsi'] > last_h['rsi_ma']
            )
            if not cond_rsi: continue  # ❌ رد شد از فیلتر مومنتوم
            
            # فیلتر ۳: 30 < RSI_MA < 70 (ناحیه متعادل، بدون اشباع)
            rsi_ma_val = last_h['rsi_ma']
            cond_balance = (
                pd.notna(rsi_ma_val) and 
                30 < rsi_ma_val < 70
            )
            if not cond_balance: continue  # ❌ رد شد از فیلتر اشباع
            
            # ✅ همه فیلترها پاس شدند!
            mkt = 'F' if (info.get('future') or info.get('swap')) else 'S'
            results.append({
                'symbol': symbol,
                'price': last_h['c'],
                'rsi': last_h['rsi'],
                'rsi_ma': last_h['rsi_ma'],
                'mc': get_market_cap(symbol),
                'mkt_type': mkt,
                'info': info
            })
            
        except Exception as e:
            if DEBUG_MODE: print(f"⚠️ Error {symbol}: {e}")
        time.sleep(0.02)  # جلوگیری از ریت‌لیمیت
    
    return results

# ================= MESSAGE BUILDER =================
def build_final_message(signals, total_scanned):
    now = jdatetime.datetime.now(pytz.timezone('Asia/Tehran')).strftime('%Y/%m/%d %H:%M:%S')
    
    header = (
        f"🎯 <b>گزارش نهایی اسکن | ۳ فیلتر هوشمند</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 نمادهای بررسی شده: <code>{total_scanned}</code>\n"
        f"✅ سیگنال‌های یافت شده: <code>{len(signals)}</code>\n"
        f"📋 شرایط فیلترها:\n"
        f" ├─ 1️⃣ روزانه: EMA30 {'>'} EMA50 ✅\n"
        f" ├─ 2️⃣ ساعتی: RSI(30) {'>'} RSI_MA ✅\n"
        f" └─ 3️⃣ اشباع: 30 {'<'} RSI_MA {'<'} 70 ✅\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    footer = f"\n⏰ {now} 🇮🇷\n🤖 AlphaScanner v3.0"
    
    msgs = []
    body = ""
    MAX = 4000  # محدودیت تلگرام
    
    for r, s in enumerate(signals, 1):
        card = (
            f"{r}. {escape(s['symbol'])} [{s['mkt_type']}]\n"
            f"💰 {s['price']:,.4f} USDT\n"
            f"📊 RSI: {s['rsi']:.1f} | RSI_MA: {s['rsi_ma']:.1f}\n"
            f"💎 MC: {fmt_mc(s['mc'])}\n"
            f"─────────────────────\n"
        )
        # مدیریت طول پیام برای جلوگیری از خطای تلگرام
        if len(header) + len(body) + len(card) + len(footer) > MAX - 100:
            msgs.append(header + body + footer)
            body = card
        else:
            body += card
    
    if body.strip():
        msgs.append(header + body + footer)
    
    if not msgs:
        msgs.append(f"{header}❌ هیچ نمادی هر سه فیلتر را پاس نکرد.{footer}")
    
    return msgs

# ================= MAIN =================
def run():
    print("🚀 شروع اسکن با منطق ۳ فیلتر...")
    load_market_caps()
    pairs = get_filtered_pairs()
    print(f"📊 کل نمادهای فعال (بدون تکرار): {len(pairs)}")
    
    # 🔹 اجرای اسکن با سه فیلتر
    results = scan_with_three_filters(pairs)
    print(f"✅ اسکن پایان یافت: {len(results)} نماد انتخاب شدند")
    
    # 🔹 ارسال گزارش به تلگرام
    for msg in build_final_message(results, len(pairs)):
        send_telegram_message(msg)
        time.sleep(0.3)
    
    print("✅ پایان کامل اسکن")
    return results

# ================= RUN =================
if __name__ == "__main__":
    send_telegram_message("🤖 <b>اسکن ۳ فیلتری شروع شد</b>\n📅 روزانه: EMA30{'>'}EMA50\n⏰ ساعتی: RSI{'>'}RSI_MA\n⚖️ اشباع: 30{'<'}RSI_MA{'<'}70")
    run()
    send_telegram_message("✅ <b>اسکن با موفقیت پایان یافت</b>")
