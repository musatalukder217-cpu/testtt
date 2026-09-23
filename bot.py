"""
CRYPTO AI TELEGRAM ASSISTANT — bot.py
Stage 1: Core bot + Binance integration + Groq AI (openai/gpt-oss-120b)

This is a single-file Telegram bot. Later stages will extend this same file
with: crypto news (CryptoPanic/RSS), global market data (CoinGecko:
TOTAL/TOTAL2/TOTAL3, dominance), admin broadcast, group handling, and
richer multilingual handling.

Run:
    pip install -r requirements.txt
    cp .env.example .env   # then fill in your keys
    python bot.py
"""

# ==================================================
# IMPORTS
# ==================================================
import os
import re
import sys
import time
import json
import logging
import sqlite3
import asyncio
import io
import unicodedata
from html import escape as html_escape
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

import httpx
from dotenv import load_dotenv
from groq import Groq

from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, BotCommandScopeDefault, BotCommandScopeChat, InputFile)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler, CallbackQueryHandler, filters,
)

# ==================================================
# CONFIGURATION
# ==================================================
load_dotenv()


def _env(name: str, default: Optional[str] = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Check your .env file / Railway environment variables."
        )
    return value


TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", required=True)
GROQ_API_KEY = _env("GROQ_API_KEY", required=True)
GROQ_MODEL = _env("GROQ_MODEL", "openai/gpt-oss-120b")  # DO NOT change silently.

_raw_admin_ids = _env("ADMIN_TELEGRAM_IDS", required=True)
ADMIN_TELEGRAM_IDS = {
    int(x.strip()) for x in _raw_admin_ids.split(",") if x.strip().isdigit()
}
if not ADMIN_TELEGRAM_IDS:
    raise RuntimeError("ADMIN_TELEGRAM_IDS must contain at least one numeric Telegram user ID.")

# Optional (public Binance endpoints work without these; only needed for
# account/trade-level calls, which this bot does not perform).
BINANCE_API_KEY = _env("BINANCE_API_KEY", "")
BINANCE_API_SECRET = _env("BINANCE_API_SECRET", "")

DATABASE_PATH = _env("DATABASE_PATH", "bot.db")
TIMEZONE_NAME = _env("TIMEZONE", "Asia/Dhaka")
BOT_TIMEZONE = ZoneInfo(TIMEZONE_NAME)

CACHE_TTL_SECONDS = int(_env("CACHE_TTL_SECONDS", "30"))
RATE_LIMIT_PER_MINUTE = int(_env("RATE_LIMIT_PER_MINUTE", "15"))

BINANCE_BASE_URL = "https://api.binance.com"
TELEGRAM_MESSAGE_LIMIT = 4096

# Owner identity used only for bot/admin notifications.
OWNER_DISPLAY_NAME = "—͞Tᴍ Mᴜsᴀ⚡️"
OWNER_TELEGRAM_USERNAME = "tmmusa73"
OWNER_TELEGRAM_URL = "https://t.me/tmmusa73"

# ==================================================
# LOGGING
# ==================================================
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
# Quiet down noisy libraries; never log secrets.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger("crypto_ai_bot")

# Per-user short-lived conversation context for follow-up market/chart requests.
LAST_MARKET_CONTEXT: dict[int, dict] = {}


# ==================================================
# DATABASE (SQLite — designed to be easy to migrate to PostgreSQL later)
# ==================================================
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init() -> None:
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id     INTEGER PRIMARY KEY,
                username        TEXT,
                first_name      TEXT,
                language        TEXT DEFAULT 'en',
                is_admin        INTEGER DEFAULT 0,
                status          TEXT DEFAULT 'pending',  -- pending | approved | rejected | blocked
                created_at      TEXT DEFAULT (datetime('now')),
                last_seen_at    TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_market_context (
                telegram_id INTEGER PRIMARY KEY,
                symbol TEXT,
                coin_name TEXT,
                interval TEXT,
                limit_count INTEGER,
                timeframe_label TEXT,
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.commit()
    logger.info("Database ready at %s", DATABASE_PATH)


def db_upsert_user(telegram_id: int, username: str, first_name: str) -> sqlite3.Row:
    is_admin = 1 if telegram_id in ADMIN_TELEGRAM_IDS else 0
    with db_connect() as conn:
        existing = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        if existing is None:
            status = "approved" if is_admin else "pending"
            conn.execute(
                """
                INSERT INTO users (telegram_id, username, first_name, is_admin, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (telegram_id, username, first_name, is_admin, status),
            )
        else:
            # Admins are always kept approved even if added to ADMIN_TELEGRAM_IDS later.
            if is_admin and existing["status"] != "approved":
                conn.execute(
                    "UPDATE users SET is_admin = 1, status = 'approved' WHERE telegram_id = ?",
                    (telegram_id,),
                )
            conn.execute(
                """
                UPDATE users
                SET username = ?, first_name = ?, last_seen_at = datetime('now')
                WHERE telegram_id = ?
                """,
                (username, first_name, telegram_id),
            )
        conn.commit()
        return conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()


def db_get_user(telegram_id: int) -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()


def db_save_chat_message(telegram_id: int, role: str, content: str) -> None:
    content = (content or "").strip()
    if not content:
        return
    # Keep stored history bounded so the single-file bot/database stays lightweight.
    content = content[:8000]
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO chat_history (telegram_id, role, content) VALUES (?, ?, ?)",
            (telegram_id, role, content),
        )
        conn.execute(
            """
            DELETE FROM chat_history
            WHERE telegram_id = ? AND id NOT IN (
                SELECT id FROM chat_history WHERE telegram_id = ?
                ORDER BY id DESC LIMIT 20
            )
            """,
            (telegram_id, telegram_id),
        )
        conn.commit()


def db_get_chat_history(telegram_id: int, limit: int = 8) -> list[sqlite3.Row]:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT role, content, created_at FROM chat_history WHERE telegram_id = ? ORDER BY id DESC LIMIT ?",
            (telegram_id, max(1, min(limit, 20))),
        ).fetchall()
    return list(reversed(rows))


def db_save_market_context(telegram_id: int, resolved: tuple[str, str], interval: str, limit_count: int, timeframe_label: str) -> None:
    symbol, coin_name = resolved
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO user_market_context (telegram_id, symbol, coin_name, interval, limit_count, timeframe_label, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(telegram_id) DO UPDATE SET
                symbol=excluded.symbol, coin_name=excluded.coin_name, interval=excluded.interval,
                limit_count=excluded.limit_count, timeframe_label=excluded.timeframe_label, updated_at=datetime('now')
            """,
            (telegram_id, symbol, coin_name, interval, limit_count, timeframe_label),
        )
        conn.commit()


def db_get_market_context(telegram_id: int) -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM user_market_context WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()


def db_set_status(telegram_id: int, status: str) -> bool:
    with db_connect() as conn:
        cur = conn.execute(
            "UPDATE users SET status = ? WHERE telegram_id = ?", (status, telegram_id)
        )
        conn.commit()
        return cur.rowcount > 0


def db_list_by_status(status: str, limit: int = 50):
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()


def db_list_all(limit: int = 50):
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()


# ==================================================
# CACHE (simple in-memory TTL cache to reduce API calls)
# ==================================================
class TTLCache:
    def __init__(self, ttl_seconds: int):
        self.ttl_seconds = ttl_seconds
        self._store: dict[str, tuple[float, object]] = {}

    def get(self, key: str):
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.time() > expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: object) -> None:
        self._store[key] = (time.time() + self.ttl_seconds, value)


market_cache = TTLCache(CACHE_TTL_SECONDS)


# ==================================================
# RATE LIMITING (per-user sliding window)
# ==================================================
class RateLimiter:
    def __init__(self, max_per_minute: int):
        self.max_per_minute = max_per_minute
        self._hits: dict[int, deque] = {}

    def allow(self, user_id: int) -> bool:
        now = time.time()
        window = self._hits.setdefault(user_id, deque())
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.max_per_minute:
            return False
        window.append(now)
        return True


rate_limiter = RateLimiter(RATE_LIMIT_PER_MINUTE)


# ==================================================
# BINANCE (public REST endpoints — no credentials required)
# ==================================================
class BinanceError(Exception):
    pass


