# ✅ Crypto Market Scanner - XT Exchange (Futures Only)
# Filters: Daily/Hourly EMA Trend | RSI 50-70 (Wilder's RMA) | Smart Volume | MarketCap 5M-300M | Risk Calc
# Telegram Output: Vertical Card Style (Each metric on separate line)
# RSI Calculation: Wilder's RMA (100% match with TradingView)

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
VOLUME_RATIO_MIN = 1.0

# تنظیمات مارکت‌کپ (دلار) - 5M تا 300M
MIN_MARKET_CAP = 5_000_000
MAX_MARKET_CAP = 300_000_000

# تنظیمات ریسک
MIN_RISK = 0.0
MAX_RISK = 20.0

# تنظیمات RSI (هماهنگ با تریدینگ‌ویو)
RSI_PERIOD = 30
RSI_MIN = 50
RSI_MAX = 70

# ================= ENV & SECURITY =================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
CMC_API_KEY = os.getenv("CMC_PRO_API_KEY", "39478549b7c94ee093d0f3cbe43a39e9")

if not TELEGRAM_BOT_TOKEN:
    print("⚠️ TELEGRAM_BOT_TOKEN not set. Running in console-only mode.")

# ================= EXCHANGE INIT =================
try:
    exchange = getattr(ccxt, EXCHANGE_ID)({
        'enableRateLimit': True,
        'timeout': 30000
    })
    exchange_markets = exchange.load_markets()
    print(f"✅ Connected to {EXCHANGE_ID.upper()}")
except Exception as e:
    print(f"❌ Exchange init error: {e}")
    exit(1)

# ================= GET FUTURES PAIRS =================
def get_filtered_pairs():
    """دریافت لیست جفت‌ارزهای فیوچرز فعال با USDT"""
    symbol_map = {}
    for symbol, info in exchange_markets.items():
        if not info.get('active'):
            continue
        if info.get('quote') != 'USDT':
            continue
        is_future = info.get('future', False) or info.get('swap', False)
        if SCAN_FUTURES and is_future:
            base = symbol.split('/')[0].upper()
            if base not in symbol_map:
                symbol_map[base] = (symbol, info)
    return list(symbol_map.values())

# ================= FETCH OHLCV =================
def fetch_ohlcv(symbol, timeframe, limit):
    """دریافت داده‌های کندل‌ها"""
    try:
        data = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if len(data) < MIN_BARS_REQUIRED:
            return None
        df = pd.DataFrame(data, columns=['timestamp','open','high','low','close','volume'])
        df['close'] = df['close'].astype(float)
        df['volume'] = df['volume'].astype(float)
        return df
    except Exception as e:
        if DEBUG_MODE:
            print(f"⚠️ Fetch {symbol}: {e}")
        return None

# ================= 🎯 WILDER'S RMA (مطابق تریدینگ‌ویو) =================
def wilders_rma(series, period):
    """
    محاسبه Wilder's Rolling Moving Average (SMMA)
    دقیقاً مشابه ta.rma() در تریدینگ‌ویو
    
    فرمول: RMA[today] = (RMA[prev] × (period-1) + value[today]) ÷ period
    """
    if len(series) < period:
        return pd.Series([np.nan] * len(series), index=series.index)
    
    result = []
    # شروع با میانگین ساده برای period اول
    rma = series.iloc[:period].mean()
    result.extend([np.nan] * (period - 1))  # مقادیر اولیه NaN
    result.append(rma)
    
    # اعمال فرمول وایلدر برای بقیه داده‌ها
    for i in range(period, len(series)):
        rma = (rma * (period - 1) + series.iloc[i]) / period
        result.append(rma)
    
    return pd.Series(result, index=series.index)

