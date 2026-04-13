"""
Data Module — Fetches and stores market data from HyperLiquid.
Handles candles, order books, funding rates, and mid prices.

Uses raw HTTP for HIP-3 deployer perps (km:, xyz:, flx: prefixed symbols)
and the SDK for native perps (PAXG, etc).
"""
import time
from typing import Optional

import pandas as pd
import requests
from hyperliquid.info import Info
from hyperliquid.utils import constants

from config.settings import BotConfig
from utils.logger import setup_logger

log = setup_logger("data")

API_URL = "https://api.hyperliquid.xyz/info"


class DataManager:
    def __init__(self, config: BotConfig):
        self.config = config
        self.base_url = constants.TESTNET_API_URL if config.testnet else constants.MAINNET_API_URL
        # SDK for native perps (account state, orders, etc.)
        try:
            self.info = Info(self.base_url, skip_ws=True)
        except Exception:
            self.info = None
            log.warning("SDK init failed — using raw HTTP only")
        self.candle_cache: dict[str, pd.DataFrame] = {}
        self.mid_prices: dict[str, float] = {}

    def _api_post(self, payload: dict, retries: int = 3, use_network_url: bool = False) -> Optional[dict]:
        """Raw HTTP POST to HyperLiquid info endpoint with retry.
        use_network_url: if True, use testnet/mainnet base_url instead of hardcoded mainnet.
        """
        url = (self.base_url + "/info") if use_network_url else API_URL
        for attempt in range(retries):
            try:
                resp = requests.post(url, json=payload, timeout=15)
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError:
                        log.warning(f"Invalid JSON response: {resp.text[:200]}")
                        continue
                if resp.status_code == 429:
                    wait = 2 ** (attempt + 1)
                    log.warning(f"Rate limited (429) — waiting {wait}s before retry ({attempt + 1}/{retries})")
                    time.sleep(wait)
                    continue
                log.warning(f"API returned {resp.status_code}: {resp.text[:200]}")
            except Exception as e:
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    log.warning(f"API request failed (attempt {attempt + 1}/{retries}): {e} — retrying in {wait}s")
                    time.sleep(wait)
                else:
                    log.error(f"API request failed after {retries} attempts: {e}")
        return None

    def fetch_candles(self, symbol: str, interval: Optional[str] = None,
                      lookback: Optional[int] = None) -> pd.DataFrame:
        """Fetch OHLCV candle data for a symbol."""
        interval = interval or self.config.candle_interval
        lookback = lookback or self.config.lookback_candles

        try:
            raw = self._api_post({
                "type": "candleSnapshot",
                "req": {"coin": symbol, "interval": interval, "startTime": 0},
            })

            if not raw:
                log.warning(f"No candle data for {symbol}")
                cached = self.candle_cache.get(symbol, pd.DataFrame())
                if not cached.empty:
                    log.debug(f"Using cached candles for {symbol} ({len(cached)} candles)")
                return cached

            if not isinstance(raw, list) or not raw:
                log.warning(f"Unexpected candle response for {symbol}: {type(raw)}")
                return self.candle_cache.get(symbol, pd.DataFrame())

            df = pd.DataFrame(raw)
            if "t" not in df.columns:
                log.warning(f"Candle response missing 't' column for {symbol}")
                return self.candle_cache.get(symbol, pd.DataFrame())
            df["t"] = pd.to_datetime(df["t"], unit="ms")
            df = df.rename(columns={
                "t": "timestamp",
                "o": "open",
                "h": "high",
                "l": "low",
                "c": "close",
                "v": "volume",
            })

            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            df = df.sort_values("timestamp").tail(lookback).reset_index(drop=True)
            self.candle_cache[symbol] = df
            log.info(f"Fetched {len(df)} candles for {symbol} ({interval})")
            return df

        except Exception as e:
            log.error(f"Failed to fetch candles for {symbol}: {e}")
            return self.candle_cache.get(symbol, pd.DataFrame())

    def fetch_all_candles(self) -> dict[str, pd.DataFrame]:
        """Fetch candles for all configured instruments using per-symbol timeframes."""
        result = {}
        symbol_timeframes = getattr(self.config, 'symbol_timeframes', {})
        for symbol in self.config.instruments:
            interval = symbol_timeframes.get(symbol, self.config.candle_interval)
            result[symbol] = self.fetch_candles(symbol, interval=interval)
            time.sleep(2.5)  # Rate limit — avoid 429s from HyperLiquid
        return result

    def fetch_mid_prices(self) -> dict[str, float]:
        """Fetch current mid prices. Uses last candle close for HIP-3 symbols."""
        # For each instrument, get the latest candle close as the price
        for symbol in self.config.instruments:
            df = self.candle_cache.get(symbol)
            if df is not None and not df.empty:
                self.mid_prices[symbol] = float(df.iloc[-1]["close"])
            else:
                log.debug(f"No cached candles for {symbol} — skipping mid price")

        if self.mid_prices:
            prices_str = ", ".join(f"{s}: ${p:.2f}" for s, p in self.mid_prices.items())
            log.info(f"Prices: {prices_str}")
        return self.mid_prices

    def fetch_live_prices(self, symbols: list[str] = None) -> tuple[dict, dict, dict]:
        """Fetch live prices + candle high/low for each symbol.

        Returns (prices, highs, lows) from the current in-progress candle.
        The high/low capture intra-candle spikes that the close misses,
        enabling trail activation and SL checks closer to TV's tick-by-tick behavior.
        """
        symbols = symbols or list(self.config.instruments.keys())
        prices = {}
        highs = {}
        lows = {}
        for symbol in symbols:
            try:
                raw = self._api_post({
                    "type": "candleSnapshot",
                    "req": {"coin": symbol, "interval": "5m", "startTime": 0},
                })
                if raw and isinstance(raw, list) and len(raw) > 0:
                    # Last element is the current (incomplete) candle
                    last = raw[-1]
                    prices[symbol] = float(last["c"])
                    highs[symbol] = float(last["h"])
                    lows[symbol] = float(last["l"])
            except Exception as e:
                log.debug(f"Live price fetch failed for {symbol}: {e}")
                # Fall back to cached price
                cached = self.mid_prices.get(symbol)
                if cached:
                    prices[symbol] = cached
            time.sleep(0.5)  # Rate limiting between symbols

        if prices:
            prices_str = ", ".join(f"{s}: ${p:.2f}" for s, p in prices.items())
            log.info(f"Prices: {prices_str}")
        return prices, highs, lows

    def fetch_quick_price(self, symbol: str) -> Optional[float]:
        """Fetch a single symbol's current price as fast as possible.

        Uses L2 orderbook mid price (~500ms) via direct API call.
        Falls back to candle API if orderbook is empty.
        """
        # Direct orderbook API — fastest method (~500ms)
        try:
            import requests
            r = requests.post("https://api.hyperliquid.xyz/info",
                              json={"type": "l2Book", "coin": symbol}, timeout=3)
            if r.status_code == 200:
                book = r.json()
                # Defensive: HL can return missing/empty levels on rate-limit;
                # don't IndexError-crash the quick price path.
                levels = book.get("levels") or []
                if len(levels) >= 2:
                    bids = levels[0] or []
                    asks = levels[1] or []
                    if bids and asks:
                        mid = (float(bids[0]["px"]) + float(asks[0]["px"])) / 2
                        return mid
        except Exception as e:
            log.debug(f"Quick price orderbook failed for {symbol}: {e}")

        # Fallback: candle API (~2.5s)
        try:
            raw = self._api_post({
                "type": "candleSnapshot",
                "req": {"coin": symbol, "interval": "5m", "startTime": 0},
            })
            if raw and isinstance(raw, list) and len(raw) > 0:
                return float(raw[-1]["c"])
        except Exception as e:
            log.debug(f"Quick price candle failed for {symbol}: {e}")

        return None

    def fetch_orderbook(self, symbol: str, depth: int = 5) -> dict:
        """Fetch L2 order book for a symbol."""
        try:
            if self.info:
                book = self.info.l2_snapshot(coin=symbol)
                return {
                    "bids": book["levels"][0][:depth],
                    "asks": book["levels"][1][:depth],
                }
        except Exception as e:
            log.error(f"Failed to fetch orderbook for {symbol}: {e}")
        return {"bids": [], "asks": []}

    def fetch_account_state(self) -> dict:
        """Fetch current account state (positions, margin, balance)."""
        if not self.config.account_address:
            log.debug("No account address configured — skipping account state fetch")
            return {}
        try:
            if self.info:
                log.debug("Fetching account state via SDK")
                return self.info.user_state(self.config.account_address)
        except Exception as e:
            log.warning(f"SDK fetch_account_state failed: {e}, trying raw HTTP")

        # Raw HTTP fallback — use correct network URL
        log.debug("Falling back to raw HTTP for account state")
        result = self._api_post({
            "type": "clearinghouseState",
            "user": self.config.account_address,
        }, use_network_url=True)
        if result:
            return result
        return {}

    def fetch_open_orders(self) -> list:
        """Fetch all open orders."""
        if not self.config.account_address:
            return []
        try:
            if self.info:
                return self.info.open_orders(self.config.account_address)
        except Exception as e:
            log.warning(f"SDK fetch_open_orders failed: {e}, trying raw HTTP")

        # Raw HTTP fallback — use correct network URL
        result = self._api_post({
            "type": "openOrders",
            "user": self.config.account_address,
        }, use_network_url=True)
        if result and isinstance(result, list):
            return result
        return []

    def fetch_user_fills(self) -> list:
        """Fetch recent trade fills."""
        if not self.config.account_address:
            return []
        try:
            if self.info:
                return self.info.user_fills(self.config.account_address)
        except Exception as e:
            log.warning(f"SDK fetch_user_fills failed: {e}, trying raw HTTP")

        # Raw HTTP fallback — use correct network URL
        result = self._api_post({
            "type": "userFills",
            "user": self.config.account_address,
        }, use_network_url=True)
        if result and isinstance(result, list):
            return result
        return []

    def get_cached_candles(self, symbol: str) -> pd.DataFrame:
        """Return cached candles or fetch if not available."""
        if symbol in self.candle_cache and not self.candle_cache[symbol].empty:
            log.debug(f"Cache hit for {symbol} ({len(self.candle_cache[symbol])} candles)")
            return self.candle_cache[symbol]
        log.debug(f"Cache miss for {symbol} — fetching from API")
        return self.fetch_candles(symbol)

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Get the latest price for a symbol."""
        if symbol in self.mid_prices:
            return self.mid_prices[symbol]
        # Try from cached candles
        df = self.candle_cache.get(symbol)
        if df is not None and not df.empty:
            price = float(df.iloc[-1]["close"])
            self.mid_prices[symbol] = price
            log.debug(f"Price for {symbol} from cache: ${price:.2f}")
            return price
        log.debug(f"No price available for {symbol}")
        return None