async def _binance_get(path: str, params: dict) -> dict | list:
    cache_key = f"{path}:{json.dumps(params, sort_keys=True)}"
    cached = market_cache.get(cache_key)
    if cached is not None:
        return cached
    url = f"{BINANCE_BASE_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
        if resp.status_code != 200:
            raise BinanceError(f"Binance API error {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        market_cache.set(cache_key, data)
        return data
    except httpx.HTTPError as exc:
        raise BinanceError(f"Binance request failed: {exc}") from exc


async def binance_ticker_24hr(symbol: str) -> dict:
    data = await _binance_get("/api/v3/ticker/24hr", {"symbol": symbol})
    if isinstance(data, dict) and data.get("code"):
        raise BinanceError(f"Unknown symbol on Binance: {symbol}")
    return data


async def binance_klines(symbol: str, interval: str = "1h", limit: int = 100) -> list:
    data = await _binance_get(
        "/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit}
    )
    if isinstance(data, dict):
        raise BinanceError(f"Unknown symbol/interval on Binance: {symbol}/{interval}")
    return data


@dataclass
class MarketSnapshot:
    symbol: str
    price: float
    change_24h_pct: float
    high_24h: float
    low_24h: float
    volume_24h_base: float
    volume_24h_quote: float


async def get_market_snapshot(symbol: str) -> MarketSnapshot:
    t = await binance_ticker_24hr(symbol)
    return MarketSnapshot(
        symbol=symbol,
        price=float(t["lastPrice"]),
        change_24h_pct=float(t["priceChangePercent"]),
        high_24h=float(t["highPrice"]),
        low_24h=float(t["lowPrice"]),
        volume_24h_base=float(t["volume"]),
        volume_24h_quote=float(t["quoteVolume"]),
    )


# ==================================================
# TECHNICAL ANALYSIS (basic indicators, no external TA library required)
# ==================================================
@dataclass
class TechnicalSummary:
    sma_20: Optional[float]
    sma_50: Optional[float]
    rsi_14: Optional[float]
    support: Optional[float]
    resistance: Optional[float]
    near_support: Optional[float]
    near_resistance: Optional[float]


def _sma(values: list[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _rsi(closes: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(-period, 0):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_technical_summary(klines: list) -> TechnicalSummary:
    # Kline format: [open_time, open, high, low, close, volume, close_time, ...]
    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]

    sma_20 = _sma(closes, 20)
    sma_50 = _sma(closes, 50)
    rsi_14 = _rsi(closes, 14)

    support = min(lows) if lows else None
    resistance = max(highs) if highs else None
    near_window = 10
    near_support = min(lows[-near_window:]) if len(lows) >= 1 else None
    near_resistance = max(highs[-near_window:]) if len(highs) >= 1 else None

    return TechnicalSummary(
        sma_20=sma_20,
        sma_50=sma_50,
        rsi_14=rsi_14,
        support=support,
        resistance=resistance,
        near_support=near_support,
        near_resistance=near_resistance,
    )


# ==================================================
# COIN / SYMBOL RESOLUTION
# ==================================================
# Small, explicit alias map for common coins. Unknown tickers are passed
# through as SYMBOL+USDT and validated against the live Binance response —
# we never invent data for a symbol Binance does not return.
COIN_ALIASES = {
    "btc": ("BTCUSDT", "Bitcoin"), "bitcoin": ("BTCUSDT", "Bitcoin"),
    "বিটকয়েন": ("BTCUSDT", "Bitcoin"), "বিটকয়েন": ("BTCUSDT", "Bitcoin"),
    "বিটকয়েনের": ("BTCUSDT", "Bitcoin"), "বিটকয়েনের": ("BTCUSDT", "Bitcoin"),
    "eth": ("ETHUSDT", "Ethereum"), "ethereum": ("ETHUSDT", "Ethereum"),
    "ইথেরিয়াম": ("ETHUSDT", "Ethereum"), "ইথেরিয়াম": ("ETHUSDT", "Ethereum"),
    "ইথারিয়াম": ("ETHUSDT", "Ethereum"), "ইথারিয়াম": ("ETHUSDT", "Ethereum"),
    "ইথার": ("ETHUSDT", "Ethereum"), "থুরিয়াম": ("ETHUSDT", "Ethereum"),
    "থুরিয়াম": ("ETHUSDT", "Ethereum"), "ইথেরিয়ামের": ("ETHUSDT", "Ethereum"),
    "bnb": ("BNBUSDT", "BNB"), "বিএনবি": ("BNBUSDT", "BNB"),
    "sol": ("SOLUSDT", "Solana"), "solana": ("SOLUSDT", "Solana"), "সোলানা": ("SOLUSDT", "Solana"),
    "xrp": ("XRPUSDT", "XRP"), "ripple": ("XRPUSDT", "XRP"), "রিপল": ("XRPUSDT", "XRP"),
    "ada": ("ADAUSDT", "Cardano"), "cardano": ("ADAUSDT", "Cardano"), "কার্ডানো": ("ADAUSDT", "Cardano"),
    "doge": ("DOGEUSDT", "Dogecoin"), "dogecoin": ("DOGEUSDT", "Dogecoin"),
    "ডজকয়েন": ("DOGEUSDT", "Dogecoin"), "ডজকয়েন": ("DOGEUSDT", "Dogecoin"),
    "ton": ("TONUSDT", "Toncoin"), "টন": ("TONUSDT", "Toncoin"),
    "trx": ("TRXUSDT", "TRON"), "ট্রন": ("TRXUSDT", "TRON"),
    "dot": ("DOTUSDT", "Polkadot"), "পোলকাডট": ("DOTUSDT", "Polkadot"),
    "matic": ("MATICUSDT", "Polygon"), "polygon": ("MATICUSDT", "Polygon"),
    "avax": ("AVAXUSDT", "Avalanche"), "ltc": ("LTCUSDT", "Litecoin"),
    "লাইটকয়েন": ("LTCUSDT", "Litecoin"), "লাইটকয়েন": ("LTCUSDT", "Litecoin"),
    "link": ("LINKUSDT", "Chainlink"), "চেইনলিংক": ("LINKUSDT", "Chainlink"),
    "shib": ("SHIBUSDT", "Shiba Inu"), "শিবা": ("SHIBUSDT", "Shiba Inu"),
    "sui": ("SUIUSDT", "Sui"), "সুই": ("SUIUSDT", "Sui"),
    "muse": ("MUSEUSDT", "MUSE"), "muse ai": ("MUSEUSDT", "MUSE"),
    "মিউজ": ("MUSEUSDT", "MUSE"), "মিউজ এআই": ("MUSEUSDT", "MUSE"),
}

CRYPTO_KEYWORDS = [
    "crypto", "cryptocurrency", "bitcoin", "btc", "ethereum", "eth", "altcoin",
    "blockchain", "defi", "web3", "nft", "token", "coin", "wallet", "mining",
    "staking", "exchange", "binance", "futures", "spot trading", "market cap",
    "dominance", "total", "total2", "total 2", "total3", "total 3", "btc.d",
    "eth.d", "support", "resistance", "sma", "rsi", "chart", "graph", "market",
    "crypto market", "price", "live price",
    "ক্রিপ্টো", "ক্রিপ্টোকারেন্সি", "বিটকয়েন", "বিটকয়েন", "ইথেরিয়াম", "ইথেরিয়াম",
    "ইথারিয়াম", "ইথারিয়াম", "ইথার", "থুরিয়াম", "থুরিয়াম", "ব্লকচেইন", "কয়েন",
    "কয়েন", "বিএনবি", "সোলানা", "রিপল", "ডজকয়েন", "ডজকয়েন", "দাম", "প্রাইস",
    "মূল্য", "লাইভ", "সাপোর্ট", "রেজিস্ট্যান্স", "রেজিস্টেন্স", "এসএমএ", "আরএসআই",
    "চার্ট", "গ্রাফ", "ছবি", "ফটো", "মার্কেট", "ডমিনেন্স", "মার্কেট ক্যাপ",
    "ट्रिप्टो", "क्रिप्टो", "बिटकॉइन", "इथेरियम", "एथेरियम", "कीमत", "सपोर्ट",
    "रेजिस्टेंस", "चार्ट", "मार्केट", "डॉमिनेंस",
]

BENGALI_LATIN_HINTS = {
    "ami", "amar", "amake", "tumi", "tomar", "apni", "apnar", "eta", "ei", "oi",
    "ki", "koto", "keno", "kemne", "kivabe", "bol", "bolo", "den", "dao", "daw",
    "chai", "chay", "price", "dam", "bazar", "market", "ekhon", "akhon", "live",
    "theke", "jonno", "er", "r", "e", "te", "ta", "ti", "ke", "diye", "dekho",
    "support", "resistance", "chart", "banai", "banaye", "dekhaw", "dekhai",
    "hobe", "ache", "nai", "parbe", "parbo", "jante", "janao", "koro", "korbe",
    "ethereum", "bitcoin", "eth", "btc", "coin", "crypto", "total", "dominance",
}
HINDI_LATIN_HINTS = {
    "mera", "meri", "mujhe", "main", "mai", "aap", "apka", "apki", "tum", "tumhara",
    "kya", "kitna", "kaise", "kyun", "hai", "hain", "batao", "bataiye", "chahiye",
    "abhi", "daam", "bhav", "bazaar", "keemat", "support", "resistance", "chart",
    "ethereum", "bitcoin", "price", "market", "crypto",
}


def normalize_user_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").strip().lower()
    replacements = {
        "ইথেরিয়াম": "ইথেরিয়াম", "ইথারিয়াম": "ইথারিয়াম",
        "বিটকয়েন": "বিটকয়েন", "ডজকয়েন": "ডজকয়েন",
        "লাইটকয়েন": "লাইটকয়েন", "কয়েন": "কয়েন",
        "থুরিয়াম": "থুরিয়াম", "রেজিস্টেন্স": "রেজিস্ট্যান্স",
    }
    for a, b in replacements.items():
        text = text.replace(a, b)
    return text


def _latin_words(text: str) -> set[str]:
    return set(re.findall(r"[a-zA-Z]+", normalize_user_text(text)))


def is_crypto_query(text: str) -> bool:
    # Do not reject a normal/general question merely because it lacks a crypto keyword.
    # The LLM is explicitly instructed to handle general knowledge too.
    return True


def extract_symbol(text: str) -> Optional[tuple[str, str]]:
    t = normalize_user_text(text)
    for alias in sorted(COIN_ALIASES, key=len, reverse=True):
        if re.search(rf"(?<![a-zA-Z0-9]){re.escape(alias)}(?![a-zA-Z0-9])", t):
            return COIN_ALIASES[alias]
    # Common dotted dominance notation should not be treated as a coin symbol.
    if re.search(r"\b(?:btc\.d|eth\.d)\b", t):
        return None
    match = re.search(r"(?<![A-Za-z])([A-Za-z]{2,10})(?![A-Za-z])", text)
    if match and match.group(1).lower() not in {
        "the", "and", "for", "with", "price", "live", "chart", "total", "market"
    }:
        candidate = match.group(1).upper()
        return f"{candidate}USDT", candidate
    return None


def detect_language(text: str) -> str:
    text = text or ""
    if re.search(r"[\u0980-\u09FF]", text):
        return "bn"
    if re.search(r"[\u0900-\u097F]", text):
        return "hi"

    words = _latin_words(text)
    bn_score = len(words & BENGALI_LATIN_HINTS)
    hi_score = len(words & HINDI_LATIN_HINTS)

    # Romanized Bengali / Banglish and Romanized Hindi / Hinglish.
    # Banglish input must receive Bengali-script output.
    if bn_score >= 2 and bn_score >= hi_score + 1:
        return "bn"
    if hi_score >= 2 and hi_score > bn_score:
        return "hi"
    return "en"


UI_TEXT = {
    "welcome": {
        "en": "Welcome to the Crypto AI Assistant! I can answer questions about crypto prices, market data, and technical analysis.",
        "bn": "ক্রিপ্টো AI অ্যাসিস্ট্যান্টে স্বাগতম! আমি ক্রিপ্টো প্রাইস, মার্কেট ডেটা এবং টেকনিক্যাল অ্যানালাইসিস নিয়ে প্রশ্নের উত্তর দিতে পারি।",
        "hi": "क्रिप्टो AI असिस्टेंट में आपका स्वागत है! मैं क्रिप्टो कीमतों, मार्केट डेटा और तकनीकी विश्लेषण के बारे में सवालों के जवाब दे सकता हूँ।",
    },
    "pending": {
        "en": "Your access request has been sent to the admin. Please wait for approval.",
        "bn": "আপনার অ্যাক্সেস রিকোয়েস্ট অ্যাডমিনের কাছে পাঠানো হয়েছে। অনুমোদনের জন্য অপেক্ষা করুন।",
        "hi": "आपका एक्सेस अनुरोध एडमिन को भेज दिया गया है। कृपया स्वीकृति की प्रतीक्षा करें।",
    },
    "blocked": {"en":"Your access is blocked by the admin.","bn":"অ্যাডমিন আপনার অ্যাক্সেস ব্লক করেছেন।","hi":"एडमिन ने आपका एक्सेस ब्लॉक कर दिया है।"},
    "rejected": {
        "en": "Your access request was declined by the admin.",
        "bn": "আপনার অ্যাক্সেস রিকোয়েস্ট অ্যাডমিন কর্তৃক প্রত্যাখ্যান করা হয়েছে।",
        "hi": "आपका एक्सेस अनुरोध एडमिन द्वारा अस्वीकार कर दिया गया है।",
    },
    "not_crypto": {
        "en": "I can only assist with cryptocurrency, blockchain, crypto markets, trading, and related crypto information.",
        "bn": "আমি শুধুমাত্র ক্রিপ্টোকারেন্সি, ব্লকচেইন, ক্রিপ্টো মার্কেট, ট্রেডিং এবং সম্পর্কিত ক্রিপ্টো তথ্য নিয়ে সাহায্য করতে পারি।",
        "hi": "मैं केवल क्रिप्टोकरेंसी, ब्लॉकचेन, क्रिप्टो मार्केट, ट्रेडिंग और संबंधित क्रिप्टो जानकारी में मदद कर सकता हूँ।",
    },
    "rate_limited": {
        "en": "You're sending requests too fast. Please wait a moment and try again.",
        "bn": "আপনি খুব দ্রুত রিকোয়েস্ট পাঠাচ্ছেন। একটু অপেক্ষা করে আবার চেষ্টা করুন।",
        "hi": "आप बहुत तेज़ी से अनुरोध भेज रहे हैं। कृपया थोड़ा इंतज़ार करें और फिर कोशिश करें।",
    },
    "unknown_symbol": {
        "en": "I couldn't find that symbol on Binance. Please check the ticker and try again.",
        "bn": "Binance-এ এই সিম্বলটি খুঁজে পাওয়া যায়নি। টিকার চেক করে আবার চেষ্টা করুন।",
        "hi": "मुझे Binance पर यह सिंबल नहीं मिला। कृपया टिकर जांचें और फिर से प्रयास करें।",
    },
}


def ui_text(key: str, lang: str) -> str:
    return UI_TEXT.get(key, {}).get(lang, UI_TEXT.get(key, {}).get("en", ""))


# ==================================================
# GROQ AI (openai/gpt-oss-120b via Groq API)
# ==================================================
groq_client = Groq(api_key=GROQ_API_KEY)

SYSTEM_PROMPT_TEMPLATE = """You are a highly capable general conversational AI assistant with strong specialization in cryptocurrency, blockchain, trading, and markets.

STRICT RULES:
1. Understand the user's meaning naturally even with Bengali, Banglish, Hindi, Hinglish, English, mixed languages, spelling mistakes, phonetic spellings, typos, and short/incorrect sentences. Infer intended meaning from context instead of refusing.
2. Answer ONLY the user's actual question. Do not add unrelated news, lectures, sections, disclaimers, or follow-up questions.
3. Crypto is your strongest domain. Use the supplied live market context whenever the question is about a coin, price, market cap, dominance, TOTAL/TOTAL2/TOTAL3, trading, support, resistance, RSI, SMA, or charts.
4. For live numeric facts, use ONLY supplied live data. Never invent current numbers.
5. If live data for the exact item is unavailable, say so briefly and still answer any conceptual part you can.
6. You are also a general-purpose AI. You may answer normal general-knowledge questions instead of rejecting them.
7. LANGUAGE OUTPUT RULES:
   - Bengali script input -> answer in Bengali script.
   - Banglish/Romanized Bengali input -> ALWAYS answer in Bengali script.
   - Hindi script input -> answer in Hindi script.
   - Hinglish/Romanized Hindi input -> answer in Hindi script.
   - English input -> answer in English.
   - Mixed input containing Bengali/Banglish -> prefer Bengali script unless the user explicitly asks for English.
8. Do not mirror Banglish/Romanized Bengali in the answer; convert it to natural Bengali script.
9. Plain text only. Never use Markdown stars (** or *), backticks, # headings, or decorative Markdown.
10. If the user asks for a crypto market chart/image, the bot generates the chart separately from live candle data. Never claim an arbitrary artistic/2D/3D image was generated unless an actual image-generation backend sends one.
11. Market direction is analysis/scenario, not certainty or personalized investment advice.
12. Keep the answer concise and directly relevant.
13. For chart analysis, use the supplied PRICE_ACTION_DRAWING_ENGINE as the primary structural drawing evidence. Do not invent drawings that the engine did not detect.
14. Drawing-tool selection must be contextual: Horizontal Line for a specific support/resistance level; Trend Line for repeated directional swing highs/lows; Parallel Channel for two reasonably parallel price boundaries; Rectangle for a defined consolidation/zone. Do not use every tool at once.
15. Distinguish wick penetration from a candle-body close. A wick through a level alone is not a confirmed breakout or breakdown.
16. For breakout/breakdown analysis, explain the trigger level, required candle-close condition, timeframe, retest condition when applicable, and invalidation condition. Describe future moves as conditional scenarios, never as guaranteed timing or certainty.
17. Never proactively tell the user to take a Long or Short position and never present Long/Short as the default next action. Only provide a Long or Short trade setup when the user explicitly asks for that specific setup. When explicitly requested, use the supplied live OHLCV and price-action structure to produce a conditional Entry, Stop Loss, TP1, TP2, TP3, invalidation level, and risk/reward values. Do not invent current prices.
18. When a chart is requested, the bot may send an annotated chart image and a separate written explanation beneath it. The written explanation must match the actual drawings and detected structure. Never place the AI drawing explanation text inside the chart image.
"""

LANGUAGE_NAMES = {"en": "English", "bn": "Bengali (বাংলা)", "hi": "Hindi (हिन्दी)", "bn_latn": "Banglish (Bengali written with Latin/English letters)", "hi_latn": "Hinglish (Hindi written with Latin/English letters)"}


def build_context_block(
    user_query: str,
    language: str,
    coin_name: Optional[str],
    snapshot: Optional[MarketSnapshot],
    ta: Optional[TechnicalSummary],
) -> str:
    now_str = datetime.now(BOT_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [
        f"CURRENT_TIME: {now_str}",
        f"USER_LANGUAGE: {LANGUAGE_NAMES.get(language, 'English')}",
        f"USER_QUERY: {user_query}",
        f"COIN: {coin_name or 'Not specified'}",
    ]
    if snapshot:
        lines.append(
            "LIVE_MARKET_DATA: "
            f"price={snapshot.price} USD, 24h_change={snapshot.change_24h_pct}%, "
            f"24h_high={snapshot.high_24h}, 24h_low={snapshot.low_24h}, "
            f"24h_volume_base={snapshot.volume_24h_base}, "
            f"24h_volume_quote(USDT)={snapshot.volume_24h_quote}"
        )
    else:
        lines.append("LIVE_MARKET_DATA: Not available for this query.")

    if ta:
        lines.append(
            "TECHNICAL_ANALYSIS: "
            f"SMA20={ta.sma_20}, SMA50={ta.sma_50}, RSI14={ta.rsi_14}, "
            f"overall_support={ta.support}, overall_resistance={ta.resistance}, "
            f"near_term_support={ta.near_support}, near_term_resistance={ta.near_resistance} "
            "(computed from the last 100 x 1h candles on Binance)"
        )
    else:
        lines.append("TECHNICAL_ANALYSIS: Not available for this query.")

    lines.append("SOURCES: Binance public REST API (live), computed locally (technical analysis).")
    return "\n".join(lines)


async def ask_groq(system_prompt: str, context_block: str) -> str:
    def _call():
        completion = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": context_block},
            ],
            temperature=0.4,
            max_tokens=900,
        )
        return completion.choices[0].message.content

    return await asyncio.to_thread(_call)


# ==================================================
# MESSAGE SPLITTING (Telegram 4096-char limit)
# ==================================================
def clean_ai_text(text: str) -> str:
    text=text.replace("**","").replace("__","").replace("`","").replace("*","")
    text=re.sub(r"^#{1,6}\s*","",text,flags=re.MULTILINE)
    return re.sub(r"\n{3,}","\n\n",text).strip()

def split_message(text: str, limit: int = 3800) -> list[str]:
    text=clean_ai_text(text)
    if len(text)<=limit: return [text]
    out=[]; rem=text
    while len(rem)>limit:
        pos=rem.rfind("\n\n",0,limit)
        if pos<800: pos=rem.rfind("\n",0,limit)
        if pos<800: pos=rem.rfind(" ",0,limit)
        if pos<1: pos=limit
        out.append(rem[:pos].strip()); rem=rem[pos:].lstrip()
    if rem: out.append(rem)
    return out

async def reply_long(update: Update, text: str) -> None:
    for chunk in split_message(text):
        await update.message.reply_text(chunk,parse_mode=None)
        await asyncio.sleep(0.05)


# ==================================================
# ACCESS CONTROL HELPERS
# ==================================================
def ensure_user_record(update: Update) -> sqlite3.Row:
    user = update.effective_user
    return db_upsert_user(user.id, user.username or "", user.first_name or "")


def is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_TELEGRAM_IDS


async def require_approved(update: Update) -> bool:
    # Administrators never need an approval request, even if an older database
    # record still has a non-approved status.
    if is_admin(update.effective_user.id):
        return True
    row=db_get_user(update.effective_user.id)
    if row is None:
        await update.message.reply_text(ui_text("pending",detect_language(update.message.text or ""))); return False
    if row["status"]=="blocked":
        await update.message.reply_text(ui_text("blocked",detect_language(update.message.text or ""))); return False
    if row["status"]!="approved":
        await update.message.reply_text(ui_text("rejected" if row["status"]=="rejected" else "pending",detect_language(update.message.text or ""))); return False
    return True

def pending_request_keyboard(uid:int):
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve",callback_data=f"approve_user:{uid}"),InlineKeyboardButton("❌ Reject",callback_data=f"reject_user:{uid}")]])

def approved_user_keyboard(uid:int):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Block",callback_data=f"block_user:{uid}")]])

def blocked_user_keyboard(uid:int):
    return InlineKeyboardMarkup([[InlineKeyboardButton("♻️ Unblock",callback_data=f"unblock_user:{uid}")]])

def format_admin_user_card(row):
    uname=f"@{row['username']}" if row["username"] else "(no username)"
    return f"👤 {row['first_name']}\nID: {row['telegram_id']}\nUsername: {uname}\nStatus: {row['status']}"

def owner_signature_html() -> str:
    return f'👑 Owner: <a href="{OWNER_TELEGRAM_URL}">{html_escape(OWNER_DISPLAY_NAME)}</a>'


async def notify_admins(context, text, reply_markup=None, include_owner_signature: bool = True):
    final_text = text
    if include_owner_signature:
        final_text = f"{text}\n\n{owner_signature_html()}"
    for admin_id in ADMIN_TELEGRAM_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=final_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception("Failed to notify admin %s", admin_id)

async def admin_callback_handler(update: Update,context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    if not q or not is_admin(q.from_user.id): return
    await q.answer(); data=q.data or ""; action,_,raw=data.partition(":")
    if action in {"approve_user","reject_user","block_user","unblock_user"} and raw.isdigit():
        uid=int(raw)
        if action=="block_user" and uid in ADMIN_TELEGRAM_IDS:
            await q.answer("Admin cannot be blocked.",show_alert=True); return
        status={"approve_user":"approved","reject_user":"rejected","block_user":"blocked","unblock_user":"approved"}[action]
        if db_set_status(uid,status):
            label={"approved":"✅ Approved","rejected":"❌ Rejected","blocked":"🚫 Blocked","unblock_user":"♻️ Unblocked"}.get(status,"♻️ Unblocked") if action!="unblock_user" else "♻️ Unblocked"
            try: await q.edit_message_reply_markup(reply_markup=None)
            except Exception: pass
            try: await context.bot.send_message(chat_id=uid,text=label)
            except Exception: logger.exception("Could not notify user %s",uid)
            await q.message.reply_text(f"{label}: {uid}")
        return
    if data=="admin:approved":
        rows=db_list_by_status("approved",1000)+db_list_by_status("blocked",1000)
        if not rows:
            await q.message.reply_text("No approved users.")
        for r in rows:
            markup=approved_user_keyboard(r["telegram_id"]) if r["status"]=="approved" else blocked_user_keyboard(r["telegram_id"])
            await q.message.reply_text(format_admin_user_card(r),reply_markup=markup)
    elif data=="admin:pending":
        rows=db_list_by_status("pending",1000)
        if not rows: await q.message.reply_text("No pending requests.")
        for r in rows: await q.message.reply_text(format_admin_user_card(r),reply_markup=pending_request_keyboard(r["telegram_id"]))
    elif data=="admin:stats":
        rows=db_list_all(100000); c={x:sum(1 for r in rows if r["status"]==x) for x in ("approved","pending","blocked","rejected")}
        await q.message.reply_text(f"📊 Bot Stats\nApproved: {c['approved']}\nPending: {c['pending']}\nBlocked: {c['blocked']}\nRejected: {c['rejected']}")
    elif data=="admin:broadcast":
        await q.message.reply_text("Use /broadcast <message> to send to approved users.")

def admin_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("👥 Approved Users",callback_data="admin:approved")],[InlineKeyboardButton("⏳ Pending Requests",callback_data="admin:pending")],[InlineKeyboardButton("📢 Broadcast",callback_data="admin:broadcast")],[InlineKeyboardButton("📊 Bot Stats",callback_data="admin:stats")]])

