"""Shared regime gate — ADX + slope + rising check.

Mirrors the validated-on-1307-trades filter from yesterday's research
note. Used by:
  - core/webhook.py — to filter incoming TV webhook entries
  - main.py — to filter HL-native shadow ledger virtual entries so
    shadow vs webhook comparisons are apples-to-apples on enforced
    symbols

Per-symbol enforce mode is defined here and imported everywhere else.
2026-04-15 morning briefing:
  ETH  — gate v2 saves $5.58, flips losing → green → ENFORCE
  HYPE — gate v2 costs $10.77 of profit → stay SHADOW
Symbols not in REGIME_GATE_ENFORCE_SYMBOLS stay in shadow (log but
do not block).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd
import ta

# ---- Config (shared constants) ----
REGIME_GATE_ENABLED = True
REGIME_GATE_ADX_MIN = 25.0
REGIME_GATE_SLOPE_MIN = 0.002  # 0.2% absolute EMA50 slope over 20 bars
REGIME_GATE_REQUIRE_RISING = True  # Require ADX > ADX[-5]
REGIME_GATE_RISING_LOOKBACK = 5

REGIME_GATE_ENFORCE_SYMBOLS: set[str] = {"ETH"}


def is_enforcing(symbol: str) -> bool:
    return symbol in REGIME_GATE_ENFORCE_SYMBOLS


@dataclass
class GateResult:
    enabled: bool
    pass_: bool   # True if gate approves the trade
    adx: float
    adx_past: float
    adx_rising: bool
    abs_slope: float
    rule_adx_ok: bool
    rule_slope_ok: bool
    rule_rising_ok: bool

    @property
    def stats_str(self) -> str:
        return (f"ADX={self.adx:.1f} (prev5={self.adx_past:.1f} "
                f"rising={self.adx_rising}) |slope|={self.abs_slope*100:.3f}%")

    @property
    def reason_str(self) -> str:
        return (f"[adx_ok={self.rule_adx_ok} slope_ok={self.rule_slope_ok} "
                f"rising_ok={self.rule_rising_ok}]")


def evaluate(df: pd.DataFrame) -> Optional[GateResult]:
    """Evaluate the regime gate on a candle dataframe.

    Returns None if the dataframe is too short to compute the gate
    (caller should treat this as 'do not block' — we simply don't know).
    Returns a GateResult with pass_=True/False otherwise.
    """
    if not REGIME_GATE_ENABLED or df is None or len(df) < 52:
        return None
    try:
        closes_s = df['close'].astype(float)
        highs_s = df['high'].astype(float)
        lows_s = df['low'].astype(float)
        adx_series = ta.trend.ADXIndicator(highs_s, lows_s, closes_s, window=14).adx()
        adx = float(adx_series.iloc[-1])
        if len(adx_series) > REGIME_GATE_RISING_LOOKBACK:
            adx_past = float(adx_series.iloc[-(REGIME_GATE_RISING_LOOKBACK + 1)])
        else:
            adx_past = 0.0
        adx_rising = adx > adx_past
        ema50_s = ta.trend.ema_indicator(closes_s, window=50)
        if not pd.isna(ema50_s.iloc[-1]) and not pd.isna(ema50_s.iloc[-21]):
            slope = float((ema50_s.iloc[-1] - ema50_s.iloc[-21]) / ema50_s.iloc[-21])
        else:
            slope = 0.0
        abs_slope = abs(slope)
        rule_adx_ok = adx >= REGIME_GATE_ADX_MIN
        rule_slope_ok = abs_slope >= REGIME_GATE_SLOPE_MIN
        rule_rising_ok = adx_rising if REGIME_GATE_REQUIRE_RISING else True
        gate_pass = rule_adx_ok and rule_slope_ok and rule_rising_ok
        return GateResult(
            enabled=True,
            pass_=gate_pass,
            adx=adx,
            adx_past=adx_past,
            adx_rising=adx_rising,
            abs_slope=abs_slope,
            rule_adx_ok=rule_adx_ok,
            rule_slope_ok=rule_slope_ok,
            rule_rising_ok=rule_rising_ok,
        )
    except Exception:
        return None
