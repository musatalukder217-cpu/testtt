"""
╔══════════════════════════════════════════════════════════════════╗
║          CRYPTO INTELLIGENCE TELEGRAM BOT v6.0 (STRICT MODE)    ║
║          Real-Time Live Price Fetching + No Noise + SEO News    ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import re
import html
import time
import logging
import asyncio
import calendar
import aiohttp
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from typing import Optional, Tuple

import feedparser
from groq import AsyncGroq
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
# GROQ API SYSTEM (Async Client)
# ─────────────────────────────────────────────────────────────
try:
    groq_client = AsyncGroq(api_key=GROQ_API_KEY)
except Exception as e:
    logger.error(f"Groq Client Init Failed: {e}")
    groq_client = None

GROQ_MODELS = [
    "llama-3.1-8b-instant",
    "llama3-70b-8192",
    "llama-3.3-70b-versatile"
]

async def get_groq_response(messages: list[dict], max_tokens: int = 1500) -> Tuple[Optional[str], Optional[str]]:
    if not groq_client: return None, "Groq client is not initialized."
    last_error = ""
    for model_name in GROQ_MODELS:
        try:
            response = await groq_client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=0.1, # Extremely low temperature to enforce strictness
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

# Common Coin Name to Ticker Mapping for accurate Binance fetch
COMMON_COINS = {
    "BITCOIN": "BTC", "ETHEREUM": "ETH", "SOLANA": "SOL", "RIPPLE": "XRP",
    "DOGECOIN": "DOGE", "TON": "TON", "POLYGON": "MATIC", "MATIC": "MATIC",
    "CARDANO": "ADA", "AVALANCHE": "AVAX", "CHAINLINK": "LINK", "BINANCE": "BNB", "BNB": "BNB"
}

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
# 1. LIVE BINANCE DATA FETCHER (MANDATORY ARCHITECTURE)
# ─────────────────────────────────────────────────────────────
async def fetch_live_market_data(user_text: str) -> str:
    """Detects coins in user text and fetches real-time data from Binance API."""
    words = re.findall(r'\b[A-Za-z]+\b', user_text.upper())
    tickers_to_check = set()
    
    for word in words:
        if word in COMMON_COINS:
            tickers_to_check.add(COMMON_COINS[word] + "USDT")
        elif 2 <= len(word) <= 6:
            tickers_to_check.add(word + "USDT")
            
    if not tickers_to_check:
        return ""
        
    results = []
    async with aiohttp.ClientSession() as session:
        for symbol in list(tickers_to_check)[:5]: # Limit to max 5 coins to prevent delay
            try:
                url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
                async with session.get(url, timeout=2.0) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        price = float(data['lastPrice'])
                        high = float(data['highPrice'])
                        low = float(data['lowPrice'])
                        change = float(data['priceChangePercent'])
                        vol = float(data['quoteVolume'])
                        
                        coin_name = symbol.replace('USDT', '')
                        results.append(
                            f"[{coin_name}] CURRENT LIVE PRICE: ${price:g} | "
                            f"24h Change: {change:+.2f}% | "
                            f"24h High: ${high:g} | 24h Low: ${low:g} | Volume: ${vol:,.0f}"
                        )
            except Exception:
                continue
    
    return "\n".join(results)

# ─────────────────────────────────────────────────────────────
# 2. REAL-TIME NEWS ANALYSIS (STRICT NO-LINK MODE)
# ─────────────────────────────────────────────────────────────
async def generate_seo_news_post(title: str, summary: str, pub_date_str: str) -> Optional[str]:
    system_prompt = """You are a strict institutional crypto news analyst.
CRITICAL RULES:
1. NO LINKS: Do not include ANY external links, URLs, or "Read more" text.
2. NO HALLUCINATION: Only use the provided Title and Summary.
3. LANGUAGE: HEADLINE MUST be in Bengali. IMPACT and DRIVERS MUST be in English.
4. FORMAT: Output exactly the 4 lines below. No Markdown asterisks.

