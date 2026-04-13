"""
Eval Agent — Evaluates backtest results against quality thresholds.

Decides if a strategy is good enough to promote to live trading.
Uses hard criteria: minimum Sharpe, max drawdown, min trades, win rate.
"""
from dataclasses import dataclass
from typing import Optional

from research.backtester import BacktestResult
from utils.logger import setup_logger

log = setup_logger("eval")


@dataclass
class EvalCriteria:
    """Minimum requirements to promote a strategy to live."""
    min_sharpe: float = 1.0           # Minimum Sharpe ratio
    max_drawdown_pct: float = 10.0    # Max drawdown %
    min_trades: int = 100             # Validation scorecard minimum (was 10 — bumped 2026-04-13 to align with CLAUDE.md)
    min_win_rate: float = 40.0        # Minimum win rate %
    min_profit_factor: float = 1.2    # Gross profit / gross loss
    max_avg_bars_held: float = 200    # Don't hold too long (200 bars = ~50 hours on 15m)
    min_total_return_pct: float = 1.0 # At least 1% return


@dataclass
class EvalResult:
    passed: bool
    grade: str          # A, B, C, D, F
    score: float        # 0-100
    checks: list        # Individual check results
    recommendation: str # "promote", "paper_test", "reject", "needs_tuning"

    def summary(self) -> str:
        lines = [
            f"  EVALUATION: Grade {self.grade} (Score: {self.score:.0f}/100)",
            f"  Recommendation: {self.recommendation.upper()}",
            f"  {'─' * 45}",
        ]
        for check in self.checks:
            icon = "PASS" if check["passed"] else "FAIL"
            lines.append(f"  [{icon}] {check['name']}: {check['value']} (need {check['threshold']})")
        return "\n".join(lines)


class StrategyEvaluator:
    def __init__(self, criteria: Optional[EvalCriteria] = None):
        self.criteria = criteria or EvalCriteria()

    def evaluate(self, result: BacktestResult) -> EvalResult:
        """Evaluate a backtest result against criteria."""
        checks = []
        score = 0

        # Sharpe ratio (25 points)
        sharpe_pass = result.sharpe_ratio >= self.criteria.min_sharpe
        sharpe_score = min(25, (result.sharpe_ratio / self.criteria.min_sharpe) * 12.5) if result.sharpe_ratio > 0 else 0
        checks.append({
            "name": "Sharpe Ratio",
            "value": f"{result.sharpe_ratio:.2f}",
            "threshold": f">= {self.criteria.min_sharpe}",
            "passed": sharpe_pass,
        })
        score += sharpe_score

        # Max drawdown (20 points)
        dd_pass = result.max_drawdown <= self.criteria.max_drawdown_pct
        dd_score = max(0, 20 - (result.max_drawdown / self.criteria.max_drawdown_pct) * 10) if self.criteria.max_drawdown_pct > 0 else 0
        checks.append({
            "name": "Max Drawdown",
            "value": f"{result.max_drawdown:.2f}%",
            "threshold": f"<= {self.criteria.max_drawdown_pct}%",
            "passed": dd_pass,
        })
        score += dd_score

        # Trade count (10 points)
        trades_pass = result.num_trades >= self.criteria.min_trades
        trades_score = min(10, (result.num_trades / self.criteria.min_trades) * 5)
        checks.append({
            "name": "Trade Count",
            "value": f"{result.num_trades}",
            "threshold": f">= {self.criteria.min_trades}",
            "passed": trades_pass,
        })
        score += trades_score

        # Win rate (15 points)
        wr_pass = result.win_rate >= self.criteria.min_win_rate
        wr_score = min(15, (result.win_rate / self.criteria.min_win_rate) * 7.5) if result.win_rate > 0 else 0
        checks.append({
            "name": "Win Rate",
            "value": f"{result.win_rate:.1f}%",
            "threshold": f">= {self.criteria.min_win_rate}%",
            "passed": wr_pass,
        })
        score += wr_score

        # Profit factor (15 points)
        pf_pass = result.profit_factor >= self.criteria.min_profit_factor
        pf_score = min(15, (result.profit_factor / self.criteria.min_profit_factor) * 7.5) if result.profit_factor > 0 else 0
        checks.append({
            "name": "Profit Factor",
            "value": f"{result.profit_factor:.2f}",
            "threshold": f">= {self.criteria.min_profit_factor}",
            "passed": pf_pass,
        })
        score += pf_score

        # Total return (10 points)
        ret_pass = result.total_return_pct >= self.criteria.min_total_return_pct
        ret_score = min(10, (result.total_return_pct / self.criteria.min_total_return_pct) * 5) if result.total_return_pct > 0 else 0
        checks.append({
            "name": "Total Return",
            "value": f"{result.total_return_pct:.2f}%",
            "threshold": f">= {self.criteria.min_total_return_pct}%",
            "passed": ret_pass,
        })
        score += ret_score

        # Avg hold time (5 points)
        hold_pass = result.avg_bars_held <= self.criteria.max_avg_bars_held
        hold_score = max(0, 5 - (result.avg_bars_held / self.criteria.max_avg_bars_held) * 2.5) if self.criteria.max_avg_bars_held > 0 else 0
        checks.append({
            "name": "Avg Hold Time",
            "value": f"{result.avg_bars_held:.0f} bars",
            "threshold": f"<= {self.criteria.max_avg_bars_held} bars",
            "passed": hold_pass,
        })
        score += hold_score

        score = min(100, max(0, score))

        # Grade
        if score >= 80:
            grade = "A"
        elif score >= 65:
            grade = "B"
        elif score >= 50:
            grade = "C"
        elif score >= 35:
            grade = "D"
        else:
            grade = "F"

        # Recommendation
        all_critical_pass = sharpe_pass and dd_pass and trades_pass
        if all_critical_pass and score >= 65:
            recommendation = "promote"
        elif all_critical_pass and score >= 45:
            recommendation = "paper_test"
        elif score >= 35:
            recommendation = "needs_tuning"
        else:
            recommendation = "reject"

        passed = recommendation in ("promote", "paper_test")

        return EvalResult(
            passed=passed,
            grade=grade,
            score=score,
            checks=checks,
            recommendation=recommendation,
        )

    def compare(self, results: list[BacktestResult]) -> str:
        """Compare multiple backtest results side by side."""
        if not results:
            return "No results to compare"

        lines = [
            f"{'Strategy':<20} {'Symbol':<12} {'P&L':>8} {'Return':>8} {'Trades':>7} {'WR':>6} {'Sharpe':>7} {'DD':>7} {'Grade':>6}",
            f"{'─' * 85}",
        ]

        for r in results:
            ev = self.evaluate(r)
            lines.append(
                f"{r.strategy_name:<20} {r.symbol:<12} "
                f"${r.total_pnl:>7.2f} {r.total_return_pct:>7.2f}% "
                f"{r.num_trades:>6} {r.win_rate:>5.1f}% "
                f"{r.sharpe_ratio:>6.2f} {r.max_drawdown:>6.2f}% "
                f"{ev.grade:>5}"
            )

        return "\n".join(lines)
