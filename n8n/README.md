# n8n Integration

Self-hosted workflow automation for the HyperLiquid Commodities Bot.

## Setup

### 1. Install n8n (Docker — recommended)

```bash
docker run -d \
  --name n8n \
  -p 5678:5678 \
  -v n8n_data:/home/node/.n8n \
  -e N8N_SECURE_COOKIE=false \
  --restart unless-stopped \
  n8nio/n8n
```

Open http://localhost:5678 and create an account.

### 2. Set Environment Variables in n8n

Go to **Settings → Variables** and add:

| Variable | Value |
|----------|-------|
| `DISCORD_WEBHOOK_TRADES` | Your Discord webhook URL for trade alerts |
| `DISCORD_WEBHOOK_ALERTS` | Your Discord webhook URL for risk/status alerts |
| `DISCORD_WEBHOOK_REPORTS` | Your Discord webhook URL for daily/weekly reports |

### 3. Import Workflows

Go to **Workflows → Import from File** and import:

- `workflows/daily_report.json` — Sends daily P&L report at 22:00
- `workflows/weekly_optimization.json` — Runs full optimization every Sunday at 06:00
- `workflows/event_router.json` — Receives bot events and routes to Discord channels

### 4. Configure the Bot

Add the n8n webhook URL to your `.env`:

```bash
# After importing event_router.json, n8n gives you a webhook URL like:
N8N_WEBHOOK_URL=http://localhost:5678/webhook/hl-bot-events
```

### 5. Activate Workflows

Click the toggle on each workflow to activate it.

## Workflows

### Daily Report (`daily_report.json`)
- **Trigger:** Every day at 22:00
- **Action:** Fetches `/api/report` from dashboard, formats rich Discord embed with balance, P&L, trade stats
- **Requires:** Dashboard running at localhost:5050

### Weekly Optimization (`weekly_optimization.json`)
- **Trigger:** Every Sunday at 06:00
- **Action:** Calls `/api/trigger/research` to run full optimization pipeline, waits 2 min, polls result, notifies Discord
- **Requires:** Dashboard running at localhost:5050

### Event Router (`event_router.json`)
- **Trigger:** Webhook receives POST from bot
- **Action:** Routes events by type to appropriate Discord channels
- **Events:** trade, risk_blocked, kill_switch, sl_tp, bot_status, daily_report, consecutive_loss

## API Endpoints (for n8n)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/state` | GET | Full bot state (balance, positions, indicators) |
| `/api/report` | GET | Formatted report (P&L by strategy/symbol, win rate) |
| `/api/deployed` | GET | List deployed strategy configs |
| `/api/trigger/research` | POST | Start optimization run (body: `{"symbol": "xyz:GOLD", "auto_deploy": true}`) |
| `/api/job/<id>` | GET | Poll background job status |

## Architecture

```
Bot (main.py) ──→ n8n Event Router ──→ Discord (trades, alerts)
                                   ──→ Telegram (optional)
                                   ──→ Email (optional)

n8n Schedule ──→ Dashboard API ──→ Discord (daily/weekly reports)
             ──→ Research API  ──→ Optimization ──→ Auto-deploy
```
