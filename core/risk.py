"""
Risk Gate — Hard rules. Not an agent. Not AI. Pure deterministic checks.

Every signal must pass through here before reaching the Execution module.
If any check fails, the trade is rejected. No exceptions.
"""
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional

from config.settings import BotConfig, RiskConfig, INSTRUMENTS
from strategies.base import Signal, SignalType
from utils.logger import setup_logger

log = setup_logger("risk")

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RISK_STATE_FILE = os.path.join(_BASE_DIR, "data", "risk_state.json")


@dataclass
class PortfolioState:
    """Current portfolio state — updated by the execution module."""
    account_balance: float = 0.0
    positions: dict = field(default_factory=dict)  # symbol -> {"side": "long"/"short", "size_usd": float, "entry_price": float}
    daily_pnl: float = 0.0
    daily_trades: int = 0
    daily_date: date = field(default_factory=lambda: date.today())
    starting_balance: float = 0.0  # Balance at start of day


class RiskGate:
    def __init__(self, config: BotConfig):
        self.config = config
        self.rules = config.risk
        self.portfolio = PortfolioState()
        self.kill_switch = False
        self.consecutive_losses: int = 0
        self.consecutive_loss_halt: bool = False
        self.last_trade_time: dict[str, datetime] = {}  # symbol -> last trade timestamp
        self.correlations: dict[str, float] = {}       # "symA|symB" -> correlation
        # Account-level peak drawdown protection
        self._account_peak_balance: float = 0.0        # Highest account balance ever
        self.account_dd_halt: bool = False              # True when account DD limit breached
        # Per-symbol DD protection (matches TV equity peak tracking)
        self._symbol_cumulative_pnl: dict[str, float] = {}  # symbol -> cumulative PnL
        self._symbol_peak_pnl: dict[str, float] = {}        # symbol -> peak cumulative PnL
        self._symbol_dd_triggered_at: dict[str, datetime] = {}  # symbol -> when DD was triggered
        self._dd_close_all_pending: set[str] = set()  # symbols needing close-all from DD protection
        self._load_risk_state()

    def _save_risk_state(self):
        """Persist risk gate state to disk so it survives crashes/restarts."""
        try:
            state = {
                "daily_pnl": self.portfolio.daily_pnl,
                "daily_trades": self.portfolio.daily_trades,
                "daily_date": self.portfolio.daily_date.isoformat(),
                "starting_balance": self.portfolio.starting_balance,
                "kill_switch": self.kill_switch,
                "consecutive_losses": self.consecutive_losses,
                "consecutive_loss_halt": self.consecutive_loss_halt,
                "account_peak_balance": self._account_peak_balance,
                "account_dd_halt": self.account_dd_halt,
                "last_trade_time": {
                    sym: ts.isoformat() for sym, ts in self.last_trade_time.items()
                },
                "symbol_cumulative_pnl": self._symbol_cumulative_pnl,
                "symbol_peak_pnl": self._symbol_peak_pnl,
                "symbol_dd_triggered_at": {
                    sym: ts.isoformat() for sym, ts in self._symbol_dd_triggered_at.items()
                },
                "saved_at": datetime.now().isoformat(),
            }
            tmp = RISK_STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, RISK_STATE_FILE)
        except Exception as e:
            log.error(f"Failed to save risk state: {e}")

    def _load_risk_state(self):
        """Load risk gate state from disk if it exists."""
        try:
            if not os.path.exists(RISK_STATE_FILE):
                return
            with open(RISK_STATE_FILE) as f:
                state = json.load(f)
            saved_date = state.get("daily_date", "")
            if saved_date == date.today().isoformat():
                self.portfolio.daily_pnl = state.get("daily_pnl", 0.0)
                self.portfolio.daily_trades = state.get("daily_trades", 0)
                self.portfolio.daily_date = date.fromisoformat(saved_date)
                self.portfolio.starting_balance = state.get("starting_balance", 0.0)
                self.kill_switch = state.get("kill_switch", False)
                log.info(
                    f"[RISK] Restored daily state: pnl=${self.portfolio.daily_pnl:.2f}, "
                    f"trades={self.portfolio.daily_trades}, kill_switch={self.kill_switch}"
                )
            else:
                log.info(f"[RISK] Saved state is from {saved_date}, today is {date.today()} — starting fresh daily counters")
            self.consecutive_losses = state.get("consecutive_losses", 0)
            self.consecutive_loss_halt = state.get("consecutive_loss_halt", False)
            self._account_peak_balance = state.get("account_peak_balance", 0.0)
            self.account_dd_halt = state.get("account_dd_halt", False)
            for sym, ts_str in state.get("last_trade_time", {}).items():
                self.last_trade_time[sym] = datetime.fromisoformat(ts_str)
            self._symbol_cumulative_pnl = state.get("symbol_cumulative_pnl", {})
            self._symbol_peak_pnl = state.get("symbol_peak_pnl", {})
            for sym, ts_str in state.get("symbol_dd_triggered_at", {}).items():
                self._symbol_dd_triggered_at[sym] = datetime.fromisoformat(ts_str)
            log.info(
                f"[RISK] Restored risk state: consecutive_losses={self.consecutive_losses}, "
                f"dd_tracking={list(self._symbol_cumulative_pnl.keys())}"
            )
        except Exception as e:
            log.warning(f"Could not load risk state: {e} — starting fresh")

    def check(self, signal: Signal) -> tuple[bool, str]:
        """
        Run all risk checks on a signal.
        Returns (passed: bool, reason: str).
        If passed is False, the trade must be rejected.
        """
        if self.kill_switch:
            if signal.signal_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
                return True, ""  # Always allow closing positions, even when halted
            return False, "KILL SWITCH ACTIVE — all trading halted"
        if self.account_dd_halt:
            # Re-check actual drawdown — auto-clear if balance recovered
            if self._account_peak_balance > 0:
                current_dd = (self._account_peak_balance - self.portfolio.account_balance) / self._account_peak_balance
                if current_dd < self.rules.max_account_drawdown_pct:
                    log.info(f"Account DD recovered to {current_dd:.2%} (below {self.rules.max_account_drawdown_pct:.0%}) — auto-clearing halt")
                    self.account_dd_halt = False
                    self._save_risk_state()
            if self.account_dd_halt and signal.signal_type not in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
                return False, (
                    f"ACCOUNT DRAWDOWN HALT — balance ${self.portfolio.account_balance:,.2f} "
                    f"is {self.rules.max_account_drawdown_pct:.0%} below peak ${self._account_peak_balance:,.2f}. "
                    f"Call reset_account_dd_halt() to resume."
                )

        self._reset_daily_counters()

        checks = [
            self._check_kill_switch,
            self._check_account_drawdown,
            self._check_consecutive_loss_halt,
            self._check_trading_hours,
            # Daily trade limit removed — match TradingView (unlimited)
            self._check_daily_loss_limit,
            self._check_confidence,
            self._check_cooldown,
            self._check_position_limit,
            self._check_single_position_size,
            self._check_group_exposure,
            self._check_duplicate_position,
            self._check_pyramiding_limit,
            self._check_symbol_drawdown,
            self._check_correlation_exposure,
            self._check_leverage,
        ]

        for check in checks:
            passed, reason = check(signal)
            if not passed:
                log.warning(f"REJECTED {signal.symbol} {signal.signal_type.value}: {reason}")
                return False, reason
            else:
                log.debug(f"  ✓ {getattr(check, '__name__', type(check).__name__)} passed for {signal.symbol}")

        log.info(f"APPROVED {signal.symbol} {signal.signal_type.value}")
        return True, "passed"

    def _reset_daily_counters(self):
        """Reset daily counters at the start of a new day."""
        today = date.today()
        if self.portfolio.daily_date != today:
            log.debug(
                f"Resetting daily counters: trades={self.portfolio.daily_trades}, "
                f"pnl=${self.portfolio.daily_pnl:.2f}, old_date={self.portfolio.daily_date}"
            )
            self.portfolio.daily_date = today
            self.portfolio.daily_trades = 0
            self.portfolio.daily_pnl = 0.0
            self.portfolio.starting_balance = self.portfolio.account_balance
            self.kill_switch = False
            log.info("Daily counters reset")

    def _check_trading_hours(self, signal: Signal) -> tuple[bool, str]:
        """Only allow new entries during configured hours. Closes always allowed."""
        if signal.signal_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
            return True, ""  # Always allow closing positions
        now = datetime.now()
        # Crypto trades 24/7 — no weekend block
        current_minutes = now.hour * 60 + now.minute
        if self.config.paper_trading:
            start_minutes = self.rules.paper_trading_start_hour * 60 + self.rules.paper_trading_start_minute
            end_minutes = self.rules.paper_trading_end_hour * 60 + self.rules.paper_trading_end_minute
        else:
            start_minutes = self.rules.trading_start_hour * 60 + self.rules.trading_start_minute
            end_minutes = self.rules.trading_end_hour * 60 + self.rules.trading_end_minute
        if not (start_minutes <= current_minutes < end_minutes):
            start_h, start_m = divmod(start_minutes, 60)
            end_h, end_m = divmod(end_minutes, 60)
            return False, (
                f"Outside trading hours: {now.strftime('%H:%M')} "
                f"(allowed {start_h:02d}:{start_m:02d}-{end_h:02d}:{end_m:02d})"
            )
        return True, ""

    def _check_kill_switch(self, signal: Signal) -> tuple[bool, str]:
        """Check if daily loss limit has been breached."""
        if self.portfolio.starting_balance <= 0:
            return True, ""
        daily_loss_pct = abs(self.portfolio.daily_pnl) / self.portfolio.starting_balance
        if self.portfolio.daily_pnl < 0 and daily_loss_pct >= self.rules.max_daily_loss_pct:
            self.kill_switch = True
            return False, f"Daily loss limit hit: {daily_loss_pct:.1%} (max {self.rules.max_daily_loss_pct:.1%})"
        return True, ""

    def _check_account_drawdown(self, signal: Signal) -> tuple[bool, str]:
        """Halt all trading if account drops X% from its highest-ever balance."""
        if signal.signal_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
            return True, ""
        if self._account_peak_balance <= 0:
            return True, ""
        dd_pct = (self._account_peak_balance - self.portfolio.account_balance) / self._account_peak_balance
        if dd_pct >= self.rules.max_account_drawdown_pct:
            self.account_dd_halt = True
            self._save_risk_state()
            return False, (
                f"ACCOUNT DRAWDOWN HALT — balance ${self.portfolio.account_balance:,.2f} "
                f"is {dd_pct:.1%} below peak ${self._account_peak_balance:,.2f} "
                f"(max {self.rules.max_account_drawdown_pct:.0%}). "
                f"Call reset_account_dd_halt() to resume."
            )
        return True, ""

    def _check_consecutive_loss_halt(self, signal: Signal) -> tuple[bool, str]:
        """Halt trading after N consecutive losses. Disabled for paper trading."""
        if signal.signal_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
            return True, ""  # Always allow closing positions
        if self.config.paper_trading:
            return True, ""  # Skip in paper trading — let TV strategy run freely
        if self.consecutive_loss_halt:
            return False, (
                f"CONSECUTIVE LOSS HALT — {self.consecutive_losses} consecutive losses. "
                f"Call reset_consecutive_losses() to resume."
            )
        return True, ""

    def _check_daily_loss_limit(self, signal: Signal) -> tuple[bool, str]:
        """Don't open new positions if daily P&L is deeply negative."""
        if signal.signal_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
            return True, ""
        if self.portfolio.starting_balance <= 0:
            return True, ""
        loss_pct = abs(self.portfolio.daily_pnl) / self.portfolio.starting_balance
        if self.portfolio.daily_pnl < 0 and loss_pct >= self.rules.max_daily_loss_pct * 0.8:
            return False, f"Approaching daily loss limit: {loss_pct:.1%}"
        return True, ""

    def _check_confidence(self, signal: Signal) -> tuple[bool, str]:
        """Reject entry signals below minimum confidence threshold."""
        if signal.signal_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
            return True, ""
        if signal.confidence < self.rules.min_signal_confidence:
            return False, (
                f"Signal confidence too low: {signal.confidence:.2f} "
                f"(min {self.rules.min_signal_confidence:.2f})"
            )
        return True, ""

    def _check_cooldown(self, signal: Signal) -> tuple[bool, str]:
        """Reject if last trade on this symbol was too recent. Always allow closes."""
        if signal.signal_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
            return True, ""
        last = self.last_trade_time.get(signal.symbol)
        if last:
            elapsed = (datetime.now() - last).total_seconds()
            if elapsed < self.rules.min_trade_cooldown_seconds:
                remaining = int(self.rules.min_trade_cooldown_seconds - elapsed)
                return False, (
                    f"Trade cooldown: {elapsed:.0f}s since last trade on {signal.symbol} "
                    f"(min {self.rules.min_trade_cooldown_seconds}s, {remaining}s remaining)"
                )
        return True, ""

    def _check_position_limit(self, signal: Signal) -> tuple[bool, str]:
        """Max number of open positions."""
        if signal.signal_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
            return True, ""
        if len(self.portfolio.positions) >= self.rules.max_open_positions:
            return False, f"Max open positions: {len(self.portfolio.positions)}/{self.rules.max_open_positions}"
        return True, ""

    def _check_single_position_size(self, signal: Signal) -> tuple[bool, str]:
        """No single position larger than X% of account."""
        if signal.signal_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
            return True, ""
        if self.portfolio.account_balance <= 0:
            return True, ""

        instrument = INSTRUMENTS.get(signal.symbol)
        # Dynamic sizing: % of account × confidence
        if signal.size_usd:
            size_usd = signal.size_usd
        elif instrument:
            size_usd = self.portfolio.account_balance * instrument.base_position_pct * signal.confidence
        else:
            size_usd = 1000 * signal.confidence
        position_pct = size_usd / self.portfolio.account_balance

        log.debug(
            f"Position size check: {signal.symbol} size_usd=${size_usd:.2f} "
            f"({position_pct:.1%} of ${self.portfolio.account_balance:.2f}, "
            f"max {self.rules.max_single_position_pct:.1%})"
        )

        if position_pct > self.rules.max_single_position_pct:
            return False, f"Position too large: {position_pct:.1%} of account (max {self.rules.max_single_position_pct:.1%})"
        return True, ""

    def _check_group_exposure(self, signal: Signal) -> tuple[bool, str]:
        """No more than X% of account in one group (metals/energy)."""
        if signal.signal_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
            return True, ""
        if self.portfolio.account_balance <= 0:
            return True, ""

        instrument = INSTRUMENTS.get(signal.symbol)
        if not instrument:
            return True, ""

        group = instrument.group
        group_exposure = sum(
            abs(pos["size_usd"])
            for pid, pos in self.portfolio.positions.items()
            if INSTRUMENTS.get(pos.get("symbol", self._base_symbol(pid)), instrument).group == group
        )

        # Dynamic sizing: % of account × confidence
        if signal.size_usd:
            size_usd = signal.size_usd
        else:
            size_usd = self.portfolio.account_balance * instrument.base_position_pct * signal.confidence
        new_group_exposure = group_exposure + size_usd
        group_pct = new_group_exposure / self.portfolio.account_balance

        log.debug(
            f"Group exposure check: {group} existing=${group_exposure:.2f} + new=${size_usd:.2f} "
            f"= {group_pct:.1%} (max {self.rules.max_group_exposure_pct:.1%})"
        )

        if group_pct > self.rules.max_group_exposure_pct:
            return False, f"{group} group exposure too high: {group_pct:.1%} (max {self.rules.max_group_exposure_pct:.1%})"
        return True, ""

    def _check_duplicate_position(self, signal: Signal) -> tuple[bool, str]:
        """Block opposite-direction entries when a position exists. Same-direction pyramiding allowed."""
        if signal.signal_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
            return True, ""
        signal_side = "long" if signal.signal_type == SignalType.LONG else "short"
        for pos_id, pos in self.portfolio.positions.items():
            sym = pos.get("symbol", self._base_symbol(pos_id))
            if sym == signal.symbol and pos["side"] != signal_side:
                return False, f"Already {pos['side']} on {signal.symbol} — close first before reversing"
        return True, ""

    def _check_pyramiding_limit(self, signal: Signal) -> tuple[bool, str]:
        """Enforce per-symbol pyramiding limits matching TradingView settings."""
        if signal.signal_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
            return True, ""
        max_positions = self.rules.max_pyramiding.get(
            signal.symbol, self.rules.default_max_pyramiding
        )
        current_count = sum(
            1 for pos_id, pos in self.portfolio.positions.items()
            if pos.get("symbol", self._base_symbol(pos_id)) == signal.symbol
        )
        if current_count >= max_positions:
            return False, (
                f"Pyramiding limit reached for {signal.symbol}: "
                f"{current_count}/{max_positions} positions"
            )
        return True, ""

    def _check_symbol_drawdown(self, signal: Signal) -> tuple[bool, str]:
        """Per-symbol DD protection matching TV's equity peak tracking + cooldown."""
        if signal.signal_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
            return True, ""  # Always allow closing
        dd_config = self.rules.symbol_max_drawdown.get(signal.symbol)
        if not dd_config:
            return True, ""
        triggered_at = self._symbol_dd_triggered_at.get(signal.symbol)
        if triggered_at:
            elapsed = (datetime.now() - triggered_at).total_seconds()
            cooldown = dd_config.get("cooldown_seconds", 15000)
            if elapsed < cooldown:
                remaining = int(cooldown - elapsed)
                return False, (
                    f"DD protection cooldown for {signal.symbol}: "
                    f"{remaining}s remaining (triggered at {triggered_at.strftime('%H:%M')})"
                )
            else:
                # Cooldown expired — reset
                del self._symbol_dd_triggered_at[signal.symbol]
                log.info(f"DD protection cooldown expired for {signal.symbol} — entries allowed")
        return True, ""

    def _check_correlation_exposure(self, signal: Signal) -> tuple[bool, str]:
        """Warn/block if opening a same-direction position highly correlated with existing ones."""
        if signal.signal_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
            return True, ""
        if not self.correlations or not self.portfolio.positions:
            return True, ""

        signal_side = "long" if signal.signal_type == SignalType.LONG else "short"

        for existing_id, pos in self.portfolio.positions.items():
            existing_sym = pos.get("symbol", self._base_symbol(existing_id))
            if existing_sym == signal.symbol:
                continue

            # Check correlation between new signal's symbol and existing position
            key1 = f"{signal.symbol}|{existing_sym}"
            key2 = f"{existing_sym}|{signal.symbol}"
            corr = self.correlations.get(key1) or self.correlations.get(key2)

            if corr is None:
                continue

            # Same direction + high positive correlation = concentrated risk
            same_direction = pos["side"] == signal_side
            if same_direction and corr > self.rules.max_correlation_exposure:
                return False, (
                    f"High correlation risk: {signal.symbol} & {existing_sym} "
                    f"corr={corr:.2f} (max {self.rules.max_correlation_exposure:.2f}), "
                    f"both {signal_side}"
                )

        return True, ""

    def _check_leverage(self, signal: Signal) -> tuple[bool, str]:
        """Enforce per-instrument max leverage."""
        if signal.signal_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
            return True, ""
        instrument = INSTRUMENTS.get(signal.symbol)
        if not instrument:
            return True, ""

        # Dynamic sizing: % of account × confidence
        if signal.size_usd:
            size_usd = signal.size_usd
        elif self.portfolio.account_balance > 0:
            size_usd = self.portfolio.account_balance * instrument.base_position_pct * signal.confidence
        else:
            size_usd = instrument.default_size * signal.confidence
        total_exposure = sum(abs(pos["size_usd"]) for pos in self.portfolio.positions.values())
        new_total = total_exposure + size_usd

        if self.portfolio.account_balance > 0:
            leverage = new_total / self.portfolio.account_balance
            log.debug(
                f"Leverage check: existing=${total_exposure:.2f} + new=${size_usd:.2f} "
                f"= {leverage:.2f}x (max {self.rules.max_portfolio_leverage:.1f}x)"
            )
            if leverage > self.rules.max_portfolio_leverage:
                return False, f"Portfolio leverage too high: {leverage:.1f}x (max {self.rules.max_portfolio_leverage:.1f}x)"

        return True, ""

    @staticmethod
    def _base_symbol(pos_id: str) -> str:
        """Extract base symbol from position ID (e.g., 'xyz:GOLD#2' -> 'xyz:GOLD')."""
        return pos_id.split('#')[0]

    def update_portfolio(self, account_state: dict):
        """Update portfolio state from HyperLiquid account data."""
        try:
            margin_summary = account_state.get("marginSummary", {})
            self.portfolio.account_balance = float(margin_summary.get("accountValue", 0))

            if self.portfolio.starting_balance == 0:
                self.portfolio.starting_balance = self.portfolio.account_balance

            # Track peak balance for account-level drawdown protection
            if self.portfolio.account_balance > self._account_peak_balance:
                self._account_peak_balance = self.portfolio.account_balance
                self._save_risk_state()

            # Update positions — keys are pos_ids (symbol or symbol#N for pyramids)
            self.portfolio.positions = {}
            for pos in account_state.get("assetPositions", []):
                position = pos.get("position", {})
                coin = position.get("coin", "")
                symbol = position.get("symbol", self._base_symbol(coin))
                szi = float(position.get("szi", 0))
                entry_px = float(position.get("entryPx", 0))

                if szi != 0 and symbol in INSTRUMENTS:
                    self.portfolio.positions[coin] = {
                        "symbol": symbol,
                        "side": "long" if szi > 0 else "short",
                        "size": abs(szi),
                        "size_usd": abs(szi) * entry_px,
                        "entry_price": entry_px,
                        "unrealized_pnl": float(position.get("unrealizedPnl", 0)),
                        "opened_at": position.get("openedAt", ""),
                    }

            log.info(
                f"Portfolio: balance=${self.portfolio.account_balance:.2f}, "
                f"positions={list(self.portfolio.positions.keys())}, "
                f"daily_pnl=${self.portfolio.daily_pnl:.2f}, "
                f"consecutive_losses={self.consecutive_losses}"
            )
        except Exception as e:
            log.error(f"Failed to update portfolio state: {e}")

    def record_trade(self, symbol: str, pnl: float = 0.0):
        """Record a completed trade for daily tracking, cooldown, and loss streaks."""
        self.portfolio.daily_trades += 1
        self.portfolio.daily_pnl += pnl
        self.last_trade_time[symbol] = datetime.now()
        log.debug(
            f"Trade recorded: {symbol} pnl=${pnl:.2f} | "
            f"daily_trades={self.portfolio.daily_trades}, daily_pnl=${self.portfolio.daily_pnl:.2f}, "
            f"consecutive_losses={self.consecutive_losses}"
        )

        # Track consecutive losses
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.rules.max_consecutive_losses:
                self.consecutive_loss_halt = True
                log.error(
                    f"CONSECUTIVE LOSS HALT: {self.consecutive_losses} losses in a row. "
                    f"Trading halted. Call reset_consecutive_losses() to resume."
                )
        elif pnl >= 0:
            # A winning or breakeven trade resets the counter
            self.consecutive_losses = 0

        # Per-symbol cumulative PnL for DD protection (matches TV strategy.equity tracking)
        base_sym = symbol.split("#")[0]
        cum = self._symbol_cumulative_pnl.get(base_sym, 0.0) + pnl
        self._symbol_cumulative_pnl[base_sym] = cum
        peak = self._symbol_peak_pnl.get(base_sym, 0.0)
        if cum > peak:
            self._symbol_peak_pnl[base_sym] = cum
            peak = cum
        dd_config = self.rules.symbol_max_drawdown.get(base_sym)
        if dd_config and peak > 0:
            dd_pct = (peak - cum) / peak
            if dd_pct >= dd_config["max_dd_pct"] and base_sym not in self._symbol_dd_triggered_at:
                self._symbol_dd_triggered_at[base_sym] = datetime.now()
                self._dd_close_all_pending.add(base_sym)
                log.warning(
                    f"DD PROTECTION triggered for {base_sym}: "
                    f"DD={dd_pct:.1%} >= {dd_config['max_dd_pct']:.0%} "
                    f"(peak PnL=${peak:.2f}, current=${cum:.2f}) — will close all positions"
                )

        self._save_risk_state()

    def get_dd_close_signals(self) -> list[Signal]:
        """Generate close-all signals for symbols where DD protection triggered.
        Matches TV's strategy.close_all(comment='DD protection')."""
        signals = []
        for symbol in list(self._dd_close_all_pending):
            for pos_id, pos in self.portfolio.positions.items():
                base = pos.get("symbol", self._base_symbol(pos_id))
                if base == symbol:
                    sig_type = SignalType.CLOSE_LONG if pos["side"] == "long" else SignalType.CLOSE_SHORT
                    signals.append(Signal(
                        symbol=symbol,
                        signal_type=sig_type,
                        strategy_name="dd_protection",
                        confidence=1.0,
                        reason=f"DD protection: closing all {symbol} positions",
                    ))
            self._dd_close_all_pending.discard(symbol)
        return signals

    def reset_consecutive_losses(self):
        """Manual reset of consecutive loss halt. Called by operator."""
        self.consecutive_losses = 0
        self.consecutive_loss_halt = False
        self._save_risk_state()
        log.info("Consecutive loss counter reset — trading resumed")

    def reset_account_dd_halt(self):
        """Manual reset of account drawdown halt. Resets peak to current balance."""
        self.account_dd_halt = False
        self._account_peak_balance = self.portfolio.account_balance
        self._save_risk_state()
        log.info(
            f"Account DD halt reset — peak set to current balance "
            f"${self.portfolio.account_balance:,.2f}, trading resumed"
        )
