"""
Strategy Research Agent — Tests parameter combinations to find optimal settings.

Runs grid search over strategy parameters, backtests each combination,
and ranks by eval score. Outputs the best parameter set per instrument.
"""
import copy
import itertools
from dataclasses import dataclass
from typing import Any

import pandas as pd

from strategies.base import BaseStrategy
from research.backtester import Backtester, BacktestResult
from research.evaluator import StrategyEvaluator, EvalResult
from utils.logger import setup_logger

log = setup_logger("optimizer")


@dataclass
class OptimizationResult:
    symbol: str
    strategy_name: str
    best_params: dict
    best_score: float
    best_grade: str
    best_result: BacktestResult
    all_results: list  # (params, BacktestResult, EvalResult)

    def summary(self) -> str:
        lines = [
            f"{'=' * 55}",
            f"  OPTIMIZATION: {self.strategy_name} on {self.symbol}",
            f"{'=' * 55}",
            f"  Best params:  {self.best_params}",
            f"  Score:        {self.best_score:.0f}/100 (Grade {self.best_grade})",
            f"  P&L:          ${self.best_result.total_pnl:.2f} ({self.best_result.total_return_pct:+.2f}%)",
            f"  Trades:       {self.best_result.num_trades}",
            f"  Win Rate:     {self.best_result.win_rate:.1f}%",
            f"  Sharpe:       {self.best_result.sharpe_ratio:.2f}",
            f"  Max DD:       {self.best_result.max_drawdown:.2f}%",
            f"",
            f"  Top 5 parameter sets:",
            f"  {'─' * 50}",
        ]

        # Sort by score descending
        sorted_results = sorted(self.all_results, key=lambda x: x[2].score, reverse=True)
        for params, bt, ev in sorted_results[:5]:
            lines.append(
                f"  Score={ev.score:>3.0f} Grade={ev.grade} "
                f"P&L=${bt.total_pnl:>7.2f} WR={bt.win_rate:>5.1f}% "
                f"Sharpe={bt.sharpe_ratio:>5.2f} | {params}"
            )

        return "\n".join(lines)


class StrategyOptimizer:
    def __init__(self, initial_balance: float = 10000.0, interval: str = "1h"):
        self.backtester = Backtester(initial_balance, interval=interval)
        self.evaluator = StrategyEvaluator()

    def optimize(self, symbol: str, df: pd.DataFrame, strategy: BaseStrategy,
                 param_grid: dict[str, list]) -> OptimizationResult:
        """
        Grid search over parameter combinations.

        Args:
            symbol: Instrument symbol
            df: Historical OHLCV data
            strategy: Strategy instance (will be copied for each test)
            param_grid: Dict of param_name -> list of values to test
                e.g. {"adx_threshold": [20, 25, 30], "rsi_overbought": [70, 75, 80]}
        """
        param_names = list(param_grid.keys())
        param_values = list(param_grid.values())
        combinations = list(itertools.product(*param_values))

        log.info(f"Optimizing {strategy.name} on {symbol}: {len(combinations)} combinations")

        all_results = []
        best_score = float('-inf')
        best_params = {}
        best_result = None
        best_eval = None

        for i, combo in enumerate(combinations):
            params = dict(zip(param_names, combo))

            # Skip combos where stop loss >= take profit (bad risk/reward)
            # But allow tp=0 (no fixed TP — trail stop handles exit)
            sl = params.get("atr_stop_multiplier")
            tp = params.get("atr_tp_multiplier")
            if sl is not None and tp is not None and tp > 0 and tp <= sl:
                continue

            # Copy strategy and apply params
            test_strategy = copy.deepcopy(strategy)
            for param, value in params.items():
                setattr(test_strategy, param, value)

            # Backtest
            bt_result = self.backtester.run(symbol, df, test_strategy)
            ev_result = self.evaluator.evaluate(bt_result)

            all_results.append((params, bt_result, ev_result))

            if ev_result.score > best_score:
                best_score = ev_result.score
                best_params = params
                best_result = bt_result
                best_eval = ev_result

            if (i + 1) % 10 == 0:
                log.info(f"  {i + 1}/{len(combinations)} tested | best score: {best_score:.0f}")

        log.info(
            f"Optimization complete: best score={best_score:.0f} "
            f"grade={best_eval.grade if best_eval else 'N/A'} params={best_params}"
        )

        return OptimizationResult(
            symbol=symbol,
            strategy_name=strategy.name,
            best_params=best_params,
            best_score=best_score,
            best_grade=best_eval.grade if best_eval else "F",
            best_result=best_result or BacktestResult(),
            all_results=all_results,
        )
