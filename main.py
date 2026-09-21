import os
import re
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

# ইন-মেমোরি স্টেট
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

def format_text_to_clean_html(text: str) -> str:
    """স্টারচিহ্ন এবং মার্কডাউন পরিষ্কার করে টেলিগ্রাম উপযোগী এইচটিএমএলে রূপান্তর"""
    if not text:
        return ""
    # কোড ব্লক সুরক্ষিত রাখা
    text = re.sub(r'```(.*?)```', r'
\1', text, flags=re.DOTALL)text = re.sub(r'(.*?)', r'\1', text)# বোল্ড ট্যাগ রূপান্তর (text -> text)text = re.sub(r'(.?)', r'\1', text)
# ইটালিক ট্যাগ রূপান্তর (text -> text)
text = re.sub(r'(?)*(.?)*(?!)', r'\1', text)# অবশিষ্ট অপ্রয়োজনীয় স্টারচিহ্ন মুছে ফেলাtext = text.replace("", "").replace("*", "")return text.strip()def get_binance_live_price(symbol="BTCUSDT"):"""বাইন্যান্স পাবলিক এপিআই থেকে লাইভ মার্কেট ডাটা সংগ্রহ"""try:url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"res = requests.get(url, timeout=5).json()price = float(res['lastPrice'])high = float(res['highPrice'])low = float(res['lowPrice'])change = float(res['priceChangePercent'])return f"\n[লাইভ মার্কেট ডাটা - {symbol} | বর্তমান দাম: ${price:,.2f} | ২৪ ঘণ্টার হাই:${high:,.2f} \vert{} ২৪ ঘণ্টার লো: ${low:,.2f} | পরিবর্তন: {change}%]\n"except Exception as e:logger.warning(f"Binance fetch error: {e}")return ""-----------------------------------------------------------------------------ডাইরেক্ট স্টেবল জেমিনি কল (কোনো SDK ডিপেন্ডেন্সি এরর ছাড়া)-----------------------------------------------------------------------------def call_gemini_direct(prompt: str):"""সরাসরি অফিশিয়াল গুগল v1 এন্ডপয়েন্টে কল করে রেসপন্স আনা"""if not GEMINI_API_KEY:return None, "API Key অনুপস্থিত।"# নতুন প্রজেক্টে সাপোর্ট করা নিশ্চিত মডেল তালিকা
models = ["gemini-1.5-flash-latest", "gemini-1.5-flash", "gemini-pro"]
headers = {"Content-Type": "application/json"}
payload = {
    "contents": [{"parts": [{"text": prompt}]}],
    "generationConfig": {"temperature": 0.3}
}

last_err = ""
for model in models:
    # v1 এবং v1beta উভয় স্টেবল এন্ডপয়েন্ট ট্রাই করা
    for ver in ["v1", "v1beta"]:
        endpoint = f"https://generativelanguage.googleapis.com/{ver}/models/{model}:generateContent?key={GEMINI_API_KEY}"
        try:
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=12)
            if resp.status_code == 200:
                data = resp.json()
                raw_ans = data["candidates"][0]["content"]["parts"][0]["text"]
                return format_text_to_clean_html(raw_ans), None
            else:
                last_err = f"{resp.status_code}: {resp.text}"
        except Exception as ex:
            last_err = str(ex)
            continue