# ==================================================
# TELEGRAM HANDLERS
# ==================================================
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row=ensure_user_record(update); lang=detect_language(update.message.text or "")
    # Administrators are always trusted and never see an approval request.
    if is_admin(update.effective_user.id):
        welcome = ui_text("welcome", lang) + "\n\n" + owner_signature_html()
        await update.message.reply_text(
            welcome, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
        return

    if row["status"]=="approved":
        welcome = ui_text("welcome", lang) + "\n\n" + owner_signature_html()
        await update.message.reply_text(
            welcome, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
    elif row["status"]=="pending":
        pending_text = ui_text("pending",lang) + "\n\n" + owner_signature_html()
        await update.message.reply_text(
            pending_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
        request_text = (
            "🆕 <b>New access request</b>\n"
            f"👤 Name: {html_escape(row['first_name'] or '(no name)')}\n"
            f"🆔 Telegram ID: <code>{row['telegram_id']}</code>\n"
            f"🔗 Username: @{html_escape(row['username'])}" if row['username'] else
            "🆕 <b>New access request</b>\n"
            f"👤 Name: {html_escape(row['first_name'] or '(no name)')}\n"
            f"🆔 Telegram ID: <code>{row['telegram_id']}</code>\n"
            "🔗 Username: (no username)"
        )
        request_text += "\n\nApprove or reject:"
        await notify_admins(context, request_text, pending_request_keyboard(row["telegram_id"]))
    elif row["status"]=="blocked":
        await update.message.reply_text(ui_text("blocked",lang))
    else:
        await update.message.reply_text(ui_text("rejected",lang))


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = detect_language(update.message.text or "")
    texts = {
        "en": (
            "Commands:\n"
            "/start - register / check access\n"
            "/help - show this help\n"
            "/price <symbol> - live price + 24h stats (e.g. /price BTC)\n"
            "/status - show your approval status\n"
            "/id - show your Telegram ID\n/news - latest crypto news\n/market - global crypto market data\n\n"
            "You can also just ask me things like:\n"
            "\"BTC এখন কোন দিকে যেতে পারে?\" or \"What's ETH support level?\""
        ),
        "bn": (
            "কমান্ডসমূহ:\n"
            "/start - রেজিস্টার / অ্যাক্সেস চেক করুন\n"
            "/help - এই হেল্প দেখুন\n"
            "/price <symbol> - লাইভ প্রাইস + ২৪ ঘণ্টার তথ্য (যেমন /price BTC)\n"
            "/status - আপনার অনুমোদনের অবস্থা দেখুন\n"
            "/id - আপনার Telegram ID দেখুন\n/news - সর্বশেষ ক্রিপ্টো নিউজ\n/market - গ্লোবাল ক্রিপ্টো মার্কেট ডেটা\n\n"
            "এছাড়া সরাসরি প্রশ্ন করতে পারেন, যেমন:\n"
            "\"BTC এখন কোন দিকে যেতে পারে?\""
        ),
        "hi": (
            "कमांड्स:\n"
            "/start - रजिस्टर करें / एक्सेस जांचें\n"
            "/help - यह सहायता दिखाएं\n"
            "/price <symbol> - लाइव कीमत + 24h आंकड़े (जैसे /price BTC)\n"
            "/status - अपनी स्वीकृति स्थिति देखें\n"
            "/id - अपना Telegram ID देखें\n/news - नवीनतम क्रिप्टो न्यूज़\n/market - ग्लोबल क्रिप्टो मार्केट डेटा"
        ),
    }
    await update.message.reply_text(texts.get(lang, texts["en"]))


async def id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Your Telegram ID: {update.effective_user.id}")


async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = db_get_user(update.effective_user.id)
    if row is None:
        await update.message.reply_text("You haven't registered yet. Send /start first.")
        return
    await update.message.reply_text(f"Status: {row['status']}")


async def price_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_approved(update):
        return
    lang = detect_language(update.message.text or "")
    if not context.args:
        await update.message.reply_text("Usage: /price BTC")
        return
    coin_input = " ".join(context.args)
    resolved = extract_symbol(coin_input)
    if not resolved:
        await update.message.reply_text(ui_text("unknown_symbol", lang))
        return
    symbol, name = resolved
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        snap = await get_market_snapshot(symbol)
    except BinanceError:
        await update.message.reply_text(ui_text("unknown_symbol", lang))
        return
    text = (
        f"{name} ({symbol})\n"
        f"Price: {snap.price} USDT\n"
        f"24h change: {snap.change_24h_pct}%\n"
        f"24h high/low: {snap.high_24h} / {snap.low_24h}\n"
        f"24h volume: {snap.volume_24h_base} {symbol.replace('USDT', '')} "
        f"({snap.volume_24h_quote} USDT)"
    )
    await update.message.reply_text(text)



# Timeframe resolution for Binance candles.
# Native Binance intervals are used where available; a yearly candle is built
# from 12 monthly candles because Binance has no native 1y kline interval.
TIMEFRAME_PATTERNS = [
    (("1 মিনিট", "১ মিনিট", "1 minute", "1m candle", "১m"), "1m", 300, "1 Minute"),
    (("3 মিনিট", "৩ মিনিট", "3 minute"), "3m", 300, "3 Minute"),
    (("5 মিনিট", "৫ মিনিট", "5 minute"), "5m", 300, "5 Minute"),
    (("15 মিনিট", "১৫ মিনিট", "15 minute"), "15m", 300, "15 Minute"),
    (("30 মিনিট", "৩০ মিনিট", "30 minute"), "30m", 300, "30 Minute"),
    (("2 ঘন্টা", "২ ঘন্টা", "2 hour", "2h"), "2h", 300, "2 Hour"),
    (("4 ঘন্টা", "৪ ঘন্টা", "4 hour", "4h"), "4h", 300, "4 Hour"),
    (("6 ঘন্টা", "৬ ঘন্টা", "6 hour", "6h"), "6h", 300, "6 Hour"),
    (("8 ঘন্টা", "৮ ঘন্টা", "8 hour", "8h"), "8h", 300, "8 Hour"),
    (("12 ঘন্টা", "১২ ঘন্টা", "12 hour", "12h"), "12h", 300, "12 Hour"),
    (("দৈনিক", "দিনের ক্যান্ডেল", "daily", "1 day", "1d"), "1d", 365, "Daily"),
    (("3 দিন", "৩ দিন", "3 day", "3d"), "3d", 300, "3 Day"),
    (("সাপ্তাহিক", "সপ্তাহের ক্যান্ডেল", "weekly", "1 week", "1w"), "1w", 260, "Weekly"),
    (("মাসিক", "মাসের ক্যান্ডেল", "monthly", "1 month", "1mo"), "1M", 240, "Monthly"),
    (("বার্ষিক", "বছরের ক্যান্ডেল", "yearly", "annual", "1 year", "1y"), "1y", 120, "Yearly"),
    (("ঘন্টার", "১ ঘন্টা", "1 hour", "hourly", "1h"), "1h", 300, "1 Hour"),
]

def resolve_timeframe(text: str) -> tuple[str, int, str]:
    t = normalize_user_text(text)
    for terms, interval, limit, label in TIMEFRAME_PATTERNS:
        if any(term in t for term in terms):
            return interval, limit, label
    return "1h", 300, "1 Hour"

def aggregate_yearly_klines(monthly_klines: list) -> list:
    """Aggregate Binance 1M klines into calendar-year OHLCV candles."""
    if not monthly_klines:
        return []
    grouped = {}
    for k in monthly_klines:
        year = datetime.fromtimestamp(int(k[0]) / 1000, tz=BOT_TIMEZONE).year
        grouped.setdefault(year, []).append(k)
    yearly = []
    for year in sorted(grouped):
        rows = grouped[year]
        yearly.append([
            rows[0][0],
            rows[0][1],
            str(max(float(r[2]) for r in rows)),
            str(min(float(r[3]) for r in rows)),
            rows[-1][4],
            str(sum(float(r[5]) for r in rows)),
            rows[-1][6],
        ])
    return yearly

async def get_timeframe_klines(symbol: str, interval: str, limit: int) -> list:
    if interval == "1y":
        monthly = await binance_klines(symbol, "1M", min(1000, max(120, limit * 12)))
        return aggregate_yearly_klines(monthly)
    return await binance_klines(symbol, interval, min(1000, limit))


# ==================================================
# PRICE-ACTION DRAWING / BREAKOUT ENGINE
# ==================================================
@dataclass
class PriceActionAnalysis:
    trend: str = "Neutral"
    support: Optional[float] = None
    resistance: Optional[float] = None
    trend_line: Optional[tuple] = None          # ((x1,y1),(x2,y2))
    trend_line_points: Optional[tuple] = None   # original swing points used for the line
    channel_upper: Optional[tuple] = None       # ((x1,y1),(x2,y2))
    channel_lower: Optional[tuple] = None
    channel_upper_points: Optional[tuple] = None
    channel_lower_points: Optional[tuple] = None
    rectangle_zone: Optional[tuple] = None      # (x1,x2,bottom,top)
    drawing_reasons: list[str] = field(default_factory=list)
    breakout_status: str = "None"
    breakout_trigger: Optional[float] = None
    breakout_index: Optional[int] = None
    retest_status: str = "Not detected"
    invalidation: Optional[float] = None
    projected_target: Optional[float] = None
    confidence: str = "Low"
    reason: str = ""


def _find_swing_points(highs: list[float], lows: list[float], window: int = 2):
    swing_highs, swing_lows = [], []
    n = len(highs)
    for i in range(window, n - window):
        if highs[i] >= max(highs[i-window:i+window+1]):
            swing_highs.append((i, highs[i]))
        if lows[i] <= min(lows[i-window:i+window+1]):
            swing_lows.append((i, lows[i]))
    return swing_highs, swing_lows


def _line_from_points(p1, p2):
    x1, y1 = p1
    x2, y2 = p2
    if x2 == x1:
        return None
    slope = (y2 - y1) / (x2 - x1)
    intercept = y1 - slope * x1
    return slope, intercept


def _line_value(line, x):
    if not line:
        return None
    slope, intercept = line
    return slope * x + intercept


def _cluster_level(values: list[float], tolerance: float = 0.0035) -> Optional[float]:
    if not values:
        return None
    values = sorted(values)
    clusters = []
    current = [values[0]]
    for value in values[1:]:
        center = sum(current) / len(current)
        if abs(value - center) / max(abs(center), 1e-12) <= tolerance:
            current.append(value)
        else:
            clusters.append(current)
            current = [value]
    clusters.append(current)
    best = max(clusters, key=len)
    return sum(best) / len(best)


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    if len(closes) < 2:
        return max((max(highs) - min(lows)) if highs and lows else 0.0, 1e-8)
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1]),
        ))
    if not trs:
        return 1e-8
    return sum(trs[-period:]) / min(period, len(trs))


