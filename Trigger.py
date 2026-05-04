# Trigger.py
# ✅ نسخه نهایی: EMA30 > EMA50 (سختگیرانه) + سه فیلتر + Risk%

import os
import time
import requests
import ccxt
import pandas as pd
import numpy as np
import pytz
import jdatetime
from html import escape
from tqdm.auto import tqdm

# ================= CONFIG =================
EXCHANGE_ID = 'xt'
SCAN_SPOT = True
SCAN_FUTURES = True

DAILY_TF = '1d'
DAILY_LIMIT = 120
HOURLY_TF = '1h'
HOURLY_LIMIT = 350
MIN_BARS_REQUIRED = 280

RSI_LENGTH = 30
RSI_MA_LENGTH = 50

# ================= ENV =================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "487817626").split(",") if cid.strip()]
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

COINGECKO_API = "https://api.coingecko.com/api/v3"

# ================= EXCHANGE =================
exchange = getattr(ccxt, EXCHANGE_ID)({
    'enableRateLimit': True,
    'timeout': 30000
})
exchange.load_markets()

market_cap_cache = {}

# ================= TELEGRAM =================
def send_telegram_message(text, chat_id=None):
    targets = [chat_id] if chat_id else TELEGRAM_CHAT_IDS
    for cid in targets:
        if not cid: continue
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    'chat_id': cid,
                    'text': text,
                    'parse_mode': 'HTML',
                    'disable_web_page_preview': True
                },
                timeout=30
            )
        except: pass

# ================= COINGECKO =================
def load_market_caps():
    global market_cap_cache
    if market_cap_cache: return
    print("📥 Loading Market Caps...")
    for page in range(1, 8):
        try:
            r = requests.get(
                f"{COINGECKO_API}/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page={page}",
                timeout=20
            )
            for coin in r.json():
                sym = str(coin.get('symbol', '')).upper()
                if sym and coin.get('market_cap'):
                    market_cap_cache[sym] = coin['market_cap']
            time.sleep(0.15)
        except: break
    print(f"✅ Loaded {len(market_cap_cache)} market caps")

# ================= PAIRS =================
def get_filtered_pairs():
    symbol_map = {}
    for symbol, info in exchange.markets.items():
        if not info.get('active') or info.get('quote') != 'USDT':
            continue
        is_spot = info.get('spot', False)
        is_future = info.get('future') or info.get('swap', False)
        
        if (SCAN_SPOT and is_spot) or (SCAN_FUTURES and is_future):
            base = symbol.split('/')[0].upper()
            if base not in symbol_map or (is_spot and not symbol_map[base][2]):
                symbol_map[base] = (symbol, info, is_spot)
    return [(sym, inf) for sym, inf, _ in symbol_map.values()]

