# scanner.py
# ✅ نسخه نهایی بهینه‌شده برای GitHub Actions

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

DAILY_TIMEFRAME = '1d'
DAILY_LIMIT = 230
HOURLY_TIMEFRAME = '1h'
HOURLY_LIMIT = 300
MIN_REQUIRED_BARS = 210
ALPHA_SHORT = 3
ALPHA_LONG = 10

# ================= ENV & SECURITY FIXES =================
# 1. اعتبارسنجی توکن
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("❌ TELEGRAM_BOT_TOKEN is not set in environment variables!")

# 2. پارس کردن امن Chat IDs (حذف آی‌دی‌های خالی)
TELEGRAM_CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "487817626").split(",") if cid.strip()]

# 3. کنترل لاگ‌ها از طریق متغیر محیطی
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

COINGECKO_API = "https://api.coingecko.com/api/v3"

# ================= EXCHANGE INIT WITH ERROR HANDLING =================
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
symbol_type_cache = {}

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
    print("📥 Loading Market Cap from CoinGecko ...")
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
                print(f"⚠️ Error in load_market_caps page={page}: {e}")
            break
    print(f"✅ Market Cap loaded: {len(market_cap_cache)} symbols")

def get_market_cap(symbol):
    base_symbol = symbol.split('/')[0].upper().strip()
    return market_cap_cache.get(base_symbol)

def format_market_cap(value):
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
    except Exception as e:
        if DEBUG_MODE:
            print(f"⚠️ Fetch error {symbol}: {e}")
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
    if rsi < 55: return "🟢", "ورود اولیه", "#4CAF50"
    elif rsi < 65: return "🔵", "روند پایدار", "#2196F3"
    elif rsi < 75: return "🟡", "نزدیک اشباع", "#FFC107"
    else: return "🔴", "اشباع خرید", "#F44336"

def build_signal_card(rank, sig):
    emoji, rsi_txt, _ = get_rsi_badge(sig['rsi'])
    type_badge = "🅂 <i>Spot</i>" if sig['market_type'] == 'S' else "🄵 <i>Futures</i>"
    mc = format_market_cap(sig.get('market_cap'))
    alpha_val = sig.get('alpha', 0) or 0
    obv_val = sig.get('obv_alpha', 0) or 0
    
    if alpha_val > 1.2 and obv_val > 1.1: signal_strength = "🔥 <b>قوی</b>"
    elif alpha_val > 1.0: signal_strength = "⚡ <b>متوسط</b>"
    else: signal_strength = "🔸 <i>ضعیف</i>"
    
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
    # چک نهایی قبل از اجرا
    if not TELEGRAM_BOT_TOKEN:
        print("❌ Error: Telegram token missing. Exiting.")
        exit(1)
        
    send_telegram_message("🤖 <b>ربات اسکن دو مرحله‌ای (روزانه ← ساعتی) شروع شد</b>")
    run()
    send_telegram_message("✅ <b>اسکن دو مرحله‌ای به پایان رسید</b>")



# ================= STAGE 2: MACD + RSI SCAN (30m) =================

STAGE2_TIMEFRAME = '30m'
STAGE2_LIMIT = 300
STAGE2_MIN_BARS = 200

# MACD Parameters (Custom)
MACD_FAST = 36
MACD_SLOW = 78
MACD_SIGNAL = 30

# RSI Parameter
RSI_LENGTH = 30

