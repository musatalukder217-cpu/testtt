import os
import io
import asyncio
import logging
from datetime import datetime, timezone, timedelta
import feedparser
import requests
from groq import Groq
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.constants import ParseMode
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
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
PUBLIC_CHANNEL_ID = os.getenv("PUBLIC_CHANNEL_ID", "").strip()

OWNER_NAME = "—͞Tᴍ Mᴜsᴀ⚡️"
OWNER_USERNAME = "tmmusa73"

try:
    groq_client = Groq(api_key=GROQ_API_KEY)
except Exception as e:
    logger.error(f"Groq Client Init Failed: {e}")
    groq_client = None

GROQ_CHAT_MODEL = "openai/gpt-oss-120b"
GROQ_VOICE_MODEL = "whisper-large-v3"

APPROVED_CHAT_USERS = {ADMIN_ID}
BLOCKED_USERS = set()
PENDING_REQUEST_USERS = set()
APPROVED_CHANNELS = {PUBLIC_CHANNEL_ID} if PUBLIC_CHANNEL_ID else set()
user_chat_histories = {}
seen_news_ids = set()
LATEST_NEWS_CACHE = []
BINANCE_SYMBOLS = set()

WAITING_REJECT_TEXT = 1

RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed"
]

def load_binance_symbols():
    global BINANCE_SYMBOLS
    try:
        url = "https://api.binance.com/api/v3/exchangeInfo"
        data = requests.get(url, timeout=6).json()
        for s in data.get("symbols", []):
            if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT":
                BINANCE_SYMBOLS.add(s["baseAsset"])
    except Exception as e:
        logger.error(f"Failed to load Binance symbols: {e}")

load_binance_symbols()

def get_channel_english_date_str() -> str:
    bd_tz = timezone(timedelta(hours=6))
    now = datetime.now(bd_tz)
    return now.strftime("%d %B %Y")

def get_user_current_time_str() -> str:
    bd_tz = timezone(timedelta(hours=6))
    now = datetime.now(bd_tz)
    return now.strftime("%d %B %Y, %I:%M %p")

def format_clean_text(text: str) -> str:
    if not text:
        return ""
    return text.replace("**", "").replace("*", "").strip()

def is_pure_english(text: str) -> bool:
    if not text:
        return False
    if any('\u0980' <= c <= '\u09FF' for c in text):
        return False
    banglish_markers = ["ami", "tumi", "kemon", "acho", "accha", "shuno", "koro", "bolo", "hobe", "bujhte", "parcho", "kto", "koto", "ki", "korbo", "kida", "kide", "banaiche"]
    low = text.lower()
    if any(m in low.split() for m in banglish_markers):
        return False
    ascii_letters = sum(1 for c in text if c.isascii() and c.isalpha())
    return ascii_letters > (len(text) * 0.4)

def has_invalid_script(text: str) -> bool:
    for c in text:
        if '\u0370' <= c <= '\u03FF' or '\u1F00' <= c <= '\u1FFF':
            return True
    return False

def get_binance_top_gainers(limit=20):
    try:
        url = "https://api.binance.com/api/v3/ticker/24hr"
        res = requests.get(url, timeout=5).json()
        usdt_pairs = [item for item in res if item['symbol'].endswith('USDT')]
        sorted_pairs = sorted(usdt_pairs, key=lambda x: float(x['priceChangePercent']), reverse=True)
        top_list = []
        for p in sorted_pairs[:limit]:
            sym = p['symbol'].replace('USDT', '')
            change = float(p['priceChangePercent'])
            price = float(p['lastPrice'])
            top_list.append(f"{sym}: ${price:,.4f} (+{change:.2f}%)")
        return "\n[BINANCE VERIFIED TOP GAINERS (LAST 24H)]:\n" + "\n".join(top_list) + "\n"
    except Exception as e:
        logger.error(f"Top gainers error: {e}")
        return ""

