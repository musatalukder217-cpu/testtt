import os
import time
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

# লগিং সেটআপ
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# রেলওয়ে এনভায়রনমেন্ট ভ্যারিয়েবল থেকে তথ্য লোড
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PUBLIC_CHANNEL_ID = os.getenv("PUBLIC_CHANNEL_ID")
OWNER_NAME = os.getenv("OWNER_NAME", "Admin")
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "")

# গুগল জেমিনি ক্লায়েন্ট সেটআপ
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# সেশন ও পারমিশন মেমোরি
APPROVED_CHAT_USERS = {ADMIN_ID}
APPROVED_CHANNELS = {PUBLIC_CHANNEL_ID}
user_chat_histories = {}
seen_news_ids = set()

# কনভারসেশন স্টেট (কাস্টম রিজেক্ট মেসেজের জন্য)
WAITING_REJECT_TEXT = 1

# ক্রিপ্টো নিউজ RSS ফিড লিস্ট
RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed"
]

# -----------------------------------------------------------------------------
# ১. জেমিনি অ্যানালাইসিস ফাংশনসমূহ
# -----------------------------------------------------------------------------

def analyze_crypto_news(title: str, summary: str):
    """নিউজের গুরুত্ব এবং প্রভাব পর্যালোচনা"""
    prompt = f"""
    You are an elite crypto market analyst. Analyze the following news.
    
    Headline: {title}
    Summary: {summary}
    
    Evaluate whether this is a Major/Black-swan single event (War, SEC ETF, Major Hack, Whale/Country buy-sell) 
    or a routine update. Keep response concise in Bengali.
    
    Output strictly in this format:
    🚨 **মার্কেট ইমপ্যাক্ট:** [বুলিশ 🟢 / বেয়ারিশ 🔴 / নিউট্রাল ⚪ / চরম ভোলাটাইল ⚠️]
    📊 **টাইপ:** [একক বড় ইভেন্ট / সাধারণ ক্লাস্টার নিউজ]
    💡 **সম্ভাব্য প্রভাব:** [১-২ লাইনে মার্কেটে তাৎক্ষণিক কী পরিবর্তন আসতে পারে]
    📝 **সারসংক্ষেপ:** [১ লাইনে মূল খবর]
    """
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        return response.text
    except Exception as e:
        logger.error(f"Gemini API Error: {e}")
        return None

def analyze_user_crypto_query(user_id: int, user_text: str):
    """ইউজারের প্রাইভেট প্রশ্নের উত্তর (কঠোরভাবে ক্রিপ্টো ও ম্যাক্রো বাউন্ডেড)"""
    # ইউজারের চ্যাট হিস্ট্রি লোড
    history = user_chat_histories.get(user_id, [])
    
    system_instruction = (
        "You are an exclusive AI Crypto & Macroeconomic Market Analyst. "
        "You ONLY answer questions directly related to cryptocurrencies (BTC, ETH, Altcoins, Memecoins), "
        "blockchain, on-chain whale activity, market support/resistance, breakouts, macroeconomics, interest rates, "
        "and geopolitical events/wars strictly regarding their impact on the financial/crypto markets. "
        "If the user asks anything outside of crypto, finance, and macro-market impact (e.g., cooking, unrelated coding, general chit-chat), "
        "strictly reply in Bengali: 'দুঃখিত, আমার কাছে এই ধরনের কোনো ডাটা নেই। আমি শুধুমাত্র ক্রিপ্টোকারেন্সি ও মার্কেট সম্পর্কিত বিষয় বিশ্লেষণে সক্ষম।'"
    )
    
    # শেষ ৪টি মেসেজ কনটেক্সটে রাখা
    context_msgs = "\n".join([f"{item['role']}: {item['text']}" for item in history[-4:]])
    prompt = f"{system_instruction}\n\nChat History:\n{context_msgs}\n\nUser: {user_text}\nAssistant:"
    
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        reply = response.text
        
        # হিস্ট্রি আপডেট (প্রতিটি ইউজারের সম্পূর্ণ আলাদা)
        history.append({"role": "user", "text": user_text})
        history.append({"role": "assistant", "text": reply})
        user_chat_histories[user_id] = history
        
        return reply
    except Exception as e:
        logger.error(f"Gemini Private Chat Error: {e}")
        return "⚠️ এআই বিশ্লেষণ করতে সাময়িক ত্রুটি হয়েছে। কিছুক্ষণ পর আবার চেষ্টা করুন।"

# -----------------------------------------------------------------------------
# ২. প্রাইভেট চ্যাট ও গ্র্যানুলার পারমিশন হ্যান্ডলার
# -----------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in APPROVED_CHAT_USERS:
        await update.message.reply_text(
            f"স্বাগতম {user.first_name}! আমি আপনার পার্সোনাল ক্রিপ্টো ইন্টেলিজেন্স এআই।\n"
            f"ক্রিপ্টো মার্কেট, টেকনিক্যাল অবস্থান, অন-চেইন তিমি মুভমেন্ট বা আসন্ন ইভেন্ট সম্পর্কে যেকোনো প্রশ্ন করতে পারেন।"
        )
    else:
        await update.message.reply_text(
            "⛔ আপনি এই চ্যাটবট ব্যবহারের অনুমোদিত ইউজার নন। আপনার এক্সেস রিকোয়েস্ট অ্যাডমিনের কাছে পাঠানো হয়েছে।"
        )
        # অ্যাডমিনকে পারমিশন বাটন পাঠানো
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
    # প্রসেসিং মেসেজ
    waiting_msg = await update.message.reply_text("🔍 মার্কেট ও ডাটা পর্যালোচনা করছি...")
    reply_text = analyze_user_crypto_query(user.id, user_query)
    
    await waiting_msg.edit_text(reply_text, parse_mode="HTML")

