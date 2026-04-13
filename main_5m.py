"""
HyperLiquid Commodities Trading Bot — 5m Scalp Mode
=====================================================

Parallel paper bot running 5m candles with Option E params:
- EMA 5/13 (injected into features as ema_9/ema_21)
- SL 0.5x ATR, TP 1.0x ATR across all strategies
- Donchian window 12, Range Flip KC window 12, MR BB 15

Runs alongside the main 15m bot with separate paper state.
"""
import os
import sys
import time
import signal as sig
import fcntl
from datetime import datetime

from config.settings import load_config, INSTRUMENTS
from core.data import DataManager
from core.strategy_manager import StrategyManager
from core.risk import RiskGate
from core.execution import ExecutionEngine
from core.alerts import AlertManager
from strategies.trend_follow import TrendFollowStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.momentum import MomentumStrategy
from strategies.squeeze_breakout import SqueezeBreakoutStrategy
from strategies.donchian_trend import DonchianTrendStrategy
from strategies.range_flip import RangeFlipStrategy
from strategies.base import SignalType
from utils.logger import setup_logger

log = setup_logger("main_5m")

# Set terminal tab title
print("\033]0;main_5m.py\007", end="", flush=True)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(_BASE_DIR, "logs"), exist_ok=True)
os.makedirs(os.path.join(_BASE_DIR, "data"), exist_ok=True)

# Override paper state file for 5m bot
import core.execution as _exec_mod
_exec_mod.PAPER_STATE_FILE = os.path.join(_BASE_DIR, "data", "paper_state_5m.json")


def _apply_5m_overrides(strategy):
    """Apply Option E params: SL 0.5x ATR, TP 1.0x ATR, faster windows."""
    # Universal SL/TP override
    if hasattr(strategy, "atr_stop_multiplier"):
        strategy.atr_stop_multiplier = 0.5
    if hasattr(strategy, "atr_tp_multiplier"):
        strategy.atr_tp_multiplier = 1.0

    # Strategy-specific overrides
    if strategy.name == "donchian_trend":
        strategy.dc_window = 12
    elif strategy.name == "range_flip":
        strategy.kc_ema_window = 12
    elif strategy.name == "squeeze_breakout":
        strategy.bb_window = 15
        strategy.kc_window = 15


