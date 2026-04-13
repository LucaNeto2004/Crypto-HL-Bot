---
tracker:
  kind: linear
  project_slug: "fab84057ed77"
  active_states:
    - Todo
    - In Progress
  terminal_states:
    - Done
    - Canceled
    - Cancelled
    - Duplicate

polling:
  interval_ms: 30000

workspace:
  root: ~/symphony-workspaces

hooks:
  after_create: |
    git clone --depth 1 https://github.com/LucaNeto2004/HyperLiquid-Commodities-Bot.git .

agent:
  max_concurrent_agents: 2
  max_turns: 20

codex:
  command: claude
  approval_policy: bypassPermissions
  stall_timeout_ms: 300000

server:
  port: 3001
---

You are working on a Linear ticket `{{ issue.identifier }}` for the HyperLiquid Commodities Trading Bot.

{% if attempt %}
Continuation context:

- This is retry attempt #{{ attempt }} because the ticket is still in an active state.
- Resume from the current workspace state instead of restarting from scratch.
{% endif %}

Issue context:
Identifier: {{ issue.identifier }}
Title: {{ issue.title }}
Current status: {{ issue.state }}
Labels: {{ issue.labels }}

Description:
{% if issue.description %}
{{ issue.description }}
{% else %}
No description provided.
{% endif %}

## Project Context

This is a Python trading bot running on HyperLiquid (perpetual futures for commodities: GOLD, SILVER, BRENTOIL, NATGAS).

### Core Architecture
- `main.py` — Entry point, main loop, auto-restart
- `config/settings.py` — Instruments, risk rules, API config
- `config/deployed/*.json` — Optimized strategy parameters (auto-loaded)
- `core/data.py` — HyperLiquid API data fetching with retry
- `core/features.py` — 20+ technical indicators, regime detection (trending/ranging/transitional)
- `core/risk.py` — Hard rules gate (deterministic, never AI)
- `core/execution.py` — Paper + live trader, SL/TP management
- `core/alerts.py` — Discord webhook notifications
- `core/strategy_manager.py` — Runs strategies with deployed params, position ownership, regime filtering
- `dashboard.py` — Flask web dashboard at port 5050
- `static/js/app.js` — Dashboard frontend JavaScript

### Strategies
- `strategies/base.py` — Base class, Signal dataclass, SignalType enum
- `strategies/trend_follow.py` — EMA crossover + ADX + MACD
- `strategies/mean_reversion.py` — Bollinger Band + RSI + Stochastic
- `strategies/momentum.py` — Momentum strategy
- `strategies/squeeze_breakout.py` — Squeeze breakout strategy
- `strategies/donchian_trend.py` — Donchian channel trend strategy

### Research Pipeline
- `research/optimizer.py` — Grid search over parameter combinations
- `research/backtester.py` — Historical replay engine (no lookahead)
- `research/evaluator.py` — Strategy grading (A-F) using Sharpe, drawdown, win rate, profit factor
- `research/deployer.py` — Save/load optimized params to `config/deployed/`
- `research/run_research.py` — CLI runner

Instruments: xyz:GOLD, xyz:SILVER, xyz:BRENTOIL, xyz:NATGAS

## Rules

1. Read the CLAUDE.md file first for full project conventions.
2. Risk gate is ALWAYS hard rules, never AI.
3. Paper trading is default — never switch to live without explicit instruction.
4. NEVER auto-deploy optimized parameters — save to `data/pending_optimization.json` for human review.
5. All strategies must inherit from `BaseStrategy` in `strategies/base.py`.
6. Strategy parameters come from `config/deployed/` — don't hardcode values.
7. Test changes work by checking imports and running `python -c "from module import Class"`.
8. Keep fixes minimal and focused — don't refactor surrounding code.

## Workflow

Determine ticket type from its team label and follow the matching flow:

### Bug Fixes (OPS team)
1. Read the ticket description and understand the bug.
2. Read the relevant source files to understand the current behavior.
3. Implement the fix with minimal changes.
4. Test the fix works.
5. Commit with a clear message referencing the ticket.
6. Push and create a PR.
7. Move ticket to "In Review".

### Strategy & Research (DEV team)

#### Parameter Deployment (ticket describes specific param changes)
1. Read the ticket description for the exact file, param names, and new values.
2. Update the JSON file(s) in `config/deployed/` with the specified values only.
3. If the ticket includes a **Validation command**, run that exact command — do NOT invent your own backtest.
4. If the ticket includes **Expected results** (e.g. "Grade B or better, WR > 40%"), check the backtest output matches. If it fails, still create the PR but note the discrepancy — do NOT block deployment, the research agent's original results take priority.
5. If no validation command is provided, do NOT run backtests. Just deploy the params.
6. Commit, push, and create a PR.
7. Move ticket to "In Review".

#### New Strategy / Code Changes
1. Read the ticket description and understand what strategy work is needed.
2. Read relevant strategy files, features, and research pipeline code.
3. Implement changes (new strategy, backtest improvements, etc.).
4. Run the validation command from the ticket, or `python -m research.run_research --backtest-only` if none specified.
5. Include backtest results in the PR description.
6. Commit, push, and create a PR with backtest results.
7. Move ticket to "In Review".

## Instructions

1. This is an unattended session. Never ask a human to perform follow-up actions.
2. Only stop early for a true blocker (missing auth/permissions).
3. Work only in the provided repository copy.
4. Keep changes minimal and focused on the ticket scope.
5. Always validate strategy changes with backtests before submitting.
6. Never deploy parameters directly — save to pending review.
