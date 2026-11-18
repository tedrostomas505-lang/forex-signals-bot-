import os
import json
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================================================
# FOREX CRT SIGNAL BOT
# Symbols: EUR/USD and GBP/JPY
# Signals only. No automatic trading or order execution.
#
# CRT rules:
# 1) Previous completed UTC 4H candle = CRT range (High/Low).
# 2) During the current 4H block, price must sweep one side and
#    close back inside the previous 4H range on a completed 15m bar.
# 3) A 15m CHoCH in the reversal direction is mandatory.
# 4) A 15m Order Block OR Fair Value Gap is required.
# 5) Price must retest that zone; a later 15m confirmation candle
#    must close in the trade direction before a signal is sent.
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
logger = logging.getLogger("forex_crt_bot")

CHAT_IDS = set()
LAST_SIGNALS = {}
NEWS_CACHE = {"checked": None, "events": None}
LAST_REPORT_DATE = None


# ------------------------- MARKET DATA ----------------------

def get_candles(symbol):
    """Download completed 15-minute Yahoo Finance candles."""
    params = {"range": "60d", "interval": "15m"}
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
    candles = [
        c for c in candles
        if c["time"] + timedelta(minutes=15) <= datetime.now(UTC)
    ]
    if len(candles) < 100:
        raise ValueError("Not enough completed 15-minute candles received.")
    return candles


