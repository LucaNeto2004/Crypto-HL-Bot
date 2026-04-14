"""
Daily adaptive stops job for the crypto bot.

Fetches 90d of 5m candles for ETH and HYPE and writes
data/adaptive_stops.json using the shared builder.

Runs nightly via cron. Also safe to run manually:
    .venv/bin/python scripts/daily_adaptive_stops.py
"""
import os
import sys
import json

BOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)

SHARED_DIR = os.path.abspath(os.path.join(BOT_DIR, "..", "shared"))
if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(BOT_DIR, ".env"))

from core.data import DataManager
from config.settings import BotConfig
from adaptive_stops import build_and_write


SYMBOLS = ["ETH", "HYPE"]

# Current deployed ATR stop multipliers (from config/deployed/*.json)
BASE_SL_PER_SYMBOL = {
    "ETH": 0.7,
    "HYPE": 0.7,
}

OUTPUT = os.path.join(BOT_DIR, "data", "adaptive_stops.json")


def main() -> int:
    config = BotConfig()
    data = DataManager(config)

    def fetcher(symbol: str):
        tf = getattr(config, "symbol_timeframes", {}).get(symbol, config.candle_interval)
        return data.fetch_candles(symbol, interval=tf)

    result = build_and_write(SYMBOLS, fetcher, OUTPUT, base_sl_per_symbol=BASE_SL_PER_SYMBOL)

    print(f"Wrote {OUTPUT}")
    print(json.dumps(result, indent=2, default=str))

    paused = [s for s, p in result["symbols"].items() if p.get("pause")]
    if paused:
        print(f"\nPAUSED: {paused}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
