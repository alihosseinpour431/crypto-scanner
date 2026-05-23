# ✅ اسکنر بازار کریپتو - صرافی XT (فقط فیوچرز)
# فیلتر ۱: روزانه - Price > EMA20 > EMA50
# فیلتر ۲: ساعتی - Price > EMA20 > EMA50
# فیلتر ۳: ساعتی - RSI بین 50 تا 70
# فیلتر ۴: هوشمند حجم (مقایسه میانگین ۵ ساعت اخیر با ۲۰۰ ساعت)
# فیلتر ۵: مارکت‌کپ بین ۵ تا ۱۰۰ میلیون دلار
# + محاسبه ریسک: (EMA20 - EMA50) / EMA50 * 100

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
SCAN_SPOT = False          # ❌ فقط فیوچرز
SCAN_FUTURES = True        # ✅ فعال

# تایم‌فریم‌ها
DAILY_TF = '1d'
DAILY_LIMIT = 300
HOURLY_TF = '1h'
HOURLY_LIMIT = 300

# حداقل کندل مورد نیاز
MIN_BARS_REQUIRED = 200

# تنظیمات حجم هوشمند
VOLUME_RATIO_MIN = 1.0      # حداقل نسبت حجم

# تنظیمات مارکت‌کپ (دلار)
MIN_MARKET_CAP = 5_000_000    # 5 میلیون دلار
MAX_MARKET_CAP = 100_000_000  # 100 میلیون دلار

# تنظیمات ریسک
MIN_RISK = 0.0
MAX_RISK = 5.0

# تنظیمات RSI
RSI_PERIOD = 30
RSI_MIN = 50
RSI_MAX = 70

# ================= ENV & SECURITY =================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
CMC_API_KEY = os.getenv("CMC_PRO_API_KEY", "39478549b7c94ee093d0f3cbe43a39e9")

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
    """
    دریافت لیست جفت‌ارزهای فیوچرز فقط
    """
    symbol_map = {}

    for symbol, info in exchange_markets.items():
        if not info.get('active'):
            continue

        # فقط جفت‌ارزهای USDT
        if info.get('quote') != 'USDT':
            continue

        is_spot = info.get('spot', False)
        is_future = info.get('future', False) or info.get('swap', False)

        # ✅ فقط فیوچرز
        if SCAN_FUTURES and is_future:
            base = symbol.split('/')[0].upper()
            if base not in symbol_map:
                symbol_map[base] = (symbol, info)

    return list(symbol_map.values())

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

# ================= RSI CALCULATION =================
def calculate_rsi(series, period=14):
    """محاسبه RSI"""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

# ================= COINMARKETCAP MARKET CAP =================
def get_market_cap_from_cmc(symbol_base):
    try:
        if not CMC_API_KEY:
            return None

        url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
        params = {'symbol': symbol_base.upper(), 'convert': 'USD'}
        headers = {
            'X-CMC_PRO_API_KEY': CMC_API_KEY,
            'Accepts': 'application/json'
        }

        resp = requests.get(url, params=params, headers=headers, timeout=5)
        resp.raise_for_status()
        
        data = resp.json()
        
        if "data" in data and symbol_base.upper() in data["data"]:
            coin_data = data["data"][symbol_base.upper()]
            market_cap = coin_data.get("quote", {}).get("USD", {}).get("market_cap")
            
            if market_cap is not None and market_cap > 0:
                return float(market_cap)
        
        return None
        
    except Exception as e:
        if DEBUG_MODE:
            print(f"⚠️ CMC error for {symbol_base}: {e}")
        return None

