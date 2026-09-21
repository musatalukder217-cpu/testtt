"""
╔══════════════════════════════════════════════════════════════════╗
║          CRYPTO INTELLIGENCE TELEGRAM BOT v2.0                  ║
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
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from typing import Optional

import feedparser
import ccxt.async_support as ccxt
from groq import Groq
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

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
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
PUBLIC_CHANNEL_ID = os.getenv("PUBLIC_CHANNEL_ID", "")
OWNER_NAME = os.getenv("OWNER_NAME", "Admin")
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "")

assert TELEGRAM_BOT_TOKEN, "TELEGRAM_BOT_TOKEN is missing!"
assert ADMIN_ID, "ADMIN_ID is missing!"
assert GROQ_API_KEY, "GROQ_API_KEY is missing!"

# ─────────────────────────────────────────────────────────────
# GROQ CLIENT
# ─────────────────────────────────────────────────────────────
groq_client = Groq(api_key=GROQ_API_KEY)

# ─────────────────────────────────────────────────────────────
# SYSTEM PROMPT — INFINITE CRYPTO BRAIN
# ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are "Crypto Intel AI" — a world-class, encyclopedic crypto intelligence analyst. Your personality is elegant, professional, and precise.

YOUR COMPLETE KNOWLEDGE DOMAIN:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. **All Cryptocurrencies**: Bitcoin (BTC), Ethereum (ETH), every altcoin (SOL, ADA, XRP, DOGE, AVAX, DOT, MATIC, LINK, UNI, AAVE, etc.), memecoins (PEPE, SHIB, FLOKI, WIF, BONK), stablecoins (USDT, USDC, DAI), and their complete fundamentals, history, tokenomics, use-cases, founders, and roadmaps.

2. **Trading Expertise**: Technical Analysis (RSI, MACD, Bollinger Bands, Fibonacci, Ichimoku, EMA/SMA), chart patterns (Head & Shoulders, Double Top/Bottom, Wedges, Flags, Triangles, Cup & Handle), support/resistance levels, breakout strategies, volume analysis, funding rates, open interest, order book depth.

3. **Leverage & Liquidation**: How perpetual futures work, cross vs isolated margin, liquidation mechanics, short/long squeeze dynamics, funding rate arbitrage, position sizing, risk management.

4. **Exchanges & Wallets**: Complete operational knowledge of Binance, Bybit, OKX, KuCoin, Coinbase, Kraken, and DEXs (Uniswap, PancakeSwap, dYdX, GMX). How to trade, deposit, withdraw, set stop-losses, use advanced order types. Trust Wallet, MetaMask, Ledger — setup, seed phrases, network configurations.

5. **On-Chain Intelligence**: Whale wallet tracking, large transfers, exchange inflows/outflows, miner behavior, staking ratios, DeFi TVL changes, NFT market dynamics.

6. **Macroeconomics & Geopolitics**: Federal Reserve interest rates, CPI data, inflation metrics, USD strength (DXY), bond yields, quantitative easing/tightening, wars, sanctions, government crypto reserves (US Strategic BTC Reserve, El Salvador), regulatory actions (SEC, EU MiCA).

7. **Market Psychology**: Fear & Greed Index interpretation, market cycles (accumulation, markup, distribution, markdown), retail vs institutional behavior, FOMO/FUD dynamics.

STRICT DOMAIN GUARDRAIL:
━━━━━━━━━━━━━━━━━━━━━━
If ANY question falls outside cryptocurrency, trading, blockchain, DeFi, or related financial markets, respond EXACTLY with:
"দুঃখিত, আমার কাছে এই ধরনের কোনো ডাটা নেই। আমি শুধুমাত্র ক্রিপ্টোকারেন্সি, ট্রেডিং ও মার্কেট সম্পর্কিত বিষয় বিশ্লেষণে সক্ষম।"

RESPONSE STYLE:
━━━━━━━━━━━━━━
- Be thorough yet concise
- Use data-driven analysis
- Include relevant metrics when discussing coins
- Structure answers with clear formatting
- Always provide balanced perspectives (bull case + bear case)
- Never give direct financial advice; always frame as analysis
- Use professional, elegant tone throughout"""

