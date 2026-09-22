"""
╔══════════════════════════════════════════════════════════════════╗
║          CRYPTO INTELLIGENCE TELEGRAM BOT v4.0                  ║
║          Merged with your working API System                    ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import html
import time
import logging
import asyncio
from collections import defaultdict
from typing import Optional, Tuple

import feedparser
import ccxt.async_support as ccxt
from groq import Groq  # <-- আপনার কাজ করা কোড অনুযায়ী Sync ক্লায়েন্ট
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
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatMemberStatus, ChatType

# ─────────────────────────────────────────────────────────────
# LOGGING
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
# GROQ API SYSTEM (Taken perfectly from your working code)
# ─────────────────────────────────────────────────────────────
try:
    groq_client = Groq(api_key=GROQ_API_KEY)
except Exception as e:
    logger.error(f"Groq Client Init Failed: {e}")
    groq_client = None

# আপনার কাজ করা মডেলটি ১ নম্বরে রাখা হয়েছে
GROQ_MODELS = [
    "openai/gpt-oss-120b",     # আপনার কাজ করা কোডের মডেল
    "llama-3.1-8b-instant",    # সবচেয়ে ফাস্ট ও ফ্রি ব্যাকআপ
    "llama-3.3-70b-versatile"
]

def get_groq_response_sync(messages: list[dict], max_tokens: int = 2048) -> Tuple[Optional[str], Optional[str]]:
    """Sync API call matching your exact working logic."""
    if not groq_client:
        return None, "Groq client is not initialized."

    last_error = ""
    for model_name in GROQ_MODELS:
        try:
            response = groq_client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=0.7,
                max_tokens=max_tokens
            )
            return response.choices[0].message.content, None
        except Exception as e:
            last_error = str(e)
            logger.warning(f"Model {model_name} failed. Trying fallback...")
            continue
    return None, last_error

# ─────────────────────────────────────────────────────────────
# SYSTEM PROMPT (Multi-Language & Domain Guardrail)
# ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are "Crypto Intel AI" — a world-class, encyclopedic crypto intelligence analyst. Your personality is elegant, professional, and precise.

GLOBAL LANGUAGE SUPPORT (CRITICAL RULE):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Automatically detect the language the user is speaking (e.g., Bengali, English, Banglish, Hindi, Arabic) and RESPOND IN THAT EXACT SAME LANGUAGE. If asked in Bengali, reply in fluent Bengali.

YOUR COMPLETE KNOWLEDGE DOMAIN:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. All Cryptocurrencies (Fundamentals, Tokenomics, History).
2. Trading Expertise (TA, Patterns, Support/Resistance, Volume, SMC).
3. Leverage & Liquidation mechanics.
4. Exchanges & Wallets (Binance, DEXs, MetaMask ops).
5. On-Chain Intelligence (Whales, TVL).
6. Macroeconomics & Geopolitics affecting crypto.

STRICT DOMAIN GUARDRAIL:
━━━━━━━━━━━━━━━━━━━━━━
If ANY question falls outside cryptocurrency, trading, blockchain, DeFi, or related financial markets, politely decline in the user's language. 
(Example in Bengali: "দুঃখিত, আমার কাছে এই ধরনের কোনো ডাটা নেই। আমি শুধুমাত্র ক্রিপ্টোকারেন্সি ও ট্রেডিং সম্পর্কিত বিষয় বিশ্লেষণে সক্ষম।")"""

# ─────────────────────────────────────────────────────────────
# IN-MEMORY STORAGE
# ─────────────────────────────────────────────────────────────
user_histories: dict[int, list[dict]] = defaultdict(list)
allowed_users: set[int] = {ADMIN_ID}
pending_users: set[int] = set()
approved_channels: set[int] = set()

if PUBLIC_CHANNEL_ID:
    try:
        approved_channels.add(int(PUBLIC_CHANNEL_ID))
    except ValueError:
        pass

MAX_HISTORY_LENGTH = 10

# ─────────────────────────────────────────────────────────────
# UTILITY FUNCTIONS
# ─────────────────────────────────────────────────────────────
def owner_link() -> str:
    if OWNER_USERNAME:
        username = OWNER_USERNAME.lstrip("@")
        return f'<a href="https://t.me/{username}">{html.escape(OWNER_NAME)}</a>'
    return html.escape(OWNER_NAME)

# ─────────────────────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.effective_chat.type != ChatType.PRIVATE: return

    if user.id in allowed_users:
        welcome = (
            f"🌟 <b>Welcome back, {html.escape(user.first_name)}!</b>\n\n"
            "I'm your <b>Crypto Intelligence AI</b>. I speak all languages!\n"
            "Ask me anything about crypto in Bengali, English, or any language you prefer.\n\n"
            f"🤖 <i>Owner: {owner_link()}</i>"
        )
        await update.message.reply_text(welcome, parse_mode=ParseMode.HTML)
    else:
        if user.id in pending_users:
            await update.message.reply_text("⏳ Your access request is already pending admin approval.")
            return

        pending_users.add(user.id)
        denied_msg = "🔒 <b>Access Denied</b>\nYour request has been sent to the admin. Please wait."
        await update.message.reply_text(denied_msg, parse_mode=ParseMode.HTML)
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Allow", callback_data=f"allow_user:{user.id}"),
             InlineKeyboardButton("❌ Deny", callback_data=f"deny_user:{user.id}")]
        ])
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🔔 <b>New Access Request</b>\nUser: <a href='tg://user?id={user.id}'>{html.escape(user.first_name)}</a>\nID: <code>{user.id}</code>",
            parse_mode=ParseMode.HTML, reply_markup=keyboard
        )

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in allowed_users:
        user_histories[user_id] = []
        await update.message.reply_text("🗑️ Chat history cleared!")