class TradingBot5m:
    def __init__(self):
        self.config = load_config()
        # Override to 5m candles and faster loop
        self.config.candle_interval = "5m"
        self.config.loop_interval_seconds = 15  # Check every 15s for 5m candles
        self.running = False

        # Core modules
        self.data = DataManager(self.config)
        self.strategy_mgr = StrategyManager()
        self.risk = RiskGate(self.config)
        self.execution = ExecutionEngine(self.config)
        # No alerts for 5m bot — avoid spamming Discord
        self.alerts = AlertManager(self.config)

        # Register strategies with 5m overrides
        strategies = [
            TrendFollowStrategy(),
            MeanReversionStrategy(),
            MomentumStrategy(),
            SqueezeBreakoutStrategy(),
            DonchianTrendStrategy(),
            RangeFlipStrategy(),
        ]
        for s in strategies:
            _apply_5m_overrides(s)
            self.strategy_mgr.register(s)

        # Track last evaluated candle timestamp per symbol
        self._last_candle_time: dict[str, str] = {}

        # Restore position ownership from persisted paper state
        positions = self.execution.paper.positions if self.execution.is_paper else {}
        for symbol, pos in positions.items():
            strategy_name = pos.get("strategy")
            if strategy_name:
                self.strategy_mgr.record_entry(symbol, strategy_name)

    def start(self):
        self.running = True
        sig.signal(sig.SIGINT, self._shutdown)

        mode = "PAPER" if self.config.paper_trading else "LIVE"

        log.info("=" * 60)
        log.info("  HyperLiquid Commodities Bot — 5m SCALP MODE")
        log.info(f"  Mode: {mode} | Candles: 5m")
        log.info(f"  Params: SL=0.5x ATR, TP=1.0x ATR, EMA 5/13")
        log.info(f"  Instruments: {list(self.config.instruments.keys())}")
        log.info(f"  Loop interval: {self.config.loop_interval_seconds}s")
        log.info("=" * 60)

        cycle = 0
        while self.running:
            try:
                cycle += 1
                log.info(f"--- 5m Cycle {cycle} | {datetime.now().strftime('%H:%M:%S')} ---")
                self._run_cycle()
            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error(f"Cycle error: {e}", exc_info=True)

            if self.running:
                time.sleep(self.config.loop_interval_seconds)

        self._on_shutdown()

    def _run_cycle(self):
        # 1. Fetch data
        candles = self.data.fetch_all_candles()
        self.data.fetch_mid_prices()

        # 2. Update account state
        account_state = self.execution.get_account_state()
        self.risk.update_portfolio(account_state)

        if self.risk.kill_switch:
            log.warning("Kill switch active — skipping")
            return

        # 3. Check SL/TP
        current_prices = dict(self.data.mid_prices)
        sl_tp_trades = self.execution.check_sl_tp(current_prices)
        for trade in sl_tp_trades:
            self.risk.record_trade(trade.symbol, trade.pnl or 0.0)
            self.strategy_mgr.clear_ownership(trade.symbol)
            log.info(f"SL/TP: {trade.side} {trade.symbol} @ {trade.price} | PnL: ${trade.pnl:.2f}")

        if sl_tp_trades:
            account_state = self.execution.get_account_state()
            self.risk.update_portfolio(account_state)

        # 4. Only evaluate on new candle close
        new_candles = {}
        for symbol, df in candles.items():
            last_ts = str(df.iloc[-1]["timestamp"])
            if last_ts != self._last_candle_time.get(symbol):
                self._last_candle_time[symbol] = last_ts
                new_candles[symbol] = df

        if not new_candles:
            log.info("No new 5m candle closes — skipping")
            return

        # 5. Inject faster EMAs into features
        # The strategy_manager computes features, but we need to override
        # ema_9/ema_21 with ema_5/ema_13 AFTER compute. We monkey-patch
        # the feature engine's get_latest_features to inject them.
        original_get_latest = self.strategy_mgr.features.get_latest_features

        def get_latest_with_fast_emas(df):
            result = original_get_latest(df)
            if result and not df.empty:
                # Inject EMA 5/13 as ema_9/ema_21 so strategies use faster EMAs
                result["ema_9"] = float(df["close"].ewm(span=5, adjust=False).mean().iloc[-1])
                result["ema_21"] = float(df["close"].ewm(span=13, adjust=False).mean().iloc[-1])
            return result

        self.strategy_mgr.features.get_latest_features = get_latest_with_fast_emas

        # 6. Run strategies
        positions = self.risk.portfolio.positions
        signals = self.strategy_mgr.evaluate_all(new_candles, positions)
        self.risk.correlations = self.strategy_mgr.correlations

        # Restore original
        self.strategy_mgr.features.get_latest_features = original_get_latest

        if not signals:
            log.info("No signals")
            return

        # 7. Process signals
        for signal_obj in signals:
            passed, reason = self.risk.check(signal_obj)
            if not passed:
                continue

            price = self.data.get_current_price(signal_obj.symbol)
            if not price:
                continue

            trade = self.execution.execute(signal_obj, price,
                                           account_balance=self.risk.portfolio.account_balance)
            if trade:
                log.info(f"TRADE: {trade.side} {trade.symbol} @ {trade.price}"
                         + (f" | PnL: ${trade.pnl:.2f}" if trade.pnl else ""))

                is_entry = signal_obj.signal_type in (SignalType.LONG, SignalType.SHORT)
                if is_entry:
                    self.strategy_mgr.record_entry(signal_obj.symbol, signal_obj.strategy_name)

                is_close = signal_obj.signal_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT)
                if is_close:
                    self.risk.record_trade(signal_obj.symbol, trade.pnl or 0.0)
                    self.strategy_mgr.clear_ownership(signal_obj.symbol)

    def _shutdown(self, signum=None, frame=None):
        log.info("Shutdown signal received...")
        self.running = False

    def _on_shutdown(self):
        if getattr(self, '_shutdown_done', False):
            return
        self._shutdown_done = True
        log.info("5m bot stopped.")


LOCKFILE = os.path.join(_BASE_DIR, "data", "bot_5m.lock")
_lock_fd = None


def _acquire_lock():
    global _lock_fd
    if os.path.exists(LOCKFILE):
        try:
            with open(LOCKFILE, "r") as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)
            log.error(f"5m bot already running (PID {old_pid}). Kill it first: kill {old_pid}")
            sys.exit(1)
        except (ValueError, ProcessLookupError, PermissionError):
            log.info("Stale lock file — taking over.")

    _lock_fd = open(LOCKFILE, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("5m bot already running (lock held).")
        sys.exit(1)
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()


def _release_lock():
    global _lock_fd
    if _lock_fd:
        try:
            _lock_fd.seek(0)
            _lock_fd.truncate()
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            _lock_fd.close()
        except OSError:
            pass
        _lock_fd = None


def main():
    _acquire_lock()

    def _signal_handler(signum, frame):
        raise KeyboardInterrupt

    sig.signal(sig.SIGTERM, _signal_handler)

    max_restarts = 10
    restart_count = 0
    bot = None

    try:
        while restart_count < max_restarts:
            try:
                bot = TradingBot5m()
                bot.start()
                break
            except KeyboardInterrupt:
                log.info("Stopped by user")
                if bot:
                    bot._on_shutdown()
                break
            except Exception as e:
                restart_count += 1
                wait = min(30, 5 * restart_count)
                log.error(f"5m bot crashed: {e} — restarting in {wait}s ({restart_count}/{max_restarts})")
                time.sleep(wait)

        if restart_count >= max_restarts:
            log.error("Max restarts reached — giving up")
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
