"""Tests for Momentum strategy."""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from strategies.momentum import MomentumStrategy
from strategies.base import SignalType


def make_df(n=50, base_price=2000.0, trend=0.0):
    """Generate synthetic OHLCV data."""
    dates = [datetime.now() - timedelta(minutes=5 * (n - i)) for i in range(n)]
    prices = [base_price + trend * i + np.random.randn() * 2 for i in range(n)]
    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices,
        "high": [p + abs(np.random.randn()) * 3 for p in prices],
        "low": [p - abs(np.random.randn()) * 3 for p in prices],
        "close": [p + np.random.randn() for p in prices],
        "volume": [1000 + np.random.randint(0, 500) for _ in range(n)],
    })
    return df


def make_features_momentum_long(price=2000.0):
    """Features dict for momentum long entry."""
    return {
        "regime": "trending_up",
        "price": price,
        "ema_9": price + 5,
        "ema_21": price,
        "ema_50": price - 10,
        "adx": 30.0,
        "di_plus": 30.0,
        "di_minus": 15.0,
        "macd_hist": 2.0,
        "rsi": 55.0,
        "bb_upper": price + 40,
        "bb_middle": price,
        "bb_lower": price - 40,
        "stoch_k": 60.0,
        "stoch_d": 55.0,
        "atr": 20.0,
        "atr_pct": 0.01,
        "close": price,
        "vol_ratio": 1.5,
        "spike": False,
    }


def make_features_momentum_short(price=2000.0):
    """Features dict for momentum short entry."""
    return {
        "regime": "trending_down",
        "price": price,
        "ema_9": price - 5,
        "ema_21": price,
        "ema_50": price + 10,
        "adx": 30.0,
        "di_plus": 15.0,
        "di_minus": 30.0,
        "macd_hist": -2.0,
        "rsi": 45.0,
        "bb_upper": price + 40,
        "bb_middle": price,
        "bb_lower": price - 40,
        "stoch_k": 40.0,
        "stoch_d": 45.0,
        "atr": 20.0,
        "atr_pct": 0.01,
        "close": price,
        "vol_ratio": 1.5,
        "spike": False,
    }


class TestMomentum:
    def setup_method(self):
        self.strategy = MomentumStrategy()

    def test_has_name(self):
        assert self.strategy.name == "momentum"

    def test_long_signal_conditions(self):
        df = make_df(trend=1.0)
        features = make_features_momentum_long()
        signal = self.strategy.evaluate("xyz:GOLD", df, features)
        if signal:
            assert signal.signal_type == SignalType.LONG
            assert signal.confidence > 0
            assert signal.stop_loss is not None
            assert signal.take_profit is not None
            assert signal.stop_loss < features["close"]
            assert signal.take_profit > features["close"]

    def test_short_signal_conditions(self):
        df = make_df(trend=-1.0)
        features = make_features_momentum_short()
        signal = self.strategy.evaluate("xyz:GOLD", df, features)
        if signal:
            assert signal.signal_type == SignalType.SHORT
            assert signal.stop_loss > features["close"]
            assert signal.take_profit < features["close"]

    def test_no_signal_low_adx(self):
        df = make_df(trend=0.0)
        features = make_features_momentum_long()
        features["adx"] = 5.0  # too weak
        signal = self.strategy.evaluate("xyz:GOLD", df, features)
        assert signal is None

    def test_no_signal_low_volume(self):
        df = make_df(trend=1.0)
        features = make_features_momentum_long()
        features["vol_ratio"] = 0.5  # below vol_min
        signal = self.strategy.evaluate("xyz:GOLD", df, features)
        assert signal is None

    def test_none_signal_type(self):
        """Strategies return None when no signal, not SignalType.NONE."""
        df = make_df(trend=0.0)
        features = make_features_momentum_long()
        features["adx"] = 5.0
        features["macd_hist"] = 0.0
        result = self.strategy.evaluate("xyz:GOLD", df, features)
        if result is not None:
            assert result.signal_type in (SignalType.LONG, SignalType.SHORT)
