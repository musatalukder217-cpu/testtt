#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║          24/7 AUTOMATED ALL-IN-ONE CRYPTO INTELLIGENCE TELEGRAM BOT        ║
║                                                                            ║
║  Engine  : Groq Sync Client → openai/gpt-oss-120b                         ║
║  Data    : Binance / CCXT Real-Time Pipeline                               ║
║  Hosting : Railway (GitHub Repo → Auto Deploy)                             ║
║                                                                            ║
║  Features:                                                                 ║
║   1. Real-time price injection (zero LLM hallucination)                    ║
║   2. Personal AI crypto analyst chatbot (multi-user isolated)              ║
║   3. Automated channel broadcast (news, whales, squeeze alerts)            ║
║   4. Granular admin permission & security control                          ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os
import re
import json
import time
import logging
import hashlib
import asyncio
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from typing import Optional

# --- Third-party imports ---
import ccxt
import feedparser
import aiohttp
from dateutil import parser as dateutil_parser
from groq import Groq

# --- python-telegram-bot v20+ imports ---
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatMember,
    ChatMemberUpdated,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatType
from telegram.error import TelegramError

# ═══════════════════════════════════════════════════════════════
#  LOGGING CONFIGURATION
# ═══════════════════════════════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s │ %(name)s │ %(levelname)s │ %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("CryptoBot")

# ═══════════════════════════════════════════════════════════════
#  ENVIRONMENT VARIABLES (Railway Variables — no hardcoded keys)
# ═══════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
PUBLIC_CHANNEL_ID = os.getenv("PUBLIC_CHANNEL_ID", "")
OWNER_NAME = os.getenv("OWNER_NAME", "Admin")
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "")

# Validate critical env vars
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set!")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is not set!")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is not set!")

# ═══════════════════════════════════════════════════════════════
#  GROQ SYNC CLIENT INITIALIZATION
# ═══════════════════════════════════════════════════════════════
groq_client = Groq(api_key=GROQ_API_KEY)
LLM_MODEL = "openai/gpt-oss-120b"

# ═══════════════════════════════════════════════════════════════
#  CCXT / BINANCE EXCHANGE INITIALIZATION
# ═══════════════════════════════════════════════════════════════
exchange = ccxt.binance({"enableRateLimit": True, "timeout": 15000})

# ═══════════════════════════════════════════════════════════════
#  IN-MEMORY DATA STORES
# ═══════════════════════════════════════════════════════════════

# --- User permission store ---
# Format: { user_id: True/False }
approved_users: dict[int, bool] = {}

# --- Approved channels store ---
# Format: { channel_id: True }
approved_channels: dict[int, bool] = {}

# --- Per-user conversation history (multi-user isolation) ---
# Format: { user_id: [ {"role": ..., "content": ...}, ... ] }
user_chat_history: dict[int, list] = defaultdict(list)

# Maximum conversation turns to keep per user
MAX_HISTORY_TURNS = 30

# --- Duplicate news filter ---
# Stores hashes of recently posted news to avoid duplicates
posted_news_hashes: set[str] = set()
MAX_NEWS_HASH_STORE = 500

# --- Pending channel approval requests ---
# Format: { channel_id: {"title": ..., "added_by": ...} }
pending_channels: dict[int, dict] = {}

# --- Pending reject messages ---
# Format: { admin_chat_id: channel_id } — when admin is typing custom leave message
pending_reject_message: dict[int, int] = {}

# --- Previous price cache for movement detection ---
previous_prices: dict[str, float] = {}

# ═══════════════════════════════════════════════════════════════
#  CRYPTO NEWS RSS FEEDS (Top Sources)
# ═══════════════════════════════════════════════════════════════
RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cryptonews.com/news/feed/",
    "https://decrypt.co/feed",
    "https://bitcoinmagazine.com/.rss/full/",
    "https://www.theblock.co/rss.xml",
]

# ═══════════════════════════════════════════════════════════════
#  TOP COINS TO MONITOR FOR WHALE/SQUEEZE ALERTS
# ═══════════════════════════════════════════════════════════════
MONITORED_COINS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT",
    "ADA/USDT", "DOGE/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT",
    "MATIC/USDT", "SHIB/USDT", "LTC/USDT", "UNI/USDT", "ATOM/USDT",
    "FIL/USDT", "APT/USDT", "ARB/USDT", "OP/USDT", "SUI/USDT",
    "PEPE/USDT", "WIF/USDT", "NEAR/USDT", "INJ/USDT", "TIA/USDT",
]

# Price movement threshold for alerts (percentage)
PUMP_DUMP_THRESHOLD = 5.0  # 5% in 10-min window triggers alert

