"""
Backtesting Agent — Replays historical data through strategies.

Takes the same strategy classes used in live trading and runs them
against historical candle data. No lookahead bias — processes one
candle at a time, exactly like the live bot.
"""
import copy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd
import numpy as np

from core.features import FeatureEngine
from strategies.base import BaseStrategy, Signal, SignalType
from config.settings import INSTRUMENTS
from utils.logger import setup_logger

log = setup_logger("backtest")

COMMISSION_RATE = 0.00006  # HyperLiquid blended 0.006% maker/taker — applied on entry AND exit


@dataclass
class BacktestTrade:
    entry_time: datetime
    exit_time: Optional[datetime]
    symbol: str
    side: str               # "long" or "short"
    entry_price: float
    exit_price: Optional[float]
    size: float
    size_usd: float
    pnl: Optional[float]
    pnl_pct: Optional[float]
    strategy: str
    exit_reason: str = ""
    bars_held: int = 0


@dataclass
class BacktestPosition:
    symbol: str
    side: str
    entry_price: float
    entry_time: datetime
    size: float
    size_usd: float
    strategy: str
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    entry_bar: int = 0
    # Trailing stop fields (ATR-based, matches Pine Script trail_points/trail_offset)
    trail_active: bool = False
    trail_offset: Optional[float] = None      # Trail distance in price (ATR * multiplier)
    trail_activation: Optional[float] = None  # Activation distance (= trail_offset for TV)
    best_price: Optional[float] = None        # Best price seen since entry


