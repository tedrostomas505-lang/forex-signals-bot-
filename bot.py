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

        if len(group) != 12:
            continue

        hourly.append({
            "time": key * 3600,
            "open": group[0]["open"],
            "high": max(c["high"] for c in group),
            "low": min(c["low"] for c in group),
            "close": group[-1]["close"]
        })

    return hourly


def make_4h(candles):
    groups = {}

    for candle in candles:
        key = candle["time"] // 14400
        groups.setdefault(key, []).append(candle)

    result = []

    for key in sorted(groups):
        group = groups[key]

        if len(group) != 4:
            continue

        result.append({
            "time": key * 14400,
            "open": group[0]["open"],
            "high": max(c["high"] for c in group),
            "low": min(c["low"] for c in group),
            "close": group[-1]["close"]
        })

    return result




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

        # Remove unfinished 5-minute candle
        five = candles[:-1]

        if len(five) < 250:
            return None

         # Build higher timeframes from 5-minute data
        hourly = make_hourly(five)
        four_hour = make_4h(hourly)

        def aggregate(data, minutes):
            size = minutes // 5
            result = []

            for i in range(0, len(data) - size + 1, size):
                group = data[i:i + size]

                if len(group) != size:
                    continue

                result.append({
                    "time": group[0]["time"],
                    "open": group[0]["open"],
                    "high": max(c["high"] for c in group),
                    "low": min(c["low"] for c in group),
                    "close": group[-1]["close"]
                })

            return result

        m30 = aggregate(five, 30)
        m15 = aggregate(five, 15)
        m5 = five

        if len(hourly) < 30 or len(m30) < 20 or len(m15) < 20:
            return None

         if len(hourly) < 30 or len(m30) < 20 or len(m15) < 20:
            return None

        # ICT liquidity sweep on 1H
        h = hourly[:-1]
        last_h = h[-1]
        previous_h = h[-21:-1]

        four = four_hour[:-1]
        if len(four) < 6:
            return None       # ICT liquidity sweep on 1H
h = hourly[:-1]
last_h = h[-1]
previous_h = h[-21:-1]

four = four_hour[:-1]
if len(four) < 6:
    return None

last_four = four[-1]
previous_four = four[-6:-1]

four_bullish = last_four["close"] > max(
    c["high"] for c in previous_four
)

four_bearish = last_four["close"] < min(
    c["low"] for c in previous_four
)
        if len(previous_h) < 20:
            return None

        old_high = max(c["high"] for c in previous_h)
        old_low = min(c["low"] for c in previous_h)

        buy_sweep = (
            last_h["low"] < old_low
            and last_h["close"] > old_low
        )

        sell_sweep = (
            last_h["high"] > old_high
            and last_h["close"] < old_high
        )

        # Market structure: BOS / CHoCH approximation
        recent = m15[:-1]
        if len(recent) < 10:
            return None

        last15 = recent[-1]
        structure = recent[-10:-1]

        structure_high = max(c["high"] for c in structure)
        structure_low = min(c["low"] for c in structure)

        buy_bos = last15["close"] > structure_high
        sell_bos = last15["close"] < structure_low

        # Fair Value Gap on 5M
        if len(m5) < 5:
            return None

        a = m5[-4]
        c = m5[-2]

        bullish_fvg = c["low"] > a["high"]
        bearish_fvg = c["high"] < a["low"]

        # Order block approximation
        previous = m5[-3]
        last5 = m5[-1]

        bullish_ob = (
            previous["close"] < previous["open"]
            and last5["close"] > previous["high"]
        )

        bearish_ob = (
            previous["close"] > previous["open"]
            and last5["close"] < previous["low"]
        )

        # Premium / Discount using recent 1H range
        range_high = max(x["high"] for x in h[-20:])
        range_low = min(x["low"] for x in h[-20:])
        midpoint = (range_high + range_low) / 2

        entry = last5["close"]
        digits = 3 if "JPY" in name else 5

        direction = None
        score = 0
        sweep = None

        # ICT + SMC BUY
        if buy_sweep:
            score += 25
        if buy_bos:
            score += 20
        if bullish_fvg:
            score += 20
        if bullish_ob:
            score += 15
        if entry < midpoint:
            score += 20

        if score >= 80 and buy_sweep and buy_bos and four_bullish:
            direction = "BUY"
            sweep = last_h["low"]

        # ICT + SMC SELL
        sell_score = 0
        if sell_sweep:
            sell_score += 25
        if sell_bos:
            sell_score += 20
        if bearish_fvg:
            sell_score += 20
        if bearish_ob:
            sell_score += 15
        if entry > midpoint:
            sell_score += 20

       if sell_score >= 80 and sell_sweep and sell_bos and four_bearish:
            if sell_score > score:
                direction = "SELL"
                score = sell_score
                sweep = last_h["high"]

        if direction is None:
            return None

        # Stop loss and 1:2 target
        if direction == "BUY":
            stop = min(sweep, min(x["low"] for x in m5[-4:]))
            stop -= entry * 0.0002
            risk = entry - stop
            target = entry + (risk * 2)
        else:
            stop = max(sweep, max(x["high"] for x in m5[-4:]))
            stop += entry * 0.0002
            risk = stop - entry
            target = entry - (risk * 2)

        if risk <= 0:
            return None

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
            f"ICT + SMC score: {score}/100\n"
            f"Timeframes: 4H + 1H + 30M + 15M + 5M\n"
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