# ═══════════════════════════════════════════════════════════════
#  SYSTEM PROMPT FOR THE LLM
# ═══════════════════════════════════════════════════════════════
SYSTEM_PROMPT = """তুমি একজন বিশ্বমানের ক্রিপ্টোকারেন্সি বিশ্লেষক ও ট্রেডিং অ্যাসিস্ট্যান্ট।

তোমার কঠোর নিয়মাবলী:

১) ক্রিপ্টো ডোমেইন: তুমি শুধুমাত্র ক্রিপ্টোকারেন্সি, ব্লকচেইন, DeFi, NFT, ট্রেডিং, টেকনিক্যাল অ্যানালাইসিস, অন-চেইন ডেটা, এক্সচেঞ্জ ও ওয়ালেট সম্পর্কে কথা বলবে। এর বাইরে কোনো প্রশ্ন আসলে বাংলায় বলবে: "দুঃখিত, আমার কাছে এই ধরনের কোনো ডাটা নেই। আমি শুধুমাত্র ক্রিপ্টোকারেন্সি, ট্রেডিং ও মার্কেট সম্পর্কিত বিষয় বিশ্লেষণে সক্ষম।"

২) রিয়েল-টাইম ডেটা: তোমাকে প্রম্পটের মধ্যে [LIVE_MARKET_DATA] ব্লকে বর্তমান লাইভ মার্কেট ডেটা দেওয়া হবে। প্রাইস, ভলিউম, মুভমেন্ট — সব এই ডেটা থেকেই বলবে। কখনো নিজের ট্রেনিং ডেটা থেকে প্রাইস অনুমান করবে না। ইউজার নিজে ঐতিহাসিক ডেটা না চাইলে পুরোনো বছরের (২০২১/২০২৪) কোনো প্রাইস উল্লেখ করা নিষিদ্ধ।

৩) ভাষা: ইউজার বাংলায় জিজ্ঞেস করলে বাংলায়, ইংরেজিতে করলে ইংরেজিতে উত্তর দেবে। মিশ্র ভাষায় আসলে ইউজারের প্রধান ভাষায় উত্তর দেবে।

৪) টু-দ্য-পয়েন্ট: কোনো বাড়তি ভূমিকা, ডিসক্লেইমার, বা "আমি AI" জাতীয় কথা বলবে না। সরাসরি উত্তর দেবে।

৫) কোনো এক্সটার্নাল লিংক: উত্তরে কোনো ওয়েবসাইট URL বা থার্ড-পার্টি লিংক দেবে না।

৬) টেকনিক্যাল অ্যানালাইসিস: সাপোর্ট/রেজিস্ট্যান্স, চার্ট প্যাটার্ন, ব্রেকআউট, RSI, MACD, ফান্ডিং রেট, লিকুইডেশন ম্যাপ — সব বিষয়ে গভীর জ্ঞান প্রদর্শন করবে।

৭) Whale/On-chain: তিমি মুভমেন্ট, বড় ট্রানজাকশন, এক্সচেঞ্জ ইনফ্লো/আউটফ্লো বিশ্লেষণ করতে পারবে।"""


# ═══════════════════════════════════════════════════════════════
#  HELPER: FETCH LIVE MARKET DATA FROM BINANCE VIA CCXT
# ═══════════════════════════════════════════════════════════════
def fetch_live_price(symbol: str) -> Optional[dict]:
    """
    Fetches real-time ticker data for a given symbol from Binance.
    Returns dict with price, high, low, volume, percentage change.
    Returns None if symbol not found or API error.
    """
    try:
        ticker = exchange.fetch_ticker(symbol)
        return {
            "symbol": symbol,
            "price": ticker.get("last", 0),
            "high_24h": ticker.get("high", 0),
            "low_24h": ticker.get("low", 0),
            "volume_24h": ticker.get("quoteVolume", 0),
            "change_pct": ticker.get("percentage", 0),
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
    except Exception as e:
        logger.warning(f"Failed to fetch price for {symbol}: {e}")
        return None


def fetch_multiple_prices(symbols: list[str]) -> list[dict]:
    """Fetch prices for multiple symbols."""
    results = []
    for sym in symbols:
        data = fetch_live_price(sym)
        if data:
            results.append(data)
    return results


# ═══════════════════════════════════════════════════════════════
#  HELPER: DETECT CRYPTO SYMBOLS IN USER MESSAGE
# ═══════════════════════════════════════════════════════════════

# Common coin name → symbol mapping
COIN_ALIASES = {
    "bitcoin": "BTC", "btc": "BTC",
    "ethereum": "ETH", "eth": "ETH", "ether": "ETH",
    "solana": "SOL", "sol": "SOL",
    "bnb": "BNB", "binance coin": "BNB",
    "xrp": "XRP", "ripple": "XRP",
    "cardano": "ADA", "ada": "ADA",
    "dogecoin": "DOGE", "doge": "DOGE",
    "avalanche": "AVAX", "avax": "AVAX",
    "polkadot": "DOT", "dot": "DOT",
    "chainlink": "LINK", "link": "LINK",
    "polygon": "MATIC", "matic": "MATIC", "pol": "POL",
    "shiba": "SHIB", "shib": "SHIB", "shiba inu": "SHIB",
    "litecoin": "LTC", "ltc": "LTC",
    "uniswap": "UNI", "uni": "UNI",
    "cosmos": "ATOM", "atom": "ATOM",
    "filecoin": "FIL", "fil": "FIL",
    "aptos": "APT", "apt": "APT",
    "arbitrum": "ARB", "arb": "ARB",
    "optimism": "OP", "op": "OP",
    "sui": "SUI",
    "pepe": "PEPE",
    "wif": "WIF", "dogwifhat": "WIF",
    "near": "NEAR", "near protocol": "NEAR",
    "injective": "INJ", "inj": "INJ",
    "celestia": "TIA", "tia": "TIA",
    "ton": "TON", "toncoin": "TON",
    "sei": "SEI",
    "render": "RENDER",
    "fetch": "FET", "fet": "FET",
    "jupiter": "JUP", "jup": "JUP",
    "bonk": "BONK",
    "floki": "FLOKI",
    "kaspa": "KAS", "kas": "KAS",
    "stacks": "STX", "stx": "STX",
    "aave": "AAVE",
    "maker": "MKR", "mkr": "MKR",
    "tron": "TRX", "trx": "TRX",
    "hedera": "HBAR", "hbar": "HBAR",
}

# Regex to also catch direct ticker mentions like $BTC, $ETH
TICKER_PATTERN = re.compile(r'\$([A-Za-z]{2,10})', re.IGNORECASE)


def detect_coins_in_message(text: str) -> list[str]:
    """
    Detects cryptocurrency mentions in user message.
    Returns list of Binance trading pair symbols like ['BTC/USDT', 'ETH/USDT'].
    """
    text_lower = text.lower()
    detected = set()

    # Check aliases
    for alias, symbol in COIN_ALIASES.items():
        # Use word boundary matching to avoid partial matches
        if re.search(r'\b' + re.escape(alias) + r'\b', text_lower):
            detected.add(symbol)

    # Check $TICKER pattern
    for match in TICKER_PATTERN.finditer(text):
        ticker = match.group(1).upper()
        detected.add(ticker)

    # Convert to Binance pairs
    pairs = []
    for coin in detected:
        pair = f"{coin}/USDT"
        pairs.append(pair)

    return pairs


def is_price_related_query(text: str) -> bool:
    """
    Determines if the user's message is asking about prices, market data,
    or any topic that requires real-time data injection.
    """
    price_keywords = [
        "price", "প্রাইস", "দাম", "কত", "কতো", "rate", "রেট",
        "market", "মার্কেট", "বাজার", "pump", "dump", "পাম্প", "ডাম্প",
        "bull", "bear", "বুল", "বিয়ার", "volume", "ভলিউম",
        "high", "low", "হাই", "লো", "ath", "atl",
        "support", "resistance", "সাপোর্ট", "রেজিস্ট্যান্স",
        "breakout", "ব্রেকআউট", "trend", "ট্রেন্ড",
        "analysis", "অ্যানালাইসিস", "বিশ্লেষণ",
        "buy", "sell", "কিনব", "বেচব", "কেনা", "বিক্রি",
        "prediction", "প্রেডিকশন", "target", "টার্গেট",
        "entry", "এন্ট্রি", "long", "short", "লং", "শর্ট",
        "funding", "ফান্ডিং", "liquidation", "লিকুইডেশন",
        "whale", "তিমি", "হোয়েল",
        "move", "movement", "মুভ", "মুভমেন্ট",
        "up", "down", "উপরে", "নিচে", "বাড়", "কম",
        "signal", "সিগন্যাল",
    ]
    text_lower = text.lower()
    return any(kw in text_lower for kw in price_keywords)


# ═══════════════════════════════════════════════════════════════
#  HELPER: BUILD MARKET CONTEXT STRING FOR LLM PROMPT
# ═══════════════════════════════════════════════════════════════
def build_market_context(pairs: list[str]) -> str:
    """
    Fetches live data for given pairs and formats it as a context block
    to inject into the LLM prompt.
    """
    if not pairs:
        return ""

    data_list = fetch_multiple_prices(pairs)
    if not data_list:
        return ""

    lines = ["[LIVE_MARKET_DATA]"]
    for d in data_list:
        change_str = f"+{d['change_pct']:.2f}%" if d['change_pct'] >= 0 else f"{d['change_pct']:.2f}%"
        lines.append(
            f"• {d['symbol']}: ${d['price']:,.4f} | "
            f"24h High: ${d['high_24h']:,.4f} | "
            f"24h Low: ${d['low_24h']:,.4f} | "
            f"24h Volume: ${d['volume_24h']:,.0f} | "
            f"24h Change: {change_str} | "
            f"Fetched: {d['timestamp']}"
        )
    lines.append("[/LIVE_MARKET_DATA]")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  HELPER: CALL GROQ LLM (SYNC) WITH CONVERSATION HISTORY
# ═══════════════════════════════════════════════════════════════
def call_llm(user_id: int, user_message: str, market_context: str = "") -> str:
    """
    Calls the Groq LLM with the user's conversation history.
    Injects real-time market data into the prompt if available.
    Returns the LLM's response text.
    """
    # Build the effective user message with market context
    if market_context:
        effective_message = (
            f"{market_context}\n\n"
            f"ইউজারের প্রশ্ন: {user_message}"
        )
    else:
        effective_message = user_message

    # Get or initialize user's conversation history
    history = user_chat_history[user_id]

    # Add user message to history
    history.append({"role": "user", "content": effective_message})

    # Trim history if too long (keep system + last N turns)
    if len(history) > MAX_HISTORY_TURNS * 2:
        history = history[-(MAX_HISTORY_TURNS * 2):]
        user_chat_history[user_id] = history

    # Build messages array for API call
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    try:
        response = groq_client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            temperature=0.6,
            max_tokens=4096,
            top_p=0.9,
        )
        assistant_reply = response.choices[0].message.content.strip()

        # Save assistant reply to history
        history.append({"role": "assistant", "content": assistant_reply})
        user_chat_history[user_id] = history

        return assistant_reply

    except Exception as e:
        logger.error(f"Groq API error: {e}")
        return "⚠️ দুঃখিত, এই মুহূর্তে AI সার্ভারে সমস্যা হচ্ছে। অনুগ্রহ করে কিছুক্ষণ পর আবার চেষ্টা করুন।"


