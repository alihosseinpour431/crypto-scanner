# scanner_xt_v2.py
# ✅ اسکنر بازار کریپتو - صرافی XT با فیلترهای آپدیت‌شده
# فیلتر ۱: روزانه - EMA50 > EMA200
# فیلتر ۲: ساعتی - EMA30 > EMA50 > EMA200
# فیلتر ۳: ریسک - |(EMA200 - EMA50) / EMA200 * 100| => بین 0 تا 10 درصد
# فیلتر ۴: V_alpha - حجم 5 ساعت اخیر / حجم 200 ساعت اخیر >= 0.025
# ➕ نمایش مارکت‌کپ به میلیون دلار در خروجی تلگرام

import os
import time
import ccxt
import pandas as pd
import numpy as np
from datetime import datetime
from html import escape
from tqdm.auto import tqdm
import requests

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

# تنظیمات V_alpha (حجم)
MIN_V_ALPHA = 0.025  # حداقل نسبت حجم 5ساعت به 200ساعت (2.5%)

# تنظیمات مارکت‌کپ
USE_COINGECKO = True  # دریافت مارکت‌کپ از CoinGecko
COINGECKO_CACHE = {}  # کش برای جلوگیری از درخواست‌های تکراری

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

# ================= HELPER: Format Market Cap =================
def format_market_cap(value_usd):
    """تبدیل مارکت‌کپ به فرمت میلیون دلار"""
    if value_usd is None:
        return "N/A"
    try:
        value_million = value_usd / 1_000_000
        if value_million >= 1000:
            return f"${value_million/1000:.2f} B"  # Billion
        elif value_million >= 1:
            return f"${value_million:.2f} M"  # Million
        else:
            return f"${value_million*1000:.2f} K"  # Thousand
    except:
        return "N/A"

# ================= HELPER: Get Market Cap =================
def get_market_cap(symbol, price):
    """
    دریافت مارکت‌کپ به دلار
    اولویت: ۱) CoinGecko  ۲) محاسبه دستی (price × supply)  ۳) None
    """
    try:
        # استخراج نام ارز پایه (مثلاً BTC از BTC/USDT)
        base = symbol.split('/')[0].upper()
        
        # ✅ روش ۱: دریافت از CoinGecko
        if USE_COINGECKO:
            # استفاده از کش برای جلوگیری از درخواست‌های تکراری
            if base in COINGECKO_CACHE:
                return COINGECKO_CACHE[base]
            
            # نگاشت نمادها به IDهای CoinGecko (برای موارد خاص)
            symbol_mapping = {
                'WBTC': 'wrapped-bitcoin',
                'STETH': 'staked-ether',
                # اضافه کنید اگر نیاز بود
            }
            cg_id = symbol_mapping.get(base, base.lower())
            
            url = f"https://api.coingecko.com/api/v3/coins/{cg_id}"
            params = {'localization': False, 'tickers': False, 'market_data': True, 'community_data': False, 'developer_data': False}
            
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                market_cap = data.get('market_data', {}).get('market_cap', {}).get('usd')
                if market_cap:
                    COINGECKO_CACHE[base] = market_cap
                    return market_cap
            
            # اگر پیدا نشد، کش کنیم که دوباره تلاش نکنیم (برای ۱ ساعت)
            COINGECKO_CACHE[base] = None
        
        # ✅ روش ۲: محاسبه دستی (price × circulating_supply)
        # نکته: صرافی XT ممکن است circulating_supply را در market info داشته باشد
        market_info = exchange_markets.get(symbol, {})
        supply = market_info.get('info', {}).get('circulating_supply') or market_info.get('limits', {}).get('amount', {}).get('max')
        
        if supply and isinstance(supply, (int, float)) and supply > 0:
            return price * supply
        
        # ❌ اگر هیچ‌کدام جواب نداد
        return None
        
    except Exception as e:
        if DEBUG_MODE:
            print(f"⚠️ MarketCap error for {symbol}: {e}")
        return None

