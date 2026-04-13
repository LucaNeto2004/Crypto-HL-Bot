"""Tests for the Risk Gate — all 12 checks + state transitions."""
import pytest
from datetime import datetime, date, timedelta
from unittest.mock import patch, MagicMock

from config.settings import BotConfig, RiskConfig, Instrument
from core.risk import RiskGate, PortfolioState
from strategies.base import Signal, SignalType

# Test-only instruments so risk gate tests work regardless of which instruments are active
_TEST_INSTRUMENTS = {
    "xyz:GOLD": Instrument(symbol="xyz:GOLD", name="Gold", group="metals",
                           tick_size=0.1, lot_size=0.001, max_leverage=5, default_size=1000.0),
    "xyz:SILVER": Instrument(symbol="xyz:SILVER", name="Silver", group="metals",
                             tick_size=0.01, lot_size=0.01, max_leverage=5, default_size=500.0),
    "xyz:BRENTOIL": Instrument(symbol="xyz:BRENTOIL", name="Brent Crude Oil", group="energy",
                               tick_size=0.01, lot_size=0.01, max_leverage=5, default_size=1000.0),
}

@pytest.fixture(autouse=True)
def _mock_trading_hours():
    """Patch trading hours check to always pass, so tests work at any CI time."""
    with patch.object(RiskGate, "_check_trading_hours", return_value=(True, "")):
        yield

@pytest.fixture(autouse=True)
def _mock_instruments():
    """Patch INSTRUMENTS so risk gate tests work regardless of active instruments."""
    with patch("core.risk.INSTRUMENTS", _TEST_INSTRUMENTS):
        yield


def make_signal(symbol="xyz:GOLD", signal_type=SignalType.LONG,
                confidence=0.8, size_usd=1000, **kwargs):
    return Signal(
        symbol=symbol,
        signal_type=signal_type,
        strategy_name="test",
        confidence=confidence,
        size_usd=size_usd,
        **kwargs,
    )


def make_gate(paper=True, **risk_overrides) -> RiskGate:
    risk = RiskConfig(**risk_overrides)
    config = BotConfig(risk=risk, paper_trading=paper)
    gate = RiskGate(config)
    gate.portfolio.account_balance = 10000.0
    gate.portfolio.starting_balance = 10000.0
    # Reset state loaded from disk so tests are isolated
    gate.consecutive_losses = 0
    gate.consecutive_loss_halt = False
    gate.portfolio.daily_pnl = 0.0
    gate.portfolio.daily_trades = 0
    gate.last_trade_time = {}
    gate._symbol_cumulative_pnl = {}
    gate._symbol_peak_pnl = {}
    gate._symbol_dd_triggered_at = {}
    gate._dd_close_all_pending = set()
    gate._account_peak_balance = 10000.0  # Match starting balance
    gate.account_dd_halt = False
    return gate


# --- Basic pass/reject ---

class TestBasicFlow:
    def test_signal_passes_all_checks(self):
        gate = make_gate()
        passed, reason = gate.check(make_signal())
        assert passed
        assert reason == "passed"

    def test_close_signals_always_pass(self):
        gate = make_gate()
        gate.kill_switch = True
        # Kill switch blocks everything except... we need to test close separately
        gate2 = make_gate()
        gate2.consecutive_loss_halt = True
        gate2.consecutive_losses = 5
        sig = make_signal(signal_type=SignalType.CLOSE_LONG)
        passed, _ = gate2.check(sig)
        assert passed  # Close signals bypass consecutive loss halt


# --- Kill Switch ---

class TestKillSwitch:
    def test_kill_switch_activates_on_daily_loss(self):
        gate = make_gate()
        gate.portfolio.daily_pnl = -500.0  # -5% of 10k
        sig = make_signal()
        passed, reason = gate.check(sig)
        assert not passed
        assert "Daily loss limit" in reason
        assert gate.kill_switch

    def test_kill_switch_blocks_all_trades(self):
        gate = make_gate()
        gate.kill_switch = True
        passed, reason = gate.check(make_signal())
        assert not passed
        assert "KILL SWITCH ACTIVE" in reason

    def test_kill_switch_allows_under_threshold(self):
        gate = make_gate()
        gate.portfolio.daily_pnl = -300.0  # -3%, well below 5% and below 80% soft stop
        passed, _ = gate.check(make_signal())
        assert passed

    def test_no_kill_switch_on_positive_pnl(self):
        gate = make_gate()
        gate.portfolio.daily_pnl = 500.0
        passed, _ = gate.check(make_signal())
        assert passed


