"""
╔══════════════════════════════════════════════════════════════════╗
║          CRYPTO INTELLIGENCE TELEGRAM BOT v5.0                  ║
║          Real-Time News Filter + SEO Broadcast + Admin Control  ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import html
import time
import logging
import asyncio
import calendar
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from typing import Optional, Tuple

import feedparser
from groq import Groq
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatType, ChatMemberStatus

# ─────────────────────────────────────────────────────────────
# LOGGING SETUP
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s │ %(name)s │ %(levelname)s │ %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("CryptoIntelBot")

# ─────────────────────────────────────────────────────────────
# ENVIRONMENT VARIABLES
# ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
PUBLIC_CHANNEL_ID = os.getenv("PUBLIC_CHANNEL_ID", "").strip()
OWNER_NAME = os.getenv("OWNER_NAME", "Admin").strip()
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "").strip()

assert TELEGRAM_BOT_TOKEN, "TELEGRAM_BOT_TOKEN is missing!"
assert ADMIN_ID, "ADMIN_ID is missing!"
assert GROQ_API_KEY, "GROQ_API_KEY is missing!"

# ─────────────────────────────────────────────────────────────
# GROQ API SYSTEM (Sync Client)
# ─────────────────────────────────────────────────────────────
try:
    groq_client = Groq(api_key=GROQ_API_KEY)
except Exception as e:
    logger.error(f"Groq Client Init Failed: {e}")
    groq_client = None

GROQ_MODELS = [
    "openai/gpt-oss-120b",
    "llama-3.1-8b-instant",
    "llama-3.3-70b-versatile"
]

def get_groq_response_sync(messages: list[dict], max_tokens: int = 1500) -> Tuple[Optional[str], Optional[str]]:
    if not groq_client: return None, "Groq client is not initialized."
    last_error = ""
    for model_name in GROQ_MODELS:
        try:
            response = groq_client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=0.3, # Low temperature to prevent hallucination
                max_tokens=max_tokens
            )
            return response.choices[0].message.content.strip(), None
        except Exception as e:
            last_error = str(e)
            logger.warning(f"Model {model_name} failed. Trying fallback...")
            continue
    return None, last_error

# ─────────────────────────────────────────────────────────────
# IN-MEMORY STORAGE & STATES
# ─────────────────────────────────────────────────────────────
user_histories: dict[int, list[dict]] = defaultdict(list)
allowed_users: set[int] = {ADMIN_ID}
pending_users: set[int] = set()
approved_channels: set[int] = set()
seen_news_ids: set[str] = set()

if PUBLIC_CHANNEL_ID:
    try:
        approved_channels.add(int(PUBLIC_CHANNEL_ID))
    except ValueError:
        pass

WAITING_REJECT_TEXT = 1
MAX_HISTORY_LENGTH = 10

RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed"
]

# ─────────────────────────────────────────────────────────────
# UTILITY FUNCTIONS
# ─────────────────────────────────────────────────────────────
def owner_link() -> str:
    if OWNER_USERNAME:
        username = OWNER_USERNAME.lstrip("@")
        return f'<a href="https://t.me/{username}">{html.escape(OWNER_NAME)}</a>'
    return html.escape(OWNER_NAME)

def clean_text(text: str) -> str:
    return text.replace("**", "").replace("*", "").strip()

# ─────────────────────────────────────────────────────────────
# 1. REAL-TIME NEWS ANALYSIS & SEO BROADCAST
# ─────────────────────────────────────────────────────────────
def generate_seo_news_post(title: str, summary: str, pub_date_str: str, link: str) -> Optional[str]:
    system_prompt = """You are a strict institutional crypto news analyst.
CRITICAL RULES:
1. Do NOT hallucinate, guess, or invent old information. ONLY base your analysis on the provided Title and Summary.
2. Do NOT use markdown asterisks (*). Use plain text.
3. You MUST output EXACTLY in the following 4-line format. Do not add any intro or outro.