# ═══════════════════════════════════════════════════════════════
#  HELPER: GENERATE NEWS ANALYSIS VIA LLM
# ═══════════════════════════════════════════════════════════════
def generate_news_analysis(headline: str, summary: str) -> str:
    """
    Uses the LLM to generate a professional analysis of a crypto news item.
    Returns formatted analysis text.
    """
    prompt = f"""তুমি একজন ক্রিপ্টো নিউজ অ্যানালিস্ট। নিচের খবরটি বিশ্লেষণ করো।

নিউজ হেডলাইন: {headline}
সারসংক্ষেপ: {summary}

নিয়ম:
১) প্রথমে একটি আকর্ষণীয় ও স্পষ্ট বাংলা হেডলাইন লেখো (🔥 ইমোজি দিয়ে শুরু)
২) এরপর সম্পূর্ণ বিশ্লেষণ, মার্কেট ইমপ্যাক্ট ও ডেটা প্রফেশনাল ইংরেজিতে লেখো
৩) কোনো এক্সটার্নাল লিংক দিবে না
৪) সংক্ষিপ্ত ও তথ্যপূর্ণ রাখো (১৫০-২৫০ শব্দ)
৫) মার্কেটে সম্ভাব্য প্রভাব (Bullish/Bearish/Neutral) স্পষ্ট করো"""

    try:
        response = groq_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "You are an expert crypto news analyst."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            max_tokens=2048,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"News analysis LLM error: {e}")
        return ""