# ================= SCAN FUNCTION =================
def scan_market(pairs):
    """
    اسکن بازار با فیلترهای جدید:
    ۱. روزانه: Price > EMA20 > EMA50
    ۲. ساعتی: Price > EMA20 > EMA50
    ۳. ساعتی: RSI بین 50 تا 70
    ۴. حجم هوشمند: avg(5h) / avg(200h) >= 1.0
    ۵. مارکت‌کپ: 5M - 100M دلار
    + ریسک: (EMA20 - EMA50) / EMA50 * 100
    """
    results = []
    total = len(pairs)

    print(f"🔍 شروع اسکن {total} نماد فیوچرز...")
    print(f"   فیلتر ۱: Price > EMA20 > EMA50 (روزانه)")
    print(f"   فیلتر ۲: Price > EMA20 > EMA50 (ساعتی)")
    print(f"   فیلتر ۳: RSI(14) بین {RSI_MIN} تا {RSI_MAX} (ساعتی)")
    print(f"   فیلتر ۴: Volume Ratio >= {VOLUME_RATIO_MIN}x")
    print(f"   فیلتر ۵: Market Cap ${MIN_MARKET_CAP/1e6:.0f}M - ${MAX_MARKET_CAP/1e6:.0f}M")
    print(f"   ریسک: (EMA20 - EMA50) / EMA50 * 100 => {MIN_RISK} تا {MAX_RISK}%")
    print("-" * 60)

    for idx, (symbol, info) in enumerate(tqdm(pairs, desc="Scanning", total=total), 1):
        try:
            # ========== فیلتر ۱: روزانه ==========
            df_daily = fetch_ohlcv(symbol, DAILY_TF, DAILY_LIMIT)
            if df_daily is None:
                continue

            df_daily['ema20'] = df_daily['close'].ewm(span=20, adjust=False).mean()
            df_daily['ema50'] = df_daily['close'].ewm(span=50, adjust=False).mean()
            last_daily = df_daily.iloc[-1]

            if pd.isna(last_daily['close']) or pd.isna(last_daily['ema20']) or pd.isna(last_daily['ema50']):
                continue

            # شرط: Price > EMA20 > EMA50 در روزانه
            if not (last_daily['close'] > last_daily['ema20'] > last_daily['ema50']):
                continue

            # ========== فیلتر ۲: ساعتی ==========
            df_hourly = fetch_ohlcv(symbol, HOURLY_TF, HOURLY_LIMIT)
            if df_hourly is None:
                continue

            df_hourly['ema20'] = df_hourly['close'].ewm(span=20, adjust=False).mean()
            df_hourly['ema50'] = df_hourly['close'].ewm(span=50, adjust=False).mean()
            last_hourly = df_hourly.iloc[-1]

            if pd.isna(last_hourly['close']) or pd.isna(last_hourly['ema20']) or pd.isna(last_hourly['ema50']):
                continue

            # شرط: Price > EMA20 > EMA50 در ساعتی
            if not (last_hourly['close'] > last_hourly['ema20'] > last_hourly['ema50']):
                continue

            # ========== فیلتر ۳: RSI بین 50 تا 70 ==========
            df_hourly['rsi'] = calculate_rsi(df_hourly['close'], RSI_PERIOD)
            last_rsi = df_hourly['rsi'].iloc[-1]
            
            if pd.isna(last_rsi) or not (RSI_MIN <= last_rsi <= RSI_MAX):
                continue

            # ========== فیلتر ۴: حجم هوشمند ==========
            avg_5h = df_hourly['volume'].iloc[-5:].mean()
            avg_200h = df_hourly['volume'].iloc[-200:].mean()
            
            if avg_200h > 0 and not np.isnan(avg_200h):
                volume_ratio = avg_5h / avg_200h
                volume_change_pct = (volume_ratio - 1) * 100
            else:
                volume_ratio = 0
                volume_change_pct = 0
            
            if volume_ratio < VOLUME_RATIO_MIN:
                continue

            # ========== فیلتر ۵: مارکت‌کپ ==========
            symbol_base = symbol.split('/')[0]
            market_cap = get_market_cap_from_cmc(symbol_base)
            
            if market_cap is None or not (MIN_MARKET_CAP <= market_cap <= MAX_MARKET_CAP):
                continue

            # ========== محاسبه ریسک ==========
            risk_pct = ((last_hourly['ema20'] - last_hourly['ema50']) / last_hourly['ema50']) * 100
            
            if not (MIN_RISK <= risk_pct <= MAX_RISK):
                continue

            # ========== ذخیره نتیجه ==========
            mkt_type = 'F'  # فقط فیوچرز

            results.append({
                'symbol': symbol,
                'symbol_base': symbol_base,
                'price': last_hourly['close'],
                'risk_pct': risk_pct,
                'v_alpha': volume_ratio,
                'volume_change_pct': volume_change_pct,
                'rsi': last_rsi,
                'market_cap': market_cap,
                'mkt_type': mkt_type,
                'info': info
            })

        except Exception as e:
            if DEBUG_MODE:
                print(f"⚠️ Error {symbol}: {e}")

        time.sleep(0.01)

    # سورت بر اساس ریسک (کم به زیاد)
    results.sort(key=lambda x: x['risk_pct'])
    return results