def extract_crypto_symbol(text: str) -> str:
    clean = text.lower().replace("?", " ").replace("/", " ").replace("-", " ")
    aliases = {
        "ইথুরেষাম": "ETH", "ইথেরিয়াম": "ETH", "ইথেরিয়াম": "ETH", "ইথিরিয়াম": "ETH", "ether": "ETH", "ethereum": "ETH",
        "বিটকয়েন": "BTC", "বিটকয়েনের": "BTC", "বিটকোইন": "BTC", "bitcoin": "BTC", "btc": "BTC",
        "সোলানা": "SOL", "সোলেয়ানা": "SOL", "solana": "SOL", "sol": "SOL",
        "বাইনান্স": "BNB", "বিএনবি": "BNB", "bnb": "BNB",
        "ডজ": "DOGE", "ডগকয়েন": "DOGE", "doge": "DOGE",
        "পেপে": "PEPE", "pepe": "PEPE", "শিবা": "SHIB", "shib": "SHIB",
        "রিপল": "XRP", "xrp": "XRP", "কার্ডানো": "ADA", "ada": "ADA",
        "সুই": "SUI", "sui": "SUI", "নিয়ার": "NEAR", "near": "NEAR",
        "পোলকাডট": "DOT", "dot": "DOT", "অ্যাভাক্স": "AVAX", "avax": "AVAX",
        "পলিগন": "POL", "ম্যাটিক": "POL", "matic": "POL", "pol": "POL"
    }

    for key, sym in aliases.items():
        if key in clean:
            return sym

    tokens = clean.upper().split()
    for token in tokens:
        clean_tok = token.replace("USDT", "")
        if clean_tok in BINANCE_SYMBOLS:
            return clean_tok
    return ""

def get_market_data_for_query(query_text: str):
    low = query_text.lower()
    if any(k in low for k in ["top gainer", "sob cheye up", "সবচেয়ে বেশি আপ", "সবচেয়ে আপ", "বেশি আপ", "top coin", "টপ কয়েন", "top 20", "২০ টা কয়েন"]):
        return get_binance_top_gainers(20)

    sym = extract_crypto_symbol(query_text)
    if sym:
        target_symbol = f"{sym}USDT"
        try:
            url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={target_symbol}"
            res = requests.get(url, timeout=4).json()
            if 'lastPrice' in res:
                price = float(res['lastPrice'])
                high = float(res['highPrice'])
                low_p = float(res['lowPrice'])
                change = float(res['priceChangePercent'])
                return f"\n[REAL-TIME LIVE BINANCE DATA: {target_symbol} | Current Price: \({price:,.4f} | 24h High:\){high:,.4f} | 24h Low: ${low_p:,.4f} | 24h Change: {change}%]\n"
        except Exception:
            pass
    return ""

