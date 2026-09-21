import os
import io
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

GROQ_CHAT_MODEL = "openai/gpt-oss-120b"
GROQ_VOICE_MODEL = "whisper-large-v3"

APPROVED_CHAT_USERS = {ADMIN_ID}
APPROVED_CHANNELS = {PUBLIC_CHANNEL_ID} if PUBLIC_CHANNEL_ID else set()
user_chat_histories = {}
seen_news_ids = set()
LATEST_NEWS_CACHE = []

WAITING_REJECT_TEXT = 1

RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed"
]

def get_channel_english_date_str() -> str:
    bd_tz = timezone(timedelta(hours=6))
    now = datetime.now(bd_tz)
    return now.strftime("%d %B %Y")

def get_user_current_time_str() -> str:
    bd_tz = timezone(timedelta(hours=6))
    now = datetime.now(bd_tz)
    return now.strftime("%d %B %Y, %I:%M %p")

def format_clean_text(text: str) -> str:
    if not text:
        return ""
    return text.replace("**", "").replace("*", "").strip()

def is_text_mostly_english(text: str) -> bool:
    """ইউজারের ইনপুট মূলত ইংরেজি কি না তা যাচাই করা"""
    if not text:
        return False
    # বাংলা বর্ণমালা উপস্থিত থাকলে বাংলা হিসেবে গণ্য
    has_bengali = any('\u0980' <= c <= '\u09FF' for c in text)
    if has_bengali:
        return False
    # ইংরেজি বর্ণের আধিক্য দেখা
    ascii_letters = sum(1 for c in text if c.isascii() and c.isalpha())
    return ascii_letters > (len(text) * 0.3)

def get_binance_live_price(query_text: str):
    """ইউজার নিজে উল্লেখ করলেই কেবল নির্দিষ্ট কয়েনের লাইভ ডাটা আনা হবে"""
    try:
        clean_q = query_text.upper().replace("/", " ").replace("-", " ")
        target_symbol = None

        if any(k in clean_q for k in ["BTC", "BITCOIN", "বিটকয়েন", "বিটকয়েনের"]):
            target_symbol = "BTCUSDT"
        elif any(k in clean_q for k in ["ETH", "ETHEREUM", "ইথেরিয়াম", "ইথিরিয়াম"]):
            target_symbol = "ETHUSDT"
        elif any(k in clean_q for k in ["SOL", "SOLANA", "সোলানা"]):
            target_symbol = "SOLUSDT"
        elif any(k in clean_q for k in ["BNB", "BINANCE COIN"]):
            target_symbol = "BNBUSDT"
        else:
            words = clean_q.split()
            for w in words:
                if len(w) >= 2 and len(w) <= 8:
                    if w.endswith("USDT"):
                        target_symbol = w
                        break
                    elif w in ["XRP", "DOGE", "ADA", "AVAX", "SUI", "NEAR", "PEPE", "SHIB", "LINK", "DOT", "MATIC", "POL", "APT", "ARB", "OP", "FET", "RENDER", "WIF"]:
                        target_symbol = f"{w}USDT"
                        break

        # জোর করে ডিফল্ট কোনো বিটকয়েন ডাটা পুশ করা হবে না
        if not target_symbol:
            return ""

        url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={target_symbol}"
        res = requests.get(url, timeout=4).json()
        if 'lastPrice' in res:
            price = float(res['lastPrice'])
            high = float(res['highPrice'])
            low = float(res['lowPrice'])
            change = float(res['priceChangePercent'])
            return f"\n[Live Binance Verified: {target_symbol} | Price: \({price:,.2f} | 24h High:\){high:,.2f} | 24h Low: ${low:,.2f} | 24h Change: {change}%]\n"
        return ""
    except Exception:
        return ""

async def send_large_text_reply(update: Update, waiting_msg, full_text: str):
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
# এআই ফাংশনসমূহ
# -----------------------------------------------------------------------------

