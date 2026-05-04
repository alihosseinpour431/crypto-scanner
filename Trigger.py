# Trigger.py
# ✅ نسخه نهایی و اصلاح‌شده: RSI on EMA (Pine Script Logic) + فیلترهای دقیق

import os, time, requests, ccxt, pandas as pd, pytz, jdatetime, numpy as np
from datetime import datetime
from html import escape
from tqdm.auto import tqdm

# ================= CONFIG =================
EXCHANGE_ID = 'xt'  # صرافی مورد نظر
SCAN_SPOT = True
SCAN_FUTURES = True
DAILY_TF, DAILY_LIMIT = '1d', 300
HOURLY_TF, HOURLY_LIMIT = '1h', 300
MIN_BARS_REQUIRED = 250

# ⚙️ تنظیمات دقیق اندیکاتورها (مطابق درخواست و عکس)
# --- تنظیمات RSI ---
RSI_LENGTH = 30              # طول دوره RSI
RSI_SOURCE_EMA_LENGTH = 14   # طول EMA برای منبع محاسبه RSI (Source = EMA 14)

# --- تنظیمات خط آبی (Smoothing) ---
# طبق منطق ارسالی شما: خط آبی یک EMA 50 است که روی مقادیر RSI اعمال می‌شود
BLUE_LINE_TYPE = "EMA"       
BLUE_LINE_LENGTH = 50        

# --- تنظیمات EMA200 (برای Risk) ---
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
    if v >= 1e9: return f" ${v/1e9:.2f}B"
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

# =========================================================
# FUNCTIONS FOR EXACT PINE SCRIPT LOGIC
# =========================================================

def pine_rma(series, length):
    """
    محاسبه RMA به سبک Wilder (دقیقاً مشابه ta.rma در Pine Script)
    فرمول: alpha = 1/length
    """
    alpha = 1.0 / length
    return series.ewm(alpha=alpha, adjust=False).mean()

def calc_hourly_indicators(df):
    """
    محاسبه دقیق اندیکاتورها طبق منطق ارسالی:
    1. Source = EMA(14) of Close
    2. RSI(30) calculated on Source using Wilder's RMA
    3. BlueLine = EMA(50) applied on the RSI values
    """
    df = df.copy()
    
    # --- Step 1: Calculate Source (EMA 14) ---
    # طبق درخواست: منبع RSI باید EMA باشد، نه قیمت بسته شدن
    rsi_source = df['c'].ewm(span=RSI_SOURCE_EMA_LENGTH, adjust=False).mean()
    
    # --- Step 2: Calculate RSI (Length 30) on Source using Wilder's RMA ---
    change = rsi_source.diff()
    
    # محاسبه Gain و Loss
    up = np.maximum(change, 0.0)
    down = np.maximum(-change, 0.0)
    
    # تبدیل به Series برای اعمال RMA
    up_series = pd.Series(up, index=df.index)
    down_series = pd.Series(down, index=df.index)
    
    # اعمال Wilder's RMA
    avg_up = pine_rma(up_series, RSI_LENGTH)
    avg_down = pine_rma(down_series, RSI_LENGTH)
    
    # محاسبه نهایی RSI
    rs = avg_up / avg_down.replace(0, np.nan)
    rsi_values = 100 - (100 / (1 + rs))
    rsi_values = rsi_values.fillna(100) # هندل کردن حالت‌های خاص
    
    df['rsi'] = rsi_values  # این همان خط بنفش (Purple) است
    
    # --- Step 3: Calculate Blue Line (EMA 50 on RSI) ---
    # طبق منطق ارسالی: خط آبی یک EMA است که روی خودِ RSI اعمال می‌شود
    if BLUE_LINE_TYPE == "EMA":
        df['rsi_ma'] = df['rsi'].ewm(span=BLUE_LINE_LENGTH, adjust=False).mean()
    elif BLUE_LINE_TYPE == "SMA":
        df['rsi_ma'] = df['rsi'].rolling(BLUE_LINE_LENGTH).mean()
    else:
        # پیش‌فرض EMA
        df['rsi_ma'] = df['rsi'].ewm(span=BLUE_LINE_LENGTH, adjust=False).mean()
        
    # --- Step 4: Calculate Risk (Distance from EMA 200) ---
    df['ema200'] = df['c'].ewm(span=EMA200_LENGTH, adjust=False).mean()
    df['risk'] = (df['c'] - df['ema200']) / df['ema200'] * 100
    
    return df