# ─────────────────────────────────────────────────────────────
# NEWS ANALYSIS PROMPT
# ─────────────────────────────────────────────────────────────
NEWS_ANALYSIS_PROMPT = """You are a senior crypto market analyst. Analyze the following crypto news and provide a structured report.

RULES:
1. The HEADLINE must be written in beautiful, fluent Bengali (বাংলা).
2. ALL other analysis content must be in professional English.
3. Include the exact date and time provided.
4. Do NOT generically say "market will go up/down". Instead, analyze the depth and real impact.
5. For a SINGLE MEGA EVENT (war, major hack, ETF approval, country-level buy/sell), emphasize its massive impact separately.
6. For MULTIPLE SMALL NEWS items, explain the combined cluster sentiment and overall trend.

OUTPUT FORMAT (use this exact structure):

📰 [Bengali Headline Here]

🕐 Date & Time: {date_time}

📊 **Market Impact Analysis:**
[Detailed analysis of how this news affects the crypto market]

🎯 **Affected Assets:**
[List specific coins/tokens affected and how]

📈 **Sentiment:** [Bullish 🟢 / Bearish 🔴 / Neutral 🟡]
🔒 **Confidence Level:** [High / Medium / Low] — [percentage]%

⚡ **Key Takeaway:**
[One powerful concluding insight]

━━━━━━━━━━━━━━━━━━━━━━
🤖 Crypto Intel AI | Powered by Groq"""

# ─────────────────────────────────────────────────────────────
# LIQUIDATION ANALYSIS PROMPT
# ─────────────────────────────────────────────────────────────
LIQUIDATION_PROMPT = """You are analyzing a significant liquidation event in the crypto market.

Analyze this liquidation data and explain what happened, why, and what traders should watch for next.

OUTPUT FORMAT:

🔥 [Bengali headline about the liquidation event]

🕐 Time: {timestamp}

💥 **Liquidation Details:**
{details}

📊 **Root Cause Analysis:**
[Explain why this liquidation cascade happened]

⚠️ **What This Means:**
[Impact on market structure, potential follow-through]

📈 **Sentiment:** [Bullish 🟢 / Bearish 🔴 / Neutral 🟡]

━━━━━━━━━━━━━━━━━━━━━━
🤖 Crypto Intel AI | Powered by Groq"""

# ─────────────────────────────────────────────────────────────
# RSS FEEDS — TOP CRYPTO NEWS SOURCES
# ─────────────────────────────────────────────────────────────
RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cryptonews.com/news/feed/",
    "https://decrypt.co/feed",
    "https://bitcoinmagazine.com/.rss/full/",
    "https://www.theblock.co/rss.xml",
    "https://cryptopotato.com/feed/",
    "https://u.today/rss",
    "https://ambcrypto.com/feed/",
    "https://beincrypto.com/feed/",
]

# ─────────────────────────────────────────────────────────────
# IN-MEMORY STORAGE
# ─────────────────────────────────────────────────────────────
# User chat histories: {user_id: [{"role": ..., "content": ...}, ...]}
user_histories: dict[int, list[dict]] = defaultdict(list)

# Allowed users set
allowed_users: set[int] = {ADMIN_ID}

# Approved channels set
approved_channels: set[int] = set()
if PUBLIC_CHANNEL_ID:
    try:
        approved_channels.add(int(PUBLIC_CHANNEL_ID))
    except ValueError:
        pass

# Seen news hashes for deduplication
seen_news_hashes: set[str] = set()

# Pending rejection messages: {admin_msg_id: channel_id}
pending_reject_messages: dict[int, int] = {}

# Previous prices for spike detection
previous_prices: dict[str, float] = {}

# Rate limiting for Groq
last_groq_call: float = 0.0
GROQ_MIN_INTERVAL = 2.0  # seconds between calls

MAX_HISTORY_LENGTH = 20  # messages per user
MAX_SEEN_NEWS = 500  # max cached news hashes


# ─────────────────────────────────────────────────────────────
# UTILITY FUNCTIONS
# ─────────────────────────────────────────────────────────────
def news_hash(title: str, link: str) -> str:
    """Generate unique hash for a news item."""
    raw = f"{title.strip().lower()}|{link.strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def owner_link() -> str:
    """Generate clickable owner link without showing raw username."""
    if OWNER_USERNAME:
        username = OWNER_USERNAME.lstrip("@")
        return f'<a href="https://t.me/{username}">{html.escape(OWNER_NAME)}</a>'
    return html.escape(OWNER_NAME)


def truncate_history(user_id: int):
    """Keep only last N messages per user."""
    if len(user_histories[user_id]) > MAX_HISTORY_LENGTH:
        user_histories[user_id] = user_histories[user_id][-MAX_HISTORY_LENGTH:]