# ================= DEDUPLICATION =================
def get_filtered_pairs():
    """
    دریافت لیست جفت‌ارزها بدون تکرار
    اگر ارزی هم اسپات دارد هم فیوچرز، فقط اسپات را برمی‌گرداند
    """
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
    """دریافت داده‌های OHLCV"""
    try:
        data = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if len(data) < MIN_BARS_REQUIRED:
            return None
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['close'] = df['close'].astype(float)
        df['volume'] = df['volume'].astype(float)
        return df
    except Exception as e:
        if DEBUG_MODE:
            print(f"⚠️ Fetch error {symbol}: {e}")
        return None

# ================= SCAN FUNCTION =================
def scan_market(pairs):
    """
    اسکن بازار با فیلترهای جدید:
    ۱. روزانه: EMA50 > EMA200
    ۲. ساعتی: EMA30 > EMA50 > EMA200
    ۳. ریسک: |(EMA200 - EMA50) / EMA200 * 100| => بین 0 تا 10 درصد
    ۴. V_alpha: حجم 5 ساعت / حجم 200 ساعت >= MIN_V_ALPHA
    """
    results = []
    total = len(pairs)

    print(f"🔍 شروع اسکن {total} نماد...")
    print(f"   فیلتر ۱: EMA50 > EMA200 (روزانه) ⭐ جدید")
    print(f"   فیلتر ۲: EMA30 > EMA50 > EMA200 (ساعتی)")
    print(f"   فیلتر ۳: Risk% = |(EMA200 - EMA50) / EMA200 * 100| => {MIN_RISK}-{MAX_RISK}%")
    print(f"   فیلتر ۴: V_alpha = Vol_5h / Vol_200h >= {MIN_V_ALPHA*100:.1f}%")
    print("-" * 60)

    for idx, (symbol, info) in enumerate(tqdm(pairs, desc="Scanning", total=total), 1):
        try:
            # ========== فیلتر ۱: روزانه - فقط EMA50 > EMA200 ==========
            df_daily = fetch_ohlcv(symbol, DAILY_TF, DAILY_LIMIT)
            if df_daily is None:
                continue

            df_daily['ema50'] = df_daily['close'].ewm(span=50, adjust=False).mean()
            df_daily['ema200'] = df_daily['close'].ewm(span=200, adjust=False).mean()
            last_daily = df_daily.iloc[-1]

            if pd.isna(last_daily['close']) or pd.isna(last_daily['ema50']) or pd.isna(last_daily['ema200']):
                continue

            # ✅ شرط جدید روزانه: فقط EMA50 > EMA200
            if not (last_daily['ema50'] > last_daily['ema200']):
                continue

            # ========== فیلتر ۲: ساعتی - EMA30 > EMA50 > EMA200 ==========
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

            # ========== فیلتر ۳: محاسبه ریسک ==========
            risk_pct = ((last_hourly['ema200'] - last_hourly['ema50']) / last_hourly['ema200']) * 100
            risk_abs = abs(risk_pct)
            if not (MIN_RISK <= risk_abs <= MAX_RISK):
                continue

            # ========== فیلتر ۴: V_alpha - نسبت حجم 5 ساعت به 200 ساعت ==========
            # جمع حجم 5 کندل آخر (5 ساعت اخیر)
            vol_5h = df_hourly['volume'].iloc[-5:].sum()
            # جمع حجم 200 کندل آخر (200 ساعت اخیر)
            vol_200h = df_hourly['volume'].iloc[-200:].sum()
            
            if vol_200h > 0:
                v_alpha = vol_5h / vol_200h
            else:
                v_alpha = 0
            
            # شرط: V_alpha باید حداقل MIN_V_ALPHA باشد
            if v_alpha < MIN_V_ALPHA:
                continue

            # ✅ همه فیلترها پاس شدند - دریافت مارکت‌کپ
            market_cap = get_market_cap(symbol, last_hourly['close'])
            mkt_type = 'F' if (info.get('future') or info.get('swap')) else 'S'

            results.append({
                'symbol': symbol,
                'price': last_hourly['close'],
                'daily_ema50': last_daily['ema50'],
                'daily_ema200': last_daily['ema200'],
                'hourly_ema30': last_hourly['ema30'],
                'hourly_ema50': last_hourly['ema50'],
                'hourly_ema200': last_hourly['ema200'],
                'risk_pct': risk_pct,
                'risk_abs': risk_abs,
                'v_alpha': v_alpha,
                'vol_5h': vol_5h,
                'vol_200h': vol_200h,
                'market_cap': market_cap,
                'mkt_type': mkt_type,
                'info': info
            })

        except Exception as e:
            if DEBUG_MODE:
                print(f"⚠️ Error {symbol}: {e}")

        time.sleep(0.01)  # رعایت Rate Limit

    # سورت بر اساس ریسک (کم به زیاد)
    results.sort(key=lambda x: x['risk_abs'])
    return results