def analyze_price_action(klines: list) -> PriceActionAnalysis:
    if not klines or len(klines) < 12:
        return PriceActionAnalysis(reason="Not enough candles for reliable price-action structure.")

    data = klines[-120:]
    highs = [float(k[2]) for k in data]
    lows = [float(k[3]) for k in data]
    closes = [float(k[4]) for k in data]
    n = len(data)
    swing_highs, swing_lows = _find_swing_points(highs, lows, 2)

    recent_highs = swing_highs[-4:]
    recent_lows = swing_lows[-4:]
    support = _cluster_level([p[1] for p in recent_lows]) or min(lows[-30:])
    resistance = _cluster_level([p[1] for p in recent_highs]) or max(highs[-30:])

    trend = "Neutral"
    if len(recent_highs) >= 2 and len(recent_lows) >= 2:
        hh = recent_highs[-1][1] > recent_highs[-2][1]
        hl = recent_lows[-1][1] > recent_lows[-2][1]
        lh = recent_highs[-1][1] < recent_highs[-2][1]
        ll = recent_lows[-1][1] < recent_lows[-2][1]
        if hh and hl:
            trend = "Bullish"
        elif lh and ll:
            trend = "Bearish"

    upper_line = lower_line = None
    trend_line = None
    trend_line_points = None
    channel_upper_points = None
    channel_lower_points = None
    channel_height = None

    if len(recent_highs) >= 2 and len(recent_lows) >= 2:
        upper_points = recent_highs[-2:]
        lower_points = recent_lows[-2:]
        upper = _line_from_points(*upper_points)
        lower = _line_from_points(*lower_points)
        if upper and lower:
            avg_price = max(sum(closes[-20:]) / min(20, len(closes)), 1e-12)
            slope_gap = abs(upper[0] - lower[0])
            slope_scale = max(abs(upper[0]), abs(lower[0]), avg_price * 0.00035)
            parallel = slope_gap <= max(slope_scale * 0.65, avg_price * 0.0009)
            # Make sure the upper boundary is actually above the lower boundary
            # across most of the visible range.
            upper_at_end = _line_value(upper, n - 1)
            lower_at_end = _line_value(lower, n - 1)
            if parallel and upper_at_end > lower_at_end:
                upper_line, lower_line = upper, lower
                channel_upper_points = tuple(upper_points)
                channel_lower_points = tuple(lower_points)
                channel_height = upper_at_end - lower_at_end

    if channel_height is None:
        if trend == "Bullish" and len(recent_lows) >= 2:
            trend_line_points = tuple(recent_lows[-2:])
            trend_line = _line_from_points(*trend_line_points)
        elif trend == "Bearish" and len(recent_highs) >= 2:
            trend_line_points = tuple(recent_highs[-2:])
            trend_line = _line_from_points(*trend_line_points)
        else:
            # A directional line can still be useful if the latest two swings
            # clearly point in the same direction.
            if len(recent_lows) >= 2 and recent_lows[-1][1] > recent_lows[-2][1]:
                trend_line_points = tuple(recent_lows[-2:])
                trend_line = _line_from_points(*trend_line_points)
            elif len(recent_highs) >= 2 and recent_highs[-1][1] < recent_highs[-2][1]:
                trend_line_points = tuple(recent_highs[-2:])
                trend_line = _line_from_points(*trend_line_points)

    # Rectangle only when price has recently compressed into a reasonably
    # defined range with repeated interaction. Do not draw it when a clear
    # parallel channel already explains the structure.
    rectangle_zone = None
    lookback = min(24, n)
    zone_highs = highs[-lookback:]
    zone_lows = lows[-lookback:]
    zone_top = max(zone_highs)
    zone_bottom = min(zone_lows)
    zone_mid = (zone_top + zone_bottom) / 2
    zone_width = zone_top - zone_bottom
    recent_range = max(closes[-12:]) - min(closes[-12:])
    compression = zone_width / max(abs(zone_mid), 1e-12)
    if channel_height is None and compression <= 0.055 and recent_range / max(abs(zone_mid), 1e-12) <= 0.045:
        top_touches = sum(1 for h in zone_highs if abs(h-zone_top) / max(abs(zone_top),1e-12) <= 0.006)
        bottom_touches = sum(1 for l in zone_lows if abs(l-zone_bottom) / max(abs(zone_bottom),1e-12) <= 0.006)
        if top_touches >= 2 and bottom_touches >= 2:
            rectangle_zone = (n - lookback, n - 1, zone_bottom, zone_top)
            support, resistance = zone_bottom, zone_top

    drawing_reasons = []
    if channel_upper_points and channel_lower_points:
        if trend == "Bullish":
            drawing_reasons.append("AI Channel: repeated higher highs and higher lows formed two reasonably parallel rising boundaries.")
        elif trend == "Bearish":
            drawing_reasons.append("AI Channel: repeated lower highs and lower lows formed two reasonably parallel falling boundaries.")
        else:
            drawing_reasons.append("AI Channel: repeated swing highs and lows formed two reasonably parallel price boundaries.")
    elif trend_line_points:
        if trend == "Bullish" or trend_line_points[-1][1] > trend_line_points[0][1]:
            drawing_reasons.append("AI Trend Line: it connects rising swing lows to track dynamic support and trend direction.")
        else:
            drawing_reasons.append("AI Trend Line: it connects falling swing highs to track dynamic resistance and trend direction.")
    if rectangle_zone:
        drawing_reasons.append("AI Rectangle: price repeatedly interacted with a defined range, so the area is treated as a zone rather than a single price level.")

    last_close = closes[-1]
    prev_close = closes[-2]
    atr = _atr(highs, lows, closes)
    trigger_up = _line_value(upper_line, n - 1) if upper_line else resistance
    trigger_down = _line_value(lower_line, n - 1) if lower_line else support

    breakout_status = "None"
    breakout_trigger = None
    breakout_index = None
    retest_status = "Not detected"
    invalidation = None
    projected_target = None

    # Detect a recent close-based breakout/breakdown. Wick-only penetration is
    # deliberately not treated as confirmation.
    recent_start = max(1, n - 10)
    for i in range(n - 1, recent_start - 1, -1):
        up_level = _line_value(upper_line, i) if upper_line else resistance
        down_level = _line_value(lower_line, i) if lower_line else support
        if up_level and closes[i] > up_level and closes[i-1] <= up_level:
            breakout_status = "Bullish Breakout"
            breakout_trigger = up_level
            breakout_index = i
            break
        if down_level and closes[i] < down_level and closes[i-1] >= down_level:
            breakout_status = "Bearish Breakdown"
            breakout_trigger = down_level
            breakout_index = i
            break

    # Current candle can be a fresh confirmation even if the exact crossing
    # candle is outside the recent search window.
    if breakout_status == "None":
        if trigger_up and last_close > trigger_up and prev_close <= trigger_up:
            breakout_status = "Bullish Breakout"
            breakout_trigger = trigger_up
            breakout_index = n - 1
        elif trigger_down and last_close < trigger_down and prev_close >= trigger_down:
            breakout_status = "Bearish Breakdown"
            breakout_trigger = trigger_down
            breakout_index = n - 1
        elif trigger_up and highs[-1] > trigger_up and last_close <= trigger_up:
            breakout_status = "Potential / False Breakout"
            breakout_trigger = trigger_up
            breakout_index = n - 1
        elif trigger_down and lows[-1] < trigger_down and last_close >= trigger_down:
            breakout_status = "Potential / False Breakdown"
            breakout_trigger = trigger_down
            breakout_index = n - 1

    if breakout_status in {"Bullish Breakout", "Bearish Breakdown"} and breakout_trigger is not None:
        # Look for a post-breakout return near the broken level.
        start = max(breakout_index + 1, 0)
        for i in range(start, n):
            touched = (
                abs(lows[i] - breakout_trigger) <= atr * 0.45
                or abs(highs[i] - breakout_trigger) <= atr * 0.45
            )
            if touched:
                if breakout_status == "Bullish Breakout" and closes[i] > breakout_trigger:
                    retest_status = "Retest held"
                    break
                if breakout_status == "Bearish Breakdown" and closes[i] < breakout_trigger:
                    retest_status = "Retest held"
                    break

        if channel_height and channel_height > 0:
            if breakout_status == "Bullish Breakout":
                projected_target = breakout_trigger + channel_height
            else:
                projected_target = breakout_trigger - channel_height
        else:
            if breakout_status == "Bullish Breakout":
                projected_target = breakout_trigger + max(atr * 2.0, breakout_trigger * 0.01)
            else:
                projected_target = breakout_trigger - max(atr * 2.0, breakout_trigger * 0.01)

        invalidation = (
            breakout_trigger - max(atr * 0.8, breakout_trigger * 0.002)
            if breakout_status == "Bullish Breakout"
            else breakout_trigger + max(atr * 0.8, breakout_trigger * 0.002)
        )

    if breakout_status == "None":
        if trigger_up and last_close < trigger_up:
            reason = f"Watch {trigger_up:.8g}: a candle-body close above it is required for bullish breakout confirmation."
        elif trigger_down and last_close > trigger_down:
            reason = f"Watch {trigger_down:.8g}: a candle-body close below it is required for bearish breakdown confirmation."
        else:
            reason = "No clean confirmed breakout/breakdown detected on the latest candles."
    elif "Bullish" in breakout_status:
        reason = "Price closed above the detected resistance/channel boundary. Follow-through and retest behavior should be monitored."
    elif "Bearish" in breakout_status:
        reason = "Price closed below the detected support/channel boundary. Follow-through and retest behavior should be monitored."
    else:
        reason = "Price pierced the boundary but did not close beyond it, so the move is not treated as a confirmed breakout/breakdown."

    if "Bullish Breakout" == breakout_status:
        drawing_reasons.append(f"Breakout: candle closed above {breakout_trigger:.8g}; this is the boundary that was being tested.")
    elif "Bearish Breakdown" == breakout_status:
        drawing_reasons.append(f"Breakdown: candle closed below {breakout_trigger:.8g}; this is the boundary that was being tested.")
    elif "Potential / False Breakout" == breakout_status:
        drawing_reasons.append(f"Breakout warning: price pierced {breakout_trigger:.8g} but did not close above it, so confirmation is absent.")
    elif "Potential / False Breakdown" == breakout_status:
        drawing_reasons.append(f"Breakdown warning: price pierced {breakout_trigger:.8g} but did not close below it, so confirmation is absent.")

    # Confidence is based on structural evidence, not a forecast probability.
    evidence = 0
    if len(recent_highs) >= 2:
        evidence += 1
    if len(recent_lows) >= 2:
        evidence += 1
    if channel_height:
        evidence += 2
    if rectangle_zone:
        evidence += 1
    if breakout_status != "None":
        evidence += 2
    confidence = "High" if evidence >= 5 else ("Medium" if evidence >= 3 else "Low")

    return PriceActionAnalysis(
        trend=trend,
        support=support,
        resistance=resistance,
        trend_line=trend_line,
        trend_line_points=trend_line_points,
        channel_upper=upper_line,
        channel_lower=lower_line,
        channel_upper_points=channel_upper_points,
        channel_lower_points=channel_lower_points,
        rectangle_zone=rectangle_zone,
        drawing_reasons=drawing_reasons,
        breakout_status=breakout_status,
        breakout_trigger=breakout_trigger,
        breakout_index=breakout_index,
        retest_status=retest_status,
        invalidation=invalidation,
        projected_target=projected_target,
        confidence=confidence,
        reason=reason,
    )