# --- Consecutive Loss Halt ---

class TestConsecutiveLosses:
    def test_halt_skipped_in_paper(self):
        gate = make_gate()  # paper=True by default
        gate.consecutive_losses = 5
        gate.consecutive_loss_halt = True
        passed, reason = gate.check(make_signal())
        assert passed  # Paper trading skips consecutive loss halt

    def test_halt_triggers_in_live(self):
        gate = make_gate(paper=False)
        gate.consecutive_losses = 5
        gate.consecutive_loss_halt = True
        passed, reason = gate.check(make_signal())
        assert not passed
        assert "CONSECUTIVE LOSS HALT" in reason

    def test_reset_consecutive_losses(self):
        gate = make_gate(paper=False)
        gate.consecutive_losses = 3
        gate.consecutive_loss_halt = True
        gate.reset_consecutive_losses()
        assert gate.consecutive_losses == 0
        assert not gate.consecutive_loss_halt

    def test_record_trade_tracks_losses(self):
        gate = make_gate()
        gate.record_trade("xyz:GOLD", pnl=-10)
        assert gate.consecutive_losses == 1
        gate.record_trade("xyz:GOLD", pnl=-20)
        assert gate.consecutive_losses == 2
        gate.record_trade("xyz:GOLD", pnl=-5)
        assert gate.consecutive_losses == 3
        assert not gate.consecutive_loss_halt  # max is 5, not halted yet
        gate.record_trade("xyz:GOLD", pnl=-8)
        assert gate.consecutive_losses == 4
        gate.record_trade("xyz:GOLD", pnl=-3)
        assert gate.consecutive_losses == 5
        assert gate.consecutive_loss_halt

    def test_winning_trade_resets_streak(self):
        gate = make_gate()
        gate.record_trade("xyz:GOLD", pnl=-10)
        gate.record_trade("xyz:GOLD", pnl=-10)
        assert gate.consecutive_losses == 2
        gate.record_trade("xyz:GOLD", pnl=50)
        assert gate.consecutive_losses == 0


# --- Account Drawdown ---

class TestAccountDrawdown:
    def test_halt_when_dd_exceeds_limit(self):
        gate = make_gate(max_account_drawdown_pct=0.15)
        gate._account_peak_balance = 12000.0
        gate.portfolio.account_balance = 10000.0  # 16.7% DD from peak
        passed, reason = gate.check(make_signal())
        assert not passed
        assert "ACCOUNT DRAWDOWN HALT" in reason
        assert gate.account_dd_halt

    def test_allows_within_dd_limit(self):
        gate = make_gate(max_account_drawdown_pct=0.15)
        gate._account_peak_balance = 11000.0
        gate.portfolio.account_balance = 10000.0  # 9.1% DD — under 15%
        passed, _ = gate.check(make_signal())
        assert passed

    def test_halt_blocks_all_entries(self):
        gate = make_gate()
        gate.account_dd_halt = True
        gate._account_peak_balance = 12000.0
        passed, reason = gate.check(make_signal())
        assert not passed
        assert "ACCOUNT DRAWDOWN HALT" in reason

    def test_halt_allows_closes(self):
        gate = make_gate()
        gate.account_dd_halt = True
        gate._account_peak_balance = 12000.0
        sig = make_signal(signal_type=SignalType.CLOSE_LONG)
        passed, _ = gate.check(sig)
        assert passed

    def test_reset_account_dd_halt(self):
        gate = make_gate()
        gate.account_dd_halt = True
        gate._account_peak_balance = 12000.0
        gate.portfolio.account_balance = 10000.0
        gate.reset_account_dd_halt()
        assert not gate.account_dd_halt
        assert gate._account_peak_balance == 10000.0  # Peak reset to current

    def test_peak_updates_upward_only(self):
        gate = make_gate()
        gate._account_peak_balance = 10000.0
        # Simulate balance going up
        gate.portfolio.account_balance = 11000.0
        gate.update_portfolio({"marginSummary": {"accountValue": "11000"}, "assetPositions": []})
        assert gate._account_peak_balance == 11000.0
        # Balance goes down — peak stays
        gate.update_portfolio({"marginSummary": {"accountValue": "10500"}, "assetPositions": []})
        assert gate._account_peak_balance == 11000.0


# --- Confidence ---