Format:
HEADLINE: [Bengali Headline, max 12 words. Start with 🔴, 🟢, or ⚪]
IMPACT: [Bullish / Bearish / Neutral - 1 short line analysis]
DRIVERS: [1-2 lines WHY this matters based on text]
TAGS: [#Tag1 #Tag2 #Tag3 - Crypto tags]"""

    user_prompt = f"Title: {title}\nSummary: {summary}"

    res, err = await get_groq_response([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ], max_tokens=800)

    if not res: return None

    headline = impact = drivers = tags = ""
    for line in res.split('\n'):
        if line.startswith("HEADLINE:"): headline = line.replace("HEADLINE:", "").strip()
        elif line.startswith("IMPACT:"): impact = line.replace("IMPACT:", "").strip()
        elif line.startswith("DRIVERS:"): drivers = line.replace("DRIVERS:", "").strip()
        elif line.startswith("TAGS:"): tags = line.replace("TAGS:", "").strip()

    if not headline or not impact: return None

    # NO URL INCLUDED AS PER MANDATE
    final_post = (
        f"{headline}\n\n"
        f"🕒 <b>Time:</b> {pub_date_str}\n"
        f"📊 <b>Market Impact:</b> {impact}\n"
        f"💡 <b>Key Drivers:</b> {drivers}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🏷️ <b>Tags:</b> {tags}"
    )
    return final_post

async def background_market_scanner(app: Application):
    await asyncio.sleep(5)
    logger.info("Strict Real-time RSS scanner started...")
    
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            
            for url in RSS_FEEDS:
                feed = await asyncio.to_thread(feedparser.parse, url)
                
                for entry in feed.entries:
                    nid = entry.get("id", entry.link)
                    if nid in seen_news_ids: continue
                    
                    if hasattr(entry, 'published_parsed') and entry.published_parsed:
                        pub_ts = calendar.timegm(entry.published_parsed)
                        pub_time = datetime.fromtimestamp(pub_ts, tz=timezone.utc)
                        
                        if now_utc - pub_time > timedelta(hours=1): # Stricter 1-hour rule
                            seen_news_ids.add(nid)
                            continue
                    else:
                        continue

                    seen_news_ids.add(nid)
                    pub_date_str = pub_time.strftime("%d %b %Y, %H:%M UTC")
                    title = entry.title
                    summary = entry.get("summary", "")[:500]

                    broadcast_text = await generate_seo_news_post(title, summary, pub_date_str)
                    if not broadcast_text: continue

                    for ch_id in list(approved_channels):
                        try:
                            await app.bot.send_message(
                                chat_id=ch_id,
                                text=broadcast_text,
                                parse_mode=ParseMode.HTML,
                                disable_web_page_preview=True
                            )
                        except Exception:
                            pass
                    
                    await asyncio.sleep(5)
                    
        except Exception as e:
            logger.error(f"Scanner Loop error: {e}")

        await asyncio.sleep(300) # Fast Scan: every 5 minutes

# ─────────────────────────────────────────────────────────────
# 3. CHANNEL APPROVAL & REJECTION LOGIC
# ─────────────────────────────────────────────────────────────
async def bot_added_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    if not result: return
    chat = result.chat
    new_status = result.new_chat_member.status
    
    if new_status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER]:
        if chat.id not in approved_channels:
            keyboard = [[
                InlineKeyboardButton("✅ Approve", callback_data=f"chnl_apprv_{chat.id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"chnl_rejct_{chat.id}")
            ]]
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"📢 <b>Channel Alert!</b>\n{chat.title} (ID: <code>{chat.id}</code>)",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

async def channel_rejection_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID: return ConversationHandler.END

    cid = int(query.data.split("_")[2])
    context.user_data['target_channel'] = cid
    await query.edit_message_text(f"📝 Rejecting Channel <code>{cid}</code>.\nType custom leave message:", parse_mode=ParseMode.HTML)
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
            await context.bot.send_message(
                chat_id=target_cid,
                text=final_msg,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            await context.bot.leave_chat(chat_id=target_cid)
            await update.message.reply_text("✅ Message sent & bot left channel.")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Error: {e}")
        
        context.user_data.pop('target_channel', None)
    
    return ConversationHandler.END

async def cancel_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

# ─────────────────────────────────────────────────────────────
# 4. PRIVATE CHATBOT & REAL-TIME INJECTION
# ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are "Crypto Intel AI" — an elite crypto analyst.
CRITICAL MANDATES:
1. ZERO CLUTTER: NEVER include introductions, disclaimers, external links, third-party mentions, or apologies. Be precise and to-the-point.
2. LIVE DATA ONLY: NEVER output cryptocurrency prices from your internal training data. I will provide real-time LIVE data in the context if the user asks. You MUST base your answer strictly on that live data.
3. LANGUAGE: Detect user's language automatically and reply in the same language.
4. OUT OF BOUNDS: Refuse any non-crypto/non-finance questions immediately and briefly."""

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    user_id = update.effective_user.id
    if update.effective_chat.type != ChatType.PRIVATE: return

    if user_id not in allowed_users:
        if user_id in pending_users:
            await update.message.reply_text("⏳ Request pending.")
        else:
            await update.message.reply_text("🔒 Access Denied. Use /start.")
        return

    text = update.message.text.strip()
    await context.bot.send_chat_action(chat_id=user_id, action="typing")

    # 🔴 CORE FEATURE: FETCH LIVE DATA BEFORE LLM CALL 🔴
    live_market_data = await fetch_live_market_data(text)
    
    dynamic_system = SYSTEM_PROMPT
    if live_market_data:
        dynamic_system += (
            "\n\n🚨 [MANDATORY REAL-TIME DATA FETCHED JUST NOW] 🚨\n"
            f"{live_market_data}\n"
            "INSTRUCTION: The user asked about a price. YOU MUST USE THE DATA WRITTEN ABOVE. DO NOT guess or use old memory."
        )

    messages = [{"role": "system", "content": dynamic_system}]
    messages.extend(user_histories[user_id])
    messages.append({"role": "user", "content": text})

    response_text, error_details = await get_groq_response(messages, 2000)

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
        await update.message.reply_text(f"🌟 <b>Welcome {html.escape(user.first_name)}!</b>\nI am connected to live market data. Ask me any crypto price.", parse_mode=ParseMode.HTML)
    else:
        if user.id in pending_users:
            await update.message.reply_text("⏳ Request pending.")
            return
        pending_users.add(user.id)
        await update.message.reply_text("🔒 Request sent to admin.")
        
        keyboard = [[
            InlineKeyboardButton("✅ Allow", callback_data=f"usr_apprv_{user.id}"),
            InlineKeyboardButton("❌ Deny", callback_data=f"usr_rejct_{user.id}")
        ]]
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🔔 <b>Access Request</b>\nUser: <code>{user.id}</code>",
            parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def standard_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID: return
    data = query.data

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
        await application.bot.send_message(ADMIN_ID, "🟢 <b>Bot System v6.0 Online!</b>\nStrict Live API mode activated.", parse_mode=ParseMode.HTML)
    except: pass

def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

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

    loop = asyncio.get_event_loop()
    loop.create_task(background_market_scanner(application))

    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
