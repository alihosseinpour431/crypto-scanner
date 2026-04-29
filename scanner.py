# scanner.py
# ⚠️ این کد دقیقاً همون کد گوگل کولب شماست، فقط با ۲ تغییر برای سازگاری با GitHub Actions

import os  # ← این خط اضافه شده
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

# تنظیمات تایم‌فریم‌ها
DAILY_TIMEFRAME = '1d'
DAILY_LIMIT = 230
HOURLY_TIMEFRAME = '1h'
HOURLY_LIMIT = 300
MIN_REQUIRED_BARS = 210

# بازه‌های محاسبه Alpha
ALPHA_SHORT = 3
ALPHA_LONG = 10

# ================= تغییر ۱: خواندن توکن از Environment Variable =================
# به جای هاردکد کردن، از گیت‌هاب سکرت می‌خونیم
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  

# ================= تغییر ۲: خواندن چت‌آیدی‌ها به صورت لیست =================
# گیت‌هاب سکرت رو به صورت رشته می‌فرسته، اینجا تبدیل به لیست می‌کنیم
TELEGRAM_CHAT_IDS = os.getenv("TELEGRAM_CHAT_ID", "487817626").split(",")

COINGECKO_API = "https://api.coingecko.com/api/v3"
DEBUG_MODE = True

exchange = getattr(ccxt, EXCHANGE_ID)({
    'enableRateLimit': True,
    'timeout': 30000
})
exchange_markets = exchange.load_markets()

# ================= CACHE =================
market_cap_cache = {}
symbol_type_cache = {}

# ================= TELEGRAM =================
def send_telegram_message(text, chat_id=None):
    """ارسال پیام به تلگرام با فرمت HTML — اگر chat_id داده نشود، به همه ارسال می‌کند"""
    targets = [chat_id] if chat_id else TELEGRAM_CHAT_IDS
    for cid in targets:
        cid = cid.strip()  # حذف فاصله‌های اضافی
        if not cid: continue
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            'chat_id': cid,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
            'disable_notification': False
        }
        try:
            r = requests.post(url, json=payload, timeout=30)
            if DEBUG_MODE and r.status_code != 200:
                print(f"⚠️ Telegram ({cid}): {r.status_code} | {r.text}")
        except Exception as e:
            print(f"❌ Telegram Error ({cid}): {e}")

# ================= COINGECKO MARKET CAP =================
def load_market_caps():
    if market_cap_cache:
        return
    print("📥 در حال دریافت Market Cap از CoinGecko ...")
    session = requests.Session()
    for page in range(1, 21):
        try:
            url = f"{COINGECKO_API}/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page={page}&sparkline=false"
            r = session.get(url, timeout=20)
            data = r.json()
            if not isinstance(data, list) or len(data) == 0:
                break
            for coin in data:
                sym = str(coin.get('symbol', '')).upper().strip()
                mc = coin.get('market_cap')
                if not sym or not isinstance(mc, (int, float)):
                    continue
                prev = market_cap_cache.get(sym)
                if prev is None or mc > prev:
                    market_cap_cache[sym] = mc
            if len(data) < 250:
                break
            time.sleep(0.15)
        except Exception as e:
            if DEBUG_MODE:
                print(f"⚠️ خطا در load_market_caps page={page}: {e}")
            break
    print(f"✅ Market Cap loaded: {len(market_cap_cache)} symbols")

def get_market_cap(symbol):
    base_symbol = symbol.split('/')[0].upper().strip()
    return market_cap_cache.get(base_symbol)

def format_market_cap(value):
    """فرمت‌بندی حرفه‌ای مارکت‌کپ با ایموجی و بولد"""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "🔸 N/A"
    try:
        value = float(value)
    except:
        return "🔸 N/A"
    if value >= 1e12: return f"💎 <b>${value/1e12:.2f}T</b>"
    if value >= 1e9: return f"💎 <b>${value/1e9:.2f}B</b>"
    if value >= 1e6: return f"💎 <b>${value/1e6:.2f}M</b>"
    if value >= 1e3: return f"💎 <b>${value/1e3:.2f}K</b>"
    return f"💎 <b>${value:,.0f}</b>"

