import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
import feedparser
import requests
from groq import Groq
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ConversationHandler,
    ContextTypes,
    filters
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# রেলওয়ে এনভায়রনমেন্ট ভ্যারিয়েবল
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip())
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
PUBLIC_CHANNEL_ID = os.getenv("PUBLIC_CHANNEL_ID", "").strip()
OWNER_NAME = os.getenv("OWNER_NAME", "Admin").strip()
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "").strip()

try:
    groq_client = Groq(api_key=GROQ_API_KEY)
except Exception as e:
    logger.error(f"Groq Client Init Failed: {e}")
    groq_client = None

GROQ_ACTIVE_MODEL = "openai/gpt-oss-120b"

APPROVED_CHAT_USERS = {ADMIN_ID}
APPROVED_CHANNELS = {PUBLIC_CHANNEL_ID} if PUBLIC_CHANNEL_ID else set()
user_chat_histories = {}
seen_news_ids = set()

WAITING_REJECT_TEXT = 1

RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed"
]

def get_channel_english_date_str() -> str:
    """চ্যানেলের জন্য ১০০% খাঁটি ইংরেজি তারিখ (যেমন: 22 September 2026)"""
    bd_tz = timezone(timedelta(hours=6))
    now = datetime.now(bd_tz)
    return now.strftime("%d %B %Y")

def get_user_current_time_str() -> str:
    """ব্যবহারকারীর জন্য বর্তমান সময় ও তারিখ"""
    bd_tz = timezone(timedelta(hours=6))
    now = datetime.now(bd_tz)
    return now.strftime("%d %B %Y, %I:%M %p")

def format_clean_text(text: str) -> str:
    """স্টারচিহ্ন এবং অপ্রয়োজনীয় মার্কডাউন দূর করা"""
    if not text:
        return ""
    return text.replace("**", "").replace("*", "").strip()

def get_binance_live_price(symbol="BTCUSDT"):
    """বাইন্যান্স থেকে যেকোনো কয়েনের লাইভ মার্কেট ডাটা সংগ্রহ"""
    try:
        url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
        res = requests.get(url, timeout=5).json()
        if 'lastPrice' in res:
            price = float(res['lastPrice'])
            high = float(res['highPrice'])
            low = float(res['lowPrice'])
            change = float(res['priceChangePercent'])
            return f"\n[Live Binance Data: {symbol} | Price: \({price:,.4f} | 24h High:\){high:,.4f} | 24h Low: ${low:,.4f} | 24h Change: {change}%]\n"
        return ""
    except Exception:
        return ""

async def send_large_text_reply(update: Update, waiting_msg, full_text: str):
    """টেলিগ্রামের লিমিট অনুযায়ী বড় মেসেজ নিরাপদভাবে পাঠানো"""
    max_len = 3900
    if len(full_text) <= max_len:
        await waiting_msg.edit_text(full_text)
        return

    parts = []
    while len(full_text) > max_len:
        split_idx = full_text.rfind("\n\n", 0, max_len)
        if split_idx == -1:
            split_idx = full_text.rfind("\n", 0, max_len)
        if split_idx == -1:
            split_idx = max_len
        parts.append(full_text[:split_idx].strip())
        full_text = full_text[split_idx:].strip()
    if full_text:
        parts.append(full_text)

    await waiting_msg.edit_text(parts[0])
    for part in parts[1:]:
        await update.message.reply_text(part)

# -----------------------------------------------------------------------------
# এআই ফাংশনসমূহ (পূর্ণাঙ্গ ক্রিপ্টো জ্ঞান ও চ্যানেল ফরম্যাটিং)
# -----------------------------------------------------------------------------

def analyze_crypto_news(title: str, summary: str):
    if not groq_client:
        return None

    system_prompt = (
        "You are an institutional crypto market analyst. "
        "Strict Formatting Rules:\n"
        "1. ONLY the Headline line MUST be translated into a powerful, fluent, and attractive BENGALI headline.\n"
        "2. ALL OTHER TEXT, labels, analysis, impact, and summary MUST be strictly in professional ENGLISH.\n"
        "3. Evaluate if the news is a genuine catalyst or routine noise. Do NOT blindly label everything bullish/bearish.\n"
        "4. Use clean, balanced emojis (🟢, 🔴, ⚪, ⚠️, 📊, 📝). Never use markdown asterisks (**).\n\n"
        "Strict Output Format:\n"
        "📢 [এখানে সংবাদের মূল শিরোনামের স্পষ্ট বাংলা অনুবাদ]\n\n"
        "📊 Market Impact: [Bullish 🟢 / Bearish 🔴 / Neutral ⚪ / High Volatility ⚠️]\n"
        "📁 Event Type: [Single Major Catalyst / Macro Regulatory / Routine Noise]\n"
        "💡 Potential Outlook: [1-2 concise lines in English detailing price or liquidity implications]\n"
        "📝 Key Summary: [1 concise sentence in English summarizing the core verified event]"
    )
    user_prompt = f"Original Title: {title}\nOriginal Summary: {summary}"

    try:
        response = groq_client.chat.completions.create(
            model=GROQ_ACTIVE_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.2,
            max_tokens=350
        )
        return format_clean_text(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"Groq News Analysis Error: {e}")
        return None

