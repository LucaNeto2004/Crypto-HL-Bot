"""
Alerts Module — Sends notifications to Discord via webhooks.
Read-only output channel. No commands, no interaction.
Alerts are sent asynchronously via a background thread to avoid blocking the trading loop.
"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

import requests

from config.settings import BotConfig
from core.execution import TradeRecord
from utils.logger import setup_logger

log = setup_logger("alerts")

# Shared session with connection pooling — reuses TCP connections to Discord
_session = requests.Session()
_session.headers.update({"Content-Type": "application/json"})


class AlertManager:
    BOT_TAG = "[CRYPTO]"  # Prefixed to all alerts to distinguish from commodities bot

    def __init__(self, config: BotConfig):
        self.config = config
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="alert")
        self._pending: list = []
        self._lock = threading.Lock()

    def _fire(self, url: str, payload: dict, timeout: float = 5):
        """Submit a webhook POST to the thread pool for immediate delivery."""
        log.debug(f"Queuing webhook: {url[:50]}... ({len(payload)} keys)")
        fut = self._pool.submit(self._do_send, url, payload, timeout)
        with self._lock:
            self._pending.append(fut)
            # Prune completed futures
            self._pending = [f for f in self._pending if not f.done()]
            log.debug(f"Alert queue depth: {len(self._pending)}")

    @staticmethod
    def _do_send(url: str, payload: dict, timeout: float):
        for attempt in range(3):
            try:
                resp = _session.post(url, json=payload, timeout=timeout)
                if resp.status_code not in (200, 204):
                    log.warning(f"Discord webhook returned {resp.status_code}: {resp.text}")
                else:
                    log.debug(f"Webhook sent OK ({resp.status_code})")
                return
            except Exception as e:
                if attempt < 2:
                    log.debug(f"Webhook attempt {attempt + 1} failed: {e} — retrying")
                    continue
                log.error(f"Failed to send alert after 3 retries: {e}")

    # ── n8n Event Forwarding ─────────────────────────────────────────────────

    def _send_n8n_event(self, event_type: str, data: dict):
        """Forward events to n8n webhook for workflow automation."""
        url = self.config.n8n_webhook_url
        if not url:
            log.debug(f"n8n webhook not configured — skipping {event_type} event")
            return
        log.debug(f"Forwarding {event_type} event to n8n")
        payload = {
            "event": event_type,
            "timestamp": datetime.now().isoformat(),
            **data,
        }
        self._fire(url, payload)

    def send_trade_alert(self, trade: TradeRecord):
        """Send a trade notification to Discord."""
        url = self.config.discord_webhook_trades
        if not url:
            return

        color = 0x22C55E if "long" in trade.side else 0xEF4444  # Green for long, red for short
        if "close" in trade.side:
            # Color by PnL: green for win, red for loss, blue if no PnL
            if trade.pnl is not None and trade.pnl >= 0:
                color = 0x22C55E  # Green — profit
            elif trade.pnl is not None and trade.pnl < 0:
                color = 0xEF4444  # Red — loss
            else:
                color = 0x3B82F6  # Blue — no PnL

        pnl_text = ""
        if trade.pnl is not None:
            pnl_emoji = "🟢" if trade.pnl >= 0 else "🔴"
            pnl_text = f"{pnl_emoji} P&L: **${trade.pnl:+.2f}**\n"
        paper_tag = " [PAPER]" if trade.paper else ""

        embed = {
            "title": f"{self.BOT_TAG} {'📈' if 'long' in trade.side else '📉'} {trade.side.upper()} {trade.symbol}{paper_tag}",
            "color": color,
            "fields": [
                {"name": "Price", "value": f"${trade.price:.2f}", "inline": True},
                {"name": "Size", "value": f"{trade.size:.4f}", "inline": True},
                {"name": "Strategy", "value": trade.strategy, "inline": True},
            ],
            "description": pnl_text,
            "timestamp": trade.timestamp.isoformat(),
        }

        self._send(url, {"embeds": [embed]})

        self._send_n8n_event("trade", {
            "symbol": trade.symbol, "side": trade.side,
            "price": trade.price, "size": trade.size,
            "pnl": trade.pnl, "strategy": trade.strategy,
            "paper": trade.paper,
        })

    def send_risk_alert(self, symbol: str, reason: str):
        """Send a risk gate rejection alert."""
        url = self.config.discord_webhook_alerts
        if not url:
            return

        embed = {
            "title": f"{self.BOT_TAG} Risk Gate Blocked: {symbol}",
            "description": reason,
            "color": 0xF59E0B,  # Amber
            "timestamp": datetime.now().isoformat(),
        }

        self._send(url, {"embeds": [embed]})
        self._send_n8n_event("risk_blocked", {"symbol": symbol, "reason": reason})

    def send_kill_switch_alert(self):
        """Send kill switch activation alert."""
        url = self.config.discord_webhook_alerts
        if not url:
            return

        embed = {
            "title": f"{self.BOT_TAG} KILL SWITCH ACTIVATED",
            "description": "Daily loss limit reached. All trading halted until tomorrow.",
            "color": 0xDC2626,  # Red
            "timestamp": datetime.now().isoformat(),
        }

        self._send(url, {"embeds": [embed]})
        self._send_n8n_event("kill_switch", {"reason": "Daily loss limit reached"})

    def send_daily_report(self, balance: float, daily_pnl: float, trades: list[TradeRecord],
                          positions: dict, start_balance: float = 0):
        """Send end-of-day performance report."""
        url = self.config.discord_webhook_reports
        if not url:
            return

        closed = [t for t in trades if t.pnl is not None]
        winning = sum(1 for t in closed if t.pnl > 0)
        losing = sum(1 for t in closed if t.pnl < 0)
        win_rate = f"{(winning / len(closed) * 100):.0f}%" if closed else "N/A"
        daily_pct = f"{(daily_pnl / start_balance * 100):.2f}%" if start_balance > 0 else "N/A"

        position_text = "\n".join(
            f"  {sym}: {pos['side']} @ ${pos.get('entry_price', 0):.2f}"
            for sym, pos in positions.items()
        ) or "None"

        embed = {
            "title": f"{self.BOT_TAG} Daily Report",
            "color": 0x22C55E if daily_pnl >= 0 else 0xEF4444,
            "fields": [
                {"name": "Balance", "value": f"${balance:.2f}", "inline": True},
                {"name": "Daily P&L", "value": f"${daily_pnl:.2f} ({daily_pct})", "inline": True},
                {"name": "Trades", "value": f"{len(closed)} ({winning}W / {losing}L)", "inline": True},
                {"name": "Win Rate", "value": win_rate, "inline": True},
                {"name": "Open Positions", "value": position_text, "inline": False},
            ],
            "timestamp": datetime.now().isoformat(),
        }

        self._send(url, {"embeds": [embed]})

        self._send_n8n_event("daily_report", {
            "balance": balance, "daily_pnl": daily_pnl,
            "trade_count": len(trades),
            "winning": sum(1 for t in trades if t.pnl and t.pnl > 0),
            "losing": sum(1 for t in trades if t.pnl and t.pnl < 0),
            "open_positions": list(positions.keys()),
        })

    def send_periodic_summary(self, balance: float, total_pnl: float,
                               trades: list[TradeRecord], positions: dict,
                               start_balance: float, daily_pnl: float = 0.0):
        """Send a short hourly summary."""
        url = self.config.discord_webhook_reports
        if not url:
            return

        # Count ALL closed trades from start of day (cumulative daily count)
        # Entries have pnl=None; closed trades have pnl set (including breakeven at 0)
        closed = [t for t in trades if t.pnl is not None]
        winning = sum(1 for t in closed if t.pnl > 0)
        losing = sum(1 for t in closed if t.pnl < 0)
        win_rate = f"{(winning / len(closed) * 100):.0f}%" if closed else "N/A"
        pnl_pct = f"{(total_pnl / start_balance * 100):.2f}%" if start_balance > 0 else "N/A"
        daily_pct = f"{(daily_pnl / start_balance * 100):.2f}%" if start_balance > 0 else "N/A"

        pos_list = ", ".join(f"{s} ({p['side']})" for s, p in positions.items()) or "None"

        embed = {
            "title": f"{self.BOT_TAG} 📊 Hourly Summary",
            "color": 0x22C55E if total_pnl >= 0 else 0xEF4444,
            "fields": [
                {"name": "Balance", "value": f"${balance:.2f}", "inline": True},
                {"name": "Total P&L", "value": f"${total_pnl:.2f} ({pnl_pct})", "inline": True},
                {"name": "Daily P&L", "value": f"${daily_pnl:.2f} ({daily_pct})", "inline": True},
                {"name": "Trades (Today)", "value": f"{len(closed)} ({winning}W/{losing}L)", "inline": True},
                {"name": "Win Rate", "value": win_rate, "inline": True},
                {"name": "Positions", "value": pos_list, "inline": False},
            ],
            "timestamp": datetime.now().isoformat(),
        }

        self._send(url, {"embeds": [embed]})
        self._send_n8n_event("periodic_summary", {
            "balance": balance, "total_pnl": total_pnl,
            "total_pnl_pct": pnl_pct,
            "daily_trade_count": len(closed), "winning": winning, "losing": losing,
        })

    def send_weekly_report(self, balance: float, weekly_pnl: float,
                           trades: list[TradeRecord], positions: dict,
                           start_balance: float):
        """Send weekly performance report (Sunday 22:00)."""
        url = self.config.discord_webhook_reports
        if not url:
            return

        winning = sum(1 for t in trades if t.pnl and t.pnl > 0)
        losing = sum(1 for t in trades if t.pnl and t.pnl < 0)
        total_trades = winning + losing
        win_rate = f"{(winning / total_trades * 100):.0f}%" if total_trades else "N/A"
        pnl_pct = f"{(weekly_pnl / start_balance * 100):.2f}%" if start_balance > 0 else "N/A"

        # Best and worst trade
        pnl_trades = [t for t in trades if t.pnl is not None]
        best = max(pnl_trades, key=lambda t: t.pnl).pnl if pnl_trades else 0
        worst = min(pnl_trades, key=lambda t: t.pnl).pnl if pnl_trades else 0

        # Strategy breakdown
        strat_pnl: dict[str, float] = {}
        for t in pnl_trades:
            strat_pnl[t.strategy] = strat_pnl.get(t.strategy, 0) + t.pnl
        strat_text = "\n".join(
            f"  {s}: ${p:.2f}" for s, p in sorted(strat_pnl.items(), key=lambda x: x[1], reverse=True)
        ) or "None"

        pos_text = "\n".join(
            f"  {sym}: {pos['side']} @ ${pos.get('entry_price', 0):.2f}"
            for sym, pos in positions.items()
        ) or "None"

        embed = {
            "title": f"{self.BOT_TAG} 📅 Weekly Report",
            "color": 0x22C55E if weekly_pnl >= 0 else 0xEF4444,
            "fields": [
                {"name": "Balance", "value": f"${balance:.2f}", "inline": True},
                {"name": "Weekly P&L", "value": f"${weekly_pnl:.2f} ({pnl_pct})", "inline": True},
                {"name": "Trades", "value": f"{total_trades} ({winning}W / {losing}L)", "inline": True},
                {"name": "Win Rate", "value": win_rate, "inline": True},
                {"name": "Best Trade", "value": f"${best:.2f}", "inline": True},
                {"name": "Worst Trade", "value": f"${worst:.2f}", "inline": True},
                {"name": "By Strategy", "value": strat_text, "inline": False},
                {"name": "Open Positions", "value": pos_text, "inline": False},
            ],
            "timestamp": datetime.now().isoformat(),
        }

        self._send(url, {"embeds": [embed]})
        self._send_n8n_event("weekly_report", {
            "balance": balance, "weekly_pnl": weekly_pnl,
            "pnl_pct": pnl_pct, "trade_count": total_trades,
            "winning": winning, "losing": losing,
            "best_trade": best, "worst_trade": worst,
        })

    def send_consecutive_loss_alert(self, current_losses: int, max_losses: int):
        """Warn when approaching or hitting consecutive loss limit."""
        url = self.config.discord_webhook_alerts
        if not url:
            return

        if current_losses >= max_losses:
            title = f"{self.BOT_TAG} CONSECUTIVE LOSS HALT"
            desc = f"{current_losses} consecutive losses — trading halted. Manual reset required."
            color = 0xDC2626
        else:
            title = f"{self.BOT_TAG} Consecutive Loss Warning"
            desc = f"{current_losses}/{max_losses} consecutive losses — approaching halt threshold."
            color = 0xF59E0B

        embed = {
            "title": title,
            "description": desc,
            "color": color,
            "timestamp": datetime.now().isoformat(),
        }

        self._send(url, {"embeds": [embed]})
        self._send_n8n_event("consecutive_loss", {
            "current": current_losses, "max": max_losses,
            "halted": current_losses >= max_losses,
        })

    def send_sl_tp_alert(self, trade: TradeRecord):
        """Send alert for SL/TP triggered closes."""
        url = self.config.discord_webhook_trades
        if not url:
            return

        is_sl = trade.exit_reason == "stop_loss"
        title = f"{self.BOT_TAG} {'STOP LOSS' if is_sl else 'TAKE PROFIT'} — {trade.symbol}"
        pnl = trade.pnl or 0
        color = 0x22C55E if pnl >= 0 else 0xDC2626
        emoji = "🟢" if pnl >= 0 else "🔴"
        paper_tag = " [PAPER]" if trade.paper else ""

        pnl_text = f"P&L: **${trade.pnl:.2f}**" if trade.pnl is not None else ""

        embed = {
            "title": f"{emoji} {title}{paper_tag}",
            "color": color,
            "fields": [
                {"name": "Exit Price", "value": f"${trade.price:.2f}", "inline": True},
                {"name": "Size", "value": f"{trade.size:.4f}", "inline": True},
                {"name": "Strategy", "value": trade.strategy, "inline": True},
            ],
            "description": pnl_text,
            "timestamp": trade.timestamp.isoformat(),
        }

        self._send(url, {"embeds": [embed]})

        self._send_n8n_event("sl_tp", {
            "symbol": trade.symbol, "side": trade.side,
            "price": trade.price, "pnl": trade.pnl,
            "exit_reason": trade.exit_reason, "strategy": trade.strategy,
        })

    def send_bot_status(self, status: str, details: str = ""):
        """Send bot health status."""
        url = self.config.discord_webhook_alerts
        if not url:
            return

        color = 0x22C55E if status == "online" else 0xDC2626
        embed = {
            "title": f"{self.BOT_TAG} Bot Status: {status.upper()}",
            "description": details,
            "color": color,
            "timestamp": datetime.now().isoformat(),
        }

        self._send(url, {"embeds": [embed]})
        self._send_n8n_event("bot_status", {"status": status, "details": details})

    def flush(self, timeout: float = 10.0):
        """Block until all pending sends complete. Call before shutdown."""
        with self._lock:
            pending = list(self._pending)
        for fut in pending:
            try:
                fut.result(timeout=timeout)
            except Exception:
                pass

    def _send(self, webhook_url: str, payload: dict):
        """Send a Discord webhook message via thread pool."""
        self._fire(webhook_url, payload)