Format:
HEADLINE: [Write an attractive Bengali headline, max 12 words. Start with 🔴, 🟢, or ⚪ depending on market sentiment]
IMPACT: [Bullish / Bearish / Neutral / Highly Volatile - Short English analysis of the impact]
DRIVERS: [1-2 lines English explanation of WHY this matters, strictly based on the text]
TAGS: [#Tag1 #Tag2 #Tag3 #Tag4 #Tag5 - High volume SEO crypto tags related to the news]"""

    user_prompt = f"Original Title: {title}\nSummary: {summary}"

    res, err = get_groq_response_sync([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ], max_tokens=1000)

    if not res:
        logger.error(f"News analysis failed: {err}")
        return None

    # Parse LLM output carefully
    headline = impact = drivers = tags = ""
    for line in res.split('\n'):
        if line.startswith("HEADLINE:"): headline = line.replace("HEADLINE:", "").strip()
        elif line.startswith("IMPACT:"): impact = line.replace("IMPACT:", "").strip()
        elif line.startswith("DRIVERS:"): drivers = line.replace("DRIVERS:", "").strip()
        elif line.startswith("TAGS:"): tags = line.replace("TAGS:", "").strip()

    if not headline or not impact: return None # Fallback if LLM messes up

    # Strict Python Formatting for SEO Post
    final_post = (
        f"{headline}\n\n"
        f"🕒 <b>Date & Time:</b> {pub_date_str}\n"
        f"📊 <b>Market Impact:</b> {impact}\n"
        f"💡 <b>Key Drivers:</b> {drivers}\n"
        f"🌐 <b>Source:</b> <a href='{link}'>Read Full Article</a>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🏷️ <b>Trending SEO Tags:</b> {tags}"
    )
    return final_post

async def background_market_scanner(app: Application):
    await asyncio.sleep(5)
    logger.info("Real-time RSS scanner started...")
    
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            
            for url in RSS_FEEDS:
                feed = await asyncio.to_thread(feedparser.parse, url)
                
                for entry in feed.entries:
                    nid = entry.get("id", entry.link)
                    if nid in seen_news_ids: continue
                    
                    # 🔴 STRICT TIMESTAMP FILTERING 🔴
                    if hasattr(entry, 'published_parsed') and entry.published_parsed:
                        pub_ts = calendar.timegm(entry.published_parsed)
                        pub_time = datetime.fromtimestamp(pub_ts, tz=timezone.utc)
                        
                        # Only process news from the last 2 hours
                        if now_utc - pub_time > timedelta(hours=2):
                            seen_news_ids.add(nid) # Mark old news as seen
                            continue
                    else:
                        continue # Skip if no timestamp exists

                    seen_news_ids.add(nid)
                    pub_date_str = pub_time.strftime("%d %b %Y, %H:%M UTC")
                    title = entry.title
                    summary = entry.get("summary", "")[:500]
                    link = entry.link

                    # Generate Post via LLM
                    broadcast_text = await asyncio.to_thread(
                        generate_seo_news_post, title, summary, pub_date_str, link
                    )
                    
                    if not broadcast_text: continue

                    # Broadcast to channels
                    for ch_id in list(approved_channels):
                        try:
                            await app.bot.send_message(
                                chat_id=ch_id,
                                text=broadcast_text,
                                parse_mode=ParseMode.HTML,
                                disable_web_page_preview=True
                            )
                        except Exception as ex:
                            logger.error(f"Broadcast error for {ch_id}: {ex}")
                    
                    await asyncio.sleep(5) # Prevent spamming
                    
        except Exception as e:
            logger.error(f"Scanner Loop error: {e}")

        await asyncio.sleep(600) # Scan every 10 minutes

# ─────────────────────────────────────────────────────────────
# 2. CHANNEL APPROVAL & REJECTION LOGIC
# ─────────────────────────────────────────────────────────────
async def bot_added_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    if not result: return
    chat = result.chat
    new_status = result.new_chat_member.status
    
    if new_status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER]:
        if chat.id not in approved_channels:
            keyboard = [[
                InlineKeyboardButton("✅ Approve Channel", callback_data=f"chnl_apprv_{chat.id}"),
                InlineKeyboardButton("❌ Reject & Leave", callback_data=f"chnl_rejct_{chat.id}")
            ]]
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"📢 <b>Unauthorized Channel Alert!</b>\nChannel: {chat.title} (ID: <code>{chat.id}</code>)\nApprove or Reject?",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

async def channel_rejection_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID: return ConversationHandler.END

    cid = int(query.data.split("_")[2])
    context.user_data['target_channel'] = cid
    await query.edit_message_text(f"📝 You are rejecting Channel <code>{cid}</code>.\nPlease type the custom leave message below:", parse_mode=ParseMode.HTML)
    return WAITING_REJECT_TEXT