# ================= INDICATORS =================
def fetch_ohlcv(symbol, timeframe, limit):
    try:
        data = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        if len(data) < MIN_BARS_REQUIRED:
            return None
        df = pd.DataFrame(data, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
        return df
    except Exception as e:
        if DEBUG_MODE:
            print(f"Fetch error {symbol}: {e}")
        return None

def calc_daily_ema(df):
    df = df.copy()
    df['ema30'] = df['c'].ewm(span=30, adjust=False).mean()
    df['ema50'] = df['c'].ewm(span=50, adjust=False).mean()
    return df

def calc_hourly_indicators(df):
    df = df.copy()
    
    # RSI(30)
    delta = df['c'].diff()
    gain = delta.where(delta > 0, 0).rolling(RSI_LENGTH).mean()
    loss = -delta.where(delta < 0, 0).rolling(RSI_LENGTH).mean()
    rs = gain / loss.replace(0, np.nan)
    df['rsi'] = 100 - (100 / (1 + rs))
    df['rsi'] = df['rsi'].fillna(100)
    
    df['rsi_ma'] = df['rsi'].ewm(span=RSI_MA_LENGTH, adjust=False).mean()
    df['ema200'] = df['c'].ewm(span=200, adjust=False).mean()
    
    return df

# ================= RENDERERS =================
def render_final_item(idx, item):
    risk_color = "🟢" if item['risk_pct'] < 10 else "🟡" if item['risk_pct'] < 30 else "🔴"
    return (
        f"{idx}. {escape(item['symbol'])} [{item['mkt_type']}]\n"
        f"💰 {item['price']:,.4f}\n"
        f"📊 RSI: {item['rsi']:.1f} | MA: {item['rsi_ma']:.1f} ({item['rsi_diff']:+.1f})\n"
        f"📅 EMA30/50: {item['daily_ema30']:.4f} / {item['daily_ema50']:.4f}\n"
        f"{risk_color} Risk: {item['risk_pct']:+.2f}%\n"
        f"💎 MC: {fmt_mc(item['mc'])}\n"
        f"─────────────────────\n"
    )

def fmt_mc(value):
    if not value or pd.isna(value): return "N/A"
    v = float(value)
    if v >= 1e12: return f"${v/1e12:.2f}T"
    if v >= 1e9:  return f"${v/1e9:.2f}B"
    if v >= 1e6:  return f"${v/1e6:.2f}M"
    return f"${v:,.0f}"

# ================= MAIN SCAN =================
def run_scan(pairs):
    results = []
    f1_count = f2_count = 0

    print(f"🔍 اسکن {len(pairs)} نماد...")

    for symbol, info in tqdm(pairs):
        try:
            # === فیلتر ۱: روزانه ===
            df_d = fetch_ohlcv(symbol, DAILY_TF, DAILY_LIMIT)
            if df_d is None: continue
            df_d = calc_daily_ema(df_d)
            last_d = df_d.iloc[-1]

            if last_d['ema30'] <= last_d['ema50']:   # ← تغییر مهم: > (سختگیرانه)
                continue

            f1_count += 1
            mkt = 'F' if (info.get('future') or info.get('swap')) else 'S'

            # === فیلتر ۲ و ۳: ساعتی ===
            df_h = fetch_ohlcv(symbol, HOURLY_TF, HOURLY_LIMIT)
            if df_h is None: continue
            df_h = calc_hourly_indicators(df_h)
            last_h = df_h.iloc[-1]

            if last_h['rsi'] <= last_h['rsi_ma']:
                continue
            f2_count += 1

            if not (30 <= last_h['rsi_ma'] <= 70):
                continue

            if pd.isna(last_h['ema200']) or last_h['ema200'] == 0:
                continue

            risk_pct = (last_h['c'] - last_h['ema200']) / last_h['ema200'] * 100

            results.append({
                'symbol': symbol,
                'price': last_h['c'],
                'daily_ema30': last_d['ema30'],
                'daily_ema50': last_d['ema50'],
                'rsi': last_h['rsi'],
                'rsi_ma': last_h['rsi_ma'],
                'rsi_diff': last_h['rsi'] - last_h['rsi_ma'],
                'ema200': last_h['ema200'],
                'risk_pct': risk_pct,
                'mc': market_cap_cache.get(symbol.split('/')[0].upper()),
                'mkt_type': mkt
            })

        except Exception as e:
            if DEBUG_MODE:
                print(f"Error {symbol}: {e}")

    results.sort(key=lambda x: x['risk_pct'])
    return results, f1_count, f2_count

# ================= RUN =================
def run():
    load_market_caps()
    pairs = get_filtered_pairs()
    print(f"📊 تعداد نمادهای بررسی: {len(pairs)}")

    final, f1, f2 = run_scan(pairs)

    summary = f"""
🚀 <b>اسکن سه مرحله‌ای (نسخه اصلاح‌شده)</b>
━━━━━━━━━━━━━━━━━━━━━━
کل نمادها: <code>{len(pairs)}</code>
✅ فیلتر ۱ (EMA30 > EMA50): <code>{f1}</code>
✅ فیلتر ۲ (RSI > RSI_MA): <code>{f2}</code>
✅ نهایی (RSI_MA 30-70): <code>{len(final)}</code>
━━━━━━━━━━━━━━━━━━━━━━
⏰ {jdatetime.datetime.now(pytz.timezone('Asia/Tehran')).strftime('%Y/%m/%d %H:%M')}
"""
    send_telegram_message(summary)

    # ارسال نتایج نهایی
    if final:
        header = "✅ <b>نمادهای نهایی (مرتب شده بر اساس ریسک کم به زیاد)</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
        msg = header
        for i, item in enumerate(final[:30], 1):   # حداکثر ۳۰ تا برای جلوگیری از طولانی شدن
            msg += render_final_item(i, item)
            if len(msg) > 3800:
                send_telegram_message(msg)
                msg = header
        if msg:
            send_telegram_message(msg)
    else:
        send_telegram_message("❌ هیچ نمادی تمام فیلترها را پاس نکرد.\n\n<b>نکته:</b> بازار ممکن است در شرایط نزولی باشد.")

    print("✅ اسکن تمام شد")

if __name__ == "__main__":
    send_telegram_message("🤖 اسکنر شروع شد...\nفیلتر ۱: EMA30 > EMA50")
    run()
