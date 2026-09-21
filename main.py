import os
import asyncio
import logging
import feedparser
import requests
import ccxt
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
from google import genai

# লগিং কনফিগারেশন
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# রেলওয়ে এনভায়রনমেন্ট ভ্যারিয়েবল
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip())
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
PUBLIC_CHANNEL_ID = os.getenv("PUBLIC_CHANNEL_ID", "").strip()
OWNER_NAME = os.getenv("OWNER_NAME", "Admin").strip()
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "").strip()

# গুগল জেমিনি ক্লায়েন্ট
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# ফ্রি পাবলিক বাইন্যান্স মার্কেট ডাটা (লাইভ প্রাইসের জন্য)
binance_spot = ccxt.binance({'enableRateLimit': True})

# মেমোরি স্টোরেজ
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

def clean_markdown_to_html(text: str) -> str:
    """জেমিনির মার্কডাউন টেক্সটকে টেলিগ্রামের এইচটিএমএলে রূপান্তর"""
    import re
    # বোল্ড রূপান্তর: **text** -> **text**
    text = re.sub(r'\*\*(.*?)\*\*', r'**\1**', text)
    # ইটালিক রূপান্তর: *text* -> *text*
    text = re.sub(r'\*(.*?)\*', r'*\1*', text)
    return text

def get_live_market_ticker(coin="BTC"):
    """বাইন্যান্স থেকে এই মুহূর্তের লাইভ প্রাইস ও হাই/লো সংগ্রহ"""
    try:
        symbol = f"{coin.upper()}/USDT"
        ticker = binance_spot.fetch_ticker(symbol)
        return (
            f"\n[লাইভ বাইন্যান্স ডাটা - {symbol}]\n"
            f"বর্তমান প্রাইস: \({ticker['last']:,.2f} | 24h High:\){ticker['high']:,.2f} | 24h Low: ${ticker['low']:,.2f} | 24h Change: {ticker['percentage']}%\n"
        )
    except Exception:
        return ""

# -----------------------------------------------------------------------------
# এআই ফাংশনসমূহ (রিয়েল-টাইম লাইভ ডাটা সহ)
# -----------------------------------------------------------------------------

def analyze_crypto_news(title: str, summary: str):
    if not ai_client:
        return None
    prompt = f"""
    You are an elite crypto analyst. Analyze this breaking crypto news.
    Headline: {title}
    Summary: {summary}
    
    Evaluate immediate market impact. Respond strictly in clear Bengali.
    Format:
    🚨 **মার্কেট ইমপ্যাক্ট:** [বুলিশ 🟢 / বেয়ারিশ 🔴 / নিউট্রাল ⚪ / চরম ভোলাটাইল ⚠️]
    📊 **টাইপ:** [একক বড় ইভেন্ট / সাধারণ ক্লাস্টার নিউজ]
    💡 **সম্ভাব্য প্রভাব:** [১-২ লাইনে প্রভাব]
    📝 **সারসংক্ষেপ:** [১ লাইনে মূল খবর]
    """
    for model_name in ['gemini-2.0-flash', 'gemini-1.5-flash']:
        try:
            res = ai_client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            return clean_markdown_to_html(res.text)
        except Exception as e:
            logger.warning(f"News Analysis failed on {model_name}: {e}")
            continue
    return None

