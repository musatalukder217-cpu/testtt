#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import hashlib
import asyncio
from datetime import datetime, timezone
from collections import defaultdict
from typing import Optional

import ccxt
import feedparser
import aiohttp
from dateutil import parser as dateutil_parser
from groq import Groq

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatMember,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatType
from telegram.error import TelegramError
import logging

# ═══════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════
logging.basicConfig(format="%(asctime)s │ %(name)s │ %(levelname)s │ %(message)s", level=logging.INFO)
logger = logging.getLogger("CryptoBot")

# ═══════════════════════════════════════════════════════════════
#  ENVIRONMENT VARIABLES
# ═══════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
PUBLIC_CHANNEL_ID = os.getenv("PUBLIC_CHANNEL_ID", "")

# ═══════════════════════════════════════════════════════════════
#  CLIENT INITIALIZATION
# ═══════════════════════════════════════════════════════════════
groq_client = Groq(api_key=GROQ_API_KEY)
LLM_MODEL = "openai/gpt-oss-120b"
exchange = ccxt.binance({"enableRateLimit": True, "timeout": 15000})

# ═══════════════════════════════════════════════════════════════
#  DATA STORES
# ═══════════════════════════════════════════════════════════════
approved_users: dict[int, bool] = {}
approved_channels: dict[int, bool] = {}
user_chat_history: dict[int, list] = defaultdict(list)
posted_news_hashes: set[str] = set()

RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cryptonews.com/news/feed/"
]

# ═══════════════════════════════════════════════════════════════
#  AI SYSTEM PROMPT (CHATBOT)
# ═══════════════════════════════════════════════════════════════
SYSTEM_PROMPT = """তুমি একজন ক্রিপ্টোকারেন্সি বিশ্লেষক ও ট্রেডিং অ্যাসিস্ট্যান্ট।
১) শুধুমাত্র ক্রিপ্টোকারেন্সি ও ট্রেডিং নিয়ে কথা বলবে। এর বাইরে কিছু জিজ্ঞেস করলে বাংলায় বলবে: "দুঃখিত, আমি শুধুমাত্র ক্রিপ্টোকারেন্সি বিশ্লেষণে সক্ষম।"
২) প্রম্পটে [LIVE_MARKET_DATA] থাকলে সেখান থেকে লাইভ প্রাইস বলবে। অনুমান করবে না।
৩) কোনো থার্ড-পার্টি ওয়েবসাইটের লিংক দেবে না।"""

def fetch_live_price(symbol: str) -> Optional[dict]:
    try:
        t = exchange.fetch_ticker(symbol)
        return {"symbol": symbol, "price": t.get("last", 0), "change_pct": t.get("percentage", 0)}
    except:
        return None

# ═══════════════════════════════════════════════════════════════
#  AI PROMPT: NEWS ANALYSIS (নতুন রিকোয়ারমেন্ট অনুযায়ী)
# ═══════════════════════════════════════════════════════════════
def generate_news_analysis(headline: str, summary: str) -> str:
    """
    নির্দেশনা অনুযায়ী এআই নিউজ প্রসেস করবে।
    """
    prompt = f"""You are an expert Crypto News Analyst. Analyze the following news.

News Headline: {headline}
Summary: {summary}

Follow these strict rules for formatting the output:
1. First line: Write a VERY SHORT, catchy Bengali headline (Start with an Emoji).
2. Everything else below the headline MUST be in English.
3. Write a brief 'Analysis' (2-3 sentences) explaining the news.
4. Add a 'Market Impact' section:
   - Clearly declare if this is "Major News" or "Minor News".
   - State the direction: Bullish, Bearish, or Neutral.
   - If it's Major News: Explain how it will directly impact the market.
   - If it's Minor News: Explain that by itself it has limited impact, but combined with broader market trends, it builds up the sentiment.
5. DO NOT include any source links (it will be added automatically).

Format Structure:
[Emoji] [Short Bengali Headline]

**Analysis:**
[English text]

**Market Impact:**
[English text stating Major/Minor and Bullish/Bearish with reasoning]"""

    try:
        response = groq_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=1500,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"News LLM error: {e}")
        return ""