def analyze_user_crypto_query(user_id: int, user_text: str):
    if not groq_client:
        return "⚠️ এআই ত্রুটি: GROQ_API_KEY সেট করা হয়নি।"

    # জনপ্রিয় কয়েনসমূহের ডাইনামিক লাইভ প্রাইস ডিটেকশন
    live_data = ""
    upper_query = user_text.upper()
    common_tickers = [
        "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "DOGE", "SUI", "NEAR",
        "LINK", "PEPE", "SHIB", "DOT", "MATIC", "POL", "LTC", "APT", "ARB", "OP", "FET"
    ]
    for ticker in common_tickers:
        if ticker in upper_query:
            live_data = get_binance_live_price(f"{ticker}USDT")
            break

    history = user_chat_histories.get(user_id, [])
    current_time_str = get_user_current_time_str()

    system_instruction = (
        f"You are the Ultimate Crypto Intelligence Brain and Senior Technical Analyst. Current Date/Time: {current_time_str}. "
        "You have complete and encyclopedic knowledge of the entire cryptocurrency industry, TradingView charts, and financial markets:\n"
        "1. TradingView Macro Indices: "
        "- TOTAL (Total crypto market cap), TOTAL2 (Total market cap excluding BTC), TOTAL3 (Altcoin market cap excluding BTC & ETH), "
        "- BTC.D (Bitcoin dominance), USDT.D (Tether dominance), OTHERS (Mid/micro-cap altcoins index). "
        "When user asks about TOTAL3 or Altcoins support/market cap, directly analyze the current billion-dollar support/resistance zones, liquidity rotation from BTC to Altcoins, and whether an Altseason setup is forming.\n"
        "2. Coins & Categories: "
        "- Layer 1s (BTC, ETH, SOL, BNB, SUI, AVAX, NEAR, ADA)\n"
        "- Layer 2s & Rollups (ARB, OP, MATIC/POL, BASE ecosystem)\n"
        "- Memecoins (DOGE, SHIB, PEPE, WIF, FLOKI, BONK)\n"
        "- AI Tokens (NEAR, FET/ASI, RENDER), DeFi (UNI, AAVE, MKR), and RWA (ONDO).\n"
        "3. Market Mechanics: Orderbook liquidity, Whales on-chain wallet tracking, Funding Rates, Long/Short liquidation heatmaps, Tokenomics (Unlocks/Vesting), Spot/Perpetual futures, and Fed Macroeconomic rate cuts (FOMC/CPI).\n\n"
        "CRITICAL RULES:\n"
        "- Never say you don't know any crypto concept, pair, or index. You understand all crypto terminology.\n"
        "- Keep answers direct, concise, and to-the-point in natural professional Bengali. Answer ONLY what was asked without unnecessary long essays.\n"
        "- Do NOT use markdown asterisks (**).\n"
        "- If live Binance price data is provided, use it for exact realistic support and resistance numbers.\n"
        "- If a completely non-financial, non-crypto question is asked, strictly reply: 'দুঃখিত, আমি শুধুমাত্র ক্রিপ্টোকারেন্সি ও মার্কেট সম্পর্কিত বিষয় বিশ্লেষণে সক্ষম।'"
    )

    messages = [{"role": "system", "content": system_instruction}]
    for h in history[-3:]:
        messages.append({"role": h["role"], "content": h["text"]})
    
    current_content = f"{live_data}\nইউজার প্রশ্ন: {user_text}"
    messages.append({"role": "user", "content": current_content})

    try:
        response = groq_client.chat.completions.create(
            model=GROQ_ACTIVE_MODEL,
            messages=messages,
            temperature=0.3,
            max_tokens=650
        )
        reply = format_clean_text(response.choices[0].message.content)
        history.append({"role": "user", "text": user_text})
        history.append({"role": "assistant", "text": reply})
        user_chat_histories[user_id] = history
        return reply
    except Exception as e:
        logger.error(f"Groq Chat Error: {e}")
        return f"⚠️ এআই ত্রুটি: {str(e)[:120]}"