def analyze_user_crypto_query(user_id: int, user_text: str):
    if not ai_client:
        return "⚠️ এআই কনফিগারেশনে সমস্যা: API Key লোড হয়নি।"

    # প্রশ্নে কয়েন উল্লেখ থাকলে লাইভ বাইন্যান্স প্রাইস নিয়ে আসা
    live_ticker = ""
    query_upper = user_text.upper()
    for coin in ["BTC", "BITCOIN", "ETH", "ETHEREUM", "SOL", "SOLANA", "XRP", "BNB", "DOGE"]:
        if coin in query_upper:
            base_coin = "BTC" if "BITCOIN" in coin else ("ETH" if "ETHEREUM" in coin else ("SOL" if "SOLANA" in coin else coin))
            live_ticker = get_live_market_ticker(base_coin)
            break

    history = user_chat_histories.get(user_id, [])
    
    system_instruction = (
        "You are an exclusive AI Crypto & Macroeconomic Market Analyst. "
        "You ONLY answer questions directly related to cryptocurrencies (BTC, ETH, Altcoins, Memecoins), "
        "blockchain, on-chain whale activity, market support/resistance, breakouts, macroeconomics, interest rates, "
        "and geopolitical events/wars strictly regarding their impact on the financial/crypto markets. "
        "Always use the provided [লাইভ বাইন্যান্স ডাটা] if available to calculate realistic current support/resistance levels. "
        "Do not invent past prices. Respond in clear, professional Bengali without messy asterisks. "
        "If the user asks anything outside of crypto and macro markets, strictly reply: "
        "'দুঃখিত, আমার কাছে এই ধরনের কোনো ডাটা নেই। আমি শুধুমাত্র ক্রিপ্টোকারেন্সি ও মার্কেট সম্পর্কিত বিষয় বিশ্লেষণে সক্ষম।'"
    )
    
    context_msgs = "\n".join([f"{item['role']}: {item['text']}" for item in history[-4:]])
    prompt = f"{system_instruction}\n{live_ticker}\n\nChat History:\n{context_msgs}\n\nUser Question: {user_text}\nAnswer:"
    
    for model_name in ['gemini-2.0-flash', 'gemini-1.5-flash']:
        try:
            # গুগল সার্চ গ্রাউন্ডিং অন করা (তাজা নিউজের জন্য)
            res = ai_client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            raw_text = clean_markdown_to_html(res.text)
            
            history.append({"role": "user", "text": user_text})
            history.append({"role": "assistant", "text": raw_text})
            user_chat_histories[user_id] = history
            return raw_text
        except Exception as e:
            logger.warning(f"Chat failed on {model_name}: {e}")
            continue

    return "⚠️ এআই সার্ভার রেসপন্স করছে না। কিছুক্ষণ পর আবার চেষ্টা করুন।"

# -----------------------------------------------------------------------------
# অ্যাডমিন ও প্রাইভেট হ্যান্ডলার
# -----------------------------------------------------------------------------

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ আপনি এই বটের মূল অ্যাডমিন নন।")
        return

    users_list = "\n".join([f"• `{u}`" for u in APPROVED_CHAT_USERS]) or "কোনো অনুমোদিত ইউজার নেই"
    channels_list = "\n".join([f"• `{c}`" for c in APPROVED_CHANNELS if c]) or "কোনো অনুমোদিত চ্যানেল নেই"

    panel_text = (
        f"👑 **Admin Control Dashboard**\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 **অনুমোদিত ইউজার তালিকা:**\n{users_list}\n\n"
        f"📢 **অনুমোদিত চ্যানেল তালিকা:**\n{channels_list}\n\n"
        f"💡 *নতুন এক্সেস রিকোয়েস্ট সরাসরি ইনবক্সে আসবে।*"
    )
    await update.message.reply_text(panel_text, parse_mode="HTML")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in APPROVED_CHAT_USERS:
        await update.message.reply_text(
            f"স্বাগতম {user.first_name}! আমি আপনার পার্সোনাল ক্রিপ্টো ইন্টেলিজেন্স এআই।\n"
            f"ক্রিপ্টো মার্কেট, লাইভ সাপোর্ট/রেজিস্ট্যান্স বা অন-চেইন খবর নিয়ে যেকোনো প্রশ্ন করতে পারেন।"
        )
    else:
        await update.message.reply_text("⛔ আপনি অনুমোদিত ইউজার নন। অ্যাডমিনের কাছে এক্সেস রিকোয়েস্ট পাঠানো হয়েছে।")
        keyboard = [[
            InlineKeyboardButton("✅ Allow Chat", callback_data=f"user_allow_{user.id}"),
            InlineKeyboardButton("❌ Deny", callback_data=f"user_deny_{user.id}")
        ]]
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"👤 **New Private Chat Request**\nUser: @{user.username} (ID: `{user.id}`)",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in APPROVED_CHAT_USERS:
        await update.message.reply_text("⛔ আপনার চ্যাট এক্সেস এখনো অনুমোদিত হয়নি।")
        return
    
    user_query = update.message.text
    waiting_msg = await update.message.reply_text("🔍 লাইভ মার্কেট ও ডাটা যাচাই করছি...")
    
    reply_text = analyze_user_crypto_query(user.id, user_query)
    
    try:
        await waiting_msg.edit_text(reply_text, parse_mode="HTML")
    except Exception:
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
                    f"📢 **New Channel Admin Alert!**\n"
                    f"চ্যানেল: **{chat.title}** (ID: `{chat.id}`)\n"
                    f"অ্যাড করেছে: @{added_by.username} (ID: `{added_by.id}`)\n\n"
                    f"*আপনি কি এই চ্যানেলে ব্রডকাস্ট অনুমোদন করবেন?*"
                ),
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )

