# scanner_xt_new.py
# ✅ اسکنر بازار کریپتو - صرافی XT با فیلترهای جدید
# فیلتر ۱: روزانه - EMA30 > EMA50
# فیلتر ۲: ساعتی - EMA30 > EMA50 > EMA200
# فیلتر ۳: ریسک - (EMA200 - EMA50) / EMA200 * 100 => بین 0 تا 10 درصد

import os
import time
import ccxt
import pandas as pd
import numpy as np
from datetime import datetime
from html import escape
from tqdm.auto import tqdm

# ================= CONFIG =================
EXCHANGE_ID = 'xt'
SCAN_SPOT = True
SCAN_FUTURES = True

# تایم‌فریم‌ها
DAILY_TF = '1d'
DAILY_LIMIT = 300
HOURLY_TF = '1h'
HOURLY_LIMIT = 300

# حداقل کندل مورد نیاز
MIN_BARS_REQUIRED = 200

# تنظیمات ریسک
MIN_RISK = 0.0
MAX_RISK = 10.0

# ================= ENV & SECURITY =================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

if not TELEGRAM_BOT_TOKEN:
    print("⚠️ TELEGRAM_BOT_TOKEN is not set. Running in console-only mode.")

# ================= EXCHANGE INIT =================
try:
    exchange = getattr(ccxt, EXCHANGE_ID)({
        'enableRateLimit': True,
        'timeout': 30000
    })
    exchange_markets = exchange.load_markets()
    print(f"✅ Connected to {EXCHANGE_ID.upper()} exchange")
except Exception as e:
    print(f"❌ Critical Error initializing exchange: {e}")
    exit(1)

# ================= DEDUPLICATION =================
def get_filtered_pairs():
    symbol_map = {}
    for symbol, info in exchange_markets.items():
        if not info.get('active'):
            continue
        if info.get('quote') != 'USDT':
            continue
        is_spot = info.get('spot', False)
        is_future = info.get('future', False) or info.get('swap', False)
        should_scan = (SCAN_SPOT and is_spot) or (SCAN_FUTURES and is_future)
        if should_scan:
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
        if len(data) < MIN_BARS_REQUIRED:
            return None
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['close'] = df['close'].astype(float)
        return df
    except Exception as e:
        if DEBUG_MODE:
            print(f"⚠️ Fetch error {symbol}: {e}")
        return None

# ================= MARKET CAP & VOLUME =================
def format_market_cap(value):
    """فرمت‌دهی مارکت کپ به میلیون دلار - همیشه بر حسب M$"""
    if value is None or pd.isna(value) or value <= 0:
        return "N/A"
    value_m = value / 1_000_000  # تبدیل به میلیون
    if value_m >= 1:
        return f"MC={value_m:.1f}M$" if value_m >= 10 else f"MC={value_m:.1f}M$"
    else:
        return f"MC={value_m:.2f}M$"

def get_market_cap(symbol, info, price):
    """دریافت یا محاسبه مارکت کپ"""
    try:
        market_info = info.get('info', {})
        mc = None
        # تلاش برای استخراج مارکت کپ از فیلدهای مختلف
        for key in ['marketCap', 'market_cap', 'quoteVolume']:
            if key in market_info and market_info[key]:
                try:
                    mc = float(market_info[key])
                    break
                except:
                    continue
        # اگر پیدا نشد: محاسبه از supply * price
        if mc is None or mc <= 0:
            circulating = None
            for key in ['circulating', 'circulatingSupply', 'totalSupply', 'supply']:
                if key in market_info and market_info[key]:
                    try:
                        circulating = float(market_info[key])
                        break
                    except:
                        continue
            if circulating and circulating > 0:
                mc = circulating * price
        return mc
    except:
        return None

