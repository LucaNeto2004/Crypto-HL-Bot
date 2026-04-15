"""
HyperLiquid Crypto Trading Bot
===============================

Main loop: Data → Features → Strategy → Risk Gate → Execution → Alerts

Instruments: ETH, HYPE
Exchange: HyperLiquid (Perpetual Futures)
"""
import os
import sys
import time
import signal as sig
import fcntl
import threading
from datetime import datetime, timedelta
from typing import Optional

from config.settings import load_config
from core.data import DataManager
from core.strategy_manager import StrategyManager
from core.risk import RiskGate
from core.execution import ExecutionEngine
from core.alerts import AlertManager
from core.audit import AuditJournal
from core.discord_bot import BotCommander
from strategies.momentum_v15 import MomentumV15Strategy
from strategies.base import SignalType
from utils.logger import setup_logger

log = setup_logger("main")

# Set terminal tab title
print("\033]0;main.py\007", end="", flush=True)

# Ensure directories exist (absolute paths relative to script location)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(_BASE_DIR, "logs"), exist_ok=True)
os.makedirs(os.path.join(_BASE_DIR, "data"), exist_ok=True)


class TradingBot:
    def __init__(self):
        self.config = load_config()
        self.running = False

        # Core modules
        self.data = DataManager(self.config)
        self.strategy_mgr = StrategyManager()
        self.risk = RiskGate(self.config)
        self.execution = ExecutionEngine(self.config)
        self.alerts = AlertManager(self.config)
        self.audit = AuditJournal()

        # HL-native shadow ledger — virtual trades from Python strategy signals.
        # Does NOT affect real bot state. Used to measure Python-on-HL expected
        # P&L forward-walking vs the real TV-webhook-driven bot P&L before
        # flipping the HL-native execution switch.
        try:
            import os
            import importlib.util
            _sl_path = os.path.join(os.path.dirname(__file__), "..", "shared", "shadow_ledger.py")
            _spec = importlib.util.spec_from_file_location("shadow_ledger_mod", _sl_path)
            _mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            self.shadow_ledger = _mod.ShadowLedger(
                path=os.path.join(os.path.dirname(__file__), "data", "hl_native_shadow_trades.json"),
                starting_balance=10_000.0,
                size_usd=200.0,
            )
            log.info(f"HL-native shadow ledger initialized (path={self.shadow_ledger.path})")
        except Exception as e:
            log.warning(f"Shadow ledger failed to init — continuing without it: {e}")
            self.shadow_ledger = None

        # Register strategies (deployed params loaded automatically)
        self.strategy_mgr.register(MomentumV15Strategy())

        # Scalp shadow runner — forward-walks the 2026-04-14 elected scalp
        # configs (ETH 15m pullback_pair, HYPE 15m pullback_pair) against
        # real HL candles. Evaluates + logs + records to a virtual ledger.
        # Does NOT touch production state.
        try:
            from core.scalp_shadow import ScalpShadowRunner
            from strategies.pullback_pair import PullbackPairStrategy
            self.scalp_shadow = ScalpShadowRunner(
                data_client=self.data,
                features_engine=self.strategy_mgr.features,
                ledger_path=os.path.join(os.path.dirname(__file__), "data", "scalp_shadow_trades.json"),
                size_usd=200.0,
                starting_balance=10_000.0,
            )
            self.scalp_shadow.register("ETH", "15m", PullbackPairStrategy())
            self.scalp_shadow.register("HYPE", "15m", PullbackPairStrategy())
            log.info("Scalp shadow runner initialized with 2 configs")
        except Exception as e:
            log.warning(f"Scalp shadow runner failed to init — continuing without it: {e}")
            self.scalp_shadow = None

        # Thread lock — serializes signal processing between 5-min cycle and webhook
        self._signal_lock = threading.Lock()

        # Track last evaluated candle timestamp per symbol
        self._last_candle_time: dict[str, str] = {}

        # Restore position ownership from persisted paper state
        positions = self.execution.paper.positions if self.execution.is_paper else {}
        for pos_id, pos in positions.items():
            symbol = pos_id.split('#')[0]
            strategy_name = pos.get("strategy")
            if strategy_name:
                self.strategy_mgr.record_entry(symbol, strategy_name)

    def start(self):
        """Start the trading bot."""
        self.running = True
        self._last_daily_report = None
        self._last_periodic_report = None  # hour of last 3h summary
        self._last_weekly_report = None    # week number of last weekly report
        self._weekly_start_balance = None
        self._last_risk_alert: dict[str, str] = {}  # "symbol:reason" -> timestamp key, dedup alerts
        sig.signal(sig.SIGINT, self._shutdown)

        mode = "PAPER" if self.config.paper_trading else "LIVE"
        network = "TESTNET" if self.config.testnet else "MAINNET"

        log.info("=" * 60)
        log.info(f"  HyperLiquid Commodities Bot")
        log.info(f"  Mode: {mode} | Network: {network}")
        log.info(f"  Instruments: {list(self.config.instruments.keys())}")
        log.info(f"  Strategies: {[s.name for s in self.strategy_mgr.strategies]}")
        log.info(f"  Loop interval: {self.config.loop_interval_seconds}s")
        log.info("=" * 60)
        log.info("")
        log.info("  To connect Claude to Discord, start Claude Code with:")
        log.info("  claude --channels plugin:discord@claude-plugins-official")
        log.info("")

        # Startup reconciliation — pull HL positions and cross-check before main loop.
        # Only meaningful in live mode; paper mode is a no-op.
        if not self.config.paper_trading:
            log.info("[LIVE] Reconciling with HyperLiquid before main loop...")
            recon = self.execution.reconcile_with_hl()
            mismatches = recon.get("mismatches", [])
            orphans_local = recon.get("tracked_only_locally", [])
            orphans_hl = recon.get("tracked_only_on_hl", [])
            if mismatches or orphans_hl:
                msg = (
                    f"Reconcile flagged: {len(mismatches)} side/size mismatches, "
                    f"{len(orphans_hl)} HL positions not tracked locally, "
                    f"{len(orphans_local)} stale local trail states. "
                    f"Review {self.config.account_address} on HL before continuing."
                )
                log.error(msg)
                self.alerts.send_bot_status("warning", msg)

        self.alerts.send_bot_status("online", f"Mode: {mode} | Network: {network}")

        # Start Discord command bot
        self.commander = BotCommander(
            token=os.getenv("DISCORD_BOT_TOKEN", ""),
            bot_ref=self,
        )
        self.commander.start()

        # Start TradingView webhook server — TV handles ENTRIES, bot handles EXITS
        from core.webhook import WebhookServer
        self.webhook = WebhookServer(
            bot_ref=self,
            port=self.config.tv_webhook_port,
            secret=self.config.tv_webhook_secret,
        )
        self.webhook.start()

        # Main loop — synced to 5-minute candle boundaries
        cycle = 0
        while self.running:
            try:
                cycle += 1
                log.info(f"--- Cycle {cycle} | {datetime.now().strftime('%H:%M:%S')} ---")
                self._run_cycle()
                self._check_daily_report()
                self._check_periodic_report()
                self._check_weekly_report()
            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error(f"Cycle error: {e}", exc_info=True)
                self.alerts.send_bot_status("error", str(e))

            if self.running:
                self._wait_for_next_candle()

        self._on_shutdown()

    def _wait_for_next_candle(self):
        """Wait until just after the next 5-minute candle close, checking SL/TP every ~10s."""
        CANDLE_SECONDS = 300  # 5-minute candles
        BUFFER = 5            # seconds after candle close for exchange to finalize
        SL_CHECK_INTERVAL = 0.5

        while self.running:
            now = datetime.now()
            seconds_past = (now.minute % 5) * 60 + now.second
            seconds_to_next = CANDLE_SECONDS - seconds_past + BUFFER
            if seconds_to_next >= CANDLE_SECONDS:
                seconds_to_next -= CANDLE_SECONDS

            if seconds_to_next <= SL_CHECK_INTERVAL:
                # Almost time — sleep the remaining seconds and return for full cycle
                time.sleep(max(1, seconds_to_next))
                return

            # Quick SL/TP + trailing stop check while waiting — use LIVE prices
            try:
                if hasattr(self.execution, 'paper'):
                    self.execution.paper.sync_from_disk()
                # Only fetch live prices for symbols with open positions
                open_symbols = list(self.execution.get_open_symbols())
                if not open_symbols:
                    next_candle_time = now + timedelta(seconds=seconds_to_next)
                    log.debug(f"No positions — sleeping until {next_candle_time.strftime('%H:%M:%S')}")
                    time.sleep(SL_CHECK_INTERVAL)
                    continue
                current_prices = {}
                for sym in open_symbols:
                    p = self.data.fetch_quick_price(sym)
                    if p:
                        current_prices[sym] = p
                if current_prices:
                    prices_str = ", ".join(f"{s}: ${p:.2f}" for s, p in current_prices.items())
                    log.info(f"Live: {prices_str}")
                    # Write to shared file for dashboard
                    try:
                        import json as _json
                        _pf = os.path.join(_BASE_DIR, "data", "live_prices.json")
                        with open(_pf, "w") as _f:
                            _json.dump(current_prices, _f)
                    except Exception:
                        pass
                sl_tp_trades = self.execution.check_sl_tp(current_prices)
                for trade in sl_tp_trades:
                    self.risk.record_trade(trade.symbol, trade.pnl or 0.0)
                    self.strategy_mgr.clear_ownership(trade.symbol)
                    self.audit.log_sl_tp_trigger(trade)
                    self.alerts.send_sl_tp_alert(trade)
                    G, R, Y, C, RST = "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[0m"
                    pnl_color = G if (trade.pnl or 0) >= 0 else R
                    log.info(f"SL/TP: {Y}{trade.symbol}{RST} {trade.side} @ {C}${trade.price:.2f}{RST} | PnL: {pnl_color}${trade.pnl:+.2f}{RST}")
                if sl_tp_trades:
                    account_state = self.execution.get_account_state()
                    self.risk.update_portfolio(account_state)
            except Exception as e:
                log.debug(f"SL/TP check error: {e}")

            # Re-check time after SL/TP work — the fetch may have taken several seconds
            now2 = datetime.now()
            seconds_past2 = (now2.minute % 5) * 60 + now2.second
            seconds_to_next2 = CANDLE_SECONDS - seconds_past2 + BUFFER
            if seconds_to_next2 >= CANDLE_SECONDS:
                seconds_to_next2 -= CANDLE_SECONDS
            if seconds_to_next2 <= SL_CHECK_INTERVAL:
                time.sleep(max(1, seconds_to_next2))
                return

            next_candle_time = now2 + timedelta(seconds=seconds_to_next2)
            log.debug(f"Next candle at {next_candle_time.strftime('%H:%M:%S')} ({seconds_to_next2:.0f}s) — checking SL/TP")
            time.sleep(SL_CHECK_INTERVAL)

    def _run_cycle(self):
        """Single iteration of the trading loop."""

        # 0. Sync state from disk (pick up dashboard manual closes)
        if hasattr(self.execution, 'paper'):
            self.execution.paper.sync_from_disk()

        # 1. Fetch data
        log.debug("Fetching candles for all instruments")
        candles = self.data.fetch_all_candles()
        self.data.fetch_mid_prices()

        # 2. Update account state
        log.debug("Fetching account state")
        account_state = self.execution.get_account_state()
        self.risk.update_portfolio(account_state)

        # 3. Check kill switch
        if self.risk.kill_switch:
            log.warning("Kill switch active — skipping signal evaluation")
            return

        # 4. Check paper SL/TP before strategy evaluation
        current_prices = dict(self.data.mid_prices)

        # Shadow ledger — update virtual positions (trails + SL hits)
        if self.shadow_ledger:
            try:
                self.shadow_ledger.update_prices(current_prices)
            except Exception as e:
                log.warning(f"Shadow ledger update_prices failed: {e}")

        # Scalp shadow — update virtual positions + tick eval (new bar check)
        if self.scalp_shadow:
            self.scalp_shadow.update_prices(current_prices)
            self.scalp_shadow.tick()
        candle_highs = {}
        candle_lows = {}
        for symbol, df in candles.items():
            if not df.empty and len(df) > 1:
                last_closed = df.iloc[-2]
                candle_highs[symbol] = float(last_closed["high"])
                candle_lows[symbol] = float(last_closed["low"])
        sl_tp_trades = self.execution.check_sl_tp(current_prices, candle_highs, candle_lows)
        for trade in sl_tp_trades:
            self.risk.record_trade(trade.symbol, trade.pnl or 0.0)
            self.strategy_mgr.clear_ownership(trade.symbol)
            self.audit.log_sl_tp_trigger(trade)
            self.alerts.send_sl_tp_alert(trade)
            G, R, Y, C, RST = "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[0m"
            pnl_color = G if (trade.pnl or 0) >= 0 else R
            log.info(f"SL/TP: {Y}{trade.symbol}{RST} {trade.side} @ {C}${trade.price:.2f}{RST} | PnL: {pnl_color}${trade.pnl:+.2f}{RST}")

        if sl_tp_trades:
            account_state = self.execution.get_account_state()
            self.risk.update_portfolio(account_state)

        # 5b. DD protection: close all positions for symbols where DD triggered
        #     Matches TV's strategy.close_all(comment="DD protection")
        dd_signals = self.risk.get_dd_close_signals()
        for signal in dd_signals:
            trade = self.execution.execute(signal)
            if trade:
                self.risk.record_trade(trade.symbol, trade.pnl or 0.0)
                self.strategy_mgr.clear_ownership(trade.symbol)
                self.audit.log_trade(trade)
                self.alerts.send_trade_alert(trade)
                log.warning(f"DD PROTECTION close: {trade.side} {trade.symbol} @ {trade.price} | PnL: ${trade.pnl:.2f}")
        if dd_signals:
            account_state = self.execution.get_account_state()
            self.risk.update_portfolio(account_state)

        # 6. Only evaluate strategies when a new candle has closed
        new_candles = {}
        for symbol, df in candles.items():
            last_ts = str(df.iloc[-1]["timestamp"])
            if last_ts != self._last_candle_time.get(symbol):
                self._last_candle_time[symbol] = last_ts
                new_candles[symbol] = df

        if not new_candles:
            log.info("No new candle closes — skipping strategy evaluation")
            return

        # TV handles entries, bot handles exits (signal reversal + SL/TP/trail)
        # Evaluate all symbols for EXIT signals only
        positions = self.risk.portfolio.positions
        signals = self.strategy_mgr.evaluate_all(new_candles, positions)

        for signal in signals:
            # Only process EXIT signals — TV handles entries
            if signal.signal_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
                with self._signal_lock:
                    current_price = float(self.data.mid_prices.get(signal.symbol, 0))
                    if current_price:
                        log.info(f"Local exit: {signal.signal_type.value} {signal.symbol} — {signal.reason}")
                        self.process_signal(signal, current_price)
            else:
                # HL-NATIVE SHADOW: log what the local strategy would have fired
                # as an entry, without actually executing. Used to compare
                # Python strategy signals against TV webhook entries bar-by-bar
                # before the full HL-native migration. See research note on
                # TV webhook reliability / full HL migration (2026-04-14).
                current_price = float(self.data.mid_prices.get(signal.symbol, 0))

                # Mirror the webhook regime gate on symbols where it's enforced
                # on the live path. Otherwise the shadow side is running with
                # looser filtering than the webhook side and the comparison is
                # unfair (see 2026-04-15 briefing).
                from core import regime_gate
                gate_blocked = False
                if regime_gate.is_enforcing(signal.symbol):
                    df_for_gate = new_candles.get(signal.symbol)
                    gate = regime_gate.evaluate(df_for_gate)
                    if gate is not None and not gate.pass_:
                        log.info(
                            f"[HL_NATIVE_SHADOW_BLOCK] {signal.symbol} {signal.signal_type.value} "
                            f"@ {current_price:.4f} gate={gate.stats_str} {gate.reason_str}"
                        )
                        gate_blocked = True

                if not gate_blocked:
                    log.info(
                        f"[HL_NATIVE_SHADOW] {signal.symbol} {signal.signal_type.value} "
                        f"@ {current_price:.4f} strategy={signal.strategy_name} "
                        f"conf={signal.confidence:.2f} reason=\"{signal.reason}\""
                    )
                    # Also open a virtual position in the shadow ledger so we can
                    # track forward-walking expected P&L from Python's signals.
                    if self.shadow_ledger and current_price > 0:
                        try:
                            side = "long" if signal.signal_type == SignalType.LONG else "short"
                            trail_offset = getattr(signal, "_trail_offset_value", None)
                            self.shadow_ledger.open_virtual(
                                symbol=signal.symbol,
                                side=side,
                                entry_price=current_price,
                                stop_loss=signal.stop_loss,
                                trail_offset=trail_offset,
                                strategy_name=signal.strategy_name,
                                reason=signal.reason or "",
                            )
                        except Exception as e:
                            log.warning(f"Shadow ledger open_virtual failed for {signal.symbol}: {e}")

    def process_signal(self, signal_obj, current_price: float, close_one: bool = False) -> dict:
        """Process a signal through risk gate → execution → bookkeeping.

        Shared by both the 5-min cycle and the webhook handler.
        Caller must hold self._signal_lock.
        """
        # Risk check
        passed, reason = self.risk.check(signal_obj)
        if not passed:
            self.audit.log_signal(signal_obj, risk_passed=False, risk_reason=reason)
            log.warning(f"REJECTED {signal_obj.symbol} {signal_obj.signal_type.value}: {reason}")
            return {"status": "rejected", "reason": reason}

        # Execute — close_one=True for TV webhook exits (FIFO per pyramid layer)
        trade = self.execution.execute(signal_obj, current_price, account_balance=self.risk.portfolio.account_balance, close_one=close_one)
        if not trade:
            return {"status": "error", "reason": "execution failed"}

        self.audit.log_signal(signal_obj, risk_passed=True, risk_reason="passed", trade=trade)
        self.alerts.send_trade_alert(trade)
        # Colored trade log
        G, R, Y, C, RST = "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[0m"
        side_color = G if "long" in trade.side and "close" not in trade.side else R
        pnl_str = ""
        if trade.pnl is not None:
            pnl_color = G if trade.pnl >= 0 else R
            pnl_str = f" | PnL: {pnl_color}${trade.pnl:+.2f}{RST}"
        log.info(f"Trade: {side_color}{trade.side.upper()}{RST} {Y}{trade.symbol}{RST} @ {C}${trade.price:.2f}{RST}{pnl_str}")

        # Track position ownership for entry trades
        is_entry = signal_obj.signal_type in (SignalType.LONG, SignalType.SHORT)
        if is_entry:
            self.strategy_mgr.record_entry(signal_obj.symbol, signal_obj.strategy_name)
            _sym = signal_obj.symbol
            if _sym not in self.risk.portfolio.positions:
                _pos_id = _sym
            else:
                _n = 2
                while f"{_sym}#{_n}" in self.risk.portfolio.positions:
                    _n += 1
                _pos_id = f"{_sym}#{_n}"
            self.risk.portfolio.positions[_pos_id] = {
                "symbol": signal_obj.symbol,
                "side": "long" if signal_obj.signal_type == SignalType.LONG else "short",
                "size_usd": trade.size * trade.price,
                "entry_price": trade.price,
            }

        # Clear ownership and record PnL for close trades
        is_close = signal_obj.signal_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT)
        if is_close:
            self.risk.record_trade(signal_obj.symbol, trade.pnl or 0.0)
            _to_remove = [pid for pid in self.risk.portfolio.positions
                          if pid.split('#')[0] == signal_obj.symbol]
            if close_one and _to_remove:
                # FIFO: only remove one position from risk tracking
                self.risk.portfolio.positions.pop(_to_remove[0], None)
            else:
                # Close all: remove everything for this symbol
                for pid in _to_remove:
                    self.risk.portfolio.positions.pop(pid, None)
                self.strategy_mgr.clear_ownership(signal_obj.symbol)

        # Check consecutive loss alert
        if trade.pnl is not None and trade.pnl < 0:
            if self.risk.consecutive_losses >= self.risk.rules.max_consecutive_losses - 1:
                self.alerts.send_consecutive_loss_alert(
                    self.risk.consecutive_losses,
                    self.risk.rules.max_consecutive_losses,
                )

        return {
            "status": "executed",
            "side": trade.side,
            "symbol": trade.symbol,
            "price": trade.price,
            "pnl": trade.pnl,
        }

    def _in_trading_hours(self) -> bool:
        """Check if current time is within configured trading hours."""
        now = datetime.now()
        if self.config.paper_trading:
            start = now.replace(hour=self.config.risk.paper_trading_start_hour,
                                minute=self.config.risk.paper_trading_start_minute, second=0)
            end = now.replace(hour=self.config.risk.paper_trading_end_hour,
                              minute=self.config.risk.paper_trading_end_minute, second=0)
        else:
            start = now.replace(hour=self.config.risk.trading_start_hour,
                                minute=self.config.risk.trading_start_minute, second=0)
            end = now.replace(hour=self.config.risk.trading_end_hour,
                              minute=self.config.risk.trading_end_minute, second=0)
        return start <= now <= end

    def _is_trading_day(self) -> bool:
        """Check if today is a trading day (Mon-Fri)."""
        return datetime.now().weekday() < 5  # 0=Mon, 4=Fri

    def _check_daily_report(self):
        """Send daily report at 22:00 (end of trading window)."""
        if not self._in_trading_hours() or not self._is_trading_day():
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if now.hour == 22 and self._last_daily_report != today:
            self._last_daily_report = today
            try:
                account_state = self.execution.get_account_state()
                balance = float(account_state.get("marginSummary", {}).get("accountValue", 0))
                trades = self.execution.get_trade_history()
                today_trades = [t for t in trades if t.timestamp.strftime("%Y-%m-%d") == today]
                daily_pnl = sum(t.pnl for t in today_trades if t.pnl) if today_trades else 0
                positions = self.risk.portfolio.positions
                start_bal = self.risk.portfolio.starting_balance or balance
                self.alerts.send_daily_report(balance, daily_pnl, today_trades, positions, start_bal)
                log.info(f"Daily report sent: balance=${balance:.2f}, P&L=${daily_pnl:.2f}")
            except Exception as e:
                log.error(f"Failed to send daily report: {e}")

    def _check_periodic_report(self):
        """Send hourly summary (at :00 of every hour), only during trading days/hours."""
        if not self._in_trading_hours() or not self._is_trading_day():
            return
        now = datetime.now()
        report_key = f"{now.strftime('%Y-%m-%d')}-{now.hour}"
        if self._last_periodic_report == report_key:
            return
        self._last_periodic_report = report_key
        try:
            account_state = self.execution.get_account_state()
            balance = float(account_state.get("marginSummary", {}).get("accountValue", 0))
            all_trades = self.execution.get_trade_history()
            total_pnl = sum(t.pnl for t in all_trades if t.pnl) if all_trades else 0
            today = now.strftime("%Y-%m-%d")
            today_trades = [t for t in all_trades if t.timestamp.strftime("%Y-%m-%d") == today]
            positions = self.risk.portfolio.positions
            start_bal = self.risk.portfolio.starting_balance or balance
            # Daily P&L = realized from today's closed trades + unrealized from open positions
            daily_pnl = sum(t.pnl for t in today_trades if t.pnl) if today_trades else 0
            for pos in positions.values():
                daily_pnl += pos.get("unrealized_pnl", 0)
            self.alerts.send_periodic_summary(balance, total_pnl, today_trades, positions, start_bal, daily_pnl)
            log.info(f"Hourly summary sent: balance=${balance:.2f}, total P&L=${total_pnl:.2f}, daily P&L=${daily_pnl:.2f}")
        except Exception as e:
            log.error(f"Failed to send periodic report: {e}")

    def _check_weekly_report(self):
        """Send weekly report on Sunday at 22:00."""
        if not self._in_trading_hours():
            return
        now = datetime.now()
        if now.weekday() != 6 or now.hour != 22:  # Sunday = 6
            return
        week_key = now.strftime("%Y-W%W")
        if self._last_weekly_report == week_key:
            return
        self._last_weekly_report = week_key
        try:
            account_state = self.execution.get_account_state()
            balance = float(account_state.get("marginSummary", {}).get("accountValue", 0))
            trades = self.execution.get_trade_history()
            # Filter to current week's trades only
            week_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            week_start = week_start - timedelta(days=week_start.weekday())
            weekly_pnl = sum(t.pnl for t in trades if t.pnl and t.timestamp >= week_start) if trades else 0
            positions = self.risk.portfolio.positions
            start_bal = self._weekly_start_balance or balance
            self.alerts.send_weekly_report(balance, weekly_pnl, trades, positions, start_bal)
            self._weekly_start_balance = balance  # reset for next week
            log.info(f"Weekly report sent: balance=${balance:.2f}, weekly P&L=${weekly_pnl:.2f}")
        except Exception as e:
            log.error(f"Failed to send weekly report: {e}")

    def _shutdown(self, signum=None, frame=None):
        """Handle graceful shutdown."""
        log.info("Shutdown signal received...")
        self.running = False

    def _on_shutdown(self):
        """Clean up on exit. Safe to call multiple times."""
        if getattr(self, '_shutdown_done', False):
            return
        self._shutdown_done = True
        log.info("Shutting down — saving state...")

        # Save risk state so it persists across restarts
        try:
            self.risk._save_risk_state()
            log.info("Risk state saved")
        except Exception as e:
            log.error(f"Failed to save risk state on shutdown: {e}")

        # Paper state is already saved after every trade, but force a final save
        try:
            if self.execution.is_paper:
                self.execution.paper._save_state()
                log.info("Paper state saved")
        except Exception as e:
            log.error(f"Failed to save paper state on shutdown: {e}")

        if hasattr(self, 'commander'):
            self.commander.stop()
        self.alerts.send_bot_status("offline", "Bot stopped")
        self.alerts.flush()  # wait for offline alert to actually send

        # Print session summary
        trades = self.execution.get_trade_history()
        if trades:
            total_pnl = sum(t.pnl for t in trades if t.pnl) or 0
            log.info(f"Session summary: {len(trades)} trades, P&L: ${total_pnl:.2f}")

        log.info("Goodbye.")


