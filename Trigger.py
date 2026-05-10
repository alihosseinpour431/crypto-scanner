# ✅ اسکنر بازار کریپتو - صرافی XT با فیلترهای ترکیبی روزانه + ساعتی
# فیلتر روزانه: Close > EMA50 و RSI(50) > 50
# فیلتر ساعتی: Close > EMA50 و RSI(50) > 50 (پس از قبولی روزانه)

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

# ================= ENV & SECURITY =================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
CMC_API_KEY = os.getenv("CMC_PRO_API_KEY", "39478549b7c94ee093d0f3cbe43a39")

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
    دریافت لیست جفت‌ارزها بدون تکرار
    اگر ارزی هم اسپات دارد هم فیوچرز، فقط اسپات را برمی‌گرداند
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

# ================= RSI CALCULATOR =================
def calculate_rsi(series, period=50):
    """
    محاسبه RSI با دوره دلخواه (پیش‌فرض: ۵۰)
    """
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
        params = {
            'symbol': symbol_base.upper(),
            'convert': 'USD'
        }
      
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
    اسکن بازار با دو سطح فیلتر:
    ۱. روزانه: Close > EMA50 و RSI(50) > 50
    ۲. ساعتی: Close > EMA50 و RSI(50) > 50 (در صورت قبولی روزانه)
    """
    results = []
    total = len(pairs)

    print(f"🔍 شروع اسکن {total} نماد...")
    print(f"   📅 فیلتر روزانه: Close > EMA50  و  RSI(50) > 50")
    print(f"   🕐 فیلتر ساعتی: Close > EMA50  و  RSI(50) > 50 (پس از قبولی روزانه)")
    print("-" * 50)

    for idx, (symbol, info) in enumerate(tqdm(pairs, desc="Scanning", total=total), 1):
        try:
            # ========== مرحله ۱: فیلتر روزانه ==========
            df_daily = fetch_ohlcv(symbol, DAILY_TF, DAILY_LIMIT)
            if df_daily is None:
                continue

            # محاسبه EMA50 و RSI50 برای روزانه
            df_daily['ema50'] = df_daily['close'].ewm(span=50, adjust=False).mean()
            df_daily['rsi_50'] = calculate_rsi(df_daily['close'], period=50)

            last_daily = df_daily.iloc[-1]
            if pd.isna(last_daily['close']) or pd.isna(last_daily['ema50']) or pd.isna(last_daily['rsi_50']):
                continue

            # شرط روزانه: Close > EMA50  و  RSI > 50
            if not (last_daily['close'] > last_daily['ema50'] and last_daily['rsi_50'] > 50):
                continue

            # ========== مرحله ۲: فیلتر ساعتی ==========
            df_hourly = fetch_ohlcv(symbol, HOURLY_TF, HOURLY_LIMIT)
            if df_hourly is None:
                continue

            # محاسبه EMA50 و RSI50 برای ساعتی
            df_hourly['ema50'] = df_hourly['close'].ewm(span=50, adjust=False).mean()
            df_hourly['rsi_50'] = calculate_rsi(df_hourly['close'], period=50)

            last_hourly = df_hourly.iloc[-1]
            if pd.isna(last_hourly['close']) or pd.isna(last_hourly['ema50']) or pd.isna(last_hourly['rsi_50']):
                continue

            # شرط ساعتی: Close > EMA50  و  RSI > 50
            if not (last_hourly['close'] > last_hourly['ema50'] and last_hourly['rsi_50'] > 50):
                continue

            # ========== محاسبه نسبت حجم (ساعتی) ==========
            avg_5h = df_hourly['volume'].iloc[-5:].mean()
            avg_50h = df_hourly['volume'].iloc[-50:].mean()
            
            if avg_50h > 0 and not np.isnan(avg_50h):
                volume_ratio = avg_5h / avg_50h
            else:
                volume_ratio = 0
            
            v_alpha = volume_ratio

            # ========== دریافت مارکت کپ ==========
            symbol_base = symbol.split('/')[0]
            market_cap = get_market_cap_from_cmc(symbol_base)

            mkt_type = 'F' if (info.get('future') or info.get('swap')) else 'S'

            # محاسبه درصد فاصله قیمت از EMA50 در هر دو تایم‌فریم
            daily_dist_pct = ((last_daily['close'] - last_daily['ema50']) / last_daily['ema50']) * 100
            hourly_dist_pct = ((last_hourly['close'] - last_hourly['ema50']) / last_hourly['ema50']) * 100

            results.append({
                'symbol': symbol,
                'symbol_base': symbol_base,
                'price': last_hourly['close'],
                'daily_ema50': last_daily['ema50'],
                'daily_rsi': last_daily['rsi_50'],
                'daily_dist_pct': daily_dist_pct,
                'hourly_ema50': last_hourly['ema50'],
                'hourly_rsi': last_hourly['rsi_50'],
                'hourly_dist_pct': hourly_dist_pct,
                'v_alpha': v_alpha,
                'market_cap': market_cap,
                'mkt_type': mkt_type,
                'info': info
            })

        except Exception as e:
            if DEBUG_MODE:
                print(f"⚠️ Error {symbol}: {e}")

        time.sleep(0.01)

    # مرتب‌سازی بر اساس RSI ساعتی (نزولی)
    results.sort(key=lambda x: x['hourly_rsi'], reverse=True)

    return results

# ================= MESSAGE BUILDER =================
def build_card_messages(signals, total_scanned):
    """ساخت پیام تلگرام"""
    now = datetime.now().strftime('%Y/%m/%d %H:%M:%S')

    header = (
        f"🔍 <b>اسکنر XT | فیلتر ترکیبی روزانه + ساعتی</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 نمادهای بررسی شده: <code>{total_scanned}</code>\n"
        f"✅ عبور کرده: <code>{len(signals)}</code>\n"
        f"📋 شرایط:\n"
        f" ├─ 📅 روزانه: Close > EMA50  و  RSI(50) > 50\n"
        f" └─ 🕐 ساعتی: Close > EMA50  و  RSI(50) > 50\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    footer = f"\n⏰ {now} 🇮🇷\n🤖 XT Scanner v5.0"

    msgs = []
    body = ""
    MAX = 4000

    for r, s in enumerate(signals, 1):
        tv_symbol = s['symbol'].replace('/', '')
        tv_link = f"https://www.tradingview.com/chart/?symbol=:{tv_symbol}"

        # فرمت مارکت کپ
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
        if s['v_alpha'] > 1.5:
            vol_emoji = "🔥"
            vol_text = f"{s['v_alpha']:.2f}x"
        elif s['v_alpha'] > 1.0:
            vol_emoji = "📈"
            vol_text = f"{s['v_alpha']:.2f}x"
        else:
            vol_emoji = "📉"
            vol_text = f"{s['v_alpha']:.2f}x"

        card = (
            f"{r}. <a href='{tv_link}'>{escape(s['symbol'])}</a> [{s['mkt_type']}]\n"
            f"💰 Price: {s['price']:,.6f} USDT\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 <b>Daily</b>\n"
            f"   ├─ EMA50: {s['daily_ema50']:,.6f}\n"
            f"   ├─ Close > EMA50: +{s['daily_dist_pct']:.2f}%\n"
            f"   └─ RSI(50): {s['daily_rsi']:.2f}\n"
            f"🕐 <b>Hourly</b>\n"
            f"   ├─ EMA50: {s['hourly_ema50']:,.6f}\n"
            f"   ├─ Close > EMA50: +{s['hourly_dist_pct']:.2f}%\n"
            f"   └─ RSI(50): <b>{s['hourly_rsi']:.2f}</b>\n"
            f"{vol_emoji} Vol Ratio: <b>{vol_text}</b>\n"
            f"🏛️ Market Cap: {mc_str}\n"
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
    print("🚀 شروع اسکنر XT با فیلترهای روزانه + ساعتی (قیمت > EMA50 و RSI > 50)...")

    pairs = get_filtered_pairs()
    print(f"📊 کل نمادهای فعال (بدون تکرار): {len(pairs)}")

    results = scan_market(pairs)

    print(f"\n✅ اسکن پایان یافت: {len(results)} نماد پیدا شد")

    # نمایش در کنسول
    if results:
        print("\n" + "=" * 70)
        print("🎯 نمادهای پیدا شده (مرتب شده بر اساس RSI ساعتی نزولی):")
        print("=" * 70)
        for i, r in enumerate(results, 1):
            mc_str = f"${r['market_cap']:,.0f}" if r['market_cap'] else "N/A"
            print(f"\n{i}. {r['symbol']} [{r['mkt_type']}]")
            print(f"   💰 Price: {r['price']:,.6f}")
            print(f"   📅 Daily: Close>EMA50 +{r['daily_dist_pct']:.2f}% | RSI={r['daily_rsi']:.2f}")
            print(f"   🕐 Hourly: Close>EMA50 +{r['hourly_dist_pct']:.2f}% | RSI={r['hourly_rsi']:.2f}")
            print(f"   📊 V_alpha: {r['v_alpha']:.2f} | 🏛️ MCap: {mc_str}")
        print("=" * 70)

    # ارسال به تلگرام
    if TELEGRAM_CHAT_IDS:
        print("\n📤 ارسال نتایج به تلگرام...")
        card_msgs = build_card_messages(results, len(pairs))
        for msg in card_msgs:
            send_telegram_message(msg)
            time.sleep(0.3)

# ================= RUN =================
if __name__ == "__main__":
    run()