def calculate_volume_ratio(hourly_df):
    """
    محاسبه نسبت حجم: Vα = (میانگین حجم ۵ساعت) / (میانگین حجم ۲۰ساعت)
    = (V5/5) / (V20/20) = V5/V20 * 4
    """
    try:
        if hourly_df is None or len(hourly_df) < 20:
            return None
        vol_5 = hourly_df['volume'].iloc[-5:].sum()
        vol_20 = hourly_df['volume'].iloc[-20:].sum()
        if vol_20 <= 0:
            return None
        ratio = (vol_5 / 5) / (vol_20 / 20)  # نرمال‌شده
        return round(ratio, 2)
    except:
        return None

# ================= SCAN FUNCTION =================
def scan_market(pairs):
    results = []
    total = len(pairs)
    print(f"🔍 شروع اسکن {total} نماد...")
    print(f"   فیلتر ۱: ارزهای با EMA30 > EMA50 (روزانه)")
    print(f"   فیلتر ۲: ارزهای با EMA30 > EMA50 > EMA200 (ساعتی)")
    print(f"   فیلتر ۳: Risk% = (EMA200 - EMA50) / EMA200 * 100 => بین {MIN_RISK} تا {MAX_RISK} درصد")
    print("-" * 50)

    for idx, (symbol, info) in enumerate(tqdm(pairs, desc="Scanning", total=total), 1):
        try:
            # فیلتر ۱: روزانه
            df_daily = fetch_ohlcv(symbol, DAILY_TF, DAILY_LIMIT)
            if df_daily is None:
                continue
            df_daily['ema30'] = df_daily['close'].ewm(span=30, adjust=False).mean()
            df_daily['ema50'] = df_daily['close'].ewm(span=50, adjust=False).mean()
            last_daily = df_daily.iloc[-1]
            if pd.isna(last_daily['close']) or pd.isna(last_daily['ema30']) or pd.isna(last_daily['ema50']):
                continue
            if not (last_daily['ema30'] > last_daily['ema50']):
                continue

            # فیلتر ۲: ساعتی
            df_hourly = fetch_ohlcv(symbol, HOURLY_TF, HOURLY_LIMIT)
            if df_hourly is None:
                continue
            df_hourly['ema30'] = df_hourly['close'].ewm(span=30, adjust=False).mean()
            df_hourly['ema50'] = df_hourly['close'].ewm(span=50, adjust=False).mean()
            df_hourly['ema200'] = df_hourly['close'].ewm(span=200, adjust=False).mean()
            last_hourly = df_hourly.iloc[-1]
            if pd.isna(last_hourly['close']) or pd.isna(last_hourly['ema30']) or \
               pd.isna(last_hourly['ema50']) or pd.isna(last_hourly['ema200']):
                continue
            if not (last_hourly['ema30'] > last_hourly['ema50'] > last_hourly['ema200']):
                continue

            # فیلتر ۳: ریسک
            risk_pct = ((last_hourly['ema200'] - last_hourly['ema50']) / last_hourly['ema200']) * 100
            risk_abs = abs(risk_pct)
            if not (MIN_RISK <= risk_abs <= MAX_RISK):
                continue

            # ✅ همه فیلترها پاس شدند
            mkt_type = 'F' if (info.get('future') or info.get('swap')) else 'S'
            price = last_hourly['close']
            market_cap = get_market_cap(symbol, info, price)
            volume_ratio = calculate_volume_ratio(df_hourly)

            results.append({
                'symbol': symbol,
                'price': price,
                'risk_abs': risk_abs,
                'risk_pct': risk_pct,
                'mkt_type': mkt_type,
                'market_cap': market_cap,
                'volume_ratio': volume_ratio,
            })
        except Exception as e:
            if DEBUG_MODE:
                print(f"⚠️ Error {symbol}: {e}")
        time.sleep(0.01)

    results.sort(key=lambda x: x['risk_abs'])
    return results