class Backtester:
    def __init__(self, initial_balance: float = 10000.0, interval: str = "5m",
                 max_pyramiding: int = 1):
        self.initial_balance = initial_balance
        self.interval = interval
        self.features = FeatureEngine()
        self.max_pyramiding = max_pyramiding  # Max simultaneous positions (TV pyramiding + 1)

    def run(self, symbol: str, df: pd.DataFrame, strategy: BaseStrategy,
            position_size_usd: Optional[float] = None) -> "BacktestResult":
        """
        Run a single strategy on a single instrument's historical data.

        Args:
            symbol: Instrument symbol
            df: Raw OHLCV DataFrame (must have timestamp, open, high, low, close, volume)
            strategy: Strategy instance to test
            position_size_usd: Override default position size
        """
        if df.empty or len(df) < 60:
            log.warning(f"Not enough data for backtest ({len(df)} candles)")
            return BacktestResult(symbol=symbol, strategy_name=strategy.name)

        instrument = INSTRUMENTS.get(symbol)
        use_dynamic_sizing = position_size_usd is None  # Use % of balance unless overridden
        size_usd = position_size_usd or (instrument.default_size if instrument else 1000)

        balance = self.initial_balance
        peak_balance = balance
        positions: list[BacktestPosition] = []
        trades: list[BacktestTrade] = []
        equity_curve = []

        # Pre-compute features on full dataset
        enriched = self.features.compute(df.copy())

        # Walk forward from bar 50 (need lookback for indicators)
        for i in range(50, len(enriched)):
            bar = enriched.iloc[i]
            price = bar["close"]
            high = bar["high"]
            low = bar["low"]
            timestamp = bar["timestamp"]

            # Build features dict for this bar
            features = self._bar_to_features(bar)

            # Slice up to current bar for strategy (no lookahead)
            df_slice = enriched.iloc[:i + 1]

            # Update trailing stops for all positions
            for pos in positions:
                self._update_trailing_stop(pos, high, low)

            # Check stop loss / take profit for all positions
            # TV behavior: strategy.exit applies to ALL pyramids with shared SL/TP
            closed_indices = []
            for idx, pos in enumerate(positions):
                hit_sl, hit_tp = self._check_sl_tp(pos, high, low)

                if hit_sl:
                    exit_price = pos.stop_loss
                    reason = "trailing_stop" if pos.trail_active else "stop_loss"
                    trade = self._close_position(pos, exit_price, timestamp, i, reason)
                    trades.append(trade)
                    balance += trade.pnl
                    closed_indices.append(idx)

                elif hit_tp:
                    exit_price = pos.take_profit
                    trade = self._close_position(pos, exit_price, timestamp, i, "take_profit")
                    trades.append(trade)
                    balance += trade.pnl
                    closed_indices.append(idx)

            for idx in sorted(closed_indices, reverse=True):
                positions.pop(idx)

            # Check exit signals (signal reversal closes ALL positions)
            if positions:
                close_signal = strategy.should_close(
                    symbol, df_slice, features, positions[0].side
                )
                if close_signal:
                    for pos in positions:
                        trade = self._close_position(pos, price, timestamp, i, "signal")
                        trades.append(trade)
                        balance += trade.pnl
                    positions.clear()

            # Check entry signals (allow pyramiding up to max)
            if len(positions) < self.max_pyramiding:
                signal = strategy.evaluate(symbol, df_slice, features)
                if signal and balance > 0:
                    side = "long" if signal.signal_type == SignalType.LONG else "short"
                    # Don't open opposite direction — TV doesn't allow mixed
                    if positions and positions[0].side != side:
                        pass  # Skip — would need to close first
                    elif signal.signal_type in (SignalType.LONG, SignalType.SHORT):
                        # Dynamic sizing: % of balance × confidence
                        if use_dynamic_sizing and instrument:
                            entry_size_usd = balance * instrument.base_position_pct * signal.confidence
                        else:
                            entry_size_usd = size_usd * signal.confidence
                        size = entry_size_usd / price

                        # Trail params from signal (matches Pine Script ATR-based trail)
                        trail_offset = getattr(signal, '_trail_offset_value', None)
                        trail_activation = trail_offset  # TV: trail_points = trail_offset

                        new_pos = BacktestPosition(
                            symbol=symbol,
                            side=side,
                            entry_price=price,
                            entry_time=timestamp,
                            size=size,
                            size_usd=entry_size_usd,
                            strategy=strategy.name,
                            stop_loss=signal.stop_loss,
                            take_profit=signal.take_profit,
                            entry_bar=i,
                            trail_offset=trail_offset,
                            trail_activation=trail_activation,
                        )
                        positions.append(new_pos)

                        # TV shared SL/TP: new entry's exit replaces ALL pyramids
                        if len(positions) > 1:
                            for pos in positions[:-1]:
                                pos.stop_loss = new_pos.stop_loss
                                pos.take_profit = new_pos.take_profit
                                if trail_offset:
                                    pos.trail_offset = trail_offset
                                    pos.trail_activation = trail_activation

            # Track equity
            unrealized = 0.0
            for pos in positions:
                if pos.side == "long":
                    unrealized += (price - pos.entry_price) * pos.size
                else:
                    unrealized += (pos.entry_price - price) * pos.size

            equity = balance + unrealized
            peak_balance = max(peak_balance, equity)
            equity_curve.append({
                "timestamp": timestamp,
                "equity": equity,
                "balance": balance,
                "drawdown": (peak_balance - equity) / peak_balance if peak_balance > 0 else 0,
            })

        # Close any remaining positions at last price
        for pos in positions:
            last_bar = enriched.iloc[-1]
            trade = self._close_position(
                pos, last_bar["close"], last_bar["timestamp"], len(enriched) - 1, "end_of_data"
            )
            trades.append(trade)
            balance += trade.pnl

        return BacktestResult(
            symbol=symbol,
            strategy_name=strategy.name,
            initial_balance=self.initial_balance,
            final_balance=balance,
            trades=trades,
            equity_curve=pd.DataFrame(equity_curve),
            interval=self.interval,
        )

    def _bar_to_features(self, bar: pd.Series) -> dict:
        """Convert a DataFrame row to a features dict."""
        return {
            "price": bar.get("close"),
            "ema_9": bar.get("ema_9"),
            "ema_21": bar.get("ema_21"),
            "ema_50": bar.get("ema_50"),
            "trend": bar.get("trend"),
            "macd": bar.get("macd"),
            "macd_signal": bar.get("macd_signal"),
            "macd_hist": bar.get("macd_hist"),
            "rsi": bar.get("rsi"),
            "stoch_k": bar.get("stoch_k"),
            "stoch_d": bar.get("stoch_d"),
            "adx": bar.get("adx"),
            "atr": bar.get("atr"),
            "atr_pct": bar.get("atr_pct"),
            "bb_upper": bar.get("bb_upper"),
            "bb_lower": bar.get("bb_lower"),
            "bb_middle": bar.get("bb_middle"),
            "bb_width": bar.get("bb_width"),
            "vol_ratio": bar.get("vol_ratio"),
            "regime": bar.get("regime"),
            "regime_score": bar.get("regime_score"),
        }

    def _update_trailing_stop(self, pos: BacktestPosition, high: float, low: float):
        """Update trailing stop using ATR-based activation (matches Pine Script).

        Pine Script: trail_points = atr * trailATR / syminfo.mintick
                     trail_offset = atr * trailATR / syminfo.mintick
        trail_points = activation distance, trail_offset = trail distance.
        Both are the same value in our scripts.
        """
        if pos.trail_offset is None or pos.trail_offset <= 0:
            return

        activation = pos.trail_activation or pos.trail_offset

        if pos.side == "long":
            # Track best price
            if pos.best_price is None:
                pos.best_price = high
            else:
                pos.best_price = max(pos.best_price, high)

            # Activate when price moves in favor by activation distance
            if not pos.trail_active and pos.best_price >= pos.entry_price + activation:
                pos.trail_active = True
                new_sl = pos.best_price - pos.trail_offset
                if pos.stop_loss is None or new_sl > pos.stop_loss:
                    pos.stop_loss = new_sl

            # Once active, ratchet SL up
            if pos.trail_active:
                new_sl = pos.best_price - pos.trail_offset
                if pos.stop_loss is None or new_sl > pos.stop_loss:
                    pos.stop_loss = new_sl

        else:  # short
            if pos.best_price is None:
                pos.best_price = low
            else:
                pos.best_price = min(pos.best_price, low)

            if not pos.trail_active and pos.best_price <= pos.entry_price - activation:
                pos.trail_active = True
                new_sl = pos.best_price + pos.trail_offset
                if pos.stop_loss is None or new_sl < pos.stop_loss:
                    pos.stop_loss = new_sl

            if pos.trail_active:
                new_sl = pos.best_price + pos.trail_offset
                if pos.stop_loss is None or new_sl < pos.stop_loss:
                    pos.stop_loss = new_sl

    def _check_sl_tp(self, pos: BacktestPosition, high: float, low: float) -> tuple[bool, bool]:
        """Check if stop loss or take profit was hit during this bar."""
        hit_sl = False
        hit_tp = False

        if pos.stop_loss is not None:
            if pos.side == "long" and low <= pos.stop_loss:
                hit_sl = True
            elif pos.side == "short" and high >= pos.stop_loss:
                hit_sl = True

        if pos.take_profit is not None:
            if pos.side == "long" and high >= pos.take_profit:
                hit_tp = True
            elif pos.side == "short" and low <= pos.take_profit:
                hit_tp = True

        return hit_sl, hit_tp

    def _close_position(self, pos: BacktestPosition, exit_price: float,
                        exit_time, bar_idx: int, reason: str) -> BacktestTrade:
        """Close a position and return the trade record."""
        if pos.side == "long":
            gross_pnl = (exit_price - pos.entry_price) * pos.size
        else:
            gross_pnl = (pos.entry_price - exit_price) * pos.size

        entry_fee = pos.size_usd * COMMISSION_RATE
        exit_fee = (exit_price * pos.size) * COMMISSION_RATE
        pnl = gross_pnl - entry_fee - exit_fee

        pnl_pct = pnl / pos.size_usd * 100

        return BacktestTrade(
            entry_time=pos.entry_time,
            exit_time=exit_time,
            symbol=pos.symbol,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            size=pos.size,
            size_usd=pos.size_usd,
            pnl=pnl,
            pnl_pct=pnl_pct,
            strategy=pos.strategy,
            exit_reason=reason,
            bars_held=bar_idx - pos.entry_bar,
        )


