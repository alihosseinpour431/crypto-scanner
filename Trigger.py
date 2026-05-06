
# ✅ اسکنر بازار کریپتو - صرافی XT با فیلترهای جدید
# فیلتر ۱: روزانه - EMA30 > EMA50
# فیلتر ۲: ساعتی - EMA30 > EMA50 > EMA200
# فیلتر ۳: ریسک - (EMA50 - EMA200) / EMA200 * 100 => بین 0 تا 10 درصد (مثبت)

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

# تنظیمات ریسک
MIN_RISK = 0.0
MAX_RISK = 8.0

# ================= ENV & SECURITY =================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
CMC_API_KEY = os.getenv("CMC_PRO_API_KEY", "39478549b7c94ee093d0f3cbe43a39e9")
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
        df['volume'] = df['volume'].astype(float)

        return df
    except Exception as e:
        if DEBUG_MODE:
            print(f"⚠️ Fetch error {symbol}: {e}")
        return None


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
            'Accepts': 'application/json'  # با s
        }

        resp = requests.get(url, params=params, headers=headers, timeout=5)
        resp.raise_for_status()  # ✅ بررسی خطاهای HTTP
        
        data = resp.json()
        
        # ✅ اصلاح ۳: بررسی ساختار پاسخ مثل کد کارآمد
        if "data" in data and symbol_base.upper() in data["data"]:
            coin_data = data["data"][symbol_base.upper()]
            
            # استخراج مارکت‌کپ از ساختار quote -> USD
            market_cap = coin_data.get("quote", {}).get("USD", {}).get("market_cap")
            
            if market_cap is not None and market_cap > 0:
                return float(market_cap)
        
        return None
        
    except requests.exceptions.HTTPError as e:
        if DEBUG_MODE:
            print(f"⚠️ CMC HTTP Error for {symbol_base}: {e}")
            if e.response is not None:
                print(f"   Status: {e.response.status_code} | Body: {e.response.text[:200]}")
        return None
    except KeyError as e:
        if DEBUG_MODE:
            print(f"⚠️ CMC KeyError for {symbol_base}: {e}")
        return None
    except Exception as e:
        if DEBUG_MODE:
            print(f"⚠️ CMC unexpected error for {symbol_base}: {e}")
        return None