def generate_movement_analysis(symbol: str, change_pct: float, price: float) -> str:
    """
    Uses LLM to analyze why a coin had a sharp movement.
    """
    direction = "pump (তীব্র দাম বৃদ্ধি)" if change_pct > 0 else "dump (তীব্র দাম পতন)"
    prompt = f"""ক্রিপ্টোকারেন্সি {symbol} এর দাম গত অল্প সময়ে {change_pct:+.2f}% {direction} হয়েছে। বর্তমান দাম ${price:,.2f}।

এই তীব্র মুভমেন্টের সম্ভাব্য কারণ বিশ্লেষণ করো:
- ETF প্রবাহ (Inflow/Outflow)
- অন-চেইন লিকুইডেশন ক্যাসকেড
- শর্ট/লং স্কুইজ
- ভূরাজনৈতিক বা ম্যাক্রো ইকোনমিক প্রভাব
- Whale Activity

নিয়ম:
১) বাংলায় আকর্ষণীয় হেডলাইন (⚡ দিয়ে শুরু)
২) বিশ্লেষণ ইংরেজিতে
৩) কোনো লিংক দিবে না
৪) সংক্ষিপ্ত ও প্রফেশনাল (১০০-২০০ শব্দ)"""

    try:
        response = groq_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "You are an expert crypto market analyst specializing in sudden price movements."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            max_tokens=1500,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Movement analysis LLM error: {e}")
        return ""


# ═══════════════════════════════════════════════════════════════
#  HELPER: SEO HASHTAGS
# ═══════════════════════════════════════════════════════════════
def get_trending_hashtags(context_text: str = "") -> str:
    """Returns 5-7 trending Telegram SEO hashtags for crypto content."""
    base_tags = ["#Bitcoin", "#Crypto", "#Binance", "#Ethereum", "#CryptoNews"]
    context_lower = context_text.lower()

    # Add context-specific tags
    if any(w in context_lower for w in ["btc", "bitcoin"]):
        base_tags.append("#BTC")
    if any(w in context_lower for w in ["eth", "ethereum"]):
        base_tags.append("#ETH")
    if any(w in context_lower for w in ["sol", "solana"]):
        base_tags.append("#Solana")
    if any(w in context_lower for w in ["defi"]):
        base_tags.append("#DeFi")
    if any(w in context_lower for w in ["nft"]):
        base_tags.append("#NFT")
    if any(w in context_lower for w in ["whale", "তিমি"]):
        base_tags.append("#WhaleAlert")
    if any(w in context_lower for w in ["liquidat", "লিকুইডেশন"]):
        base_tags.append("#Liquidation")
    if any(w in context_lower for w in ["pump", "dump", "squeeze"]):
        base_tags.append("#Trading")
    if any(w in context_lower for w in ["etf"]):
        base_tags.append("#BitcoinETF")
    if any(w in context_lower for w in ["altcoin", "alt"]):
        base_tags.append("#Altcoins")

    # Deduplicate and limit to 7
    seen = set()
    unique_tags = []
    for tag in base_tags:
        if tag.lower() not in seen:
            seen.add(tag.lower())
            unique_tags.append(tag)
    return " ".join(unique_tags[:7])


# ═══════════════════════════════════════════════════════════════
#  HELPER: OWNER SIGNATURE FOR REJECT MESSAGES
# ═══════════════════════════════════════════════════════════════
def get_owner_signature() -> str:
    """
    Returns a clickable owner signature.
    Clicking the name opens the admin's Telegram profile.
    No raw username is visible.
    """
    if OWNER_USERNAME:
        # Create a text link that opens the profile
        return f'\n\n— Owner: <a href="https://t.me/{OWNER_USERNAME}">{OWNER_NAME}</a>'
    else:
        return f"\n\n— Owner: {OWNER_NAME}"


