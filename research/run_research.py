"""
Research Pipeline Runner
========================

Runs the full research pipeline:
  Strategy Research (optimize) → Backtest → Eval → Deploy

Usage:
    python -m research.run_research                    # Full pipeline, all instruments
    python -m research.run_research --symbol xyz:GOLD  # Single instrument
    python -m research.run_research --backtest-only    # Just backtest current params
    python -m research.run_research --status           # Show deployed strategies
"""
import argparse
import json
import math
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.data import DataManager
from config.settings import load_config, INSTRUMENTS
from strategies.momentum import MomentumStrategy
from strategies.ema_reversal import EmaReversalStrategy
from research.backtester import Backtester
from research.evaluator import StrategyEvaluator
from research.optimizer import StrategyOptimizer
from research.deployer import StrategyDeployer
from utils.logger import setup_logger

log = setup_logger("research")


# Parameter grids for optimization — matches Pine Script parameter space
MOMENTUM_GRID = {
    "rsi_long_min": [48, 50, 52, 55],
    "rsi_long_max": [65, 70, 75],
    "rsi_short_max": [45, 48, 50, 52],
    "rsi_short_min": [25, 30, 35],
    "atr_stop_multiplier": [0.75, 1.0, 1.5, 2.0],
    "atr_tp_multiplier": [0.0],  # Trail only — no fixed TP
    "trail_atr_multiplier": [0.5, 0.75, 1.0],
    "adx_min": [12, 14, 16, 18],
}

EMA_REVERSAL_GRID = {
    "atr_stop_multiplier": [1.0, 1.5, 2.0],
    "atr_tp_multiplier": [0.0],  # Trail only
    "trail_atr_multiplier": [0.5, 0.75, 1.0],
    "adx_min": [12, 13, 15, 18],
    "rsi_long_min": [30, 35, 40],
    "rsi_long_max": [70, 75, 80],
    "rsi_short_min": [20, 25, 30],
    "rsi_short_max": [60, 65, 70],
}


# Per-instrument pyramiding limits (from Pine Scripts)
PYRAMIDING = {
    "xyz:GOLD": 7,      # pyramiding=6
    "xyz:SILVER": 3,    # pyramiding=2
    "xyz:BRENTOIL": 4,  # pyramiding=3
}


def fetch_data(config, symbols=None):
    """Fetch historical data for backtesting."""
    data = DataManager(config)
    symbols = symbols or list(INSTRUMENTS.keys())
    candles = {}

    for symbol in symbols:
        log.info(f"Fetching data for {symbol}...")
        # Fetch 5m candles to match live trading (all available history)
        df = data.fetch_candles(symbol, interval="5m", lookback=10000)
        if not df.empty:
            candles[symbol] = df
            log.info(f"  Got {len(df)} candles for {symbol} ({len(df)*5/60:.0f} hours)")
        else:
            log.warning(f"  No data for {symbol}")

    return candles


def run_backtest_only(candles):
    """Backtest current strategy parameters (no optimization)."""
    ev = StrategyEvaluator()
    results = []

    # Strategy/symbol mapping (matches deployed configs)
    strategy_map = {
        "xyz:GOLD": [MomentumStrategy(), EmaReversalStrategy()],
        "xyz:SILVER": [MomentumStrategy()],
        "xyz:BRENTOIL": [MomentumStrategy()],
    }

    for symbol, df in candles.items():
        strategies = strategy_map.get(symbol, [MomentumStrategy()])
        max_pyr = PYRAMIDING.get(symbol, 1)

        for strategy in strategies:
            bt = Backtester(interval="5m", max_pyramiding=max_pyr)
            log.info(f"Backtesting {strategy.name} on {symbol} (pyramiding={max_pyr})...")
            result = bt.run(symbol, df, strategy)
            eval_result = ev.evaluate(result)
            results.append(result)

            print(result.summary())
            print(eval_result.summary())
            print()

    # Comparison table
    if results:
        print("\n" + ev.compare(results))