# ================= MESSAGE BUILDER =================
def build_card_messages(signals, total_scanned):  
    """ساخت پیام تلگرام - نمایش خطی نمادها پشت سر هم"""
    now = datetime.now().strftime('%Y/%m/%d %H:%M:%S')

    header = (
        f"🔍 <b>اسکنر XT | فیوچرز | فیلتر ترکیبی</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 اسکن شده: <code>{total_scanned}</code> | ✅ یافته: <code>{len(signals)}</code>\n"
        f"📋 فیلترها: Price>EMA20>EMA50 (1D/1H) | RSI({RSI_MIN}-{RSI_MAX}) | Vol≥{VOLUME_RATIO_MIN}x | MC ${MIN_MARKET_CAP/1e6:.0f}-{MAX_MARKET_CAP/1e6:.0f}M | Risk {MIN_RISK}-{MAX_RISK}%\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    footer = f"\n⏰ {now} 🇮🇷\n🤖 XT Futures Scanner v4.1"

    msgs = []
    body = ""
    MAX = 4000

    for r, s in enumerate(signals, 1):
        tv_symbol = s['symbol'].replace('/', '')
        tv_link = f"https://www.tradingview.com/chart/?symbol=XT:{tv_symbol}"

        # فرمت مارکت‌کپ
        if s['market_cap'] is not None:
            if s['market_cap'] >= 1e9:
                mc_str = f"${s['market_cap']/1e9:.2f}B"
            elif s['market_cap'] >= 1e6:
                mc_str = f"${s['market_cap']/1e6:.2f}M"
            else:
                mc_str = f"${s['market_cap']:,.0f}"
        else:
            mc_str = "N/A"

        # ایموجی حجم
        if s['v_alpha'] > 2.0:
            vol_emoji = "🔥"
        elif s['v_alpha'] > 1.5:
            vol_emoji = "📈"
        else:
            vol_emoji = "📊"
        
        # فرمت خطی فشرده برای هر نماد
        card = (
            f"{r}. <a href='{tv_link}'>{escape(s['symbol_base'])}</a> | "
            f"💰{s['price']:,.4f} | "
            f"📊RSI:{s['rsi']:.1f} | "
            f"⚠️{s['risk_pct']:.2f}% | "
            f"{vol_emoji}{s['v_alpha']:.2f}x | "
            f"🏛️{mc_str}\n"
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
            'disable_web_page_preview': False
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
    print("🚀 شروع اسکنر XT Futures با فیلترهای جدید...")

    pairs = get_filtered_pairs()
    print(f"📊 کل نمادهای فیوچرز فعال: {len(pairs)}")

    results = scan_market(pairs)

    print(f"\n✅ اسکن پایان یافت: {len(results)} نماد پیدا شد")

    if results:
        print("\n" + "=" * 70)
        print("🎯 نمادهای پیدا شده (مرتب شده بر اساس ریسک):")
        print("=" * 70)
        for i, r in enumerate(results, 1):
            mc_str = f"${r['market_cap']/1e6:.2f}M" if r['market_cap'] else "N/A"
            print(f"\n{i}. {r['symbol']} [{r['mkt_type']}]")
            print(f"   Price: {r['price']:,.6f} USDT")
            print(f"   📊 RSI: {r['rsi']:.1f}")
            print(f"   ⚠️ Risk: {r['risk_pct']:.2f}%")
            print(f"   📈 Vol Ratio: {r['v_alpha']:.2f}x ({r['volume_change_pct']:+.1f}%)")
            print(f"   🏛️ Market Cap: {mc_str}")
        print("=" * 70)

    if TELEGRAM_CHAT_IDS:
        print("\n📤 ارسال نتایج به تلگرام...")
        
        card_msgs = build_card_messages(results, len(pairs))
        for msg in card_msgs:
            send_telegram_message(msg)
            time.sleep(0.3)
        
        print("✅ همه پیام‌ها ارسال شدند")

# ================= RUN =================
if __name__ == "__main__":
    run()
