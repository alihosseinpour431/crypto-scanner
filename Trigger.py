
# scanner_xt.py
# ✅ اسکنر بازار کریپتو - صرافی XT
# فیلتر ۱: روزانه - EMA30 > EMA50
# فیلتر ۲: RSI > RSI_MA (با منطق Pine Script)
# فیلتر ۳: RSI بین 30 تا 70

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

# تایم‌فریم روزانه
DAILY_TF = '1d'
DAILY_LIMIT = 300

# پارامترهای RSI (مطابق کد کاربر)
RSI_LENGTH = 30
RSI_SOURCE_TYPE = "EMA"  # EMA یا Close

# Smoothing settings
MA_TYPE = "EMA"      # None / SMA / EMA / SMMA / WMA
MA_LENGTH = 50

# حداقل کندل مورد نیاز
MIN_BARS_REQUIRED = 100

# ================= ENV & SECURITY =================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

# اگر توکن تلگرام تنظیم نشده باشد، فقط در کنسول نمایش می‌دهد
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

        # بررسی اینکه آیا باید اسکن شود
        should_scan = (SCAN_SPOT and is_spot) or (SCAN_FUTURES and is_future)

        if should_scan:
            base = symbol.split('/')[0].upper()

            # اگر قبلاً ثبت نشده، یا اگر اسپات است و قبلاً فیوچرز ثبت شده بود
            if base not in symbol_map:
                symbol_map[base] = (symbol, info, is_spot)
            elif is_spot and not symbol_map[base][2]:
                # ترجیح با اسپات است
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

        return df
    except Exception as e:
        if DEBUG_MODE:
            print(f"⚠️ Fetch error {symbol}: {e}")
        return None

# ================= PINE RMA (Wilder's Smoothing) =================
def pine_rma(series, length):
    """
    محاسبه RMA مطابق با ta.rma() در Pine Script
    این همان Wilder's smoothing است
    """
    alpha = 1.0 / length
    return series.ewm(alpha=alpha, adjust=False).mean()

# ================= PINE MA =================
def pine_ma(series, length, ma_type):
    """محاسبه Moving Average مطابق با Pine Script"""
    if ma_type == "None" or ma_type is None:
        return pd.Series(np.nan, index=series.index)

    elif ma_type == "SMA":
        return series.rolling(length).mean()

    elif ma_type == "EMA":
        return series.ewm(span=length, adjust=False).mean()

    elif ma_type == "SMMA":
        return pine_rma(series, length)

    elif ma_type == "WMA":
        weights = np.arange(1, length + 1)
        return series.rolling(length).apply(
            lambda x: np.dot(x, weights) / weights.sum(),
            raw=True
        )

    else:
        raise ValueError(f"Unsupported MA type: {ma_type}")

# ================= EXACT PINE RSI =================
def calc_pine_rsi(df, rsi_length, rsi_source_type="EMA"):
    """
    محاسبه RSI دقیقاً مطابق با Pine Script:
    change = ta.change(rsiSourceInput)
    up = ta.rma(math.max(change,0), rsiLengthInput)
    down = ta.rma(-math.min(change,0), rsiLengthInput)
    """
    # تعیین منبع RSI
    if rsi_source_type == "EMA":
        rsi_source = df["close"].ewm(span=rsi_length, adjust=False).mean()
    else:
        rsi_source = df["close"]

    # محاسبه تغییرات
    change = rsi_source.diff()

    # محاسبه up و down
    up = np.maximum(change, 0.0)
    down = np.maximum(-change, 0.0)

    up = pd.Series(up, index=df.index)
    down = pd.Series(down, index=df.index)

    # اعمال RMA (Wilder's smoothing)
    avg_up = pine_rma(up, rsi_length)
    avg_down = pine_rma(down, rsi_length)

    # محاسبه RS و RSI
    rs = avg_up / avg_down

    rsi = np.where(
        avg_down == 0,
        100.0,
        np.where(avg_up == 0, 0.0, 100.0 - (100.0 / (1.0 + rs)))
    )

    df["rsi"] = pd.Series(rsi, index=df.index)

    return df