def run_full_pipeline(candles, auto_deploy=False):
    """Run the full research pipeline: optimize → eval → deploy."""
    evaluator = StrategyEvaluator()
    deployer = StrategyDeployer(min_grade="B")

    all_opt_results = []

    for symbol, df in candles.items():
        max_pyr = PYRAMIDING.get(symbol, 1)

        # Optimize momentum
        log.info(f"\n{'=' * 60}")
        log.info(f"OPTIMIZING momentum on {symbol} (pyramiding={max_pyr})")
        log.info(f"{'=' * 60}")
        optimizer = StrategyOptimizer(interval="5m")
        optimizer.backtester.max_pyramiding = max_pyr
        mom_result = optimizer.optimize(symbol, df, MomentumStrategy(), MOMENTUM_GRID)
        all_opt_results.append(mom_result)
        print(mom_result.summary())

        eval_r = evaluator.evaluate(mom_result.best_result)
        print(eval_r.summary())

        if auto_deploy:
            deployer.deploy(mom_result)

        # Optimize EMA reversal (only for GOLD)
        if symbol == "xyz:GOLD":
            log.info(f"\n{'=' * 60}")
            log.info(f"OPTIMIZING ema_reversal on {symbol} (pyramiding={max_pyr})")
            log.info(f"{'=' * 60}")
            optimizer2 = StrategyOptimizer(interval="5m")
            optimizer2.backtester.max_pyramiding = max_pyr
            ema_result = optimizer2.optimize(symbol, df, EmaReversalStrategy(), EMA_REVERSAL_GRID)
            all_opt_results.append(ema_result)
            print(ema_result.summary())

            eval_r = evaluator.evaluate(ema_result.best_result)
            print(eval_r.summary())

            if auto_deploy:
                deployer.deploy(ema_result)

    # Summary
    print(f"\n{'=' * 60}")
    print("RESEARCH COMPLETE")
    print(f"{'=' * 60}")

    best_per_symbol = {}
    for r in all_opt_results:
        key = f"{r.symbol}/{r.strategy_name}"
        best_per_symbol[key] = r

    for key, r in best_per_symbol.items():
        print(f"  {key}: Grade {r.best_grade}, Sharpe {r.best_result.sharpe_ratio:.2f}")
        print(f"    Params: {r.best_params}")

    # Deployment status
    if auto_deploy:
        print(f"\n{deployer.status()}")

    # Always save pending results for review
    save_pending_results(all_opt_results)

    return all_opt_results


PENDING_RESULTS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "pending_optimization.json"
)


def save_pending_results(opt_results):
    """Save optimization results to JSON for review before deployment."""
    deployer = StrategyDeployer(min_grade="B")
    pending = []
    for r in opt_results:
        can_deploy, reason = deployer.can_deploy(r)
        pending.append({
            "strategy": r.strategy_name,
            "symbol": r.symbol,
            "grade": r.best_grade,
            "score": r.best_score,
            "sharpe": round(r.best_result.sharpe_ratio, 2) if math.isfinite(r.best_result.sharpe_ratio) else 0,
            "win_rate": round(r.best_result.win_rate, 1) if math.isfinite(r.best_result.win_rate) else 0,
            "profit_factor": round(r.best_result.profit_factor, 2) if math.isfinite(r.best_result.profit_factor) else 0,
            "max_drawdown": round(r.best_result.max_drawdown, 2) if math.isfinite(r.best_result.max_drawdown) else 0,
            "total_return_pct": round(r.best_result.total_return_pct, 2) if math.isfinite(r.best_result.total_return_pct) else 0,
            "num_trades": r.best_result.num_trades,
            "params": r.best_params,
            "deployable": can_deploy,
            "deploy_reason": reason,
        })

    output = {
        "generated_at": __import__("datetime").datetime.now().isoformat(),
        "status": "pending_review",
        "results": pending,
    }

    os.makedirs(os.path.dirname(PENDING_RESULTS_FILE), exist_ok=True)
    with open(PENDING_RESULTS_FILE, "w") as f:
        json.dump(output, f, indent=2)

    log.info(f"Saved {len(pending)} optimization results to {PENDING_RESULTS_FILE}")
    return output


def main():
    parser = argparse.ArgumentParser(description="Research Pipeline")
    parser.add_argument("--symbol", type=str, help="Single symbol to test")
    parser.add_argument("--backtest-only", action="store_true", help="Just backtest current params")
    parser.add_argument("--auto-deploy", action="store_true", help="Auto-deploy passing strategies")
    parser.add_argument("--status", action="store_true", help="Show deployed strategies")
    args = parser.parse_args()

    if args.status:
        deployer = StrategyDeployer()
        print(deployer.status())
        return

    config = load_config()
    if args.symbol and args.symbol not in INSTRUMENTS:
        log.error(f"Invalid symbol: {args.symbol}. Valid symbols: {list(INSTRUMENTS.keys())}")
        return
    symbols = [args.symbol] if args.symbol else None
    candles = fetch_data(config, symbols)

    if not candles:
        log.error("No data fetched — cannot proceed")
        return

    if args.backtest_only:
        run_backtest_only(candles)
    else:
        run_full_pipeline(candles, auto_deploy=args.auto_deploy)


if __name__ == "__main__":
    main()
