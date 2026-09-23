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
    "btc": ("BTCUSDT", "Bitcoin"), "bitcoin": ("BTCUSDT", "Bitcoin"), "বিটকয়েন": ("BTCUSDT", "Bitcoin"), "বিটকয়েন": ("BTCUSDT", "Bitcoin"),
    "eth": ("ETHUSDT", "Ethereum"), "ethereum": ("ETHUSDT", "Ethereum"), "ইথেরিয়াম": ("ETHUSDT", "Ethereum"), "ইথেরিয়াম": ("ETHUSDT", "Ethereum"), "ইথারিয়াম": ("ETHUSDT", "Ethereum"), "ইথারিয়াম": ("ETHUSDT", "Ethereum"), "ইথার": ("ETHUSDT", "Ethereum"), "থুরিয়াম": ("ETHUSDT", "Ethereum"), "থুরিয়াম": ("ETHUSDT", "Ethereum"),
    "bnb": ("BNBUSDT", "BNB"), "বিএনবি": ("BNBUSDT", "BNB"), "sol": ("SOLUSDT", "Solana"), "solana": ("SOLUSDT", "Solana"), "সোলানা": ("SOLUSDT", "Solana"),
    "xrp": ("XRPUSDT", "XRP"), "ripple": ("XRPUSDT", "XRP"), "রিপল": ("XRPUSDT", "XRP"), "ada": ("ADAUSDT", "Cardano"), "cardano": ("ADAUSDT", "Cardano"), "কার্ডানো": ("ADAUSDT", "Cardano"),
    "doge": ("DOGEUSDT", "Dogecoin"), "dogecoin": ("DOGEUSDT", "Dogecoin"), "ডজকয়েন": ("DOGEUSDT", "Dogecoin"), "ডজকয়েন": ("DOGEUSDT", "Dogecoin"), "ton": ("TONUSDT", "Toncoin"), "টন": ("TONUSDT", "Toncoin"),
    "trx": ("TRXUSDT", "TRON"), "ট্রন": ("TRXUSDT", "TRON"), "dot": ("DOTUSDT", "Polkadot"), "পোলকাডট": ("DOTUSDT", "Polkadot"), "matic": ("MATICUSDT", "Polygon"), "polygon": ("MATICUSDT", "Polygon"),
    "avax": ("AVAXUSDT", "Avalanche"), "ltc": ("LTCUSDT", "Litecoin"), "লাইটকয়েন": ("LTCUSDT", "Litecoin"), "লাইটকয়েন": ("LTCUSDT", "Litecoin"), "link": ("LINKUSDT", "Chainlink"), "চেইনলিংক": ("LINKUSDT", "Chainlink"),
    "shib": ("SHIBUSDT", "Shiba Inu"), "শিবা": ("SHIBUSDT", "Shiba Inu"), "sui": ("SUIUSDT", "Sui"), "সুই": ("SUIUSDT", "Sui"),
}
CRYPTO_KEYWORDS = ["crypto","cryptocurrency","bitcoin","btc","ethereum","eth","altcoin","blockchain","defi","web3","nft","token","coin","wallet","mining","staking","exchange","binance","futures","spot trading","market cap","dominance","bull","bear","pump","dump","airdrop","tokenomics","ক্রিপ্টো","ক্রিপ্টোকারেন্সি","বিটকয়েন","বিটকয়েন","ইথেরিয়াম","ইথেরিয়াম","ইথারিয়াম","ইথারিয়াম","ইথার","থুরিয়াম","থুরিয়াম","ব্লকচেইন","কয়েন","কয়েন","বিএনবি","সোলানা","রিপল","ডজকয়েন","ডজকয়েন","দাম","প্রাইস","মূল্য","কত","লাইভ","क्रिप्टो","बिटकॉइन","इथेरियम","एथेरियम","कीमत"]

def normalize_user_text(text: str) -> str:
    text=unicodedata.normalize("NFKC", text or "").strip().lower()
    for a,b in {"ইথেরিয়াম":"ইথেরিয়াম","ইথারিয়াম":"ইথারিয়াম","বিটকয়েন":"বিটকয়েন","ডজকয়েন":"ডজকয়েন","লাইটকয়েন":"লাইটকয়েন","কয়েন":"কয়েন","থুরিয়াম":"থুরিয়াম"}.items(): text=text.replace(a,b)
    return text

