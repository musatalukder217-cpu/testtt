"""
╔══════════════════════════════════════════════════════════════════╗
║          CRYPTO INTELLIGENCE TELEGRAM BOT v3.1                  ║
║          24/7 Automated All-in-One Crypto Analyst               ║
║          Powered by Groq AI + Binance + Telegram                ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import re
import json
import html
import time
import hashlib
import logging
import asyncio
from datetime import datetime, timezone
from collections import defaultdict
from typing import Optional, Tuple

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
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatType

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

# ✅ FIXED: Model now configurable + safe default
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-70b-versatile").strip()

assert TELEGRAM_BOT_TOKEN, "TELEGRAM_BOT_TOKEN is missing!"
assert ADMIN_ID, "ADMIN_ID is missing!"
assert GROQ_API_KEY, "GROQ_API_KEY is missing!"

# ─────────────────────────────────────────────────────────────
# GROQ CLIENT (Async)
# ─────────────────────────────────────────────────────────────
groq_client = AsyncGroq(api_key=GROQ_API_KEY)

# ─────────────────────────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are "Crypto Intel AI" — a world-class, encyclopedic crypto intelligence analyst. Your personality is elegant, professional, and precise.

GLOBAL LANGUAGE SUPPORT:
━━━━━━━━━━━━━━━━━━━━━━
Automatically detect the language the user is speaking (e.g., Bengali, English, Hindi, Arabic, etc.) and RESPOND IN THAT EXACT SAME LANGUAGE. If asked in Bengali, reply in fluent Bengali.

YOUR COMPLETE KNOWLEDGE DOMAIN:
━━━━━━━━━━━━━━━━━━━━━━
1. All Cryptocurrencies (Fundamentals, Tokenomics, History).
2. Trading Expertise (TA, Patterns, Support/Resistance, Volume).
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
approved_channels: set[int] = set()

if PUBLIC_CHANNEL_ID:
    try:
        approved_channels.add(int(PUBLIC_CHANNEL_ID))
    except ValueError:
        pass

last_groq_call: float = 0.0
GROQ_MIN_INTERVAL = 1.0
MAX_HISTORY_LENGTH = 20

# ─────────────────────────────────────────────────────────────
# UTILITY FUNCTIONS
# ─────────────────────────────────────────────────────────────
def owner_link() -> str:
    if OWNER_USERNAME:
        username = OWNER_USERNAME.lstrip("@")
        return f'<a href="https://t.me/{username}">{html.escape(OWNER_NAME)}</a>'
    return html.escape(OWNER_NAME)


async def rate_limited_groq_call(messages: list[dict], max_tokens: int = 2048) -> Tuple[Optional[str], Optional[str]]:
    """Call Groq API asynchronously and return (result, error_message)."""
    global last_groq_call
    now = time.time()
    elapsed = now - last_groq_call
    if elapsed < GROQ_MIN_INTERVAL:
        await asyncio.sleep(GROQ_MIN_INTERVAL - elapsed)

    try:
        last_groq_call = time.time()
        response = await groq_client.chat.completions.create(
            model=GROQ_MODEL,  # ✅ FIXED: uses env variable
            messages=messages,
            temperature=0.7,
            max_tokens=max_tokens,
            top_p=0.9,
        )
        return response.choices[0].message.content, None
    except Exception as e:
        logger.error(f"Groq API error: {e}")
        return None, str(e)

# ─────────────────────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    if user.id in allowed_users:
        welcome = (
            f"🌟 <b>Welcome back, {html.escape(user.first_name)}!</b>\n\n"
            "I'm your <b>Crypto Intelligence AI</b>. I speak all languages!\n"
            "Ask me anything about crypto in Bengali, English, or any language you prefer.\n\n"
            f"🤖 <i>Owner: {owner_link()}</i>"
        )
        await update.message.reply_text(welcome, parse_mode=ParseMode.HTML)
    else:
        denied_msg = "🔒 <b>Access Denied</b>\nWait for admin approval."
        await update.message.reply_text(denied_msg, parse_mode=ParseMode.HTML)

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Allow", callback_data=f"allow_user:{user.id}"),
                InlineKeyboardButton("❌ Deny", callback_data=f"deny_user:{user.id}"),
            ]
        ])
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"🔔 <b>Access Request</b>\n"
                f"User: <a href='tg://user?id={user.id}'>{html.escape(user.first_name)}</a>\n"
                f"ID: <code>{user.id}</code>"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in allowed_users:
        user_histories[user_id] = []
        await update.message.reply_text("🗑️ Chat history cleared!")


async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
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
        await update.message.reply_text(
            f"🚫 User <code>{target_id}</code> has been blocked.",
            parse_mode=ParseMode.HTML,
        )
    except ValueError:
        await update.message.reply_text("Invalid User ID.")


async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /unblock <user_id>")
        return
    try:
        target_id = int(context.args[0])
        allowed_users.add(target_id)
        await update.message.reply_text(
            f"✅ User <code>{target_id}</code> has been unblocked/granted access.",
            parse_mode=ParseMode.HTML,
        )
    except ValueError:
        await update.message.reply_text("Invalid User ID.")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not allowed_users:
        await update.message.reply_text("No allowed users.")
        return

    await update.message.reply_text("👥 <b>Allowed Users List:</b>", parse_mode=ParseMode.HTML)

    for uid in list(allowed_users):
        is_admin = uid == ADMIN_ID
        text = f"👤 User ID: <code>{uid}</code> {'👑 (Admin)' if is_admin else ''}"

        keyboard = []
        if not is_admin:
            keyboard.append(
                [InlineKeyboardButton("❌ Block User", callback_data=f"block_user:{uid}")]
            )

        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)


# ─────────────────────────────────────────────────────────────
# CALLBACK QUERY HANDLER
# ─────────────────────────────────────────────────────────────
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return
    data = query.data

    if data.startswith("allow_user:"):
        uid = int(data.split(":")[1])
        allowed_users.add(uid)
        await query.edit_message_text(
            f"✅ User <code>{uid}</code> approved.", parse_mode=ParseMode.HTML
        )
        try:
            await context.bot.send_message(
                uid,
                "🎉 <b>Access Granted!</b>\nYou can now chat with me.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    elif data.startswith("deny_user:"):
        uid = int(data.split(":")[1])
        allowed_users.discard(uid)
        await query.edit_message_text(
            f"❌ User <code>{uid}</code> denied.", parse_mode=ParseMode.HTML
        )

    elif data.startswith("block_user:"):
        uid = int(data.split(":")[1])
        allowed_users.discard(uid)
        user_histories.pop(uid, None)
        await query.edit_message_text(
            f"🚫 User <code>{uid}</code> has been blocked.", parse_mode=ParseMode.HTML
        )


# ─────────────────────────────────────────────────────────────
# MESSAGE HANDLER — PERSONAL CHATBOT
# ─────────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    user_id = update.effective_user.id
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    if user_id not in allowed_users:
        await update.message.reply_text("🔒 Access Denied. You are blocked or not approved.")
        return

    text = update.message.text.strip()
    await context.bot.send_chat_action(chat_id=user_id, action="typing")

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(user_histories[user_id])
    messages.append({"role": "user", "content": text})

    response_text, error_details = await rate_limited_groq_call(messages, max_tokens=3000)

    if response_text:
        user_histories[user_id].append({"role": "user", "content": text})
        user_histories[user_id].append({"role": "assistant", "content": response_text})
        if len(user_histories[user_id]) > MAX_HISTORY_LENGTH:
            user_histories[user_id] = user_histories[user_id][-MAX_HISTORY_LENGTH:]

        try:
            await update.message.reply_text(response_text, parse_mode=ParseMode.HTML)
        except Exception:
            await update.message.reply_text(response_text)
    else:
        err_msg = (
            "❌ <b>Groq API Connection Failed!</b>\n\n"
            f"<b>Error Details:</b>\n<code>{html.escape(str(error_details))}</code>\n\n"
            "💡 <b>How to Fix:</b>\n"
            "1. Check your <code>GROQ_API_KEY</code> in Railway Variables\n"
            f"2. Current model: <code>{html.escape(GROQ_MODEL)}</code>\n"
            "3. Try changing <code>GROQ_MODEL</code> to: <code>llama-3.1-8b-instant</code> or <code>llama3-70b-8192</code>"
        )
        await update.message.reply_text(err_msg, parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────────────────────────
# MAIN APPLICATION SETUP
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
            f"🟢 <b>Bot Updated (v3.1)</b>\n"
            f"Model: <code>{html.escape(GROQ_MODEL)}</code>\n"
            f"Debug Mode Active.",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


def main():
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("clear", cmd_clear))
    application.add_handler(CommandHandler("users", cmd_users))
    application.add_handler(CommandHandler("block", cmd_block))
    application.add_handler(CommandHandler("unblock", cmd_unblock))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
            handle_message,
        )
    )

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
