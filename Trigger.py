# ✅ اسکنر بازار کریپتو - صرافی XT با فیلترهای جدید
# فیلتر روزانه: قیمت > EMA50 و RSI(50) > 50
# سپس بررسی در دو مستطیل (مثبت و منفی) روی تایم ساعتی:
#   مستطیل مثبت: EMA50 > EMA200
#   مستطیل منفی: EMA50 < EMA200
# محاسبه Re = (EMA50 - EMA200) / EMA200 * 100
# محاسبه Rp = (قیمت - EMA200) / EMA200 * 100 
# فیلتر نهایی: |Re| و |Rp| طبق محدوده‌های مجزای مثبت/منفی
# فیلتر مارکت کپ: بین MIN_MARKET_CAP و MAX_MARKET_CAP (پیش‌فرض ۱ تا ۸۰ میلیون دلار)
# نمایش RSI روزانه و ساعتی + مارکت کپ + نسبت حجم (Volume Ratio) 

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
HOURLY_LIMIT = 400   # برای محاسبه EMA200 نیازمند حداقل ۲۰۰ کندل هستیم

# حداقل کندل مورد نیاز
MIN_BARS_REQUIRED = 200

# تنظیمات فیلتر روزانه
RSI_PERIOD = 50
RSI_THRESHOLD = 50   # RSI(50) > 50

# تنظیمات ریسک جداگانه برای مستطیل مثبت و منفی
POS_MIN_RISK = 0.0    # حداقل |Re| و |Rp| برای مستطیل مثبت
POS_MAX_RISK = 2.0    # حداکثر |Re| و |Rp| برای مستطیل مثبت
NEG_MIN_RISK = -2.0    # حداقل |Re| و |Rp| برای مستطیل منفی
NEG_MAX_RISK = 0.0    # حداکثر |Re| و |Rp| برای مستطیل منفی

# تنظیمات مارکت کپ (به دلار)
MIN_MARKET_CAP = 1_000_000       # حداقل ۱ میلیون دلار
MAX_MARKET_CAP = 80_000_000      # حداکثر ۸۰ میلیون دلار

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
    symbol_map = {}
    for symbol, info in exchange_markets.items():
        if not info.get('active') or info.get('quote') != 'USDT':
            continue
        is_spot = info.get('spot', False)
        is_future = info.get('future', False) or info.get('swap', False)
        should_scan = (SCAN_SPOT and is_spot) or (SCAN_FUTURES and is_future)
        if not should_scan:
            continue
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
        df['volume'] = df['volume'].astype(float)
        return df
    except Exception as e:
        if DEBUG_MODE:
            print(f"⚠️ Fetch error {symbol}: {e}")
        return None

