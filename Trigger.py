# Trigger.py
# ✅ نسخه نهایی با مرتب‌سازی ریسک و فاصله از EMA200 ساعتی

import os, time, requests, ccxt, pandas as pd, pytz, jdatetime, numpy as np
from datetime import datetime
from html import escape
from tqdm.auto import tqdm

# ================= CONFIG =================
EXCHANGE_ID = 'xt'
SCAN_SPOT = True
SCAN_FUTURES = True
DAILY_TF, DAILY_LIMIT = '1d', 300
HOURLY_TF, HOURLY_LIMIT = '1h', 300
MIN_BARS_REQUIRED = 250
RSI_LENGTH = 30
RSI_EMA_LENGTH = 50
EMA200_LENGTH = 200

# ================= ENV =================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN: raise ValueError("❌ TELEGRAM_BOT_TOKEN not set!")
TELEGRAM_CHAT_IDS = [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "487817626").split(",") if c.strip()]
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
COINGECKO_API = "https://api.coingecko.com/api/v3"

# ================= EXCHANGE INIT =================
exchange = getattr(ccxt, EXCHANGE_ID)({'enableRateLimit': True, 'timeout': 30000})
exchange_markets = exchange.load_markets()

# ================= CACHE =================
market_cap_cache = {}

def send_telegram_message(text):
    for cid in TELEGRAM_CHAT_IDS:
        try:
            r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                              json={'chat_id': cid, 'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True},
                              timeout=30)
            if r.status_code == 200: print(f"✅ پیام به {cid} ارسال شد.")
            else: print(f"⚠️ Telegram Error ({cid}): {r.status_code} | {r.text[:200]}")
        except Exception as e: print(f"❌ Telegram Exception ({cid}): {e}")

def load_market_caps():
    if market_cap_cache: return
    print("📥 Loading Market Cap...")
    sess = requests.Session()
    for page in range(1, 11):
        try:
            data = sess.get(f"{COINGECKO_API}/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page={page}&sparkline=false").json()
            if not isinstance(data, list) or not data: break
            for c in data:
                sym = str(c.get('symbol','')).upper()
                mc = c.get('market_cap')
                if sym and isinstance(mc, (int,float)): market_cap_cache[sym] = mc
            if len(data) < 250: break
            time.sleep(0.1)
        except: break
    print(f"✅ Market Cap loaded: {len(market_cap_cache)} symbols")

def get_market_cap(symbol): return market_cap_cache.get(symbol.split('/')[0].upper())

def fmt_mc(v):
    if v is None or (isinstance(v, float) and np.isnan(v)): return "🔸 N/A"
    v = float(v)
    if v >= 1e12: return f"💎 ${v/1e12:.2f}T"
    if v >= 1e9: return f"💎 ${v/1e9:.2f}B"
    if v >= 1e6: return f"💎 ${v/1e6:.2f}M"
    return f"💎 ${v:,.0f}"

def get_filtered_pairs():
    smap = {}
    for sym, inf in exchange_markets.items():
        if not inf.get('active') or inf.get('quote') != 'USDT': continue
        spot = inf.get('spot', False)
        fut = inf.get('future', False) or inf.get('swap', False)
        if (SCAN_SPOT and spot) or (SCAN_FUTURES and fut):
            base = sym.split('/')[0].upper()
            if base not in smap: smap[base] = (sym, inf, spot)
            elif spot and not smap[base][2]: smap[base] = (sym, inf, True)
    return [(s, i) for s, i, _ in smap.values()]

