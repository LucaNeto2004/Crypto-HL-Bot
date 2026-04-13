"""
Deployment Agent — Promotes optimized strategy parameters to live config.

Takes the best parameters from optimization and writes them to a
deployment config that the live bot reads on startup.
"""
import json
import os
from datetime import datetime
from typing import Optional

from research.optimizer import OptimizationResult
from research.evaluator import StrategyEvaluator, EvalCriteria
from utils.logger import setup_logger

log = setup_logger("deployer")

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY_DIR = os.path.join(_BASE_DIR, "config", "deployed")


class StrategyDeployer:
    def __init__(self, min_grade: str = "B"):
        self.min_grade = min_grade
        self.grade_order = {"A": 4, "B": 3, "C": 2, "D": 1, "F": 0}

    def can_deploy(self, opt_result: OptimizationResult) -> tuple[bool, str]:
        """Check if optimization result meets deployment criteria."""
        result_rank = self.grade_order.get(opt_result.best_grade, 0)
        required_rank = self.grade_order.get(self.min_grade, 3)

        if result_rank < required_rank:
            return False, f"Grade {opt_result.best_grade} below minimum {self.min_grade}"

        if opt_result.best_result.num_trades < 5:
            return False, f"Only {opt_result.best_result.num_trades} trades — not enough data"

        return True, "Meets deployment criteria"

    def deploy(self, opt_result: OptimizationResult, force: bool = False) -> bool:
        """
        Save optimized parameters to deployment config.

        Creates a JSON file per strategy+symbol that the live bot
        can load to override default parameters.
        """
        can, reason = self.can_deploy(opt_result)
        if not can and not force:
            log.warning(f"Cannot deploy: {reason}")
            return False

        os.makedirs(DEPLOY_DIR, exist_ok=True)

        filename = f"{opt_result.strategy_name}_{opt_result.symbol.replace(':', '_')}.json"
        filepath = os.path.join(DEPLOY_DIR, filename)

        deployment = {
            "strategy": opt_result.strategy_name,
            "symbol": opt_result.symbol,
            "parameters": opt_result.best_params,
            "metrics": {
                "grade": opt_result.best_grade,
                "score": opt_result.best_score,
                "sharpe": opt_result.best_result.sharpe_ratio,
                "max_drawdown": opt_result.best_result.max_drawdown,
                "win_rate": opt_result.best_result.win_rate,
                "profit_factor": opt_result.best_result.profit_factor,
                "num_trades": opt_result.best_result.num_trades,
                "total_return_pct": opt_result.best_result.total_return_pct,
            },
            "deployed_at": datetime.now().isoformat(),
            "forced": force,
        }

        with open(filepath, "w") as f:
            json.dump(deployment, f, indent=2)

        log.info(f"Deployed {opt_result.strategy_name} for {opt_result.symbol} → {filepath}")
        return True

    def load_deployed_params(self, strategy_name: str, symbol: str) -> Optional[dict]:
        """Load deployed parameters for a strategy+symbol."""
        filename = f"{strategy_name}_{symbol.replace(':', '_')}.json"
        filepath = os.path.join(DEPLOY_DIR, filename)

        if not os.path.exists(filepath):
            return None

        try:
            with open(filepath, "r") as f:
                deployment = json.load(f)
            return deployment.get("parameters")
        except Exception as e:
            log.error(f"Failed to load deployed params: {e}")
            return None

    def list_deployed(self) -> list[dict]:
        """List all deployed strategy configs."""
        if not os.path.exists(DEPLOY_DIR):
            return []

        deployed = []
        for filename in sorted(os.listdir(DEPLOY_DIR)):
            if filename.endswith(".json"):
                filepath = os.path.join(DEPLOY_DIR, filename)
                try:
                    with open(filepath, "r") as f:
                        deployed.append(json.load(f))
                except Exception:
                    pass
        return deployed

    def status(self) -> str:
        """Print deployment status."""
        deployed = self.list_deployed()
        if not deployed:
            return "No strategies deployed"

        lines = [
            f"{'Strategy':<20} {'Symbol':<15} {'Grade':>6} {'Sharpe':>7} {'WR':>6} {'Deployed'}",
            f"{'─' * 75}",
        ]
        for d in deployed:
            m = d.get("metrics", {})
            lines.append(
                f"{d['strategy']:<20} {d['symbol']:<15} "
                f"{m.get('grade', '?'):>5} {m.get('sharpe', 0):>6.2f} "
                f"{m.get('win_rate', 0):>5.1f}% {d.get('deployed_at', '?')[:16]}"
            )
        return "\n".join(lines)
