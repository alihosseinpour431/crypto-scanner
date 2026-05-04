# Trigger.py
# ✅ نسخه نهایی: سه فیلتر متوالی + محاسبه Risk%
# فیلتر ۱ (روزانه): EMA30 >= EMA50
# فیلتر ۲ (ساعتی): RSI(30) > RSI_MA(50)
# فیلتر ۳ (ساعتی): RSI_MA بین 30 تا 70
# ریسک: (Price - EMA200) / EMA200 * 100
# سورت: صعودی بر اساس Risk%

import os
import time
import requests
import ccxt
import pandas as pd
import pytz
import jdatetime
import numpy as np
from html import escape
from tqdm.auto import tqdm

# ================= CONFIG =================
EXCHANGE_ID = 'xt'
SCAN_SPOT = True
SCAN_FUTURES = True

# تایم‌فریم‌ها
DAILY_TF = '1d'
DAILY_LIMIT = 100
HOURLY_TF = '1h'
HOURLY_LIMIT = 300
MIN_BARS_REQUIRED = 250

# پارامترهای اندیکاتور
RSI_LENGTH = 30
RSI_MA_LENGTH = 50

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
    raise SystemExit(1)

# ================= CACHE =================
market_cap_cache = {}

# ================= TELEGRAM =================
def send_telegram_message(text, chat_id=None):
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
                print(f"⚠️ Telegram ({cid}): {r.status_code} | {r.text[:120]}")
        except Exception as e:
            print(f"❌ Telegram Error ({cid}): {e}")

# ================= COINGECKO =================
def load_market_caps():
    if market_cap_cache:
        return

    print("📥 Loading Market Cap from CoinGecko ...")
    session = requests.Session()

    for page in range(1, 11):
        try:
            url = (
                f"{COINGECKO_API}/coins/markets?"
                f"vs_currency=usd&order=market_cap_desc&per_page=250&page={page}&sparkline=false"
            )
            r = session.get(url, timeout=20)
            data = r.json()

            if not isinstance(data, list) or len(data) == 0:
                break

            for coin in data:
                sym = str(coin.get('symbol', '')).upper().strip()
                mc = coin.get('market_cap')
                if sym and isinstance(mc, (int, float)):
                    market_cap_cache[sym] = mc

            if len(data) < 250:
                break

            time.sleep(0.1)

        except Exception as e:
            if DEBUG_MODE:
                print(f"⚠️ CoinGecko error on page {page}: {e}")
            break

    print(f"✅ Market Cap loaded: {len(market_cap_cache)} symbols")

def get_market_cap(symbol):
    base = symbol.split('/')[0].upper()
    return market_cap_cache.get(base)

def fmt_mc(value):
    if value is None or pd.isna(value):
        return "🔸 N/A"
    v = float(value)
    if v >= 1e12:
        return f"💎 ${v/1e12:.2f}T"
    if v >= 1e9:
        return f"💎 ${v/1e9:.2f}B"
    if v >= 1e6:
        return f"💎 ${v/1e6:.2f}M"
    return f"💎 ${v:,.0f}"

# ================= DEDUPLICATION =================
def get_filtered_pairs():
    symbol_map = {}
    for symbol, info in exchange_markets.items():
        if not info.get('active') or info.get('quote') != 'USDT':
            continue

        is_spot = info.get('spot', False)
        is_future = info.get('future', False) or info.get('swap', False)

        if (SCAN_SPOT and is_spot) or (SCAN_FUTURES and is_future):
            base = symbol.split('/')[0].upper()

            # اگر برای یک base چند مارکت داریم، spot را ترجیح بده
            if base not in symbol_map:
                symbol_map[base] = (symbol, info, is_spot)
            elif is_spot and not symbol_map[base][2]:
                symbol_map[base] = (symbol, info, True)

    return [(sym, inf) for sym, inf, _ in symbol_map.values()]

