#!/bin/bash
# Stop all bot processes

echo "Stopping all services..."

# Dashboard
kill $(lsof -ti :5050) 2>/dev/null && echo "  Dashboard stopped" || echo "  Dashboard not running"

# Symphony
kill $(lsof -ti :3001) 2>/dev/null && echo "  Symphony stopped" || echo "  Symphony not running"

# Bot (find python main.py process)
pkill -f "python main.py" 2>/dev/null && echo "  Bot stopped" || echo "  Bot not running"

echo "All services stopped."