def is_crypto_query(text: str) -> bool:
    t=normalize_user_text(text)
    return any(a in t for a in COIN_ALIASES) or any(k in t for k in CRYPTO_KEYWORDS)

def extract_symbol(text: str) -> Optional[tuple[str,str]]:
    t=normalize_user_text(text)
    for a in sorted(COIN_ALIASES,key=len,reverse=True):
        if a in t: return COIN_ALIASES[a]
    m=re.search(r"(?<![A-Za-z])([A-Za-z]{2,10})(?![A-Za-z])",text)
    if m and m.group(1).lower() not in {"the","and","for","with","price","live","chart"}:
        c=m.group(1).upper(); return f"{c}USDT",c
    return None

def detect_language(text: str) -> str:
    if re.search(r"[\u0980-\u09FF]",text): return "bn"
    if re.search(r"[\u0900-\u097F]",text): return "hi"
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

SYSTEM_PROMPT_TEMPLATE = """You are a highly capable general conversational AI assistant with strong specialization in cryptocurrency and markets.

RULES:
1. Understand Bengali, Banglish, Hindi, English, mixed languages, spelling variants, typos, and short phrases naturally.
2. Answer only the exact question asked. Do not add unrelated sections, news, lectures, or extra questions.
3. Crypto is your strongest domain. For crypto questions, use the live Binance/technical context below when available.
4. For live price, percentage, high/low, volume, RSI, SMA, support, or resistance, use ONLY supplied data. Never invent numbers.
5. If live data is unavailable, say so briefly.
6. You may answer normal general-knowledge questions too; do not reject them just because they are not crypto keywords.
7. Reply in the user's language.
8. Keep answers concise and focused.
9. Plain text only: no Markdown stars, backticks, headings with #, or decorative formatting.
10. If the user asks for a chart/image, the bot generates it separately; never claim an image exists unless one is actually sent.
11. Market direction is analysis/scenario, not certainty or personalized investment advice.
"""

LANGUAGE_NAMES = {"en": "English", "bn": "Bengali (বাংলা)", "hi": "Hindi (हिन्दी)"}


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

async def notify_admins(context,text,reply_markup=None):
    for admin_id in ADMIN_TELEGRAM_IDS:
        try: await context.bot.send_message(chat_id=admin_id,text=text,reply_markup=reply_markup)
        except Exception: logger.exception("Failed to notify admin %s",admin_id)

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
    if row["status"]=="approved": await update.message.reply_text(ui_text("welcome",lang))
    elif row["status"]=="pending":
        await update.message.reply_text(ui_text("pending",lang)); await notify_admins(context,f"🆕 New access request:\n{format_user_row(row)}\n\nApprove or reject:",pending_request_keyboard(row["telegram_id"]))
    elif row["status"]=="blocked": await update.message.reply_text(ui_text("blocked",lang))
    else: await update.message.reply_text(ui_text("rejected",lang))


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