# ================= DATA & INDICATORS =================
def fetch_ohlcv(symbol, timeframe, limit):
    try:
        data = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if len(data) < MIN_BARS_REQUIRED:
            return None
        df = pd.DataFrame(data, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
        return df
    except Exception as e:
        if DEBUG_MODE:
            print(f"⚠️ Fetch error {symbol}: {e}")
        return None

def calc_daily_ema(df):
    """محاسبه EMA30 و EMA50 برای فیلتر روزانه"""
    df = df.copy()
    df['ema30'] = df['c'].ewm(span=30, adjust=False).mean()
    df['ema50'] = df['c'].ewm(span=50, adjust=False).mean()
    return df

def calc_hourly_indicators(df):
    """
    محاسبه تمام اندیکاتورهای ساعتی:
    - RSI(30)
    - RSI_MA(50)
    - EMA200 (برای محاسبه ریسک)
    """
    df = df.copy()

    # RSI با طول 30
    delta = df['c'].diff()
    gain = delta.where(delta > 0, 0).rolling(RSI_LENGTH).mean()
    loss = -delta.where(delta < 0, 0).rolling(RSI_LENGTH).mean()
    rs = gain / loss.replace(0, np.nan)
    df['rsi'] = 100 - (100 / (1 + rs))
    df['rsi'] = df['rsi'].fillna(100)

    # EMA روی RSI
    df['rsi_ma'] = df['rsi'].ewm(span=RSI_MA_LENGTH, adjust=False).mean()

    # EMA200 روی قیمت
    df['ema200'] = df['c'].ewm(span=200, adjust=False).mean()

    return df

# ================= MESSAGE HELPERS =================
def chunk_message(header, lines, footer="", max_len=4000):
    """
    یک لیست از خطوط را به پیام‌های چندتکه تبدیل می‌کند تا از محدودیت تلگرام رد نشود.
    """
    if not lines:
        return [header + "❌ هیچ نمادی این مرحله را پاس نکرد.\n" + footer]

    messages = []
    current = header

    for line in lines:
        if len(current) + len(line) + len(footer) > max_len:
            messages.append(current + footer)
            current = header + line
        else:
            current += line

    if current.strip():
        messages.append(current + footer)

    return messages

def render_filter1_item(idx, item):
    return (
        f"{idx}. {escape(item['symbol'])} [{item['mkt_type']}]\n"
        f"💰 {item['price']:,.4f} USDT\n"
        f"📅 Daily EMA30: {item['daily_ema30']:,.4f}\n"
        f"📅 Daily EMA50: {item['daily_ema50']:,.4f}\n"
        f"📈 EMA30-EMA50: {item['daily_ema30'] - item['daily_ema50']:+,.4f}\n"
        f"💎 MC: {fmt_mc(item['mc'])}\n"
        f"─────────────────────\n"
    )

def render_filter2_item(idx, item):
    return (
        f"{idx}. {escape(item['symbol'])} [{item['mkt_type']}]\n"
        f"💰 {item['price']:,.4f} USDT\n"
        f"📊 RSI(30): {item['rsi']:.2f} | RSI_MA(50): {item['rsi_ma']:.2f}\n"
        f"📈 RSI Diff: {item['rsi_diff']:+.2f}\n"
        f"📅 Daily EMA30: {item['daily_ema30']:,.4f}\n"
        f"📅 Daily EMA50: {item['daily_ema50']:,.4f}\n"
        f"💎 MC: {fmt_mc(item['mc'])}\n"
        f"─────────────────────\n"
    )

def render_final_item(idx, item):
    risk_color = "🟢" if item['risk_pct'] < 10 else "🟡" if item['risk_pct'] < 30 else "🔴"
    return (
        f"{idx}. {escape(item['symbol'])} [{item['mkt_type']}]\n"
        f"💰 {item['price']:,.4f} USDT\n"
        f"📊 RSI(30): {item['rsi']:.2f} | RSI_MA(50): {item['rsi_ma']:.2f}\n"
        f"📈 RSI Diff: {item['rsi_diff']:+.2f}\n"
        f"📅 Daily EMA30: {item['daily_ema30']:,.4f}\n"
        f"📅 Daily EMA50: {item['daily_ema50']:,.4f}\n"
        f" EMA200: {item['ema200']:,.4f}\n"
        f"{risk_color} Risk: {item['risk_pct']:+.2f}%\n"
        f"💎 MC: {fmt_mc(item['mc'])}\n"
        f"─────────────────────\n"
    )

def build_summary_message(total_pairs, passed_f1, passed_f2, passed_f3):
    now = jdatetime.datetime.now(pytz.timezone('Asia/Tehran')).strftime('%Y/%m/%d %H:%M:%S')
    return (
        f"🚀 <b>گزارش اسکن سه مرحله‌ای + Risk%</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 کل نمادها: <code>{total_pairs}</code>\n"
        f"✅ عبور از فیلتر ۱: <code>{passed_f1}</code>\n"
        f"✅ عبور از فیلتر ۲: <code>{passed_f2}</code>\n"
        f"✅ عبور از فیلتر ۳: <code>{passed_f3}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 شرایط:\n"
        f" ├─ ۱. روزانه: EMA30 >= EMA50\n"
        f" ├─ ۲. ساعتی: RSI(30) > EMA(50) روی RSI\n"
        f" ├─ ۳. ساعتی: RSI_MA بین 30 تا 70\n"
        f" └─ 📊 مرتب شده بر اساس Risk% (کم به زیاد)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {now} 🇮🇷\n"
        f"🤖 AlphaScanner v5.0\n"
    )

def build_stage_messages(stage_title, stage_note, items, renderer, total_pairs, passed_count):
    now = jdatetime.datetime.now(pytz.timezone('Asia/Tehran')).strftime('%Y/%m/%d %H:%M:%S')
    header = (
        f"📌 <b>{stage_title}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 کل نمادها: <code>{total_pairs}</code>\n"
        f"✅ عبور کرده‌ها: <code>{passed_count}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 {stage_note}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    footer = f"\n⏰ {now} 🇮🇷\n🤖 AlphaScanner v5.0"
    lines = [renderer(i, item) for i, item in enumerate(items, 1)]
    return chunk_message(header, lines, footer)

# ================= MAIN SCAN LOGIC =================
def run_scan(pairs):
    results = []
    passed_filter1_items = []
    passed_filter2_items = []

    total = len(pairs)
    passed_filter1 = 0
    passed_filter2 = 0

    print("🔍 شروع اسکن سه مرحله‌ای...")
    print(f"📊 کل نمادها: {total}")

    for idx, (symbol, info) in enumerate(tqdm(pairs, desc="Scanning", total=total), 1):
        try:
            # ================= فیلتر ۱: روزانه =================
            df_daily = fetch_ohlcv(symbol, DAILY_TF, DAILY_LIMIT)
            if df_daily is None:
                continue

            df_daily = calc_daily_ema(df_daily)
            last_daily = df_daily.iloc[-1]

            if not (pd.notna(last_daily['ema30']) and pd.notna(last_daily['ema50'])):
                continue

            if last_daily['ema30'] < last_daily['ema50']:
                continue  # ❌ فیلتر ۱ رد شد

            passed_filter1 += 1

            mkt = 'F' if (info.get('future') or info.get('swap')) else 'S'

            # ثبت عبور از فیلتر ۱
            passed_filter1_items.append({
                'symbol': symbol,
                'price': last_daily['c'],
                'daily_ema30': last_daily['ema30'],
                'daily_ema50': last_daily['ema50'],
                'mkt_type': mkt,
                'mc': get_market_cap(symbol)
            })

            # ================= فیلتر ۲: ساعتی =================
            df_hourly = fetch_ohlcv(symbol, HOURLY_TF, HOURLY_LIMIT)
            if df_hourly is None:
                continue

            df_hourly = calc_hourly_indicators(df_hourly)
            last_hourly = df_hourly.iloc[-1]

            if not (pd.notna(last_hourly['rsi']) and pd.notna(last_hourly['rsi_ma'])):
                continue

            if last_hourly['rsi'] <= last_hourly['rsi_ma']:
                continue  # ❌ فیلتر ۲ رد شد

            passed_filter2 += 1

            # ثبت عبور از فیلتر ۲
            passed_filter2_items.append({
                'symbol': symbol,
                'price': last_hourly['c'],
                'daily_ema30': last_daily['ema30'],
                'daily_ema50': last_daily['ema50'],
                'rsi': last_hourly['rsi'],
                'rsi_ma': last_hourly['rsi_ma'],
                'rsi_diff': last_hourly['rsi'] - last_hourly['rsi_ma'],
                'mkt_type': mkt,
                'mc': get_market_cap(symbol)
            })

            # ================= فیلتر ۳: ساعتی =================
            if not (30 <= last_hourly['rsi_ma'] <= 70):
                continue  # ❌ فیلتر ۳ رد شد

            # ================= محاسبه ریسک =================
            if pd.notna(last_hourly['ema200']) and last_hourly['ema200'] != 0:
                risk_pct = ((last_hourly['c'] - last_hourly['ema200']) / last_hourly['ema200']) * 100
            else:
                continue

            # ✅ هر سه فیلتر پاس شدند
            results.append({
                'symbol': symbol,
                'price': last_hourly['c'],
                'daily_ema30': last_daily['ema30'],
                'daily_ema50': last_daily['ema50'],
                'rsi': last_hourly['rsi'],
                'rsi_ma': last_hourly['rsi_ma'],
                'rsi_diff': last_hourly['rsi'] - last_hourly['rsi_ma'],
                'ema200': last_hourly['ema200'],
                'risk_pct': risk_pct,
                'mc': get_market_cap(symbol),
                'mkt_type': mkt,
                'info': info
            })

        except Exception as e:
            if DEBUG_MODE:
                print(f"⚠️ Error {symbol}: {e}")

        time.sleep(0.02)

    # ✅ سورت صعودی بر اساس Risk% (کم‌ریسک اول)
    results.sort(key=lambda x: x['risk_pct'] if x['risk_pct'] is not None else 999)

    print(f"✅ فیلتر ۱ (روزانه): {passed_filter1} نماد")
    print(f"✅ فیلتر ۲ (ساعتی RSI>MA): {passed_filter2} نماد")
    print(f"✅ فیلتر ۳ (ساعتی MA 30-70): {len(results)} نماد")

    return results, passed_filter1, passed_filter2, passed_filter1_items, passed_filter2_items

# ================= MAIN =================
def run():
    print("🚀 شروع اسکن سه مرحله‌ای...")
    load_market_caps()
    pairs = get_filtered_pairs()
    print(f"📊 کل نمادهای فعال: {len(pairs)}")

    # اجرای اسکن
    final_results, passed_f1, passed_f2, f1_items, f2_items = run_scan(pairs)

    # پیام خلاصه
    summary_msg = build_summary_message(len(pairs), passed_f1, passed_f2, len(final_results))
    send_telegram_message(summary_msg)

    # گزارش فیلتر ۱
    for msg in build_stage_messages(
        stage_title="نمادهای عبور کرده از فیلتر ۱",
        stage_note="شرط: EMA30 روزانه >= EMA50 روزانه",
        items=f1_items,
        renderer=render_filter1_item,
        total_pairs=len(pairs),
        passed_count=len(f1_items)
    ):
        send_telegram_message(msg)
        time.sleep(0.3)

    # گزارش فیلتر ۲
    for msg in build_stage_messages(
        stage_title="نمادهای عبور کرده از فیلتر ۲",
        stage_note="شرط: RSI(30) > RSI_MA(50) در تایم ساعتی",
        items=f2_items,
        renderer=render_filter2_item,
        total_pairs=len(pairs),
        passed_count=len(f2_items)
    ):
        send_telegram_message(msg)
        time.sleep(0.3)

    # گزارش نهایی فیلتر ۳
    for msg in build_stage_messages(
        stage_title="نمادهای عبور کرده از فیلتر ۳",
        stage_note="شرط: RSI_MA بین 30 تا 70 + محاسبه Risk%",
        items=final_results,
        renderer=render_final_item,
        total_pairs=len(pairs),
        passed_count=len(final_results)
    ):
        send_telegram_message(msg)
        time.sleep(0.3)

    print("✅ پایان کامل اسکن")

# ================= RUN =================
if __name__ == "__main__":
    send_telegram_message(
        "🤖 <b>اسکن سه مرحله‌ای شروع شد</b>\n"
        "1️⃣ روزانه: EMA30 ≥ EMA50\n"
        "2️⃣ ساعتی: RSI(30) > RSI_MA(50)\n"
        "3️⃣ ساعتی: RSI_MA 30-70\n"
        "📊 سورت بر اساس Risk%"
    )
    run()
    send_telegram_message("✅ <b>اسکن با موفقیت پایان یافت</b>")
