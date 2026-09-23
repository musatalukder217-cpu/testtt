# Crypto AI Telegram Bot — Final Railway Package

## Required Railway Variables
TELEGRAM_BOT_TOKEN
ADMIN_TELEGRAM_IDS
GROQ_API_KEY

## Optional
COINGECKO_API_KEY
BINANCE_API_KEY
BINANCE_API_SECRET

## Deploy
1. Upload these files to GitHub.
2. Railway -> New Project -> Deploy from GitHub Repo.
3. Add the variables above in Railway Variables.
4. Start command: `python bot.py`.
5. Deploy.

## Changed only as requested
- New-user approval now uses Approve/Reject inline buttons.
- All users get only Start in the Telegram menu; admin IDs get the admin menu.
- Bengali crypto names such as ইথেরিয়াম/ইথেরিয়াম are resolved.
- AI replies are cleaned to plain text (Markdown stars/backticks removed).
- Long replies are automatically split into multiple complete Telegram messages.
- Answers are restricted to the exact user question.
- Requests for a chart/photo generate a Binance 1H market chart with candles, SMA20/SMA50 and support/resistance.
- Existing Stage 1 + Stage 2 functionality is retained.
- CryptoPanic is not required; news uses public RSS feeds.
