import os
import asyncio
import logging
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

# রেলওয়ে এনভায়রনমেন্ট ভ্যারিয়েবল
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip())
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
PUBLIC_CHANNEL_ID = os.getenv("PUBLIC_CHANNEL_ID", "").strip()
OWNER_NAME = os.getenv("OWNER_NAME", "Admin").strip()
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "").strip()

# Groq ক্লায়েন্ট ইনিশিয়ালাইজেশন
try:
    groq_client = Groq(api_key=GROQ_API_KEY)
except Exception as e:
    logger.error(f"Groq Client Init Failed: {e}")
    groq_client = None

APPROVED_CHAT_USERS = {ADMIN_ID}
APPROVED_CHANNELS = {PUBLIC_CHANNEL_ID} if PUBLIC_CHANNEL_ID else set()
user_chat_histories = {}
seen_news_ids = set()

WAITING_REJECT_TEXT = 1

# তাজা ও ব্রেকিং ক্রিপ্টো আরএসএস ফিডস
RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed"
]

def format_clean_text(text: str) -> str:
    """স্টারচিহ্ন এবং অপ্রয়োজনীয় মার্কডাউন দূর করা"""
    if not text:
        return ""
    clean = text.replace("**", "").replace("*", "")
    return clean.strip()

def get_binance_live_price(symbol="BTCUSDT"):
    """বাইন্যান্স থেকে সরাসরি বর্তমান লাইভ মার্কেট ডাটা"""
    try:
        url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
        res = requests.get(url, timeout=5).json()
        price = float(res['lastPrice'])
        high = float(res['highPrice'])
        low = float(res['lowPrice'])
        change = float(res['priceChangePercent'])
        return f"\n[Binance Live: {symbol} | বর্তমান দাম: \({price:,.2f} | 24h High:\){high:,.2f} | 24h Low: ${low:,.2f} | 24h পরিবর্তন: {change}%]\n"
    except Exception as e:
        logger.warning(f"Binance fetch error: {e}")
        return ""

# -----------------------------------------------------------------------------
# Groq AI ইঞ্জিন (Llama-3.3-70b-versatile)
# -----------------------------------------------------------------------------

def analyze_crypto_news(title: str, summary: str):
    if not groq_client:
        return None

    system_prompt = (
        "You are an elite crypto market analyst. Analyze the given news headline and summary. "
        "Output strictly in Bengali with clean formatting (no asterisks). "
        "Format:\n"
        "মার্কেট ইমপ্যাক্ট: [বুলিশ / বেয়ারিশ / নিউট্রাল / চরম ভোলাটাইল]\n"
        "সম্ভাব্য প্রভাব: [১-২ লাইনে মূল প্রভাব]\n"
        "সারসংক্ষেপ: [১ লাইনে মূল খবর]"
    )
    user_prompt = f"Headline: {title}\nSummary: {summary}"

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
            max_tokens=250
        )
        return format_clean_text(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"Groq News Error: {e}")
        return None

def analyze_user_crypto_query(user_id: int, user_text: str):
    if not groq_client:
        return "⚠️ এআই ত্রুটি: GROQ_API_KEY সেট করা হয়নি।"

    # লাইভ প্রাইস ডাটা আনা
    live_data = ""
    upper_query = user_text.upper()
    if any(k in upper_query for k in ["BTC", "BITCOIN"]) or "বিটকয়েন" in user_text:
        live_data = get_binance_live_price("BTCUSDT")
    elif any(k in upper_query for k in ["ETH", "ETHEREUM"]) or "ইথেরিয়াম" in user_text:
        live_data = get_binance_live_price("ETHUSDT")
    elif any(k in upper_query for k in ["SOL", "SOLANA"]):
        live_data = get_binance_live_price("SOLUSDT")

    history = user_chat_histories.get(user_id, [])

    system_instruction = (
        "You are an expert AI Crypto & Macroeconomic Market Analyst. "
        "Strictly answer ONLY questions regarding cryptocurrencies, blockchain, market support/resistance, "
        "whale movements, and financial/macro trends. "
        "Always calculate realistic support/resistance levels based on the live Binance data provided. "
        "Respond in fluent, clear Bengali without messy asterisks or broken markdown. "
        "If unrelated questions are asked, strictly reply: 'দুঃখিত, আমি শুধুমাত্র ক্রিপ্টোকারেন্সি ও মার্কেট সম্পর্কিত বিষয় বিশ্লেষণে সক্ষম।'"
    )

    messages = [{"role": "system", "content": system_instruction}]
    for h in history[-4:]:
        messages.append({"role": h["role"], "content": h["text"]})
    
    current_content = f"{live_data}\nইউজার প্রশ্ন: {user_text}"
    messages.append({"role": "user", "content": current_content})

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.4,
            max_tokens=700
        )
        reply = format_clean_text(response.choices[0].message.content)

        history.append({"role": "user", "text": user_text})
        history.append({"role": "assistant", "text": reply})
        user_chat_histories[user_id] = history
        return reply
    except Exception as e:
        logger.error(f"Groq Chat Error: {e}")
        return f"⚠️ এআই সার্ভার ত্রুটি: {str(e)[:120]}"