# ================= 🎯 RSI CALCULATION (Wilder's Method) =================
def calculate_rsi(series, period=14):
    """
    محاسبه RSI با فرمول استاندارد وایلدر (100% هماهنگ با تریدینگ‌ویو)
    
    مراحل:
    1. محاسبه تغییرات قیمت
    2. جدا کردن سود و زیان
    3. هموارسازی با Wilder's RMA (نه EMA!)
    4. محاسبه RS و تبدیل به RSI
    """
    delta = series.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    
    # ✅ استفاده از Wilder's RMA (مطابق تریدینگ‌ویو)
    avg_gain = wilders_rma(gain, period)
    avg_loss = wilders_rma(loss, period)
    
    # محاسبه RS با مدیریت خطای تقسیم بر صفر
    rs = pd.Series(np.zeros(len(series)), index=series.index)
    for i in range(len(series)):
        if avg_loss.iloc[i] == 0:
            rs.iloc[i] = 100  # RSI = 100 وقتی loss = 0
        elif avg_gain.iloc[i] == 0:
            rs.iloc[i] = 0    # RSI = 0 وقتی gain = 0
        else:
            rs.iloc[i] = avg_gain.iloc[i] / avg_loss.iloc[i]
    
    rsi = 100 - (100 / (1 + rs))
    return rsi