def clean_seen_news():
    """Prevent seen_news_hashes from growing indefinitely."""
    global seen_news_hashes
    if len(seen_news_hashes) > MAX_SEEN_NEWS:
        # Keep only the most recent half
        seen_list = list(seen_news_hashes)
        seen_news_hashes = set(seen_list[len(seen_list) // 2:])


async def rate_limited_groq_call(messages: list[dict], max_tokens: int = 2048) -> Optional[str]:
    """Call Groq API with rate limiting."""
    global last_groq_call

    now = time.time()
    elapsed = now - last_groq_call
    if elapsed < GROQ_MIN_INTERVAL:
        await asyncio.sleep(GROQ_MIN_INTERVAL - elapsed)

    try:
        last_groq_call = time.time()
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.7,
            max_tokens=max_tokens,
            top_p=0.9,
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Groq API error: {e}")
        return None


def is_crypto_related(text: str) -> bool:
    """Quick heuristic check if text might be crypto-related."""
    crypto_keywords = [
        "bitcoin", "btc", "ethereum", "eth", "crypto", "blockchain", "defi",
        "nft", "token", "coin", "altcoin", "trading", "exchange", "wallet",
        "binance", "bybit", "okx", "kucoin", "coinbase", "uniswap",
        "pancakeswap", "metamask", "trust wallet", "ledger", "staking",
        "yield", "liquidity", "airdrop", "ico", "ido", "launchpad",
        "bull", "bear", "long", "short", "leverage", "margin", "futures",
        "perpetual", "funding rate", "liquidation", "rsi", "macd",
        "bollinger", "fibonacci", "support", "resistance", "breakout",
        "whale", "on-chain", "halving", "mining", "pow", "pos",
        "solana", "sol", "cardano", "ada", "xrp", "ripple", "doge",
        "dogecoin", "shib", "pepe", "avax", "avalanche", "polygon",
        "matic", "link", "chainlink", "dot", "polkadot", "uni",
        "aave", "compound", "maker", "dai", "usdt", "usdc", "tether",
        "stable", "meme", "floki", "bonk", "wif", "bnb", "ton",
        "tron", "trx", "near", "apt", "aptos", "sui", "arb", "arbitrum",
        "op", "optimism", "layer", "l1", "l2", "rollup", "zk",
        "sec", "etf", "regulation", "fed", "interest rate", "cpi",
        "inflation", "dxy", "dollar", "macro", "geopolit",
        "কয়েন", "ক্রিপ্টো", "বিটকয়েন", "ইথেরিয়াম", "ট্রেডিং",
        "এক্সচেঞ্জ", "ওয়ালেট", "মার্কেট", "বাজার", "লিকুইডেশন",
        "chart", "candle", "volume", "order", "stop loss", "take profit",
        "dca", "hodl", "fomo", "fud", "fear", "greed", "market cap",
        "dominance", "gas fee", "gwei", "smart contract", "dapp",
    ]
    text_lower = text.lower()
    return any(kw in text_lower for kw in crypto_keywords)


# ─────────────────────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command with permission check."""
    user = update.effective_user
    user_id = user.id
    chat_type = update.effective_chat.type

    # Only handle in private chat
    if chat_type != ChatType.PRIVATE:
        return

    if user_id in allowed_users:
        welcome = (
            f"🌟 <b>Welcome back, {html.escape(user.first_name)}!</b>\n\n"
            "I'm your <b>Crypto Intelligence AI</b> — a living encyclopedia of the crypto world.\n\n"
            "💡 <b>What I can do:</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "📊 Deep analysis of any coin, token, or project\n"
            "📈 Technical analysis & chart pattern insights\n"
            "🏦 Exchange & wallet guidance (Binance, Bybit, MetaMask...)\n"
            "🐋 Whale movement & on-chain intelligence\n"
            "🌍 Macro & geopolitical impact on crypto\n"
            "🔥 Real-time liquidation & price spike alerts\n\n"
            "Simply type your question and I'll analyze it for you.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 <i>Powered by Groq AI | Owner: {owner_link()}</i>"
        )
        await update.message.reply_text(welcome, parse_mode=ParseMode.HTML)
    else:
        # Access denied — notify user
        denied_msg = (
            "🔒 <b>Access Denied</b>\n\n"
            "You are not authorized to use this bot.\n"
            "Your access request has been sent to the administrator.\n"
            "Please wait for approval.\n\n"
            f"🤖 <i>Crypto Intel AI | Owner: {owner_link()}</i>"
        )
        await update.message.reply_text(denied_msg, parse_mode=ParseMode.HTML)

        # Send approval request to admin
        user_mention = f'<a href="tg://user?id={user_id}">{html.escape(user.first_name)}</a>'
        username_info = f" (@{user.username})" if user.username else ""

        admin_msg = (
            f"🔔 <b>New Access Request</b>\n\n"
            f"👤 User: {user_mention}{username_info}\n"
            f"🆔 ID: <code>{user_id}</code>\n\n"
            "Grant this user access to the personal chatbot?"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Allow Chat", callback_data=f"allow_user:{user_id}"),
                InlineKeyboardButton("❌ Deny", callback_data=f"deny_user:{user_id}"),
            ]
        ])
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=admin_msg,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error(f"Failed to send access request to admin: {e}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot status (admin only)."""
    if update.effective_user.id != ADMIN_ID:
        return

    status_text = (
        "📊 <b>Bot Status Report</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Allowed Users: <code>{len(allowed_users)}</code>\n"
        f"📢 Approved Channels: <code>{len(approved_channels)}</code>\n"
        f"📰 Cached News Hashes: <code>{len(seen_news_hashes)}</code>\n"
        f"💬 Active Chat Sessions: <code>{len(user_histories)}</code>\n"
        f"📡 RSS Feeds Monitored: <code>{len(RSS_FEEDS)}</code>\n"
        f"🕐 Server Time: <code>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 <i>All systems operational</i>"
    )
    await update.message.reply_text(status_text, parse_mode=ParseMode.HTML)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear chat history for the user."""
    user_id = update.effective_user.id
    if user_id not in allowed_users:
        return

    user_histories[user_id] = []
    await update.message.reply_text(
        "🗑️ <b>Chat history cleared.</b>\n"
        "Your conversation has been reset. Feel free to start fresh!",
        parse_mode=ParseMode.HTML,
    )


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List allowed users (admin only)."""
    if update.effective_user.id != ADMIN_ID:
        return

    if not allowed_users:
        await update.message.reply_text("No allowed users.")
        return

    lines = ["👥 <b>Allowed Users:</b>\n━━━━━━━━━━━━━━━━━━"]
    for uid in allowed_users:
        tag = " 👑 (Admin)" if uid == ADMIN_ID else ""
        lines.append(f"• <code>{uid}</code>{tag}")
    lines.append(f"\nTotal: {len(allowed_users)}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────────────────────────
# CALLBACK QUERY HANDLER
# ─────────────────────────────────────────────────────────────
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks."""
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Only the admin can perform this action.", show_alert=True)
        return

    data = query.data

    # ── Allow/Deny User ──────────────────────────────────
    if data.startswith("allow_user:"):
        user_id = int(data.split(":")[1])
        allowed_users.add(user_id)

        await query.edit_message_text(
            f"✅ <b>User Approved</b>\n\n"
            f"🆔 User <code>{user_id}</code> now has chat access.",
            parse_mode=ParseMode.HTML,
        )

        # Notify the user
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "🎉 <b>Access Granted!</b>\n\n"
                    "Your request has been approved by the administrator.\n"
                    "You can now chat with me about anything crypto-related!\n\n"
                    "Simply type your question to get started.\n\n"
                    f"🤖 <i>Crypto Intel AI | Owner: {owner_link()}</i>"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error(f"Failed to notify user {user_id}: {e}")

    elif data.startswith("deny_user:"):
        user_id = int(data.split(":")[1])
        allowed_users.discard(user_id)

        await query.edit_message_text(
            f"❌ <b>User Denied</b>\n\n"
            f"🆔 User <code>{user_id}</code> was not granted access.",
            parse_mode=ParseMode.HTML,
        )

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "❌ <b>Access Denied</b>\n\n"
                    "Your access request was not approved.\n"
                    "Contact the bot owner for more information.\n\n"
                    f"🤖 <i>Crypto Intel AI | Owner: {owner_link()}</i>"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    # ── Approve/Reject Channel ──────────────────────────
    elif data.startswith("approve_channel:"):
        channel_id = int(data.split(":")[1])
        approved_channels.add(channel_id)

        await query.edit_message_text(
            f"✅ <b>Channel Approved</b>\n\n"
            f"📢 Channel <code>{channel_id}</code> is now active.\n"
            f"Bot will silently serve this channel (no welcome message).",
            parse_mode=ParseMode.HTML,
        )

    elif data.startswith("reject_channel:"):
        channel_id = int(data.split(":")[1])

        # Ask admin for custom leave message
        pending_reject_messages[ADMIN_ID] = channel_id
        context.user_data["awaiting_reject_msg"] = channel_id

        await query.edit_message_text(
            f"📝 <b>Custom Leave Message</b>\n\n"
            f"Channel: <code>{channel_id}</code>\n\n"
            f"Please type the message you want to post in the channel before leaving.\n"
            f"(Your message will automatically include the owner signature)",
            parse_mode=ParseMode.HTML,
        )


# ─────────────────────────────────────────────────────────────
# MESSAGE HANDLER — PERSONAL CHATBOT
# ─────────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle private messages — personal AI analyst."""
    if not update.message or not update.message.text:
        return

    user = update.effective_user
    user_id = user.id
    chat_type = update.effective_chat.type
    text = update.message.text.strip()

    # Only handle private messages
    if chat_type != ChatType.PRIVATE:
        return

    # Check if admin is providing a rejection message
    if user_id == ADMIN_ID and "awaiting_reject_msg" in context.user_data:
        channel_id = context.user_data.pop("awaiting_reject_msg")
        pending_reject_messages.pop(ADMIN_ID, None)

        # Build the leave message with owner signature
        leave_message = (
            f"{text}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Owner: {owner_link()}"
        )

        try:
            # Post message in the channel
            await context.bot.send_message(
                chat_id=channel_id,
                text=leave_message,
                parse_mode=ParseMode.HTML,
            )
            # Leave the channel
            await context.bot.leave_chat(channel_id)

            await update.message.reply_text(
                f"✅ <b>Done!</b>\n\n"
                f"Message posted and bot left channel <code>{channel_id}</code>.",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error(f"Failed to post leave message or leave channel {channel_id}: {e}")
            await update.message.reply_text(
                f"⚠️ Error: Could not complete the action.\n<code>{html.escape(str(e))}</code>",
                parse_mode=ParseMode.HTML,
            )
        return

    # Check permission
    if user_id not in allowed_users:
        await update.message.reply_text(
            "🔒 <b>Access Denied</b>\n\n"
            "You don't have permission to chat with me.\n"
            "Use /start to request access.\n\n"
            f"🤖 <i>Crypto Intel AI | Owner: {owner_link()}</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    # Show typing action
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Build conversation messages
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Add user's chat history (isolated per user)
    messages.extend(user_histories[user_id])

    # Add current message
    messages.append({"role": "user", "content": text})

    # Call Groq AI
    response_text = await rate_limited_groq_call(messages, max_tokens=3000)

    if response_text:
        # Save to history
        user_histories[user_id].append({"role": "user", "content": text})
        user_histories[user_id].append({"role": "assistant", "content": response_text})
        truncate_history(user_id)

        # Send response (handle long messages)
        if len(response_text) > 4000:
            # Split into chunks
            chunks = [response_text[i:i + 4000] for i in range(0, len(response_text), 4000)]
            for chunk in chunks:
                try:
                    await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
                except Exception:
                    await update.message.reply_text(chunk)
        else:
            try:
                await update.message.reply_text(response_text, parse_mode=ParseMode.HTML)
            except Exception:
                # If HTML parsing fails, send as plain text
                await update.message.reply_text(response_text)
    else:
        await update.message.reply_text(
            "⚠️ <b>Temporary Issue</b>\n\n"
            "I'm experiencing a brief connectivity issue with my AI engine.\n"
            "Please try again in a moment.\n\n"
            f"🤖 <i>Crypto Intel AI</i>",
            parse_mode=ParseMode.HTML,
        )


# ─────────────────────────────────────────────────────────────
# CHAT MEMBER HANDLER — CHANNEL ADD/REMOVE DETECTION
# ─────────────────────────────────────────────────────────────
async def bot_added_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detect when bot is added to a channel/group."""
    my_chat_member = update.my_chat_member
    if not my_chat_member:
        return

    chat = my_chat_member.chat
    new_status = my_chat_member.new_chat_member.status
    old_status = my_chat_member.old_chat_member.status

    # Bot was added as admin/member
    if new_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER):
        if old_status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED, None):
            chat_id = chat.id
            chat_title = chat.title or "Unknown"

            # Check if already approved
            if chat_id in approved_channels:
                logger.info(f"Bot added to already-approved channel: {chat_title} ({chat_id})")
                return

            # Not approved — alert admin
            logger.info(f"Bot added to unapproved channel: {chat_title} ({chat_id})")

            added_by = my_chat_member.from_user
            added_by_info = ""
            if added_by:
                added_by_mention = f'<a href="tg://user?id={added_by.id}">{html.escape(added_by.first_name)}</a>'
                username_info = f" (@{added_by.username})" if added_by.username else ""
                added_by_info = f"\n👤 Added by: {added_by_mention}{username_info}"

            alert_msg = (
                f"⚠️ <b>Unauthorized Channel Alert</b>\n\n"
                f"📢 Channel: <b>{html.escape(chat_title)}</b>\n"
                f"🆔 ID: <code>{chat_id}</code>"
                f"{added_by_info}\n\n"
                f"The bot was added to this channel but it's not approved.\n"
                f"What would you like to do?"
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Approve Channel", callback_data=f"approve_channel:{chat_id}"),
                    InlineKeyboardButton("❌ Reject & Leave", callback_data=f"reject_channel:{chat_id}"),
                ]
            ])

            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=alert_msg,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            except Exception as e:
                logger.error(f"Failed to alert admin about channel add: {e}")


# ─────────────────────────────────────────────────────────────
# NEWS MONITORING — RSS FEEDS
# ─────────────────────────────────────────────────────────────
async def fetch_and_analyze_news(context: ContextTypes.DEFAULT_TYPE):
    """Fetch RSS feeds, analyze with Groq, broadcast to channel."""
    if not PUBLIC_CHANNEL_ID:
        return

    try:
        channel_id = int(PUBLIC_CHANNEL_ID)
    except ValueError:
        channel_id = PUBLIC_CHANNEL_ID  # Handle @channel_username format

    if channel_id not in approved_channels and not isinstance(channel_id, str):
        return

    logger.info("Starting news fetch cycle...")
    new_articles = []

    for feed_url in RSS_FEEDS:
        try:
            feed = await asyncio.get_event_loop().run_in_executor(
                None, feedparser.parse, feed_url
            )

            for entry in feed.entries[:5]:  # Latest 5 per feed
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                summary = entry.get("summary", entry.get("description", "")).strip()
                published = entry.get("published", entry.get("updated", ""))

                if not title:
                    continue

                h = news_hash(title, link)
                if h in seen_news_hashes:
                    continue

                seen_news_hashes.add(h)
                new_articles.append({
                    "title": title,
                    "link": link,
                    "summary": summary[:500],
                    "published": published,
                    "source": feed_url.split("/")[2],
                })

        except Exception as e:
            logger.warning(f"Failed to fetch {feed_url}: {e}")
            continue

    clean_seen_news()

    if not new_articles:
        logger.info("No new articles found this cycle.")
        return

    logger.info(f"Found {len(new_articles)} new articles. Analyzing...")

    # Process articles (batch similar ones, or individual for major events)
    # Group by time proximity for cluster analysis
    if len(new_articles) >= 3:
        # Cluster analysis — multiple news items
        combined_news = "\n\n".join([
            f"📌 {a['title']}\n   Source: {a['source']}\n   Published: {a['published']}\n   Summary: {a['summary']}"
            for a in new_articles[:8]  # Max 8 for context window
        ])

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        messages = [
            {"role": "system", "content": NEWS_ANALYSIS_PROMPT},
            {"role": "user", "content": (
                f"Current time: {now_str}\n\n"
                f"Multiple crypto news items detected. Analyze the CLUSTER SENTIMENT:\n\n"
                f"{combined_news}\n\n"
                f"Provide a combined analysis showing the overall market trend from these news items."
            )}
        ]

        analysis = await rate_limited_groq_call(messages, max_tokens=2500)
        if analysis:
            try:
                await context.bot.send_message(
                    chat_id=channel_id,
                    text=analysis,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:
                try:
                    await context.bot.send_message(
                        chat_id=channel_id,
                        text=analysis,
                        disable_web_page_preview=True,
                    )
                except Exception as e:
                    logger.error(f"Failed to send cluster analysis: {e}")

    else:
        # Individual analysis for each article
        for article in new_articles:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            messages = [
                {"role": "system", "content": NEWS_ANALYSIS_PROMPT},
                {"role": "user", "content": (
                    f"Current time: {now_str}\n\n"
                    f"News Title: {article['title']}\n"
                    f"Source: {article['source']}\n"
                    f"Published: {article['published']}\n"
                    f"Summary: {article['summary']}\n"
                    f"Link: {article['link']}\n\n"
                    f"Analyze this single news item thoroughly."
                )}
            ]

            analysis = await rate_limited_groq_call(messages, max_tokens=2000)
            if analysis:
                try:
                    await context.bot.send_message(
                        chat_id=channel_id,
                        text=analysis,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                except Exception:
                    try:
                        await context.bot.send_message(
                            chat_id=channel_id,
                            text=analysis,
                            disable_web_page_preview=True,
                        )
                    except Exception as e:
                        logger.error(f"Failed to send analysis: {e}")

                await asyncio.sleep(3)  # Avoid flooding


# ─────────────────────────────────────────────────────────────
# MARKET MONITORING — BINANCE VIA CCXT
# ─────────────────────────────────────────────────────────────
MONITORED_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT",
    "ADA/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT", "MATIC/USDT",
    "BNB/USDT", "TON/USDT", "NEAR/USDT", "APT/USDT", "SUI/USDT",
    "ARB/USDT", "OP/USDT", "PEPE/USDT", "SHIB/USDT", "WIF/USDT",
]

PRICE_SPIKE_THRESHOLD = 5.0  # 5% price change triggers alert
LARGE_LIQUIDATION_THRESHOLD = 1_000_000  # $1M+ liquidation alert


async def monitor_market(context: ContextTypes.DEFAULT_TYPE):
    """Monitor Binance for price spikes and anomalies."""
    if not PUBLIC_CHANNEL_ID:
        return

    try:
        channel_id = int(PUBLIC_CHANNEL_ID)
    except ValueError:
        channel_id = PUBLIC_CHANNEL_ID

    exchange = ccxt.binance({
        "enableRateLimit": True,
        "options": {"defaultType": "future"},
    })

    try:
        tickers = await exchange.fetch_tickers(MONITORED_SYMBOLS)

        alerts = []

        for symbol, ticker in tickers.items():
            current_price = ticker.get("last", 0)
            price_change_pct = ticker.get("percentage", 0)
            volume_24h = ticker.get("quoteVolume", 0)

            if not current_price:
                continue

            prev_price = previous_prices.get(symbol)

            # Check for sudden spike (compared to our own tracking)
            if prev_price and prev_price > 0:
                our_change_pct = ((current_price - prev_price) / prev_price) * 100
                if abs(our_change_pct) >= PRICE_SPIKE_THRESHOLD:
                    direction = "📈 SURGE" if our_change_pct > 0 else "📉 CRASH"
                    alerts.append({
                        "symbol": symbol,
                        "direction": direction,
                        "change_pct": our_change_pct,
                        "current_price": current_price,
                        "prev_price": prev_price,
                        "volume_24h": volume_24h,
                        "exchange_24h_change": price_change_pct,
                    })

            # Also check 24h change from exchange
            elif price_change_pct and abs(price_change_pct) >= 8:
                direction = "📈 SURGE" if price_change_pct > 0 else "📉 CRASH"
                alerts.append({
                    "symbol": symbol,
                    "direction": direction,
                    "change_pct": price_change_pct,
                    "current_price": current_price,
                    "prev_price": prev_price or current_price,
                    "volume_24h": volume_24h,
                    "exchange_24h_change": price_change_pct,
                })

            previous_prices[symbol] = current_price

        await exchange.close()

        if alerts:
            for alert in alerts[:3]:  # Max 3 alerts per cycle
                await process_price_alert(context, channel_id, alert)

    except Exception as e:
        logger.error(f"Market monitoring error: {e}")
        try:
            await exchange.close()
        except Exception:
            pass


async def process_price_alert(context, channel_id, alert: dict):
    """Analyze and broadcast a price alert."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    alert_text = (
        f"Symbol: {alert['symbol']}\n"
        f"Direction: {alert['direction']}\n"
        f"Change: {alert['change_pct']:.2f}%\n"
        f"Current Price: ${alert['current_price']:,.4f}\n"
        f"24h Volume: ${alert['volume_24h']:,.0f}\n"
        f"24h Exchange Change: {alert.get('exchange_24h_change', 'N/A')}%"
    )

    messages = [
        {"role": "system", "content": LIQUIDATION_PROMPT.replace("{timestamp}", now_str)},
        {"role": "user", "content": (
            f"Time: {now_str}\n\n"
            f"SUDDEN PRICE MOVEMENT DETECTED:\n\n"
            f"{alert_text}\n\n"
            f"Analyze why this sudden {alert['direction'].split()[1].lower()} might have occurred. "
            f"Consider recent market events, whale activity, liquidation cascades, or breaking news. "
            f"Provide the analysis in the structured format."
        )}
    ]

    analysis = await rate_limited_groq_call(messages, max_tokens=2000)
    if analysis:
        try:
            await context.bot.send_message(
                chat_id=channel_id,
                text=analysis,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            try:
                await context.bot.send_message(
                    chat_id=channel_id,
                    text=analysis,
                )
            except Exception as e:
                logger.error(f"Failed to send price alert: {e}")


async def monitor_liquidations(context: ContextTypes.DEFAULT_TYPE):
    """Monitor for large liquidation events via Binance futures."""
    if not PUBLIC_CHANNEL_ID:
        return

    try:
        channel_id = int(PUBLIC_CHANNEL_ID)
    except ValueError:
        channel_id = PUBLIC_CHANNEL_ID

    exchange = ccxt.binance({
        "enableRateLimit": True,
        "options": {"defaultType": "future"},
    })

    try:
        # Fetch recent trades for major pairs to detect liquidation-like activity
        # Using funding rate as a proxy for squeeze detection
        funding_alerts = []

        for symbol in ["BTC/USDT", "ETH/USDT", "SOL/USDT"]:
            try:
                funding = await exchange.fetch_funding_rate(symbol)
                rate = funding.get("fundingRate", 0)

                if rate and abs(rate) > 0.001:  # Extreme funding rate (>0.1%)
                    direction = "Long-heavy (Short Squeeze risk)" if rate > 0 else "Short-heavy (Long Squeeze risk)"
                    funding_alerts.append({
                        "symbol": symbol,
                        "rate": rate,
                        "direction": direction,
                    })
            except Exception:
                continue

        await exchange.close()

        if funding_alerts:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

            details = "\n".join([
                f"• {fa['symbol']}: Funding Rate {fa['rate']*100:.4f}% → {fa['direction']}"
                for fa in funding_alerts
            ])

            messages = [
                {"role": "system", "content": LIQUIDATION_PROMPT.replace("{timestamp}", now_str).replace("{details}", details)},
                {"role": "user", "content": (
                    f"Time: {now_str}\n\n"
                    f"EXTREME FUNDING RATE DETECTED — Possible Squeeze:\n\n"
                    f"{details}\n\n"
                    f"Analyze the liquidation/squeeze risk and market implications."
                )}
            ]

            analysis = await rate_limited_groq_call(messages, max_tokens=2000)
            if analysis:
                try:
                    await context.bot.send_message(
                        chat_id=channel_id,
                        text=analysis,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    try:
                        await context.bot.send_message(
                            chat_id=channel_id,
                            text=analysis,
                        )
                    except Exception as e:
                        logger.error(f"Failed to send liquidation alert: {e}")

    except Exception as e:
        logger.error(f"Liquidation monitoring error: {e}")
        try:
            await exchange.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# ERROR HANDLER
# ─────────────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors gracefully."""
    logger.error(f"Exception while handling an update: {context.error}")

    if update and isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ An unexpected error occurred. Please try again.",
            )
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# POST-INIT — SET BOT COMMANDS
# ─────────────────────────────────────────────────────────────
async def post_init(application: Application):
    """Set bot commands after initialization."""
    commands = [
        BotCommand("start", "Start the bot / Request access"),
        BotCommand("clear", "Clear your chat history"),
        BotCommand("status", "Bot status (Admin only)"),
        BotCommand("users", "List allowed users (Admin only)"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands set successfully.")

    # Notify admin
    try:
        await application.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "🟢 <b>Crypto Intel Bot is now ONLINE!</b>\n\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "📊 All monitoring systems active\n"
                "📰 News feed scanner: ✅\n"
                "📈 Market monitor: ✅\n"
                "🔥 Liquidation tracker: ✅\n"
                "💬 Personal chatbot: ✅\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"Failed to send startup notification: {e}")


# ─────────────────────────────────────────────────────────────
# SCHEDULER SETUP
# ─────────────────────────────────────────────────────────────
def setup_scheduler(application: Application):
    """Configure background job scheduling."""
    job_queue = application.job_queue

    # News monitoring — every 10 minutes
    job_queue.run_repeating(
        fetch_and_analyze_news,
        interval=600,  # 10 minutes
        first=30,  # Start 30 seconds after boot
        name="news_monitor",
    )

    # Market price monitoring — every 5 minutes
    job_queue.run_repeating(
        monitor_market,
        interval=300,  # 5 minutes
        first=15,
        name="market_monitor",
    )

    # Liquidation/funding rate monitoring — every 15 minutes
    job_queue.run_repeating(
        monitor_liquidations,
        interval=900,  # 15 minutes
        first=45,
        name="liquidation_monitor",
    )

    logger.info("Background schedulers configured successfully.")


# ─────────────────────────────────────────────────────────────
# MAIN — APPLICATION SETUP & LAUNCH
# ─────────────────────────────────────────────────────────────
def main():
    """Initialize and run the bot."""
    logger.info("═" * 60)
    logger.info("  CRYPTO INTELLIGENCE TELEGRAM BOT v2.0")
    logger.info("  Starting up...")
    logger.info("═" * 60)

    # Build application
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .build()
    )

    # ── Register Handlers ──────────────────────────────────
    # Commands
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("clear", cmd_clear))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("users", cmd_users))

    # Callback queries (inline buttons)
    application.add_handler(CallbackQueryHandler(callback_handler))

    # Chat member updates (bot added to channel/group)
    application.add_handler(
        ChatMemberHandler(bot_added_to_channel, ChatMemberHandler.MY_CHAT_MEMBER)
    )

    # Private messages (personal chatbot) — must be last
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
            handle_message,
        )
    )

    # Error handler
    application.add_error_handler(error_handler)

    # ── Setup Background Jobs ──────────────────────────────
    setup_scheduler(application)

    # ── Start Polling ──────────────────────────────────────
    logger.info("Bot is now polling for updates...")
    application.run_polling(
        allowed_updates=[
            Update.MESSAGE,
            Update.CALLBACK_QUERY,
            Update.MY_CHAT_MEMBER,
            Update.CHAT_MEMBER,
        ],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
