import os
import json
import asyncio
import requests
from datetime import datetime, time
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes
)

TOKEN = os.getenv("BOT_TOKEN")
TZ = ZoneInfo("Asia/Qatar")



SYMBOLS = {
    "EUR/USD": "EURUSD=X",
    "GBP/JPY": "GBPJPY=X"
}

CHAT_IDS = set()
LAST_SIGNALS = {}
TRADES_FILE = "trades.json"




def load_trades():
    try:
        with open(TRADES_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def save_trades(trades):
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2)


def trading_hours():
    now = datetime.now(TZ).time()
    return time(8, 0) <= now <= time(20, 0)


def get_candles(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "60d", "interval": "5m"}
    headers = {"User-Agent": "Mozilla/5.0"}

    response = requests.get(
        url, params=params, headers=headers, timeout=20
    )
    response.raise_for_status()

    result = response.json()["chart"]["result"][0]
    timestamps = result["timestamp"]
    quote = result["indicators"]["quote"][0]

    candles = []

    for i, stamp in enumerate(timestamps):
        try:
            values = [
                quote["open"][i],
                quote["high"][i],
                quote["low"][i],
                quote["close"][i]
            ]

            if all(v is not None for v in values):
                candles.append({
                    "time": stamp,
                    "open": values[0],
                    "high": values[1],
                    "low": values[2],
                    "close": values[3]
                })
        except (IndexError, TypeError):
            continue

    return candles


def make_hourly(candles):
    groups = {}

    for candle in candles:
        key = candle["time"] // 3600
        groups.setdefault(key, []).append(candle)

    hourly = []

    for key in sorted(groups):
        group = groups[key]
        hourly.append({
            "time": key * 3600,
            "open": group[0]["open"],
            "high": max(c["high"] for c in group),
            "low": min(c["low"] for c in group),
            "close": group[-1]["close"]
        })

    return hourly


def news_blocked():
    now = datetime.now(TZ)

    events = getattr(news_blocked, "_events", None)
    fetched_at = getattr(news_blocked, "_fetched_at", None)
    last_attempt = getattr(news_blocked, "_last_attempt", None)

    should_fetch = (
        last_attempt is None
        or (now - last_attempt).total_seconds() >= 1800
    )

    if should_fetch:
        news_blocked._last_attempt = now

        try:
            url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
            response = requests.get(url, timeout=10)
            response.raise_for_status()

            events = response.json()
            news_blocked._events = events
            news_blocked._fetched_at = now

        except Exception as error:
            print("News calendar error:", error)

            if (
                events is None
                or fetched_at is None
                or (now - fetched_at).total_seconds() > 6 * 3600
            ):
                return True

    events = getattr(news_blocked, "_events", None)

    if events is None:
        return True

    for event in events:
        if event.get("impact") != "High":
            continue

        if event.get("country") not in ("USD", "GBP", "EUR"):
            continue

        date_text = event.get("date")
        if not date_text:
            continue

        try:
            event_time = datetime.fromisoformat(
                date_text.replace("Z", "+00:00")
            ).astimezone(TZ)

            difference = abs((now - event_time).total_seconds())

            if difference <= 15 * 60:
                return True

        except Exception:
            continue

    return False


def get_signal(symbol, name):
    try:
        candles = get_candles(symbol)

        if len(candles) < 300:
            return None

        # Ignore unfinished 5-minute candle
        five = candles[:-1]

        # Build 1-hour candles
        hourly = make_hourly(five)

        # Ignore unfinished 1-hour candle
        hourly = hourly[:-1]

        if len(hourly) < 25 or len(five) < 20:
            return None

        last1h = hourly[-1]
        previous1h = hourly[-21:-1]

        support = min(c["low"] for c in previous1h)
        resistance = max(c["high"] for c in previous1h)

        last5 = five[-1]
        previous5 = five[-7:-1]

        break_high = max(c["high"] for c in previous5)
        break_low = min(c["low"] for c in previous5)

        direction = None
        sweep = None
        score = 0

        # BUY: 1H liquidity sweep and reclaim
        if (
            last1h["low"] < support
            and last1h["close"] > support
            and last5["close"] > break_high
        ):
            direction = "BUY"
            sweep = last1h["low"]
            score = 75

        # SELL: 1H liquidity sweep and reclaim
        elif (
            last1h["high"] > resistance
            and last1h["close"] < resistance
            and last5["close"] < break_low
        ):
            direction = "SELL"
            sweep = last1h["high"]
            score = 75

        if direction is None or score < 65:
            return None

        entry = last5["close"]

        if direction == "BUY":
            stop = min(sweep, min(c["low"] for c in five[-4:]))
            stop -= entry * 0.0002
            risk = entry - stop
            target = entry + (risk * 2)

        else:
            stop = max(sweep, max(c["high"] for c in five[-4:]))
            stop += entry * 0.0002
            risk = stop - entry
            target = entry - (risk * 2)

        if risk <= 0:
            return None

        digits = 2 if "Gold" in name else 5

        key = (
            direction,
            round(entry, digits),
            round(sweep, digits)
        )

        message = (
            f"{name}\n"
            f"Signal: {direction}\n\n"
            f"Entry: {entry:.{digits}f}\n"
            f"Stop Loss: {stop:.{digits}f}\n"
            f"Take Profit: {target:.{digits}f}\n\n"
            f"Setup score: {score}/100\n"
            f"Timeframes: 1H + 5M\n"
            f"Risk/Reward: 1:2\n"
            f"Planned risk: 1%\n\n"
            "Paper trading only. Not financial advice."
        )

        trade = {
            "symbol": name,
            "direction": direction,
            "entry": entry,
            "stop": stop,
            "target": target,
            "status": "OPEN",
            "date": datetime.now(TZ).date().isoformat()
        }

        return key, message, trade

    except Exception as error:
        print("Analysis error:", name, error)
        return None