# ================= MARKET CAP FROM CMC =================
def get_market_cap_from_cmc(symbol_base):
    """دریافت مارکت‌کپ از CoinMarketCap API"""
    try:
        if not CMC_API_KEY:
            return None
        url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
        headers = {'X-CMC_PRO_API_KEY': CMC_API_KEY, 'Accepts': 'application/json'}
        params = {'symbol': symbol_base.upper(), 'convert': 'USD'}
        resp = requests.get(url, params=params, headers=headers, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if "data" in data and symbol_base.upper() in data["data"]:
            mc = data["data"][symbol_base.upper()].get("quote",{}).get("USD",{}).get("market_cap")
            return float(mc) if mc and mc > 0 else None
    except Exception as e:
        if DEBUG_MODE:
            print(f"⚠️ CMC {symbol_base}: {e}")
    return None

# ================= SCAN MARKET =================
def scan_market(pairs):
    """اسکن بازار با اعمال تمام فیلترها"""
    results = []
    total = len(pairs)
    
    print(f"🔍 Scanning {total} futures")
    print(f"🎯 Filters: EMA↑(1D/1H) | RSI({RSI_MIN}-{RSI_MAX}) [Wilder] | Vol≥{VOLUME_RATIO_MIN}x | MC ${MIN_MARKET_CAP/1e6:.0f}-{MAX_MARKET_CAP/1e6:.0f}M | Risk {MIN_RISK}-{MAX_RISK}%")
    print("─" * 70)

    for symbol, info in tqdm(pairs, desc="Scanning"):
        try:
            # 📅 فیلتر ۱: روند روزانه (Price > EMA20 > EMA50)
            df_d = fetch_ohlcv(symbol, DAILY_TF, DAILY_LIMIT)
            if df_d is None:
                continue
            df_d['ema20'] = df_d['close'].ewm(span=20, adjust=False).mean()
            df_d['ema50'] = df_d['close'].ewm(span=50, adjust=False).mean()
            last_d = df_d.iloc[-1]
            if pd.isna(last_d['close']) or pd.isna(last_d['ema20']) or pd.isna(last_d['ema50']):
                continue
            if not (last_d['close'] > last_d['ema20'] > last_d['ema50']):
                continue

            # ⏰ فیلتر ۲: روند ساعتی (Price > EMA20 > EMA50)
            df_h = fetch_ohlcv(symbol, HOURLY_TF, HOURLY_LIMIT)
            if df_h is None:
                continue
            df_h['ema20'] = df_h['close'].ewm(span=20, adjust=False).mean()
            df_h['ema50'] = df_h['close'].ewm(span=50, adjust=False).mean()
            last_h = df_h.iloc[-1]
            if pd.isna(last_h['close']) or pd.isna(last_h['ema20']) or pd.isna(last_h['ema50']):
                continue
            if not (last_h['close'] > last_h['ema20'] > last_h['ema50']):
                continue

            # 📊 فیلتر ۳: RSI بین 50 تا 70 (با Wilder's RMA)
            df_h['rsi'] = calculate_rsi(df_h['close'], RSI_PERIOD)
            rsi_val = df_h['rsi'].iloc[-1]
            if pd.isna(rsi_val) or not (RSI_MIN <= rsi_val <= RSI_MAX):
                continue

            # 📈 فیلتر ۴: حجم هوشمند (مقایسه ۵ ساعت اخیر با ۲۰۰ ساعت)
            vol_5h = df_h['volume'].iloc[-5:].mean()
            vol_200h = df_h['volume'].iloc[-200:].mean()
            vol_ratio = vol_5h / vol_200h if vol_200h > 0 and not np.isnan(vol_200h) else 0
            if vol_ratio < VOLUME_RATIO_MIN:
                continue

            # 🏛️ فیلتر ۵: مارکت‌کپ بین 5M تا 300M دلار
            base = symbol.split('/')[0]
            mc = get_market_cap_from_cmc(base)
            if mc is None or not (MIN_MARKET_CAP <= mc <= MAX_MARKET_CAP):
                continue

            # ⚠️ محاسبه ریسک: فاصله درصدی EMA20 و EMA50
            risk = ((last_h['ema20'] - last_h['ema50']) / last_h['ema50']) * 100
            if not (MIN_RISK <= risk <= MAX_RISK):
                continue

            # ✅ ذخیره نتیجه نهایی
            results.append({
                'symbol': symbol,
                'base': base,
                'price': last_h['close'],
                'risk': risk,
                'vol_ratio': vol_ratio,
                'rsi': rsi_val,
                'mc': mc,
                'info': info
            })
        except Exception as e:
            if DEBUG_MODE:
                print(f"⚠️ {symbol}: {e}")
        time.sleep(0.01)  # جلوگیری از ریت‌لیمیت

    # مرتب‌سازی بر اساس ریسک (کم به زیاد)
    return sorted(results, key=lambda x: x['risk'])

# ================= 🎨 TELEGRAM MESSAGE BUILDER (VERTICAL CARD STYLE) =================
def build_telegram_messages(signals, total_scanned):
    """ساخت پیام تلگرام با استایل کارت عمودی - هر متریک در خط جدا"""
    now = datetime.now().strftime('%Y/%m/%d %H:%M')
    
    # هدر پیام
    header = (
        f"🔍 <b>XT Futures Scanner</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Scanned: <code>{total_scanned}</code> | ✅ Found: <code>{len(signals)}</code>\n"
        f"🎯 EMA↑(1D/1H) | RSI({RSI_MIN}-{RSI_MAX}) [Wilder] | Vol≥{VOLUME_RATIO_MIN}x | MC ${MIN_MARKET_CAP/1e6:.0f}-{MAX_MARKET_CAP/1e6:.0f}M\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
    )
    
    # فوتر پیام
    footer = f"\n⏰ {now} 🇮🇷  |  🤖 XT Scan v4.4 [Wilder-RMA]"

    msgs, body, MAX_LEN = [], "", 4000

    for i, s in enumerate(signals, 1):
        # 🔗 لینک مستقیم به تریدینگ‌ویو
        tv_symbol = s['symbol'].replace('/', '')
        tv_link = f"https://www.tradingview.com/chart/?symbol=XT:{tv_symbol}"
        
        # 💰 فرمت‌دهی قیمت (هوشمند بر اساس مقدار)
        price = f"{s['price']:,.4f}" if s['price'] < 1000 else f"{s['price']:,.2f}"
        
        # 🏛️ فرمت‌دهی مارکت‌کپ (K/M/B)
        mc = s['mc']
        if mc >= 1e9:
            mc_str = f"{mc/1e9:.2f}B"
        elif mc >= 1e6:
            mc_str = f"{mc/1e6:.1f}M"
        else:
            mc_str = f"{mc/1e3:.0f}K"
        
        # 📈 ایموجی وضعیت حجم
        if s['vol_ratio'] > 2.0:
            vol_emoji = "🔥"
        elif s['vol_ratio'] > 1.5:
            vol_emoji = "📈"
        else:
            vol_emoji = "📊"
        
        # ⚠️ رنگ‌بندی ریسک بر اساس مقدار
        risk_val = s['risk']
        if risk_val < 5:
            risk_color = "🟢"      # ریسک کم
        elif risk_val < 12:
            risk_color = "🟡"      # ریسک متوسط
        else:
            risk_color = "🟠"      # ریسک بالا
        
        # 🎴 فرمت کارت عمودی - هر فیلد در خط مجزا
        card = (
            f"▫️ <a href='{tv_link}'><b>#{i} {escape(s['base'])}</b></a>\n"
            f"   💰 <code>Price: {price} USDT</code>\n"
            f"   📊 <code>RSI: {s['rsi']:.1f}</code>\n"
            f"   ⚠️  <code>Risk: {risk_color} {risk_val:.1f}%</code>\n"
            f"   📈 <code>Volume: {vol_emoji} {s['vol_ratio']:.2f}x</code>\n"
            f"   🏛️ <code>MC: ${mc_str}</code>\n"
            f"   ──────────────\n"
        )
        
        # مدیریت طول پیام (تقسیم خودکار اگر بیش از حد طولانی شود)
        if len(header) + len(body) + len(card) + len(footer) > MAX_LEN - 100:
            msgs.append(header + body + footer)
            body = card
        else:
            body += card

    # اضافه کردن بخش باقی‌مانده
    if body.strip():
        msgs.append(header + body + footer)
    
    # اگر هیچ سیگنالی پیدا نشد
    if not msgs:
        msgs.append(f"{header}❌ <i>No signals found matching criteria.</i>{footer}")
    
    return msgs

# ================= SEND TELEGRAM =================
def send_telegram_message(text, chat_id=None):
    """ارسال پیام به تلگرام با فرمت HTML"""
    if not TELEGRAM_BOT_TOKEN:
        return
    
    targets = [chat_id] if chat_id else TELEGRAM_CHAT_IDS
    
    for cid in targets:
        cid = cid.strip()
        if not cid:
            continue
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                'chat_id': cid,
                'text': text,
                'parse_mode': 'HTML',
                'disable_web_page_preview': False
            }
            resp = requests.post(url, json=payload, timeout=30)
            if DEBUG_MODE and resp.status_code != 200:
                print(f"⚠️ TG ({cid}): {resp.status_code} | {resp.text[:100]}")
        except Exception as e:
            print(f"❌ Telegram Error: {e}")

