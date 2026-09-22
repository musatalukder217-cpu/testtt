import os
import logging
import feedparser
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from google import genai

# লগিং সেটআপ
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# পরিবেশ ভেরিয়েবল (Environment Variables)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# জেমিনাই ক্লায়েন্ট ইনিশিয়ালাইজেশন
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# RSS Feed থেকে একদম ফ্রিতে ও নিরাপদে নিউজ ফেচ করা
def fetch_latest_crypto_news() -> list:
    feed_url = "https://cointelegraph.com/rss"
    try:
        feed = feedparser.parse(feed_url)
        news_items = []
        
        # লেটেস্ট ৩টি নিউজ সংগ্রহ
        for entry in feed.entries[:3]:
            title = entry.title
            summary = entry.summary if hasattr(entry, 'summary') else ''
            news_items.append(f"Title: {title}\nDetails: {summary[:200]}...")
            
        return news_items
    except Exception as e:
        logging.error(f"Error fetching RSS: {e}")
        return []

# Gemini AI অ্যানালিসিস ফাংশন
def analyze_crypto_news(news_text: str) -> str:
    prompt = f"""
    You are an expert Crypto Financial & Sentiment Analyst.
    Analyze the following crypto news and provide:
    1. A concise 3-line Bengali summary (সহজ বাংলায় ৩ লাইনের সারসংক্ষেপ).
    2. Market Sentiment: [BULLISH / BEARISH / NEUTRAL] with a brief 1-line reason.
    3. Potential Impacted Coins (e.g., BTC, ETH, SOL).

    News Content:
    {news_text}
    """
    try:
        response = ai_client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt
        )
        return response.text
    except Exception as e:
        return f"AI অ্যানালিসিস করতে সমস্যা হয়েছে: {str(e)}"

# /start কমান্ড হ্যান্ডলার
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "👋 **স্বাগতম ক্রিপ্টো এআই অ্যানালিস্ট বটে!**\n\n"
        "আমি ক্রিপ্টো নিউজ বিশ্লেষণ করে বুলিশ/বেয়ারিশ সংকেত দিতে পারি।\n\n"
        "📌 **কমান্ডসমূহ:**\n"
        "• `/news` - লেটেস্ট ৩টি ক্রিপ্টো নিউজের এআই বিশ্লেষণ ও সেন্টিমেন্ট পেতে।\n"
        "• যেকোনো ক্রিপ্টো প্রশ্ন বা কয়েন সম্পর্কে সাধারণ মেসেজ পাঠালে আমি বিশ্লেষণ করে উত্তর দেব।"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

# /news কমান্ড হ্যান্ডলার
async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("🔄 লেটেস্ট ক্রিপ্টো নিউজ সংগ্রহ ও এআই দিয়ে বিশ্লেষণ করা হচ্ছে...")
    
    news_list = fetch_latest_crypto_news()
    if not news_list:
        await status_msg.edit_text("❌ এই মুহূর্তে নিউজ ফেচ করা যাচ্ছে না। দয়া করে কিছুক্ষণ পর চেষ্টা করুন।")
        return

    combined_news = "\n---\n".join(news_list)
    analysis = analyze_crypto_news(combined_news)

    response_text = f"📰 **সাম্প্রতিক ক্রিপ্টো নিউজ অ্যানালিসিস:**\n\n{analysis}"
    await status_msg.edit_text(response_text)

# সাধারণ চ্যাট/প্রশ্ন হ্যান্ডলার (Gemini AI Chat)
async def chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_query = update.message.text
    system_instruction = "You are a professional crypto trading and blockchain assistant. Answer clearly in Bengali."
    
    try:
        response = ai_client.models.generate_content(
            model='gemini-3.6-flash',
            contents=f"{system_instruction}\nUser Query: {user_query}"
        )
        await update.message.reply_text(response.text)
    except Exception as e:
        await update.message.reply_text(f"⚠️ দুঃখিত, প্রসেস করা সম্ভব হয়নি: {str(e)}")

# মেইন রানার
def main():
    if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
        raise ValueError("Environment variables TELEGRAM_BOT_TOKEN and GEMINI_API_KEY must be set in Railway!")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("news", news_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_handler))

    print("🤖 Crypto AI Bot is running with RSS Feed...")
    app.run_polling()

if __name__ == "__main__":
    main()
