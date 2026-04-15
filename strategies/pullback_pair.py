"""
Pullback Pair Strategy — EMA reversal + trend pullback combined (crypto port).

Target instruments (per 2026-04-14 scalp research):
  - ETH  15m: +9.49% over 52 days, 47 trades, PF 2.89
  - HYPE 15m: +6.68% over 52 days, 34 trades, PF 2.15

Both survived sensitivity (±20% param perturbation) and train/test split.
Same class as commodities-bot/strategies/pullback_pair.py — the regime gate
bounds (ADX [18, 28], |slope| [0.05%, 0.40%]) apply uniformly across
Silver 5m / ETH 15m / HYPE 15m since they sit in the same mild-trend
sweet spot.

Fires on either of two trigger shapes (both share the regime gate):
  1. ema_reversal   — deep pullback to EMA21 + bullish rejection bar
  2. trend_pullback — shallow pullback to EMA9 + fresh RSI-50 cross

trend_pullback wins tie (stronger fresh-cross signal).

DISABLED by default — runs in shadow-only mode via strategy_manager
until forward-walking validation confirms the notebook numbers.
"""
from typing import Optional

import pandas as pd

from strategies.base import BaseStrategy, Signal, SignalType
from utils.logger import setup_logger

log = setup_logger("pullback_pair")


class PullbackPairStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("pullback_pair")
        self.enabled = False  # Do not run in production until explicitly enabled

        # Regime gate bounds (mild-trend sweet spot) — same as commodities
        self.adx_min = 18.0
        self.adx_max = 28.0
        self.abs_slope_min = 0.0005  # 0.05%
        self.abs_slope_max = 0.0040  # 0.40%

        # Volatility regime guard
        self.atr_ratio_min = 0.7
        self.atr_ratio_max = 2.5

        # Exit engine
        self.atr_stop_multiplier = 0.8
        self.atr_tp_multiplier = 1.0
        self.trail_atr_multiplier = 0.0
        self.time_stop_bars = 12

        # Ema reversal sub-strategy params
        self.rejection_rsi_reset = 40.0
        self.rejection_rsi_trigger = 40.0
        self.rejection_lookback_bars = 3

        # Trend pullback sub-strategy params
        self.pullback_rsi_long = 50.0
        self.pullback_rsi_short = 50.0

    def _regime_ok(self, adx: float, slope: float, atr_ratio: float) -> tuple[bool, bool]:
        vol_ok = self.atr_ratio_min <= atr_ratio <= self.atr_ratio_max
        adx_ok = self.adx_min <= adx <= self.adx_max
        slope_up_ok = self.abs_slope_min <= slope <= self.abs_slope_max
        slope_dn_ok = -self.abs_slope_max <= slope <= -self.abs_slope_min
        return (adx_ok and vol_ok and slope_up_ok, adx_ok and vol_ok and slope_dn_ok)

    def _trend_pullback_trigger(self, df: pd.DataFrame, side: str) -> bool:
        if len(df) < 3:
            return False
        prev = df.iloc[-2]
        prev_prev = df.iloc[-3]
        close = prev.get("close")
        low = prev.get("low")
        high = prev.get("high")
        ema9 = prev.get("ema_9")
        ema21 = prev.get("ema_21")
        rsi = prev.get("rsi")
        pp_rsi = prev_prev.get("rsi")
        if None in (close, low, high, ema9, ema21, rsi, pp_rsi):
            return False
        if any(pd.isna(x) for x in (close, low, high, ema9, ema21, rsi, pp_rsi)):
            return False
        if side == "long":
            return (
                close > ema21
                and low <= ema9
                and close > ema9
                and rsi > self.pullback_rsi_long
                and pp_rsi <= self.pullback_rsi_long
            )
        else:
            return (
                close < ema21
                and high >= ema9
                and close < ema9
                and rsi < self.pullback_rsi_short
                and pp_rsi >= self.pullback_rsi_short
            )

    def _ema_reversal_trigger(self, df: pd.DataFrame, side: str) -> bool:
        if len(df) < self.rejection_lookback_bars + 2:
            return False
        prev = df.iloc[-2]
        close = prev.get("close")
        open_ = prev.get("open")
        ema21 = prev.get("ema_21")
        ema50 = prev.get("ema_50")
        rsi = prev.get("rsi")
        if None in (close, open_, ema21, ema50, rsi):
            return False
        if any(pd.isna(x) for x in (close, open_, ema21, ema50, rsi)):
            return False
        lookback = df.iloc[-(self.rejection_lookback_bars + 1):-1]
        if len(lookback) < self.rejection_lookback_bars:
            return False
        if side == "long":
            low_hit = (lookback["low"] <= lookback["ema_21"]).any()
            rsi_reset = (lookback["rsi"] < self.rejection_rsi_reset).any()
            return bool(
                close > ema50
                and low_hit
                and close > ema21
                and close > open_
                and rsi > self.rejection_rsi_trigger
                and rsi_reset
            )
        else:
            high_hit = (lookback["high"] >= lookback["ema_21"]).any()
            rsi_reset = (lookback["rsi"] > 100 - self.rejection_rsi_reset).any()
            return bool(
                close < ema50
                and high_hit
                and close < ema21
                and close < open_
                and rsi < 100 - self.rejection_rsi_trigger
                and rsi_reset
            )

    def evaluate(self, symbol: str, df: pd.DataFrame, features: dict) -> Optional[Signal]:
        if not features or len(df) < 52:
            return None

        adx = features.get("adx") or 0
        slope = features.get("ema_50_slope") or 0
        atr_ratio = features.get("atr_ratio") or 1.0
        atr = features.get("atr") or 0
        price = features.get("price") or 0

        if not atr or not price:
            return None

        regime_long, regime_short = self._regime_ok(adx, slope, atr_ratio)
        if not regime_long and not regime_short:
            return None

        if regime_long:
            if self._trend_pullback_trigger(df, "long"):
                return self._build_signal(symbol, "long", price, atr, "trend_pullback_long")
            if self._ema_reversal_trigger(df, "long"):
                return self._build_signal(symbol, "long", price, atr, "ema_reversal_long")
        if regime_short:
            if self._trend_pullback_trigger(df, "short"):
                return self._build_signal(symbol, "short", price, atr, "trend_pullback_short")
            if self._ema_reversal_trigger(df, "short"):
                return self._build_signal(symbol, "short", price, atr, "ema_reversal_short")

        return None

    def _build_signal(self, symbol: str, side: str, price: float, atr: float, reason: str) -> Signal:
        sl_dist = atr * self.atr_stop_multiplier
        tp_dist = atr * self.atr_tp_multiplier
        if side == "long":
            sl = price - sl_dist
            tp = price + tp_dist
            sig_type = SignalType.LONG
        else:
            sl = price + sl_dist
            tp = price - tp_dist
            sig_type = SignalType.SHORT
        sig = Signal(
            symbol=symbol,
            signal_type=sig_type,
            strategy_name=self.name,
            confidence=1.0,
            stop_loss=sl,
            take_profit=tp if self.trail_atr_multiplier == 0 else None,
            trail_atr_mult=self.trail_atr_multiplier if self.trail_atr_multiplier > 0 else None,
            reason=f"{reason}: ATR={atr:.4f}",
        )
        sig.atr_stop_mult = self.atr_stop_multiplier
        if self.trail_atr_multiplier > 0:
            sig._trail_offset_value = atr * self.trail_atr_multiplier
        return sig

    def should_close(self, symbol: str, df: pd.DataFrame, features: dict,
                     position_side: str) -> Optional[Signal]:
        return None