def calculate_macd_rsi(df, fast=MACD_FAST, slow=MACD_SLOW, signal_len=MACD_SIGNAL, rsi_len=RSI_LENGTH):
    """
    Calculate MACD and RSI indicators
    MACD: Custom parameters (36, 78, 30)
    RSI: Length 30
    """
    df = df.copy()
    
    # Calculate MACD
    ema_fast = df['c'].ewm(span=fast, adjust=False).mean()
    ema_slow = df['c'].ewm(span=slow, adjust=False).mean()
    df['macd'] = ema_fast - ema_slow
    df['macd_signal'] = df['macd'].ewm(span=signal_len, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']
    
    # Calculate RSI (Length 30)
    delta = df['c'].diff()
    gain = delta.where(delta > 0, 0).rolling(rsi_len).mean()
    loss = -delta.where(delta < 0, 0).rolling(rsi_len).mean()
    rs = gain / loss.replace(0, np.nan)
    df['rsi_30'] = 100 - (100 / (1 + rs))
    
    return df

def check_stage2_conditions(df):
    """
    Check Stage 2 conditions:
    1. MACD line > Signal line
    2. MACD Histogram > 0
    3. RSI(30) > 50
    """
    if len(df) < STAGE2_MIN_BARS:
        return False, None, None, None
    
    last = df.iloc[-1]
    
    # Check if values are valid
    if pd.isna(last['macd']) or pd.isna(last['macd_signal']) or pd.isna(last['macd_hist']) or pd.isna(last['rsi_30']):
        return False, None, None, None
    
    # Conditions
    macd_above_signal = last['macd'] > last['macd_signal']
    hist_positive = last['macd_hist'] > 0
    rsi_above_50 = last['rsi_30'] > 50
    
    if macd_above_signal and hist_positive and rsi_above_50:
        return True, last['c'], last['rsi_30'], {
            'macd': last['macd'],
            'macd_signal': last['macd_signal'],
            'macd_hist': last['macd_hist']
        }
    
    return False, None, None, None

def scan_stage2(symbols_to_scan, stage_name="🔍 اسکن نهایی (30m)"):
    """
    Scan Stage 2: MACD(36,78,30) + RSI(30) on 30m timeframe
    """
    signals = []
    total = len(symbols_to_scan)
    print(f"🚀 شروع {stage_name}: {total} نماد برای بررسی")
    
    for idx, (symbol, info) in enumerate(tqdm(symbols_to_scan, desc=stage_name, total=total), 1):
        try:
            # Fetch 30m data
            df = fetch_ohlcv(symbol, timeframe=STAGE2_TIMEFRAME, limit=STAGE2_LIMIT)
            if df is None:
                continue
            
            # Calculate MACD and RSI
            df = calculate_macd_rsi(df)
            
            # Check conditions
            ok, price, rsi, macd_data = check_stage2_conditions(df)
            
            if ok:
                market_type = 'F' if (info.get('future', False) or info.get('swap', False)) else 'S'
                signals.append({
                    'symbol': symbol,
                    'price': price,
                    'rsi': rsi,
                    'macd': macd_data['macd'],
                    'macd_signal': macd_data['macd_signal'],
                    'macd_hist': macd_data['macd_hist'],
                    'market_cap': get_market_cap(symbol),
                    'market_type': market_type,
                    'info': info
                })
                
        except Exception as e:
            if DEBUG_MODE:
                print(f"⚠️ {idx}/{total} {symbol}: {e}")
        
        # Progress update
        if idx % 50 == 0 or idx == total:
            print(f"✅ {stage_name}: {idx}/{total} بررسی شد | سیگنال‌ها: {len(signals)}")
        
        time.sleep(0.02)
    
    return signals

def build_stage2_card(rank, sig):
    """
    Build signal card for Stage 2 results
    """
    # RSI Badge
    rsi_val = sig['rsi']
    if rsi_val < 60:
        rsi_emoji, rsi_txt = "🟢", "روند صعودی"
    elif rsi_val < 70:
        rsi_emoji, rsi_txt = "🔵", "قدرتمند"
    elif rsi_val < 80:
        rsi_emoji, rsi_txt = "🟡", "نزدیک اشباع"
    else:
        rsi_emoji, rsi_txt = "🔴", "اشباع خرید"
    
    # Type badge
    type_badge = "🅂 <i>Spot</i>" if sig['market_type'] == 'S' else "🄵 <i>Futures</i>"
    
    # Market cap
    mc = format_market_cap(sig.get('market_cap'))
    
    # MACD status
    macd_hist = sig.get('macd_hist', 0) or 0
    macd_status = "🟢 مثبت" if macd_hist > 0 else "🔴 منفی"
    
    card = (
        f"┌─ {rank}. {rsi_emoji} <b>{escape(sig['symbol'])}</b> {type_badge}\n"
        f"│\n"
        f"│  💰 قیمت: <code>{sig['price']:,.4f} USDT</code>\n"
        f"│  📊 RSI(30): <code>{rsi_val:5.1f}</code> │ <i>{rsi_txt}</i>\n"
        f"│  📈 MACD: <code>{fmt_3(sig['macd'])}</code>\n"
        f"│  📉 Signal: <code>{fmt_3(sig['macd_signal'])}</code>\n"
        f"│  📊 Histogram: <code>{fmt_3(macd_hist)}</code> │ {macd_status}\n"
        f"│  {mc}\n"
        f"└─────────────────────────────\n"
    )
    return card

def build_stage2_message(signals, stage1_count, stage2_count):
    """
    Build final message for Stage 2 results
    """
    now_tehran = datetime.now(pytz.timezone('Asia/Tehran'))
    tehran_time = jdatetime.datetime.fromgregorian(datetime=now_tehran).strftime('%Y/%m/%d %H:%M:%S')
    
    header = (
        f"🎯 <b>اسکن نهایی | تایم‌فریم 30 دقیقه</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>فیلترهای اعمال شده:</b>\n"
        f"├─ 📊 MACD(36,78,30): خط MACD > Signal\n"
        f"├─ 📈 Histogram MACD > 0 (مثبت)\n"
        f"└─ 💪 RSI(30) > 50\n"
        f"\n📈 <b>آمار نهایی:</b>\n"
        f"├─ اسکن 1 (روزانه): <code>{stage1_count:4d}</code> نماد\n"
        f"├─ اسکن 2 (ساعتی): <code>{stage2_count:4d}</code> نماد\n"
        f"└─ ✅ نهایی (30m): <code>{len(signals):4d}</code> نماد\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    
    footer = (
        f"\n⏰ بروزرسانی: <b>{tehran_time}</b> 🇮🇷 تهران\n"
        f"⚠️ <i>این گزارش صرفاً تحلیلی است و توصیه مالی محسوب نمی‌شود.</i>\n"
        f"🤖 <code>AlphaScanner v2.2 - Stage 2</code>"
    )
    
    messages = []
    MAX_LEN = 4000
    body = ""
    
    for rank, sig in enumerate(signals, 1):
        card = build_stage2_card(rank, sig)
        if len(header) + len(body) + len(card) + len(footer) > MAX_LEN - 200:
            full_msg = header + body + footer
            messages.append(full_msg)
            body = card
        else:
            body += card
    
    if body.strip():
        final_msg = header + body + footer
        messages.append(final_msg)
    
    if not messages:
        empty_msg = (
            f"{header}\n"
            f"❌ <b>هیچ ارزی شرایط نهایی را نداشت.</b>\n"
            f"• MACD(36,78,30) باید مثبت باشد و خط MACD بالای Signal\n"
            f"• RSI(30) باید بالای 50 باشد\n"
            f"{footer}"
        )
        messages.append(empty_msg)
    
    return messages

# ================= MODIFY MAIN FUNCTION =================

def run():
    print("🚀 شروع اسکن دو مرحله‌ای (روزانه ← ساعتی)...")
    load_market_caps()
    all_pairs = get_filtered_pairs()
    print(f"📊 کل نمادهای فعال برای اسکن: {len(all_pairs)}")
    
    # Stage 1: Daily filter
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
            f"• شرایط: قیمت>EMA30>EMA50 و RSI>50 در تایم‌فریم {DAILY_TIMEFRAME}"
        )
        return
    
    # Stage 2: Hourly filter
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
    print(f"✅ {hourly_count} ارز از فیلتر ساعتی نیز عبور کردند.")
    
    # Send Stage 1 & 2 results
    if hourly_signals:
        signals_sorted = sorted(hourly_signals, key=lambda x: x['rsi'])
        stage_info = {
            'daily_passed': daily_count,
            'hourly_passed': hourly_count
        }
        messages = build_batch_message(signals_sorted, stage_info)
        print(f"📤 در حال ارسال {len(messages)} پیام تجمیعی ({hourly_count} سیگنال)...")
        for msg in messages:
            for chat_id in TELEGRAM_CHAT_IDS:
                send_telegram_message(msg, chat_id)
            time.sleep(0.3)
    else:
        send_telegram_message(
            f"⚠️ <b>نتیجه اسکن دو مرحله‌ای:</b>\n"
            f"• {daily_count} ارز از فیلتر روزانه عبور کردند.\n"
            f"• اما هیچ‌کدام از فیلتر ساعتی عبور نکردند."
        )
    
    # ================= STAGE 3: MACD + RSI SCAN =================
    print("\n" + "="*60)
    print("🎯 شروع اسکن نهایی (MACD + RSI در تایم‌فریم 30 دقیقه)")
    print("="*60)
    
    if hourly_count > 0:
        stage2_candidates = [(sig['symbol'], sig['info']) for sig in hourly_signals]
        stage2_signals = scan_stage2(
            symbols_to_scan=stage2_candidates,
            stage_name="🔍 اسکن نهایی (30m - MACD+RSI)"
        )
        stage2_count = len(stage2_signals)
        print(f"✅ {stage2_count} ارز از اسکن نهایی عبور کردند.")
        
        if stage2_signals:
            # Sort by RSI
            stage2_sorted = sorted(stage2_signals, key=lambda x: x['rsi'], reverse=True)
            messages = build_stage2_message(stage2_sorted, daily_count, hourly_count)
            print(f"📤 در حال ارسال {len(messages)} پیام نهایی ({stage2_count} سیگنال)...")
            for msg in messages:
                for chat_id in TELEGRAM_CHAT_IDS:
                    send_telegram_message(msg, chat_id)
                time.sleep(0.3)
        else:
            send_telegram_message(
                f"⚠️ <b>اسکن نهایی (30m):</b>\n"
                f"• {hourly_count} ارز از اسکن ساعتی عبور کردند.\n"
                f"• اما هیچ‌کدام شرایط MACD + RSI را نداشتند.\n"
                f"• شرایط: MACD(36,78,30) > 0 و RSI(30) > 50"
            )
    
    print("✅ پایان کامل اسکن سه مرحله‌ای")

# ================= RUN =================
if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN:
        print("❌ Error: Telegram token missing. Exiting.")
        exit(1)
    
    send_telegram_message("🤖 <b>ربات اسکن سه مرحله‌ای شروع شد</b>\n📅 روزانه ← ⏰ ساعتی ← 🎯 30 دقیقه (MACD+RSI)")
    run()
    send_telegram_message("✅ <b>اسکن سه مرحله‌ای به پایان رسید</b>")