# ================= RSI Calculation =================
def compute_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ================= COINMARKETCAP MARKET CAP =================
def get_market_cap_from_cmc(symbol_base):
    try:
        if not CMC_API_KEY:
            return None
        url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
        params = {'symbol': symbol_base.upper(), 'convert': 'USD'}
        headers = {'X-CMC_PRO_API_KEY': CMC_API_KEY, 'Accepts': 'application/json'}
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
    results = []
    total = len(pairs)

    print(f"🔍 شروع اسکن {total} نماد...")
    print(f"   فیلتر روزانه: قیمت > EMA50 و RSI({RSI_PERIOD}) > {RSI_THRESHOLD}")
    print(f"   مستطیل مثبت: EMA50 > EMA200 | فیلتر ریسک: |Re|,|Rp| ∈ [{POS_MIN_RISK}%, {POS_MAX_RISK}%]")
    print(f"   مستطیل منفی: EMA50 < EMA200 | فیلتر ریسک: |Re|,|Rp| ∈ [{NEG_MIN_RISK}%, {NEG_MAX_RISK}%]")
    print(f"   فیلتر مارکت کپ: {MIN_MARKET_CAP:,} - {MAX_MARKET_CAP:,} USD")
    print("-" * 50)

    for idx, (symbol, info) in enumerate(tqdm(pairs, desc="Scanning", total=total), 1):
        try:
            # 1️⃣ فیلتر روزانه
            df_daily = fetch_ohlcv(symbol, DAILY_TF, DAILY_LIMIT)
            if df_daily is None:
                continue

            df_daily['ema50'] = df_daily['close'].ewm(span=50, adjust=False).mean()
            df_daily['rsi'] = compute_rsi(df_daily['close'], period=RSI_PERIOD)

            last_daily = df_daily.iloc[-1]
            if pd.isna(last_daily['close']) or pd.isna(last_daily['ema50']) or pd.isna(last_daily['rsi']):
                continue

            if not (last_daily['close'] > last_daily['ema50'] and last_daily['rsi'] > RSI_THRESHOLD):
                continue

            # 2️⃣ دریافت داده ساعتی و محاسبات
            df_hourly = fetch_ohlcv(symbol, HOURLY_TF, HOURLY_LIMIT)
            if df_hourly is None:
                continue

            df_hourly['ema50'] = df_hourly['close'].ewm(span=50, adjust=False).mean()
            df_hourly['ema200'] = df_hourly['close'].ewm(span=200, adjust=False).mean()
            df_hourly['rsi'] = compute_rsi(df_hourly['close'], period=RSI_PERIOD)

            last_hourly = df_hourly.iloc[-1]
            if pd.isna(last_hourly['close']) or pd.isna(last_hourly['ema50']) or pd.isna(last_hourly['ema200']) or pd.isna(last_hourly['rsi']):
                continue

            # تشخیص مستطیل
            if last_hourly['ema50'] > last_hourly['ema200']:
                rect = '🟢 POS'
                min_risk, max_risk = POS_MIN_RISK, POS_MAX_RISK
            elif last_hourly['ema50'] < last_hourly['ema200']:
                rect = '🔴 NEG'
                min_risk, max_risk = NEG_MIN_RISK, NEG_MAX_RISK
            else:
                continue

            # محاسبه Re و Rp
            re_val = (last_hourly['ema50'] - last_hourly['ema200']) / last_hourly['ema200'] * 100
            rp_val = (last_hourly['close'] - last_hourly['ema200']) / last_hourly['ema200'] * 100

            # فیلتر بر اساس محدوده مجزای همان مستطیل
            if not (min_risk <= abs(re_val) <= max_risk and min_risk <= abs(rp_val) <= max_risk):
                continue

            # محاسبه Volume Ratio
            avg_5h = df_hourly['volume'].iloc[-5:].mean()
            avg_200h = df_hourly['volume'].iloc[-200:].mean()
            if avg_200h > 0 and not np.isnan(avg_200h):
                volume_ratio = avg_5h / avg_200h
            else:
                volume_ratio = 0

            # دریافت مارکت کپ
            symbol_base = symbol.split('/')[0]
            market_cap = get_market_cap_from_cmc(symbol_base)

            # فیلتر مارکت کپ
            if market_cap is not None:
                if market_cap < MIN_MARKET_CAP or market_cap > MAX_MARKET_CAP:
                    continue
            else:
                if MIN_MARKET_CAP > 0:   # اگر مارکت کپ نامشخص باشد و حداقل مقداری تنظیم شده، رد کن
                    continue

            daily_rsi = last_daily['rsi']
            hourly_rsi = last_hourly['rsi']
            mkt_type = 'F' if (info.get('future') or info.get('swap')) else 'S'

            results.append({
                'symbol': symbol,
                'symbol_base': symbol_base,
                'price': last_hourly['close'],
                'rect': rect,
                'Re': re_val,
                'Rp': rp_val,
                'rsi_daily': daily_rsi,
                'rsi_hourly': hourly_rsi,
                'volume_ratio': volume_ratio,
                'market_cap': market_cap,
                'mkt_type': mkt_type,
                'info': info
            })

        except Exception as e:
            if DEBUG_MODE:
                print(f"⚠️ Error {symbol}: {e}")

        time.sleep(0.01)

    results.sort(key=lambda x: abs(x['Re']))
    return results

