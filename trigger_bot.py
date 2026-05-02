#!/usr/bin/env python3
import os, requests, logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ================= CONFIG =================
# ⚠️ توکن‌ها را از Environment Variable می‌خوانیم (برای امنیت)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GITHUB_USERNAME = os.getenv("GH_USERNAME", "alihosseinpour431")
GITHUB_REPO = os.getenv("GITHUB_REPO", "crypo-scanner")
GITHUB_TOKEN = os.getenv("MY_GITHUB_TOKEN")  # ← این توکن را بعداً در تنظیمات سرور وارد می‌کنی

# ================= GITHUB API =================
def trigger_github_action():
    """ارسال درخواست به گیت‌هاب برای اجرای Workflow اسکن"""
    url = f"https://api.github.com/repos/{GITHUB_USERNAME}/{GITHUB_REPO}/dispatches"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {GITHUB_TOKEN}"
    }
    payload = {
        "event_type": "start_scan",
        "client_payload": {"source": "telegram"}
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=30)
        print(f"GitHub API Response: {r.status_code} - {r.text}")
        return r.status_code == 204
    except Exception as e:
        print(f"❌ GitHub API Error: {e}")
        return False

# ================= TELEGRAM HANDLERS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پیام خوش‌آمدگویی + دکمه اسکن"""
    keyboard = [[InlineKeyboardButton("🔍 شروع اسکن بازار", callback_data='scan_now')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🤖 <b>ربات اسکنر هوشمند XT آماده است!</b>\n\n"
        "برای شروع اسکن دو مرحله‌ای، دکمه زیر را بزنید:\n"
        "📅 فیلتر روزانه: Price>EMA30>EMA50 | RSI>50\n"
        "⏰ فیلتر ساعتی: Price>EMA50>EMA200 | RSI>50\n"
        "⚡ Alpha: میانگین ۳ / ۱۰ دوره (حجم + OBV)",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت کلیک روی دکمه"""
    query = update.callback_query
    await query.answer()  # حذف حالت لودینگ دکمه
    
    if query.data == 'scan_now':
        # تغییر متن پیام به "در حال پردازش..."
        await query.edit_message_text("⏳ <b>در حال ارسال درخواست به گیت‌هاب...</b>\nاین عملیات ممکن است ۱-۲ دقیقه طول بکشد.", parse_mode='HTML')
        
        # اجرای تابع تریگر گیت‌هاب
        if trigger_github_action():
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="✅ <b>درخواست اسکن با موفقیت ارسال شد!</b>\n\n"
                     "📊 گیت‌هاب در حال اجرای اسکن است...\n"
                     "⏱ نتیجه طی ۲-۵ دقیقه به این چت ارسال خواهد شد.\n\n"
                     "🔗 مشاهده وضعیت اجرا در گیت‌هاب:\n"
                     f"https://github.com/{GITHUB_USERNAME}/{GITHUB_REPO}/actions",
                parse_mode='HTML',
                disable_web_page_preview=True
            )
        else:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="❌ <b>خطا در ارتباط با گیت‌هاب</b>\n\n"
                     "لطفاً توکن گیت‌هاب (GITHUB_TOKEN) و تنظیمات را بررسی کنید.",
                parse_mode='HTML'
            )

# ================= MAIN =================
def main():
    print("🤖 Trigger bot starting...")
    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
    
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    print("✅ Bot is running! Send /start to begin.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