async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not context.args:
        await update.message.reply_text("Usage: /block <user_id>")
        return
    try:
        target_id = int(context.args[0])
        if target_id == ADMIN_ID:
            await update.message.reply_text("⚠️ You cannot block yourself.")
            return
        allowed_users.discard(target_id)
        user_histories.pop(target_id, None)
        await update.message.reply_text(f"🚫 User <code>{target_id}</code> has been blocked.", parse_mode=ParseMode.HTML)
    except ValueError:
        await update.message.reply_text("Invalid User ID.")

async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not context.args:
        await update.message.reply_text("Usage: /unblock <user_id>")
        return
    try:
        target_id = int(context.args[0])
        allowed_users.add(target_id)
        await update.message.reply_text(f"✅ User <code>{target_id}</code> has been unblocked/granted access.", parse_mode=ParseMode.HTML)
    except ValueError:
        await update.message.reply_text("Invalid User ID.")

async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not allowed_users:
        await update.message.reply_text("No allowed users.")
        return

    await update.message.reply_text("👥 <b>Allowed Users List:</b>", parse_mode=ParseMode.HTML)
    for uid in list(allowed_users):
        is_admin = (uid == ADMIN_ID)
        text = f"👤 User ID: <code>{uid}</code> {'👑 (Admin)' if is_admin else ''}"
        keyboard = []
        if not is_admin:
            keyboard.append([InlineKeyboardButton("❌ Block User", callback_data=f"block_user:{uid}")])
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

# ─────────────────────────────────────────────────────────────
# CALLBACK QUERY HANDLER
# ─────────────────────────────────────────────────────────────
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID: return
    data = query.data

    if data.startswith("allow_user:"):
        uid = int(data.split(":")[1])
        allowed_users.add(uid)
        pending_users.discard(uid)
        await query.edit_message_text(f"✅ User <code>{uid}</code> approved.", parse_mode=ParseMode.HTML)
        try:
            await context.bot.send_message(uid, "🎉 <b>Access Granted!</b>\nYou can now chat with me.", parse_mode=ParseMode.HTML)
        except: pass

    elif data.startswith("deny_user:"):
        uid = int(data.split(":")[1])
        allowed_users.discard(uid)
        pending_users.discard(uid)
        await query.edit_message_text(f"❌ User <code>{uid}</code> denied.", parse_mode=ParseMode.HTML)
        try:
            await context.bot.send_message(uid, "⛔ Sorry, your access request was denied.", parse_mode=ParseMode.HTML)
        except: pass

    elif data.startswith("block_user:"):
        uid = int(data.split(":")[1])
        allowed_users.discard(uid)
        user_histories.pop(uid, None)
        await query.edit_message_text(f"🚫 User <code>{uid}</code> has been blocked.", parse_mode=ParseMode.HTML)

# ─────────────────────────────────────────────────────────────
# MESSAGE HANDLER — CHATBOT
# ─────────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    # Run the exact working sync API call inside an async wrapper so it doesn't block Telegram
    response_text, error_details = await asyncio.to_thread(get_groq_response_sync, messages, 3000)

    if response_text:
        user_histories[user_id].append({"role": "user", "content": text})
        user_histories[user_id].append({"role": "assistant", "content": response_text})
        if len(user_histories[user_id]) > MAX_HISTORY_LENGTH * 2:
            user_histories[user_id] = user_histories[user_id][-MAX_HISTORY_LENGTH * 2:]

        # Clean markdown asterisks to avoid HTML parse errors
        clean_text = response_text.replace("**", "")
        try:
            await update.message.reply_text(clean_text, parse_mode=ParseMode.HTML)
        except:
            await update.message.reply_text(clean_text)
    else:
        err_msg = f"❌ <b>API Error!</b>\n\n<code>{html.escape(str(error_details))}</code>"
        await update.message.reply_text(err_msg, parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────────────────────────
# MAIN SETUP
# ─────────────────────────────────────────────────────────────
async def post_init(application: Application):
    commands = [
        BotCommand("start", "Start Bot"),
        BotCommand("clear", "Clear Chat History"),
        BotCommand("users", "Manage Users (Admin)"),
        BotCommand("block", "Block User (Admin)"),
        BotCommand("unblock", "Unblock User (Admin)"),
    ]
    await application.bot.set_my_commands(commands)
    try:
        await application.bot.send_message(
            ADMIN_ID, 
            "🟢 <b>Bot System Merged & Online!</b>\nUsing your working Groq Sync API.", 
            parse_mode=ParseMode.HTML
        )
    except: pass

def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("clear", cmd_clear))
    application.add_handler(CommandHandler("users", cmd_users))
    application.add_handler(CommandHandler("block", cmd_block))
    application.add_handler(CommandHandler("unblock", cmd_unblock))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, handle_message))

    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