def generate_market_chart(klines,coin_name,ta,snapshot):
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    data=klines[-60:]; o=[float(k[1]) for k in data]; h=[float(k[2]) for k in data]; l=[float(k[3]) for k in data]; c=[float(k[4]) for k in data]; v=[float(k[5]) for k in data]; x=list(range(len(data)))
    fig,(ax,av)=plt.subplots(2,1,figsize=(11,7.5),gridspec_kw={"height_ratios":[4,1]},sharex=True)
    fig.patch.set_facecolor("#071421"); ax.set_facecolor("#071421"); av.set_facecolor("#071421")
    for i,(op,hi,lo,cl) in enumerate(zip(o,h,l,c)):
        edge="#00d9a5" if cl>=op else "#ff4d5a"; ax.vlines(i,lo,hi,color=edge,linewidth=1); ax.add_patch(Rectangle((i-.31,min(op,cl)),.62,max(abs(cl-op),1e-9),facecolor=edge,edgecolor=edge)); av.bar(i,v[i],color=edge,width=.62,alpha=.7)
    def sma(vals,n): return [sum(vals[i-n+1:i+1])/n if i>=n-1 else None for i in range(len(vals))]
    ax.plot(x,sma(c,20),label="SMA20",linewidth=1.7); ax.plot(x,sma(c,50),label="SMA50",linewidth=1.7)
    if ta:
        for level,label in [(ta.support,"Support 1"),(ta.near_support,"Support 2"),(ta.near_resistance,"Resistance 1"),(ta.resistance,"Resistance 2")]:
            if level is not None: ax.axhline(level,linestyle="--",linewidth=1.1,alpha=.75); ax.text(len(x)-1.5,level,f" {label} ${level:,.2f}",ha="right",va="bottom",fontsize=8)
    if snapshot: ax.axhline(snapshot.price,linestyle=":",linewidth=1.1); ax.text(len(x)-1.5,snapshot.price,f" Current ${snapshot.price:,.2f}",ha="right",va="top",fontsize=8)
    ax.set_title(f"{coin_name} ({'ETH' if coin_name=='Ethereum' else coin_name}/USDT) — 1 Hour Chart (Binance)",color="white",fontsize=14); ax.legend(loc="upper left",frameon=False); ax.grid(alpha=.12); av.grid(alpha=.08)
    ax.tick_params(colors="white"); av.tick_params(colors="white"); fig.tight_layout(); buf=io.BytesIO(); fig.savefig(buf,format="png",dpi=160,bbox_inches="tight",facecolor=fig.get_facecolor()); plt.close(fig); buf.seek(0); return buf

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text or not await require_approved(update): return
    uid=update.effective_user.id; text=update.message.text.strip(); lang=detect_language(text)
    if not rate_limiter.allow(uid): await update.message.reply_text(ui_text("rate_limited",lang)); return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id,action=ChatAction.TYPING)
    resolved=extract_symbol(text); snapshot=None; ta=None; coin_name=None; klines=None
    if resolved:
        symbol,coin_name=resolved
        try: snapshot=await get_market_snapshot(symbol); klines=await binance_klines(symbol,"1h",100); ta=compute_technical_summary(klines)
        except BinanceError as exc: logger.warning("Binance lookup failed for %s: %s",symbol,exc)
    t=normalize_user_text(text); wants_chart=any(w in t for w in ("chart","graph","image","photo","চার্ট","গ্রাফ","ছবি","ফটো","ইমেজ","ক্যান্ডেল"))
    if wants_chart and resolved and klines:
        try: await update.message.reply_photo(photo=InputFile(await asyncio.to_thread(generate_market_chart,klines,coin_name,ta,snapshot),filename="market_chart.png")); return
        except Exception: logger.exception("Chart generation failed")
    system_prompt=SYSTEM_PROMPT_TEMPLATE.format(language_name=LANGUAGE_NAMES.get(lang,"English")); context_block=build_context_block(text,lang,coin_name,snapshot,ta)
    try: reply=await ask_groq(system_prompt,context_block)
    except Exception: logger.exception("Groq API call failed"); await update.message.reply_text("AI service is temporarily unavailable. Please try again shortly."); return
    await reply_long(update,reply or "No response generated.")


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
        text = (
            "🌍 Global Crypto Market\n"
            f"Total Market Cap: {_format_usd(m['total_market_cap_usd'])}\n"
            f"24h Market Cap Change: {m['market_cap_change_24h_pct']:.2f}%\n"
            f"24h Volume: {_format_usd(m['total_volume_usd'])}\n"
            f"BTC Dominance: {m['btc_dominance']:.2f}%\n"
            f"ETH Dominance: {m['eth_dominance']:.2f}%\n"
            f"Active Cryptocurrencies: {m['active_cryptocurrencies']:,}\n"
            f"Markets: {m['markets']:,}"
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

def build_application() -> Application:
    app=ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(configure_bot_commands).build()
    for name,fn in [("start",start_handler),("help",help_handler),("id",id_handler),("status",status_handler),("price",price_handler),("admin",admin_handler),("users",users_handler),("approved",approved_handler),("pending",pending_handler),("stats",stats_handler),("approve",approve_handler),("reject",reject_handler),("news",news_handler),("market",market_handler),("broadcast",broadcast_handler)]: app.add_handler(CommandHandler(name,fn))
    app.add_handler(CallbackQueryHandler(admin_callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,message_handler)); app.add_error_handler(error_handler); return app


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
