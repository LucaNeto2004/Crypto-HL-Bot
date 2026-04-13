#!/bin/bash
# ═══════════════════════════════════════════════════════
# HyperLiquid Commodities Bot — Startup Script
# Run: ./start.sh
# ═══════════════════════════════════════════════════════

BOT_DIR="/Users/lucaneto/HyperLiquid Commodeties Bot"
SYMPHONY_DIR="/Users/lucaneto/symphony-claude"

echo "═══════════════════════════════════════════"
echo "  HyperLiquid Commodities Bot — Startup"
echo "═══════════════════════════════════════════"
echo ""

# Add Node.js to PATH
export PATH="/opt/homebrew/opt/node@22/bin:$PATH"

# Load env
set -a
source "$BOT_DIR/.env"
set +a

cd "$BOT_DIR"
source .venv/bin/activate

# Kill any existing processes
echo "[1/4] Cleaning up old processes..."
kill $(lsof -ti :5050) 2>/dev/null
sleep 1

# Start Dashboard
echo "[2/4] Starting Dashboard (port 5050)..."
nohup python dashboard.py >> logs/dashboard.log 2>&1 & disown
echo "       Dashboard: http://localhost:5050"

# Start Bot
echo "[3/4] Starting Trading Bot..."
nohup python main.py >> logs/bot_live.log 2>&1 & disown
echo "       Logs: tail -f logs/bot_live.log"

# Start Symphony
echo "[4/4] Starting Symphony (port 3001)..."
cd "$SYMPHONY_DIR"
nohup node dist/index.js "$BOT_DIR/WORKFLOW.md" --no-tui --port 3001 >> "$BOT_DIR/logs/symphony.log" 2>&1 & disown
echo "       Symphony: http://localhost:3001"

echo ""
echo "═══════════════════════════════════════════"
echo "  All systems running!"
echo ""
echo "  Dashboard:  http://localhost:5050"
echo "  Symphony:   http://localhost:3001"
echo "  Bot logs:   tail -f logs/bot_live.log"
echo "  Symphony:   tail -f logs/symphony.log"
echo "═══════════════════════════════════════════"
echo ""
echo "  💬 To connect Claude to Discord, start Claude Code with:"
echo "     claude --channels plugin:discord@claude-plugins-official"
echo "═══════════════════════════════════════════"