# ================= MESSAGE BUILDER =================
def build_message(signals, total_scanned):
    now = datetime.now().strftime('%Y/%m/%d %H:%M:%S')
    header = (
        f"🔍 <b>اسکنر XT | فیلتر ترکیبی جدید</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 نمادهای بررسی شده: <code>{total_scanned}</code>\n"
        f"✅ عبور کرده: <code>{len(signals)}</code>\n"
        f"📋 شرایط:\n"
        f" ├─ ۱) ارزهای با EMA30 > EMA50 (روزانه)\n"
        f" ├─ ۲) ارزهای با EMA30 > EMA50 > EMA200 (ساعتی)\n"
        f" └─ ۳) Risk% = (EMA200 - EMA50) / EMA200 * 100 => 0-10%\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    footer = f"\n⏰ {now} 🇮🇷\n🤖 XT Scanner v2.1"
    msgs = []
    body = ""
    MAX = 4000

    for r, s in enumerate(signals, 1):
        mc_str = format_market_cap(s['market_cap'])
        vol_str = f"Vα={s['volume_ratio']:.2f}" if s['volume_ratio'] else "Vα=N/A"
        card = (
            f"{r}. {escape(s['symbol'])} [{s['mkt_type']}]\n"
            f"💰 {s['price']:,.6f} USDT | {mc_str}\n"
            f"📊 {vol_str} | ⚠️ Risk: {s['risk_abs']:.2f}%\n"
            f"─────────────────────\n"
        )
        if len(header) + len(body) + len(card) + len(footer) > MAX - 100:
            msgs.append(header + body + footer)
            body = card
        else:
            body += card

    if body.strip():
        msgs.append(header + body + footer)
    if not msgs:
        msgs.append(f"{header}❌ هیچ نمادی شرایط را نداشت.{footer}")
    return msgs

# ================= TELEGRAM =================
def send_telegram_message(text, chat_id=None):
    if not TELEGRAM_BOT_TOKEN:
        print("⚠️ Telegram token not set, skipping message")
        return
    targets = [chat_id] if chat_id else TELEGRAM_CHAT_IDS
    for cid in targets:
        cid = cid.strip()
        if not cid:
            continue
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            'chat_id': cid,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True
        }
        try:
            import requests
            r = requests.post(url, json=payload, timeout=30)
            if DEBUG_MODE and r.status_code != 200:
                print(f"⚠️ Telegram ({cid}): {r.status_code} | {r.text[:100]}")
        except Exception as e:
            print(f"❌ Telegram Error ({cid}): {e}")

# ================= MAIN =================
def run():
    print("🚀 شروع اسکنر XT با فیلترهای جدید...")
    pairs = get_filtered_pairs()
    print(f"📊 کل نمادهای فعال (بدون تکرار): {len(pairs)}")
    results = scan_market(pairs)
    print(f"\n✅ اسکن پایان یافت: {len(results)} نماد پیدا شد")

    if results:
        print("\n" + "=" * 60)
        print("🎯 نمادهای پیدا شده (مرتب شده بر اساس ریسک):")
        print("=" * 60)
        for i, r in enumerate(results, 1):
            mc_str = format_market_cap(r['market_cap'])
            vol_str = f"Vα={r['volume_ratio']:.2f}" if r['volume_ratio'] else "Vα=N/A"
            print(f"\n{i}. {r['symbol']} [{r['mkt_type']}]")
            print(f"   Price: {r['price']:,.6f} | {mc_str}")
            print(f"   {vol_str} | ⚠️ Risk: {r['risk_abs']:.2f}%")
        print("=" * 60)

    if TELEGRAM_CHAT_IDS:
        print("\n📤 ارسال نتایج به تلگرام...")
        messages = build_message(results, len(pairs))
        for msg in messages:
            send_telegram_message(msg)
            time.sleep(0.3)
        print("✅ پیام‌ها ارسال شدند")
    else:
        print("\n⚠️ TELEGRAM_CHAT_ID تنظیم نشده است")
    return results

# ================= RUN =================
if __name__ == "__main__":
    run()
