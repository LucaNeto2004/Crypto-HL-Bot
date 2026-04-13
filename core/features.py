"""
Features Module — Computes technical indicators and detects market regime.
Transforms raw OHLCV into signals the Strategy module can act on.
"""
import numpy as np
import pandas as pd
import ta

from utils.logger import setup_logger

log = setup_logger("features")


class FeatureEngine:
    def compute(self, df: pd.DataFrame, symbol: str = "") -> pd.DataFrame:
        """Compute all features on a candle DataFrame. Returns the same df with new columns."""
        if df.empty or len(df) < 50:
            log.warning(f"Not enough data to compute features ({len(df)} candles, need 50)")
            return df

        df = df.copy()
        df = self._trend_indicators(df)
        df = self._momentum_indicators(df)
        df = self._volatility_indicators(df)
        df = self._volume_indicators(df)
        df = self._candlestick_patterns(df)
        df = self._support_resistance(df)
        df = self._early_momentum(df, symbol)
        df = self._regime_detection(df)

        # Log latest indicator snapshot
        latest = df.iloc[-1]
        log.debug(
            f"Indicators: price={latest['close']:.2f} RSI={latest.get('rsi', 0):.1f} "
            f"ADX={latest.get('adx', 0):.1f} MACD_hist={latest.get('macd_hist', 0):.4f} "
            f"BB_width={latest.get('bb_width', 0):.4f} ATR%={latest.get('atr_pct', 0):.2f} "
            f"trend={latest.get('trend', 0)} vol_ratio={latest.get('vol_ratio', 0):.2f}"
        )
        return df

    def _trend_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Moving averages and trend direction."""
        # EMAs
        df["ema_9"] = ta.trend.ema_indicator(df["close"], window=9)
        df["ema_20"] = ta.trend.ema_indicator(df["close"], window=20)
        df["ema_21"] = ta.trend.ema_indicator(df["close"], window=21)
        df["ema_50"] = ta.trend.ema_indicator(df["close"], window=50)

        # MACD
        macd = ta.trend.MACD(df["close"])
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["macd_hist"] = macd.macd_diff()

        # ADX — trend strength
        adx = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=14)
        df["adx"] = adx.adx()
        df["di_plus"] = adx.adx_pos()
        df["di_minus"] = adx.adx_neg()

        # Trend direction: 1 = up, -1 = down, 0 = flat
        # Primary: 5-candle rate of change (fast, reacts in 1-2 candles)
        # Secondary: EMA-9 slope over 3 candles (confirms momentum)
        # Both must agree for a directional call; disagreement = flat
        roc_5 = df["close"].pct_change(5)
        ema9_slope = df["ema_9"].pct_change(3)
        roc_threshold = 0.003  # 0.3% minimum move to declare direction

        roc_up = roc_5 > roc_threshold
        roc_down = roc_5 < -roc_threshold
        ema_confirms_up = ema9_slope > 0
        ema_confirms_down = ema9_slope < 0

        df["trend"] = np.where(
            roc_up & ema_confirms_up, 1,
            np.where(roc_down & ema_confirms_down, -1, 0)
        )

        # Log trend direction reasoning for latest candle
        latest_roc = roc_5.iloc[-1] if not roc_5.empty else 0
        latest_ema_slope = ema9_slope.iloc[-1] if not ema9_slope.empty else 0
        latest_trend = df["trend"].iloc[-1]
        if pd.notna(latest_roc) and pd.notna(latest_ema_slope):
            log.debug(
                f"Trend direction={latest_trend}: ROC_5={latest_roc:.4f} "
                f"(threshold=±{roc_threshold}), EMA9_slope={latest_ema_slope:.4f}"
            )

        return df

    def _momentum_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """RSI, Stochastic, momentum."""
        # RSI
        df["rsi"] = ta.momentum.rsi(df["close"], window=14)

        # Stochastic
        stoch = ta.momentum.StochasticOscillator(df["high"], df["low"], df["close"])
        df["stoch_k"] = stoch.stoch()
        df["stoch_d"] = stoch.stoch_signal()

        # Rate of change
        df["roc"] = ta.momentum.roc(df["close"], window=12)

        return df

    def _candlestick_patterns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Detect reversal candlestick patterns."""
        o = df["open"]
        c = df["close"]
        h = df["high"]
        lo = df["low"]
        body = (c - o).abs()
        prev_c = c.shift(1)
        prev_o = o.shift(1)

        df["prev_close"] = prev_c
        df["prev_open"] = prev_o

        # Bullish engulfing
        df["bull_engulf"] = (c > o) & (prev_c < prev_o) & (c > prev_o) & (o <= prev_c)
        # Bearish engulfing
        df["bear_engulf"] = (c < o) & (prev_c > prev_o) & (c < prev_o) & (o >= prev_c)
        # Hammer (long lower wick, small upper wick)
        df["hammer"] = (c > o) & ((o - lo) > 2 * body) & ((h - c) < body)
        # Shooting star (long upper wick, small lower wick)
        df["shoot_star"] = (c < o) & ((h - o) > 2 * body) & ((c - lo) < body)
        # Bullish pin bar (very long lower wick)
        df["bull_pin"] = ((o - lo) > 2.5 * body) & (c > o)
        # Bearish pin bar (very long upper wick)
        df["bear_pin"] = ((h - o) > 2.5 * body) & (c < o)

        return df

    def _volatility_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Bollinger Bands, ATR."""
        # Bollinger Bands
        bb = ta.volatility.BollingerBands(df["close"], window=20, window_dev=2)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_middle"] = bb.bollinger_mavg()
        df["bb_lower"] = bb.bollinger_lband()
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_middle"]

        # ATR
        df["atr"] = ta.volatility.average_true_range(
            df["high"], df["low"], df["close"], window=14
        )

        # Normalized ATR (% of price)
        df["atr_pct"] = df["atr"] / df["close"].replace(0, np.nan) * 100

        # ATR ratio: current ATR vs 20-period average (regime filter)
        df["atr_ratio"] = df["atr"] / df["atr"].rolling(20).mean()

        return df

    def _volume_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Volume-based indicators."""
        # Volume SMA
        df["vol_sma_20"] = df["volume"].rolling(window=20).mean()
        df["vol_ratio"] = df["volume"] / df["vol_sma_20"]

        # OBV
        df["obv"] = ta.volume.on_balance_volume(df["close"], df["volume"])

        return df

    def _support_resistance(self, df: pd.DataFrame) -> pd.DataFrame:
        """Detect dynamic support and resistance from recent swing highs/lows."""
        log.debug("Computing support/resistance levels")
        lookback = 5  # candles each side to confirm a swing point

        highs = df["high"].values
        lows = df["low"].values
        n = len(df)

        swing_high = np.full(n, np.nan)
        swing_low = np.full(n, np.nan)

        for i in range(lookback, n - lookback):
            # Swing high: highest high in window centered on i
            if highs[i] == max(highs[i - lookback:i + lookback + 1]):
                swing_high[i] = highs[i]
            # Swing low: lowest low in window centered on i
            if lows[i] == min(lows[i - lookback:i + lookback + 1]):
                swing_low[i] = lows[i]

        df["swing_high"] = swing_high
        df["swing_low"] = swing_low

        # Forward-fill the most recent support/resistance levels
        df["resistance"] = df["swing_high"].ffill()
        df["support"] = df["swing_low"].ffill()

        # Distance from current price to support/resistance (as % of price)
        df["dist_to_support"] = (df["close"] - df["support"]) / df["close"]
        df["dist_to_resistance"] = (df["resistance"] - df["close"]) / df["close"]

        return df

    def _early_momentum(self, df: pd.DataFrame, symbol: str = "") -> pd.DataFrame:
        """
        Detect momentum shifts BEFORE ADX catches up.

        Composite score from 5 fast sub-signals:
        1. ROC-3 (3-bar price change, normalized by ATR%)
        2. EMA-9 acceleration (slope of slope)
        3. Candle body momentum (consecutive directional candles)
        4. Volume confirmation (amplifier, not standalone)
        5. BB breakout while narrow (compression breakout)

        Output: early_momentum_score (-1 to +1), early_momentum_flag (bool)
        """
        n = len(df)

        # 1. Fast ROC-3 normalized by ATR%
        roc_3 = df["close"].pct_change(3) * 100  # in percentage
        df["roc_3"] = roc_3
        atr_pct = df["atr_pct"].replace(0, np.nan)
        roc_norm = (roc_3 / atr_pct).clip(-1.0, 1.0).fillna(0)

        # 2. EMA-9 acceleration (second derivative of EMA)
        ema9_slope = df["ema_9"].pct_change(3)
        ema9_accel = ema9_slope.diff(2)
        df["ema9_accel"] = ema9_accel
        # Positive accel + positive slope = strongly bullish, and vice versa
        accel_norm = np.where(
            (ema9_accel > 0) & (ema9_slope > 0), np.minimum(ema9_accel * 500, 1.0),
            np.where(
                (ema9_accel < 0) & (ema9_slope < 0), np.maximum(ema9_accel * 500, -1.0),
                0.0
            )
        )
        accel_norm = pd.Series(accel_norm, index=df.index).fillna(0)

        # 3. Candle body momentum (last 4 bars)
        body = df["close"] - df["open"]
        full_range = (df["high"] - df["low"]).replace(0, np.nan)
        body_ratio = body.abs() / full_range
        is_bullish = (body > 0) & (body_ratio > 0.5)
        is_bearish = (body < 0) & (body_ratio > 0.5)
        bull_count = is_bullish.rolling(4, min_periods=4).sum().fillna(0)
        bear_count = is_bearish.rolling(4, min_periods=4).sum().fillna(0)
        candle_score = np.where(
            bull_count >= 4, 1.0,
            np.where(bull_count >= 3, 0.6,
                     np.where(bear_count >= 4, -1.0,
                              np.where(bear_count >= 3, -0.6, 0.0)))
        )
        candle_score = pd.Series(candle_score, index=df.index)

        # 4. Volume confirmation (amplifier)
        vol_score = (df["vol_ratio"] - 1.0).clip(-1.0, 1.0).fillna(0)
        vol_multiplier = 1.0 + vol_score.clip(lower=0) * 0.5  # range 1.0 to 1.5

        # 5. BB breakout while narrow (compression breakout)
        bb_width_avg = df["bb_width"].rolling(20).mean()
        bb_narrow = df["bb_width"] < bb_width_avg
        above_upper = df["close"] > df["bb_upper"]
        below_lower = df["close"] < df["bb_lower"]
        bb_breakout = np.where(
            above_upper & bb_narrow, 1.0,
            np.where(below_lower & bb_narrow, -1.0, 0.0)
        )
        bb_breakout = pd.Series(bb_breakout, index=df.index)

        # Composite score
        raw_score = (
            roc_norm * 0.30
            + accel_norm * 0.20
            + candle_score * 0.20
            + bb_breakout * 0.15
        )
        df["early_momentum_score"] = (raw_score * vol_multiplier).clip(-1.0, 1.0)
        df["early_momentum_flag"] = df["early_momentum_score"].abs() > 0.4

        # Log when flag fires
        score = df["early_momentum_score"].iloc[-1]
        flag = df["early_momentum_flag"].iloc[-1]
        if flag:
            direction = "BULLISH" if score > 0 else "BEARISH"
            tag = f"{symbol} " if symbol else ""
            log.warning(
                f"{tag}EARLY MOMENTUM {direction}: score={score:.2f} "
                f"(roc3={roc_norm.iloc[-1]:.2f}, accel={accel_norm.iloc[-1]:.2f}, "
                f"candles={candle_score.iloc[-1]:.1f}, bb={bb_breakout.iloc[-1]:.1f}, "
                f"vol_mult={vol_multiplier.iloc[-1]:.2f})"
            )
        else:
            log.debug(f"{symbol} Early momentum: score={score:.2f} (no flag)")

        return df

    def _regime_detection(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Classify market regime: trending vs mean-reverting.

        Regime (ADX + BB width):
        - ADX > 25 + expanding BB → trending
        - ADX > 35 → trending (strong override)
        - ADX < 25 → ranging
        - Otherwise → transitional

        Staleness filters (downgrade trending → transitional):
        - ADX declining over 3 candles + price within 0.15% of EMA-9

        Direction (fast — reacts in 1-2 candles):
        - Primary: 5-candle ROC + EMA-9 slope must agree (>0.3%)
        - ADX < 18: direction suppressed entirely (no trend = no direction)
        - ADX > 35: DI+/DI- overrides ROC (with price-action contradiction check)
        """
        bb_expanding = df["bb_width"] > df["bb_width"].rolling(20).mean()

        # Base regime — wider thresholds + hysteresis to stop ADX flip-flop in
        # the 25-35 dead zone. Switch into a new regime only after N consecutive
        # bars of agreement; otherwise carry the previous regime forward.
        df["regime_raw"] = "transitional"
        df.loc[(df["adx"] > 25) & bb_expanding, "regime_raw"] = "trending"
        df.loc[df["adx"] < 25, "regime_raw"] = "ranging"
        df.loc[df["adx"] > 35, "regime_raw"] = "trending"

        HYSTERESIS_BARS = 3
        regime_raw = df["regime_raw"].tolist()
        regime_smoothed = list(regime_raw)
        for i in range(1, len(regime_raw)):
            prev = regime_smoothed[i - 1]
            current = regime_raw[i]
            if current == prev:
                continue
            window_start = max(0, i - HYSTERESIS_BARS + 1)
            window = regime_raw[window_start:i + 1]
            if len(window) >= HYSTERESIS_BARS and all(r == current for r in window):
                regime_smoothed[i] = current
            else:
                regime_smoothed[i] = prev
        df["regime"] = regime_smoothed
        df.drop(columns=["regime_raw"], inplace=True)

        # Staleness filter: downgrade trending → transitional when BOTH conditions met
        # (AND logic — conservative, only downgrades when trend is clearly stale)
        adx_declining = df["adx"].diff(3) < 0  # ADX losing steam
        price_ema_gap = (df["close"] - df["ema_9"]).abs() / df["ema_9"]
        ema_hugging = price_ema_gap < 0.0015  # price within 0.15% of EMA-9
        stale_trending = df["regime"] == "trending"
        stale_mask = stale_trending & adx_declining & ema_hugging
        df.loc[stale_mask, "regime"] = "transitional"

        # Log staleness filter on latest candle
        if not stale_mask.empty and stale_mask.iloc[-1]:
            log.debug(
                f"Staleness filter triggered: ADX declining ({df['adx'].diff(3).iloc[-1]:.2f}), "
                f"price hugging EMA-9 (gap={price_ema_gap.iloc[-1]:.4f})"
            )

        # Direction from fast trend indicator
        trend_dir = df["trend"]  # 1 = up, -1 = down, 0 = flat
        is_up = (trend_dir == 1).copy()
        is_down = (trend_dir == -1).copy()

        # Suppress direction when ADX < 18 — too weak for any directional call
        weak_adx = df["adx"] < 18
        if not weak_adx.empty and weak_adx.iloc[-1]:
            log.debug(f"Direction suppressed: ADX={df['adx'].iloc[-1]:.1f} < 18")
        is_up.loc[weak_adx] = False
        is_down.loc[weak_adx] = False

        # When ADX > 35, use DI+/DI- for direction (reacts faster in strong trends)
        # Override with price-action check to catch post-spike staleness
        strong_adx = df["adx"] > 35
        di_up = (df["di_plus"] > df["di_minus"]).loc[strong_adx]
        di_down = (df["di_minus"] > df["di_plus"]).loc[strong_adx]

        price_move_3 = df["close"].pct_change(3)
        di_contradicted = (
            (di_up & (price_move_3.loc[strong_adx] < -0.005))
            | (di_down & (price_move_3.loc[strong_adx] > 0.005))
        )
        is_up.loc[strong_adx] = di_up & ~di_contradicted
        is_down.loc[strong_adx] = di_down & ~di_contradicted

        # Log DI override on latest candle
        if not strong_adx.empty and strong_adx.iloc[-1]:
            log.debug(
                f"DI override active (ADX={df['adx'].iloc[-1]:.1f}>35): "
                f"DI+={df['di_plus'].iloc[-1]:.1f}, DI-={df['di_minus'].iloc[-1]:.1f}, "
                f"price_move_3={price_move_3.iloc[-1]:.4f}"
            )

        df.loc[(df["regime"] == "trending") & is_up, "regime"] = "trending_up"
        df.loc[(df["regime"] == "trending") & is_down, "regime"] = "trending_down"
        df.loc[(df["regime"] == "ranging") & is_up, "regime"] = "ranging_up"
        df.loc[(df["regime"] == "ranging") & is_down, "regime"] = "ranging_down"
        df.loc[(df["regime"] == "transitional") & is_up, "regime"] = "transitional_up"
        df.loc[(df["regime"] == "transitional") & is_down, "regime"] = "transitional_down"

        # Regime numeric: 1 = trending, 0 = transitional, -1 = ranging
        df["regime_score"] = np.where(
            df["regime"].str.startswith("trending"), 1,
            np.where(df["regime"].str.startswith("ranging"), -1, 0)
        )

        # Spike detection: candle range vs 20-period average range
        # Must be 3x above average AND above 2% absolute to avoid false flags on low-volume periods
        # Also flags the next 2 candles after a spike (cooldown) since price is still unstable
        candle_range = (df["high"] - df["low"]) / df["low"]
        avg_range = candle_range.rolling(20).mean()
        raw_spike = (candle_range > (avg_range * 3)) & (candle_range > 0.02)
        df["spike"] = raw_spike | raw_spike.shift(1, fill_value=False) | raw_spike.shift(2, fill_value=False)

        # Chase detection: percentage move over last 4 candles
        # Used by strategy_manager to block entries that chase extended moves
        df["recent_move_4"] = df["close"].pct_change(4)

        log.info(f"Current regime: {df['regime'].iloc[-1]} (ADX={df['adx'].iloc[-1]:.1f})")
        log.debug(
            f"Regime detail: BB_expanding={bb_expanding.iloc[-1]}, "
            f"DI+={df['di_plus'].iloc[-1]:.1f}, DI-={df['di_minus'].iloc[-1]:.1f}, "
            f"regime_score={df['regime_score'].iloc[-1]}"
        )
        if df["spike"].iloc[-1]:
            spike_pct = candle_range.iloc[-1] * 100
            log.warning(f"SPIKE detected: candle range {spike_pct:.1f}% (3x above normal)")
        return df

    def compute_correlations(self, candles: dict[str, pd.DataFrame], window: int = 50) -> dict:
        """
        Compute rolling correlation matrix between all instruments.
        Returns dict of {(sym_a, sym_b): correlation} for all pairs.
        """
        # Build a DataFrame of close returns for each symbol
        returns = {}
        for symbol, df in candles.items():
            if df.empty or len(df) < window:
                log.debug(f"Skipping {symbol} for correlation — only {len(df)} candles (need {window})")
                continue
            returns[symbol] = df["close"].pct_change().dropna().tail(window)

        if len(returns) < 2:
            log.debug(f"Not enough symbols for correlation ({len(returns)} < 2)")
            return {}

        # Align on index
        returns_df = pd.DataFrame(returns)
        corr_matrix = returns_df.corr()

        result = {}
        symbols = list(returns_df.columns)
        for i, sym_a in enumerate(symbols):
            for j, sym_b in enumerate(symbols):
                if i < j:
                    val = corr_matrix.loc[sym_a, sym_b]
                    if not pd.isna(val):
                        result[f"{sym_a}|{sym_b}"] = round(float(val), 3)

        if result:
            log.debug(f"Correlations: {result}")
        return result

    def get_latest_features(self, df: pd.DataFrame) -> dict:
        """Extract the latest row of features as a dict for strategy consumption."""
        if df.empty:
            return {}
        latest = df.iloc[-1]
        return {
            "price": latest["close"],
            "ema_9": latest.get("ema_9"),
            "ema_21": latest.get("ema_21"),
            "ema_50": latest.get("ema_50"),
            "trend": latest.get("trend"),
            "macd": latest.get("macd"),
            "macd_signal": latest.get("macd_signal"),
            "macd_hist": latest.get("macd_hist"),
            "rsi": latest.get("rsi"),
            "stoch_k": latest.get("stoch_k"),
            "stoch_d": latest.get("stoch_d"),
            "adx": latest.get("adx"),
            "atr": latest.get("atr"),
            "atr_pct": latest.get("atr_pct"),
            "atr_ratio": latest.get("atr_ratio"),
            "bb_upper": latest.get("bb_upper"),
            "bb_middle": latest.get("bb_middle"),
            "bb_lower": latest.get("bb_lower"),
            "bb_width": latest.get("bb_width"),
            "vol_ratio": latest.get("vol_ratio"),
            "regime": latest.get("regime"),
            "regime_score": latest.get("regime_score"),
            "spike": bool(latest.get("spike", False)),
            "support": latest.get("support"),
            "resistance": latest.get("resistance"),
            "dist_to_support": latest.get("dist_to_support"),
            "dist_to_resistance": latest.get("dist_to_resistance"),
            "early_momentum_score": latest.get("early_momentum_score"),
            "early_momentum_flag": bool(latest.get("early_momentum_flag", False)),
            "roc_3": latest.get("roc_3"),
            "ema9_accel": latest.get("ema9_accel"),
            "recent_move_4": latest.get("recent_move_4"),
            "di_plus": latest.get("di_plus"),
            "di_minus": latest.get("di_minus"),
            "ema_20": latest.get("ema_20"),
            "bull_engulf": bool(latest.get("bull_engulf", False)),
            "bear_engulf": bool(latest.get("bear_engulf", False)),
            "hammer": bool(latest.get("hammer", False)),
            "shoot_star": bool(latest.get("shoot_star", False)),
            "bull_pin": bool(latest.get("bull_pin", False)),
            "bear_pin": bool(latest.get("bear_pin", False)),
            "prev_close": latest.get("prev_close"),
            "prev_open": latest.get("prev_open"),
        }