def build_price_action_context(analysis: PriceActionAnalysis, closes: list[float]) -> str:
    def fmt(v):
        return f"{v:.10g}" if v is not None else "None"

    lines = [
        "PRICE_ACTION_DRAWING_ENGINE:",
        f"trend={analysis.trend}",
        f"support={fmt(analysis.support)}",
        f"resistance={fmt(analysis.resistance)}",
        f"trend_line_detected={'yes' if analysis.trend_line else 'no'}",
        f"parallel_channel_detected={'yes' if analysis.channel_upper and analysis.channel_lower else 'no'}",
        f"rectangle_zone_detected={'yes' if analysis.rectangle_zone else 'no'}",
        f"breakout_status={analysis.breakout_status}",
        f"breakout_trigger={fmt(analysis.breakout_trigger)}",
        f"retest_status={analysis.retest_status}",
        f"invalidation={fmt(analysis.invalidation)}",
        f"projected_target={fmt(analysis.projected_target)}",
        f"structure_confidence={analysis.confidence}",
        f"engine_reason={analysis.reason}",
        "drawing_reasons=" + " | ".join(analysis.drawing_reasons),
        "IMPORTANT: A wick through a level is not a confirmed breakout/breakdown. "
        "Use candle-body close and follow-through/retest logic.",
        "Tool selection rule: Horizontal Line = specific level; Trend Line = directional swings; "
        "Parallel Channel = two reasonably parallel boundaries; Rectangle = a defined consolidation/zone. "
        "Do not draw unused tools.",
    ]
    return "\n".join(lines)