# ═══════════════════════════════════════════════════════════════
#  RSS NEWS SCANNER (BACKGROUND JOB)
# ═══════════════════════════════════════════════════════════════
async def scan_rss_feeds(context: ContextTypes.DEFAULT_TYPE):
    target_channels = []
    if PUBLIC_CHANNEL_ID:
        try: target_channels.append(int(PUBLIC_CHANNEL_ID))
        except: target_channels.append(PUBLIC_CHANNEL_ID)
    
    if not target_channels: return

    now = datetime.now(timezone.utc)
    new_articles = []

    for feed_url in RSS_FEEDS:
        try:
            feed = await asyncio.get_event_loop().run_in_executor(None, feedparser.parse, feed_url)
            for entry in feed.entries[:3]:
                title = entry.get("title", "").strip()
                summary = entry.get("summary", "").strip()
                link = entry.get("link", "").strip() # সোর্স লিংক সংগ্রহ করা হচ্ছে
                
                if not title or not link: continue
                
                title_hash = hashlib.md5(title.encode()).hexdigest()
                if title_hash in posted_news_hashes: continue

                new_articles.append({
                    "title": title,
                    "summary": summary,
                    "link": link,
                    "hash": title_hash,
                    "time": now.strftime("%Y-%m-%d | %I:%M %p (UTC)"),
                })
        except Exception as e:
            logger.warning(f"RSS Error: {e}")

    for article in new_articles[:2]:
        analysis = await asyncio.get_event_loop().run_in_executor(
            None, generate_news_analysis, article["title"], article["summary"]
        )
        if not analysis: continue

        # ── ফাইনাল পোস্ট ফরম্যাট (নতুন নিয়ম অনুযায়ী) ──
        final_post = (
            f"{analysis}\n\n"
            f"📅 **Date & Time:** {article['time']}\n"
            f"🔗 **Source:** [Click Here to Read]({article['link']})\n\n"
            f"#CryptoNews #Bitcoin #MarketUpdate"
        )

        for ch_id in target_channels:
            try:
                await context.bot.send_message(
                    chat_id=ch_id,
                    text=final_post,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True # বড় থাম্বনেইল অফ রাখা হলো
                )
            except TelegramError as e:
                logger.error(f"Post error: {e}")

        posted_news_hashes.add(article["hash"])
        await asyncio.sleep(3)


# ═══════════════════════════════════════════════════════════════
#  BOT COMMANDS & HANDLERS (Basic Chatbot logic)
# ═══════════════════════════════════════════════════════════════
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id == ADMIN_ID:
        approved_users[user.id] = True
        await update.message.reply_text("✅ Admin Access Granted.")
    elif approved_users.get(user.id):
        await update.message.reply_text("✅ Welcome back!")
    else:
        await update.message.reply_text("🔒 Request sent to Admin.")
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Allow", callback_data=f"allow_{user.id}"),
            InlineKeyboardButton("❌ Deny", callback_data=f"deny_{user.id}")
        ]])
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"New User: {user.full_name}", reply_markup=keyboard)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID: return

    if query.data.startswith("allow_"):
        uid = int(query.data.replace("allow_", ""))
        approved_users[uid] = True
        await query.edit_message_text(f"✅ User {uid} Approved.")
    elif query.data.startswith("deny_"):
        uid = int(query.data.replace("deny_", ""))
        await query.edit_message_text(f"❌ User {uid} Denied.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    if update.effective_chat.type != ChatType.PRIVATE: return
    if user_id != ADMIN_ID and not approved_users.get(user_id): return

    await context.bot.send_chat_action(chat_id=user_id, action="typing")
    
    # Simple direct LLM call for chatbot
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": text}]
    try:
        res = groq_client.chat.completions.create(model=LLM_MODEL, messages=messages, temperature=0.6, max_tokens=1500)
        await update.message.reply_text(res.choices[0].message.content.strip())
    except Exception as e:
        await update.message.reply_text("⚠️ API Error.")

# ═══════════════════════════════════════════════════════════════
#  MAIN LAUNCHER
# ═══════════════════════════════════════════════════════════════
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, handle_message))
    
    # News Job Queue (Every 5 Minutes)
    app.job_queue.run_repeating(scan_rss_feeds, interval=300, first=10, name="rss_scanner")
    
    logger.info("🟢 Bot Started Successfully!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
