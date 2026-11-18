import os
import json
import logging
import asyncio
from datetime import datetime, timedelta, timezone, time as dt_time
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes
)

# ============================================================
# FOREX SMC SIGNAL BOT
# Symbols: EUR/USD and GBP/JPY
# Signals only. No automatic trading or order execution.
# ============================================================

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing.")

SYMBOLS = {
    "EUR/USD": "EURUSD=X",
    "GBP/JPY": "GBPJPY=X",
}

QATAR = ZoneInfo("Asia/Qatar")
UTC = timezone.utc

RISK_PERCENT = 1.0
REWARD_RISK = 2.0
MIN_SCORE = 70
TRADING_START_HOUR = 2
TRADING_END_HOUR = 23
DAILY_REPORT_HOUR = 23
DAILY_REPORT_MINUTE = 30

DATA_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
TRADES_FILE = "trades.json"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("forex_smc_bot")

CHAT_IDS = set()
LAST_SIGNALS = {}
NEWS_CACHE = {"checked": None, "events": None}
LAST_REPORT_DATE = None


# ------------------------- DATA -----------------------------

def get_candles(symbol):
    """Download 5-minute candles from Yahoo Finance."""
    params = {"range": "60d", "interval": "5m"}
    headers = {"User-Agent": "Mozilla/5.0"}

    response = requests.get(
        DATA_URL.format(symbol),
        params=params,
        headers=headers,
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()

    result = payload.get("chart", {}).get("result")
    if not result:
        raise ValueError("Yahoo Finance returned no candle data.")

    item = result[0]
    timestamps = item.get("timestamp") or []
    quote = item.get("indicators", {}).get("quote", [{}])[0]

    candles = []
    for i, stamp in enumerate(timestamps):
        try:
            o = quote["open"][i]
            h = quote["high"][i]
            low = quote["low"][i]
            c = quote["close"][i]
            if None in (o, h, low, c):
                continue
            candles.append({
                "time": datetime.fromtimestamp(stamp, UTC),
                "open": float(o),
                "high": float(h),
                "low": float(low),
                "close": float(c),
            })
        except (IndexError, TypeError, ValueError):
            continue

    candles.sort(key=lambda x: x["time"])
    if len(candles) < 300:
        raise ValueError("Not enough 5-minute candles received.")
    return candles


def completed_5m_candles(candles):
    """Exclude the currently forming 5-minute candle."""
    now = datetime.now(UTC)
    return [c for c in candles if c["time"] + timedelta(minutes=5) <= now]


def build_timeframe(candles, minutes):
    """Aggregate complete, UTC-aligned bars from 5-minute candles."""
    step = minutes * 60
    expected = minutes // 5
    groups = {}

    for candle in candles:
        stamp = int(candle["time"].timestamp())
        bucket = (stamp // step) * step
        groups.setdefault(bucket, []).append(candle)

    output = []
    for bucket in sorted(groups):
        group = sorted(groups[bucket], key=lambda x: x["time"])
        if len(group) != expected:
            continue

        # Ensure every 5-minute candle is present and consecutive.
        expected_times = [
            datetime.fromtimestamp(bucket + i * 300, UTC)
            for i in range(expected)
        ]
        if [x["time"] for x in group] != expected_times:
            continue

        output.append({
            "time": datetime.fromtimestamp(bucket, UTC),
            "open": group[0]["open"],
            "high": max(x["high"] for x in group),
            "low": min(x["low"] for x in group),
            "close": group[-1]["close"],
        })

    return output


# ---------------------- SMC HELPERS -------------------------

def bullish_bos(candles, lookback=10):
    if len(candles) < lookback + 2:
        return False
    previous = candles[-lookback-1:-1]
    current = candles[-1]
    return current["close"] > max(x["high"] for x in previous)


def bearish_bos(candles, lookback=10):
    if len(candles) < lookback + 2:
        return False
    previous = candles[-lookback-1:-1]
    current = candles[-1]
    return current["close"] < min(x["low"] for x in previous)


def find_fvg(candles, direction, lookback=8):
    """Return the newest three-candle FVG (simple mechanical definition)."""
    if len(candles) < 3:
        return None

    start = max(2, len(candles) - lookback)
    for i in range(len(candles) - 1, start - 1, -1):
        first = candles[i - 2]
        third = candles[i]

        if direction == "BUY" and third["low"] > first["high"]:
            return {
                "low": first["high"],
                "high": third["low"],
                "time": third["time"],
            }

        if direction == "SELL" and third["high"] < first["low"]:
            return {
                "low": third["high"],
                "high": first["low"],
                "time": third["time"],
            }

    return None


def liquidity_sweep(candles, direction, recent=3, lookback=10):
    """Wick takes a prior high/low, then candle closes back inside."""
    if len(candles) < recent + lookback + 1:
        return False

    prior = candles[-(recent + lookback):-recent]
    recent_candles = candles[-recent:]
    prior_high = max(x["high"] for x in prior)
    prior_low = min(x["low"] for x in prior)

    if direction == "BUY":
        return any(
            x["low"] < prior_low and x["close"] > prior_low
            for x in recent_candles
        )

    return any(
        x["high"] > prior_high and x["close"] < prior_high
        for x in recent_candles
    )


def find_order_block(candles, direction, lookback=12):
    """Find an opposite candle before a later close breaks its range."""
    if len(candles) < 4:
        return None

    start = max(0, len(candles) - lookback - 1)
    for i in range(len(candles) - 2, start - 1, -1):
        candle = candles[i]
        later = candles[i + 1:]

        if direction == "BUY" and candle["close"] < candle["open"]:
            if any(x["close"] > candle["high"] for x in later):
                return {
                    "low": candle["low"],
                    "high": candle["open"],
                    "time": candle["time"],
                }

        if direction == "SELL" and candle["close"] > candle["open"]:
            if any(x["close"] < candle["low"] for x in later):
                return {
                    "low": candle["open"],
                    "high": candle["high"],
                    "time": candle["time"],
                }

    return None


def find_entry_zone(candles, direction):
    """The 5m entry area is the overlap of the latest OB and FVG."""
    ob = find_order_block(candles, direction)
    fvg = find_fvg(candles, direction, lookback=8)

    if not ob or not fvg:
        return None

    low = max(ob["low"], fvg["low"])
    high = min(ob["high"], fvg["high"])

    if low >= high:
        return None

    return {
        "low": low,
        "high": high,
        "ob_time": ob["time"],
        "fvg_time": fvg["time"],
    }


# ---------------------- NEWS FILTER -------------------------

def get_high_impact_events():
    """Cache the calendar for 30 minutes. Fail closed if unavailable."""
    now = datetime.now(UTC)
    checked = NEWS_CACHE["checked"]

    if checked and NEWS_CACHE["events"] is not None:
        if now - checked < timedelta(minutes=30):
            return NEWS_CACHE["events"]

    response = requests.get(
        NEWS_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    response.raise_for_status()
    events = response.json()

    if not isinstance(events, list) or not events:
        raise ValueError("Economic calendar is empty or invalid.")

    NEWS_CACHE["checked"] = now
    NEWS_CACHE["events"] = events
    return events



def is_news_blocked(pair):
    currencies = {
        "EUR/USD": {"EUR", "USD"},
        "GBP/JPY": {"GBP"},
    }.get(pair, set())

    try:
        events = get_high_impact_events()
    except Exception as exc:
        logger.warning("Calendar unavailable; blocking signals: %s", exc)
        return True, "Economic calendar unavailable"

    now = datetime.now(UTC)

    for event in events:
        try:
            if str(event.get("impact", "")).lower() != "high":
                continue
            currency = str(event.get("country", "")).upper()
            if currency not in currencies:
                continue

            date_text = event.get("date")
            if not date_text:
                continue

            event_time = datetime.fromisoformat(
                str(date_text).replace("Z", "+00:00")
            )
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=UTC)
            else:
                event_time = event_time.astimezone(UTC)

            if abs((event_time - now).total_seconds()) <= 15 * 60:
                title = event.get("title", "High impact news")
                return True, f"High impact news: {title}"
        except (TypeError, ValueError):
            continue

    return False, ""


# ---------------------- SIGNAL ENGINE -----------------------

def get_signal(pair, yahoo_symbol):
    candles_5m = completed_5m_candles(get_candles(yahoo_symbol))
    candles_15m = build_timeframe(candles_5m, 15)
    candles_4h = build_timeframe(candles_5m, 240)

    if len(candles_15m) < 20 or len(candles_4h) < 12 or len(candles_5m) < 100:
        return None, "Not enough completed candles."

    # The newest completed 5m candle is used only for retest confirmation.
    zone_history = candles_5m[:-1]
    retest_candle = candles_5m[-1]

    buy_score = 0
    sell_score = 0

    # 4H: BOS and FVG are both mandatory.
    h4_buy_bos = bullish_bos(candles_4h)
    h4_sell_bos = bearish_bos(candles_4h)
    h4_buy_fvg = find_fvg(candles_4h, "BUY", lookback=8)
    h4_sell_fvg = find_fvg(candles_4h, "SELL", lookback=8)

    if h4_buy_bos and h4_buy_fvg:
        buy_score += 25
    if h4_sell_bos and h4_sell_fvg:
        sell_score += 25

    # 15m: structure break and liquidity sweep are both mandatory.
    m15_buy_bos = bullish_bos(candles_15m)
    m15_sell_bos = bearish_bos(candles_15m)
    m15_buy_sweep = liquidity_sweep(candles_15m, "BUY")
    m15_sell_sweep = liquidity_sweep(candles_15m, "SELL")

    if m15_buy_bos and m15_buy_sweep:
        buy_score += 25
    if m15_sell_bos and m15_sell_sweep:
        sell_score += 25

    # 5m: overlapping OB + FVG, formed before the retest candle.
    buy_zone = find_entry_zone(zone_history, "BUY")
    sell_zone = find_entry_zone(zone_history, "SELL")

    if buy_zone:
        buy_score += 30
    if sell_zone:
        sell_score += 30

    # Retest must happen after both the OB and FVG have formed.
    if buy_zone:
        zone_ready = retest_candle["time"] > max(
            buy_zone["ob_time"], buy_zone["fvg_time"]
        )
        touched = (
            retest_candle["low"] <= buy_zone["high"]
            and retest_candle["high"] >= buy_zone["low"]
        )
        rejected = retest_candle["close"] > buy_zone["high"]
        if zone_ready and touched and rejected:
            buy_score += 10
        else:
            buy_zone = None

    if sell_zone:
        zone_ready = retest_candle["time"] > max(
            sell_zone["ob_time"], sell_zone["fvg_time"]
        )
        touched = (
            retest_candle["high"] >= sell_zone["low"]
            and retest_candle["low"] <= sell_zone["high"]
        )
        rejected = retest_candle["close"] < sell_zone["low"]
        if zone_ready and touched and rejected:
            sell_score += 10
        else:
            sell_zone = None

    # Require every timeframe condition, not score alone.
    buy_valid = (
        h4_buy_bos and h4_buy_fvg
        and m15_buy_bos and m15_buy_sweep
        and buy_zone is not None and buy_score >= MIN_SCORE
    )
    sell_valid = (
        h4_sell_bos and h4_sell_fvg
        and m15_sell_bos and m15_sell_sweep
        and sell_zone is not None and sell_score >= MIN_SCORE
    )

    if buy_valid and sell_valid:
        return None, "Conflicting BUY and SELL conditions."

    if not buy_valid and not sell_valid:
        return None, "No confirmed SMC setup right now."

    direction = "BUY" if buy_valid else "SELL"
    score = buy_score if buy_valid else sell_score
    zone = buy_zone if buy_valid else sell_zone

    entry = retest_candle["close"]
    buffer = entry * 0.0002

    recent = candles_5m[-8:]
    if direction == "BUY":
        stop_loss = min(zone["low"], min(x["low"] for x in recent)) - buffer
        risk_distance = entry - stop_loss
        take_profit = entry + risk_distance * REWARD_RISK
    else:
        stop_loss = max(zone["high"], max(x["high"] for x in recent)) + buffer
        risk_distance = stop_loss - entry
        take_profit = entry - risk_distance * REWARD_RISK

    if risk_distance <= 0 or take_profit <= 0:
        return None, "Invalid risk calculation."

    signal = {
        "pair": pair,
        "direction": direction,
        "score": score,
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "zone_low": zone["low"],
        "zone_high": zone["high"],
        "signal_time": retest_candle["time"].isoformat(),
        "signal_key": (
            f"{pair}|{direction}|{retest_candle['time'].isoformat()}|"
            f"{zone['low']:.6f}|{zone['high']:.6f}"
        ),
    }
    return signal, ""


# ---------------------- TRADE JOURNAL -----------------------

def load_trades():
    try:
        with open(TRADES_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def save_trades(trades):
    temp_file = TRADES_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(trades, file, indent=2)
    os.replace(temp_file, TRADES_FILE)


def record_signal(signal):
    trades = load_trades()
    trades.append({
        **signal,
        "status": "OPEN",
        "result_r": None,
        "created_at": datetime.now(UTC).isoformat(),
    })
    save_trades(trades)


def update_trade_results():
    trades = load_trades()
    changed = False

    for trade in trades:
        if trade.get("status") != "OPEN":
            continue

        symbol = SYMBOLS.get(trade.get("pair"))
        if not symbol:
            continue

        try:
            candles = completed_5m_candles(get_candles(symbol))
            entry_time = datetime.fromisoformat(trade["signal_time"])
            if entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=UTC)

            later_candles = [c for c in candles if c["time"] > entry_time]
            direction = trade["direction"]
            sl = float(trade["stop_loss"])
            tp = float(trade["take_profit"])

            for candle in later_candles:
                if direction == "BUY":
                    hit_sl = candle["low"] <= sl
                    hit_tp = candle["high"] >= tp
                else:
                    hit_sl = candle["high"] >= sl
                    hit_tp = candle["low"] <= tp

                # If SL and TP are both touched in one candle, count it as loss.
                if hit_sl:
                    trade["status"] = "LOSS"
                    trade["result_r"] = -1
                    trade["closed_at"] = candle["time"].isoformat()
                    changed = True
                    break
                if hit_tp:
                    trade["status"] = "WIN"
                    trade["result_r"] = REWARD_RISK
                    trade["closed_at"] = candle["time"].isoformat()
                    changed = True
                    break

        except Exception as exc:
            logger.warning("Could not update trade %s: %s", trade.get("pair"), exc)

    if changed:
        save_trades(trades)


def make_daily_report():
    trades = load_trades()
    today = datetime.now(QATAR).date()

    todays = []
    for trade in trades:
        try:
            created = datetime.fromisoformat(trade["created_at"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if created.astimezone(QATAR).date() == today:
                todays.append(trade)
        except (KeyError, TypeError, ValueError):
            continue

    wins = sum(1 for x in todays if x.get("status") == "WIN")
    losses = sum(1 for x in todays if x.get("status") == "LOSS")
    opened = sum(1 for x in todays if x.get("status") == "OPEN")
    net_r = sum(float(x.get("result_r") or 0) for x in todays)
    estimated_percent = net_r * RISK_PERCENT

    return (
        f"📊 Daily report — {today.isoformat()} (Qatar)\n\n"
        f"Trades: {len(todays)}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Open: {opened}\n"
        f"Net result: {net_r:+.1f}R\n"
        f"Estimated result: {estimated_percent:+.1f}% "
        f"(paper estimate at 1% risk per trade)\n\n"
        "Note: This is a journal estimate, not verified broker P/L."
    )


# ---------------------- TELEGRAM ----------------------------

def is_trading_time():
    now = datetime.now(QATAR)
    return TRADING_START_HOUR <= now.hour < TRADING_END_HOUR


def format_signal(signal):
    decimals = 3 if signal["pair"] == "GBP/JPY" else 5
    return (
        f"📈 SMC SIGNAL — {signal['pair']}\n\n"
        f"Direction: {signal['direction']}\n"
        f"Score: {signal['score']}/100 "
        "(setup score, NOT win probability)\n\n"
        f"Entry: {signal['entry']:.{decimals}f}\n"
        f"Stop Loss: {signal['stop_loss']:.{decimals}f}\n"
        f"Take Profit: {signal['take_profit']:.{decimals}f}\n"
        f"Entry zone: {signal['zone_low']:.{decimals}f} – "
        f"{signal['zone_high']:.{decimals}f}\n\n"
        f"Risk plan: {RISK_PERCENT:.1f}% | RR 1:{REWARD_RISK:.0f}\n"
        "Manual trading only. Check the price on Exness before entry.\n"
        "Data source: Yahoo Finance; price may differ from Exness."
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    CHAT_IDS.add(chat_id)

    await update.message.reply_text(
        "Welcome to the Forex SMC Signal Bot.\n\n"
        "Pairs: EUR/USD and GBP/JPY only\n"
        "Analysis: 4H + 15M\n"
        "Entry: 5M OB + FVG retest\n"
        "Risk plan: 1% | Risk/Reward: 1:2\n"
        "Trading hours: 02:00–23:00 Qatar time\n"
        "Daily report: 23:30 Qatar time\n\n"
        "Commands:\n"
        "/signal — check for a confirmed setup\n"
        "/start — register this chat for reports\n\n"
        "Signals only; the bot does not place trades."
    )


async def signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    CHAT_IDS.add(chat_id)

    if not is_trading_time():
        await update.message.reply_text(
            "Trading time is closed. Qatar hours: 02:00–23:00."
        )
        return

    await update.message.reply_text("Checking EUR/USD and GBP/JPY SMC setup...")

    sent = 0
    for pair, symbol in SYMBOLS.items():
        try:
            blocked, reason = is_news_blocked(pair)
            if blocked:
                logger.info("%s blocked: %s", pair, reason)
                continue

            signal, message = await asyncio.to_thread(get_signal, pair, symbol)
            if not signal:
                logger.info("%s: %s", pair, message)
                continue

            key = signal["signal_key"]
            if LAST_SIGNALS.get(pair) == key:
                continue

            # Save journal entry before sending the signal.
            record_signal(signal)
            LAST_SIGNALS[pair] = key
            await update.message.reply_text(format_signal(signal))
            sent += 1

        except Exception:
            logger.exception("Signal check failed for %s", pair)

    if sent == 0:
        await update.message.reply_text(
            "No confirmed setup right now.\n"
            "The bot waits for every required SMC condition and the 5M retest."
        )


# ---------------------- BACKGROUND --------------------------

async def background_tasks(application: Application):
    global LAST_REPORT_DATE

    while True:
        try:
            await asyncio.to_thread(update_trade_results)
        except Exception:
            logger.exception("Trade update task failed.")

        now = datetime.now(QATAR)
        today = now.date()

        if (
            now.hour == DAILY_REPORT_HOUR
            and now.minute >= DAILY_REPORT_MINUTE
            and LAST_REPORT_DATE != today
        ):
            report = await asyncio.to_thread(make_daily_report)
            for chat_id in list(CHAT_IDS):
                try:
                    await application.bot.send_message(
                        chat_id=chat_id,
                        text=report,
                    )
                except Exception:
                    logger.exception("Could not send daily report to %s", chat_id)
            LAST_REPORT_DATE = today

        await asyncio.sleep(60)


async def post_init(application: Application):
    application.create_task(background_tasks(application))


def main():
    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("signal", signal_command))

    logger.info("Forex SMC bot is starting.")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