# ================= MAIN RUN =================
def run():
    """تابع اصلی اجرای اسکنر"""
    print("🚀 XT Futures Scanner Starting...")
    
    # دریافت لیست جفت‌ارزها
    pairs = get_filtered_pairs()
    print(f"📦 Active USDT Futures: {len(pairs)}")
    
    # اجرای اسکن
    results = scan_market(pairs)
    print(f"\n✅ Scan Complete! Found: {len(results)} signals")
    
    # نمایش نتایج در کنسول
    if results:
        print("\n" + "═" * 70)
        print("🎯 Results (Sorted by Risk - Low to High):")
        print("═" * 70)
        for i, r in enumerate(results, 1):
            mc_str = f"${r['mc']/1e6:.2f}M" if r['mc'] else "N/A"
            print(f"{i}. {r['symbol']}")
            print(f"   💰 Price: {r['price']:,.4f} USDT")
            print(f"   📊 RSI: {r['rsi']:.1f} [Wilder]")
            print(f"   ⚠️  Risk: {r['risk']:.2f}%")
            print(f"   📈 Volume Ratio: {r['vol_ratio']:.2f}x")
            print(f"   🏛️ Market Cap: {mc_str}")
            print("   ──────────────")
        print("═" * 70)
    
    # ارسال به تلگرام
    if TELEGRAM_CHAT_IDS:
        print("\n📤 Sending results to Telegram...")
        messages = build_telegram_messages(results, len(pairs))
        for msg in messages:
            send_telegram_message(msg)
            time.sleep(0.3)  # فاصله بین پیام‌ها
        print("✅ All messages sent successfully!")

# ================= ENTRY POINT =================
if __name__ == "__main__":
    run()