class TestConfidence:
    def test_rejects_low_confidence(self):
        gate = make_gate(min_signal_confidence=0.4)
        sig = make_signal(confidence=0.3)
        passed, reason = gate.check(sig)
        assert not passed
        assert "confidence too low" in reason

    def test_passes_high_confidence(self):
        gate = make_gate(min_signal_confidence=0.4)
        sig = make_signal(confidence=0.5)
        passed, _ = gate.check(sig)
        assert passed

    def test_close_ignores_confidence(self):
        gate = make_gate(min_signal_confidence=0.4)
        sig = make_signal(signal_type=SignalType.CLOSE_LONG, confidence=0.1)
        passed, _ = gate.check(sig)
        assert passed


# --- Cooldown ---

class TestCooldown:
    def test_rejects_trade_within_cooldown(self):
        gate = make_gate(min_trade_cooldown_seconds=300)
        gate.last_trade_time["xyz:GOLD"] = datetime.now()
        passed, reason = gate.check(make_signal())
        assert not passed
        assert "cooldown" in reason.lower()

    def test_allows_trade_after_cooldown(self):
        gate = make_gate(min_trade_cooldown_seconds=300)
        gate.last_trade_time["xyz:GOLD"] = datetime.now() - timedelta(seconds=301)
        passed, _ = gate.check(make_signal())
        assert passed

    def test_close_ignores_cooldown(self):
        gate = make_gate(min_trade_cooldown_seconds=300)
        gate.last_trade_time["xyz:GOLD"] = datetime.now()
        sig = make_signal(signal_type=SignalType.CLOSE_LONG)
        passed, _ = gate.check(sig)
        assert passed

    def test_different_symbol_no_cooldown(self):
        gate = make_gate(min_trade_cooldown_seconds=300)
        gate.last_trade_time["xyz:GOLD"] = datetime.now()
        sig = make_signal(symbol="xyz:SILVER")
        passed, _ = gate.check(sig)
        assert passed


# --- Position Limits ---

class TestPositionLimits:
    def test_rejects_over_max_positions(self):
        gate = make_gate(max_open_positions=2)
        gate.portfolio.positions = {
            "xyz:GOLD": {"side": "long", "size_usd": 1000},
            "xyz:SILVER": {"side": "short", "size_usd": 500},
        }
        sig = make_signal(symbol="xyz:BRENTOIL")
        passed, reason = gate.check(sig)
        assert not passed
        assert "Max open positions" in reason

    def test_allows_under_max_positions(self):
        gate = make_gate(max_open_positions=4)
        gate.portfolio.positions = {
            "xyz:GOLD": {"side": "long", "size_usd": 1000},
        }
        sig = make_signal(symbol="xyz:SILVER")
        passed, _ = gate.check(sig)
        assert passed


# --- Single Position Size ---

class TestPositionSize:
    def test_rejects_oversized_position(self):
        gate = make_gate(max_single_position_pct=0.30)
        sig = make_signal(size_usd=3500)  # 35% of 10k
        passed, reason = gate.check(sig)
        assert not passed
        assert "Position too large" in reason

    def test_allows_normal_size(self):
        gate = make_gate(max_single_position_pct=0.30)
        sig = make_signal(size_usd=2000)  # 20%
        passed, _ = gate.check(sig)
        assert passed


# --- Group Exposure ---

class TestGroupExposure:
    def test_rejects_excess_group_exposure(self):
        gate = make_gate(max_group_exposure_pct=0.50)
        gate.portfolio.positions = {
            "xyz:GOLD": {"side": "long", "size_usd": 3000},
        }
        sig = make_signal(symbol="xyz:SILVER", size_usd=2500)  # metals total = 5500 = 55%
        passed, reason = gate.check(sig)
        assert not passed
        assert "group exposure" in reason.lower()

    def test_allows_within_group_limit(self):
        gate = make_gate(max_group_exposure_pct=0.50)
        gate.portfolio.positions = {
            "xyz:GOLD": {"side": "long", "size_usd": 2000},
        }
        sig = make_signal(symbol="xyz:SILVER", size_usd=2000)  # metals total = 4000 = 40%
        passed, _ = gate.check(sig)
        assert passed

    def test_different_group_independent(self):
        gate = make_gate(max_group_exposure_pct=0.50)
        gate.portfolio.positions = {
            "xyz:GOLD": {"side": "long", "size_usd": 4000},
        }
        sig = make_signal(symbol="xyz:BRENTOIL", size_usd=2000)  # energy, not metals
        passed, _ = gate.check(sig)
        assert passed


