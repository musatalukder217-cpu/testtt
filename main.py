import os
import asyncio
import logging
import feedparser
import requests
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

# লগিং সেটআপ (রেলওয়ের Logs ট্যাবে এরর দেখার জন্য)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# এনভায়রনমেন্ট ভ্যারিয়েবল
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip())
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
PUBLIC_CHANNEL_ID = os.getenv("PUBLIC_CHANNEL_ID", "").strip()
OWNER_NAME = os.getenv("OWNER_NAME", "Admin").strip()
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "").strip()

# গুগল জেমিনি ক্লায়েন্ট
try:
    ai_client = genai.Client(api_key=GEMINI_API_KEY)
except Exception as e:
    logger.error(f"Gemini Client Init Failed: {e}")
    ai_client = None

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

# -----------------------------------------------------------------------------
# এআই ফাংশনসমূহ
# -----------------------------------------------------------------------------

def analyze_crypto_news(title: str, summary: str):
    if not ai_client:
        return None
    prompt = f"""
    You are an elite crypto market analyst. Analyze the following news.
    Headline: {title}
    Summary: {summary}
    
    Evaluate market sentiment. Keep response concise in Bengali.
    Format:
    🚨 **মার্কেট ইমপ্যাক্ট:** [বুলিশ 🟢 / বেয়ারিশ 🔴 / নিউট্রাল ⚪ / চরম ভোলাটাইল ⚠️]
    📊 **টাইপ:** [একক বড় ইভেন্ট / সাধারণ ক্লাস্টার নিউজ]
    💡 **সম্ভাব্য প্রভাব:** [১-২ লাইনে প্রভাব]
    📝 **সারসংক্ষেপ:** [১ লাইনে মূল খবর]
    """
    try:
        # gemini-2.5-flash বা fallback হিসেবে 1.5-flash
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        return response.text
    except Exception as e:
        logger.error(f"News Analysis Error: {e}")
        return None

def analyze_user_crypto_query(user_id: int, user_text: str):
    if not ai_client:
        return "⚠️ এআই কনফিগারেশনে সমস্যা: API Key লোড হয়নি।"

    history = user_chat_histories.get(user_id, [])
    
    system_instruction = (
        "You are an exclusive AI Crypto & Macroeconomic Market Analyst. "
        "You ONLY answer questions directly related to cryptocurrencies (BTC, ETH, Altcoins, Memecoins), "
        "blockchain, on-chain whale activity, market support/resistance, breakouts, macroeconomics, interest rates, "
        "and geopolitical events/wars strictly regarding their impact on the financial/crypto markets. "
        "Respond in fluent Bengali. "
        "If the user asks anything outside of crypto, finance, and macro-market impact, "
        "strictly reply: 'দুঃখিত, আমার কাছে এই ধরনের কোনো ডাটা নেই। আমি শুধুমাত্র ক্রিপ্টোকারেন্সি ও মার্কেট সম্পর্কিত বিষয় বিশ্লেষণে সক্ষম।'"
    )
    
    context_msgs = "\n".join([f"{item['role']}: {item['text']}" for item in history[-4:]])
    prompt = f"{system_instruction}\n\nChat History:\n{context_msgs}\n\nUser Question: {user_text}\nAnswer:"
    
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        reply = response.text
        
        history.append({"role": "user", "text": user_text})
        history.append({"role": "assistant", "text": reply})
        user_chat_histories[user_id] = history
        return reply
    except Exception as e:
        logger.error(f"Private Chat Gemini Error: {e}")
        # যদি 2.5-flash এ কোনো সমস্যা হয় তবে বিকল্প চেষ্টা
        try:
            res_backup = ai_client.models.generate_content(model='gemini-1.5-flash', contents=prompt)
            return res_backup.text
        except Exception as e2:
            logger.error(f"Backup Model Error: {e2}")
            return f"⚠️ এআই ত্রুটি: {str(e)[:120]}"

# -----------------------------------------------------------------------------
# অ্যাডমিন কন্ট্রোল প্যানেল (/admin)
# -----------------------------------------------------------------------------

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """শুধুমাত্র মূল অ্যাডমিনের জন্য ড্যাশবোর্ড"""
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
        f"💡 *যেকোনো ইউজার বা চ্যানেল বটে রিকোয়েস্ট পাঠালে আপনি সরাসরি এখানেই অনুমোদন/বাতিল বাটন পাবেন।*"
    )
    await update.message.reply_text(panel_text, parse_mode="HTML")

# -----------------------------------------------------------------------------
# বট হ্যান্ডলার ও পারমিশন
# -----------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in APPROVED_CHAT_USERS:
        await update.message.reply_text(
            f"স্বাগতম {user.first_name}! আমি আপনার পার্সোনাল ক্রিপ্টো ইন্টেলিজেন্স এআই।\n"
            f"ক্রিপ্টো মার্কেট, সাপোর্ট/রেজিস্ট্যান্স বা অন-চেইন তিমি মুভমেন্ট নিয়ে যেকোনো প্রশ্ন করতে পারেন।"
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
    waiting_msg = await update.message.reply_text("🔍 বিশ্লেষণ করছি, অপেক্ষা করুন...")
    
    reply_text = analyze_user_crypto_query(user.id, user_query)
    
    try:
        await waiting_msg.edit_text(reply_text, parse_mode="HTML")
    except Exception:
        # যদি HTML পার্সিংয়ে সমস্যা হয় তবে সাধারণ টেক্সট হিসেবে পাঠানো
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
# ব্যাকগ্রাউন্ড নিউজ মনিটরিং
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
    app.add_handler(CommandHandler("admin", admin_panel))  # নতুন অ্যাডমিন কমান্ড
    app.add_handler(CallbackQueryHandler(handle_button_actions))
    app.add_handler(ChatMemberHandler(handle_bot_channel_add, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_private_message))

    # ব্যাকগ্রাউন্ড লুপ চালু
    loop = asyncio.get_event_loop()
    loop.create_task(background_market_scanner(app))

    logger.info("Bot is active...")
    app.run_polling()

if __name__ == "__main__":
    main()
