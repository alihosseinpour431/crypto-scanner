# scanner.py
# ✅ نسخه نهایی بهینه‌شده برای GitHub Actions - منطق دو مستطیل
# مستطیل ۱: فیلتر ترکیبی روزانه + ساعتی + محاسبه Alpha
# مستطیل ۲: فیلتر ۳۰ دقیقه + محاسبه Risk% + سورت صعودی

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
DAILY_LIMIT = 230
HOURLY_TF = '1h'
HOURLY_LIMIT = 300
TF_30M = '30m'
LIMIT_30M = 250
MIN_BARS_REQUIRED = 200

# پارامترهای اندیکاتور
RSI_LENGTH = 30
ALPHA_SHORT = 3
ALPHA_LONG = 10

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
    if value is None or np.isnan(value): return "🔸 N/A"
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

# ================= DATA & INDICATORS =================
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

def calc_indicators(df):
    """محاسبه EMAها، RSI(30)، OBV و Alphaهای حجم و OBV"""
    df = df.copy()
    df['ema30'] = df['c'].ewm(span=30, adjust=False).mean()
    df['ema50'] = df['c'].ewm(span=50, adjust=False).mean()
    df['ema200'] = df['c'].ewm(span=200, adjust=False).mean()
    
    delta = df['c'].diff()
    gain = delta.where(delta > 0, 0).rolling(RSI_LENGTH).mean()
    loss = -delta.where(delta < 0, 0).rolling(RSI_LENGTH).mean()
    rs = gain / loss.replace(0, np.nan)
    df['rsi'] = 100 - (100 / (1 + rs))
    
    direction = np.sign(df['c'].diff()).fillna(0)
    df['obv'] = (direction * df['v']).cumsum()
    
    df['vol_alpha'] = df['v'].rolling(ALPHA_SHORT).mean() / df['v'].rolling(ALPHA_LONG).mean()
    df['obv_alpha'] = df['obv'].rolling(ALPHA_SHORT).mean() / df['obv'].rolling(ALPHA_LONG).mean()
    return df

# ================= SCAN RECTANGLE 1 =================
def scan_rectangle1(pairs):
    results = []
    total = len(pairs)
    print(f"🔲 شروع مستطیل ۱: {total} نماد")
    
    for idx, (symbol, info) in enumerate(tqdm(pairs, desc="Rect1", total=total), 1):
        try:
            # ۱. فیلتر روزانه
            df_d = fetch_ohlcv(symbol, DAILY_TF, DAILY_LIMIT)
            if df_d is None: continue
            df_d = calc_indicators(df_d)
            last_d = df_d.iloc[-1]
            daily_ok = (
                pd.notna(last_d['c']) and pd.notna(last_d['ema30']) and pd.notna(last_d['ema50']) and pd.notna(last_d['rsi']) and
                last_d['c'] > last_d['ema30'] > last_d['ema50'] and last_d['rsi'] > 50
            )
            if not daily_ok: continue
            
            # ۲. فیلتر ساعتی (فقط اگر روزانه پاس شد)
            df_h = fetch_ohlcv(symbol, HOURLY_TF, HOURLY_LIMIT)
            if df_h is None: continue
            df_h = calc_indicators(df_h)
            last_h = df_h.iloc[-1]
            hourly_ok = (
                pd.notna(last_h['c']) and pd.notna(last_h['ema50']) and pd.notna(last_h['ema200']) and pd.notna(last_h['rsi']) and
                last_h['c'] > last_h['ema50'] > last_h['ema200'] and last_h['rsi'] > 50
            )
            if not hourly_ok: continue
            
            # ✅ هر دو فیلتر پاس شدند
            mkt = 'F' if (info.get('future') or info.get('swap')) else 'S'
            results.append({
                'symbol': symbol,
                'price': last_h['c'],
                'rsi': last_h['rsi'],
                'vol_alpha': last_h['vol_alpha'],
                'obv_alpha': last_h['obv_alpha'],
                'mc': get_market_cap(symbol),
                'mkt_type': mkt,
                'info': info
            })
        except Exception as e:
            if DEBUG_MODE: print(f"⚠️ Rect1 {symbol}: {e}")
        time.sleep(0.02)
    return results

# ================= SCAN RECTANGLE 2 =================
def scan_rectangle2(rect1_results):
    results = []
    total = len(rect1_results)
    print(f"🔲 شروع مستطیل ۲: {total} نماد")
    
    for idx, sig in enumerate(tqdm(rect1_results, desc="Rect2", total=total), 1):
        symbol = sig['symbol']
        info = sig['info']
        try:
            df = fetch_ohlcv(symbol, TF_30M, LIMIT_30M)
            if df is None: continue
            df = calc_indicators(df)
            last = df.iloc[-1]
            
            # فیلتر ۳۰ دقیقه
            cond = (
                pd.notna(last['c']) and pd.notna(last['ema30']) and pd.notna(last['ema50']) and 
                pd.notna(last['ema200']) and pd.notna(last['rsi']) and
                last['c'] > last['ema30'] > last['ema50'] > last['ema200'] and
                last['rsi'] > 50
            )
            if not cond: continue
            
            # محاسبه Risk%
            risk_pct = ((last['c'] - last['ema200']) / last['ema200']) * 100
            
            results.append({
                'symbol': symbol,
                'price': last['c'],
                'rsi': last['rsi'],
                'ema200': last['ema200'],
                'risk': risk_pct,
                'mc': get_market_cap(symbol),
                'mkt_type': sig['mkt_type']
            })
        except Exception as e:
            if DEBUG_MODE: print(f"⚠️ Rect2 {symbol}: {e}")
        time.sleep(0.02)
    
    # ✅ سورت صعودی بر اساس Risk% (کم‌ریسک اول)
    results.sort(key=lambda x: x['risk'] if x['risk'] is not None else 999)
    return results