def build_trade_plan(analysis: PriceActionAnalysis, klines: list, side: str) -> dict:
    data = klines[-120:]
    highs = [float(k[2]) for k in data]
    lows = [float(k[3]) for k in data]
    closes = [float(k[4]) for k in data]
    current = closes[-1]
    atr = _atr(highs, lows, closes)
    side = side.lower()

    if side == "long":
        trigger = analysis.breakout_trigger or analysis.resistance
        if not trigger:
            return {"status": "No clear long trigger detected."}
        confirmed = analysis.breakout_status == "Bullish Breakout"
        entry = current if confirmed else trigger
        recent_low = min(lows[-8:])
        sl = min(recent_low, entry - atr * 1.0)
        risk = max(entry - sl, atr * 0.5)
        tp1 = entry + risk
        tp2 = entry + risk * 2
        tp3 = analysis.projected_target if analysis.projected_target and analysis.projected_target > entry else entry + risk * 3
        return {
            "status": "Confirmed" if confirmed else "Conditional",
            "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "invalidation": analysis.invalidation or sl,
            "rr": [(tp1-entry)/risk, (tp2-entry)/risk, (tp3-entry)/risk],
        }

    trigger = analysis.breakout_trigger or analysis.support
    if not trigger:
        return {"status": "No clear short trigger detected."}
    confirmed = analysis.breakout_status == "Bearish Breakdown"
    entry = current if confirmed else trigger
    recent_high = max(highs[-8:])
    sl = max(recent_high, entry + atr * 1.0)
    risk = max(sl - entry, atr * 0.5)
    tp1 = entry - risk
    tp2 = entry - risk * 2
    tp3 = analysis.projected_target if analysis.projected_target and analysis.projected_target < entry else entry - risk * 3
    return {
        "status": "Confirmed" if confirmed else "Conditional",
        "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "invalidation": analysis.invalidation or sl,
        "rr": [(entry-tp1)/risk, (entry-tp2)/risk, (entry-tp3)/risk],
    }


def format_trade_plan(plan: dict, side: str) -> str:
    if "entry" not in plan:
        return f"{side.upper()} SETUP: {plan.get('status', 'Unavailable')}"
    return (
        f"{side.upper()} SETUP ({plan['status']})\n"
        f"Entry: {plan['entry']:.10g}\n"
        f"Stop Loss: {plan['sl']:.10g}\n"
        f"TP1: {plan['tp1']:.10g} | R:R {plan['rr'][0]:.2f}\n"
        f"TP2: {plan['tp2']:.10g} | R:R {plan['rr'][1]:.2f}\n"
        f"TP3: {plan['tp3']:.10g} | R:R {plan['rr'][2]:.2f}\n"
        f"Invalidation: {plan['invalidation']:.10g}"
    )


def generate_market_chart(
    klines, coin_name, ta, snapshot, timeframe_label="1 Hour",
    price_action=None, trade_plan=None, trade_side=None
):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle as MplRectangle

    data = klines[-100:]
    opens = [float(k[1]) for k in data]
    highs = [float(k[2]) for k in data]
    lows = [float(k[3]) for k in data]
    closes = [float(k[4]) for k in data]
    volumes = [float(k[5]) for k in data]
    x = list(range(len(data)))

    fig, (ax, av) = plt.subplots(
        2, 1, figsize=(13, 9), gridspec_kw={"height_ratios": [4.5, 1]}, sharex=True
    )

    # Trading-style candles: green bullish, red bearish.
    for i, (op, hi, lo, cl) in enumerate(zip(opens, highs, lows, closes)):
        candle_color = "#16a34a" if cl >= op else "#dc2626"
        ax.vlines(i, lo, hi, color=candle_color, linewidth=1.25, zorder=2)
        body_height = max(abs(cl - op), max(abs(cl) * 0.00012, 1e-8))
        ax.add_patch(MplRectangle(
            (i - 0.34, min(op, cl)), 0.68, body_height,
            facecolor=candle_color, edgecolor=candle_color, linewidth=1.0, zorder=3
        ))
        av.bar(i, volumes[i], width=0.68, color=candle_color, alpha=0.65)

    def sma(values, period):
        return [
            sum(values[i-period+1:i+1]) / period if i >= period-1 else None
            for i in range(len(values))
        ]

    ax.plot(x, sma(closes, 20), label="SMA20", linewidth=2.0, color="#f59e0b")
    ax.plot(x, sma(closes, 50), label="SMA50", linewidth=2.0, color="#0ea5e9")

    # Existing support/resistance behavior is preserved.
    if ta:
        levels = [
            (ta.support, "Major Support", "#15803d", "-"),
            (ta.near_support, "Near Support", "#22c55e", "--"),
            (ta.near_resistance, "Near Resistance", "#f97316", "--"),
            (ta.resistance, "Major Resistance", "#dc2626", "-"),
        ]
        seen = set()
        for level, label, color, style in levels:
            if level is None:
                continue
            key = round(float(level), 8)
            if key in seen:
                continue
            seen.add(key)
            level = float(level)
            ax.axhline(level, color=color, linestyle=style, linewidth=2.8, alpha=0.98, zorder=5)
            ax.annotate(
                f"{label}  ${level:,.2f}",
                xy=(len(x)-1, level), xytext=(-8, 0), textcoords="offset points",
                ha="right", va="center", fontsize=9, fontweight="bold", color=color,
                bbox=dict(boxstyle="round,pad=0.25", facecolor="white", edgecolor=color, alpha=0.90),
                zorder=6,
            )

    # New autonomous price-action drawings.
    if price_action:
        # Draw only from the real swing anchors. The previous version projected
        # lines all the way back to x=0, which could create misleading empty
        # lines across the left side of the chart and hide candles.
        if price_action.trend_line:
            line = price_action.trend_line
            points = price_action.trend_line_points
            if points:
                start_x = max(0, int(points[0][0]))
                end_x = len(x) - 1
            else:
                start_x = 0
                end_x = len(x) - 1
            xs = [start_x, end_x]
            ys = [_line_value(line, xi) for xi in xs]
            ax.plot(xs, ys, color="#7c3aed", linewidth=2.4, linestyle="-",
                    label="AI Trend Line", zorder=7)

        # Parallel channel: draw from the first real swing anchor to the
        # current chart edge instead of projecting backward into empty space.
        if price_action.channel_upper and price_action.channel_lower:
            channel_specs = [
                (price_action.channel_upper, price_action.channel_upper_points, "AI Channel Resistance"),
                (price_action.channel_lower, price_action.channel_lower_points, "AI Channel Support"),
            ]
            for line, points, label in channel_specs:
                start_x = max(0, int(points[0][0])) if points else 0
                xs = [start_x, len(x) - 1]
                ys = [_line_value(line, xi) for xi in xs]
                ax.plot(xs, ys, color="#2563eb", linewidth=2.3,
                        linestyle="-", label=label, zorder=7)

        # Rectangle = zone/consolidation only, not a substitute for a line.
        if price_action.rectangle_zone:
            x1, x2, bottom, top = price_action.rectangle_zone
            x1 = max(0, x1)
            x2 = min(len(x)-1, x2)
            ax.add_patch(MplRectangle(
                (x1 - 0.5, bottom), max(x2 - x1 + 1, 1), top - bottom,
                facecolor="#f59e0b", edgecolor="#b45309", alpha=0.12,
                linewidth=2.0, linestyle="--", label="AI Zone", zorder=1
            ))

        # Breakout/breakdown trigger and the exact candle that crossed it.
        if price_action.breakout_trigger is not None:
            trigger = float(price_action.breakout_trigger)
            idx = price_action.breakout_index
            if idx is not None and 0 <= idx < len(x):
                if "Bullish" in price_action.breakout_status:
                    marker_y = highs[idx]
                    marker = "^"
                    marker_color = "#16a34a"
                elif "Bearish" in price_action.breakout_status:
                    marker_y = lows[idx]
                    marker = "v"
                    marker_color = "#dc2626"
                else:
                    marker_y = closes[idx]
                    marker = "o"
                    marker_color = "#f59e0b"
                ax.scatter([idx], [marker_y], s=90, marker=marker,
                           color=marker_color, edgecolor="white", linewidth=1.2,
                           zorder=10)


        # Trade-plan levels are added only when the user explicitly asks for
        # a Long/Short plan.
        if trade_plan and "entry" in trade_plan:
            plan_levels = [
                (trade_plan["entry"], "Entry", "#2563eb"),
                (trade_plan["sl"], "Stop Loss", "#dc2626"),
                (trade_plan["tp1"], "TP1", "#16a34a"),
                (trade_plan["tp2"], "TP2", "#16a34a"),
                (trade_plan["tp3"], "TP3", "#16a34a"),
            ]
            for level, label, color in plan_levels:
                ax.axhline(level, color=color, linestyle="--", linewidth=1.8, alpha=0.85, zorder=6)
                ax.annotate(
                    f"{label} ${level:,.4f}",
                    xy=(len(x)-1, level), xytext=(-8, 0), textcoords="offset points",
                    ha="right", va="center", fontsize=8.5, fontweight="bold", color=color,
                    bbox=dict(boxstyle="round,pad=0.22", facecolor="white",
                              edgecolor=color, alpha=0.88), zorder=9
                )


    if snapshot:
        price = float(snapshot.price)
        ax.axhline(price, color="#2563eb", linestyle=":", linewidth=2.6, alpha=0.98, zorder=5)
        ax.annotate(
            f"Current Price  ${price:,.2f}",
            xy=(len(x)-1, price), xytext=(-8, 15), textcoords="offset points",
            ha="right", va="bottom", fontsize=9, fontweight="bold", color="#2563eb",
            bbox=dict(boxstyle="round,pad=0.25", facecolor="white", edgecolor="#2563eb", alpha=0.90),
            zorder=6,
        )

    symbol_text = snapshot.symbol if snapshot else coin_name
    title_extra = ""
    if price_action:
        title_extra = f" | {price_action.trend} | {price_action.breakout_status}"
    ax.set_title(
        f"{coin_name} ({symbol_text}) — {timeframe_label} Binance Chart{title_extra}",
        fontsize=15, fontweight="bold"
    )
    ax.set_ylabel("Price (USDT)")
    ax.grid(alpha=0.18)
    av.set_ylabel("Volume")
    av.grid(alpha=0.10)
    ax.legend(loc="upper left", frameon=True, fontsize=8)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=210, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