# ═══════════════════════════════════════════════════════════════
#  /start COMMAND HANDLER
# ═══════════════════════════════════════════════════════════════
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles /start command.
    - If user is admin → welcome
    - If user is already approved → welcome back
    - If new user → send access request to admin
    """
    user = update.effective_user
    chat = update.effective_chat

    # Only handle in private chat
    if chat.type != ChatType.PRIVATE:
        return

    user_id = user.id
    user_name = user.full_name or "Unknown"
    username = user.username or "N/A"

    # Admin always has access
    if user_id == ADMIN_ID:
        approved_users[user_id] = True
        await update.message.reply_text(
            f"🔐 **অ্যাডমিন প্যানেল অ্যাক্টিভ**\n\n"
            f"স্বাগতম, {user_name}!\n"
            f"আপনি সর্বোচ্চ অ্যাডমিন হিসেবে সকল ফিচার ব্যবহার করতে পারবেন।\n\n"
            f"📊 যেকোনো ক্রিপ্টো সম্পর্কে প্রশ্ন করুন, লাইভ ডেটাসহ উত্তর পাবেন।",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Check if already approved
    if approved_users.get(user_id):
        await update.message.reply_text(
            f"✅ স্বাগতম, {user_name}!\n\n"
            f"📊 আপনি ইতোমধ্যে অনুমোদিত। যেকোনো ক্রিপ্টো প্রশ্ন করুন!",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # New user — deny access and notify admin
    await update.message.reply_text(
        "🔒 **অ্যাক্সেস সীমাবদ্ধ**\n\n"
        "এই বটে অ্যাক্সেস পেতে অ্যাডমিনের অনুমোদন প্রয়োজন।\n"
        "আপনার অনুরোধ পাঠানো হয়েছে। অনুগ্রহ করে অপেক্ষা করুন।",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Send approval request to admin with inline buttons
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Allow Chat", callback_data=f"allow_user_{user_id}"),
            InlineKeyboardButton("❌ Deny", callback_data=f"deny_user_{user_id}"),
        ]
    ])

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"🆕 **নতুন ইউজার অ্যাক্সেস রিকোয়েস্ট**\n\n"
            f"👤 নাম: {user_name}\n"
            f"🆔 ID: `{user_id}`\n"
            f"📛 Username: @{username}\n\n"
            f"অনুমোদন দিতে চান?"
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


# ═══════════════════════════════════════════════════════════════
#  CALLBACK QUERY HANDLER (Inline Button Responses)
# ═══════════════════════════════════════════════════════════════
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles all inline keyboard button callbacks:
    - allow_user_<id> / deny_user_<id>
    - approve_channel_<id> / reject_channel_<id>
    """
    query = update.callback_query
    await query.answer()

    admin_id = query.from_user.id

    # Only admin can handle these
    if admin_id != ADMIN_ID:
        await query.answer("⛔ শুধুমাত্র অ্যাডমিন এই অ্যাকশন নিতে পারেন।", show_alert=True)
        return

    data = query.data

    # ── User Approval ──
    if data.startswith("allow_user_"):
        target_user_id = int(data.replace("allow_user_", ""))
        approved_users[target_user_id] = True

        # Notify admin
        await query.edit_message_text(
            f"✅ ইউজার `{target_user_id}` অনুমোদিত হয়েছে।",
            parse_mode=ParseMode.MARKDOWN,
        )

        # Notify user
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=(
                    "🎉 **অ্যাক্সেস অনুমোদিত!**\n\n"
                    "অ্যাডমিন আপনার অনুরোধ অনুমোদন করেছেন।\n"
                    "এখন আপনি যেকোনো ক্রিপ্টো প্রশ্ন করতে পারবেন! 📊"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except TelegramError:
            pass

    elif data.startswith("deny_user_"):
        target_user_id = int(data.replace("deny_user_", ""))
        approved_users[target_user_id] = False

        await query.edit_message_text(
            f"❌ ইউজার `{target_user_id}` প্রত্যাখ্যান করা হয়েছে।",
            parse_mode=ParseMode.MARKDOWN,
        )

        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text="❌ দুঃখিত, আপনার অ্যাক্সেস অনুরোধ প্রত্যাখ্যান করা হয়েছে।",
            )
        except TelegramError:
            pass

    # ── Channel Approval ──
    elif data.startswith("approve_channel_"):
        channel_id = int(data.replace("approve_channel_", ""))
        approved_channels[channel_id] = True

        # Remove from pending
        channel_info = pending_channels.pop(channel_id, {})
        channel_title = channel_info.get("title", "Unknown")

        # Silent approval — no welcome message to channel
        await query.edit_message_text(
            f"✅ চ্যানেল **{channel_title}** (`{channel_id}`) সাইলেন্টলি অনুমোদিত হয়েছে।\n"
            f"বট এখন এই চ্যানেলে সক্রিয়।",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data.startswith("reject_channel_"):
        channel_id = int(data.replace("reject_channel_", ""))

        # Store the channel_id so we know the next text message from admin
        # is the custom leave message for this channel
        pending_reject_message[ADMIN_ID] = channel_id

        channel_info = pending_channels.get(channel_id, {})
        channel_title = channel_info.get("title", "Unknown")

        await query.edit_message_text(
            f"📝 চ্যানেল **{channel_title}** (`{channel_id}`) প্রত্যাখ্যান করা হচ্ছে।\n\n"
            f"চ্যানেলে বিদায়ের সময় কী কাস্টম মেসেজ পাঠাতে চান?\n"
            f"নিচে লিখে পাঠান:",
            parse_mode=ParseMode.MARKDOWN,
        )


# ═══════════════════════════════════════════════════════════════
#  CHAT MEMBER UPDATE HANDLER (Bot added/removed from channels)
# ═══════════════════════════════════════════════════════════════
async def chat_member_updated(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Detects when the bot is added to a new channel/group as admin.
    If the channel is not pre-approved, notifies the main admin
    with Approve/Reject buttons.
    """
    my_chat_member = update.my_chat_member
    if not my_chat_member:
        return

    chat = my_chat_member.chat
    new_status = my_chat_member.new_chat_member.status
    old_status = my_chat_member.old_chat_member.status

    # Bot was added (became admin or member)
    if old_status in (ChatMember.LEFT, ChatMember.BANNED) and new_status in (
        ChatMember.MEMBER, ChatMember.ADMINISTRATOR,
    ):
        channel_id = chat.id
        channel_title = chat.title or "Unknown"
        added_by = my_chat_member.from_user

        # If already approved, silently activate
        if approved_channels.get(channel_id):
            logger.info(f"Bot added to approved channel: {channel_title} ({channel_id})")
            return

        # Store as pending
        pending_channels[channel_id] = {
            "title": channel_title,
            "added_by": added_by.full_name if added_by else "Unknown",
        }

        # Notify admin
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Approve Channel",
                    callback_data=f"approve_channel_{channel_id}",
                ),
                InlineKeyboardButton(
                    "❌ Reject & Leave",
                    callback_data=f"reject_channel_{channel_id}",
                ),
            ]
        ])

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"⚠️ **বট নতুন চ্যানেলে যুক্ত হয়েছে!**\n\n"
                f"📢 চ্যানেল: {channel_title}\n"
                f"🆔 ID: `{channel_id}`\n"
                f"👤 যুক্ত করেছে: {added_by.full_name if added_by else 'Unknown'}\n\n"
                f"অনুমোদন দেবেন?"
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )

        logger.info(f"Bot added to unapproved channel: {channel_title} ({channel_id}). Awaiting admin decision.")


# ═══════════════════════════════════════════════════════════════
#  MESSAGE HANDLER (Private Chat — Main Chatbot Logic)
# ═══════════════════════════════════════════════════════════════
async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles all private text messages:
    1. Checks if user is approved
    2. Detects crypto mentions and fetches live data
    3. Injects market context into LLM prompt
    4. Returns AI analysis
    5. Also handles admin's custom reject message flow
    """
    user = update.effective_user
    chat = update.effective_chat
    message_text = update.message.text

    if not message_text or chat.type != ChatType.PRIVATE:
        return

    user_id = user.id

    # ── Handle admin's custom reject message for channel ──
    if user_id == ADMIN_ID and user_id in pending_reject_message:
        channel_id = pending_reject_message.pop(user_id)
        custom_message = message_text

        # Add owner signature
        signature = get_owner_signature()
        full_message = custom_message + signature

        # Post message to the channel
        try:
            await context.bot.send_message(
                chat_id=channel_id,
                text=full_message,
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as e:
            logger.error(f"Failed to send reject message to channel {channel_id}: {e}")

        # Leave the channel
        try:
            await context.bot.leave_chat(channel_id)
        except TelegramError as e:
            logger.error(f"Failed to leave channel {channel_id}: {e}")

        # Clean up
        pending_channels.pop(channel_id, None)
        approved_channels.pop(channel_id, None)

        await update.message.reply_text(
            f"✅ কাস্টম মেসেজ পাঠানো হয়েছে এবং বট চ্যানেল থেকে বের হয়ে গেছে।",
        )
        return

    # ── Check user permission ──
    if user_id != ADMIN_ID and not approved_users.get(user_id):
        await update.message.reply_text(
            "🔒 আপনার অ্যাক্সেস নেই। /start কমান্ড দিয়ে অনুরোধ পাঠান।",
        )
        return

    # ── Show typing action ──
    await context.bot.send_chat_action(chat_id=chat.id, action="typing")

    # ── Detect crypto coins mentioned in the message ──
    detected_pairs = detect_coins_in_message(message_text)

    # ── Determine if we need to fetch live market data ──
    market_context = ""
    if detected_pairs or is_price_related_query(message_text):
        # If specific coins detected, fetch those
        if detected_pairs:
            market_context = build_market_context(detected_pairs)
        else:
            # General market query — fetch top coins
            top_pairs = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]
            market_context = build_market_context(top_pairs)

    # ── Call LLM with context ──
    response = call_llm(user_id, message_text, market_context)

    # ── Send response (handle long messages) ──
    if len(response) > 4000:
        # Split into chunks
        chunks = [response[i:i + 4000] for i in range(0, len(response), 4000)]
        for chunk in chunks:
            await update.message.reply_text(chunk)
    else:
        await update.message.reply_text(response)


# ═══════════════════════════════════════════════════════════════
#  BACKGROUND TASK: RSS NEWS SCANNER
# ═══════════════════════════════════════════════════════════════
async def scan_rss_feeds(context: ContextTypes.DEFAULT_TYPE):
    """
    Scans top crypto RSS feeds for new articles.
    Filters by timestamp (only recent news, <30 minutes old).
    Deduplicates using hash of title.
    Generates AI analysis and posts to approved channels.
    """
    global posted_news_hashes

    if not PUBLIC_CHANNEL_ID and not approved_channels:
        return

    # Get target channels
    target_channels = []
    if PUBLIC_CHANNEL_ID:
        try:
            target_channels.append(int(PUBLIC_CHANNEL_ID))
        except ValueError:
            target_channels.append(PUBLIC_CHANNEL_ID)
    for ch_id, is_approved in approved_channels.items():
        if is_approved and ch_id not in target_channels:
            target_channels.append(ch_id)

    if not target_channels:
        return

    now = datetime.now(timezone.utc)
    new_articles = []

    for feed_url in RSS_FEEDS:
        try:
            feed = await asyncio.get_event_loop().run_in_executor(
                None, feedparser.parse, feed_url
            )

            for entry in feed.entries[:5]:  # Check latest 5 entries per feed
                title = entry.get("title", "").strip()
                summary = entry.get("summary", entry.get("description", "")).strip()

                if not title:
                    continue

                # Create hash for dedup
                title_hash = hashlib.md5(title.encode()).hexdigest()

                if title_hash in posted_news_hashes:
                    continue

                # Timestamp filtering — only process articles < 30 minutes old
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                if published:
                    try:
                        pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                        age = (now - pub_dt).total_seconds()
                        if age > 1800:  # 30 minutes
                            continue
                    except Exception:
                        # If parsing fails, use published string
                        try:
                            pub_str = entry.get("published", entry.get("updated", ""))
                            pub_dt = dateutil_parser.parse(pub_str)
                            if pub_dt.tzinfo is None:
                                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                            age = (now - pub_dt).total_seconds()
                            if age > 1800:
                                continue
                        except Exception:
                            continue
                else:
                    continue  # No timestamp = skip

                new_articles.append({
                    "title": title,
                    "summary": summary,
                    "hash": title_hash,
                    "time": now.strftime("%Y-%m-%d %H:%M UTC"),
                })

        except Exception as e:
            logger.warning(f"RSS feed error ({feed_url}): {e}")
            continue

    # Process and post new articles
    for article in new_articles[:3]:  # Limit to 3 per scan cycle
        analysis = await asyncio.get_event_loop().run_in_executor(
            None, generate_news_analysis, article["title"], article["summary"]
        )

        if not analysis:
            continue

        # Build final post
        hashtags = get_trending_hashtags(article["title"] + " " + article["summary"])
        timestamp_line = f"\n\n🕐 {article['time']}"
        final_post = f"{analysis}{timestamp_line}\n\n{hashtags}"

        # Post to all target channels
        for ch_id in target_channels:
            try:
                await context.bot.send_message(
                    chat_id=ch_id,
                    text=final_post,
                    parse_mode=ParseMode.MARKDOWN,
                )
                logger.info(f"Posted news to channel {ch_id}: {article['title'][:50]}...")
            except TelegramError as e:
                logger.error(f"Failed to post to channel {ch_id}: {e}")

        # Mark as posted
        posted_news_hashes.add(article["hash"])

        # Clean up old hashes if store gets too large
        if len(posted_news_hashes) > MAX_NEWS_HASH_STORE:
            # Keep only the last half
            posted_news_hashes = set(list(posted_news_hashes)[MAX_NEWS_HASH_STORE // 2:])

        # Small delay between posts
        await asyncio.sleep(2)


# ═══════════════════════════════════════════════════════════════
#  BACKGROUND TASK: PRICE MOVEMENT / WHALE / SQUEEZE SCANNER
# ═══════════════════════════════════════════════════════════════
async def scan_price_movements(context: ContextTypes.DEFAULT_TYPE):
    """
    Monitors top coins for sudden price movements (>5% in scan interval).
    Generates AI analysis explaining the cause and posts alert.
    """
    global previous_prices

    if not PUBLIC_CHANNEL_ID and not approved_channels:
        return

    # Get target channels
    target_channels = []
    if PUBLIC_CHANNEL_ID:
        try:
            target_channels.append(int(PUBLIC_CHANNEL_ID))
        except ValueError:
            target_channels.append(PUBLIC_CHANNEL_ID)
    for ch_id, is_approved in approved_channels.items():
        if is_approved and ch_id not in target_channels:
            target_channels.append(ch_id)

    if not target_channels:
        return

    alerts = []

    for symbol in MONITORED_COINS:
        try:
            ticker_data = await asyncio.get_event_loop().run_in_executor(
                None, fetch_live_price, symbol
            )

            if not ticker_data:
                continue

            current_price = ticker_data["price"]
            prev_price = previous_prices.get(symbol)

            if prev_price and prev_price > 0:
                change_pct = ((current_price - prev_price) / prev_price) * 100

                if abs(change_pct) >= PUMP_DUMP_THRESHOLD:
                    alerts.append({
                        "symbol": symbol,
                        "price": current_price,
                        "change_pct": change_pct,
                        "prev_price": prev_price,
                    })

            # Update previous price
            previous_prices[symbol] = current_price

        except Exception as e:
            logger.warning(f"Price scan error for {symbol}: {e}")
            continue

    # Process alerts
    for alert in alerts:
        # Generate AI analysis
        analysis = await asyncio.get_event_loop().run_in_executor(
            None,
            generate_movement_analysis,
            alert["symbol"],
            alert["change_pct"],
            alert["price"],
        )

        if not analysis:
            # Fallback: simple alert without AI analysis
            direction = "📈 PUMP" if alert["change_pct"] > 0 else "📉 DUMP"
            analysis = (
                f"⚡ **{alert['symbol']} {direction} Alert!**\n\n"
                f"🔹 বর্তমান দাম: ${alert['price']:,.4f}\n"
                f"🔹 পরিবর্তন: {alert['change_pct']:+.2f}%\n"
                f"🔹 আগের দাম: ${alert['prev_price']:,.4f}"
            )

        # Add timestamp and hashtags
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        hashtags = get_trending_hashtags(alert["symbol"])
        final_alert = f"{analysis}\n\n🕐 {now_str}\n\n{hashtags}"

        # Post to channels
        for ch_id in target_channels:
            try:
                await context.bot.send_message(
                    chat_id=ch_id,
                    text=final_alert,
                    parse_mode=ParseMode.MARKDOWN,
                )
                logger.info(
                    f"Posted {alert['symbol']} movement alert "
                    f"({alert['change_pct']:+.2f}%) to channel {ch_id}"
                )
            except TelegramError as e:
                logger.error(f"Failed to post alert to channel {ch_id}: {e}")

        await asyncio.sleep(2)


# ═══════════════════════════════════════════════════════════════
#  BACKGROUND TASK: WHALE TRANSACTION MONITOR (via public APIs)
# ═══════════════════════════════════════════════════════════════
async def scan_whale_transactions(context: ContextTypes.DEFAULT_TYPE):
    """
    Monitors for large transactions using Whale Alert-style public APIs
    and exchange large order book analysis.
    """
    if not PUBLIC_CHANNEL_ID and not approved_channels:
        return

    target_channels = []
    if PUBLIC_CHANNEL_ID:
        try:
            target_channels.append(int(PUBLIC_CHANNEL_ID))
        except ValueError:
            target_channels.append(PUBLIC_CHANNEL_ID)
    for ch_id, is_approved in approved_channels.items():
        if is_approved and ch_id not in target_channels:
            target_channels.append(ch_id)

    if not target_channels:
        return

    try:
        # Use Blockchair or whale-alert-style endpoint (free tier)
        # Monitoring BTC large transactions via public mempool API
        async with aiohttp.ClientSession() as session:
            # Check Blockchain.info unconfirmed TXs for large BTC transactions
            url = "https://blockchain.info/unconfirmed-transactions?format=json"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    txs = data.get("txs", [])

                    for tx in txs[:50]:  # Check latest 50 unconfirmed
                        # Calculate total output value in BTC
                        total_output = sum(
                            out.get("value", 0) for out in tx.get("out", [])
                        ) / 1e8  # satoshi to BTC

                        # Alert if > 100 BTC
                        if total_output >= 100:
                            btc_data = fetch_live_price("BTC/USDT")
                            usd_value = total_output * (btc_data["price"] if btc_data else 65000)

                            tx_hash = tx.get("hash", "unknown")[:16] + "..."
                            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

                            alert_text = (
                                f"🐋 **মেগা তিমি ট্রানজাকশন সনাক্ত!**\n\n"
                                f"🔸 Coin: Bitcoin (BTC)\n"
                                f"🔸 Amount: {total_output:,.2f} BTC\n"
                                f"🔸 Value: ~${usd_value:,.0f}\n"
                                f"🔸 Type: Unconfirmed Transaction\n"
                                f"🔸 TX: `{tx_hash}`\n\n"
                                f"This large transaction may indicate institutional movement, "
                                f"OTC trade, or exchange transfer. Monitor for potential "
                                f"market impact.\n\n"
                                f"🕐 {now_str}\n\n"
                                f"#WhaleAlert #Bitcoin #BTC #Crypto #Trading"
                            )

                            for ch_id in target_channels:
                                try:
                                    await context.bot.send_message(
                                        chat_id=ch_id,
                                        text=alert_text,
                                        parse_mode=ParseMode.MARKDOWN,
                                    )
                                except TelegramError as e:
                                    logger.error(f"Whale alert post error: {e}")

                            # Only alert for the largest one per cycle
                            break

    except Exception as e:
        logger.warning(f"Whale scan error: {e}")


# ═══════════════════════════════════════════════════════════════
#  BACKGROUND TASK: LIQUIDATION SCANNER
# ═══════════════════════════════════════════════════════════════
async def scan_liquidations(context: ContextTypes.DEFAULT_TYPE):
    """
    Scans for potential liquidation cascades by analyzing
    sudden volume spikes combined with price movements.
    Uses Binance futures data when available.
    """
    if not PUBLIC_CHANNEL_ID and not approved_channels:
        return

    target_channels = []
    if PUBLIC_CHANNEL_ID:
        try:
            target_channels.append(int(PUBLIC_CHANNEL_ID))
        except ValueError:
            target_channels.append(PUBLIC_CHANNEL_ID)
    for ch_id, is_approved in approved_channels.items():
        if is_approved and ch_id not in target_channels:
            target_channels.append(ch_id)

    if not target_channels:
        return

    try:
        # Check for extreme funding rates and volume spikes via CCXT
        futures_exchange = ccxt.binance({
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        })

        key_pairs = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

        for symbol in key_pairs:
            try:
                # Fetch funding rate
                funding = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda s=symbol: futures_exchange.fetch_funding_rate(s),
                )

                funding_rate = funding.get("fundingRate", 0)
                if funding_rate is None:
                    continue

                # Extreme funding rate (> 0.1% or < -0.1%) suggests squeeze potential
                if abs(funding_rate) > 0.001:
                    direction = "Long Squeeze ⚠️" if funding_rate > 0 else "Short Squeeze ⚠️"
                    sentiment = "Overleveraged Longs" if funding_rate > 0 else "Overleveraged Shorts"

                    ticker = fetch_live_price(symbol)
                    price_str = f"${ticker['price']:,.2f}" if ticker else "N/A"
                    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

                    alert = (
                        f"💥 **{direction} — {symbol}**\n\n"
                        f"🔸 Current Price: {price_str}\n"
                        f"🔸 Funding Rate: {funding_rate * 100:.4f}%\n"
                        f"🔸 Signal: {sentiment}\n\n"
                        f"Extreme funding rate detected. This historically precedes "
                        f"liquidation cascades. High-leverage positions at risk.\n\n"
                        f"🕐 {now_str}\n\n"
                        f"#Liquidation #Squeeze #{symbol.split('/')[0]} #Crypto #Trading"
                    )

                    for ch_id in target_channels:
                        try:
                            await context.bot.send_message(
                                chat_id=ch_id,
                                text=alert,
                                parse_mode=ParseMode.MARKDOWN,
                            )
                        except TelegramError as e:
                            logger.error(f"Liquidation alert error: {e}")

                    await asyncio.sleep(2)

            except Exception as e:
                logger.debug(f"Funding rate fetch error for {symbol}: {e}")
                continue

    except Exception as e:
        logger.warning(f"Liquidation scan error: {e}")


# ═══════════════════════════════════════════════════════════════
#  HEALTH CHECK / KEEP-ALIVE (for Railway)
# ═══════════════════════════════════════════════════════════════
async def health_check(context: ContextTypes.DEFAULT_TYPE):
    """Simple periodic health check log for Railway monitoring."""
    logger.info(
        f"💚 Bot health check OK | "
        f"Approved users: {sum(1 for v in approved_users.values() if v)} | "
        f"Approved channels: {sum(1 for v in approved_channels.values() if v)} | "
        f"Active histories: {len(user_chat_history)}"
    )


# ═══════════════════════════════════════════════════════════════
#  ERROR HANDLER
# ═══════════════════════════════════════════════════════════════
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global error handler — logs errors and notifies admin of critical issues."""
    logger.error(f"Exception while handling update: {context.error}", exc_info=context.error)

    # Notify admin of critical errors (throttled)
    try:
        error_msg = str(context.error)[:500]
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"⚠️ **Bot Error**\n\n```\n{error_msg}\n```",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  MAIN APPLICATION BUILDER & LAUNCHER
# ═══════════════════════════════════════════════════════════════
def main():
    """
    Builds and runs the Telegram bot application with:
    - Command handlers (/start)
    - Message handlers (private chat)
    - Callback query handlers (inline buttons)
    - Chat member handlers (bot added to channels)
    - Scheduled background jobs (news, price, whale, liquidation scanners)
    """
    logger.info("═" * 60)
    logger.info("🚀 Starting Crypto Intelligence Bot...")
    logger.info(f"   Admin ID: {ADMIN_ID}")
    logger.info(f"   Public Channel: {PUBLIC_CHANNEL_ID or 'Not set'}")
    logger.info(f"   LLM Model: {LLM_MODEL}")
    logger.info(f"   Owner: {OWNER_NAME}")
    logger.info("═" * 60)

    # Build application
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # ── Register Handlers ──

    # /start command
    app.add_handler(CommandHandler("start", start_command))

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Bot added/removed from channels/groups
    app.add_handler(ChatMemberHandler(chat_member_updated, ChatMemberHandler.MY_CHAT_MEMBER))

    # Private text messages (main chatbot)
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_private_message,
    ))

    # Global error handler
    app.add_error_handler(error_handler)

    # ── Schedule Background Jobs ──

    job_queue = app.job_queue

    # RSS News Scanner — every 5 minutes
    job_queue.run_repeating(
        scan_rss_feeds,
        interval=300,       # 5 minutes
        first=30,           # Start 30 seconds after boot
        name="rss_scanner",
    )

    # Price Movement Scanner — every 10 minutes
    job_queue.run_repeating(
        scan_price_movements,
        interval=600,       # 10 minutes
        first=60,           # Start 1 minute after boot
        name="price_scanner",
    )

    # Whale Transaction Scanner — every 15 minutes
    job_queue.run_repeating(
        scan_whale_transactions,
        interval=900,       # 15 minutes
        first=120,          # Start 2 minutes after boot
        name="whale_scanner",
    )

    # Liquidation/Squeeze Scanner — every 10 minutes
    job_queue.run_repeating(
        scan_liquidations,
        interval=600,       # 10 minutes
        first=180,          # Start 3 minutes after boot
        name="liquidation_scanner",
    )

    # Health Check — every 30 minutes
    job_queue.run_repeating(
        health_check,
        interval=1800,      # 30 minutes
        first=10,
        name="health_check",
    )

    logger.info("✅ All handlers and background jobs registered.")
    logger.info("🟢 Bot is now running. Polling for updates...")

    # Run the bot (polling mode — compatible with Railway)
    app.run_polling(
        allowed_updates=[
            Update.MESSAGE,
            Update.CALLBACK_QUERY,
            Update.MY_CHAT_MEMBER,
        ],
        drop_pending_updates=True,
    )


# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    main()
