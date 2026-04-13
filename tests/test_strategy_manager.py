"""Tests for StrategyManager — position ownership enforcement (DEV-4).

Verifies that only the strategy that opened a position can close it,
preventing conflicting strategies from fighting over the same position.
"""
from typing import Optional

import pandas as pd
from unittest.mock import MagicMock

from core.strategy_manager import StrategyManager
from strategies.base import BaseStrategy, Signal, SignalType


class StubStrategy(BaseStrategy):
    """Minimal strategy stub for testing ownership logic."""

    def __init__(self, name: str, close_signal: Optional[Signal] = None):
        super().__init__(name)
        self._close_signal = close_signal

    def evaluate(self, symbol, df, features):
        return None

    def should_close(self, symbol, df, features, position_side):
        return self._close_signal


def _make_enriched_df():
    """Minimal enriched DataFrame with regime column."""
    return pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=5, freq="5min"),
        "close": [100.0] * 5,
        "regime": ["trending"] * 5,
    })


def _make_features():
    return {"regime": "trending", "price": 100.0}


class TestOwnershipEnforcement:
    """DEV-4: Only the opener strategy can close a position."""

    def setup_method(self):
        self.mgr = StrategyManager()
        # Patch deployer so it doesn't try to load files
        self.mgr.deployer = MagicMock()
        self.mgr.deployer.load_deployed_params.return_value = None
        # Patch features so compute/correlations work
        self.mgr.features = MagicMock()
        self.mgr.features.compute.return_value = _make_enriched_df()
        self.mgr.features.get_latest_features.return_value = _make_features()
        self.mgr.features.compute_correlations.return_value = {}

    def test_owner_can_close_own_position(self):
        """The strategy that opened a position should be able to close it."""
        close_sig = Signal(
            symbol="xyz:SILVER",
            signal_type=SignalType.CLOSE_LONG,
            strategy_name="momentum",
            confidence=0.8,
            reason="exit signal",
        )
        momentum = StubStrategy("momentum", close_signal=close_sig)
        self.mgr.register(momentum)

        self.mgr.record_entry("xyz:SILVER", "momentum")

        candles = {"xyz:SILVER": _make_enriched_df()}
        positions = {"xyz:SILVER": {"side": "long"}}

        signals = self.mgr.evaluate_all(candles, positions)
        assert len(signals) == 1
        assert signals[0].strategy_name == "momentum"
        assert signals[0].signal_type == SignalType.CLOSE_LONG

    def test_non_owner_cannot_close_position(self):
        """A different strategy must NOT close another strategy's position."""
        close_sig = Signal(
            symbol="xyz:SILVER",
            signal_type=SignalType.CLOSE_LONG,
            strategy_name="other_strategy",
            confidence=0.9,
            reason="some reason",
        )
        other = StubStrategy("other_strategy", close_signal=close_sig)
        self.mgr.register(other)

        # momentum owns the position, not other_strategy
        self.mgr.record_entry("xyz:SILVER", "momentum")

        candles = {"xyz:SILVER": _make_enriched_df()}
        positions = {"xyz:SILVER": {"side": "long"}}

        signals = self.mgr.evaluate_all(candles, positions)
        assert len(signals) == 0

    def test_clear_ownership_allows_new_entry(self):
        """After a position is closed, ownership is cleared for future entries."""
        self.mgr.record_entry("xyz:SILVER", "momentum")
        assert self.mgr.position_owners.get("xyz:SILVER") == "momentum"

        self.mgr.clear_ownership("xyz:SILVER")
        assert "xyz:SILVER" not in self.mgr.position_owners

    def test_record_entry_tracks_ownership(self):
        """record_entry correctly maps symbol to strategy name."""
        self.mgr.record_entry("xyz:GOLD", "momentum")
        assert self.mgr.position_owners["xyz:GOLD"] == "momentum"

        self.mgr.record_entry("xyz:SILVER", "momentum")
        assert self.mgr.position_owners["xyz:SILVER"] == "momentum"

    def test_no_owner_allows_any_strategy_to_close(self):
        """If no owner is recorded (e.g., after restart), any strategy can close."""
        close_sig = Signal(
            symbol="xyz:SILVER",
            signal_type=SignalType.CLOSE_LONG,
            strategy_name="momentum",
            confidence=0.8,
            reason="RSI reversal",
        )
        momentum = StubStrategy("momentum", close_signal=close_sig)
        self.mgr.register(momentum)

        # No record_entry call — simulates post-restart with no ownership data
        candles = {"xyz:SILVER": _make_enriched_df()}
        positions = {"xyz:SILVER": {"side": "long"}}

        signals = self.mgr.evaluate_all(candles, positions)
        assert len(signals) == 1