# ================= MESSAGE BUILDER (بدون جدول) =================
def build_messages_for_rect(signals, rect_type, total_scanned, min_risk, max_risk):
    filtered = [s for s in signals if s['rect'] == rect_type]
    if not filtered:
        return []

    now = datetime.now().strftime('%Y/%m/%d %H:%M:%S')
    rect_label = "مستطیل مثبت (EMA50 > EMA200)" if rect_type == '🟢 POS' else "مستطیل منفی (EMA50 < EMA200)"
    header = (
        f"🔍 <b>اسکنر XT | {rect_label}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 نمادهای بررسی شده: <code>{total_scanned}</code>\n"
        f"✅ عبور کرده در این گروه: <code>{len(filtered)}</code>\n"
        f"📋 فیلترها:\n"
        f" ├─ روزانه: Price > EMA50 & RSI({RSI_PERIOD}) > {RSI_THRESHOLD}\n"
        f" ├─ |Re|, |Rp| ∈ [{min_risk}%, {max_risk}%]\n"
        f" └─ مارکت کپ: {MIN_MARKET_CAP:,.0f} - {MAX_MARKET_CAP:,.0f} USD\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    footer = f"\n⏰ {now} 🇮🇷\n🤖 XT Scanner v3.2"

    cards_text = ""
    for r, s in enumerate(filtered, 1):
        tv_symbol = s['symbol'].replace('/', '')
        tv_link = f"https://www.tradingview.com/chart/?symbol=:{tv_symbol}"
        mc_str = "N/A"
        if s['market_cap'] is not None:
            if s['market_cap'] >= 1e9:
                mc_str = f"${s['market_cap']/1e9:.2f}B"
            elif s['market_cap'] >= 1e6:
                mc_str = f"${s['market_cap']/1e6:.2f}M"
            else:
                mc_str = f"${s['market_cap']:,.0f}"

        vol_emoji = "🔥" if s['volume_ratio'] > 1.5 else ("📈" if s['volume_ratio'] > 1.0 else "📉")
        vol_text = f"{s['volume_ratio']:.2f}x"

        card = (
            f"{r}. <a href='{tv_link}'>{escape(s['symbol'])}</a> [{s['mkt_type']}] {s['rect']}\n"
            f"💰 Price: {s['price']:,.6f} USDT\n"
            f"📐 Re: {s['Re']:+.2f}% \n"
            f"📐 Rp: {s['Rp']:+.2f}%\n"
            f"📊 RSI:D {s['rsi_daily']:.1f} \n"
            f"📊 RSI:H {s['rsi_hourly']:.1f}\n"
            f"{vol_emoji} Vol Ratio: <b>{vol_text}</b>\n"
            f"🏛️ Market Cap: {mc_str}\n"
            f"─────────────────────\n"
        )
        cards_text += card

    full_text = header + cards_text + footer

    max_len = 4000
    messages = []
    if len(full_text) <= max_len:
        messages.append(full_text)
    else:
        current = header
        for line in cards_text.splitlines(True):
            if len(current) + len(line) + len(footer) + 50 > max_len:
                messages.append(current + footer)
                current = line
            else:
                current += line
        if current.strip():
            messages.append(current + footer)
    return messages

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
    print("🚀 شروع اسکنر XT با فیلترهای جدید...")
    pairs = get_filtered_pairs()
    print(f"📊 کل نمادهای فعال (بدون تکرار): {len(pairs)}")
    results = scan_market(pairs)
    print(f"\n✅ اسکن پایان یافت: {len(results)} نماد پیدا شد")

    if results:
        print("\n" + "=" * 60)
        for i, r in enumerate(results, 1):
            mc_str = f"${r['market_cap']:,.0f}" if r['market_cap'] else "N/A"
            print(f"\n{i}. {r['symbol']} [{r['mkt_type']}] {r['rect']}")
            print(f"   Price: {r['price']:,.6f}")
            print(f"   Re: {r['Re']:+.2f}% | Rp: {r['Rp']:+.2f}%")
            print(f"   RSI: D {r['rsi_daily']:.1f} | H {r['rsi_hourly']:.1f}")
            print(f"   Vol Ratio: {r['volume_ratio']:.2f}x")
            print(f"   Market Cap: {mc_str}")
        print("=" * 60)

    if TELEGRAM_CHAT_IDS:
        print("\n📤 ارسال نتایج به تلگرام...")
        rects = [
            ('🟢 POS', POS_MIN_RISK, POS_MAX_RISK),
            ('🔴 NEG', NEG_MIN_RISK, NEG_MAX_RISK)
        ]
        for rect, min_r, max_r in rects:
            msgs = build_messages_for_rect(results, rect, len(pairs), min_r, max_r)
            for msg in msgs:
                send_telegram_message(msg)
                time.sleep(0.4)
        print("✅ همه پیام‌ها ارسال شدند")

if __name__ == "__main__":
    run()