async def handle_button_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("user_allow_"):
        uid = int(data.split("_")[2])
        APPROVED_CHAT_USERS.add(uid)
        await query.edit_message_text(f"✅ ইউজার `{uid}` চ্যাটের জন্য অনুমোদিত।", parse_mode="HTML")
        try:
            await context.bot.send_message(chat_id=uid, text="🎉 অ্যাডমিন আপনার রিকোয়েস্ট অনুমোদন করেছেন! এখন প্রশ্ন করতে পারেন।")
        except Exception:
            pass

    elif data.startswith("user_deny_"):
        uid = int(data.split("_")[2])
        await query.edit_message_text(f"❌ ইউজার `{uid}` এর রিকোয়েস্ট বাতিল করা হয়েছে।", parse_mode="HTML")

    elif data.startswith("chnl_approve_"):
        cid = int(data.split("_")[2])
        APPROVED_CHANNELS.add(cid)
        await query.edit_message_text(f"✅ চ্যানেল `{cid}` অনুমোদিত হয়েছে (সাইলেন্টলি সক্রিয়)।", parse_mode="HTML")

    elif data.startswith("chnl_reject_"):
        cid = int(data.split("_")[2])
        context.user_data['target_channel_to_leave'] = cid
        await query.edit_message_text(
            f"❌ চ্যানেলটি বাতিল করা হচ্ছে।\n\n"
            f"✍️ **এই চ্যানেলে লিভ নেওয়ার আগে আপনি কী কাস্টম মেসেজ পাঠাতে চান, তা এখনই লিখে পাঠান:**",
            parse_mode="HTML"
        )
        return WAITING_REJECT_TEXT

    return ConversationHandler.END

async def receive_custom_leave_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    custom_text = update.message.text
    target_channel_id = context.user_data.get('target_channel_to_leave')

    if target_channel_id:
        final_msg = (
            f"{custom_text}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 **Owner:** [{OWNER_NAME}](https://t.me/{OWNER_USERNAME})"
        )
        try:
            await context.bot.send_message(
                chat_id=target_channel_id,
                text=final_msg,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.error(f"Leave message send error: {e}")

        try:
            await context.bot.leave_chat(chat_id=target_channel_id)
            await update.message.reply_text("🚀 সফলভাবে কাস্টম মেসেজ পাঠিয়ে বট লিভ নিয়েছে!")
        except Exception as e:
            await update.message.reply_text(f"⚠️ লিভ নিতে সমস্যা হয়েছে: {e}")

        context.user_data.pop('target_channel_to_leave', None)

    return ConversationHandler.END

# -----------------------------------------------------------------------------
# ব্যাকগ্রাউন্ড নিউজ মনিটর (তাজা ব্রেকিং নিউজ)
# -----------------------------------------------------------------------------

async def background_market_scanner(app):
    await asyncio.sleep(10)
    for feed_url in RSS_FEEDS:
        try:
            f = feedparser.parse(feed_url)
            for entry in f.entries[:5]:
                seen_news_ids.add(entry.get("id", entry.link))
        except Exception:
            pass

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
                        if analysis:
                            broadcast_text = (
                                f"📰 **Breaking Crypto News**\n"
                                f"**{title}**\n\n"
                                f"{analysis}\n\n"
                                f"🔗 [Read Full Coverage]({link})\n"
                                f"━━━━━━━━━━━━━━━━━━\n"
                                f"⚠️ *Automated AI Intelligence • DYOR*"
                            )
                            for ch_id in list(APPROVED_CHANNELS):
                                try:
                                    await app.bot.send_message(
                                        chat_id=ch_id,
                                        text=broadcast_text,
                                        parse_mode="HTML",
                                        disable_web_page_preview=True
                                    )
                                except Exception as e:
                                    logger.error(f"Broadcast error for {ch_id}: {e}")
                        await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Scanner Loop Error: {e}")

        # প্রতি ৫ মিনিট পর পর চেক
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

    logger.info("Bot is active with Live Binance Data...")
    app.run_polling()

if __name__ == "__main__":
    main()