def build_4h_candles(candles_15m):
    """Build only fully populated UTC-aligned 4H bars."""
    groups = {}
    for candle in candles_15m:
        stamp = int(candle["time"].timestamp())
        bucket = (stamp // 14400) * 14400
        groups.setdefault(bucket, []).append(candle)

    output = []
    for bucket in sorted(groups):
        group = sorted(groups[bucket], key=lambda x: x["time"])
        expected_times = [
            datetime.fromtimestamp(bucket + i * 900, UTC)
            for i in range(16)
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


# ------------------------- CRT HELPERS ----------------------

def find_choch(candles, direction, sweep_index, lookback=5):
    """
    Find the first completed 15m close through the opposing local
    structure after the CRT sweep/reclaim candle.
    """
    for i in range(sweep_index + 1, len(candles)):
        prior = candles[max(sweep_index, i - lookback):i]
        if len(prior) < 2:
            continue
        if direction == "BUY" and candles[i]["close"] > max(x["high"] for x in prior):
            return i
        if direction == "SELL" and candles[i]["close"] < min(x["low"] for x in prior):
            return i
    return None


def find_fvg(candles, direction, start_index, end_index):
    """Find the newest three-candle FVG formed within the index range."""
    for i in range(end_index, start_index + 1, -1):
        if i < 2:
            continue
        first = candles[i - 2]
        third = candles[i]
        if direction == "BUY" and third["low"] > first["high"]:
            return {
                "low": first["high"],
                "high": third["low"],
                "time": third["time"],
                "kind": "FVG",
            }
        if direction == "SELL" and third["high"] < first["low"]:
            return {
                "low": third["high"],
                "high": first["low"],
                "time": third["time"],
                "kind": "FVG",
            }
    return None


def find_order_block(candles, direction, start_index, choch_index):
    """
    Find the latest opposite-colour candle before the CHoCH candle
    whose high/low is broken by the CHoCH move.
    """
    for i in range(choch_index - 1, max(start_index - 1, choch_index - 9), -1):
        candle = candles[i]
        if direction == "BUY" and candle["close"] < candle["open"]:
            if candles[choch_index]["close"] > candle["high"]:
                return {
                    "low": candle["low"],
                    "high": candle["open"],
                    "time": candle["time"],
                    "kind": "Order Block",
                }
        if direction == "SELL" and candle["close"] > candle["open"]:
            if candles[choch_index]["close"] < candle["low"]:
                return {
                    "low": candle["open"],
                    "high": candle["high"],
                    "time": candle["time"],
                    "kind": "Order Block",
                }
    return None


def zone_after_choch(candles, direction, sweep_index, choch_index):
    """Use the most recent valid 15m OB or FVG; either one is enough."""
    ob = find_order_block(candles, direction, sweep_index + 1, choch_index)
    fvg = find_fvg(candles, direction, sweep_index + 1, choch_index)

    choices = [z for z in (ob, fvg) if z and z["low"] < z["high"]]
    if not choices:
        return None
    return max(choices, key=lambda z: z["time"])


def detect_crt_setup(candles_15m, candles_4h, direction):
    """
    Return a signal only when the latest closed 15m candle is the
    confirmation candle following a retest of the selected zone.
    """
    if len(candles_4h) < 2 or len(candles_15m) < 30:
        return None, "Not enough completed 4H/15M candles."

    latest_15m = candles_15m[-1]
    current_4h_bucket = (
        int(latest_15m["time"].timestamp()) // 14400
    ) * 14400

    # The range is the immediately preceding completed 4H candle.
    previous_4h = next(
        (
            c for c in reversed(candles_4h)
            if int(c["time"].timestamp()) == current_4h_bucket - 14400
        ),
        None,
    )
    if previous_4h is None:
        return None, "Previous completed 4H CRT range is not available."

    range_high = previous_4h["high"]
    range_low = previous_4h["low"]

    current_block = [
        c for c in candles_15m
        if int(c["time"].timestamp()) >= current_4h_bucket
    ]
    if len(current_block) < 3:
        return None, "Waiting for more 15M candles in the current 4H block."

    # Locate a sweep and reclaim of one edge of the previous 4H range.
    sweep_index = None
    for i, candle in enumerate(candles_15m):
        if int(candle["time"].timestamp()) < current_4h_bucket:
            continue
        if direction == "BUY":
            swept = candle["low"] < range_low and range_low < candle["close"] < range_high
        else:
            swept = candle["high"] > range_high and range_low < candle["close"] < range_high
        if swept:
            sweep_index = i

    if sweep_index is None:
        return None, f"No {direction} CRT sweep/reclaim of the previous 4H range."

    choch_index = find_choch(candles_15m, direction, sweep_index)
    if choch_index is None:
        return None, "Waiting for mandatory 15M CHoCH."

    zone = zone_after_choch(candles_15m, direction, sweep_index, choch_index)
    if zone is None:
        return None, "No valid 15M Order Block or FVG after CHoCH."

    # The zone must be revisited by a later closed candle. The latest
    # candle must be a separate confirmation candle after that retest.
    retest_index = None
    for i in range(choch_index + 1, len(candles_15m) - 1):
        candle = candles_15m[i]
        if candle["time"] <= zone["time"]:
            continue
        touched = (
            candle["low"] <= zone["high"]
            and candle["high"] >= zone["low"]
        )
        if touched:
            retest_index = i

    if retest_index is None or retest_index != len(candles_15m) - 2:
        return None, "Waiting for a fresh 15M zone retest, then confirmation candle."

    confirmation = candles_15m[-1]
    retest = candles_15m[retest_index]

    if direction == "BUY":
        confirmed = (
            confirmation["close"] > confirmation["open"]
            and confirmation["close"] > zone["high"]
            and confirmation["close"] > retest["high"]
        )
    else:
        confirmed = (
            confirmation["close"] < confirmation["open"]
            and confirmation["close"] < zone["low"]
            and confirmation["close"] < retest["low"]
        )

    if not confirmed:
        return None, "Retest seen; waiting for a closed 15M confirmation candle."

    entry = confirmation["close"]
    recent = candles_15m[max(0, len(candles_15m) - 5):]
    buffer = entry * 0.0001

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

    return {
        "direction": direction,
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "zone_low": zone["low"],
        "zone_high": zone["high"],
        "zone_type": zone["kind"],
        "crt_high": range_high,
        "crt_low": range_low,
        "signal_time": confirmation["time"].isoformat(),
        "signal_key": (
            f"{confirmation['time'].isoformat()}|{direction}|"
            f"{zone['kind']}|{zone['low']:.6f}|{zone['high']:.6f}"
        ),
    }, ""


# ------------------------- NEWS FILTER ----------------------

def get_high_impact_events():
    """Cache calendar for 30 minutes; unavailable calendar blocks signals."""
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
                return True, f"High impact news: {event.get('title', 'High impact news')}"
        except (TypeError, ValueError):
            continue
    return False, ""


# ------------------------- SIGNAL ENGINE --------------------

def get_signal(pair, yahoo_symbol):
    candles_15m = get_candles(yahoo_symbol)
    candles_4h = build_4h_candles(candles_15m)

    buy_signal, buy_reason = detect_crt_setup(
        candles_15m, candles_4h, "BUY"
    )
    sell_signal, sell_reason = detect_crt_setup(
        candles_15m, candles_4h, "SELL"
    )

    if buy_signal and sell_signal:
        return None, "Conflicting BUY and SELL CRT conditions."
    if not buy_signal and not sell_signal:
        return None, f"BUY: {buy_reason} | SELL: {sell_reason}"

    result = buy_signal or sell_signal
    result["pair"] = pair
    return result, ""


# ------------------------- TRADE JOURNAL --------------------

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
            candles = get_candles(symbol)
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

                # Conservative rule: if both are hit in one 15m bar, count loss.
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
        f"Trades: {len(todays)}\nWins: {wins}\nLosses: {losses}\nOpen: {opened}\n"
        f"Net result: {net_r:+.1f}R\n"
        f"Estimated result: {estimated_percent:+.1f}% "
        "(paper estimate at 1% risk per trade)\n\n"
        "Note: This is a journal estimate, not verified broker P/L."
    )


# ------------------------- TELEGRAM -------------------------

def is_trading_time():
    now = datetime.now(QATAR)
    return TRADING_START_HOUR <= now.hour < TRADING_END_HOUR


def format_signal(signal):
    decimals = 3 if signal["pair"] == "GBP/JPY" else 5
    return (
        f"📈 CRT SIGNAL — {signal['pair']}\n\n"
        f"Direction: {signal['direction']}\n"
        "Setup: Previous 4H candle range sweep + 15M CHoCH\n"
        f"15M entry zone: {signal['zone_type']}\n"
        f"CRT range: {signal['crt_low']:.{decimals}f} – "
        f"{signal['crt_high']:.{decimals}f}\n"
        f"Entry zone: {signal['zone_low']:.{decimals}f} – "
        f"{signal['zone_high']:.{decimals}f}\n\n"
        f"Entry: {signal['entry']:.{decimals}f}\n"
        f"Stop Loss: {signal['stop_loss']:.{decimals}f}\n"
        f"Take Profit: {signal['take_profit']:.{decimals}f}\n\n"
        f"Risk plan: {RISK_PERCENT:.1f}% | RR 1:{REWARD_RISK:.0f}\n"
        "15M retest and confirmation candle closed.\n"
        "Manual trading only. Check the price on Exness before entry.\n"
        "Data source: Yahoo Finance; price may differ from Exness."
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CHAT_IDS.add(update.effective_chat.id)
    await update.message.reply_text(
        "Welcome to the Forex CRT Signal Bot.\n\n"
        "Pairs: EUR/USD and GBP/JPY only\n"
        "CRT range: Previous completed 4H candle High/Low\n"
        "Confirmation: 15M sweep/reclaim + CHoCH\n"
        "Entry: 15M Order Block OR FVG retest + closed confirmation candle\n"
        "Risk plan: 1% | Risk/Reward: 1:2\n"
        "Trading hours: 02:00–23:00 Qatar time\n"
        "Daily report: 23:30 Qatar time\n\n"
        "Commands:\n"
        "/signal — check for a confirmed CRT setup\n"
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

    await update.message.reply_text("Checking EUR/USD and GBP/JPY CRT setup...")
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
            record_signal(signal)
            LAST_SIGNALS[pair] = key
            await update.message.reply_text(format_signal(signal))
            sent += 1
        except Exception:
            logger.exception("Signal check failed for %s", pair)

    if sent == 0:
        await update.message.reply_text(
            "No confirmed CRT setup right now.\n"
            "The bot waits for the full 4H range sweep, 15M CHoCH, "
            "OB or FVG retest, and a closed confirmation candle."
        )


# ------------------------- BACKGROUND -----------------------

async def background_tasks(application: Application):
    global LAST_REPORT_DATE

    while True:
        try:
            await asyncio.to_thread(update_trade_results)
        except Exception:
            logger.exception("Trade update task failed.")

        if is_trading_time():
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
                    record_signal(signal)
                    LAST_SIGNALS[pair] = key
                    for chat_id in list(CHAT_IDS):
                        try:
                            await application.bot.send_message(
                                chat_id=chat_id,
                                text=format_signal(signal),
                            )
                        except Exception:
                            logger.exception("Could not send signal to %s", chat_id)
                except Exception:
                    logger.exception("Automatic signal check failed for %s", pair)

        now = datetime.now(QATAR)
        today = now.date()
        if (
            now.hour == DAILY_REPORT_HOUR
            and now.minute >= DAILY_REPORT_MINUTE
            and LAST_REPORT_DATE != today
        ):
            try:
                report = await asyncio.to_thread(make_daily_report)
                for chat_id in list(CHAT_IDS):
                    try:
                        await application.bot.send_message(chat_id=chat_id, text=report)
                    except Exception:
                        logger.exception("Could not send daily report to %s", chat_id)
                LAST_REPORT_DATE = today
            except Exception:
                logger.exception("Daily report failed.")

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

    logger.info("Forex CRT bot is starting.")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
