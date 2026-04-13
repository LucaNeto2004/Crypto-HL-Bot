"""
Strategy Manager — Runs all active strategies against each instrument
and collects signals for the risk gate.

Loads optimized parameters from config/deployed/ if available.
Includes position ownership tracking and regime-based strategy filtering.
"""
from datetime import datetime
from typing import Optional

import pandas as pd

from core.features import FeatureEngine
from strategies.base import BaseStrategy, Signal, SignalType
from research.deployer import StrategyDeployer
from config.settings import INSTRUMENTS, load_config
from utils.logger import setup_logger

log = setup_logger("strategy")

# Regime → allowed strategies mapping
REGIME_STRATEGY_MAP: dict[str, list[str]] = {
    "trending": ["momentum_v15"],
    "ranging": ["momentum_v15"],
    "transitional": ["momentum_v15"],
}


class StrategyManager:
    def __init__(self):
        self.strategies: list[BaseStrategy] = []
        self.features = FeatureEngine()
        self.deployer = StrategyDeployer()
        self.position_owners: dict[str, str] = {}  # symbol -> strategy name that opened it
        self.correlations: dict[str, float] = {}   # "symA|symB" -> correlation
        self._tuned_cache: dict[str, BaseStrategy] = {}  # "strategy_name|symbol" -> tuned instance
        self._min_hold_seconds = load_config().risk.min_hold_seconds
        self._cross_close_delay = load_config().risk.cross_close_delay_seconds

    def register(self, strategy: BaseStrategy):
        """Register a strategy and load deployed params for each instrument."""
        self.strategies.append(strategy)
        self._apply_deployed_params(strategy)
        log.info(f"Registered strategy: {strategy.name}")

    def _apply_deployed_params(self, strategy: BaseStrategy):
        """Load optimized parameters from deployed configs."""
        for symbol in INSTRUMENTS:
            params = self.deployer.load_deployed_params(strategy.name, symbol)
            if params:
                # Store per-symbol overrides on the strategy
                if not hasattr(strategy, '_symbol_params'):
                    strategy._symbol_params = {}
                strategy._symbol_params[symbol] = params
                log.info(f"  Loaded deployed params for {strategy.name}/{symbol}: {params}")

    def _get_strategy_for_symbol(self, strategy: BaseStrategy, symbol: str) -> BaseStrategy:
        """Apply per-symbol parameter overrides if they exist (cached to preserve state)."""
        params = getattr(strategy, '_symbol_params', {}).get(symbol)
        if params:
            cache_key = f"{strategy.name}|{symbol}"
            if cache_key not in self._tuned_cache:
                import copy
                tuned = copy.deepcopy(strategy)
                unknown = []
                for param, value in params.items():
                    if hasattr(tuned, param):
                        setattr(tuned, param, value)
                    else:
                        unknown.append(param)
                if unknown:
                    log.warning(
                        f"Deployed params for {strategy.name}/{symbol} contain unknown keys "
                        f"that were silently dropped: {unknown}. "
                        f"Either rename in the JSON or add the attribute to the strategy class."
                    )
                self._tuned_cache[cache_key] = tuned
            return self._tuned_cache[cache_key]
        return strategy

    def _get_allowed_strategies(self, regime: str) -> list[str]:
        """Return list of strategy names allowed for the given regime."""
        # Strip direction suffix to match base regime (e.g. "trending_down" → "trending")
        base_regime = regime.split("_")[0] if "_" in regime else regime
        return REGIME_STRATEGY_MAP.get(base_regime, [s.name for s in self.strategies])

    @staticmethod
    def _parse_hold_seconds(opened_at_str: str) -> Optional[float]:
        """Parse ISO timestamp and return seconds held, or None if invalid."""
        if not opened_at_str:
            return None
        try:
            opened_at = datetime.fromisoformat(opened_at_str)
            return (datetime.now() - opened_at).total_seconds()
        except (ValueError, TypeError):
            return None

    def clear_ownership(self, symbol: str):
        """Clear position ownership after a close trade executes."""
        owner = self.position_owners.pop(symbol, None)
        if owner:
            log.info(f"Cleared ownership: {symbol} (was owned by {owner})")

    def evaluate_all(self, candles: dict[str, pd.DataFrame],
                     positions: dict) -> list[Signal]:
        """
        Run all strategies on all instruments.
        Returns a list of signals (entries and exits).
        """
        signals = []

        # Compute cross-instrument correlations
        self.correlations = self.features.compute_correlations(candles)

        for symbol, df in candles.items():
            if df.empty:
                continue

            # Drop the last (incomplete/forming) candle so strategies
            # evaluate on fully closed candles only.  SL/TP still uses
            # real-time prices from mid_prices (checked before this).
            df_closed = df.iloc[:-1] if len(df) > 1 else df

            # Compute features
            enriched = self.features.compute(df_closed, symbol=symbol)
            latest = self.features.get_latest_features(enriched)

            if not latest:
                log.debug(f"No features for {symbol} — skipping")
                continue

            regime = latest.get("regime", "transitional")
            allowed_strategies = self._get_allowed_strategies(regime)
            log.debug(f"{symbol}: regime={regime}, allowed={allowed_strategies}")

            # Check for exit signals on existing positions (find by base symbol)
            matching_positions = [(pid, pos) for pid, pos in positions.items()
                                  if pid.split('#')[0] == symbol]
            if matching_positions:
                pos_side = matching_positions[0][1]["side"]
                owner = self.position_owners.get(symbol)

                # Enforce minimum hold time — use oldest position's opened_at
                opened_at_str = min(
                    (pos.get("opened_at", "") for _, pos in matching_positions),
                    default=""
                )
                held_seconds = self._parse_hold_seconds(opened_at_str)
                if held_seconds is not None and self._min_hold_seconds > 0:
                    if held_seconds < self._min_hold_seconds:
                        log.debug(
                            f"Hold time not met for {symbol}: {held_seconds:.0f}s < {self._min_hold_seconds}s — skipping strategy exit"
                        )
                        continue

                # Strategies allowed to close positions opened by another strategy
                CROSS_CLOSE_ALLOWED: dict[str, list[str]] = {
                    "momentum": [],
                }

                # Cross-close delay: require 2 candles (30 min) before another strategy can close
                cross_close_ok = True
                if owner and self._cross_close_delay > 0 and held_seconds is not None:
                    if held_seconds < self._cross_close_delay:
                        cross_close_ok = False
                        log.debug(f"{symbol}: cross-close blocked — held {held_seconds:.0f}s < {self._cross_close_delay}s")

                for strategy in self.strategies:
                    if not strategy.enabled:
                        continue
                    # Ownership check: owner can always close; others need allowlist + delay
                    if owner and strategy.name != owner:
                        if strategy.name not in CROSS_CLOSE_ALLOWED.get(owner, []):
                            log.debug(f"{symbol}: {strategy.name} cannot close position owned by {owner}")
                            continue
                        if not cross_close_ok:
                            log.debug(f"{symbol}: {strategy.name} cross-close delayed — position too young")
                            continue
                    tuned = self._get_strategy_for_symbol(strategy, symbol)
                    log.debug(f"{symbol}: evaluating exit from {strategy.name} (pos={pos_side}, owner={owner})")
                    close_signal = tuned.should_close(symbol, enriched, latest, pos_side)
                    if close_signal:
                        close_signal.metadata["regime"] = regime
                        signals.append(close_signal)
                        log.info(f"EXIT signal: {symbol} {close_signal.signal_type.value} from {strategy.name}")
                        break

            # Check for entry signals (pyramiding allowed — risk gate handles limits)
            if True:
                for strategy in self.strategies:
                    if not strategy.enabled:
                        continue
                    # Only run strategies that have deployed params for this symbol
                    if not getattr(strategy, '_symbol_params', {}).get(symbol):
                        log.debug(f"{symbol}: skipping {strategy.name} — no deployed params")
                        continue
                    tuned = self._get_strategy_for_symbol(strategy, symbol)
                    log.debug(f"{symbol}: evaluating entry from {strategy.name}")
                    entry_signal = tuned.evaluate(symbol, enriched, latest)
                    if entry_signal:
                        entry_signal.metadata["regime"] = regime
                        signals.append(entry_signal)
                        log.debug(
                            f"ENTRY signal: {symbol} {entry_signal.signal_type.value} "
                            f"from {strategy.name} (confidence={entry_signal.confidence:.2f}, regime={regime})"
                        )
                        break

        return signals

    def record_entry(self, symbol: str, strategy_name: str):
        """Record which strategy opened a position (called after execution)."""
        self.position_owners[symbol] = strategy_name
        log.info(f"Position ownership: {symbol} → {strategy_name}")