# -----------------------------------------------------------------------------
# ৩. অননুমোদিত চ্যানেলে অ্যাডমিন ডিটেকশন ও পারমিশন সিস্টেম
# -----------------------------------------------------------------------------

async def handle_bot_channel_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    if not result:
        return
    
    chat = result.chat
    new_status = result.new_chat_member.status
    added_by = result.from_user

    if new_status in ["administrator", "member"]:
        # যদি চ্যানেলটি পূর্বে অনুমোদিত না হয়
        if chat.id not in APPROVED_CHANNELS and str(chat.id) != str(PUBLIC_CHANNEL_ID):
            keyboard = [[
                InlineKeyboardButton("✅ Approve Channel", callback_data=f"chnl_approve_{chat.id}"),
                InlineKeyboardButton("❌ Reject & Leave", callback_data=f"chnl_reject_{chat.id}")
            ]]
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"📢 **New Channel/Group Admin Alert!**\n"
                    f"চ্যানেল: **{chat.title}** (ID: `{chat.id}`)\n"
                    f"অ্যাড করেছে: @{added_by.username} (ID: `{added_by.id}`)\n\n"
                    f"*আপনি কি এই চ্যানেলে ব্রডকাস্ট অনুমোদন করবেন?*"
                ),
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )

# বাটন ক্লিক হ্যান্ডলার
async def handle_button_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # ইউজারের চ্যাট এক্সেস রিকোয়েস্ট
    if data.startswith("user_allow_"):
        uid = int(data.split("_")[2])
        APPROVED_CHAT_USERS.add(uid)
        await query.edit_message_text(f"✅ ইউজার `{uid}` চ্যাটের জন্য অনুমোদিত।", parse_mode="HTML")
        try:
            await context.bot.send_message(chat_id=uid, text="🎉 অ্যাডমিন আপনার চ্যাট রিকোয়েস্ট অনুমোদন করেছেন! এখন আপনি প্রশ্ন করতে পারেন।")
        except Exception:
            pass

    elif data.startswith("user_deny_"):
        uid = int(data.split("_")[2])
        await query.edit_message_text(f"❌ ইউজার `{uid}` এর রিকোয়েস্ট বাতিল করা হয়েছে।", parse_mode="HTML")

    # চ্যানেলের এপ্রুভ (কোনো ওয়েলকাম ছাড়া সাইলেন্টলি সক্রিয়)
    elif data.startswith("chnl_approve_"):
        cid = int(data.split("_")[2])
        APPROVED_CHANNELS.add(cid)
        await query.edit_message_text(f"✅ চ্যানেল `{cid}` অনুমোদিত হয়েছে (সাইলেন্টলি সক্রিয়)।", parse_mode="HTML")

    # চ্যানেলের রিজেক্ট (কাস্টম মেসেজের জন্য প্রম্পট)
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

# কাস্টম মেসেজ লিখে পাঠালে তা চ্যানেলে পোস্ট করে লিভ নেওয়া
async def receive_custom_leave_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    custom_text = update.message.text
    target_channel_id = context.user_data.get('target_channel_to_leave')

    if target_channel_id:
        # ক্লিকেবল অনার ট্যাগ যুক্ত করা
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
            await update.message.reply_text("🚀 সফলভাবে কাস্টম মেসেজ পোস্ট করে বট চ্যানেল থেকে লিভ নিয়েছে!")
        except Exception as e:
            await update.message.reply_text(f"⚠️ লিভ নিতে সমস্যা হয়েছে: {e}")

        context.user_data.pop('target_channel_to_leave', None)

    return ConversationHandler.END

# -----------------------------------------------------------------------------
# ৪. ব্যাকগ্রাউন্ড ব্রডকাস্ট লুপ (নিউজ ও লিকুইডেশন মনিটর)
# -----------------------------------------------------------------------------

async def background_market_scanner(app):
    """২৪ ঘণ্টা ব্যাকগ্রাউন্ডে ফিড এবং মার্কেট মনিটর করা"""
    await asyncio.sleep(10)
    logger.info("Background Market Scanner started...")

    # প্রথমবার চালুর সময় আগের পুরোনো নিউজগুলো মেমোরিতে রেখে দেওয়া
    for feed_url in RSS_FEEDS:
        try:
            f = feedparser.parse(feed_url)
            for entry in f.entries[:5]:
                seen_news_ids.add(entry.get("id", entry.link))
        except Exception:
            pass

    while True:
        try:
            # ১. নিউজ স্ক্যানিং
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
                            # সকল অনুমোদিত চ্যানেলে সম্প্রচার
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

        # প্রতি ৫ মিনিট (৩০০ সেকেন্ড) পর পর চেক
        await asyncio.sleep(300)

# -----------------------------------------------------------------------------
# ৫. মূল অ্যাপ্লিকেশন ইনিশিয়ালাইজেশন
# -----------------------------------------------------------------------------

def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # রিজেক্ট কনভারসেশন হ্যান্ডলার
    reject_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_button_actions, pattern="^chnl_reject_")],
        states={
            WAITING_REJECT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_custom_leave_message)]
        },
        fallbacks=[]
    )

    app.add_handler(reject_conv)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(handle_button_actions))
    app.add_handler(ChatMemberHandler(handle_bot_channel_add, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_private_message))

    # ব্যাকগ্রাউন্ড স্ক্যানার চালু
    loop = asyncio.get_event_loop()
    loop.create_task(background_market_scanner(app))

    logger.info("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