async def receive_custom_leave_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    custom_text = update.message.text
    target_cid = context.user_data.get('target_channel')

    if target_cid:
        final_msg = (
            f"{clean_text(custom_text)}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Owner: {owner_link()}"
        )
        try:
            # Send the message
            await context.bot.send_message(
                chat_id=target_cid,
                text=final_msg,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            # Immediately leave the channel
            await context.bot.leave_chat(chat_id=target_cid)
            await update.message.reply_text("✅ Message sent and bot successfully left the channel.")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Error executing action: {e}")
        
        context.user_data.pop('target_channel', None)
    
    return ConversationHandler.END

async def cancel_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Action cancelled.")
    return ConversationHandler.END

# ─────────────────────────────────────────────────────────────
# 3. PRIVATE CHATBOT & USER PERMISSIONS
# ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are "Crypto Intel AI" — a world-class crypto intelligence analyst.
GLOBAL LANGUAGE SUPPORT: Detect the user's language (Bengali, English, Banglish) and respond in the same language.
DOMAIN GUARDRAIL: If the question is outside crypto, trading, or finance, politely decline in their language."""

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    user_id = update.effective_user.id
    if update.effective_chat.type != ChatType.PRIVATE: return

    if user_id not in allowed_users:
        if user_id in pending_users:
            await update.message.reply_text("⏳ Your request is still pending.")
        else:
            await update.message.reply_text("🔒 Access Denied. Use /start to request access.")
        return

    text = update.message.text.strip()
    await context.bot.send_chat_action(chat_id=user_id, action="typing")

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(user_histories[user_id])
    messages.append({"role": "user", "content": text})

    response_text, error_details = await asyncio.to_thread(get_groq_response_sync, messages, 2000)

    if response_text:
        user_histories[user_id].append({"role": "user", "content": text})
        user_histories[user_id].append({"role": "assistant", "content": response_text})
        if len(user_histories[user_id]) > MAX_HISTORY_LENGTH * 2:
            user_histories[user_id] = user_histories[user_id][-MAX_HISTORY_LENGTH * 2:]

        clean_resp = clean_text(response_text)
        try:
            await update.message.reply_text(clean_resp, parse_mode=ParseMode.HTML)
        except:
            await update.message.reply_text(clean_resp)
    else:
        await update.message.reply_text(f"❌ API Error: {error_details}")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.effective_chat.type != ChatType.PRIVATE: return

    if user.id in allowed_users:
        await update.message.reply_text(f"🌟 <b>Welcome back {html.escape(user.first_name)}!</b>\nAsk me any crypto questions.", parse_mode=ParseMode.HTML)
    else:
        if user.id in pending_users:
            await update.message.reply_text("⏳ Request pending.")
            return
        pending_users.add(user.id)
        await update.message.reply_text("🔒 Access Denied. Request sent to admin.")
        
        keyboard = [[
            InlineKeyboardButton("✅ Allow", callback_data=f"usr_apprv_{user.id}"),
            InlineKeyboardButton("❌ Deny", callback_data=f"usr_rejct_{user.id}")
        ]]
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🔔 <b>New Access Request</b>\nUser: <code>{user.id}</code>",
            parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def standard_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID: return
    data = query.data

    # User Permissions
    if data.startswith("usr_apprv_"):
        uid = int(data.split("_")[2])
        allowed_users.add(uid)
        pending_users.discard(uid)
        await query.edit_message_text(f"✅ User {uid} approved.")
        try: await context.bot.send_message(uid, "🎉 Access Granted!")
        except: pass
    elif data.startswith("usr_rejct_"):
        uid = int(data.split("_")[2])
        pending_users.discard(uid)
        await query.edit_message_text(f"❌ User {uid} denied.")
    
    # Channel Silent Approval
    elif data.startswith("chnl_apprv_"):
        cid = int(data.split("_")[2])
        approved_channels.add(cid)
        await query.edit_message_text(f"✅ Channel <code>{cid}</code> silently approved.", parse_mode=ParseMode.HTML)

# ─────────────────────────────────────────────────────────────
# MAIN APPLICATION SETUP
# ─────────────────────────────────────────────────────────────
async def post_init(application: Application):
    commands = [BotCommand("start", "Start Bot")]
    await application.bot.set_my_commands(commands)
    try:
        await application.bot.send_message(ADMIN_ID, "🟢 <b>Bot System v5.0 Online!</b>\nReal-time strict SEO news filter active.", parse_mode=ParseMode.HTML)
    except: pass

def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Conversation handler for Channel Reject
    reject_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(channel_rejection_start, pattern="^chnl_rejct_")],
        states={
            WAITING_REJECT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_custom_leave_message)]
        },
        fallbacks=[CommandHandler("cancel", cancel_reject)]
    )

    application.add_handler(reject_conv)
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CallbackQueryHandler(standard_callback_handler))
    application.add_handler(ChatMemberHandler(bot_added_to_channel, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private_message))

    # Start Background Task
    loop = asyncio.get_event_loop()
    loop.create_task(background_market_scanner(application))

    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