# ================= SCAN FUNCTION =================
def scan_market(pairs):
    """
    اسکن بازار با سه فیلتر:
    ۱. روزانه: EMA30 > EMA50
    ۲. ساعتی: EMA30 > EMA50 > EMA200
    ۳. ریسک: (EMA50 - EMA200) / EMA200 * 100 => بین 0 تا 10 درصد (مثبت)
    """
    results = []
    total = len(pairs)

    print(f"🔍 شروع اسکن {total} نماد...")
    print(f"   فیلتر ۱: EMA30 > EMA50 (روزانه)")
    print(f"   فیلتر ۲: EMA30 > EMA50 > EMA200 (ساعتی)")
    print(f"   فیلتر ۳: Risk% = (EMA50 - EMA200) / EMA200 * 100 => بین {MIN_RISK} تا {MAX_RISK} درصد (مثبت)")
    print("-" * 50)

    for idx, (symbol, info) in enumerate(tqdm(pairs, desc="Scanning", total=total), 1):
        try:
            # ========== فیلتر ۱: روزانه ==========
            df_daily = fetch_ohlcv(symbol, DAILY_TF, DAILY_LIMIT)

            if df_daily is None:
                continue

            # محاسبه EMA30 و EMA50 برای روزانه
            df_daily['ema30'] = df_daily['close'].ewm(span=30, adjust=False).mean()
            df_daily['ema50'] = df_daily['close'].ewm(span=50, adjust=False).mean()

            last_daily = df_daily.iloc[-1]

            # بررسی مقادیر NaN
            if pd.isna(last_daily['close']) or pd.isna(last_daily['ema30']) or pd.isna(last_daily['ema50']):
                continue

            # شرط ۱: EMA30 > EMA50 در روزانه
            if not (last_daily['ema30'] > last_daily['ema50']):
                continue

            # ========== فیلتر ۲: ساعتی ==========
            df_hourly = fetch_ohlcv(symbol, HOURLY_TF, HOURLY_LIMIT)

            if df_hourly is None:
                continue

            # محاسبه EMA30, EMA50, EMA200 برای ساعتی
            df_hourly['ema30'] = df_hourly['close'].ewm(span=30, adjust=False).mean()
            df_hourly['ema50'] = df_hourly['close'].ewm(span=50, adjust=False).mean()
            df_hourly['ema200'] = df_hourly['close'].ewm(span=200, adjust=False).mean()

            last_hourly = df_hourly.iloc[-1]

            # بررسی مقادیر NaN
            if pd.isna(last_hourly['close']) or pd.isna(last_hourly['ema30']) or \
               pd.isna(last_hourly['ema50']) or pd.isna(last_hourly['ema200']):
                continue

            # شرط ۲: EMA30 > EMA50 > EMA200 در ساعتی
            if not (last_hourly['ema30'] > last_hourly['ema50'] > last_hourly['ema200']):
                continue

            # ========== فیلتر ۳: محاسبه ریسک ==========
            # Risk% = (EMA50 - EMA200) / EMA200 * 100
            # فقط مقادیر مثبت (یعنی EMA50 > EMA200)
            risk_pct = ((last_hourly['ema50'] - last_hourly['ema200']) / last_hourly['ema200']) * 100

            # شرط ۳: ریسک بین 0 تا 10 درصد (مثبت)
            if not (MIN_RISK <= risk_pct <= MAX_RISK):
                continue

         
            # ========== محاسبه Volume Ratio (مقایسه میانگین‌ها) ==========
            avg_5h = df_hourly['volume'].iloc[-5:].mean()   # میانگین حجم ۵ ساعت اخیر
            avg_200h = df_hourly['volume'].iloc[-200:].mean() # میانگین حجم ۲۰۰ ساعت اخیر
            
            if avg_200h > 0 and not np.isnan(avg_200h):
                volume_ratio = avg_5h / avg_200h          # نسبت: مثلاً 2.5 یعنی ۲.۵ برابر میانگین
                volume_change_pct = (volume_ratio - 1) * 100  # درصد تغییر: مثلاً +150%
            else:
                volume_ratio = 0
                volume_change_pct = 0
            
            # ذخیره در دیکشنری (کلید v_alpha را نگه می‌داریم تا کدهای بعدی خراب نشوند)
            # اما مقدار آن را همان ratio قرار می‌دهیم
            v_alpha = volume_ratio 

            # ========== دریافت مارکت کپ ==========
            symbol_base = symbol.split('/')[0]
          
            market_cap = get_market_cap_from_cmc(symbol_base)
            # اگر مارکت کپ پیدا نشد، محاسبه تقریبی
            if market_cap is None:
                # برای صرافی XT، اطلاعات circulating supply موجود نیست
                # بنابراین از محاسبه تقریبی استفاده نمی‌کنیم و None می‌گذاریم
                market_cap = None

            # ✅ همه فیلترها پاس شدند
            mkt_type = 'F' if (info.get('future') or info.get('swap')) else 'S'

            results.append({
                'symbol': symbol,
                'symbol_base': symbol_base,
                'price': last_hourly['close'],
                'risk_pct': risk_pct,
                'v_alpha': v_alpha,
                'market_cap': market_cap,
                'mkt_type': mkt_type,
                'info': info
            })

        except Exception as e:
            if DEBUG_MODE:
                print(f"⚠️ Error {symbol}: {e}")

        # تأخیر کوتاه برای رعایت rate limit
        time.sleep(0.01)

    # سورت بر اساس ریسک (کم به زیاد)
    results.sort(key=lambda x: x['risk_pct'])

    return results

# ================= MESSAGE BUILDER =================