def transcribe_audio_file(audio_bytes: bytes) -> str:
    """ভয়েসকে টেক্সটে রূপান্তর"""
    if not groq_client:
        return ""
    try:
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = "voice.ogg"
        # প্রম্পট দিয়ে দেওয়া যাতে ভুল অন্য কোনো ভাষায় কনভার্ট না করে
        transcription = groq_client.audio.transcriptions.create(
            file=audio_file,
            model=GROQ_VOICE_MODEL,
            response_format="text",
            prompt="Bengali and English conversation about daily topics and crypto"
        )
        return transcription.strip()
    except Exception as e:
        logger.error(f"Voice Transcription Error: {e}")
        return ""

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
            model=GROQ_CHAT_MODEL,
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
        return "⚠️ এআই ত্রুটি: GROQ_API_KEY কনফিগার করা হয়নি।"

    live_data = get_binance_live_price(user_text)
    history = user_chat_histories.get(user_id, [])
    current_time_str = get_user_current_time_str()

    recent_news_context = ""
    if live_data and LATEST_NEWS_CACHE:
        recent_news_context = "\n[LIVE BREAKING NEWS HAPPENING RIGHT NOW]:\n" + "\n".join(
            [f"- {n['title']}: {n['summary'][:150]}..." for n in LATEST_NEWS_CACHE[:2]]
        ) + "\n"

    system_instruction = (
        f"You are a versatile, highly intelligent AI assistant and expert Crypto/Financial Brain. Current Date/Time: {current_time_str}.\n"
        "IMPORTANT RULES ON TOPICS AND CONTEXT:\n"
        "1. Context Relevance: Answer EXACTLY what the user asks! If the user asks for a Biryani recipe, cooking tips, or general life topics, provide that gracefully and accurately. DO NOT force or inject crypto charts/prices into cooking or everyday conversations.\n"
        "2. Crypto Topics: When the user asks about crypto, Binance, trading, coins, or market direction, leverage your institutional trading mastery and the provided live data.\n"
        "3. Language & Dialect Matching: ALWAYS detect the exact language of the user's input:\n"
        "- If the user writes or speaks in Bengali (or any regional dialect like Sylheti, Chittagonian, etc.), respond in that exact Bengali/dialect.\n"
        "- If the user writes or speaks in English, respond completely in professional English.\n"
        "- If the user asks to switch languages, obey immediately.\n"
        "4. Tone: Natural, direct, helpful, and concise. No markdown asterisks (**)."
    )

    messages = [{"role": "system", "content": system_instruction}]
    for h in history[-3:]:
        messages.append({"role": h["role"], "content": h["text"]})

    current_content = f"{live_data}{recent_news_context}\nUser Input: {user_text}"
    messages.append({"role": "user", "content": current_content})

    try:
        response = groq_client.chat.completions.create(
            model=GROQ_CHAT_MODEL,
            messages=messages,
            temperature=0.3,
            max_tokens=850
        )
        reply = format_clean_text(response.choices[0].message.content)
        history.append({"role": "user", "text": user_text})
        history.append({"role": "assistant", "text": reply})
        user_chat_histories[user_id] = history
        return reply
    except Exception as e:
        logger.error(f"Groq Chat Error: {e}")
        return f"⚠️ Error: {str(e)[:120]}"

# -----------------------------------------------------------------------------
# অ্যাডমিন ও হ্যান্ডলার
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
            f"স্বাগতম {user.first_name}! আমি আপনার পার্সোনাল এআই।\n"
            "ক্রিপ্টো মার্কেট, ট্রেডিং কিংবা সাধারণ যেকোনো প্রশ্ন লিখে বা ভয়েসে জানাতে পারেন।"
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
    # ভাষা অনুযায়ী ওয়েটিং মেসেজ
    if is_text_mostly_english(user_query):
        wait_text = "🔍 Processing..."
    else:
        wait_text = "🔍 পর্যালোচনা করছি..."

    waiting_msg = await update.message.reply_text(wait_text)
    reply_text = analyze_user_crypto_query(user.id, user_query)
    await send_large_text_reply(update, waiting_msg, reply_text)

async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in APPROVED_CHAT_USERS:
        await update.message.reply_text("⛔ আপনার চ্যাট এক্সেস এখনো অনুমোদিত হয়নি।")
        return

    voice = update.message.voice or update.message.audio
    if not voice:
        return

    waiting_msg = await update.message.reply_text("🎙️ Processing voice / ভয়েস শুনছি...")

    try:
        tg_file = await context.bot.get_file(voice.file_id)
        audio_stream = io.BytesIO()
        await tg_file.download_to_memory(audio_stream)
        audio_bytes = audio_stream.getvalue()

        transcribed_text = transcribe_audio_file(audio_bytes)
        if not transcribed_text:
            await waiting_msg.edit_text("⚠️ ভয়েস বোঝা যায়নি / Could not understand voice.")
            return

        reply_text = analyze_user_crypto_query(user.id, transcribed_text)
        
        # ট্রান্সক্রাইবড ভাষা অনুযায়ী হেডার দেওয়া
        if is_text_mostly_english(transcribed_text):
            final_reply = f"🗣️ Your input: \"{transcribed_text}\"\n\n{reply_text}"
        else:
            final_reply = f"🗣️ আপনার কথা: \"{transcribed_text}\"\n\n{reply_text}"

        await send_large_text_reply(update, waiting_msg, final_reply)
    except Exception as e:
        logger.error(f"Voice Handle Error: {e}")
        await waiting_msg.edit_text(f"⚠️ Error: {str(e)[:100]}")

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
# ব্যাকগ্রাউন্ড স্ক্যানার
# -----------------------------------------------------------------------------

async def background_market_scanner(app):
    global LATEST_NEWS_CACHE
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

                    LATEST_NEWS_CACHE.insert(0, {"title": title, "summary": summary})
                    LATEST_NEWS_CACHE = LATEST_NEWS_CACHE[:10]

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

                        LATEST_NEWS_CACHE.insert(0, {"title": title, "summary": summary})
                        LATEST_NEWS_CACHE = LATEST_NEWS_CACHE[:10]

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
# মেইন অ্যাপ
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
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.VOICE | filters.AUDIO), handle_voice_message))

    loop = asyncio.get_event_loop()
    loop.create_task(background_market_scanner(app))

    logger.info("Bot is active with Context-Aware Multi-Domain Engine...")
    app.run_polling()

if __name__ == "__main__":
    main()