# ================= SCAN FUNCTION =================
def scan_market(pairs):
    """
    اسکن بازار با سه فیلتر:
    ۱. روزانه: EMA30 > EMA50
    ۲. RSI > RSI_MA (با منطق Pine)
    ۳. RSI بین 30 تا 70
    """
    results = []
    total = len(pairs)

    print(f"🔍 شروع اسکن {total} نماد...")
    print(f"   فیلتر ۱: EMA30 > EMA50 (روزانه)")
    print(f"   فیلتر ۲: RSI > RSI_MA ({MA_TYPE} {MA_LENGTH})")
    print(f"   فیلتر ۳: 30 < RSI < 70")
    print("-" * 50)

    for idx, (symbol, info) in enumerate(tqdm(pairs, desc="Scanning", total=total), 1):
        try:
            # دریافت داده‌های روزانه
            df = fetch_ohlcv(symbol, DAILY_TF, DAILY_LIMIT)

            if df is None:
                continue

            # محاسبه EMA30 و EMA50 قیمت
            df['ema30'] = df['close'].ewm(span=30, adjust=False).mean()
            df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()

            # محاسبه RSI با منطق Pine
            df = calc_pine_rsi(df, RSI_LENGTH, RSI_SOURCE_TYPE)

            # محاسبه RSI_MA
            df['rsi_ma'] = pine_ma(df['rsi'], MA_LENGTH, MA_TYPE)

            # گرفتن آخرین مقدار معتبر
            last = df.iloc[-1]

            # بررسی مقادیر NaN
            if pd.isna(last['close']) or pd.isna(last['ema30']) or pd.isna(last['ema50']):
                continue
            if pd.isna(last['rsi']) or pd.isna(last['rsi_ma']):
                continue

            # فیلتر ۱: EMA30 > EMA50
            if not (last['ema30'] > last['ema50']):
                continue

            # فیلتر ۲: RSI > RSI_MA
            if not (last['rsi'] > last['rsi_ma']):
                continue

            # فیلتر ۳: RSI بین 30 تا 70
            if not (30 < last['rsi'] < 70):
                continue

            # ✅ همه فیلترها پاس شدند
            mkt_type = 'F' if (info.get('future') or info.get('swap')) else 'S'

            results.append({
                'symbol': symbol,
                'price': last['close'],
                'ema30': last['ema30'],
                'ema50': last['ema50'],
                'rsi': last['rsi'],
                'rsi_ma': last['rsi_ma'],
                'mkt_type': mkt_type,
                'info': info
            })

        except Exception as e:
            if DEBUG_MODE:
                print(f"⚠️ Error {symbol}: {e}")

        # تأخیر کوتاه برای رعایت rate limit
        time.sleep(0.01)

    return results

# ================= MESSAGE BUILDER =================
def build_message(signals, total_scanned):
    """ساخت پیام تلگرام"""
    now = datetime.now().strftime('%Y/%m/%d %H:%M:%S')

    header = (
        f"🔍 <b>اسکنر XT | فیلتر ترکیبی</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 نمادهای بررسی شده: <code>{total_scanned}</code>\n"
        f"✅ عبور کرده: <code>{len(signals)}</code>\n"
        f"📋 شرایط:\n"
        f" ├─ ۱) EMA30 > EMA50 (روزانه)\n"
        f" ├─ ۲) RSI > RSI_MA ({MA_TYPE} {MA_LENGTH})\n"
        f" └─ ۳) 30 < RSI < 70\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    footer = f"\n⏰ {now} 🇮🇷\n🤖 XT Scanner v1.0"

    msgs = []
    body = ""
    MAX = 4000

    for r, s in enumerate(signals, 1):
        card = (
            f"{r}. {escape(s['symbol'])} [{s['mkt_type']}]\n"
            f"💰 Price: {s['price']:,.6f} USDT\n"
            f"📈 EMA30: {s['ema30']:,.6f}\n"
            f"📉 EMA50: {s['ema50']:,.6f}\n"
            f"📊 RSI: {s['rsi']:.2f}\n"
            f"📊 RSI_MA: {s['rsi_ma']:.2f}\n"
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
            import requests
            r = requests.post(url, json=payload, timeout=30)
            if DEBUG_MODE and r.status_code != 200:
                print(f"⚠️ Telegram ({cid}): {r.status_code} | {r.text[:100]}")
        except Exception as e:
            print(f"❌ Telegram Error ({cid}): {e}")

# ================= MAIN =================
def run():
    """تابع اصلی اجرا"""
    print("🚀 شروع اسکنر XT...")

    # دریافت لیست جفت‌ارزها
    pairs = get_filtered_pairs()
    print(f"📊 کل نمادهای فعال (بدون تکرار): {len(pairs)}")

    # اسکن بازار
    results = scan_market(pairs)

    print(f"\n✅ اسکن پایان یافت: {len(results)} نماد پیدا شد")

    # نمایش نتایج در کنسول
    if results:
        print("\n" + "=" * 60)
        print("🎯 نمادهای پیدا شده:")
        print("=" * 60)
        for i, r in enumerate(results, 1):
            print(f"\n{i}. {r['symbol']} [{r['mkt_type']}]")
            print(f"   Price: {r['price']:,.6f}")
            print(f"   EMA30: {r['ema30']:,.6f} | EMA50: {r['ema50']:,.6f}")
            print(f"   RSI: {r['rsi']:.2f} | RSI_MA: {r['rsi_ma']:.2f}")
        print("=" * 60)

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