def scan_with_three_filters(pairs):
    results = []
    total = len(pairs)
    d_pass = r_pass = rsi_pass = 0
    
    print(f"🔍 شروع اسکن روی {total} نماد...")
    
    for symbol, info in tqdm(pairs, desc="Scanning", total=total):
        try:
            # --- 🛡️ فیلتر ۱: روزانه (EMA30 > EMA50) ---
            df_d = fetch_ohlcv(symbol, DAILY_TF, DAILY_LIMIT)
            if df_d is None: continue
            df_d = calc_daily_ema(df_d)
            last_d = df_d.iloc[-1]
            
            # چک کردن NaN و شرط صعودی بودن
            if not (pd.notna(last_d['ema30']) and pd.notna(last_d['ema50']) and last_d['ema30'] > last_d['ema50']):
                continue
            d_pass += 1

            # --- 🛡️ فیلتر ۲ و ۳: ساعتی (منطق دقیق Pine Script) ---
            df_h = fetch_ohlcv(symbol, HOURLY_TF, HOURLY_LIMIT)
            if df_h is None: continue
            df_h = calc_hourly_indicators(df_h)
            last_h = df_h.iloc[-1]

            # استخراج مقادیر نهایی
            rsi_val = last_h['rsi']      # خط بنفش (Purple)
            rsi_ma_val = last_h['rsi_ma'] # خط آبی (Blue)

            # 🛡️ فیلتر ۲: خط بنفش > خط آبی
            # اگر هر کدام NaN بودند، رد کن
            if not (pd.notna(rsi_val) and pd.notna(rsi_ma_val) and rsi_val > rsi_ma_val):
                continue
            r_pass += 1

            # 🛡️ فیلتر ۳: محدوده امن (30 < RSI < 70)
            # طبق درخواست: فقط آن‌هایی که RSI (خط بنفش) بین ۳۰ و ۷۰ است نمایش داده شود
            if not (pd.notna(rsi_val) and 30 < rsi_val < 70):
                continue
            rsi_pass += 1

            # محاسبه Risk برای مرتب‌سازی
            risk_val = last_h['risk'] if pd.notna(last_h['risk']) else 0.0

            # تعیین نوع بازار
            mkt = 'F' if (info.get('future') or info.get('swap')) else 'S'
            
            results.append({
                'symbol': symbol,
                'price': last_h['c'],
                'rsi': rsi_val,          # Purple
                'rsi_ma': rsi_ma_val,    # Blue
                'risk': risk_val,
                'mc': get_market_cap(symbol),
                'mkt_type': mkt,
                'info': info
            })
        except Exception as e:
            if DEBUG_MODE: print(f"⚠️ Error {symbol}: {e}")
        time.sleep(0.02) # جلوگیری از بن شدن IP

    final_pass = len(results)
    
    # مرتب‌سازی صعودی بر اساس Risk (کمترین ریسک اول)
    results.sort(key=lambda x: x['risk'])

    print("\n" + "="*50)
    print(f"📊 گزارش فیلترها:")
    print(f"   کل نمادها: {total}")
    print(f"   ✅ فیلتر ۱ (Daily EMA): {d_pass}")
    print(f"   ✅ فیلتر ۲ (Purple > Blue): {r_pass}")
    print(f"   ✅ فیلتر ۳ (30 < RSI < 70): {rsi_pass}")
    print(f"   🎯 خروجی نهایی: {final_pass}")
    print("="*50)

    stats = {'daily': d_pass, 'rsi_cross': r_pass, 'rsi_range': rsi_pass, 'final': final_pass}
    return results, stats

def build_message(signals, total, d_pass, r_pass, rsi_pass, final_pass):
    now = jdatetime.datetime.now(pytz.timezone('Asia/Tehran')).strftime('%Y/%m/%d %H:%M:%S')
    
    header = (
        f"🎯 <b>گزارش اسکن | RSI on EMA (Exact Logic)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 کل نمادها: <code>{total}</code>\n"
        f"├─ ✅ فیلتر ۱ (Daily EMA30&gt;50): <code>{d_pass}</code>\n"
        f"├─ ✅ فیلتر ۲ (Purple&gt;Blue): <code>{r_pass}</code>\n"
        f"├─ ✅ فیلتر ۳ (30 &lt; RSI &lt; 70): <code>{rsi_pass}</code>\n"
        f"└─ 🎯 سیگنال نهایی: <code>{final_pass}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f" ⚙️ تنظیمات محاسباتی:\n"
        f" ├─ منبع RSI: EMA(14) قیمت\n"
        f" ├─ RSI Length: 30 (Wilder's RMA)\n"
        f" ├─ خط آبی: EMA(50) روی RSI\n"
        f" └─ مرتب‌سازی: بر اساس کمترین Risk (فاصله از EMA200)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    footer = f"\n⏰ {now} 🇮\n🤖 AlphaScanner v6.0 (PineLogic)"

    msgs = []
    body = ""
    MAX = 4000
    
    if signals:
        for r, s in enumerate(signals, 1):
            risk_str = f"{s['risk']:+.2f}%" if pd.notna(s['risk']) else "N/A"
            
            # نمایش دقیق مقادیر برای اطمینان کاربر
            card = (
                f"{r}. {escape(s['symbol'])} [{s['mkt_type']}]\n"
                f"💰 Price: {s['price']:,.4f} USDT\n"
                f"🟣 Purple (RSI): <b>{s['rsi']:.2f}</b>\n"
                f"🔵 Blue (EMA50): {s['rsi_ma']:.2f}\n"
                f"⚠️ Risk: {risk_str}\n"
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
    print("🚀 شروع اسکن...")
    load_market_caps()
    pairs = get_filtered_pairs()
    print(f"📊 کل نمادهای فعال: {len(pairs)}")

    results, stats = scan_with_three_filters(pairs)
    print(f"✅ اسکن پایان یافت: {stats['final']} نماد")

    msgs = build_message(results, len(pairs), stats['daily'], stats['rsi_cross'], stats['rsi_range'], stats['final'])
    print(f"📨 تعداد پیام‌ها: {len(msgs)}")
    for msg in msgs:
        send_telegram_message(msg)
        time.sleep(0.3)
    print("✅ پایان کامل اسکن")
    return results

if __name__ == "__main__":
    start_msg = (
        "🤖 <b>اسکن RSI on EMA (Pine Logic) شروع شد</b>\n"
        "📅 روزانه: EMA30 &gt; EMA50\n"
        "⏰ ساعتی: RSI(30) on EMA(14) &gt; EMA(50)\n"
        "⚖️ محدوده RSI: 30 &lt; RSI &lt; 70\n"
        "📉 متد: Wilder's RMA\n"
        "📏 مرتب‌سازی بر اساس Risk"
    )
    send_telegram_message(start_msg)
    run()
    send_telegram_message("✅ <b>اسکن با موفقیت پایان یافت</b>")