return None, last_err
def analyze_crypto_news(title: str, summary: str):prompt = f"""You are an elite crypto analyst. Analyze this breaking crypto news:Headline: {title}Summary: {summary}Respond in concise, professional Bengali.Format:🚨 মার্কেট ইমপ্যাক্ট: [বুলিশ 🟢 / বেয়ারিশ 🔴 / নিউট্রাল ⚪ / চরম ভোলাটাইল ⚠️]📊 ধরন: [একক বড় ইভেন্ট / সাধারণ ক্লাস্টার নিউজ]💡 সম্ভাব্য প্রভাব: [১-২ লাইনে মূল প্রভাব]📝 সারসংক্ষেপ: [১ লাইনে মূল খবর]"""ans, _ = call_gemini_direct(prompt)return ansdef analyze_user_crypto_query(user_id: int, user_text: str):live_data = ""upper_query = user_text.upper()if any(k in upper_query for k in ["BTC", "BITCOIN"]) or "বিটকয়েন" in user_text:live_data = get_binance_live_price("BTCUSDT")elif any(k in upper_query for k in ["ETH", "ETHEREUM"]) or "ইথেরিয়াম" in user_text:live_data = get_binance_live_price("ETHUSDT")elif any(k in upper_query for k in ["SOL", "SOLANA"]):live_data = get_binance_live_price("SOLUSDT")history = user_chat_histories.get(user_id, [])
history_text = "\n".join([f"{item['role']}: {item['text']}" for item in history[-3:]])

system_instruction = (
    "You are an exclusive AI Crypto & Macroeconomic Market Analyst. "
    "Strictly answer only questions related to cryptocurrencies, blockchain, market levels, and finance. "
    "Calculate realistic support/resistance levels from the live market data provided. "
    "Write in clean, fluent Bengali without markdown asterisk clutter. "
    "If unrelated questions are asked, strictly reply: 'দুঃখিত, আমি শুধুমাত্র ক্রিপ্টোকারেন্সি ও মার্কেট সম্পর্কিত বিষয় বিশ্লেষণে সক্ষম।'"
)

full_prompt = f"{system_instruction}\n{live_data}\n\nRecent History:\n{history_text}\n\nUser Question: {user_text}\nAnswer:"
ans, err = call_gemini_direct(full_prompt)

if ans:
    history.append({"role": "user", "text": user_text})
    history.append({"role": "assistant", "text": ans})
    user_chat_histories[user_id] = history
    return ans
else:
    logger.error(f"Gemini Call Failed: {err}")
    return f"⚠️ এআই রেসপন্স করতে পারছে না। বিস্তারিত: {err[:150]}"
-----------------------------------------------------------------------------অ্যাডমিন ও টেলিগ্রাম হ্যান্ডলার-----------------------------------------------------------------------------async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):if update.effective_user.id != ADMIN_ID:await update.message.reply_text("⛔ আপনি এই বটের মূল অ্যাডমিন নন।")returnusers_list = "\n".join([f"• `{u}`" for u in APPROVED_CHAT_USERS]) or "কোনো অনুমোদিত ইউজার নেই"
channels_list = "\n".join([f"• `{c}`" for c in APPROVED_CHANNELS if c]) or "কোনো অনুমোদিত চ্যানেল নেই"

panel_text = (
    f"👑 **Admin Control Dashboard**\n"
    f"━━━━━━━━━━━━━━━━━━\n\n"
    f"👥 **অনুমোদিত ইউজার:**\n{users_list}\n\n"
    f"📢 **অনুমোদিত চ্যানেল:**\n{channels_list}"
)
await update.message.reply_text(panel_text, parse_mode="HTML")
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):user = update.effective_userif user.id in APPROVED_CHAT_USERS:await update.message.reply_text(f"স্বাগতম {user.first_name}! আমি আপনার পার্সোনাল ক্রিপ্টো ইন্টেলিজেন্স এআই।\n"f"মার্কেট বিশ্লেষণ, সাপোর্ট/রেজিস্ট্যান্স কিংবা অন-চেইন খবর নিয়ে যেকোনো প্রশ্ন করতে পারেন।")else:await update.message.reply_text("⛔ আপনি অনুমোদিত ইউজার নন। অ্যাডমিনের কাছে এক্সেস রিকোয়েস্ট পাঠানো হয়েছে।")keyboard = [[InlineKeyboardButton("✅ Allow Chat", callback_data=f"user_allow_{user.id}"),InlineKeyboardButton("❌ Deny", callback_data=f"user_deny_{user.id}")]]await context.bot.send_message(chat_id=ADMIN_ID,text=f"👤 New Private Chat Request\nUser: @{user.username} (ID: {user.id})",reply_markup=InlineKeyboardMarkup(keyboard),parse_mode="HTML")async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):user = update.effective_userif user.id not in APPROVED_CHAT_USERS:await update.message.reply_text("⛔ আপনার চ্যাট এক্সেস এখনো অনুমোদিত হয়নি।")returnuser_query = update.message.text
waiting_msg = await update.message.reply_text("🔍 লাইভ ডাটা ও মার্কেট বিশ্লেষণ করছি...")