@dataclass
class BacktestResult:
    symbol: str = ""
    strategy_name: str = ""
    initial_balance: float = 10000.0
    final_balance: float = 10000.0
    trades: list = field(default_factory=list)
    equity_curve: pd.DataFrame = field(default_factory=pd.DataFrame)
    interval: str = "1h"
    # Optional metric overrides (used when reconstructing from saved results)
    _metric_overrides: dict = field(default_factory=dict)

    @property
    def total_pnl(self) -> float:
        return self.final_balance - self.initial_balance

    @property
    def total_return_pct(self) -> float:
        if "total_return_pct" in self._metric_overrides:
            return self._metric_overrides["total_return_pct"]
        if self.initial_balance == 0:
            return 0
        return (self.total_pnl / self.initial_balance) * 100

    @property
    def num_trades(self) -> int:
        if "num_trades" in self._metric_overrides:
            return self._metric_overrides["num_trades"]
        return len(self.trades)

    @property
    def winners(self) -> list:
        return [t for t in self.trades if t.pnl and t.pnl > 0]

    @property
    def losers(self) -> list:
        return [t for t in self.trades if t.pnl is not None and t.pnl < 0]

    @property
    def win_rate(self) -> float:
        if "win_rate" in self._metric_overrides:
            return self._metric_overrides["win_rate"]
        if not self.trades:
            return 0
        return len(self.winners) / len(self.trades) * 100

    @property
    def avg_win(self) -> float:
        if not self.winners:
            return 0
        return sum(t.pnl for t in self.winners) / len(self.winners)

    @property
    def avg_loss(self) -> float:
        if not self.losers:
            return 0
        return sum(t.pnl for t in self.losers) / len(self.losers)

    @property
    def profit_factor(self) -> float:
        if "profit_factor" in self._metric_overrides:
            return self._metric_overrides["profit_factor"]
        gross_profit = sum(t.pnl for t in self.winners) if self.winners else 0
        gross_loss = abs(sum(t.pnl for t in self.losers)) if self.losers else 0
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 0
        return gross_profit / gross_loss

    @property
    def max_drawdown(self) -> float:
        if "max_drawdown" in self._metric_overrides:
            return self._metric_overrides["max_drawdown"]
        if self.equity_curve.empty:
            return 0
        return self.equity_curve["drawdown"].max() * 100

    @property
    def sharpe_ratio(self) -> float:
        """Annualized Sharpe ratio (timeframe-aware, 24/7 market)."""
        if "sharpe" in self._metric_overrides:
            return self._metric_overrides["sharpe"]
        if len(self.trades) < 2:
            return 0
        returns = [t.pnl_pct for t in self.trades if t.pnl_pct is not None]
        if not returns or np.std(returns) < 1e-10:
            return 0
        _bpy = {"5m": 105120, "15m": 35040, "1h": 8760, "4h": 2190, "1d": 365}
        bars_per_year = _bpy.get(self.interval, 8760)
        avg_hold = max(1, sum(t.bars_held for t in self.trades) / len(self.trades))

        trades_per_year = bars_per_year / avg_hold
        return (np.mean(returns) / np.std(returns)) * np.sqrt(trades_per_year)

    @property
    def avg_bars_held(self) -> float:
        if not self.trades:
            return 0
        return sum(t.bars_held for t in self.trades) / len(self.trades)

    def summary(self) -> str:
        """Human-readable summary."""
        lines = [
            f"{'=' * 55}",
            f"  BACKTEST: {self.strategy_name} on {self.symbol}",
            f"{'=' * 55}",
            f"  Balance:      ${self.initial_balance:.2f} → ${self.final_balance:.2f}",
            f"  Total P&L:    ${self.total_pnl:.2f} ({self.total_return_pct:+.2f}%)",
            f"  Trades:       {self.num_trades} ({len(self.winners)}W / {len(self.losers)}L)",
            f"  Win Rate:     {self.win_rate:.1f}%",
            f"  Avg Win:      ${self.avg_win:.2f}",
            f"  Avg Loss:     ${self.avg_loss:.2f}",
            f"  Profit Factor:{self.profit_factor:.2f}",
            f"  Max Drawdown: {self.max_drawdown:.2f}%",
            f"  Sharpe Ratio: {self.sharpe_ratio:.2f}",
            f"  Avg Hold:     {self.avg_bars_held:.0f} bars",
            f"{'=' * 55}",
        ]

        # Trade log
        if self.trades:
            lines.append(f"\n  {'Side':<6} {'Entry':>10} {'Exit':>10} {'P&L':>10} {'Bars':>5} {'Reason'}")
            lines.append(f"  {'-' * 55}")
            for t in self.trades:
                lines.append(
                    f"  {t.side:<6} ${t.entry_price:>9.2f} ${t.exit_price:>9.2f} "
                    f"${t.pnl:>9.2f} {t.bars_held:>5} {t.exit_reason}"
                )

        return "\n".join(lines)