# -----------------------------------------------------------------------------
# অ্যাডমিন ড্যাশবোর্ড ও বট কমান্ড
# -----------------------------------------------------------------------------

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ আপনি বটের মূল অ্যাডমিন নন।")
        return

    users_list = "\n".join([f"• {u}" for u in APPROVED_CHAT_USERS]) or "None"
    channels_list = "\n".join([f"• {c}" for c in APPROVED_CHANNELS if c]) or "None"
    current_time_str = get_user_current_time_str()

    panel_text = (
        "👑 Admin Control Dashboard\n"
        f"📅 Date: {current_time_str}\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Approved Users:\n{users_list}\n\n"
        f"📢 Approved Channels:\n{channels_list}"
    )
    await update.message.reply_text(panel_text)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in APPROVED_CHAT_USERS:
        await update.message.reply_text(
            f"স্বাগতম {user.first_name}! আমি আপনার সর্বজনীন ক্রিপ্টো ও ট্রেডিং ইন্টেলিজেন্স এআই।\n"
            "TOTAL3, বিটকয়েন ডমিন্যান্স, অল্টকয়েন সাপোর্ট-রেজিস্ট্যান্স বা যেকোনো কয়েন নিয়ে প্রশ্ন করতে পারেন।"
        )
    else:
        await update.message.reply_text("⛔ আপনি অনুমোদিত ইউজার নন। অ্যাডমিনের কাছে এক্সেস রিকোয়েস্ট পাঠানো হয়েছে।")
        keyboard = [[
            InlineKeyboardButton("✅ Allow Chat", callback_data=f"user_allow_{user.id}"),
            InlineKeyboardButton("❌ Deny", callback_data=f"user_deny_{user.id}")
        ]]
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"👤 New Chat Request\nUser: @{user.username} (ID: {user.id})",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in APPROVED_CHAT_USERS:
        await update.message.reply_text("⛔ আপনার চ্যাট এক্সেস এখনো অনুমোদিত হয়নি।")
        return

    user_query = update.message.text
    waiting_msg = await update.message.reply_text("🔍 ক্রিপ্টো মেট্রিক্স পর্যালোচনা করছি...")

    reply_text = analyze_user_crypto_query(user.id, user_query)
    await send_large_text_reply(update, waiting_msg, reply_text)

async def handle_bot_channel_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    if not result:
        return
    chat = result.chat
    new_status = result.new_chat_member.status
    added_by = result.from_user

    if new_status in ["administrator", "member"]:
        if chat.id not in APPROVED_CHANNELS and str(chat.id) != str(PUBLIC_CHANNEL_ID):
            keyboard = [[
                InlineKeyboardButton("✅ Approve Channel", callback_data=f"chnl_approve_{chat.id}"),
                InlineKeyboardButton("❌ Reject & Leave", callback_data=f"chnl_reject_{chat.id}")
            ]]
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"📢 New Channel Alert!\n"
                    f"Channel: {chat.title} (ID: {chat.id})\n"
                    f"Added By: @{added_by.username} (ID: {added_by.id})\n\n"
                    "Approve this channel for broadcasts?"
                ),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

async def handle_button_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("user_allow_"):
        uid = int(data.split("_")[2])
        APPROVED_CHAT_USERS.add(uid)
        await query.edit_message_text(f"✅ User {uid} approved.")
        try:
            await context.bot.send_message(chat_id=uid, text="🎉 অ্যাডমিন আপনার চ্যাট রিকোয়েস্ট অনুমোদন করেছেন!")
        except Exception:
            pass

    elif data.startswith("user_deny_"):
        uid = int(data.split("_")[2])
        await query.edit_message_text(f"❌ User {uid} denied.")

    elif data.startswith("chnl_approve_"):
        cid = int(data.split("_")[2])
        APPROVED_CHANNELS.add(cid)
        await query.edit_message_text(f"✅ Channel {cid} approved (silently active).")

    elif data.startswith("chnl_reject_"):
        cid = int(data.split("_")[2])
        context.user_data['target_channel_to_leave'] = cid
        await query.edit_message_text("❌ Channel rejected. Enter custom leave message:")
        return WAITING_REJECT_TEXT

    return ConversationHandler.END

