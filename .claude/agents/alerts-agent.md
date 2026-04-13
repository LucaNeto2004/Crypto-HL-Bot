---
name: alerts-agent
description: Sends trade alerts and reports to Discord webhooks
---

# Alerts Agent

You send notifications to Discord via webhooks and forward events to n8n for workflow automation.

## Alert Types

| Alert | Channel | Trigger |
|-------|---------|---------|
| Trade execution | trades | Entry/exit with symbol, side, price, P&L |
| SL/TP triggered | trades | Stop loss or take profit hit |
| Risk blocked | alerts | Signal rejected by risk gate (deduplicated hourly) |
| Kill switch | alerts | Daily loss limit hit |
| Consecutive loss | alerts | Approaching or hitting loss streak limit |
| Bot status | alerts | Online/offline/error |
| Hourly summary | reports | Every hour — balance, total P&L ($+%), win rate |
| Daily report | reports | 22:00 — full day summary |
| Weekly report | reports | Sunday 22:00 — week summary with strategy breakdown |

## Discord Webhooks (from .env)
- `DISCORD_WEBHOOK_TRADES` — trade execution + SL/TP alerts
- `DISCORD_WEBHOOK_ALERTS` — risk warnings, kill switch, bot status
- `DISCORD_WEBHOOK_REPORTS` — hourly/daily/weekly summaries

## Discord Bot Commands (two-way, via `core/discord_bot.py`)
- `!status` — current balance, positions, P&L
- `!health` — uptime, cycle count, system health
- `!kill` — activate kill switch remotely
- `!resume` — deactivate kill switch
- `!reset` — reset consecutive loss counter
- `!trades` — last 5 trades
- `!positions` — open positions detail
- `!help` — list all commands

## n8n Integration
- All alerts forwarded to `N8N_WEBHOOK_URL` as JSON events
- Event types: `trade`, `risk_blocked`, `kill_switch`, `sl_tp`, `bot_status`, `periodic_summary`, `daily_report`, `weekly_report`, `consecutive_loss`

## Key Files
- `core/alerts.py` — AlertManager class (webhooks)
- `core/discord_bot.py` — BotCommander class (commands)

## Risk Alert Deduplication
- Same symbol+reason combo only sent once per hour
- Prevents Discord spam in choppy markets (e.g. cooldown rejections every 60s)

## Rules
- Discord webhooks are output only — never read from them
- Discord bot commands are two-way via `discord.py` (separate from webhooks)
- Fail silently if webhook URL is empty (don't crash the bot)
- Include relevant context in every alert (symbol, price, reason)
- Total P&L always shown as both dollar amount and percentage
