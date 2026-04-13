"""Backtest Range Flip strategy with the 4 starting parameter combos."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.data import DataManager
from config.settings import load_config, INSTRUMENTS
from strategies.range_flip import RangeFlipStrategy
from research.backtester import Backtester
from research.evaluator import StrategyEvaluator
from utils.logger import setup_logger

log = setup_logger("backtest_rf")

COMBOS = {
    "conservative": {
        "kc_ema_window": 20, "kc_atr_mult_entry": 2.5, "kc_atr_mult_exit": 1.0,
        "rsi_buy_threshold": 30, "rsi_sell_threshold": 70, "adx_max": 20,
        "atr_stop_multiplier": 3.0, "tp_mode": "opposite_band",
        "min_hold_bars": 3, "cooldown_bars": 2, "flip_on_exit": False,
    },
    "aggressive": {
        "kc_ema_window": 15, "kc_atr_mult_entry": 1.5, "kc_atr_mult_exit": 0.5,
        "rsi_buy_threshold": 45, "rsi_sell_threshold": 55, "adx_max": 25,
        "atr_stop_multiplier": 2.0, "tp_mode": "fixed", "atr_tp_multiplier": 2.5,
        "min_hold_bars": 1, "cooldown_bars": 0, "flip_on_exit": True,
    },
    "balanced": {
        "kc_ema_window": 20, "kc_atr_mult_entry": 2.0, "kc_atr_mult_exit": 1.0,
        "rsi_buy_threshold": 35, "rsi_sell_threshold": 65, "adx_max": 22,
        "atr_stop_multiplier": 2.5, "tp_mode": "opposite_band",
        "use_stoch": True, "stoch_buy_threshold": 25, "stoch_sell_threshold": 75,
        "min_hold_bars": 2, "cooldown_bars": 1, "flip_on_exit": False,
    },
    "stoch_heavy": {
        "kc_ema_window": 25, "kc_atr_mult_entry": 2.0, "kc_atr_mult_exit": 0.8,
        "rsi_buy_threshold": 40, "rsi_sell_threshold": 60, "adx_max": 20,
        "atr_stop_multiplier": 2.0, "tp_mode": "fixed", "atr_tp_multiplier": 3.0,
        "use_stoch": True, "stoch_buy_threshold": 20, "stoch_sell_threshold": 80,
        "min_hold_bars": 2, "cooldown_bars": 2, "flip_on_exit": True,
    },
}


def make_strategy(params: dict) -> RangeFlipStrategy:
    s = RangeFlipStrategy()
    for k, v in params.items():
        setattr(s, k, v)
    return s


def main():
    config = load_config()
    data = DataManager(config)
    bt = Backtester(interval="4h")
    ev = StrategyEvaluator()

    symbols = list(INSTRUMENTS.keys())
    candles = {}
    for symbol in symbols:
        df = data.fetch_candles(symbol, interval="4h", lookback=500)
        if not df.empty:
            candles[symbol] = df
            print(f"Fetched {len(df)} candles for {symbol}")

    if not candles:
        print("No data fetched!")
        return

    all_results = []

    for combo_name, params in COMBOS.items():
        print(f"\n{'=' * 60}")
        print(f"  COMBO: {combo_name.upper()}")
        print(f"  Params: {params}")
        print(f"{'=' * 60}")

        for symbol, df in candles.items():
            strategy = make_strategy(params)
            result = bt.run(symbol, df, strategy)
            eval_result = ev.evaluate(result)
            all_results.append((combo_name, symbol, result, eval_result))

            print(result.summary())
            print(eval_result.summary())
            print()

    # Comparison table
    print(f"\n{'=' * 80}")
    print("  RANGE FLIP — ALL COMBOS COMPARISON")
    print(f"{'=' * 80}")
    print(f"  {'Combo':<15} {'Symbol':<15} {'Trades':>6} {'WR%':>6} {'PF':>6} {'Sharpe':>7} {'DD%':>6} {'Ret%':>7} {'Grade'}")
    print(f"  {'-' * 75}")

    for combo_name, symbol, result, eval_result in all_results:
        short_sym = symbol.replace("xyz:", "")
        print(
            f"  {combo_name:<15} {short_sym:<15} "
            f"{result.num_trades:>6} "
            f"{result.win_rate:>5.1f}% "
            f"{result.profit_factor:>6.2f} "
            f"{result.sharpe_ratio:>7.2f} "
            f"{result.max_drawdown:>5.1f}% "
            f"{result.total_return_pct:>+6.2f}% "
            f"{eval_result.grade}"
        )

    # Best per symbol
    print(f"\n  BEST COMBO PER SYMBOL:")
    print(f"  {'-' * 50}")
    for symbol in candles:
        symbol_results = [(c, r, e) for c, s, r, e in all_results if s == symbol and r.num_trades > 0]
        if symbol_results:
            best = max(symbol_results, key=lambda x: x[1].sharpe_ratio)
            short_sym = symbol.replace("xyz:", "")
            print(f"  {short_sym:<15} → {best[0]:<15} (Sharpe={best[1].sharpe_ratio:.2f}, WR={best[1].win_rate:.1f}%, Grade={best[2].grade})")


if __name__ == "__main__":
    main()