# -----------------------------------------------------------------------------
# অ্যাডমিন ড্যাশবোর্ড ও বট কমান্ড
# -----------------------------------------------------------------------------

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ আপনি এই বটের মূল অ্যাডমিন নন।")
        return

    users_list = "\n".join([f"• {u}" for u in APPROVED_CHAT_USERS]) or "কোনো ইউজার নেই"
    channels_list = "\n".join([f"• {c}" for c in APPROVED_CHANNELS if c]) or "কোনো চ্যানেল নেই"

    panel_text = (
        "👑 Admin Control Dashboard\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 অনুমোদিত ইউজার তালিকা:\n{users_list}\n\n"
        f"📢 অনুমোদিত চ্যানেল তালিকা:\n{channels_list}"
    )
    await update.message.reply_text(panel_text)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in APPROVED_CHAT_USERS:
        await update.message.reply_text(
            f"স্বাগতম {user.first_name}! আমি আপনার পার্সোনাল ক্রিপ্টো ইন্টেলিজেন্স এআই।\n"
            "মার্কেট সাপোর্ট, রেজিস্ট্যান্স, বর্তমান বিটকয়েন মুভমেন্ট বা অন-চেইন তথ্য নিয়ে প্রশ্ন করতে পারেন।"
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
    waiting_msg = await update.message.reply_text("🔍 লাইভ ডাটা ও মার্কেট বিশ্লেষণ করছি...")

    reply_text = analyze_user_crypto_query(user.id, user_query)
    await waiting_msg.edit_text(reply_text)

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
                    f"চ্যানেল: {chat.title} (ID: {chat.id})\n"
                    f"অ্যাড করেছে: @{added_by.username} (ID: {added_by.id})\n\n"
                    "আপনি কি অনুমোদন করবেন?"
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
        await query.edit_message_text(f"✅ ইউজার {uid} সফলভাবে অনুমোদিত।")
        try:
            await context.bot.send_message(chat_id=uid, text="🎉 অ্যাডমিন আপনার চ্যাট রিকোয়েস্ট অনুমোদন করেছেন!")
        except Exception:
            pass

    elif data.startswith("user_deny_"):
        uid = int(data.split("_")[2])
        await query.edit_message_text(f"❌ ইউজার {uid} এর রিকোয়েস্ট বাতিল করা হয়েছে।")

    elif data.startswith("chnl_approve_"):
        cid = int(data.split("_")[2])
        APPROVED_CHANNELS.add(cid)
        await query.edit_message_text(f"✅ চ্যানেল {cid} অনুমোদিত হয়েছে (সাইলেন্টলি সক্রিয়)।")

    elif data.startswith("chnl_reject_"):
        cid = int(data.split("_")[2])
        context.user_data['target_channel_to_leave'] = cid
        await query.edit_message_text("❌ চ্যানেল বাতিল। বিদায় নেওয়ার আগে চ্যানেলে কী মেসেজ পাঠাতে চান তা লিখে পাঠান:")
        return WAITING_REJECT_TEXT

    return ConversationHandler.END

async def receive_custom_leave_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    custom_text = update.message.text
    target_channel_id = context.user_data.get('target_channel_to_leave')

    if target_channel_id:
        final_msg = (
            f"{custom_text}\n\n"
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
            await update.message.reply_text("🚀 মেসেজ পোস্ট করে বট চ্যানেল থেকে লিভ নিয়েছে!")
        except Exception as e:
            await update.message.reply_text(f"⚠️ লিভ নিতে সমস্যা: {e}")

        context.user_data.pop('target_channel_to_leave', None)

    return ConversationHandler.END

# -----------------------------------------------------------------------------
# ব্যাকগ্রাউন্ড স্ক্যানার (তাজা লাইভ নিউজ সম্প্রচার)
# -----------------------------------------------------------------------------

async def background_market_scanner(app):
    await asyncio.sleep(5)
    logger.info("Market scanner active. Sending initial fresh feeds...")

    initial_count = 0
    # শুরুতেই টাটকা ৫টি ব্রেকিং নিউজ সংগ্রহ ও এআই বিশ্লেষণ
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
                        analysis = "মার্কেট ইমপ্যাক্ট: নজরদারিতে রাখুন\nসারসংক্ষেপ: লাইভ ক্রিপ্টো আপডেট।"

                    broadcast_text = (
                        f"📰 Breaking Crypto News\n"
                        f"{title}\n\n"
                        f"{analysis}\n\n"
                        f"Source: {link}\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"Automated AI Intelligence"
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
                            logger.error(f"Channel broadcast error: {ex}")
        except Exception as e:
            logger.error(f"Initial feed error: {e}")

    # এরপর প্রতি ৫ মিনিট অন্তর স্ক্যান চলবে
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
                            analysis = "মার্কেট ইমপ্যাক্ট: বাজার পর্যবেক্ষণ করুন\nসারসংক্ষেপ: সাম্প্রতিক আপডেট।"

                        broadcast_text = (
                            f"📰 Breaking Crypto News\n"
                            f"{title}\n\n"
                            f"{analysis}\n\n"
                            f"Source: {link}\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"Automated AI Intelligence"
                        )

                        for ch_id in list(APPROVED_CHANNELS):
                            try:
                                await app.bot.send_message(
                                    chat_id=ch_id,
                                    text=broadcast_text,
                                    disable_web_page_preview=True
                                )
                            except Exception as ex:
                                logger.error(f"Broadcast error: {ex}")
                        await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Scanner loop error: {e}")

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

    loop = asyncio.get_event_loop()
    loop.create_task(background_market_scanner(app))

    logger.info("Bot is active with Groq AI...")
    app.run_polling()

if __name__ == "__main__":
    main()