LOCKFILE = os.path.join(_BASE_DIR, "data", "bot.lock")
_lock_fd = None


def _acquire_lock():
    """Prevent multiple bot instances using PID check + flock.

    Never delete the lock file — that causes a new inode and breaks flock.
    Instead, check if the PID inside is still alive.
    """
    global _lock_fd

    # Check if another instance is alive via PID
    if os.path.exists(LOCKFILE):
        try:
            with open(LOCKFILE, "r") as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)  # signal 0 = check if alive
            log.error(f"Bot already running (PID {old_pid}). Kill it first: kill {old_pid}")
            sys.exit(1)
        except (ValueError, ProcessLookupError, PermissionError):
            # PID is stale or unreadable — safe to proceed
            log.info("Stale lock file found — previous instance died. Taking over.")

    # Write our PID and hold flock as second layer of protection
    _lock_fd = open(LOCKFILE, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("Bot already running (lock held). Kill the other instance first.")
        sys.exit(1)
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()


def _release_lock():
    """Release file lock. Do NOT delete the file — keeps the inode stable for flock."""
    global _lock_fd
    if _lock_fd:
        try:
            # Clear PID so stale check works
            _lock_fd.seek(0)
            _lock_fd.truncate()
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            _lock_fd.close()
        except OSError:
            pass
        _lock_fd = None


def main():
    _acquire_lock()

    # Register SIGTERM to trigger graceful shutdown (same as Ctrl+C)
    def _signal_handler(signum, frame):
        raise KeyboardInterrupt

    sig.signal(sig.SIGTERM, _signal_handler)

    max_restarts = 10
    restart_count = 0

    bot = None
    try:
        while restart_count < max_restarts:
            try:
                bot = TradingBot()
                bot.start()
                break  # Clean shutdown via Ctrl+C
            except KeyboardInterrupt:
                log.info("Stopped by user")
                if bot:
                    bot._on_shutdown()
                break
            except Exception as e:
                restart_count += 1
                wait = min(30, 5 * restart_count)
                log.error(f"Bot crashed: {e} — restarting in {wait}s ({restart_count}/{max_restarts})")
                # Save state before restart so nothing is lost
                try:
                    if bot:
                        bot.risk._save_risk_state()
                        if bot.execution.is_paper:
                            bot.execution.paper._save_state()
                        log.info("State saved before restart")
                except Exception as save_err:
                    log.error(f"Failed to save state on crash: {save_err}")
                # Disconnect old Discord bot before creating a new one
                try:
                    if bot and hasattr(bot, 'commander'):
                        bot.commander.stop()
                except Exception:
                    pass
                time.sleep(wait)

        if restart_count >= max_restarts:
            log.error("Max restarts reached — giving up")
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
