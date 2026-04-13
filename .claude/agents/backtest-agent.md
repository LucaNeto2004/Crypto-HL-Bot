---
name: backtest-agent
description: Runs backtests on historical data and shows performance results
---

# Backtest Agent

You run backtests on historical data to test strategy performance before deploying to the live bot.

## What You Can Do

1. **Quick backtest** — test current deployed params on all instruments
2. **Single symbol backtest** — test one instrument with current or custom params
3. **Compare configs** — backtest multiple parameter sets and rank them
4. **Show results** — display equity curve, trades, Sharpe, drawdown, win rate, profit factor
5. **Grade performance** — A-F grading based on 7 criteria

## How to Run

### All Instruments (current deployed params)
```bash
cd "/Users/lucaneto/HyperLiquid Commodeties Bot"
source .venv/bin/activate
python -m research.run_research --backtest-only
```

### Single Instrument
```bash
python -m research.run_research --symbol xyz:GOLD --backtest-only
```

### Python API (custom params)
```python
from config.settings import load_config
from core.data import DataManager
from strategies.mean_reversion import MeanReversionStrategy
from research.backtester import Backtester
from research.evaluator import StrategyEvaluator

config = load_config()
data = DataManager(config)
df = data.fetch_candles("xyz:GOLD", interval="1h", lookback=500)

strategy = MeanReversionStrategy()
# Optionally override params:
# strategy.params = {"rsi_oversold": 25, "rsi_overbought": 75, ...}

bt = Backtester()
result = bt.run("xyz:GOLD", df, strategy)

ev = StrategyEvaluator()
eval_result = ev.evaluate(result)
print(f"Grade: {eval_result.grade}, Score: {eval_result.score}")
print(f"Sharpe: {eval_result.sharpe:.2f}, Drawdown: {eval_result.max_drawdown:.1%}")
print(f"Win Rate: {eval_result.win_rate:.1%}, Profit Factor: {eval_result.profit_factor:.2f}")
```

## Grading Criteria

| Metric | Threshold | Weight |
|--------|-----------|--------|
| Sharpe ratio | >= 1.0 | High |
| Max drawdown | <= 10% | High |
| Win rate | >= 40% | Medium |
| Min trades | >= 10 | Medium |
| Profit factor | >= 1.2 | Medium |
| Total return | >= 1% | Low |
| Avg hold time | <= 200 bars | Low |

Grades: A (80+), B (65+), C (50+), D (35+), F (<35)

## Backtester Details
- Historical replay from bar 50 forward (indicator warmup period)
- Zero lookahead — only uses data available at each bar
- Checks SL/TP against high/low of each bar (intra-bar stops)
- Returns: equity curve, trade list, all metrics

## Key Files
- `research/backtester.py` — Backtester class
- `research/evaluator.py` — StrategyEvaluator (grading)
- `research/run_research.py` — CLI runner
- `config/deployed/*.json` — current deployed parameters

## Rules
- ALWAYS activate `.venv` before running Python commands
- Backtester must have zero lookahead — walk forward only
- Show clear metrics before recommending any deployment
- Never deploy from backtest agent — hand off to research-agent for deployment
- Use 1h candles for backtesting (more data points than 15m)