# ================= MESSAGE BUILDER =================
def build_message(signals, total_scanned):
    """ساخت پیام تلگرام با نمایش مارکت‌کپ"""
    now = datetime.now().strftime('%Y/%m/%d %H:%M:%S')

    header = (
        f"🔍 <b>اسکنر XT | فیلتر ترکیبی v2</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 نمادهای بررسی شده: <code>{total_scanned}</code>\n"
        f"✅ عبور کرده: <code>{len(signals)}</code>\n"
        f"📋 شرایط:\n"
        f" ├─ ۱) EMA50 > EMA200 (روزانه) ⭐\n"
        f" ├─ ۲) EMA30 > EMA50 > EMA200 (ساعتی)\n"
        f" ├─ ۳) Risk% = |(EMA200-EMA50)/EMA200×100| ➜ 0-10%\n"
        f" └─ ۴) V_alpha = Vol_5h/Vol_200h ➜ ≥{MIN_V_ALPHA*100:.1f}%\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    footer = f"\n⏰ {now} 🇮🇷\n🤖 XT Scanner v2.1"

    msgs = []
    body = ""
    MAX = 4000  # محدودیت کاراکتر تلگرام

    for r, s in enumerate(signals, 1):
        # فرمت مارکت‌کپ به میلیون/میلیارد دلار
        mc_display = format_market_cap(s['market_cap'])
        
        card = (
            f"{r}. {escape(s['symbol'])} [{s['mkt_type']}]\n"
            f"💰 Price: {s['price']:,.6f} USDT\n"
            f"💎 MarketCap: {mc_display}\n"
            f"📈 Daily: EMA50={s['daily_ema50']:,.6f} > EMA200={s['daily_ema200']:,.6f}\n"
            f"📈 Hourly: EMA30={s['hourly_ema30']:,.6f} > EMA50={s['hourly_ema50']:,.6f} > EMA200={s['hourly_ema200']:,.6f}\n"
            f"⚠️ Risk: {s['risk_abs']:.2f}% (signed: {s['risk_pct']:+.2f}%)\n"
            f"📊 V_alpha: {s['v_alpha']*100:.2f}% (5h/200h vol)\n"
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
    """ارسال پیام به تلگرام"""
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
            r = requests.post(url, json=payload, timeout=30)
            if DEBUG_MODE and r.status_code != 200:
                print(f"⚠️ Telegram ({cid}): {r.status_code} | {r.text[:100]}")
        except Exception as e:
            print(f"❌ Telegram Error ({cid}): {e}")

# ================= MAIN =================
def run():
    """تابع اصلی اجرا"""
    print("🚀 شروع اسکنر XT با فیلترهای v2...")

    pairs = get_filtered_pairs()
    print(f"📊 کل نمادهای فعال (بدون تکرار): {len(pairs)}")

    results = scan_market(pairs)

    print(f"\n✅ اسکن پایان یافت: {len(results)} نماد پیدا شد")

    # نمایش نتایج در کنسول
    if results:
        print("\n" + "=" * 70)
        print("🎯 نمادهای پیدا شده (مرتب شده بر اساس ریسک):")
        print("=" * 70)
        for i, r in enumerate(results, 1):
            mc_fmt = format_market_cap(r['market_cap'])
            print(f"\n{i}. {r['symbol']} [{r['mkt_type']}] | MC: {mc_fmt}")
            print(f"   Price: {r['price']:,.6f} | V_alpha: {r['v_alpha']*100:.2f}%")
            print(f"   Daily: EMA50={r['daily_ema50']:,.6f} > EMA200={r['daily_ema200']:,.6f}")
            print(f"   Hourly: EMA30>EMA50>EMA200 ✓")
            print(f"   ⚠️ Risk: {r['risk_abs']:.2f}% (signed: {r['risk_pct']:+.2f}%)")
        print("=" * 70)

    # ارسال به تلگرام
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