async def transcribe_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    voice = update.message.voice if update.message else None
    if not voice:
        return ""

    tg_file = await context.bot.get_file(voice.file_id)
    audio_bytes = bytes(await tg_file.download_as_bytearray())

    def _transcribe():
        response = groq_client.audio.transcriptions.create(
            file=("telegram_voice.ogg", audio_bytes),
            model="whisper-large-v3",
            prompt=(
                "Primary language is Bangla/Bengali from Bangladesh. Accurately transcribe natural "
                "Bangladeshi Bengali speech, colloquial wording, fast speech, imperfect grammar, "
                "Banglish code-switching and common pronunciation variations. Use surrounding meaning "
                "to preserve intended crypto terminology and coin names such as Bitcoin, Ethereum, BTC, "
                "ETH, BNB, SOL, XRP, MUSE, Binance, TOTAL, TOTAL2, TOTAL3, BTC.D, ETH.D, market cap, "
                "dominance, candle, timeframe, support, resistance, RSI, SMA, price, chart and trading. "
                "Do not translate. Return what the speaker meant in Bengali text while keeping standard "
                "crypto tickers/names recognizable."
            ),
            response_format="json",
            language="bn",
            temperature=0.0,
        )
        return (response.text or "").strip()

    return await asyncio.to_thread(_transcribe)


async def process_text_query(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if not text or not await require_approved(update):
        return

    uid = update.effective_user.id
    text = text.strip()
    lang = detect_language(text)

    if not rate_limiter.allow(uid):
        await update.message.reply_text(ui_text("rate_limited", lang if lang in {"en","bn","hi"} else "en"))
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    resolved = extract_symbol(text)
    if not resolved and uid in LAST_MARKET_CONTEXT:
        # Natural follow-up: "এইটা চার্ট দাও", "support resistance দেখাও" etc.
        resolved = LAST_MARKET_CONTEXT[uid].get("resolved")
    if not resolved:
        # Restore the last requested market from SQLite after restart, so follow-ups
        # such as "এটার চার্ট দাও" do not lose the previous coin context.
        saved_market = db_get_market_context(uid)
        if saved_market and saved_market["symbol"]:
            resolved = (saved_market["symbol"], saved_market["coin_name"] or saved_market["symbol"])
            candle_interval = saved_market["interval"] or candle_interval
            candle_limit = int(saved_market["limit_count"] or candle_limit)
            timeframe_label = saved_market["timeframe_label"] or timeframe_label

    snapshot = None
    ta = None
    coin_name = None
    klines = None
    candle_interval, candle_limit, timeframe_label = resolve_timeframe(text)

    if resolved:
        symbol, coin_name = resolved
        try:
            snapshot = await get_market_snapshot(symbol)
            klines = await get_timeframe_klines(symbol, candle_interval, candle_limit)
            ta = compute_technical_summary(klines)
            LAST_MARKET_CONTEXT[uid] = {
                "resolved": resolved,
                "snapshot": snapshot,
                "ta": ta,
                "klines": klines,
            }
            db_save_market_context(uid, resolved, candle_interval, candle_limit, timeframe_label)
        except BinanceError as exc:
            logger.warning("Binance lookup failed for %s: %s", symbol, exc)

    normalized = normalize_user_text(text)
    chart_words = (
        "chart", "graph", "image", "photo", "চার্ট", "গ্রাফ", "ছবি", "ফটো",
        "ইমেজ", "ক্যান্ডেল", "সাপোর্ট রেজিস্ট্যান্স", "support resistance",
        "chart pattern", "চার্ট প্যাটার্ন", "চার্ট বানাও", "চার্ট দাও",
        "ছবি বানাও", "ফটো বানাও", "edit chart", "চার্ট এডিট",
    )
    wants_chart = any(w in normalized for w in chart_words)

    # Build autonomous price-action drawings whenever candle data is available.
    price_action = None
    requested_trade_side = None
    if klines:
        try:
            price_action = analyze_price_action(klines)
        except Exception:
            logger.exception("Price-action analysis failed")

    lowered = normalize_user_text(text)
    if any(term in lowered for term in (
        "long position", "long setup", "go long", "long trade", "লং পজিশন",
        "লং সেটআপ", "লং ট্রেড", "লং সাজাও", "লং দাও"
    )):
        requested_trade_side = "long"
    elif any(term in lowered for term in (
        "short position", "short setup", "go short", "short trade", "শর্ট পজিশন",
        "শর্ট সেটআপ", "শর্ট ট্রেড", "শর্ট সাজাও", "শর্ট দাও"
    )):
        requested_trade_side = "short"

    trade_plan = None
    if requested_trade_side and klines and price_action:
        try:
            trade_plan = build_trade_plan(price_action, klines, requested_trade_side)
        except Exception:
            logger.exception("Trade-plan calculation failed")

    context_block = build_context_block(text, lang, coin_name, snapshot, ta)
    previous_history = db_get_chat_history(uid, limit=8)
    if previous_history:
        history_lines = ["PREVIOUS_CONVERSATION_CONTEXT:"]
        for item in previous_history:
            history_lines.append(f"{item['role'].upper()}: {item['content']}")
        context_block += "\n" + "\n".join(history_lines)
        context_block += (
            "\nUse this previous conversation only to resolve references such as 'this coin', "
            "'the previous chart', 'that breakout', or follow-up questions. "
            "For current prices/market data, always use the newly fetched live data.\n"
        )
    if klines:
        closes_for_context = [float(k[4]) for k in klines[-120:]]
        context_block += "\n" + build_price_action_context(price_action, closes_for_context)
        context_block += (
            f"\nCANDLE_TIMEFRAME: {timeframe_label} ({candle_interval}); "
            f"CANDLE_COUNT: {len(klines)}; "
            "OHLCV candles were fetched from Binance for this requested timeframe. "
            "Do not say candle/OHLC data is unavailable when this line is present."
        )

    if trade_plan:
        context_block += "\nREQUESTED_TRADE_PLAN:\n" + format_trade_plan(trade_plan, requested_trade_side)
    if klines:
        context_block += (
            f"\nCANDLE_TIMEFRAME: {timeframe_label} ({candle_interval}); "
            f"CANDLE_COUNT: {len(klines)}; "
            "OHLCV candles were fetched from Binance for this requested timeframe. "
            "Do not say candle/OHLC data is unavailable when this line is present."
        )

    # Supply global market context when the user asks about TOTAL/TOTAL2/TOTAL3,
    # dominance, total market cap, or the overall crypto market.
    global_terms = (
        "total", "total2", "total 2", "total3", "total 3", "btc.d", "eth.d",
        "dominance", "ডমিনেন্স", "মার্কেট ক্যাপ", "global market", "গ্লোবাল মার্কেট",
        "ক্রিপ্টো মার্কেট", "crypto market", "overall market",
    )
    if any(term in normalized for term in global_terms):
        try:
            gm = await get_global_market_context()
            context_block += (
                "\nGLOBAL_MARKET_DATA: "
                f"total_market_cap={gm['total_market_cap_usd']} USD; "
                f"TOTAL2_approx={gm['total2_market_cap_usd']} USD; "
                f"TOTAL3_approx={gm['total3_market_cap_usd']} USD; "
                f"BTC_dominance={gm['btc_dominance']}%; "
                f"ETH_dominance={gm['eth_dominance']}%; "
                f"24h_global_market_cap_change={gm['market_cap_change_24h_pct']}%. "
                "TOTAL2/TOTAL3 are calculated from total market cap and dominance, "
                "so label them as approximate when reporting them."
            )
        except Exception:
            logger.exception("Global market context lookup failed")

    news_terms = (
        "news", "latest", "today", "আজকের", "আজ", "নিউজ", "খবর", "আপডেট",
        "breaking", "সর্বশেষ", "কি হলো", "কি হইছে", "what happened"
    )
    if any(term in normalized for term in news_terms):
        try:
            fresh_items = await fetch_rss_news(12)
            if fresh_items:
                headline_lines = []
                for item in fresh_items[:12]:
                    headline_lines.append(
                        f"- {item.get('title','')} | {item.get('source',{}).get('title','')} | "
                        f"{item.get('published_at','')} | {item.get('url','')}"
                    )
                context_block += (
                    "\nFRESH_CRYPTO_NEWS: These headlines were fetched live for this request.\n"
                    + "\n".join(headline_lines)
                )
        except Exception:
            logger.exception("Fresh news context lookup failed")

    # For chart requests, send the annotated chart first, then generate a
    # separate text explanation from the exact same structural analysis.
    if wants_chart and resolved and klines:
        try:
            chart_buf = await asyncio.to_thread(
                generate_market_chart,
                klines, coin_name, ta, snapshot, timeframe_label,
                price_action, trade_plan, requested_trade_side
            )
            await update.message.reply_photo(
                photo=InputFile(chart_buf, filename="market_chart.png"),
            )
            if price_action and price_action.drawing_reasons:
                explanation_lines = ["📌 AI Chart Drawing Explanation"]
                explanation_lines.extend(
                    f"• {reason}" for reason in price_action.drawing_reasons[:6]
                )
                if price_action.breakout_status:
                    explanation_lines.append(
                        f"• Current structure/status: {price_action.breakout_status}"
                    )
                # Use the same safe chunking system as normal AI replies so
                # Telegram never truncates a long explanation.
                await reply_long(update, "\n".join(explanation_lines))
        except Exception:
            logger.exception("Chart generation failed")

    # Explicitly tell the model what output language/script to use.
    context_block += (
        f"\nOUTPUT_LANGUAGE_REQUIREMENT: {LANGUAGE_NAMES.get(lang, 'English')}. "
        "If the user wrote Banglish/Romanized Bengali, output natural Bengali script. "
        "If the user wrote Hinglish/Romanized Hindi, output natural Hindi script. "
        "Do not output Banglish/Hinglish unless explicitly requested."
    )

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        language_name=LANGUAGE_NAMES.get(lang, "English")
    )
    db_save_chat_message(uid, "user", text)
    try:
        reply = await ask_groq(system_prompt, context_block)
    except Exception:
        logger.exception("Groq API call failed")
        await update.message.reply_text(
            "AI service is temporarily unavailable. Please try again shortly."
        )
        return

    final_reply = reply or "No response generated."
    db_save_chat_message(uid, "assistant", final_reply)
    await reply_long(update, final_reply)


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    await process_text_query(update, context, update.message.text)


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not await require_approved(update):
        return
    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action=ChatAction.TYPING
        )
        transcript = await transcribe_voice_message(update, context)
        if not transcript:
            await update.message.reply_text(
                "ভয়েসটি পরিষ্কারভাবে বুঝতে পারিনি। আবার একটু পরিষ্কার করে বলুন।"
            )
            return
        await process_text_query(update, context, transcript)
    except Exception:
        logger.exception("Voice transcription failed")
        await update.message.reply_text(
            "ভয়েসটি প্রসেস করতে সমস্যা হয়েছে। আবার চেষ্টা করুন।"
        )