# ================= MESSAGE BUILDERS =================
def build_rect1_message(signals, total_pairs):
    now = jdatetime.datetime.now(pytz.timezone('Asia/Tehran')).strftime('%Y/%m/%d %H:%M:%S')
    header = (
        f"🔍 <b>گزارش مستطیل ۱ | فیلتر ترکیبی</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 نمادهای بررسی شده: <code>{total_pairs}</code>\n"
        f"✅ عبور کرده: <code>{len(signals)}</code>\n"
        f"📋 شرایط:\n"
        f" ├─ روزانه: Price>EMA30>EMA50 | RSI>50\n"
        f" └─ ساعتی: Price>EMA50>EMA200 | RSI>50\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    footer = f"\n⏰ {now} 🇮🇷\n🤖 AlphaScanner v2.0"
    
    msgs = []
    body = ""
    MAX = 4000
    for r, s in enumerate(signals, 1):
        card = (
            f"{r}. {escape(s['symbol'])} [{s['mkt_type']}]\n"
            f"💰 {s['price']:,.4f} USDT\n"
            f"📊 RSI: {s['rsi']:.1f}\n"
            f"⚡ Vα: {s['vol_alpha']:.3f} | OBVα: {s['obv_alpha']:.3f}\n"
            f"💎 MC: {fmt_mc(s['mc'])}\n"
            f"─────────────────────\n"
        )
        if len(header) + len(body) + len(card) + len(footer) > MAX - 100:
            msgs.append(header + body + footer)
            body = card
        else:
            body += card
    if body.strip(): msgs.append(header + body + footer)
    if not msgs: msgs.append(f"{header}❌ هیچ نمادی از مستطیل ۱ عبور نکرد.{footer}")
    return msgs

def build_rect2_message(signals, rect1_count):
    now = jdatetime.datetime.now(pytz.timezone('Asia/Tehran')).strftime('%Y/%m/%d %H:%M:%S')
    header = (
        f"🎯 <b>گزارش مستطیل ۲ | فیلتر نهایی 30m</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📥 ورودی از مستطیل ۱: <code>{rect1_count}</code>\n"
        f"✅ نهایی شده: <code>{len(signals)}</code>\n"
        f"📋 شرایط 30m: Price>EMA30>EMA50>EMA200 | RSI>50\n"
        f"🔽 مرتب شده بر اساس Risk% (کم به زیاد)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    footer = f"\n⏰ {now} 🇮🇷\n🤖 AlphaScanner v2.0"
    
    msgs = []
    body = ""
    MAX = 4000
    for r, s in enumerate(signals, 1):
        card = (
            f"{r}. {escape(s['symbol'])} [{s['mkt_type']}]\n"
            f"💰 {s['price']:,.4f} | 🎯 EMA200: {s['ema200']:,.4f}\n"
            f"📊 RSI: {s['rsi']:.1f}\n"
            f"⚠️ Risk: {s['risk']:+.2f}%\n"
            f"💎 MC: {fmt_mc(s['mc'])}\n"
            f"─────────────────────\n"
        )
        if len(header) + len(body) + len(card) + len(footer) > MAX - 100:
            msgs.append(header + body + footer)
            body = card
        else:
            body += card
    if body.strip(): msgs.append(header + body + footer)
    if not msgs: msgs.append(f"{header}❌ هیچ نمادی شرایط مستطیل ۲ را نداشت.{footer}")
    return msgs

# ================= MAIN =================
def run():
    print("🚀 شروع اسکن دو مستطیلی...")
    load_market_caps()
    pairs = get_filtered_pairs()
    print(f"📊 کل نمادهای فعال (بدون تکرار): {len(pairs)}")
    
    # 🔲 مستطیل ۱
    rect1_res = scan_rectangle1(pairs)
    print(f"✅ مستطیل ۱ پایان یافت: {len(rect1_res)} نماد")
    
    if rect1_res:
        for msg in build_rect1_message(rect1_res, len(pairs)):
            send_telegram_message(msg)
            time.sleep(0.3)
    
    # 🔲 مستطیل ۲
    rect2_res = scan_rectangle2(rect1_res)
    print(f"✅ مستطیل ۲ پایان یافت: {len(rect2_res)} نماد")
    
    for msg in build_rect2_message(rect2_res, len(rect1_res)):
        send_telegram_message(msg)
        time.sleep(0.3)
        
    print("✅ پایان کامل اسکن")

# ================= RUN =================
if __name__ == "__main__":
    send_telegram_message("🤖 <b>اسکن دو مستطیلی شروع شد</b>\n📦 مستطیل ۱ (روزانه+ساعتی) ← 🎯 مستطیل ۲ (30m+Risk)")
    run()
    send_telegram_message("✅ <b>اسکن با موفقیت پایان یافت</b>")
