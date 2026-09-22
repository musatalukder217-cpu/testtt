#!/usr/bin/env python3
"""
Ultra-Advanced Private Telegram Crypto Intelligence Bot
=======================================================
Production-grade async Telegram bot with:
- Admin approval & user whitelisting
- Groq AI (openai/gpt-oss-120b) integration
- Live Binance market data & technical analysis
- Candlestick pattern recognition
- Fair Value Gap (FVG) detection
- Support/Resistance calculation
- Automated annotated chart generation
- Live crypto news sentiment (zero API keys)
- Universal multilingual support
"""

import os
import io
import json
import re
import logging
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import pandas as pd
import numpy as np
import mplfinance as mpf
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import xmltodict

from groq import Groq

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatAction,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("CryptoIntelBot")

# ─────────────────────────────────────────────
# ENVIRONMENT VARIABLES
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_FALLBACK_MODEL = "llama-3.3-70b-versatile"
PORT = int(os.getenv("PORT", "8443"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set!")
if not ADMIN_TELEGRAM_ID:
    raise RuntimeError("ADMIN_TELEGRAM_ID environment variable is not set!")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY environment variable is not set!")

# ─────────────────────────────────────────────
# GROQ CLIENT (Synchronous — wrapped with asyncio.to_thread)
# ─────────────────────────────────────────────
groq_client = Groq(api_key=GROQ_API_KEY)

# ─────────────────────────────────────────────
# ALLOWED USERS PERSISTENCE
# ─────────────────────────────────────────────
ALLOWED_USERS_FILE = Path("allowed_users.json")


def _load_allowed_users() -> set:
    """Load authorized user IDs from persistent JSON file."""
    try:
        if ALLOWED_USERS_FILE.exists():
            with open(ALLOWED_USERS_FILE, "r") as f:
                data = json.load(f)
                users = set(int(uid) for uid in data)
        else:
            users = set()
    except (json.JSONDecodeError, ValueError):
        users = set()
    # Admin is ALWAYS whitelisted
    users.add(ADMIN_TELEGRAM_ID)
    return users


def _save_allowed_users(users: set):
    """Persist authorized user IDs to JSON file."""
    with open(ALLOWED_USERS_FILE, "w") as f:
        json.dump(list(users), f, indent=2)


allowed_users: set = _load_allowed_users()
pending_requests: dict = {}  # uid -> {user info}

# ─────────────────────────────────────────────
# MODULE 1: PERMISSION & ACCESS CONTROL
# ─────────────────────────────────────────────


def is_authorized(user_id: int) -> bool:
    """Check if a user is in the whitelist."""
    return user_id in allowed_users


def authorize_user(user_id: int):
    """Add user to permanent whitelist."""
    global allowed_users
    allowed_users.add(user_id)
    _save_allowed_users(allowed_users)


def revoke_user(user_id: int):
    """Remove user from whitelist."""
    global allowed_users
    allowed_users.discard(user_id)
    _save_allowed_users(allowed_users)


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_TELEGRAM_ID


async def send_access_request_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward access request to admin with approve/reject buttons."""
    user = update.effective_user
    uid = user.id
    first_name = user.first_name or "N/A"
    username = f"@{user.username}" if user.username else "No username"

    pending_requests[uid] = {
        "first_name": first_name,
        "username": username,
        "user_id": uid,
    }

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{uid}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{uid}"),
        ]
    ])

    admin_message = (
        "🔔 <b>New Access Request</b>\n\n"
        f"👤 <b>Name:</b> {first_name}\n"
        f"🆔 <b>Username:</b> {username}\n"
        f"🔢 <b>Telegram ID:</b> <code>{uid}</code>\n\n"
        "Approve or reject this user's access:"
    )

    try:
        await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=admin_message,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"Failed to send access request to admin: {e}")


async def handle_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin's approve/reject button clicks."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ You are not authorized to perform this action.")
        return

    data = query.data
    action, uid_str = data.split(":", 1)
    uid = int(uid_str)

    if action == "approve":
        authorize_user(uid)
        await query.edit_message_text(
            f"✅ <b>Approved!</b>\n\nUser <code>{uid}</code> has been granted access.",
            parse_mode="HTML",
        )
        # Notify the user
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=(
                    "🎉 <b>Access Granted!</b>\n\n"
                    "Your access request has been approved! "
                    "You can now use the bot.\n\n"
                    "Type /help to see available commands."
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Could not notify user {uid}: {e}")

    elif action == "reject":
        pending_requests.pop(uid, None)
        await query.edit_message_text(
            f"❌ <b>Rejected.</b>\n\nUser <code>{uid}</code> was denied access.",
            parse_mode="HTML",
        )
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=(
                    "😔 <b>Access Denied</b>\n\n"
                    "Your access request has been reviewed and unfortunately denied. "
                    "If you believe this is a mistake, please contact the administrator."
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Could not notify rejected user {uid}: {e}")

    pending_requests.pop(uid, None)


async def unauthorized_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle messages from unauthorized users."""
    user = update.effective_user
    await update.message.reply_text(
        "🔒 <b>Private Bot</b>\n\n"
        "This bot is private and requires admin approval.\n"
        "Your access request has been automatically forwarded to the administrator.\n\n"
        "⏳ Please wait for approval.",
        parse_mode="HTML",
    )
    await send_access_request_to_admin(update, context)


# ─────────────────────────────────────────────
# MODULE 3: LIVE NEWS & SENTIMENT (Zero API Key)
# ─────────────────────────────────────────────

NEWS_RSS_FEEDS = [
    "https://cryptopanic.com/news/rss/",
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
]


def fetch_crypto_news(max_headlines: int = 5) -> list[dict]:
    """
    Fetch breaking crypto headlines from public RSS feeds.
    No API key required. Returns list of {title, link, source} dicts.
    """
    headlines = []

    for feed_url in NEWS_RSS_FEEDS:
        try:
            resp = requests.get(
                feed_url,
                timeout=8,
                headers={"User-Agent": "CryptoIntelBot/1.0"},
            )
            if resp.status_code != 200:
                continue

            parsed = xmltodict.parse(resp.content)
            channel = parsed.get("rss", {}).get("channel", {})
            items = channel.get("item", [])

            if isinstance(items, dict):
                items = [items]

            source_name = channel.get("title", feed_url.split("/")[2])

            for item in items[:max_headlines]:
                title = item.get("title", "").strip()
                link = item.get("link", "").strip()
                if title:
                    headlines.append({
                        "title": title,
                        "link": link,
                        "source": source_name,
                    })

            if len(headlines) >= max_headlines:
                break

        except Exception as e:
            logger.warning(f"RSS fetch failed for {feed_url}: {e}")
            continue

    return headlines[:max_headlines]


def format_news_for_prompt(headlines: list[dict]) -> str:
    """Format headlines into text for AI prompt injection."""
    if not headlines:
        return "No recent crypto news available."

    lines = []
    for i, h in enumerate(headlines, 1):
        lines.append(f"{i}. {h['title']} (Source: {h['source']})")
    return "\n".join(lines)


def format_news_for_display(headlines: list[dict]) -> str:
    """Format headlines for user-facing display."""
    if not headlines:
        return "📰 No recent news available at the moment."

    lines = ["📰 <b>Latest Crypto Headlines</b>\n"]
    for i, h in enumerate(headlines, 1):
        link_text = f"<a href='{h['link']}'>{h['title']}</a>" if h['link'] else h['title']
        lines.append(f"  {i}. {link_text}")
        lines.append(f"     <i>— {h['source']}</i>\n")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# MODULE 4: BINANCE MARKET DATA ENGINE
# ─────────────────────────────────────────────

BINANCE_BASE = "https://api.binance.com"


def fetch_klines(
    symbol: str,
    interval: str = "1h",
    limit: int = 100,
) -> Optional[pd.DataFrame]:
    """
    Fetch candlestick data from Binance public API.
    Returns a DataFrame with OHLCV + datetime index.
    """
    symbol = symbol.upper().replace("/", "")
    url = f"{BINANCE_BASE}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"Binance klines fetch error: {e}")
        return None

    if not data or not isinstance(data, list):
        return None

    df = pd.DataFrame(data, columns=[
        "open_time", "Open", "High", "Low", "Close", "Volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["Date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df.set_index("Date", inplace=True)
    df = df[["Open", "High", "Low", "Close", "Volume"]]
    return df


def fetch_ticker_24h(symbol: str) -> Optional[dict]:
    """Fetch 24h ticker statistics from Binance."""
    symbol = symbol.upper().replace("/", "")
    url = f"{BINANCE_BASE}/api/v3/ticker/24hr"
    params = {"symbol": symbol}

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Binance 24h ticker error: {e}")
        return None


def fetch_order_book(symbol: str, limit: int = 20) -> Optional[dict]:
    """Fetch order book depth from Binance."""
    symbol = symbol.upper().replace("/", "")
    url = f"{BINANCE_BASE}/api/v3/depth"
    params = {"symbol": symbol, "limit": limit}

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Binance order book error: {e}")
        return None


def fetch_current_price(symbol: str) -> Optional[float]:
    """Fetch the current price for a symbol."""
    symbol = symbol.upper().replace("/", "")
    url = f"{BINANCE_BASE}/api/v3/ticker/price"
    params = {"symbol": symbol}

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return float(data.get("price", 0))
    except Exception as e:
        logger.error(f"Price fetch error: {e}")
        return None


# ─────────────────────────────────────────────
# TECHNICAL ANALYSIS ENGINE
# ─────────────────────────────────────────────


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Calculate Relative Strength Index."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def calculate_ema(series: pd.Series, span: int) -> pd.Series:
    """Calculate Exponential Moving Average."""
    return series.ewm(span=span, adjust=False).mean()


def calculate_macd(series: pd.Series) -> tuple:
    """Calculate MACD line, signal line, and histogram."""
    ema12 = calculate_ema(series, 12)
    ema26 = calculate_ema(series, 26)
    macd_line = ema12 - ema26
    signal_line = calculate_ema(macd_line, 9)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calculate_support_resistance(df: pd.DataFrame) -> dict:
    """
    Calculate dynamic support and resistance levels using
    swing highs/lows, 24h range, and pivot points.
    """
    highs = df["High"].values
    lows = df["Low"].values
    closes = df["Close"].values

    # Recent pivot points (classic)
    last_high = float(df["High"].iloc[-1])
    last_low = float(df["Low"].iloc[-1])
    last_close = float(df["Close"].iloc[-1])
    pivot = (last_high + last_low + last_close) / 3

    r1 = 2 * pivot - last_low
    r2 = pivot + (last_high - last_low)
    s1 = 2 * pivot - last_high
    s2 = pivot - (last_high - last_low)

    # Swing-based support/resistance
    swing_highs = []
    swing_lows = []
    lookback = 5

    for i in range(lookback, len(highs) - lookback):
        if highs[i] == max(highs[i - lookback:i + lookback + 1]):
            swing_highs.append(float(highs[i]))
        if lows[i] == min(lows[i - lookback:i + lookback + 1]):
            swing_lows.append(float(lows[i]))

    # Get nearest levels
    recent_swing_high = max(swing_highs[-3:]) if swing_highs else r1
    recent_swing_low = min(swing_lows[-3:]) if swing_lows else s1

    # 24h high/low
    period_high = float(df["High"].tail(24).max())
    period_low = float(df["Low"].tail(24).min())

    return {
        "pivot": round(pivot, 6),
        "resistance_1": round(r1, 6),
        "resistance_2": round(r2, 6),
        "support_1": round(s1, 6),
        "support_2": round(s2, 6),
        "swing_high": round(recent_swing_high, 6),
        "swing_low": round(recent_swing_low, 6),
        "period_high": round(period_high, 6),
        "period_low": round(period_low, 6),
        "current_price": round(last_close, 6),
    }


def detect_candlestick_patterns(df: pd.DataFrame) -> list[str]:
    """Detect major candlestick patterns from recent candles."""
    patterns = []
    if len(df) < 3:
        return patterns

    # Last 3 candles
    c1 = df.iloc[-3]  # oldest of the 3
    c2 = df.iloc[-2]
    c3 = df.iloc[-1]  # most recent

    def body(candle):
        return abs(candle["Close"] - candle["Open"])

    def upper_shadow(candle):
        return candle["High"] - max(candle["Close"], candle["Open"])

    def lower_shadow(candle):
        return min(candle["Close"], candle["Open"]) - candle["Low"]

    def is_bullish(candle):
        return candle["Close"] > candle["Open"]

    def is_bearish(candle):
        return candle["Close"] < candle["Open"]

    body3 = body(c3)
    total_range3 = c3["High"] - c3["Low"]

    if total_range3 == 0:
        return patterns

    # Doji
    if body3 / total_range3 < 0.1:
        patterns.append("⚡ Doji — Indecision; potential reversal signal")

    # Hammer (bullish reversal)
    if (lower_shadow(c3) >= 2 * body3 and
            upper_shadow(c3) <= body3 * 0.3 and
            body3 > 0):
        patterns.append("🔨 Hammer — Bullish reversal signal")

    # Inverted Hammer
    if (upper_shadow(c3) >= 2 * body3 and
            lower_shadow(c3) <= body3 * 0.3 and
            body3 > 0):
        patterns.append("🔨 Inverted Hammer — Potential bullish reversal")

    # Shooting Star (bearish reversal at top)
    if (upper_shadow(c3) >= 2 * body3 and
            lower_shadow(c3) <= body3 * 0.3 and
            is_bearish(c3)):
        patterns.append("🌟 Shooting Star — Bearish reversal signal")

    # Bullish Engulfing
    if (is_bearish(c2) and is_bullish(c3) and
            c3["Open"] <= c2["Close"] and
            c3["Close"] >= c2["Open"]):
        patterns.append("🟢 Bullish Engulfing — Strong buy signal")

    # Bearish Engulfing
    if (is_bullish(c2) and is_bearish(c3) and
            c3["Open"] >= c2["Close"] and
            c3["Close"] <= c2["Open"]):
        patterns.append("🔴 Bearish Engulfing — Strong sell signal")

    # Morning Star (3-candle bullish reversal)
    if (is_bearish(c1) and
            body(c2) / (c2["High"] - c2["Low"] + 1e-10) < 0.3 and
            is_bullish(c3) and
            c3["Close"] > (c1["Open"] + c1["Close"]) / 2):
        patterns.append("🌅 Morning Star — Bullish reversal pattern")

    # Evening Star (3-candle bearish reversal)
    if (is_bullish(c1) and
            body(c2) / (c2["High"] - c2["Low"] + 1e-10) < 0.3 and
            is_bearish(c3) and
            c3["Close"] < (c1["Open"] + c1["Close"]) / 2):
        patterns.append("🌇 Evening Star — Bearish reversal pattern")

    # Three White Soldiers
    if (is_bullish(c1) and is_bullish(c2) and is_bullish(c3) and
            c2["Close"] > c1["Close"] and c3["Close"] > c2["Close"] and
            c2["Open"] > c1["Open"] and c3["Open"] > c2["Open"]):
        patterns.append("🟢🟢🟢 Three White Soldiers — Strong bullish continuation")

    # Three Black Crows
    if (is_bearish(c1) and is_bearish(c2) and is_bearish(c3) and
            c2["Close"] < c1["Close"] and c3["Close"] < c2["Close"] and
            c2["Open"] < c1["Open"] and c3["Open"] < c2["Open"]):
        patterns.append("🔴🔴🔴 Three Black Crows — Strong bearish continuation")

    return patterns


def detect_fvg(df: pd.DataFrame) -> list[dict]:
    """
    Detect Fair Value Gaps (FVG) from candle data.
    An FVG is a 3-candle pattern where the wicks of candle 1 and candle 3
    do not overlap, leaving a gap through candle 2.
    """
    fvgs = []
    if len(df) < 3:
        return fvgs

    for i in range(2, min(len(df), 20)):  # Check last 20 candles
        c1 = df.iloc[i - 2]
        c3 = df.iloc[i]

        # Bullish FVG: candle 1 high < candle 3 low
        if c1["High"] < c3["Low"]:
            fvgs.append({
                "type": "Bullish FVG",
                "top": float(c3["Low"]),
                "bottom": float(c1["High"]),
                "index": i,
            })

        # Bearish FVG: candle 1 low > candle 3 high
        if c1["Low"] > c3["High"]:
            fvgs.append({
                "type": "Bearish FVG",
                "top": float(c1["Low"]),
                "bottom": float(c3["High"]),
                "index": i,
            })

    return fvgs[-5:]  # Return last 5 FVGs


def analyze_order_book(order_book: dict) -> dict:
    """Analyze order book for buy/sell pressure."""
    if not order_book:
        return {"bid_volume": 0, "ask_volume": 0, "ratio": 0, "pressure": "Unknown"}

    bid_volume = sum(float(b[1]) for b in order_book.get("bids", []))
    ask_volume = sum(float(a[1]) for a in order_book.get("asks", []))

    total = bid_volume + ask_volume
    ratio = bid_volume / ask_volume if ask_volume > 0 else float('inf')

    if ratio > 1.5:
        pressure = "🟢 Strong Buy Pressure"
    elif ratio > 1.1:
        pressure = "🟢 Moderate Buy Pressure"
    elif ratio < 0.67:
        pressure = "🔴 Strong Sell Pressure"
    elif ratio < 0.9:
        pressure = "🔴 Moderate Sell Pressure"
    else:
        pressure = "⚖️ Balanced / Neutral"

    return {
        "bid_volume": round(bid_volume, 4),
        "ask_volume": round(ask_volume, 4),
        "ratio": round(ratio, 4),
        "pressure": pressure,
    }


def build_full_analysis(symbol: str) -> dict:
    """Build comprehensive analysis for a symbol."""
    result = {"symbol": symbol.upper(), "success": False}

    # Fetch data
    df_1h = fetch_klines(symbol, "1h", 100)
    df_4h = fetch_klines(symbol, "4h", 50)
    ticker = fetch_ticker_24h(symbol)
    ob = fetch_order_book(symbol, 50)

    if df_1h is None or df_1h.empty:
        result["error"] = f"Could not fetch data for {symbol}. Please check the symbol."
        return result

    result["success"] = True

    # Current price
    result["current_price"] = float(df_1h["Close"].iloc[-1])

    # 24h stats
    if ticker:
        result["price_change_24h"] = f"{float(ticker.get('priceChangePercent', 0)):.2f}%"
        result["high_24h"] = float(ticker.get("highPrice", 0))
        result["low_24h"] = float(ticker.get("lowPrice", 0))
        result["volume_24h"] = f"{float(ticker.get('quoteVolume', 0)):,.0f} USDT"

    # Technical indicators
    rsi = calculate_rsi(df_1h["Close"])
    macd_line, signal_line, histogram = calculate_macd(df_1h["Close"])
    ema_20 = calculate_ema(df_1h["Close"], 20)
    ema_50 = calculate_ema(df_1h["Close"], 50)
    ema_200 = calculate_ema(df_1h["Close"], 200)

    result["rsi"] = round(float(rsi.iloc[-1]), 2) if not pd.isna(rsi.iloc[-1]) else None
    result["macd"] = round(float(macd_line.iloc[-1]), 6) if not pd.isna(macd_line.iloc[-1]) else None
    result["macd_signal"] = round(float(signal_line.iloc[-1]), 6) if not pd.isna(signal_line.iloc[-1]) else None
    result["ema_20"] = round(float(ema_20.iloc[-1]), 6) if not pd.isna(ema_20.iloc[-1]) else None
    result["ema_50"] = round(float(ema_50.iloc[-1]), 6) if not pd.isna(ema_50.iloc[-1]) else None
    result["ema_200"] = round(float(ema_200.iloc[-1]), 6) if not pd.isna(ema_200.iloc[-1]) else None

    # Support & Resistance
    sr = calculate_support_resistance(df_1h)
    result["support_resistance"] = sr

    # Candlestick patterns
    result["candlestick_patterns"] = detect_candlestick_patterns(df_1h)

    # FVG
    fvg_1h = detect_fvg(df_1h)
    fvg_4h = detect_fvg(df_4h) if df_4h is not None and not df_4h.empty else []
    result["fvg_1h"] = fvg_1h
    result["fvg_4h"] = fvg_4h

    # Order book
    result["order_book"] = analyze_order_book(ob)

    return result


def format_analysis_text(analysis: dict) -> str:
    """Format analysis dictionary into a readable text block for AI context."""
    if not analysis.get("success"):
        return analysis.get("error", "Analysis failed.")

    lines = [
        f"Symbol: {analysis['symbol']}",
        f"Current Price: {analysis['current_price']}",
        f"24h Change: {analysis.get('price_change_24h', 'N/A')}",
        f"24h High: {analysis.get('high_24h', 'N/A')}",
        f"24h Low: {analysis.get('low_24h', 'N/A')}",
        f"24h Volume: {analysis.get('volume_24h', 'N/A')}",
        f"RSI(14): {analysis.get('rsi', 'N/A')}",
        f"MACD: {analysis.get('macd', 'N/A')}",
        f"MACD Signal: {analysis.get('macd_signal', 'N/A')}",
        f"EMA 20: {analysis.get('ema_20', 'N/A')}",
        f"EMA 50: {analysis.get('ema_50', 'N/A')}",
        f"EMA 200: {analysis.get('ema_200', 'N/A')}",
    ]

    sr = analysis.get("support_resistance", {})
    if sr:
        lines.append(f"\nSupport/Resistance Levels:")
        lines.append(f"  Pivot: {sr.get('pivot')}")
        lines.append(f"  Support 1: {sr.get('support_1')}")
        lines.append(f"  Support 2: {sr.get('support_2')}")
        lines.append(f"  Resistance 1: {sr.get('resistance_1')}")
        lines.append(f"  Resistance 2: {sr.get('resistance_2')}")
        lines.append(f"  Swing High: {sr.get('swing_high')}")
        lines.append(f"  Swing Low: {sr.get('swing_low')}")

    patterns = analysis.get("candlestick_patterns", [])
    if patterns:
        lines.append(f"\nDetected Candlestick Patterns:")
        for p in patterns:
            lines.append(f"  • {p}")

    fvg_1h = analysis.get("fvg_1h", [])
    if fvg_1h:
        lines.append(f"\n1H Fair Value Gaps:")
        for f in fvg_1h:
            lines.append(f"  • {f['type']}: {f['bottom']} - {f['top']}")

    fvg_4h = analysis.get("fvg_4h", [])
    if fvg_4h:
        lines.append(f"\n4H Fair Value Gaps:")
        for f in fvg_4h:
            lines.append(f"  • {f['type']}: {f['bottom']} - {f['top']}")

    ob = analysis.get("order_book", {})
    if ob:
        lines.append(f"\nOrder Book Analysis:")
        lines.append(f"  Bid Volume: {ob.get('bid_volume')}")
        lines.append(f"  Ask Volume: {ob.get('ask_volume')}")
        lines.append(f"  Bid/Ask Ratio: {ob.get('ratio')}")
        lines.append(f"  Pressure: {ob.get('pressure')}")

    return "\n".join(lines)


# ─────────────────────────────────────────────
# MODULE 5: CHART IMAGE GENERATOR
# ─────────────────────────────────────────────


def generate_chart_image(
    symbol: str,
    interval: str = "1h",
    limit: int = 80,
) -> Optional[tuple[io.BytesIO, dict]]:
    """
    Generate an annotated candlestick chart with S/R levels,
    EMA lines, and FVG zones. Returns (BytesIO image, analysis dict).
    """
    df = fetch_klines(symbol, interval, limit)
    if df is None or df.empty:
        return None

    sr = calculate_support_resistance(df)
    fvgs = detect_fvg(df)

    # Calculate EMAs for overlay
    ema20 = calculate_ema(df["Close"], 20)
    ema50 = calculate_ema(df["Close"], 50)

    # Build horizontal lines for support/resistance
    hlines_values = []
    hlines_colors = []
    hlines_labels = []

    for key, color, label in [
        ("support_1", "green", "S1"),
        ("support_2", "lime", "S2"),
        ("resistance_1", "red", "R1"),
        ("resistance_2", "orangered", "R2"),
        ("pivot", "blue", "Pivot"),
    ]:
        val = sr.get(key)
        if val and val > 0:
            hlines_values.append(val)
            hlines_colors.append(color)
            hlines_labels.append(label)

    # Custom style
    mc = mpf.make_marketcolors(
        up="#00c853",
        down="#ff1744",
        edge="inherit",
        wick="inherit",
        volume={"up": "#00c85355", "down": "#ff174455"},
    )
    style = mpf.make_mpf_style(
        marketcolors=mc,
        figcolor="#1a1a2e",
        facecolor="#1a1a2e",
        edgecolor="#1a1a2e",
        gridcolor="#333366",
        gridstyle="--",
        rc={
            "axes.labelcolor": "white",
            "xtick.color": "white",
            "ytick.color": "white",
        },
    )

    # Additional plots: EMAs
    ap = [
        mpf.make_addplot(ema20, color="#00bcd4", width=1.0, label="EMA 20"),
        mpf.make_addplot(ema50, color="#ff9800", width=1.0, label="EMA 50"),
    ]

    # Create figure
    fig, axes = mpf.plot(
        df,
        type="candle",
        style=style,
        volume=True,
        addplot=ap,
        title=f"\n{symbol.upper()} ({interval.upper()}) — Crypto Intel Bot",
        ylabel="Price",
        ylabel_lower="Volume",
        figsize=(14, 8),
        returnfig=True,
        tight_layout=True,
    )

    ax_main = axes[0]

    # Draw S/R horizontal lines with labels
    for val, color, label in zip(hlines_values, hlines_colors, hlines_labels):
        ax_main.axhline(y=val, color=color, linestyle="--", linewidth=1.2, alpha=0.8)
        ax_main.text(
            ax_main.get_xlim()[1] * 0.98,
            val,
            f" {label}: {val:.4f}" if val < 1 else f" {label}: {val:.2f}",
            color=color,
            fontsize=8,
            fontweight="bold",
            va="center",
            ha="right",
            bbox=dict(boxstyle="round,pad=0.2", facecolor="#1a1a2e", edgecolor=color, alpha=0.9),
        )

    # Draw FVG zones
    for fvg in fvgs[:3]:
        fvg_color = "#00c85330" if "Bullish" in fvg["type"] else "#ff174430"
        fvg_edge = "#00c853" if "Bullish" in fvg["type"] else "#ff1744"
        ax_main.axhspan(
            fvg["bottom"], fvg["top"],
            alpha=0.25,
            color=fvg_color,
            linewidth=0,
        )
        mid = (fvg["top"] + fvg["bottom"]) / 2
        ax_main.text(
            ax_main.get_xlim()[0] + 1,
            mid,
            f" {fvg['type']}",
            color=fvg_edge,
            fontsize=7,
            fontstyle="italic",
            va="center",
        )

    # Watermark
    ax_main.text(
        0.5, 0.5, "Crypto Intel Bot",
        transform=ax_main.transAxes,
        fontsize=28, color="white", alpha=0.06,
        ha="center", va="center", fontweight="bold",
    )

    # Save to BytesIO
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor="#1a1a2e")
    plt.close(fig)
    buf.seek(0)

    analysis = build_full_analysis(symbol)
    return buf, analysis


# ─────────────────────────────────────────────
# MODULE 2: AI ENGINE (Groq Sync → asyncio.to_thread)
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """You are an ultra-advanced Crypto Intelligence AI, built into a private Telegram bot. Your capabilities:

🔹 LANGUAGE: Automatically detect the user's input language and script (Bengali/বাংলা, English, Hindi/हिन्दी, Urdu/اردو, Arabic/العربية, Chinese/中文, Spanish, etc.) and respond ENTIRELY in that same language. Default to English only if the language is truly ambiguous.

🔹 CRYPTO EXPERTISE: You have complete, professional-level knowledge of:
- Technical Analysis: RSI, MACD, EMA, Bollinger Bands, Fibonacci retracements, candlestick patterns
- Market Structure: Support/Resistance, Fair Value Gaps (FVG), Order Blocks, Break of Structure (BOS), Change of Character (CHOCH)
- Candlestick Behavior: Explain precisely how specific candle closes (above/below key MAs, at S/R levels) impact bullish/bearish continuation or reversal
- Trading Psychology, Risk Management, Position Sizing
- DeFi, DEX operations, yield farming, liquidity pools
- Wallet safety (Trust Wallet, MetaMask), seed phrase security
- Exchange tutorials (Binance, Bybit, OKX, TradingView)

🔹 ANALYSIS STYLE: When provided with live market data, combine the data with your expertise to provide actionable insights. Always mention specific price levels, indicator readings, and probabilities where relevant. Be confident but include appropriate risk warnings.

🔹 FORMAT: Use clean formatting with emojis for readability. Structure responses with headers and bullet points for complex analyses.

🔹 IMPORTANT: You analyze data provided to you. Always base your analysis on the actual numbers given. Never fabricate price data."""


def _call_groq_sync(messages: list[dict], max_tokens: int = 4096) -> str:
    """Synchronous Groq API call — to be wrapped with asyncio.to_thread."""
    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.7,
        )
        return response.choices[0].message.content
    except Exception as primary_error:
        logger.warning(f"Primary model ({GROQ_MODEL}) failed: {primary_error}. Trying fallback...")
        try:
            response = groq_client.chat.completions.create(
                model=GROQ_FALLBACK_MODEL,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.7,
            )
            return response.choices[0].message.content
        except Exception as fallback_error:
            logger.error(f"Fallback model also failed: {fallback_error}")
            return "⚠️ AI service is temporarily unavailable. Please try again later."


async def call_groq_ai(user_message: str, context_data: str = "") -> str:
    """Async wrapper for Groq AI calls."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]

    if context_data:
        messages.append({
            "role": "system",
            "content": f"LIVE MARKET DATA & CONTEXT:\n{context_data}",
        })

    messages.append({"role": "user", "content": user_message})

    result = await asyncio.to_thread(_call_groq_sync, messages)
    return result


# ─────────────────────────────────────────────
# SYMBOL EXTRACTION HELPER
# ─────────────────────────────────────────────

COMMON_SYMBOLS = {
    "btc": "BTCUSDT", "bitcoin": "BTCUSDT",
    "eth": "ETHUSDT", "ethereum": "ETHUSDT",
    "sol": "SOLUSDT", "solana": "SOLUSDT",
    "bnb": "BNBUSDT",
    "xrp": "XRPUSDT", "ripple": "XRPUSDT",
    "doge": "DOGEUSDT", "dogecoin": "DOGEUSDT",
    "ada": "ADAUSDT", "cardano": "ADAUSDT",
    "dot": "DOTUSDT", "polkadot": "DOTUSDT",
    "avax": "AVAXUSDT", "avalanche": "AVAXUSDT",
    "matic": "MATICUSDT", "polygon": "MATICUSDT",
    "link": "LINKUSDT", "chainlink": "LINKUSDT",
    "shib": "SHIBUSDT",
    "ltc": "LTCUSDT", "litecoin": "LTCUSDT",
    "uni": "UNIUSDT", "uniswap": "UNIUSDT",
    "atom": "ATOMUSDT", "cosmos": "ATOMUSDT",
    "near": "NEARUSDT",
    "apt": "APTUSDT", "aptos": "APTUSDT",
    "arb": "ARBUSDT", "arbitrum": "ARBUSDT",
    "op": "OPUSDT", "optimism": "OPUSDT",
    "sui": "SUIUSDT",
    "pepe": "PEPEUSDT",
    "fet": "FETUSDT",
    "inj": "INJUSDT", "injective": "INJUSDT",
    "sei": "SEIUSDT",
    "ton": "TONUSDT", "toncoin": "TONUSDT",
    "trx": "TRXUSDT", "tron": "TRXUSDT",
}


def extract_symbol(text: str) -> Optional[str]:
    """Extract a crypto trading symbol from text."""
    text_lower = text.lower().strip()

    # Direct match: e.g., "BTCUSDT"
    match = re.search(r'\b([a-zA-Z]{2,10})(usdt|busd|usdc|btc|eth)\b', text_lower)
    if match:
        return match.group(0).upper()

    # Common name match
    for key, sym in COMMON_SYMBOLS.items():
        pattern = rf'\b{re.escape(key)}\b'
        if re.search(pattern, text_lower):
            return sym

    # Single token match (e.g., just "BTC")
    match = re.search(r'\b([a-zA-Z]{2,6})\b', text_lower)
    if match:
        token = match.group(1).upper()
        if token in [v.replace("USDT", "") for v in COMMON_SYMBOLS.values()]:
            return f"{token}USDT"

    return None


def is_chart_request(text: str) -> bool:
    """Detect if the user is asking for a chart."""
    chart_keywords = [
        "chart", "চার্ট", "ग्राफ", "चार्ट", "grafik", "gráfico",
        "support resistance", "সাপোর্ট রেজিস্ট্যান্স", "s/r",
        "visual", "graph", "plot", "draw", "image", "ছবি",
        "দেখাও", "mark", "মার্ক", "candle", "ক্যান্ডেল",
        "candlestick", "technical chart",
    ]
    text_lower = text.lower()
    return any(kw in text_lower for kw in chart_keywords)


def is_market_question(text: str) -> bool:
    """Detect if the user is asking about market direction, prediction, or analysis."""
    keywords = [
        "market", "মার্কেট", "बाज़ार", "direction", "prediction",
        "trend", "ট্রেন্ড", "analysis", "এনালাইসিস", "price",
        "দাম", "কোথায়", "কত", "support", "resistance",
        "সাপোর্ট", "রেজিস্ট্যান্স", "pump", "dump", "bull",
        "bear", "buy", "sell", "long", "short", "target",
        "কিনব", "বিক্রি", "signal", "সিগনাল", "where",
        "zone", "জোন", "level", "লেভেল", "fvg", "gap",
        "volume", "ভলিউম", "rsi", "macd", "ema",
        "oversold", "overbought", "breakout", "breakdown",
        "entry", "exit", "stop loss", "take profit",
        "কোন দিকে", "uptrend", "downtrend",
    ]
    text_lower = text.lower()
    return any(kw in text_lower for kw in keywords)


# ─────────────────────────────────────────────
# TELEGRAM COMMAND HANDLERS
# ─────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user = update.effective_user

    if not is_authorized(user.id):
        await unauthorized_handler(update, context)
        return

    welcome = (
        f"🤖 <b>Welcome to Crypto Intelligence Bot!</b>\n\n"
        f"Hello {user.first_name}! I'm your all-in-one crypto analyst.\n\n"
        f"<b>What I can do:</b>\n"
        f"📊 Real-time price & technical analysis\n"
        f"📈 Annotated candlestick charts with S/R zones\n"
        f"🕯 Candlestick pattern recognition\n"
        f"📐 Fair Value Gap (FVG) detection\n"
        f"📰 Live crypto news & sentiment\n"
        f"🤖 AI-powered market insights\n"
        f"🌐 Multilingual support (Bengali, Hindi, Arabic, etc.)\n\n"
        f"<b>Commands:</b>\n"
        f"/price BTCUSDT — Get current price\n"
        f"/chart BTCUSDT — Get annotated chart\n"
        f"/news — Latest crypto headlines\n"
        f"/help — Full help guide\n\n"
        f"💬 Or just ask me anything in natural language!"
    )

    await update.message.reply_text(welcome, parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    user = update.effective_user

    if not is_authorized(user.id):
        await unauthorized_handler(update, context)
        return

    help_text = (
        "📖 <b>Crypto Intelligence Bot — Full Guide</b>\n\n"
        "<b>📌 Commands:</b>\n"
        "• /start — Welcome screen\n"
        "• /price &lt;SYMBOL&gt; — Get live price & 24h stats\n"
        "  Example: <code>/price BTCUSDT</code>\n"
        "• /chart &lt;SYMBOL&gt; — Generate annotated chart\n"
        "  Example: <code>/chart ETHUSDT</code>\n"
        "• /news — Latest crypto headlines\n"
        "• /help — This guide\n\n"
        "<b>💬 Natural Language Queries:</b>\n"
        "• \"BTC সাপোর্ট জোন কোথায়?\"\n"
        "• \"SOL এর চার্টটা সাপোর্ট রেজিস্ট্যান্স মার্ক করে দাও\"\n"
        "• \"মার্কেট এখন কোন দিকে যেতে পারে?\"\n"
        "• \"ETH technical analysis\"\n"
        "• \"What's the RSI for BTC?\"\n"
        "• \"Show me DOGE chart with FVG\"\n\n"
        "<b>🤖 General Crypto Questions:</b>\n"
        "• \"How to use MetaMask?\"\n"
        "• \"Explain RSI indicator\"\n"
        "• \"What is a Fair Value Gap?\"\n"
        "• \"বিটকয়েন কি?\"\n\n"
        "<b>🌐 Languages:</b> English, বাংলা, हिन्दी, اردو, العربية, 中文, and more!\n\n"
    )

    if is_admin(user.id):
        help_text += (
            "<b>👑 Admin Commands:</b>\n"
            "• /users — List all approved users\n"
            "• /revoke &lt;USER_ID&gt; — Remove user access\n"
        )

    await update.message.reply_text(help_text, parse_mode="HTML")


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /price command."""
    user = update.effective_user

    if not is_authorized(user.id):
        await unauthorized_handler(update, context)
        return

    if not context.args:
        await update.message.reply_text(
            "❓ Please specify a symbol.\nExample: <code>/price BTCUSDT</code>",
            parse_mode="HTML",
        )
        return

    symbol = context.args[0].upper().replace("/", "")
    if not symbol.endswith(("USDT", "BUSD", "USDC", "BTC", "ETH")):
        symbol += "USDT"

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    # Run analysis in thread to prevent blocking
    analysis = await asyncio.to_thread(build_full_analysis, symbol)

    if not analysis.get("success"):
        await update.message.reply_text(
            f"❌ {analysis.get('error', 'Failed to fetch data.')}",
        )
        return

    sr = analysis.get("support_resistance", {})
    patterns = analysis.get("candlestick_patterns", [])
    ob = analysis.get("order_book", {})
    fvg_1h = analysis.get("fvg_1h", [])

    # Format price display
    price = analysis["current_price"]
    price_fmt = f"{price:,.2f}" if price >= 1 else f"{price:.8f}"

    msg = (
        f"💰 <b>{analysis['symbol']}</b>\n\n"
        f"📍 <b>Price:</b> ${price_fmt}\n"
        f"📊 <b>24h Change:</b> {analysis.get('price_change_24h', 'N/A')}\n"
        f"📈 <b>24h High:</b> ${analysis.get('high_24h', 'N/A')}\n"
        f"📉 <b>24h Low:</b> ${analysis.get('low_24h', 'N/A')}\n"
        f"🔊 <b>24h Volume:</b> {analysis.get('volume_24h', 'N/A')}\n\n"
        f"<b>📐 Technical Indicators:</b>\n"
        f"  RSI(14): {analysis.get('rsi', 'N/A')}"
    )

    rsi_val = analysis.get('rsi')
    if rsi_val:
        if rsi_val > 70:
            msg += " ⚠️ Overbought"
        elif rsi_val < 30:
            msg += " ⚠️ Oversold"
    msg += "\n"

    msg += (
        f"  MACD: {analysis.get('macd', 'N/A')}\n"
        f"  EMA 20: {analysis.get('ema_20', 'N/A')}\n"
        f"  EMA 50: {analysis.get('ema_50', 'N/A')}\n\n"
    )

    if sr:
        msg += (
            f"<b>🟢 Support Zones:</b>\n"
            f"  S1: {sr.get('support_1')} | S2: {sr.get('support_2')}\n"
            f"<b>🔴 Resistance Zones:</b>\n"
            f"  R1: {sr.get('resistance_1')} | R2: {sr.get('resistance_2')}\n"
            f"  Pivot: {sr.get('pivot')}\n\n"
        )

    if patterns:
        msg += "<b>🕯 Candlestick Patterns:</b>\n"
        for p in patterns:
            msg += f"  {p}\n"
        msg += "\n"

    if fvg_1h:
        msg += "<b>📐 Fair Value Gaps (1H):</b>\n"
        for f in fvg_1h:
            msg += f"  {f['type']}: {f['bottom']} → {f['top']}\n"
        msg += "\n"

    if ob:
        msg += (
            f"<b>📚 Order Book:</b>\n"
            f"  {ob.get('pressure', 'N/A')}\n"
            f"  Bid/Ask Ratio: {ob.get('ratio', 'N/A')}\n"
        )

    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /chart command — generate and send annotated chart."""
    user = update.effective_user

    if not is_authorized(user.id):
        await unauthorized_handler(update, context)
        return

    if not context.args:
        await update.message.reply_text(
            "❓ Please specify a symbol.\nExample: <code>/chart BTCUSDT</code>",
            parse_mode="HTML",
        )
        return

    symbol = context.args[0].upper().replace("/", "")
    if not symbol.endswith(("USDT", "BUSD", "USDC", "BTC", "ETH")):
        symbol += "USDT"

    interval = context.args[1] if len(context.args) > 1 else "1h"

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO
    )

    result = await asyncio.to_thread(generate_chart_image, symbol, interval, 80)

    if result is None:
        await update.message.reply_text(
            f"❌ Could not generate chart for <b>{symbol}</b>. Please verify the symbol.",
            parse_mode="HTML",
        )
        return

    buf, analysis = result

    # Build caption
    sr = analysis.get("support_resistance", {})
    patterns = analysis.get("candlestick_patterns", [])
    price = analysis.get("current_price", "N/A")
    price_fmt = f"{price:,.2f}" if isinstance(price, (int, float)) and price >= 1 else f"{price}"

    caption = (
        f"📊 <b>{symbol} ({interval.upper()}) — Live Chart</b>\n\n"
        f"💰 Price: ${price_fmt}\n"
        f"📊 24h: {analysis.get('price_change_24h', 'N/A')}\n"
        f"RSI: {analysis.get('rsi', 'N/A')} | MACD: {analysis.get('macd', 'N/A')}\n\n"
    )

    if sr:
        caption += (
            f"🟢 S1: {sr.get('support_1')} | S2: {sr.get('support_2')}\n"
            f"🔴 R1: {sr.get('resistance_1')} | R2: {sr.get('resistance_2')}\n"
        )

    if patterns:
        caption += "\n🕯 Patterns: " + " | ".join(p.split("—")[0].strip() for p in patterns[:3])

    # Truncate caption if too long for Telegram (1024 chars for photo caption)
    if len(caption) > 1020:
        caption = caption[:1017] + "..."

    await update.message.reply_photo(
        photo=buf,
        caption=caption,
        parse_mode="HTML",
    )


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /news command."""
    user = update.effective_user

    if not is_authorized(user.id):
        await unauthorized_handler(update, context)
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    headlines = await asyncio.to_thread(fetch_crypto_news, 5)
    display = format_news_for_display(headlines)

    await update.message.reply_text(display, parse_mode="HTML", disable_web_page_preview=True)


# ─────────────────────────────────────────────
# ADMIN COMMANDS
# ─────────────────────────────────────────────


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: list all approved users."""
    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text("⛔ Admin only command.")
        return

    users = _load_allowed_users()
    if not users:
        await update.message.reply_text("No approved users.")
        return

    lines = ["👥 <b>Approved Users</b>\n"]
    for uid in sorted(users):
        marker = " 👑 (Admin)" if uid == ADMIN_TELEGRAM_ID else ""
        lines.append(f"  • <code>{uid}</code>{marker}")

    lines.append(f"\n<b>Total:</b> {len(users)} users")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: revoke user access."""
    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text("⛔ Admin only command.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/revoke USER_ID</code>",
            parse_mode="HTML",
        )
        return

    try:
        target_uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    if target_uid == ADMIN_TELEGRAM_ID:
        await update.message.reply_text("❌ Cannot revoke admin access.")
        return

    if target_uid not in allowed_users:
        await update.message.reply_text(f"User <code>{target_uid}</code> is not in the whitelist.", parse_mode="HTML")
        return

    revoke_user(target_uid)
    await update.message.reply_text(
        f"✅ Access revoked for user <code>{target_uid}</code>.",
        parse_mode="HTML",
    )

    try:
        await context.bot.send_message(
            chat_id=target_uid,
            text="⚠️ Your access to Crypto Intelligence Bot has been revoked by the administrator.",
        )
    except Exception:
        pass


# ─────────────────────────────────────────────
# MODULE 6: NATURAL LANGUAGE MESSAGE HANDLER
# ─────────────────────────────────────────────


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main message handler for natural language processing."""
    user = update.effective_user
    message = update.message

    if not message or not message.text:
        return

    if not is_authorized(user.id):
        await unauthorized_handler(update, context)
        return

    user_text = message.text.strip()

    if not user_text:
        return

    # Check if it's a chart request
    if is_chart_request(user_text):
        symbol = extract_symbol(user_text)
        if symbol:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO
            )
            result = await asyncio.to_thread(generate_chart_image, symbol, "1h", 80)

            if result:
                buf, analysis = result
                sr = analysis.get("support_resistance", {})
                price = analysis.get("current_price", "N/A")
                price_fmt = f"{price:,.2f}" if isinstance(price, (int, float)) and price >= 1 else f"{price}"

                caption = (
                    f"📊 <b>{symbol} (1H) — Live Analysis</b>\n\n"
                    f"💰 Price: ${price_fmt}\n"
                    f"📊 24h: {analysis.get('price_change_24h', 'N/A')}\n"
                    f"RSI: {analysis.get('rsi', 'N/A')}\n"
                )

                if sr:
                    caption += (
                        f"\n🟢 S1: {sr.get('support_1')} | S2: {sr.get('support_2')}\n"
                        f"🔴 R1: {sr.get('resistance_1')} | R2: {sr.get('resistance_2')}\n"
                    )

                patterns = analysis.get("candlestick_patterns", [])
                if patterns:
                    caption += "\n🕯 " + " | ".join(p.split("—")[0].strip() for p in patterns[:3])

                if len(caption) > 1020:
                    caption = caption[:1017] + "..."

                await message.reply_photo(photo=buf, caption=caption, parse_mode="HTML")

                # Also send AI commentary
                analysis_text = format_analysis_text(analysis)
                news = await asyncio.to_thread(fetch_crypto_news, 3)
                news_text = format_news_for_prompt(news)
                context_data = f"{analysis_text}\n\nRecent Crypto News:\n{news_text}"

                await context.bot.send_chat_action(
                    chat_id=update.effective_chat.id, action=ChatAction.TYPING
                )
                ai_reply = await call_groq_ai(user_text, context_data)
                await message.reply_text(ai_reply, parse_mode="Markdown")
                return
            else:
                await message.reply_text(f"❌ Could not generate chart for detected symbol. Please try /chart {symbol}")
                return
        else:
            # Chart requested but no symbol found
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, action=ChatAction.TYPING
            )
            ai_reply = await call_groq_ai(
                user_text,
                "The user seems to be asking for a chart but didn't specify a clear trading pair. Ask them to specify the symbol like BTCUSDT.",
            )
            await message.reply_text(ai_reply)
            return

    # Check if it's a market/analysis question with a detectable symbol
    symbol = extract_symbol(user_text)
    context_data = ""

    if symbol and is_market_question(user_text):
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action=ChatAction.TYPING
        )

        # Fetch live data for context
        analysis = await asyncio.to_thread(build_full_analysis, symbol)
        if analysis.get("success"):
            context_data = format_analysis_text(analysis)

        # Add news context
        news = await asyncio.to_thread(fetch_crypto_news, 3)
        news_text = format_news_for_prompt(news)
        context_data += f"\n\nRecent Crypto News:\n{news_text}"

    elif is_market_question(user_text):
        # Market question without specific symbol - add general news
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action=ChatAction.TYPING
        )
        news = await asyncio.to_thread(fetch_crypto_news, 5)
        news_text = format_news_for_prompt(news)
        context_data = f"Recent Crypto News:\n{news_text}"

        # Try to get BTC as general market indicator
        btc_analysis = await asyncio.to_thread(build_full_analysis, "BTCUSDT")
        if btc_analysis.get("success"):
            context_data = f"BTC Market Data (as general indicator):\n{format_analysis_text(btc_analysis)}\n\n{context_data}"
    else:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action=ChatAction.TYPING
        )

    # Call AI
    ai_reply = await call_groq_ai(user_text, context_data)

    # Split long messages (Telegram limit: 4096 chars)
    if len(ai_reply) <= 4096:
        try:
            await message.reply_text(ai_reply, parse_mode="Markdown")
        except Exception:
            # Fallback if Markdown parsing fails
            await message.reply_text(ai_reply)
    else:
        # Split into chunks
        chunks = [ai_reply[i:i + 4000] for i in range(0, len(ai_reply), 4000)]
        for chunk in chunks:
            try:
                await message.reply_text(chunk, parse_mode="Markdown")
            except Exception:
                await message.reply_text(chunk)


# ─────────────────────────────────────────────
# ERROR HANDLER
# ─────────────────────────────────────────────


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global error handler."""
    logger.error(f"Exception while handling update: {context.error}", exc_info=context.error)

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ An unexpected error occurred. Please try again."
            )
        except Exception:
            pass


# ─────────────────────────────────────────────
# MAIN APPLICATION
# ─────────────────────────────────────────────


def main():
    """Initialize and run the Telegram bot."""
    logger.info("🚀 Starting Crypto Intelligence Bot...")
    logger.info(f"Admin ID: {ADMIN_TELEGRAM_ID}")
    logger.info(f"Groq Model: {GROQ_MODEL} (Fallback: {GROQ_FALLBACK_MODEL})")
    logger.info(f"Whitelisted users: {len(allowed_users)}")

    # Build application
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .build()
    )

    # Register handlers (order matters!)
    # 1. Callback query handler for approve/reject buttons
    app.add_handler(CallbackQueryHandler(handle_approval_callback, pattern=r"^(approve|reject):"))

    # 2. Command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("chart", cmd_chart))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("revoke", cmd_revoke))

    # 3. Natural language message handler (must be last)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # 4. Error handler
    app.add_error_handler(error_handler)

    # Run the bot with polling
    logger.info("✅ Bot is now running!")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