def fetch_ohlcv(symbol, tf, limit):
    try:
        data = exchange.fetch_ohlcv(symbol, tf, limit)
        if len(data) < MIN_BARS_REQUIRED: return None
        df = pd.DataFrame(data, columns=['ts','o','h','l','c','v'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
        return df
    except: return None

def calc_daily_ema(df):
    df = df.copy()
    df['ema30'] = df['c'].ewm(span=30, adjust=False).mean()
    df['ema50'] = df['c'].ewm(span=50, adjust=False).mean()
    return df

def calc_hourly_indicators(df):
    df = df.copy()
    # RSI
    delta = df['c'].diff()
    gain = delta.where(delta>0, 0).rolling(RSI_LENGTH).mean()
    loss = -delta.where(delta<0, 0).rolling(RSI_LENGTH).mean()
    rs = gain / loss.replace(0, np.nan)
    df['rsi'] = 100 - (100/(1+rs))
    df['rsi'] = df['rsi'].fillna(100)
    # RSI_MA
    df['rsi_ma'] = df['rsi'].ewm(span=RSI_EMA_LENGTH, adjust=False).mean()
    # EMA200
    df['ema200'] = df['c'].ewm(span=EMA200_LENGTH, adjust=False).mean()
    # Distance %
    df['dist_ema200'] = (df['c'] - df['ema200']) / df['ema200'] * 100
    return df

def scan_with_three_filters(pairs):
    results = []
    total = len(pairs)
    d_pass = r_pass = 0
    print(f"🔍 شروع اسکن با ۳ فیلتر و امتیاز ریسک روی {total} نماد...")
    for symbol, info in tqdm(pairs, desc="Scanning", total=total):
        try:
            # --- فیلتر ۱: روزانه ---
            df_d = fetch_ohlcv(symbol, DAILY_TF, DAILY_LIMIT)
            if df_d is None: continue
            df_d = calc_daily_ema(df_d)
            last_d = df_d.iloc[-1]
            if not (pd.notna(last_d['ema30']) and pd.notna(last_d['ema50']) and last_d['ema30'] > last_d['ema50']):
                continue
            d_pass += 1

            # --- فیلتر ۲ و ۳: ساعتی ---
            df_h = fetch_ohlcv(symbol, HOURLY_TF, HOURLY_LIMIT)
            if df_h is None: continue
            df_h = calc_hourly_indicators(df_h)
            last_h = df_h.iloc[-1]

            # فیلتر ۲: RSI > RSI_MA
            if not (pd.notna(last_h['rsi']) and pd.notna(last_h['rsi_ma']) and last_h['rsi'] > last_h['rsi_ma']):
                continue
            r_pass += 1

            # فیلتر ۳: 30 < RSI_MA < 70
            rsi_ma = last_h['rsi_ma']
            if not (pd.notna(rsi_ma) and 30 < rsi_ma < 70):
                continue

            # محاسبه فاصله از EMA200
            dist = last_h['dist_ema200'] if pd.notna(last_h['dist_ema200']) else 0.0

            mkt = 'F' if (info.get('future') or info.get('swap')) else 'S'
            results.append({
                'symbol': symbol,
                'price': last_h['c'],
                'rsi': last_h['rsi'],
                'rsi_ma': rsi_ma,
                'dist_ema200': dist,
                'mc': get_market_cap(symbol),
                'mkt_type': mkt,
                'info': info
            })
        except Exception as e:
            if DEBUG_MODE: print(f"⚠️ Error {symbol}: {e}")
        time.sleep(0.02)

    final_pass = len(results)
    # مرتب‌سازی صعودی بر اساس dist_ema200 (کمترین ریسک → بیشترین ریسک)
    results.sort(key=lambda x: x['dist_ema200'])

    print("\n" + "="*50)
    print(f"📊 نتیجه فیلترها:")
    print(f"   کل: {total}")
    print(f"   ✅ فیلتر ۱ (EMA روزانه): {d_pass}")
    print(f"   ✅ فیلتر ۲ (RSI>RSI_MA): {r_pass}")
    print(f"   ✅ فیلتر ۳ (30<RSI_MA<70): {final_pass} (سیگنال نهایی)")
    print("="*50)

    stats = {'daily': d_pass, 'rsi': r_pass, 'final': final_pass}
    return results, stats

def build_message(signals, total, d_pass, r_pass, final_pass):
    now = jdatetime.datetime.now(pytz.timezone('Asia/Tehran')).strftime('%Y/%m/%d %H:%M:%S')
    header = (
        f"🎯 <b>گزارش نهایی اسکن | ۳ فیلتر + ریسک</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 کل نمادها: <code>{total}</code>\n"
        f"├─ ✅ فیلتر ۱ (EMA): <code>{d_pass}</code>\n"
        f"├─ ✅ فیلتر ۲ (RSI): <code>{r_pass}</code>\n"
        f"└─ ✅ فیلتر ۳ (تعادل): <code>{final_pass}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 شرایط:\n"
        f" ├─ 1️⃣ روزانه: EMA30 &gt; EMA50 ✅\n"
        f" ├─ 2️⃣ ساعتی: RSI(30) &gt; RSI_MA ✅\n"
        f" └─ 3️⃣ ساعتی: 30 &lt; RSI_MA &lt; 70 ✅\n"
        f" ⚡ مرتب‌سازی: بر اساس کمترین فاصله از EMA200 (ریسک کم)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    footer = f"\n⏰ {now} 🇮🇷\n🤖 AlphaScanner v3.0"

    msgs = []
    body = ""
    MAX = 4000
    if signals:
        for r, s in enumerate(signals, 1):
            dist_str = f"{s['dist_ema200']:+.2f}%" if pd.notna(s['dist_ema200']) else "N/A"
            card = (
                f"{r}. {escape(s['symbol'])} [{s['mkt_type']}]\n"
                f"💰 {s['price']:,.4f} USDT\n"
                f"📊 RSI: {s['rsi']:.1f} | RSI_MA: {s['rsi_ma']:.1f}\n"
                f"📏 فاصله از EMA200: {dist_str}\n"
                f"💎 MC: {fmt_mc(s['mc'])}\n"
                f"─────────────────────\n"
            )
            if len(header)+len(body)+len(card)+len(footer) > MAX-100:
                msgs.append(header+body+footer)
                body = card
            else:
                body += card
        if body.strip(): msgs.append(header+body+footer)
    else:
        msgs.append(f"{header}❌ هیچ نمادی هر سه فیلتر را پاس نکرد.{footer}")
    return msgs

def run():
    print("🚀 شروع اسکن با فیلترها و معیار ریسک...")
    load_market_caps()
    pairs = get_filtered_pairs()
    print(f"📊 کل نمادهای فعال: {len(pairs)}")

    results, stats = scan_with_three_filters(pairs)
    print(f"✅ اسکن پایان یافت: {stats['final']} نماد")

    msgs = build_message(results, len(pairs), stats['daily'], stats['rsi'], stats['final'])
    print(f"📨 تعداد پیام‌ها: {len(msgs)}")
    for msg in msgs:
        send_telegram_message(msg)
        time.sleep(0.3)
    print("✅ پایان کامل اسکن")
    return results

if __name__ == "__main__":
    start_msg = (
        "🤖 <b>اسکن ۳ فیلتری + ریسک شروع شد</b>\n"
        "📅 روزانه: EMA30 &gt; EMA50\n"
        "⏰ ساعتی: RSI &gt; RSI_MA\n"
        "⚖️ اشباع: 30 &lt; RSI_MA &lt; 70\n"
        "📏 مرتب‌سازی بر اساس فاصله از EMA200 (کمترین ریسک)"
    )
    send_telegram_message(start_msg)
    run()
    send_telegram_message("✅ <b>اسکن با موفقیت پایان یافت</b>")