# ---- Admin handlers ----
async def admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("👑 Admin Panel", reply_markup=admin_keyboard())


async def users_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    rows = db_list_all()
    if not rows:
        await update.message.reply_text("No users yet.")
        return
    text = "\n".join(format_user_row(r) for r in rows)
    await reply_long(update, text)


async def pending_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    rows = db_list_by_status("pending")
    if not rows:
        await update.message.reply_text("No pending requests.")
        return
    text = "\n".join(format_user_row(r) for r in rows)
    await reply_long(update, text)


async def approve_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /approve <telegram_id>")
        return
    target_id = int(context.args[0])
    if db_set_status(target_id, "approved"):
        await update.message.reply_text(f"✅ Approved {target_id}.")
        try:
            await context.bot.send_message(
                chat_id=target_id, text="✅ Your access has been approved. Send /start to begin."
            )
        except Exception:
            logger.exception("Could not notify approved user %s", target_id)
    else:
        await update.message.reply_text("User not found.")


async def reject_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /reject <telegram_id>")
        return
    target_id = int(context.args[0])
    if db_set_status(target_id, "rejected"):
        await update.message.reply_text(f"🚫 Rejected {target_id}.")
    else:
        await update.message.reply_text("User not found.")


async def approved_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    rows=db_list_by_status("approved",1000)+db_list_by_status("blocked",1000)
    if not rows:
        await update.message.reply_text("No approved users.")
        return
    for r in rows:
        markup=approved_user_keyboard(r["telegram_id"]) if r["status"]=="approved" else blocked_user_keyboard(r["telegram_id"])
        await update.message.reply_text(format_admin_user_card(r),reply_markup=markup)


async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id): return
    rows=db_list_all(100000); c={x:sum(1 for r in rows if r["status"]==x) for x in ("approved","pending","blocked","rejected")}
    await update.message.reply_text(f"📊 Bot Stats\nApproved: {c['approved']}\nPending: {c['pending']}\nBlocked: {c['blocked']}\nRejected: {c['rejected']}")

# ==================================================
# ERROR HANDLING
# ==================================================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception while processing update", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "An unexpected error occurred. The issue has been logged."
            )
    except Exception:
        logger.exception("Failed to send error message to user")



# ==================================================
# STAGE 2 — FREE RSS CRYPTO NEWS / GLOBAL MARKET / BROADCAST / GROUPS
# ==================================================

COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"
COINGECKO_API_KEY = _env("COINGECKO_API_KEY", "")
BROADCAST_BATCH_SIZE = int(_env("BROADCAST_BATCH_SIZE", "20"))

async def _generic_get(url: str, params: dict | None = None, headers: dict | None = None):
    try:
        async with httpx.AsyncClient(timeout=15, headers=headers or {}) as client:
            resp = await client.get(url, params=params or {})
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"HTTP request failed: {exc}") from exc


async def coingecko_global() -> dict:
    headers = {}
    if COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_API_KEY
    return await _generic_get(f"{COINGECKO_BASE_URL}/global", headers=headers)


async def coingecko_global_markets() -> dict:
    """Return global market data plus dominance information."""
    data = await coingecko_global()
    market = data.get("data", {})
    total_market_cap = market.get("total_market_cap", {})
    total_volume = market.get("total_volume", {})
    market_cap_percentage = market.get("market_cap_percentage", {})
    return {
        "total_market_cap_usd": total_market_cap.get("usd"),
        "total_volume_usd": total_volume.get("usd"),
        "btc_dominance": market_cap_percentage.get("btc"),
        "eth_dominance": market_cap_percentage.get("eth"),
        "active_cryptocurrencies": market.get("active_cryptocurrencies"),
        "markets": market.get("markets"),
        "market_cap_change_24h_pct": market.get("market_cap_change_percentage_24h_usd"),
    }


async def get_global_market_context() -> dict:
    """Build live global metrics including TOTAL/TOTAL2/TOTAL3 approximations."""
    m = await coingecko_global_markets()
    total = float(m.get("total_market_cap_usd") or 0)
    btc_d = float(m.get("btc_dominance") or 0)
    eth_d = float(m.get("eth_dominance") or 0)
    return {
        **m,
        "total_market_cap_usd": total,
        "total2_market_cap_usd": total * max(0.0, 1.0 - btc_d / 100.0),
        "total3_market_cap_usd": total * max(0.0, 1.0 - (btc_d + eth_d) / 100.0),
    }


async def fetch_rss_news(limit: int = 10) -> list[dict]:
    """Fetch crypto news from public RSS feeds; no API key required."""
    feeds = [
        ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("Cointelegraph", "https://cointelegraph.com/rss"),
    ]

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        results = []
        for source_name, url in feeds:
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    logger.warning("RSS feed %s returned HTTP %s", source_name, resp.status_code)
                    continue

                import xml.etree.ElementTree as ET
                root = ET.fromstring(resp.text)

                for item in root.findall(".//item"):
                    title = (item.findtext("title") or "").strip()
                    link = (item.findtext("link") or "").strip()
                    pub_date = (item.findtext("pubDate") or "").strip()
                    if title and link:
                        results.append({
                            "title": title,
                            "url": link,
                            "source": {"title": source_name},
                            "published_at": pub_date,
                        })
            except Exception:
                logger.exception("RSS lookup failed for %s", source_name)

    # Deduplicate by URL/title and keep the feed order.
    seen = set()
    unique = []
    for item in results:
        key = item.get("url") or item.get("title")
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    return unique[:limit]


def _format_usd(value) -> str:
    if value is None:
        return "N/A"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if value >= 1_000_000_000_000:
        return f"${value/1_000_000_000_000:.2f}T"
    if value >= 1_000_000_000:
        return f"${value/1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value/1_000_000:.2f}M"
    return f"${value:,.0f}"


def _format_news_items(items: list[dict], lang: str) -> str:
    if not items:
        return "No recent crypto news found."

    lines = ["📰 Crypto News"]
    for i, item in enumerate(items[:10], 1):
        title = item.get("title") or "Untitled"
        url = item.get("url") or item.get("original_url") or ""
        source = item.get("source")
        source_name = source.get("title") if isinstance(source, dict) else ""
        suffix = f" — {source_name}" if source_name else ""
        lines.append(f"{i}. {title}{suffix}")
        if url:
            lines.append(url)
    return "\n".join(lines)


async def send_broadcast(context: ContextTypes.DEFAULT_TYPE, text: str) -> tuple[int, int]:
    """Broadcast only to approved users; admins are included if approved."""
    rows = db_list_all(limit=100000)
    approved = [r for r in rows if r["status"] == "approved"]
    sent = failed = 0

    for start in range(0, len(approved), BROADCAST_BATCH_SIZE):
        batch = approved[start:start + BROADCAST_BATCH_SIZE]
        for row in batch:
            try:
                await context.bot.send_message(chat_id=row["telegram_id"], text=text)
                sent += 1
            except Exception:
                failed += 1
                logger.exception("Broadcast failed for %s", row["telegram_id"])
        if start + BROADCAST_BATCH_SIZE < len(approved):
            await asyncio.sleep(1)

    return sent, failed


async def news_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_approved(update):
        return
    try:
        items = await fetch_rss_news(10)
        await reply_long(
            update,
            _format_news_items(items, detect_language(update.message.text or "")),
        )
    except Exception:
        logger.exception("RSS news lookup failed")
        await update.message.reply_text(
            "Crypto news is temporarily unavailable. Please try again shortly."
        )


async def market_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_approved(update):
        return
    try:
        m = await coingecko_global_markets()
        gm = await get_global_market_context()
        text = (
            "🌍 Global Crypto Market\n"
            f"Total Market Cap: {_format_usd(gm['total_market_cap_usd'])}\n"
            f"TOTAL2 (approx): {_format_usd(gm['total2_market_cap_usd'])}\n"
            f"TOTAL3 (approx): {_format_usd(gm['total3_market_cap_usd'])}\n"
            f"24h Market Cap Change: {gm['market_cap_change_24h_pct']:.2f}%\n"
            f"24h Volume: {_format_usd(gm['total_volume_usd'])}\n"
            f"BTC Dominance: {gm['btc_dominance']:.2f}%\n"
            f"ETH Dominance: {gm['eth_dominance']:.2f}%\n"
            f"Active Cryptocurrencies: {gm['active_cryptocurrencies']:,}\n"
            f"Markets: {gm['markets']:,}"
        )
        await update.message.reply_text(text)
    except Exception:
        logger.exception("CoinGecko global lookup failed")
        await update.message.reply_text("Global market data is temporarily unavailable.")


async def broadcast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast Your message here")
        return

    text = " ".join(context.args).strip()
    sent, failed = await send_broadcast(context, text)
    await update.message.reply_text(
        f"📢 Broadcast complete.\nSent: {sent}\nFailed: {failed}"
    )


def is_group_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type in {"group", "supergroup"})


# ==================================================
# MAIN
# ==================================================
async def configure_bot_commands(app: Application) -> None:
    await app.bot.set_my_commands([BotCommand("start","Start")],scope=BotCommandScopeDefault())
    for admin_id in ADMIN_TELEGRAM_IDS:
        await app.bot.set_my_commands([BotCommand("start","Start"),BotCommand("admin","Admin Panel"),BotCommand("approved","Approved Users"),BotCommand("pending","Pending Requests"),BotCommand("broadcast","Broadcast"),BotCommand("stats","Bot Stats")],scope=BotCommandScopeChat(chat_id=admin_id))

    startup_text = (
        "This AI provides live crypto market and price analysis, multi-timeframe candle analysis, support and resistance detection, trendline, channel and rectangle zone analysis, breakout, breakdown and retest analysis, along with chart annotation and clear market-structure explanations. The bot is now ready to receive your request."
        "\n\n"
        f"{owner_signature_html()}"
    )
    for admin_id in ADMIN_TELEGRAM_IDS:
        try:
            await app.bot.send_message(
                chat_id=admin_id,
                text=startup_text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception("Failed to send startup notification to admin %s", admin_id)

def build_application() -> Application:
    app=ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(configure_bot_commands).build()
    for name,fn in [("start",start_handler),("help",help_handler),("id",id_handler),("status",status_handler),("price",price_handler),("admin",admin_handler),("users",users_handler),("approved",approved_handler),("pending",pending_handler),("stats",stats_handler),("approve",approve_handler),("reject",reject_handler),("news",news_handler),("market",market_handler),("broadcast",broadcast_handler)]: app.add_handler(CommandHandler(name,fn))
    app.add_handler(CallbackQueryHandler(admin_callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,message_handler)); app.add_handler(MessageHandler(filters.VOICE,voice_handler)); app.add_error_handler(error_handler); return app


def main() -> None:
    db_init()
    logger.info("Starting Crypto AI Telegram Assistant (model=%s)", GROQ_MODEL)
    app = build_application()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        logger.error("Startup failed: %s", exc)
        sys.exit(1)