# ================= MARKET =================
def get_symbol_type(symbol):
    if symbol in symbol_type_cache:
        return symbol_type_cache[symbol]
    try:
        info = exchange_markets.get(symbol, {})
        is_future = info.get('future', False) or info.get('swap', False)
        result = 'F' if is_future else 'S'
        symbol_type_cache[symbol] = result
        return result
    except:
        return 'S'

def get_filtered_pairs():
    pairs = []
    for symbol, info in exchange_markets.items():
        if not info.get('active'): continue
        if info.get('quote') != 'USDT': continue
        is_spot = info.get('spot', False)
        is_future = info.get('future', False) or info.get('swap', False)
        if (SCAN_SPOT and is_spot) or (SCAN_FUTURES and is_future):
            pairs.append((symbol, info))
    return pairs

# ================= DATA =================
def fetch_ohlcv(symbol, timeframe, limit):
    try:
        data = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if len(data) < MIN_REQUIRED_BARS:
            return None
        df = pd.DataFrame(data, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
        return df
    except:
        return None

# ================= INDICATORS =================
def calculate(df, short_win=ALPHA_SHORT, long_win=ALPHA_LONG):
    df = df.copy()
    df['ema30'] = df['c'].ewm(span=30, adjust=False).mean()
    df['ema50'] = df['c'].ewm(span=50, adjust=False).mean()
    df['ema200'] = df['c'].ewm(span=200, adjust=False).mean()
    delta = df['c'].diff()
    gain = delta.where(delta > 0, 0).rolling(30).mean()
    loss = -delta.where(delta < 0, 0).rolling(30).mean()
    rs = gain / loss.replace(0, np.nan)
    df['rsi'] = 100 - (100 / (1 + rs))
    vol_short = df['v'].rolling(short_win).mean()
    vol_long = df['v'].rolling(long_win).mean()
    df['alpha'] = vol_short / vol_long.replace(0, np.nan)
    diff_close = df['c'].diff()
    direction = np.where(diff_close > 0, 1, np.where(diff_close < 0, -1, 0))
    df['obv'] = (pd.Series(direction, index=df.index) * df['v']).cumsum()
    obv_short = df['obv'].rolling(short_win).mean()
    obv_long = df['obv'].rolling(long_win).mean()
    df['obv_alpha'] = obv_short / obv_long.replace(0, np.nan)
    return df

# ================= SIGNAL CHECKERS =================
def check_daily(df):
    last = df.iloc[-1]
    if pd.isna(last['ema30']) or pd.isna(last['ema50']) or pd.isna(last['rsi']):
        return False, None, None
    cond_price = last['c'] > last['ema30']
    cond_ema = last['ema30'] > last['ema50']
    cond_rsi = last['rsi'] > 50
    if cond_price and cond_ema and cond_rsi:
        return True, last['c'], last['rsi']
    return False, None, None

def check_hourly(df):
    last = df.iloc[-1]
    if pd.isna(last['ema50']) or pd.isna(last['ema200']) or pd.isna(last['rsi']):
        return False, None, None
    cond_price = last['c'] > last['ema50']
    cond_ema = last['ema50'] > last['ema200']
    cond_rsi = last['rsi'] > 50
    if cond_price and cond_ema and cond_rsi:
        return True, last['c'], last['rsi']
    return False, None, None

# ================= FORMATTING =================
def fmt_3(value):
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "N/A"
    try:
        return f"{float(value):.3f}"
    except:
        return "N/A"

def get_rsi_badge(rsi):
    if rsi < 55:
        return "🟢", "ورود اولیه", "#4CAF50"
    elif rsi < 65:
        return "🔵", "روند پایدار", "#2196F3"
    elif rsi < 75:
        return "🟡", "نزدیک اشباع", "#FFC107"
    else:
        return "🔴", "اشباع خرید", "#F44336"

def build_signal_card(rank, sig):
    emoji, rsi_txt, _ = get_rsi_badge(sig['rsi'])
    type_badge = "🅂 <i>Spot</i>" if sig['market_type'] == 'S' else "🄵 <i>Futures</i>"
    mc = format_market_cap(sig.get('market_cap'))
    alpha_val = sig.get('alpha', 0) or 0
    obv_val = sig.get('obv_alpha', 0) or 0
    if alpha_val > 1.2 and obv_val > 1.1:
        signal_strength = "🔥 <b>قوی</b>"
    elif alpha_val > 1.0:
        signal_strength = "⚡ <b>متوسط</b>"
    else:
        signal_strength = "🔸 <i>ضعیف</i>"
    is_hot = alpha_val > 1.0
    hot_banner = ""
    if is_hot:
        hot_banner = f"🔥 <b>Vα فعال:</b> <code>{fmt_3(alpha_val)}</code> │ حجم در حال ورود 📈\n"
    alpha_display = (
        f"⚡ Vα: <b><code>{fmt_3(alpha_val)}</code></b> 🔺" if is_hot
        else f"⚡ Vα: <code>{fmt_3(alpha_val)}</code>"
    )
    card = (
        f"{hot_banner}"
        f"┌─ {rank}. {emoji} <b>{escape(sig['symbol'])}</b> {type_badge}\n"
        f"│\n"
        f"│  💰 قیمت: <code>{sig['price']:,.4f} USDT</code>\n"
        f"│  📊 RSI(1h): <code>{sig['rsi']:5.1f}</code> │ <i>{rsi_txt}</i>\n"
        f"│  {alpha_display}\n"
        f"│  📦 OBVα: <code>{fmt_3(obv_val)}</code>\n"
        f"│  🎯 قدرت: {signal_strength}\n"
        f"│  {mc}\n"
        f"└─────────────────────────────\n"
    )
    return card

def build_batch_message(signals, stage_info=None):
    now_tehran = datetime.now(pytz.timezone('Asia/Tehran'))
    tehran_time = jdatetime.datetime.fromgregorian(datetime=now_tehran).strftime('%Y/%m/%d %H:%M:%S')
    header = (
        f"🔍 <b>گزارش اسکن هوشمند | XT Exchange</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>پارامترهای فیلتر:</b>\n"
        f"├─ 📅 روزانه: Price>EMA30>EMA50 | RSI(30)>50\n"
        f"├─ ⏰ ساعتی: Price>EMA50>EMA200 | RSI(30)>50\n"
        f"└─ ⚡ Alpha: میانگین ۳ / ۱۰ دوره (حجم + OBV)\n"
    )
    stats_box = ""
    if stage_info:
        stats_box = (
            f"\n📈 <b>آمار فیلتر دو مرحله‌ای:</b>\n"
            f"├─ ✅ عبور از فیلتر روزانه: <code>{stage_info['daily_passed']:4d}</code> نماد\n"
            f"└─ 🎯 عبور از فیلتر ساعتی: <code>{stage_info['hourly_passed']:4d}</code> نماد نهایی\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        )
    footer = (
        f"\n⏰ بروزرسانی: <b>{tehran_time}</b> 🇮🇷 تهران\n"
        f"⚠️ <i>این گزارش صرفاً تحلیلی است و توصیه مالی محسوب نمی‌شود.</i>\n"
        f"🤖 <code>AlphaScanner v2.1</code>"
    )
    messages = []
    MAX_LEN = 4000
    body = ""
    for rank, sig in enumerate(signals, 1):
        card = build_signal_card(rank, sig)
        if len(header) + len(stats_box) + len(body) + len(card) + len(footer) > MAX_LEN - 200:
            full_msg = header + stats_box + body + footer
            messages.append(full_msg)
            body = card
            stats_box = ""
        else:
            body += card
    if body.strip():
        final_msg = header + stats_box + body + footer
        messages.append(final_msg)
    if not messages:
        empty_msg = (
            f"{header}\n"
            f"\n❌ <b>هیچ سیگنال معتبری یافت نشد.</b>\n"
            f"• شرایط فیلترها بسیار سخت‌گیرانه است.\n"
            f"• پیشنهاد: پارامترها را بررسی یا تایم‌فریم را تغییر دهید.\n"
            f"{footer}"
        )
        messages.append(empty_msg)
    return messages

# ================= SCAN STAGE =================
def scan_stage(symbols_to_scan, timeframe, limit, check_func, stage_name, calc_short=ALPHA_SHORT, calc_long=ALPHA_LONG):
    signals = []
    total = len(symbols_to_scan)
    print(f"🔍 شروع {stage_name}: {total} نماد برای بررسی")
    for idx, (symbol, info) in enumerate(tqdm(symbols_to_scan, desc=stage_name, total=total), 1):
        try:
            df = fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            if df is None:
                continue
            df = calculate(df, short_win=calc_short, long_win=calc_long)
            ok, price, rsi = check_func(df)
            if ok:
                market_type = 'F' if (info.get('future', False) or info.get('swap', False)) else 'S'
                signals.append({
                    'symbol': symbol,
                    'price': price,
                    'rsi': rsi,
                    'alpha': df.iloc[-1]['alpha'],
                    'obv_alpha': df.iloc[-1]['obv_alpha'],
                    'market_cap': get_market_cap(symbol),
                    'market_type': market_type,
                    'info': info
                })
        except Exception as e:
            if DEBUG_MODE:
                print(f"⚠️ {idx}/{total} {symbol}: {e}")
        if idx % 50 == 0 or idx == total:
            print(f"✅ {stage_name}: {idx}/{total} بررسی شد | سیگنال‌ها: {len(signals)}")
        time.sleep(0.02)
    return signals

# ================= MAIN =================
def run():
    print("🚀 شروع اسکن دو مرحله‌ای (روزانه ← ساعتی)...")
    load_market_caps()
    all_pairs = get_filtered_pairs()
    print(f"📊 کل نمادهای فعال برای اسکن: {len(all_pairs)}")
    daily_signals = scan_stage(
        symbols_to_scan=all_pairs,
        timeframe=DAILY_TIMEFRAME,
        limit=DAILY_LIMIT,
        check_func=check_daily,
        stage_name="📅 فیلتر روزانه (1d)",
        calc_short=3,
        calc_long=10
    )
    daily_count = len(daily_signals)
    print(f"✅ {daily_count} ارز از فیلتر روزانه عبور کردند.")
    if daily_count == 0:
        send_telegram_message(
            f"❌ <b>سیگنالی یافت نشد</b>\n"
            f"• هیچ ارزی از فیلتر روزانه عبور نکرد.\n"
            f"• شرایط:قیمت>EMA30>EMA50 و RSI>50 در تایم‌فریم {DAILY_TIMEFRAME}"
        )
        return
    hourly_candidates = [(sig['symbol'], sig['info']) for sig in daily_signals]
    hourly_signals = scan_stage(
        symbols_to_scan=hourly_candidates,
        timeframe=HOURLY_TIMEFRAME,
        limit=HOURLY_LIMIT,
        check_func=check_hourly,
        stage_name="⏰ فیلتر ساعتی (1h)",
        calc_short=3,
        calc_long=10
    )
    hourly_count = len(hourly_signals)
    print(f"✅ {hourly_count} ارز از فیلتر ساعتی نیز عبور کردند (نهایی).")
    if hourly_signals:
        signals_sorted = sorted(hourly_signals, key=lambda x: x['rsi'])
        stage_info = {
            'daily_passed': daily_count,
            'hourly_passed': hourly_count
        }
        messages = build_batch_message(signals_sorted, stage_info)
        print(f"📤 در حال ارسال {len(messages)} پیام تجمیعی ({hourly_count} سیگنال نهایی)...")
        for msg in messages:
            for chat_id in TELEGRAM_CHAT_IDS:
                send_telegram_message(msg, chat_id)
            time.sleep(0.3)
    else:
        send_telegram_message(
            f"⚠️ <b>نتیجه اسکن دو مرحله‌ای:</b>\n"
            f"• {daily_count} ارز از فیلتر روزانه عبور کردند.\n"
            f"• اما هیچ‌کدام از فیلتر ساعتی عبور نکردند.\n"
            f"• شرایط نهایی: قیمت>EMA50>EMA200 و RSI>50 در تایم‌فریم {HOURLY_TIMEFRAME}"
        )
    print("✅ پایان اسکن دو مرحله‌ای")

# ================= RUN =================
if __name__ == "__main__":
    send_telegram_message("🤖 <b>ربات اسکن دو مرحله‌ای (روزانه ← ساعتی) شروع شد</b>")
    run()
    send_telegram_message("✅ <b>اسکن دو مرحله‌ای به پایان رسید</b>")