def update_trade_results():
    trades = load_trades()
    changed = False

    for trade in trades:
        if trade["status"] != "OPEN":
            continue

        symbol = SYMBOLS.get(trade["symbol"])

        try:
            candles = get_candles(symbol)
            candle = candles[-1]

            if trade["direction"] == "BUY":
                hit_stop = candle["low"] <= trade["stop"]
                hit_target = candle["high"] >= trade["target"]
            else:
                hit_stop = candle["high"] >= trade["stop"]
                hit_target = candle["low"] <= trade["target"]

            # Conservative rule if both levels are touched
            if hit_stop:
                trade["status"] = "LOSS"
                changed = True
            elif hit_target:
                trade["status"] = "WIN"
                changed = True

        except Exception as error:
            print("Trade result error:", error)

    if changed:
        save_trades(trades)


async def daily_report(app):
    today = datetime.now(TZ).date().isoformat()
    trades = load_trades()

    todays = [t for t in trades if t["date"] == today]

    wins = sum(t["status"] == "WIN" for t in todays)
    losses = sum(t["status"] == "LOSS" for t in todays)
    opened = sum(t["status"] == "OPEN" for t in todays)

    net_r = (wins * 2) - losses

    report = (
        "DAILY REPORT\n\n"
        f"Trades: {len(todays)}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Still open: {opened}\n"
        f"Net result: {net_r}R\n"
        f"Estimated result at 1% risk: {net_r}%\n\n"
        "Paper-trading estimate only."
    )

    for chat_id in list(CHAT_IDS):
        await app.bot.send_message(chat_id, report)


async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CHAT_IDS.add(update.effective_chat.id)

    if not trading_hours():
        await update.message.reply_text(
            "Outside trading hours. Qatar time: 08:00-20:00."
        )
        return

    if await asyncio.to_thread(news_blocked):
        await update.message.reply_text(
            "Major news window or news calendar unavailable. No new signal."
        )
        return

    found = False

    for name, symbol in SYMBOLS.items():
        result = await asyncio.to_thread(get_signal, symbol, name)

        if result:
            key, message, trade = result

            if LAST_SIGNALS.get(name) != key:
                await update.message.reply_text(message)

                trades = load_trades()
                trades.append(trade)
                save_trades(trades)

                LAST_SIGNALS[name] = key
                found = True

    if not found:
        await update.message.reply_text(
            "No confirmed setup right now."
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CHAT_IDS.add(update.effective_chat.id)

    await update.message.reply_text(
        "Forex Signal Bot active!\n"
        "Analysis: 1H\n"
        "Entry: 5M\n"
        "Trading hours: 08:00-20:00 Qatar time.\n"
        "Use /signal to check."
    )


async def background(app):
    last_report_date = None

    while True:
        await asyncio.sleep(300)

        await asyncio.to_thread(update_trade_results)

        now = datetime.now(TZ)

        if now.hour == 20 and now.minute >= 5:
            if last_report_date != now.date():
                await daily_report(app)
                last_report_date = now.date()

        if not trading_hours():
            continue

        if await asyncio.to_thread(news_blocked):
            continue

        for name, symbol in SYMBOLS.items():
            result = await asyncio.to_thread(get_signal, symbol, name)

            if not result:
                continue

            key, message, trade = result

            if LAST_SIGNALS.get(name) == key:
                continue

            for chat_id in list(CHAT_IDS):
                try:
                    await app.bot.send_message(chat_id, message)
                except Exception as error:
                    print("Telegram send error:", error)

            trades = load_trades()
            trades.append(trade)
            save_trades(trades)

            LAST_SIGNALS[name] = key


async def post_init(app):
    app.create_task(background(app))


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

    print("Forex signal bot running!")
    app.run_polling()


if __name__ == "__main__":
    main()