async def receive_custom_leave_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    custom_text = update.message.text
    target_channel_id = context.user_data.get('target_channel_to_leave')

    if target_channel_id:
        final_msg = (
            f"{format_clean_text(custom_text)}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Owner: https://t.me/{OWNER_USERNAME} ({OWNER_NAME})"
        )
        try:
            await context.bot.send_message(
                chat_id=target_channel_id,
                text=final_msg,
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.error(f"Leave send error: {e}")

        try:
            await context.bot.leave_chat(chat_id=target_channel_id)
            await update.message.reply_text("🚀 Bot has left the channel.")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Error leaving: {e}")

        context.user_data.pop('target_channel_to_leave', None)

    return ConversationHandler.END

# -----------------------------------------------------------------------------
# ব্যাকগ্রাউন্ড স্ক্যানার (চ্যানেলে হেডলাইন বাংলায়, বাকি সব ১০০% ইংরেজিতে)
# -----------------------------------------------------------------------------

async def background_market_scanner(app):
    await asyncio.sleep(5)
    logger.info("Market scanner active. Monitoring feeds...")

    initial_count = 0
    for feed_url in RSS_FEEDS:
        if initial_count >= 5:
            break
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:3]:
                if initial_count >= 5:
                    break
                nid = entry.get("id", entry.link)
                if nid not in seen_news_ids:
                    seen_news_ids.add(nid)
                    title = entry.title
                    summary = entry.get("summary", "")
                    link = entry.link

                    analysis = analyze_crypto_news(title, summary)
                    if not analysis:
                        analysis = (
                            f"📢 {title}\n\n"
                            f"📊 Market Impact: Neutral ⚪\n"
                            f"📁 Event Type: Routine News\n"
                            f"💡 Potential Outlook: Market stability expected.\n"
                            f"📝 Key Summary: General crypto sector update."
                        )

                    channel_date = get_channel_english_date_str()
                    broadcast_text = (
                        f"📰 Breaking News Update\n"
                        f"📅 {channel_date}\n\n"
                        f"{analysis}\n\n"
                        f"🔗 Source: {link}\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"Automated Crypto Intelligence"
                    )

                    for ch_id in list(APPROVED_CHANNELS):
                        try:
                            await app.bot.send_message(
                                chat_id=ch_id,
                                text=broadcast_text,
                                disable_web_page_preview=True
                            )
                            initial_count += 1
                            await asyncio.sleep(2)
                        except Exception as ex:
                            logger.error(f"Broadcast error: {ex}")
        except Exception as e:
            logger.error(f"Feed error: {e}")

    while True:
        try:
            for feed_url in RSS_FEEDS:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:3]:
                    nid = entry.get("id", entry.link)
                    if nid not in seen_news_ids:
                        seen_news_ids.add(nid)
                        title = entry.title
                        summary = entry.get("summary", "")
                        link = entry.link

                        analysis = analyze_crypto_news(title, summary)
                        if not analysis:
                            analysis = (
                                f"📢 {title}\n\n"
                                f"📊 Market Impact: Neutral ⚪\n"
                                f"📁 Event Type: Routine News\n"
                                f"💡 Potential Outlook: Market stability expected.\n"
                                f"📝 Key Summary: General crypto sector update."
                            )

                        channel_date = get_channel_english_date_str()
                        broadcast_text = (
                            f"📰 Breaking News Update\n"
                            f"📅 {channel_date}\n\n"
                            f"{analysis}\n\n"
                            f"🔗 Source: {link}\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"Automated Crypto Intelligence"
                        )

                        for ch_id in list(APPROVED_CHANNELS):
                            try:
                                await app.bot.send_message(
                                    chat_id=ch_id,
                                    text=broadcast_text,
                                    disable_web_page_preview=True
                                )
                            except Exception as ex:
                                logger.error(f"Loop dispatch error: {ex}")
                        await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Loop error: {e}")

        await asyncio.sleep(300)

# -----------------------------------------------------------------------------
# মেইন অ্যাপ রানার
# -----------------------------------------------------------------------------

def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    reject_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_button_actions, pattern="^chnl_reject_")],
        states={
            WAITING_REJECT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_custom_leave_message)]
        },
        fallbacks=[]
    )

    app.add_handler(reject_conv)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CallbackQueryHandler(handle_button_actions))
    app.add_handler(ChatMemberHandler(handle_bot_channel_add, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_private_message))

    loop = asyncio.get_event_loop()
    loop.create_task(background_market_scanner(app))

    logger.info("Bot is active with Complete Institutional Crypto Brain...")
    app.run_polling()

if __name__ == "__main__":
    main()
