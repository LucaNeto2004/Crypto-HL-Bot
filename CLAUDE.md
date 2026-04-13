# HyperLiquid Crypto Trading Bot

## Architecture

Two pipelines:

### Live Trading Pipeline
`Data → Features → Strategy → Risk Gate → Execution → Alerts`

Runs every 35s. Fetches 5M candles for ETH and HYPE on HyperLiquid (native perps), computes 20+ indicators, classifies market regime, runs Momentum v15 strategy with per-symbol deployed params, passes signals through hard-rules risk gate, executes paper or live orders with SL/trailing stop.

### Research Pipeline
`Strategy Research (optimizer) → Backtest → Eval → Deploy`

Grid search over parameter combinations, replay historical candles with no lookahead, evaluate with Sharpe/drawdown/win-rate/profit-factor, grade A-F, auto-deploy passing configs to `config/deployed/`.

## Instruments

| Symbol | Name | Group | Strategy |
|--------|------|-------|----------|
| ETH | Ethereum | crypto | momentum_v15 |
| HYPE | HyperLiquid | crypto | momentum_v15 |

## Key Files

| File | Purpose |
|------|---------|
| `main.py` | Entry point, main loop, auto-restart |
| `config/settings.py` | Instruments, risk rules, API config |
| `config/deployed/*.json` | Optimized strategy parameters (auto-loaded) |
| `core/data.py` | HyperLiquid API data fetching with retry |
| `core/features.py` | Technical indicators + regime detection + support/resistance |
| `core/strategy_manager.py` | Runs strategies with deployed params |
| `core/risk.py` | Hard rules gate (not AI, deterministic) |
| `core/execution.py` | Paper + live trader, SL/TP management |
| `core/alerts.py` | Discord webhook notifications |
| `core/webhook.py` | TradingView webhook receiver (port 5061) |
| `strategies/momentum_v15.py` | EMA stack + RSI + MACD + regime filter (choppy/trend/vol) |
| `strategies/base.py` | Abstract strategy interface |
| `research/backtester.py` | Historical replay engine |
| `research/evaluator.py` | Strategy grading (A-F) |
| `research/optimizer.py` | Grid search parameter optimization |
| `research/deployer.py` | Save/load optimized params |
| `research/run_research.py` | CLI runner for research pipeline |
| `dashboard.py` | Web dashboard (port 5060) |

## Commands

```bash
# Run live bot (paper mode)
source .venv/bin/activate
python main.py

# Run in background
nohup python main.py >> logs/bot_live.log 2>&1 & disown

# Backtest current parameters
python -m research.run_research --backtest-only

# Full optimization + auto-deploy
python -m research.run_research --auto-deploy

# Single instrument optimization
python -m research.run_research --symbol ETH

# Check deployed strategies
python -m research.run_research --status

# Monitor live bot
tail -f logs/bot_live.log
```

## Strategy: Momentum v15

| Parameter | Value | Source |
|-----------|-------|--------|
| Timeframe | 5M | TV backtest |
| Pyramiding | 3 (max 4 positions) | TV backtest |
| Qty per entry | 20% of equity | TV backtest |
| ATR Stop Mult | 0.7 | TV Gold v15 |
| Trail ATR Mult | 0.3 | TV Gold v15 |
| RSI Long | 50-80 | TV Gold v15 |
| RSI Short | 20-50 | TV Gold v15 |
| Signal Rev RSI | Long<40, Short>60 | TV Gold v15 |
| Max ATR Ratio | 1.5 | OOS validated |
| Choppy Mult | 0.8 | TV Gold v15 |
| Min Slope | 0.1% | TV Gold v15 |

**Entry Long:** close[1] > EMA9[1] > EMA21[1], close[1] > EMA50[1], RSI 50-80, MACD hist > 0, regime OK
**Entry Short:** close[1] < EMA9[1] < EMA21[1], close[1] < EMA50[1], RSI 20-50, MACD hist < 0, regime OK
**Exit:** Signal reversal (RSI + MACD flip) OR trailing stop OR stop loss
**Regime filter:** Skip choppy (ATR < 0.8*ATR_MA), weak trend (EMA50 slope < 0.1%), extreme vol (ATR/ATR_MA > 1.5)

## Risk Gate Rules

- Max portfolio leverage: 5.0x
- Max single position: 100% (pyramiding handles sizing)
- Max group exposure: 500% (disabled — both crypto)
- Max daily loss: 5% → kill switch
- Max daily trades: unlimited
- Max open positions: 8 (ETH 4 + HYPE 4)
- Max consecutive losses: 5 → halt (paper mode skips)
- Account drawdown: 15% from peak → halt
- Trading hours: 24/7 (crypto never closes)

## Ports

| Service | Port |
|---------|------|
| Dashboard | 5060 |
| TV Webhook | 5061 |

(Commodities bot uses 5050/5051 — no conflicts)

## Conventions

- Risk gate is ALWAYS hard rules, never AI
- Strategies use deployed params from `config/deployed/` — don't hardcode
- Paper trading is default — set `paper_trading: false` explicitly for live
- HyperLiquid native perp symbols: ETH, HYPE (no prefix)
- Network errors retry 3 times with exponential backoff
- Bot auto-restarts up to 10 times on crash
- Always commit and push after code changes — never leave uncommitted work
- Trading hours: 24/7 (crypto markets)
- Never auto-deploy optimized params — always ask the user first
