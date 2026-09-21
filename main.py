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

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip())
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
PUBLIC_CHANNEL_ID = os.getenv("PUBLIC_CHANNEL_ID", "").strip()
OWNER_NAME = os.getenv("OWNER_NAME", "Admin").strip()
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "").strip()

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
    if not text:
        return ""
    clean = text.replace("**", "").replace("*", "")
    return clean.strip()

def get_binance_live_price(symbol="BTCUSDT"):
    try:
        url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
        res = requests.get(url, timeout=5).json()
        price = float(res['lastPrice'])
        high = float(res['highPrice'])
        low = float(res['lowPrice'])
        change = float(res['priceChangePercent'])
        return f"\n[লাইভ ডাটা: {symbol} | দাম: \({price:,.2f} | 24h High:\){high:,.2f} | 24h Low: ${low:,.2f} | পরিবর্তন: {change}%]\n"
    except Exception as e:
        logger.warning(f"Binance error: {e}")
        return ""

def call_gemini_direct(prompt: str):
    if not GEMINI_API_KEY:
        return None, "API Key পাওয়া যায়নি।"

    models = ["gemini-1.5-flash-latest", "gemini-1.5-flash", "gemini-pro"]
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3}
    }

    last_err = ""
    for model in models:
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

def analyze_crypto_news(title: str, summary: str):
    prompt = f"""
You are a crypto analyst. Analyze this news:
Headline: {title}
Summary: {summary}

Respond in concise Bengali strictly in this format:
মার্কেট ইমপ্যাক্ট: [বুলিশ / বেয়ারিশ / নিউট্রাল]
সম্ভাব্য প্রভাব: [১-২ লাইনে মূল প্রভাব]
সারসংক্ষেপ: [১ লাইনে মূল খবর]
"""
    ans, _ = call_gemini_direct(prompt)
    return ans

def analyze_user_crypto_query(user_id: int, user_text: str):
    live_data = ""
    upper_query = user_text.upper()
    if any(k in upper_query for k in ["BTC", "BITCOIN"]) or "বিটকয়েন" in user_text:
        live_data = get_binance_live_price("BTCUSDT")
    elif any(k in upper_query for k in ["ETH", "ETHEREUM"]) or "ইথেরিয়াম" in user_text:
        live_data = get_binance_live_price("ETHUSDT")
    elif any(k in upper_query for k in ["SOL", "SOLANA"]):
        live_data = get_binance_live_price("SOLUSDT")

    history = user_chat_histories.get(user_id, [])
    history_text = "\n".join([f"{item['role']}: {item['text']}" for item in history[-3:]])

    system_instruction = (
        "You are an exclusive AI Crypto Analyst. Answer only about crypto, bitcoin, support/resistance, and finance. "
        "Use provided live market data to calculate real levels. "
        "Write in clean Bengali without markdown asterisks. "
        "For non-crypto topics reply: 'দুঃখিত, আমি শুধুমাত্র ক্রিপ্টো সম্পর্কিত প্রশ্নের উত্তর দিতে পারি।'"
    )

    full_prompt = f"{system_instruction}\n{live_data}\n\nHistory:\n{history_text}\n\nUser: {user_text}\nAnswer:"
    ans, err = call_gemini_direct(full_prompt)

    if ans:
        history.append({"role": "user", "text": user_text})
        history.append({"role": "assistant", "text": ans})
        user_chat_histories[user_id] = history
        return ans
    else:
        logger.error(f"Gemini error: {err}")
        return f"⚠️ এআই এরর: {err[:120]}"

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ আপনি বটের মূল অ্যাডমিন নন।")
        return

    users_list = "\n".join([f"• {u}" for u in APPROVED_CHAT_USERS]) or "কোনো ইউজার নেই"
    channels_list = "\n".join([f"• {c}" for c in APPROVED_CHANNELS if c]) or "কোনো চ্যানেল নেই"

    panel_text = (
        "👑 Admin Control Dashboard\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 অনুমোদিত ইউজার:\n{users_list}\n\n"
        f"📢 অনুমোদিত চ্যানেল:\n{channels_list}"
    )
    await update.message.reply_text(panel_text)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in APPROVED_CHAT_USERS:
        await update.message.reply_text(
            f"স্বাগতম {user.first_name}! আমি আপনার ক্রিপ্টো এআই।\n"
            "মার্কেট সাপোর্ট, রেজিস্ট্যান্স বা বর্তমান অবস্থা নিয়ে যেকোনো প্রশ্ন করতে পারেন।"
        )
    else:
        await update.message.reply_text("⛔ আপনি অনুমোদিত ইউজার নন। অ্যাডমিনের কাছে রিকোয়েস্ট পাঠানো হয়েছে।")
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
    waiting_msg = await update.message.reply_text("🔍 বিশ্লেষণ করছি...")

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
                    "অনুমোদন দিতে চান?"
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
        await query.edit_message_text(f"✅ ইউজার {uid} অনুমোদিত।")
        try:
            await context.bot.send_message(chat_id=uid, text="🎉 আপনার রিকোয়েস্ট অনুমোদিত হয়েছে!")
        except Exception:
            pass

    elif data.startswith("user_deny_"):
        uid = int(data.split("_")[2])
        await query.edit_message_text(f"❌ ইউজার {uid} বাতিল করা হয়েছে।")

    elif data.startswith("chnl_approve_"):
        cid = int(data.split("_")[2])
        APPROVED_CHANNELS.add(cid)
        await query.edit_message_text(f"✅ চ্যানেল {cid} অনুমোদিত হয়েছে।")

    elif data.startswith("chnl_reject_"):
        cid = int(data.split("_")[2])
        context.user_data['target_channel_to_leave'] = cid
        await query.edit_message_text("❌ চ্যানেল বাতিল। বিদায় নেওয়ার সময় চ্যানেলে কী মেসেজ পাঠাতে চান তা লিখে পাঠান:")
        return WAITING_REJECT_TEXT

    return ConversationHandler.END

async def receive_custom_leave_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    custom_text = update.message.text
    target_channel_id = context.user_data.get('target_channel_to_leave')

    if target_channel_id:
        clean_user_text = format_text_to_clean_html(custom_text)
        final_msg = (
            f"{clean_user_text}\n\n"
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
            await update.message.reply_text("🚀 মেসেজ পাঠিয়ে বট চ্যানেল থেকে বের হয়ে গেছে!")
        except Exception as e:
            await update.message.reply_text(f"⚠️ বের হতে সমস্যা: {e}")

        context.user_data.pop('target_channel_to_leave', None)

    return ConversationHandler.END

async def background_market_scanner(app):
    await asyncio.sleep(5)
    logger.info("Background scanner started...")

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
                        analysis = "মার্কেট ইমপ্যাক্ট: নজরদারিতে রাখুন\nসারসংক্ষেপ: লাইভ ক্রিপ্টো নিউজ।"

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
                            logger.error(f"Dispatch error: {ex}")
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
                            analysis = "মার্কেট ইমপ্যাক্ট: বাজার নজরদারিতে রাখুন\nসারসংক্ষেপ: সাম্প্রতিক আপডেট।"

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
                                logger.error(f"Channel error: {ex}")
                        await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Loop error: {e}")

        await asyncio.sleep(300)

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

    logger.info("Bot running without syntax errors...")
    app.run_polling()

if __name__ == "__main__":
    main()