def build_card_messages(signals, total_scanned):  
    """ساخت پیام تلگرام"""
    now = datetime.now().strftime('%Y/%m/%d %H:%M:%S')

    header = (
        f"🔍 <b>اسکنر XT | فیلتر ترکیبی جدید</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 نمادهای بررسی شده: <code>{total_scanned}</code>\n"
        f"✅ عبور کرده: <code>{len(signals)}</code>\n"
        f"📋 شرایط:\n"
        f" ├─ ۱) EMA30 > EMA50 (روزانه)\n"
        f" ├─ ۲) EMA30 > EMA50 > EMA200 (ساعتی)\n"
        f" └─ ۳) Risk% = (EMA50 - EMA200) / EMA200 * 100 => 0-10% (مثبت)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    footer = f"\n⏰ {now} 🇮🇷\n🤖 XT Scanner v3.0"

    msgs = []
    body = ""
    MAX = 4000

    for r, s in enumerate(signals, 1):
        # ساخت لینک TradingView
        tv_symbol = s['symbol'].replace('/', '')
        tv_link = f"https://www.tradingview.com/chart/?symbol=:{tv_symbol}"

        # فرمت کردن مارکت کپ
        if s['market_cap'] is not None:
            if s['market_cap'] >= 1e9:
                mc_str = f"${s['market_cap']/1e9:.2f}B"
            elif s['market_cap'] >= 1e6:
                mc_str = f"${s['market_cap']/1e6:.2f}M"
            else:
                mc_str = f"${s['market_cap']:,.0f}"
        else:
            mc_str = "N/A"

        
        # اگر volume_ratio > 1.5 باشد (جهش حجم)، با ایموجی 🔥 نمایش بده
        if s['v_alpha'] > 1.5:
            vol_emoji = "🔥"
            vol_text = f"{s['v_alpha']:.2f}x"  # نمایش به صورت ضریب
        elif s['v_alpha'] > 1.0:
            vol_emoji = "📈"
            vol_text = f"{s['v_alpha']:.2f}x"
        else:
            vol_emoji = "📉"
            vol_text = f"{s['v_alpha']:.2f}x"
        
        card = (
            f"{r}. <a href='{tv_link}'>{escape(s['symbol'])}</a> [{s['mkt_type']}]\n"
            f"💰 Price: {s['price']:,.6f} USDT\n"
            f"⚠️ Risk: {s['risk_pct']:.2f}%\n"
            f"{vol_emoji} Vol Ratio: <b>{vol_text}</b>\n"  # نمایش جدید
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

# ================= NEW: 3 SORTED MESSAGES BUILDER =================
def build_sorted_summary_messages(signals, total_scanned):
    """
    ساخت ۳ پیام جداگانه برای نمایش ۴۰ مورد اول بر اساس:
    ۱. ریسک (کم به زیاد)
    ۲. حجم/آلفا (کم به زیاد)
    ۳. مارکت‌کپ (زیاد به کم)
    """
    from html import escape
    
    if not signals:
        return []
    
    messages = []
    now = datetime.now().strftime('%Y/%m/%d %H:%M:%S')
    LIMIT = 40  # ✅ نمایش حداقل ۴۰ مورد
    
    # --- پیام ۱: سورت بر اساس ریسک (کم ➡ زیاد) ---
    sorted_risk = sorted(signals, key=lambda x: x['risk_pct'])[:LIMIT]
    msg1 = f"📊 <b>۴۰ نماد برتر | سورت بر اساس ریسک (کم ➡ زیاد)</b>\n"
    msg1 += f"<code>🔹 Total Scanned: {total_scanned} | Showing: {len(sorted_risk)}\n"
    msg1 += "Rank | Symbol   | Risk%  | Price\n"
    msg1 += "-----+----------+--------+----------\n"
    
    for i, s in enumerate(sorted_risk, 1):
        tv_link = f"https://www.tradingview.com/chart/?symbol=:{s['symbol'].replace('/', '')}"
        symbol_link = f"<a href='{tv_link}'>{escape(s['symbol_base'])}</a>"
        msg1 += f"{i:4} | {symbol_link:<8} | {s['risk_pct']:6.2f}% | {s['price']:,.6f}\n"
    msg1 += f"</code>\n⏰ {now} 🇮🇷"
    messages.append(msg1)
    
    # --- پیام ۲: سورت بر اساس Volume Ratio (کم ➡ زیاد) ---
    sorted_vol = sorted(signals, key=lambda x: x['v_alpha'])[:LIMIT]
    msg2 = f"📈 <b>۴۰ نماد برتر | سورت بر اساس حجم (کم ➡ زیاد)</b>\n"
    msg2 += f"<code>🔹 Total Scanned: {total_scanned} | Showing: {len(sorted_vol)}\n"
    msg2 += "Rank | Symbol   | V_Alpha | Price\n"
    msg2 += "-----+----------+---------+----------\n"
    
    for i, s in enumerate(sorted_vol, 1):
        tv_link = f"https://www.tradingview.com/chart/?symbol=:{s['symbol'].replace('/', '')}"
        symbol_link = f"<a href='{tv_link}'>{escape(s['symbol_base'])}</a>"
        # ایموجی برای حجم‌های بالا
        vol_mark = " 🔥" if s['v_alpha'] > 2.0 else ""
        msg2 += f"{i:4} | {symbol_link:<8} | {s['v_alpha']:7.2f}x{vol_mark} | {s['price']:,.6f}\n"
    msg2 += f"</code>\n⏰ {now} 🇮🇷"
    messages.append(msg2)
    
    # --- پیام ۳: سورت بر اساس مارکت‌کپ (زیاد ➡ کم) ---
    # فیلتر کردن نمادهایی که مارکت‌کپ معتبر دارند
    valid_mc = [s for s in signals if s.get('market_cap') and s['market_cap'] > 0]
    sorted_mc = sorted(valid_mc, key=lambda x: x['market_cap'], reverse=True)[:LIMIT]
    
    msg3 = f"🏛️ <b>۴۰ نماد برتر | سورت بر اساس مارکت‌کپ (زیاد ➡ کم)</b>\n"
    msg3 += f"<code>🔹 Valid MC: {len(valid_mc)} | Showing: {len(sorted_mc)}\n"
    msg3 += "Rank | Symbol   | MarketCap | Price\n"
    msg3 += "-----+----------+-----------+----------\n"
    
    for i, s in enumerate(sorted_mc, 1):
        tv_link = f"https://www.tradingview.com/chart/?symbol=:{s['symbol'].replace('/', '')}"
        symbol_link = f"<a href='{tv_link}'>{escape(s['symbol_base'])}</a>"
        # فرمت‌بندی مارکت‌کپ
        mc = s['market_cap']
        if mc >= 1e9: mc_str = f"${mc/1e9:.2f}B"
        elif mc >= 1e6: mc_str = f"${mc/1e6:.2f}M"
        else: mc_str = f"${mc:,.0f}"
        
        msg3 += f"{i:4} | {symbol_link:<8} | {mc_str:>9} | {s['price']:,.6f}\n"
    
    if not sorted_mc:
        msg3 += "\n⚠️ هیچ داده‌ی مارکت‌کپی یافت نشد."
        
    msg3 += f"</code>\n⏰ {now} 🇮🇷"
    messages.append(msg3)
    
    return messages

# ================= TABLE SUMMARY BUILDER =================
def build_table_summary(signals):
    """ساخت پیام خلاصه جدولی - بعد از کارت‌ها"""
    if not signals:
        return None

    # آماده‌سازی دیتا
    data = []
    for s in signals:
        tv_symbol = s['symbol'].replace('/', '')
        link = f"https://www.tradingview.com/chart/?symbol=:{tv_symbol}"
        symbol_link = f"<a href='{link}'>{escape(s['symbol_base'])}</a>"
        data.append({
            'symbol': symbol_link,
            'risk': s['risk_pct'],
            'vol': s['v_alpha']
        })

    # جدول ۱: سورت بر اساس ریسک (کم ➡ زیاد)
    tbl_risk = "<b>📋 خلاصه جدولی | سورت بر اساس ریسک</b>\n"
    tbl_risk += "<code>🔹 Risk: Low → High\n"
    tbl_risk += "Rank | Symbol   | Risk% | Vol\n"
    tbl_risk += "-----+----------+-------+------\n"
    
    for i, row in enumerate(sorted(data, key=lambda x: x['risk'])[:12], 1):
        tbl_risk += f"{i:4} | {row['symbol']:<8} | {row['risk']:5.2f} | {row['vol']:4.2f}x\n"
    tbl_risk += "</code>"

    # جدول ۲: سورت بر اساس حجم (زیاد ➡ کم)
    tbl_vol = "\n<b>🔥 سورت بر اساس حجم</b>\n"
    tbl_vol += "<code>🔹 Volume: High → Low\n"
    tbl_vol += "Rank | Symbol   | Risk% | Vol\n"
    tbl_vol += "-----+----------+-------+------\n"
    
    for i, row in enumerate(sorted(data, key=lambda x: x['vol'], reverse=True)[:12], 1):
        mark = "🔥" if row['vol'] > 2.0 else ""
        tbl_vol += f"{i:4} | {row['symbol']:<8} | {row['risk']:5.2f} | {row['vol']:4.2f}x{mark}\n"
    tbl_vol += "</code>"

    return tbl_risk + tbl_vol

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
            'disable_web_page_preview': False  # فعال کردن لینک‌ها
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
    print("🚀 شروع اسکنر XT با فیلترهای جدید...")

    # دریافت لیست جفت‌ارزها
    pairs = get_filtered_pairs()
    print(f"📊 کل نمادهای فعال (بدون تکرار): {len(pairs)}")

    # اسکن بازار
    results = scan_market(pairs)

    print(f"\n✅ اسکن پایان یافت: {len(results)} نماد پیدا شد")

    # نمایش نتایج در کنسول
    if results:
        print("\n" + "=" * 60)
        print("🎯 نمادهای پیدا شده (مرتب شده بر اساس ریسک):")
        print("=" * 60)
        for i, r in enumerate(results, 1):
            mc_str = f"${r['market_cap']:,.0f}" if r['market_cap'] else "N/A"
            print(f"\n{i}. {r['symbol']} [{r['mkt_type']}]")
            print(f"   Price: {r['price']:,.6f}")
            print(f"   ⚠️ Risk: {r['risk_pct']:.2f}%")
            print(f"   📊 V_alpha: {r['v_alpha']:.2f}")
            print(f"   🏛️ Market Cap: {mc_str}")
        print("=" * 60)

    # ارسال به تلگرام
      # ========== ارسال به تلگرام ==========
    if TELEGRAM_CHAT_IDS:
        print("\n📤 ارسال نتایج به تلگرام...")
        
        # ۱️⃣ اول: ارسال کارت‌های کامل (همان کد قبلی)
        card_msgs = build_card_messages(results, len(pairs))  # ✅ نام جدید تابع
        for msg in card_msgs:
            send_telegram_message(msg)
            time.sleep(0.3)
        

    # ========== ارسال به تلگرام ==========
    if TELEGRAM_CHAT_IDS:
        print("\n📤 ارسال نتایج به تلگرام...")
        
        # ۱️⃣ ارسال کارت‌های کامل اصلی (کد قبلی)
        card_msgs = build_card_messages(results, len(pairs))
        for msg in card_msgs:
            send_telegram_message(msg)
            time.sleep(0.3)
        
        # ✅ ۲️⃣ ارسال ۳ پیام جدیدِ تفکیک‌شده (جدید)
        print("🔄 در حال ساخت پیام‌های تفکیک‌شده...")
        sorted_msgs = build_sorted_summary_messages(results, len(pairs))
        
        for i, msg in enumerate(sorted_msgs, 1):
            print(f"   📤 ارسال پیام شماره {i}...")
            send_telegram_message(msg)
            time.sleep(0.5)  # مکث برای جلوگیری از ریت‌لیمیت تلگرام
            
        print("✅ همه پیام‌ها (اصلی + ۳ پیام جدید) ارسال شدند")

# ================= RUN =================
if __name__ == "__main__":
    run()
