"""
Discord Command Bot — Receive commands from Discord to control the bot.
Runs in a separate thread alongside the main trading loop.

Commands:
  !status   — Bot status, balance, positions, P&L
  !health   — Health check (uptime, cycle count, errors)
  !kill     — Activate kill switch (halt all trading)
  !resume   — Deactivate kill switch
  !reset    — Reset consecutive loss halt
  !trades   — Recent trade history
  !positions — Open positions detail
"""
import asyncio
import threading
from datetime import datetime
from typing import Optional

import discord

from utils.logger import setup_logger

log = setup_logger("discord_bot")


class BotCommander:
    """Discord bot that accepts commands to query/control the trading bot."""

    def __init__(self, token: str, bot_ref):
        """
        Args:
            token: Discord bot token
            bot_ref: Reference to the TradingBot instance
        """
        self.token = token
        self.bot_ref = bot_ref
        self.client: Optional[discord.Client] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._start_time = datetime.now()

    def start(self):
        """Start the Discord bot in a background thread."""
        if not self.token:
            log.info("No Discord bot token — command bot disabled")
            return

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("Discord command bot starting...")

    def stop(self):
        """Disconnect the Discord bot cleanly (prevents duplicate connections on restart)."""
        if self.client and self._loop:
            try:
                asyncio.run_coroutine_threadsafe(self.client.close(), self._loop).result(timeout=5)
                log.info("Discord bot disconnected")
            except Exception as e:
                log.warning(f"Discord bot disconnect error: {e}")

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        intents = discord.Intents.default()
        intents.message_content = True
        self.client = discord.Client(intents=intents)

        @self.client.event
        async def on_ready():
            log.info(f"Discord bot connected as {self.client.user}")

        @self.client.event
        async def on_message(message):
            if message.author.bot:
                return
            if not message.content.startswith("!"):
                return

            cmd = message.content.strip().lower().split()[0]
            try:
                response = self._handle_command(cmd)
                if response:
                    await message.channel.send(response)
            except Exception as e:
                log.error(f"Discord command error: {e}")
                await message.channel.send(f"Error: {e}")

        try:
            self._loop.run_until_complete(self.client.start(self.token))
        except Exception as e:
            log.error(f"Discord bot failed: {e}")

    def _handle_command(self, cmd: str) -> Optional[str]:
        bot = self.bot_ref

        if cmd == "!status":
            return self._cmd_status()
        elif cmd == "!health":
            return self._cmd_health()
        elif cmd == "!kill":
            return self._cmd_kill()
        elif cmd == "!resume":
            return self._cmd_resume()
        elif cmd == "!reset":
            return self._cmd_reset()
        elif cmd == "!trades":
            return self._cmd_trades()
        elif cmd == "!positions":
            return self._cmd_positions()
        elif cmd == "!help":
            return self._cmd_help()
        return None

    def _cmd_status(self) -> str:
        bot = self.bot_ref
        balance = bot.risk.portfolio.account_balance
        positions = bot.risk.portfolio.positions
        mode = "PAPER" if bot.config.paper_trading else "LIVE"
        network = "TESTNET" if bot.config.testnet else "MAINNET"

        trades = bot.execution.get_trade_history()
        # Count only closed trades (pnl is not None) — consistent with hourly/daily
        closed = [t for t in trades if t.pnl is not None]
        session_pnl = sum(t.pnl for t in closed) if closed else 0
        won = sum(1 for t in closed if t.pnl > 0)
        lost = sum(1 for t in closed if t.pnl < 0)
        win_rate = f"{won/(won+lost)*100:.0f}%" if (won + lost) > 0 else "N/A"

        # Daily P&L = realized from today's closed trades + unrealized (matches dashboard)
        today = datetime.now().strftime("%Y-%m-%d")
        today_closed = [t for t in closed if t.timestamp.strftime("%Y-%m-%d") == today]
        daily_pnl = sum(t.pnl for t in today_closed) if today_closed else 0.0
        positions = bot.risk.portfolio.positions
        for pos in positions.values():
            daily_pnl += pos.get("unrealized_pnl", 0)

        pos_text = "\n".join(
            f"  {s}: {p['side']} @ ${p.get('entry_price', 0):.2f}"
            for s, p in positions.items()
        ) or "  None"

        kill = "YES" if bot.risk.kill_switch else "No"
        loss_halt = "YES" if bot.risk.consecutive_loss_halt else "No"

        today_won = sum(1 for t in today_closed if t.pnl > 0)
        today_lost = sum(1 for t in today_closed if t.pnl < 0)

        start_bal = bot.risk.portfolio.starting_balance or balance
        daily_pct = f" ({daily_pnl / start_bal * 100:.2f}%)" if start_bal > 0 else ""
        total_pct = f" ({session_pnl / start_bal * 100:.2f}%)" if start_bal > 0 else ""

        return (
            f"```\n"
            f"═══ BOT STATUS ═══\n"
            f"Mode:        {mode} | {network}\n"
            f"Balance:     ${balance:.2f}\n"
            f"Daily P&L:   ${daily_pnl:.2f}{daily_pct}\n"
            f"Total P&L:   ${session_pnl:.2f}{total_pct}\n"
            f"Trades:      {len(closed)} ({won}W / {lost}L | {win_rate})\n"
            f"Today:       {len(today_closed)} ({today_won}W / {today_lost}L)\n"
            f"Kill Switch: {kill}\n"
            f"Loss Halt:   {loss_halt} ({bot.risk.consecutive_losses} streak)\n"
            f"Positions:\n{pos_text}\n"
            f"```"
        )

    def _cmd_health(self) -> str:
        bot = self.bot_ref
        uptime = datetime.now() - self._start_time
        hours = int(uptime.total_seconds() // 3600)
        mins = int((uptime.total_seconds() % 3600) // 60)

        return (
            f"```\n"
            f"═══ HEALTH CHECK ═══\n"
            f"Status:  RUNNING\n"
            f"Uptime:  {hours}h {mins}m\n"
            f"Mode:    {'PAPER' if bot.config.paper_trading else 'LIVE'}\n"
            f"Balance: ${bot.risk.portfolio.account_balance:.2f}\n"
            f"Kill Switch: {'ACTIVE' if bot.risk.kill_switch else 'Off'}\n"
            f"Loss Halt:   {'ACTIVE' if bot.risk.consecutive_loss_halt else 'Off'}\n"
            f"Losses:  {bot.risk.consecutive_losses} consecutive\n"
            f"```"
        )

    def _cmd_kill(self) -> str:
        self.bot_ref.risk.kill_switch = True
        log.warning("KILL SWITCH activated via Discord command")
        return "🚨 **KILL SWITCH ACTIVATED** — All trading halted. Use `!resume` to resume."

    def _cmd_resume(self) -> str:
        self.bot_ref.risk.kill_switch = False
        log.info("Kill switch deactivated via Discord command")
        return "✅ **Kill switch deactivated** — Trading resumed."

    def _cmd_reset(self) -> str:
        self.bot_ref.risk.reset_consecutive_losses()
        log.info("Consecutive loss counter reset via Discord command")
        return "✅ **Consecutive loss counter reset** — Trading resumed."

    def _cmd_trades(self) -> str:
        trades = self.bot_ref.execution.get_trade_history()
        if not trades:
            return "No trades yet."

        recent = trades[-10:]
        lines = ["```", "═══ RECENT TRADES ═══"]
        for t in reversed(recent):
            pnl = f"${t.pnl:.2f}" if t.pnl is not None else "—"
            time = t.timestamp.strftime("%H:%M")
            lines.append(f"  {time} {t.side:12s} {t.symbol:12s} ${t.price:.2f}  P&L:{pnl}")
        lines.append("```")
        return "\n".join(lines)

    def _cmd_positions(self) -> str:
        # Read from paper trader which has SL/TP data
        positions = self.bot_ref.execution.paper.positions if self.bot_ref.config.paper_trading else self.bot_ref.risk.portfolio.positions
        if not positions:
            return "No open positions."

        lines = ["```", "═══ OPEN POSITIONS ═══"]
        for sym, p in positions.items():
            sl = f"${p.get('stop_loss', 0):.2f}" if p.get('stop_loss') else "—"
            tp = f"${p.get('take_profit', 0):.2f}" if p.get('take_profit') else "—"
            unrealized = p.get("unrealized_pnl")
            upnl = f"  uPnL: ${unrealized:.2f}" if unrealized else ""
            lines.append(
                f"  {sym}: {p['side']} @ ${p.get('entry_price', 0):.2f}\n"
                f"    Size: ${p.get('size_usd', 0):.2f}  SL: {sl}  TP: {tp}{upnl}"
            )
        lines.append("```")
        return "\n".join(lines)

    def _cmd_help(self) -> str:
        return (
            "```\n"
            "═══ BOT COMMANDS ═══\n"
            "!status    — Balance, P&L, positions\n"
            "!health    — Uptime, health check\n"
            "!kill      — Activate kill switch\n"
            "!resume    — Deactivate kill switch\n"
            "!reset     — Reset consecutive loss halt\n"
            "!trades    — Last 10 trades\n"
            "!positions — Open positions detail\n"
            "!help      — This message\n"
            "```"
        )