reply_text = analyze_user_crypto_query(user.id, user_query)
try:
    await waiting_msg.edit_text(reply_text, parse_mode="HTML")
except Exception:
    await waiting_msg.edit_text(reply_text)
async def handle_bot_channel_add(update: Update, context: ContextTypes.DEFAULT_TYPE):result = update.my_chat_memberif not result:returnchat = result.chatnew_status = result.new_chat_member.statusadded_by = result.from_userif new_status in ["administrator", "member"]:
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
async def handle_button_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):query = update.callback_queryawait query.answer()data = query.dataif data.startswith("user_allow_"):
    uid = int(data.split("_")[2])
    APPROVED_CHAT_USERS.add(uid)
    await query.edit_message_text(f"✅ ইউজার `{uid}` চ্যাটের জন্য অনুমোদিত।", parse_mode="HTML")
    try:
        await context.bot.send_message(chat_id=uid, text="🎉 অ্যাডমিন আপনার রিকোয়েস্ট অনুমোদন করেছেন!")
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
async def receive_custom_leave_message(update: Update, context: ContextTypes.DEFAULT_TYPE):custom_text = update.message.texttarget_channel_id = context.user_data.get('target_channel_to_leave')if target_channel_id:
    clean_user_text = format_text_to_clean_html(custom_text)
    final_msg = (
        f"{clean_user_text}\n\n"
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
-----------------------------------------------------------------------------ব্যাকগ্রাউন্ড স্ক্যানার (চালু হওয়ামাত্রই ৫টি তাজা নিউজ পাঠানো)-----------------------------------------------------------------------------async def background_market_scanner(app):await asyncio.sleep(5)logger.info("Scanner running. Dispatching initial live feeds...")initial_count = 0
# চালু হওয়ামাত্র সেরা ৫টি ব্রেকিং নিউজ সংগ্রহ
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
                # এআই ফেইল করলে ব্যাকআপ তথ্য সহ পোস্ট নিশ্চিত করা
                if not analysis:
                    analysis = "🚨 **মার্কেট ইমপ্যাক্ট:** উচ্চ ভোলাটিলিটি নজরদারিতে\n📝 **সারসংক্ষেপ:** ব্রেকিং ক্রিপ্টো আপডেট।"

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
                        initial_count += 1
                        await asyncio.sleep(2)
                    except Exception as ex:
                        logger.error(f"Channel dispatch error: {ex}")
    except Exception as e:
        logger.error(f"Feed error: {e}")

# এরপর প্রতি ৫ মিনিট অন্তর ব্যাকগ্রাউন্ড স্ক্যান চলবে
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
                        analysis = "🚨 **মার্কেট ইমপ্যাক্ট:** বাজার নজরে রাখুন\n📝 **সারসংক্ষেপ:** তাৎক্ষণিক ক্রিপ্টো আপডেট।"

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
                        except Exception as ex:
                            logger.error(f"Broadcast error for {ch_id}: {ex}")
                    await asyncio.sleep(3)
    except Exception as e:
        logger.error(f"Loop error: {e}")

    await asyncio.sleep(300)
-----------------------------------------------------------------------------মেইন অ্যাপ এক্সিকিউশন-----------------------------------------------------------------------------def main():app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()reject_conv = ConversationHandler(
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

logger.info("Bot successfully deployed and listening...")
app.run_polling()
if name == "main":main()
