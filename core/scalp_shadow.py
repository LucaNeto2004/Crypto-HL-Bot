"""Scalp Shadow Runner — evaluates the 2026-04-14 elected scalp configs
forward-walking on real HL candles without touching production state.

For each configured (symbol, interval, strategy_instance):
  1. Fetches candles at the config's target interval (uses data layer cache)
  2. Detects new closed bar via timestamp diff
  3. Computes features on the fetched df
  4. Calls strategy.evaluate() directly
  5. On signal, logs [SCALP_SHADOW] and opens a virtual position in the
     scalp shadow ledger

The ledger lives at data/scalp_shadow_trades.json — separate from the
HL-native shadow ledger so we can compare the scalp vs momentum tape
side-by-side.

Entry prices use the LAST CLOSED BAR'S CLOSE (not live mid) to eliminate
cycle-lag bias vs the TV webhook path which also fires on bar close.

Zero production impact — never touches risk gate, paper_state, or live
execution. Evaluates only, logs only, records to its own JSON.
"""
from __future__ import annotations

import importlib.util
import os
from typing import Optional

import pandas as pd

from strategies.base import BaseStrategy, SignalType
from utils.logger import setup_logger

log = setup_logger("scalp_shadow")


def _load_shadow_ledger_class():
    """Load ShadowLedger without triggering shared/ package init (httpx issue)."""
    path = os.path.join(os.path.dirname(__file__), "..", "..", "shared", "shadow_ledger.py")
    spec = importlib.util.spec_from_file_location("shadow_ledger_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ShadowLedger


class ScalpShadowRunner:
    def __init__(self, data_client, features_engine, ledger_path: str,
                 position_pct: float = 0.20, starting_balance: float = 10_000.0):
        self.data = data_client
        self.features = features_engine
        ShadowLedger = _load_shadow_ledger_class()
        self.ledger = ShadowLedger(
            path=ledger_path,
            starting_balance=starting_balance,
            position_pct=position_pct,
        )
        self.configs: list[tuple[str, str, BaseStrategy]] = []
        self._last_bar_ts: dict[tuple[str, str], str] = {}

    def register(self, symbol: str, interval: str, strategy: BaseStrategy) -> None:
        strategy.enabled = True
        self.configs.append((symbol, interval, strategy))
        log.info(f"Scalp shadow registered: {symbol} {interval} {strategy.name}")

    def tick(self) -> None:
        for symbol, interval, strategy in self.configs:
            try:
                self._tick_one(symbol, interval, strategy)
            except Exception as e:
                log.warning(f"Scalp shadow tick failed for {symbol} {interval} {strategy.name}: {e}")

    def _tick_one(self, symbol: str, interval: str, strategy: BaseStrategy) -> None:
        df_raw = self.data.fetch_candles(symbol, interval=interval)
        if df_raw is None or df_raw.empty or len(df_raw) < 60:
            return

        last_ts = str(df_raw.iloc[-1]["timestamp"])
        key = (symbol, interval)
        if self._last_bar_ts.get(key) == last_ts:
            return
        self._last_bar_ts[key] = last_ts

        df_enriched = self.features.compute(df_raw.copy())
        if df_enriched is None or df_enriched.empty:
            return

        last_bar = df_enriched.iloc[-1]
        features = self._bar_to_features(last_bar)
        if not features:
            return

        signal = strategy.evaluate(symbol, df_enriched, features)
        if signal is None:
            return
        if signal.signal_type not in (SignalType.LONG, SignalType.SHORT):
            return

        entry_price = float(last_bar["close"])
        side = "long" if signal.signal_type == SignalType.LONG else "short"

        log.info(
            f"[SCALP_SHADOW] {strategy.name} {symbol} {interval} {side} "
            f"@ {entry_price:.4f} sl={signal.stop_loss:.4f} "
            f"reason=\"{signal.reason}\""
        )

        try:
            trail_offset = getattr(signal, "_trail_offset_value", None)
            self.ledger.open_virtual(
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                stop_loss=signal.stop_loss,
                trail_offset=trail_offset,
                strategy_name=f"{strategy.name}_{interval}",
                reason=signal.reason or "",
            )
        except Exception as e:
            log.warning(f"Scalp ledger open_virtual failed: {e}")

    def update_prices(self, prices: dict) -> int:
        try:
            return self.ledger.update_prices(prices)
        except Exception as e:
            log.warning(f"Scalp ledger update_prices failed: {e}")
            return 0

    def _bar_to_features(self, bar: pd.Series) -> Optional[dict]:
        try:
            return {
                "price": float(bar["close"]),
                "atr": float(bar.get("atr", 0) or 0),
                "atr_ratio": float(bar.get("atr_ratio", 1.0) or 1.0),
                "adx": float(bar.get("adx", 0) or 0),
                "rsi": float(bar.get("rsi", 50) or 50),
                "macd_hist": float(bar.get("macd_hist", 0) or 0),
                "ema_9": float(bar.get("ema_9", 0) or 0),
                "ema_21": float(bar.get("ema_21", 0) or 0),
                "ema_50": float(bar.get("ema_50", 0) or 0),
                "ema_50_slope": float(bar.get("ema_50_slope", 0) or 0),
            }
        except Exception:
            return None
