import os
import asyncio
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.getenv("BOT_TOKEN")

SYMBOLS = {
    "GBP/USD": "GBPUSD=X",
    "EUR/USD": "EURUSD=X",
    "Gold Futures": "GC=F"
}

CHAT_IDS = set()

def get_signal(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "5d", "interval": "5m"}
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        data = r.json()["chart"]["result"][0]
        closes = [
            x for x in data["indicators"]["quote"][0]["close"]
            if x is not None
        ]

        if len(closes) < 30:
            return "WAIT (not enough data)"

        prices = closes[-30:]
        changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains = [x if x > 0 else 0 for x in changes[-14:]]
        losses = [-x if x < 0 else 0 for x in changes[-14:]]

        avg_gain = sum(gains) / 14
        avg_loss = sum(losses) / 14

        if avg_loss == 0:
            rsi = 100
        else:
            rsi = 100 - (100 / (1 + avg_gain / avg_loss))

        ma_short = sum(prices[-10:]) / 10
        ma_long = sum(prices[-30:]) / 30

        if rsi < 30 and ma_short > ma_long:
            return f"BUY | RSI {rsi:.1f}"
        elif rsi > 70 and ma_short < ma_long:
            return f"SELL | RSI {rsi:.1f}"
        else:
            return f"WAIT | RSI {rsi:.1f}"

    except Exception as e:
        print("Data error:", symbol, e)
        return "WAIT (data unavailable)"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CHAT_IDS.add(update.effective_chat.id)
    await update.message.reply_text(
        "Forex signal bot active!\n"
        "Updates every 5 minutes.\n"
        "Use /signal to check now."
    )

async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CHAT_IDS.add(update.effective_chat.id)
    lines = ["Market analysis (5-minute data):"]

    for name, symbol in SYMBOLS.items():
        result = await asyncio.to_thread(get_signal, symbol)
        lines.append(f"{name}: {result}")

    lines.append("\nEducational signals only. Not financial advice.")
    await update.message.reply_text("\n".join(lines))

async def automatic_updates(app):
    while True:
        await asyncio.sleep(300)
        for chat_id in list(CHAT_IDS):
            try:
                lines = ["5-minute market update:"]
                for name, symbol in SYMBOLS.items():
                    result = await asyncio.to_thread(get_signal, symbol)
                    lines.append(f"{name}: {result}")

                lines.append("\nEducational signals only.")
                await app.bot.send_message(
                    chat_id=chat_id,
                    text="\n".join(lines)
                )
            except Exception as e:
                print("Send error:", e)

async def post_init(app):
    app.create_task(automatic_updates(app))

def main():
    if not TOKEN:
        print("BOT_TOKEN is missing")
        return

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", signal))

    print("Bot is running!")
    app.run_polling()

if __name__ == "__main__":
    main()