# --- Duplicate Position ---

class TestDuplicatePosition:
    def test_allows_same_direction_pyramid(self):
        gate = make_gate()
        gate.portfolio.positions = {
            "xyz:GOLD": {"side": "long", "size_usd": 1000},
        }
        sig = make_signal(signal_type=SignalType.LONG)
        passed, _ = gate.check(sig)
        assert passed

    def test_rejects_opposite_direction(self):
        gate = make_gate()
        gate.portfolio.positions = {
            "xyz:GOLD": {"side": "long", "size_usd": 1000},
        }
        sig = make_signal(signal_type=SignalType.SHORT)
        passed, reason = gate.check(sig)
        assert not passed
        assert "close first" in reason.lower()


# --- Leverage ---

class TestLeverage:
    def test_rejects_excessive_leverage(self):
        gate = make_gate(max_portfolio_leverage=3.0, max_single_position_pct=1.0,
                         max_group_exposure_pct=1.0)
        gate.portfolio.positions = {
            "xyz:GOLD": {"side": "long", "size_usd": 15000},
            "xyz:SILVER": {"side": "short", "size_usd": 10000},
        }
        sig = make_signal(symbol="xyz:BRENTOIL", size_usd=6000)  # total = 31k, 3.1x
        passed, reason = gate.check(sig)
        assert not passed
        assert "leverage too high" in reason.lower()

    def test_allows_within_leverage(self):
        gate = make_gate(max_portfolio_leverage=3.0)
        gate.portfolio.positions = {
            "xyz:GOLD": {"side": "long", "size_usd": 5000},
        }
        sig = make_signal(symbol="xyz:BRENTOIL", size_usd=2000)  # total = 7k, 0.7x
        passed, _ = gate.check(sig)
        assert passed


# --- Daily Trade Limit ---

# --- Record Trade ---

class TestRecordTrade:
    def test_increments_daily_trades(self):
        gate = make_gate()
        gate.record_trade("xyz:GOLD", pnl=50)
        assert gate.portfolio.daily_trades == 1
        assert gate.portfolio.daily_pnl == 50

    def test_tracks_cooldown_time(self):
        gate = make_gate()
        gate.record_trade("xyz:GOLD", pnl=0)
        assert "xyz:GOLD" in gate.last_trade_time

    def test_accumulates_daily_pnl(self):
        gate = make_gate()
        gate.record_trade("xyz:GOLD", pnl=100)
        gate.record_trade("xyz:SILVER", pnl=-30)
        assert gate.portfolio.daily_pnl == 70
        assert gate.portfolio.daily_trades == 2


# --- Update Portfolio ---

class TestUpdatePortfolio:
    def test_updates_from_account_state(self):
        gate = make_gate()
        state = {
            "marginSummary": {"accountValue": "12345.67"},
            "assetPositions": [
                {
                    "position": {
                        "coin": "xyz:GOLD",
                        "szi": "0.5",
                        "entryPx": "2000.0",
                        "unrealizedPnl": "25.0",
                    }
                }
            ],
        }
        gate.update_portfolio(state)
        assert gate.portfolio.account_balance == 12345.67
        assert "xyz:GOLD" in gate.portfolio.positions
        assert gate.portfolio.positions["xyz:GOLD"]["side"] == "long"

    def test_negative_size_is_short(self):
        gate = make_gate()
        state = {
            "marginSummary": {"accountValue": "10000"},
            "assetPositions": [
                {
                    "position": {
                        "coin": "xyz:GOLD",
                        "szi": "-0.3",
                        "entryPx": "2000.0",
                        "unrealizedPnl": "0",
                    }
                }
            ],
        }
        gate.update_portfolio(state)
        assert gate.portfolio.positions["xyz:GOLD"]["side"] == "short"

    def test_sets_starting_balance_once(self):
        gate = make_gate()
        gate.portfolio.starting_balance = 0
        state = {"marginSummary": {"accountValue": "5000"}, "assetPositions": []}
        gate.update_portfolio(state)
        assert gate.portfolio.starting_balance == 5000.0
        state2 = {"marginSummary": {"accountValue": "6000"}, "assetPositions": []}
        gate.update_portfolio(state2)
        assert gate.portfolio.starting_balance == 5000.0  # unchanged
