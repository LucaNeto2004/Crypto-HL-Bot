"""
Momentum v15 Strategy — Replicated from Pine Script "Momentum GOLD v15-5M"

Entry Long:  close[1] > EMA9[1] > EMA21[1], close[1] > EMA50[1], RSI in range, MACD hist > 0, regime OK
Entry Short: close[1] < EMA9[1] < EMA21[1], close[1] < EMA50[1], RSI in range, MACD hist < 0, regime OK
Exit: Signal reversal (RSI + MACD flip) closes all positions
SL: ATR[1] * atr_stop_mult
Trail: ATR[1] * trail_atr_mult (replaces fixed TP)

All conditions use [1] shift (previous bar) to avoid look-ahead bias.
Regime filter: skip choppy (ATR < 0.8 * ATR_MA), weak trend (EMA50 slope < 0.1%),
               extreme volatility (ATR/ATR_MA > 1.5)
"""
from typing import Optional

import pandas as pd

from strategies.base import BaseStrategy, Signal, SignalType
from utils.logger import setup_logger

log = setup_logger("momentum_v15")


class MomentumV15Strategy(BaseStrategy):
    def __init__(self):
        super().__init__("momentum_v15")

        # RSI thresholds
        self.rsi_long_min = 50
        self.rsi_long_max = 80
        self.rsi_short_max = 50
        self.rsi_short_min = 20

        # Stop loss & trailing
        self.atr_stop_multiplier = 0.7
        self.trail_atr_multiplier = 0.3

        # Volume filter
        self.vol_min = 0.0  # 0 = disabled

        # Signal reversal exit
        self.signal_reversal_rsi_long = 40
        self.signal_reversal_rsi_short = 60

        # Regime filter
        self.use_regime_filter = True
        self.slope_period = 20
        self.min_slope = 0.1
        self.choppy_mult = 0.8
        self.max_atr_ratio = 2.0  # ETH + HYPE; deployed JSON overrides to 2.0 (was hardcoded 1.5 — stale)

        # ---- Momentum regime gate (2026-04-14, validated on 1307 live trades) ----
        # Live bucket analysis: avg $/trade by ADX bucket climbs monotonically from
        # -$0.17 at ADX 0-18 to +$0.78 at ADX 40+. Blocking low-ADX + flat-slope
        # entries captured +$0.63 per trade lift across the full history.
        # See research/2026-04-14_174500_momentum-regime-gate.md for the analysis.
        self.regime_gate_enabled = True  # Master flag
        self.regime_gate_shadow = True  # Shadow: log "would skip" but don't block
        self.regime_gate_adx_min = 25.0  # ADX floor
        self.regime_gate_slope_min = 0.002  # |EMA50 slope over 20 bars| floor (0.2%)

    def evaluate(self, symbol: str, df: pd.DataFrame, features: dict) -> Optional[Signal]:
        if not features or len(df) < 52:
            return None

        prev = df.iloc[-2]
        price_prev = float(prev["close"])
        ema9_prev = float(prev.get("ema_9", 0))
        ema21_prev = float(prev.get("ema_21", 0))
        ema50_prev = float(prev.get("ema_50", 0))
        rsi_prev = float(prev.get("rsi", 50))
        macd_hist_prev = float(prev.get("macd_hist", 0))
        atr_prev = float(prev.get("atr", 0))
        vol_ratio_prev = float(prev.get("vol_ratio", 1.0))

        if not price_prev or not atr_prev or not ema9_prev:
            return None

        price = features.get("price", price_prev)

        # Regime filter
        if self.use_regime_filter and not self._check_regime(df):
            log.debug(f"{symbol}: regime filter blocked entry")
            return None

        # ---- Momentum regime gate ----
        # Block entries outside the high-edge regime identified from live bucket analysis.
        # Shadow mode logs what would be skipped without actually blocking the trade.
        if self.regime_gate_enabled:
            adx_prev = float(prev.get("adx", 0))
            ema50_slope_prev = float(prev.get("ema_50_slope") or 0.0)
            abs_slope = abs(ema50_slope_prev)
            gate_pass = (adx_prev >= self.regime_gate_adx_min
                         and abs_slope >= self.regime_gate_slope_min)
            gate_stats = f"ADX={adx_prev:.1f} |slope|={abs_slope*100:.3f}%"
            if gate_pass:
                log.info(f"REGIME_GATE {symbol}: PASS {gate_stats}")
            elif self.regime_gate_shadow:
                log.info(f"REGIME_GATE {symbol}: SHADOW_BLOCK {gate_stats} "
                         f"(need ADX>={self.regime_gate_adx_min} |slope|>={self.regime_gate_slope_min*100:.2f}%)")
            else:
                log.info(f"REGIME_GATE {symbol}: BLOCK {gate_stats}")
                return None

        log.debug(
            f"{symbol} [prev]: price={price_prev:.2f} EMA9={ema9_prev:.2f} "
            f"EMA21={ema21_prev:.2f} EMA50={ema50_prev:.2f} RSI={rsi_prev:.1f} "
            f"MACD_hist={macd_hist_prev:.4f} vol={vol_ratio_prev:.2f}"
        )

        # Long
        if (price_prev > ema9_prev > ema21_prev
                and price_prev > ema50_prev
                and rsi_prev > self.rsi_long_min
                and rsi_prev < self.rsi_long_max
                and macd_hist_prev > 0
                and vol_ratio_prev >= self.vol_min):
            sl = price_prev - (atr_prev * self.atr_stop_multiplier)
            trail_offset = atr_prev * self.trail_atr_multiplier
            signal = Signal(
                symbol=symbol,
                signal_type=SignalType.LONG,
                strategy_name=self.name,
                confidence=1.0,
                stop_loss=sl,
                take_profit=None,
                trail_atr_mult=self.trail_atr_multiplier,
                reason=f"Momentum long: RSI={rsi_prev:.1f}, MACD={macd_hist_prev:.4f}",
            )
            signal.atr_stop_mult = self.atr_stop_multiplier
            signal._trail_offset_value = trail_offset
            return signal

        # Short
        if (price_prev < ema9_prev < ema21_prev
                and price_prev < ema50_prev
                and rsi_prev < self.rsi_short_max
                and rsi_prev > self.rsi_short_min
                and macd_hist_prev < 0
                and vol_ratio_prev >= self.vol_min):
            sl = price_prev + (atr_prev * self.atr_stop_multiplier)
            trail_offset = atr_prev * self.trail_atr_multiplier
            signal = Signal(
                symbol=symbol,
                signal_type=SignalType.SHORT,
                strategy_name=self.name,
                confidence=1.0,
                stop_loss=sl,
                take_profit=None,
                trail_atr_mult=self.trail_atr_multiplier,
                reason=f"Momentum short: RSI={rsi_prev:.1f}, MACD={macd_hist_prev:.4f}",
            )
            signal.atr_stop_mult = self.atr_stop_multiplier
            signal._trail_offset_value = trail_offset
            return signal

        return None

    def should_close(self, symbol: str, df: pd.DataFrame, features: dict,
                     position_side: str) -> Optional[Signal]:
        """Signal reversal exit using [1] shift."""
        if len(df) < 2:
            return None

        prev = df.iloc[-2]
        rsi_prev = float(prev.get("rsi", 50))
        macd_hist_prev = float(prev.get("macd_hist", 0))

        if position_side == "long" and rsi_prev < self.signal_reversal_rsi_long and macd_hist_prev < 0:
            return Signal(
                symbol=symbol,
                signal_type=SignalType.CLOSE_LONG,
                strategy_name=self.name,
                confidence=1.0,
                reason=f"Signal reversal: RSI={rsi_prev:.1f}<{self.signal_reversal_rsi_long}, MACD<0",
            )

        if position_side == "short" and rsi_prev > self.signal_reversal_rsi_short and macd_hist_prev > 0:
            return Signal(
                symbol=symbol,
                signal_type=SignalType.CLOSE_SHORT,
                strategy_name=self.name,
                confidence=1.0,
                reason=f"Signal reversal: RSI={rsi_prev:.1f}>{self.signal_reversal_rsi_short}, MACD>0",
            )

        return None

    def _check_regime(self, df: pd.DataFrame) -> bool:
        """Regime filter matching Pine Script — uses [1] shift."""
        if len(df) < self.slope_period + 2:
            return True

        prev = df.iloc[-2]
        atr_prev = float(prev.get("atr", 0))
        atr_ma = df["atr"].rolling(20).mean().iloc[-2] if len(df) > 21 else atr_prev
        atr_ratio = atr_prev / atr_ma if atr_ma > 0 else 1.0

        is_choppy = atr_prev < atr_ma * self.choppy_mult

        ema50 = df["ema_50"]
        if len(ema50) > self.slope_period + 1:
            ema50_now = float(ema50.iloc[-2])
            ema50_past = float(ema50.iloc[-2 - self.slope_period])
            ema50_slope = abs((ema50_now - ema50_past) / ema50_past * 100) if ema50_past else 0
        else:
            ema50_slope = self.min_slope
        is_weak_trend = ema50_slope < self.min_slope

        is_extreme_vol = atr_ratio > self.max_atr_ratio

        regime_ok = not is_choppy and not is_weak_trend and not is_extreme_vol

        if not regime_ok:
            reasons = []
            if is_choppy:
                reasons.append(f"choppy")
            if is_weak_trend:
                reasons.append(f"weak_trend({ema50_slope:.2f}%)")
            if is_extreme_vol:
                reasons.append(f"extreme_vol({atr_ratio:.2f})")
            log.debug(f"Regime blocked: {', '.join(reasons)}")

        return regime_ok