async def send_large_text_reply(update: Update, waiting_msg, full_text: str):
    max_len = 3900
    if len(full_text) <= max_len:
        try:
            await waiting_msg.edit_text(full_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception:
            await waiting_msg.edit_text(full_text, disable_web_page_preview=True)
        return

    parts = []
    while len(full_text) > max_len:
        split_idx = full_text.rfind("\n\n", 0, max_len)
        if split_idx == -1:
            split_idx = full_text.rfind("\n", 0, max_len)
        if split_idx == -1:
            split_idx = max_len
        parts.append(full_text[:split_idx].strip())
        full_text = full_text[split_idx:].strip()
    if full_text:
        parts.append(full_text)

    try:
        await waiting_msg.edit_text(parts[0], parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        await waiting_msg.edit_text(parts[0], disable_web_page_preview=True)

    for part in parts[1:]:
        try:
            await update.message.reply_text(part, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception:
            await update.message.reply_text(part, disable_web_page_preview=True)

# -----------------------------------------------------------------------------
# অডিও ও এআই ইঞ্জিন
# -----------------------------------------------------------------------------

def transcribe_audio_file(audio_bytes: bytes) -> str:
    if not groq_client:
        return ""

    acoustic_prompt = (
        "বাঙালি আঞ্চলিক উপভাষা, ইথেরিয়াম, বিটকয়েন, সোলানা, ক্যান্ডেলস্টিক, ভলিউম, "
        "Ethereum, Bitcoin, Binance, TradingView, support, resistance, SUI, DOGE, PEPE."
    )

    try:
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = "voice.ogg"
        transcription = groq_client.audio.transcriptions.create(
            file=audio_file,
            model=GROQ_VOICE_MODEL,
            response_format="text",
            prompt=acoustic_prompt
        )
        res_text = transcription.strip()

        if has_invalid_script(res_text):
            audio_file.seek(0)
            retry_transcription = groq_client.audio.transcriptions.create(
                file=audio_file,
                model=GROQ_VOICE_MODEL,
                response_format="text",
                language="bn",
                prompt="বাংলায় ক্রিপ্টো এবং সাধারণ প্রশ্ন।"
            )
            res_text = retry_transcription.strip()

        return res_text
    except Exception as e:
        logger.error(f"Voice Transcription Error: {e}")
        return ""

def analyze_crypto_news(title: str, summary: str):
    if not groq_client:
        return None

    system_prompt = (
        "You are an institutional crypto market analyst. "
        "Strict Formatting Rules:\n"
        "1. ONLY the Headline line MUST be translated into a powerful, fluent, and attractive BENGALI headline.\n"
        "2. ALL OTHER TEXT, labels, analysis, impact, and summary MUST be strictly in professional ENGLISH.\n"
        "3. Evaluate if the news is a genuine catalyst or routine noise. Do NOT blindly label everything bullish/bearish.\n"
        "4. Use clean, balanced emojis (🟢, 🔴, ⚪, ⚠️, 📊, 📝). Never use markdown asterisks (**).\n\n"
        "Strict Output Format:\n"
        "📢 [এখানে সংবাদের মূল শিরোনামের স্পষ্ট বাংলা অনুবাদ]\n\n"
        "📊 Market Impact: [Bullish 🟢 / Bearish 🔴 / Neutral ⚪ / High Volatility ⚠️]\n"
        "📁 Event Type: [Single Major Catalyst / Macro Regulatory / Routine Noise]\n"
        "💡 Potential Outlook: [1-2 concise lines in English detailing price or liquidity implications]\n"
        "📝 Key Summary: [1 concise sentence in English summarizing the core verified event]"
    )
    user_prompt = f"Original Title: {title}\nOriginal Summary: {summary}"

    try:
        response = groq_client.chat.completions.create(
            model=GROQ_CHAT_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.2,
            max_tokens=350
        )
        return format_clean_text(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"Groq News Analysis Error: {e}")
        return None

def analyze_user_crypto_query(user_id: int, user_text: str):
    if not groq_client:
        return "⚠️ এআই ত্রুটি: GROQ_API_KEY কনফিগার করা হয়নি।"

    low_text = user_text.lower().replace("?", "").strip()
    creator_queries = [
        "কে বানিয়েছে", "কে তৈরি করেছে", "who made you", "who created you", "who is your creator",
        "owner কে", "তৈরি কে করেছে", "কার বট", "কে বানাইসে", "কে বানাইছে", "কার তৈরি", "maker",
        "who built you", "tuhe kisne banaya", "tumhe kisne banaya", "কেনে বানাইল", "খনে বানাইসে",
        "tomake k baniyeche", "tomake k banise", "tomar owner k", "tomar malik k", "tomake banaiche kida", "banaiche kida"
    ]
    if any(q in low_text for q in creator_queries):
        owner_html = f'[{OWNER_NAME}](https://t.me/{OWNER_USERNAME})'
        if is_pure_english(user_text):
            return f"I was created by {owner_html}."
        elif "kida" in low_text or "kide" in low_text:
            return f"আঁরে বানাইয়ে {owner_html}।"
        else:
            return f"আমাকে তৈরি করেছেন {owner_html}।"

    greetings_map = {
        "hi": "Hello! How can I assist you with crypto, technical analysis, or markets today?",
        "hello": "Hello! How can I assist you with crypto, technical analysis, or markets today?",
        "হাই": "হ্যালো! ক্রিপ্টো মার্কেট, ক্যান্ডেলস্টিক বা চার্ট নিয়ে আপনাকে কীভাবে সাহায্য করতে পারি?",
        "হ্যালো": "হ্যালো! ক্রিপ্টো মার্কেট, ক্যান্ডেলস্টিক বা চার্ট নিয়ে আপনাকে কীভাবে সাহায্য করতে পারি?",
        "কেমন আছো": "আমি ভালো আছি, ধন্যবাদ! আপনার ট্রেডিং কেমন চলছে?",
        "kemon acho": "আমি ভালো আছি, ধন্যবাদ! আপনার ট্রেডিং কেমন চলছে?",
        "tumi kemon acho": "আমি চমৎকার আছি! আপনার ক্রিপ্টো সংক্রান্ত কোনো আপডেট লাগবে?",
        "tumi ki amar kotha bujhte parcho": "হ্যাঁ, আমি আপনার কথা পুরোপুরি বুঝতে পারছি। ক্রিপ্টো মার্কেট বা চার্ট নিয়ে যেকোনো প্রশ্ন করুন।"
    }
    for g_key, g_reply in greetings_map.items():
        if g_key in low_text:
            return g_reply

    market_data = get_market_data_for_query(user_text)
    history = user_chat_histories.get(user_id, [])
    current_time_str = get_user_current_time_str()

    recent_news_context = ""
    if market_data and LATEST_NEWS_CACHE:
        recent_news_context = "\n[LIVE BREAKING NEWS HAPPENING RIGHT NOW]:\n" + "\n".join(
            [f"- {n['title']}: {n['summary'][:150]}..." for n in LATEST_NEWS_CACHE[:2]]
        ) + "\n"

    system_instruction = (
        f"You are the Ultimate Real-Time Crypto Intelligence, Technical Analyst & Educational Brain. Current Live Date/Time: {current_time_str}.\n"
        f"Creator Info: You were created by {OWNER_NAME} (Telegram: https://t.me/{OWNER_USERNAME}).\n\n"
        "ENCYCLOPEDIC CRYPTO KNOWLEDGE BASE:\n"
        "1. Technical Analysis & Price Action: Comprehensive mastery of Candlestick patterns (Doji, Hammer, Engulfing, Morning/Evening Star, Three White Soldiers), Smart Money Concepts (SMC, Order Blocks, Liquidity Sweeps, Fair Value Gap / FVG, Break of Structure / BOS, Change of Character / CHoCH).\n"
        "2. Indicators & Volume: Relative Strength Index (RSI Divergences), MACD, Bollinger Bands, Moving Averages (EMA 20, 50, 100, 200), Fibonacci Retracements/Extensions, Cumulative Volume Delta (CVD), Volume Spread Analysis (VSA), Open Interest (OI), Funding Rates, Liquidation Heatmaps.\n"
        "3. Market Cap & Indices: TOTAL (Total Crypto Cap), TOTAL2 (ex-BTC), TOTAL3 (Altcoin Market Cap ex-BTC & ETH), BTC Dominance (BTC.D), USDT Dominance (USDT.D), FDV (Fully Diluted Valuation), Tokenomics, and Whale On-chain Flow.\n"
        "4. Exchanges & Practical Trading: Spot Trading, Futures Trading (Leverage, Cross/Isolated Margin, Long/Short liquidation management, Stop-Loss/Take-Profit) across Binance, Bybit, Bitget, KuCoin, MEXC, OKX, and DEXs (Uniswap, Raydium).\n"
        "5. Real-Time Data Handling: When [BINANCE VERIFIED TOP GAINERS] or [REAL-TIME LIVE BINANCE DATA] is provided, you have full live market feed access! Never say you lack live data. Directly present the live figures.\n"
        "6. Language & Dialects: Romanized Bengali (Banglish) is strictly Bengali. Match the user's language and regional dialect. Never use Devanagari Hindi for Banglish.\n"
        "7. Tone: Direct, expert, concise, authoritative, and completely free of markdown asterisks (**)."
    )

    messages = [{"role": "system", "content": system_instruction}]
    for h in history[-3:]:
        messages.append({"role": h["role"], "content": h["text"]})

    current_content = f"{market_data}{recent_news_context}\nUser Input: {user_text}"
    messages.append({"role": "user", "content": current_content})

    try:
        response = groq_client.chat.completions.create(
            model=GROQ_CHAT_MODEL,
            messages=messages,
            temperature=0.2,
            max_tokens=850
        )
        reply = format_clean_text(response.choices[0].message.content)
        history.append({"role": "user", "text": user_text})
        history.append({"role": "assistant", "text": reply})
        user_chat_histories[user_id] = history
        return reply
    except Exception as e:
        logger.error(f"Groq Chat Error: {e}")
        return f"⚠️ Error: {str(e)[:120]}"

# -----------------------------------------------------------------------------
# অ্যাডমিন প্যানেল ও ইউজার ব্লক/আনব্লক ম্যানেজমেন্ট
# -----------------------------------------------------------------------------

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ আপনি বটের মূল অ্যাডমিন নন।")
        return

    current_time_str = get_user_current_time_str()
    channels_list = "\n".join([f"• {c}" for c in APPROVED_CHANNELS if c]) or "None"

    panel_text = (
        "👑 **Admin Control Dashboard**\n"
        f"📅 Date: {current_time_str}\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"📢 **Approved Channels:**\n{channels_list}\n\n"
        "👥 **User Management:**\nনিচে অনুমোদিত ইউজারদের ব্লক বা আনব্লক করতে বাটনে চাপুন:"
    )

    keyboard = []
    for uid in list(APPROVED_CHAT_USERS):
        if uid == ADMIN_ID:
            continue
        keyboard.append([
            InlineKeyboardButton(f"🚫 Block {uid}", callback_data=f"user_block_{uid}")
        ])

    for b_uid in list(BLOCKED_USERS):
        keyboard.append([
            InlineKeyboardButton(f"✅ Unblock {b_uid}", callback_data=f"user_unblock_{b_uid}")
        ])

    if not keyboard:
        keyboard = [[InlineKeyboardButton("ℹ️ কোনো সাধারণ ইউজার নেই", callback_data="none")]]

    await update.message.reply_text(panel_text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if user.id in BLOCKED_USERS:
        await update.message.reply_text("🚫 দুঃখিত, আপনাকে এই বট থেকে ব্লক করা হয়েছে।")
        return

    if user.id in APPROVED_CHAT_USERS:
        await update.message.reply_text(
            f"স্বাগতম {user.first_name}! আমি আপনার সর্বজনীন ক্রিপ্টো, ট্রেডিং ও টেকনিক্যাল এআই।\n"
            "ক্যান্ডেলস্টিক, ভলিউম, চার্ট প্যাটার্ন, টপ গেইনার বা ফিউচার ট্রেডিং নিয়ে যেকোনো প্রশ্ন করতে পারেন।"
        )
    else:
        if user.id in PENDING_REQUEST_USERS:
            await update.message.reply_text("⏳ আপনার এক্সেস রিকোয়েস্ট ইতিমধ্যে বিবেচনাধীন রয়েছে। অনুগ্রহ করে অপেক্ষা করুন।")
            return

        PENDING_REQUEST_USERS.add(user.id)
        await update.message.reply_text("⛔ আপনি অনুমোদিত ইউজার নন। অ্যাডমিনের কাছে এক্সেস রিকোয়েস্ট পাঠানো হয়েছে।")
        
        keyboard = [[
            InlineKeyboardButton("✅ Confirm", callback_data=f"user_confirm_{user.id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"user_reject_{user.id}")
        ]]
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"👤 New Access Request\nUser: @{user.username} (ID: {user.id})\nName: {user.full_name}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if user.id in BLOCKED_USERS:
        await update.message.reply_text("🚫 আপনি এই বটের এক্সেস থেকে ব্লকড রয়েছেন।")
        return

    if user.id not in APPROVED_CHAT_USERS:
        if user.id in PENDING_REQUEST_USERS:
            await update.message.reply_text("⏳ আপনার রিকোয়েস্ট বিবেচনাধীন রয়েছে।")
        else:
            await start_command(update, context)
        return

    user_query = update.message.text
    if is_pure_english(user_query):
        wait_text = "🔍 Analyzing live market..."
    else:
        wait_text = "🔍 লাইভ মার্কেট ও টেকনিক্যাল চার্ট যাচাই করছি..."

    waiting_msg = await update.message.reply_text(wait_text)
    reply_text = analyze_user_crypto_query(user.id, user_query)
    await send_large_text_reply(update, waiting_msg, reply_text)

async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if user.id in BLOCKED_USERS:
        await update.message.reply_text("🚫 আপনি এই বটের এক্সেস থেকে ব্লকড রয়েছেন।")
        return

    if user.id not in APPROVED_CHAT_USERS:
        if user.id in PENDING_REQUEST_USERS:
            await update.message.reply_text("⏳ আপনার রিকোয়েস্ট বিবেচনাধীন রয়েছে।")
        else:
            await start_command(update, context)
        return

    voice = update.message.voice or update.message.audio
    if not voice:
        return

    waiting_msg = await update.message.reply_text("🎙️ ভয়েস শুনছি...")

    try:
        tg_file = await context.bot.get_file(voice.file_id)
        audio_stream = io.BytesIO()
        await tg_file.download_to_memory(audio_stream)
        audio_bytes = audio_stream.getvalue()

        transcribed_text = transcribe_audio_file(audio_bytes)
        if not transcribed_text:
            await waiting_msg.edit_text("⚠️ কথাটি পরিষ্কারভাবে শোনা যায়নি। অনুগ্রহ করে আবার বলুন।")
            return

        is_eng = is_pure_english(transcribed_text)
        if is_eng:
            header = f"🗣️ Your voice: \"{transcribed_text}\"\n\n"
        else:
            header = f"🗣️ আপনার কথা: \"{transcribed_text}\"\n\n"

        reply_text = analyze_user_crypto_query(user.id, transcribed_text)
        await send_large_text_reply(update, waiting_msg, f"{header}{reply_text}")
    except Exception as e:
        logger.error(f"Voice Handle Error: {e}")
        await waiting_msg.edit_text(f"⚠️ Error: {str(e)[:100]}")

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
                    f"Channel: {chat.title} (ID: {chat.id})\n"
                    f"Added By: @{added_by.username} (ID: {added_by.id})\n\n"
                    "Approve this channel for broadcasts?"
                ),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

async def handle_button_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("user_confirm_"):
        uid = int(data.split("_")[2])
        APPROVED_CHAT_USERS.add(uid)
        PENDING_REQUEST_USERS.discard(uid)
        BLOCKED_USERS.discard(uid)
        await query.edit_message_text(f"✅ User {uid} approved successfully.")
        try:
            await context.bot.send_message(chat_id=uid, text="🎉 অ্যাডমিন আপনার এক্সেস অনুমোদন করেছেন! এখন থেকে আপনি বট ব্যবহার করতে পারেন।")
        except Exception:
            pass

    elif data.startswith("user_reject_"):
        uid = int(data.split("_")[2])
        PENDING_REQUEST_USERS.discard(uid)
        await query.edit_message_text(f"❌ User {uid} rejected. (Status reset: User can re-apply).")
        try:
            await context.bot.send_message(chat_id=uid, text="⛔ দুঃখিত, আপনার এক্সেস রিকোয়েস্ট বাতিল করা হয়েছে।")
        except Exception:
            pass

    elif data.startswith("user_block_"):
        uid = int(data.split("_")[2])
        APPROVED_CHAT_USERS.discard(uid)
        BLOCKED_USERS.add(uid)
        await query.edit_message_text(f"🚫 User {uid} has been BLOCKED. They cannot use the bot.")
        try:
            await context.bot.send_message(chat_id=uid, text="🚫 আপনার এক্সেস অ্যাডমিন কর্তৃক ব্লক করা হয়েছে।")
        except Exception:
            pass

    elif data.startswith("user_unblock_"):
        uid = int(data.split("_")[2])
        BLOCKED_USERS.discard(uid)
        APPROVED_CHAT_USERS.add(uid)
        await query.edit_message_text(f"✅ User {uid} has been UNBLOCKED and approved.")
        try:
            await context.bot.send_message(chat_id=uid, text="🎉 আপনাকে আনব্লক করা হয়েছে! এখন পুনরায় বট ব্যবহার করতে পারেন।")
        except Exception:
            pass

    elif data.startswith("chnl_approve_"):
        cid = int(data.split("_")[2])
        APPROVED_CHANNELS.add(cid)
        await query.edit_message_text(f"✅ Channel {cid} approved (silently active).")

    elif data.startswith("chnl_reject_"):
        cid = int(data.split("_")[2])
        context.user_data['target_channel_to_leave'] = cid
        await query.edit_message_text("❌ Channel rejected. Enter custom leave message:")
        return WAITING_REJECT_TEXT

    return ConversationHandler.END

async def receive_custom_leave_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    custom_text = update.message.text
    target_channel_id = context.user_data.get('target_channel_to_leave')

    if target_channel_id:
        owner_html = f'[{OWNER_NAME}](https://t.me/{OWNER_USERNAME})'
        final_msg = (
            f"{format_clean_text(custom_text)}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Owner: {owner_html}"
        )
        try:
            await context.bot.send_message(
                chat_id=target_channel_id,
                text=final_msg,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.error(f"Leave send error: {e}")

        try:
            await context.bot.leave_chat(chat_id=target_channel_id)
            await update.message.reply_text("🚀 Bot has left the channel.")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Error leaving: {e}")

        context.user_data.pop('target_channel_to_leave', None)

    return ConversationHandler.END

# -----------------------------------------------------------------------------
# ব্যাকগ্রাউন্ড স্ক্যানার
# -----------------------------------------------------------------------------

async def background_market_scanner(app):
    global LATEST_NEWS_CACHE
    await asyncio.sleep(5)
    logger.info("Market scanner active. Monitoring feeds...")

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

                    LATEST_NEWS_CACHE.insert(0, {"title": title, "summary": summary})
                    LATEST_NEWS_CACHE = LATEST_NEWS_CACHE[:10]

                    analysis = analyze_crypto_news(title, summary)
                    if not analysis:
                        analysis = (
                            f"📢 {title}\n\n"
                            f"📊 Market Impact: Neutral ⚪\n"
                            f"📁 Event Type: Routine News\n"
                            f"💡 Potential Outlook: Market stability expected.\n"
                            f"📝 Key Summary: General crypto sector update."
                        )

                    channel_date = get_channel_english_date_str()
                    broadcast_text = (
                        f"📰 Breaking News Update\n"
                        f"📅 {channel_date}\n\n"
                        f"{analysis}\n\n"
                        f"🔗 Source: {link}\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"Automated Crypto Intelligence"
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
                            logger.error(f"Broadcast error: {ex}")
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

                        LATEST_NEWS_CACHE.insert(0, {"title": title, "summary": summary})
                        LATEST_NEWS_CACHE = LATEST_NEWS_CACHE[:10]

                        analysis = analyze_crypto_news(title, summary)
                        if not analysis:
                            analysis = (
                                f"📢 {title}\n\n"
                                f"📊 Market Impact: Neutral ⚪\n"
                                f"📁 Event Type: Routine News\n"
                                f"💡 Potential Outlook: Market stability expected.\n"
                                f"📝 Key Summary: General crypto sector update."
                            )

                        channel_date = get_channel_english_date_str()
                        broadcast_text = (
                            f"📰 Breaking News Update\n"
                            f"📅 {channel_date}\n\n"
                            f"{analysis}\n\n"
                            f"🔗 Source: {link}\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"Automated Crypto Intelligence"
                        )

                        for ch_id in list(APPROVED_CHANNELS):
                            try:
                                await app.bot.send_message(
                                    chat_id=ch_id,
                                    text=broadcast_text,
                                    disable_web_page_preview=True
                                )
                            except Exception as ex:
                                logger.error(f"Loop dispatch error: {ex}")
                        await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Loop error: {e}")

        await asyncio.sleep(300)

# -----------------------------------------------------------------------------
# মেইন অ্যাপ
# -----------------------------------------------------------------------------

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
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.VOICE | filters.AUDIO), handle_voice_message))

    loop = asyncio.get_event_loop()
    loop.create_task(background_market_scanner(app))

    logger.info("Bot is active with Complete Institutional Technical Brain & User Ban System...")
    app.run_polling()

if __name__ == "__main__":
    main()
