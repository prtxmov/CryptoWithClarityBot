# Telegram Payment Bot

## Features
- Telegram commands: `/start`, `/buy10`, `/buy20`, `/buy50`, `/help`, `/about`, `/earn`
- Generates MaxelPay payment links
- Flask webhook endpoint `/webhook`
- Deployable on Render/Heroku

## Setup
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Set environment variables:
   ```bash
   export TELEGRAM_BOT_TOKEN=your_telegram_token
   export MAXELPAY_API_KEY=your_api_key
   export MAXELPAY_SECRET_KEY=your_secret
   ```
3. Run locally:
   ```bash
   python final_bot1.py
   ```
4. Deploy on Render/Heroku with `Procfile`.

