"""Tests for PaperTrader — P&L, SL/TP, position sizing, state."""
import json
import os
import pytest
import tempfile
from unittest.mock import patch
from datetime import datetime

from core.execution import PaperTrader, TradeRecord, PAPER_STATE_FILE
from strategies.base import Signal, SignalType


def make_signal(symbol="xyz:GOLD", signal_type=SignalType.LONG,
                confidence=0.8, size_usd=1000, stop_loss=None,
                take_profit=None, **kwargs):
    return Signal(
        symbol=symbol,
        signal_type=signal_type,
        strategy_name="test",
        confidence=confidence,
        size_usd=size_usd,
        stop_loss=stop_loss,
        take_profit=take_profit,
        **kwargs,
    )


@pytest.fixture
def trader(tmp_path):
    """Create a PaperTrader with isolated state file."""
    state_file = str(tmp_path / "paper_state.json")
    with patch("core.execution.PAPER_STATE_FILE", state_file):
        t = PaperTrader()
        t.balance = 10000.0
        yield t


# --- Opening Positions ---

class TestOpenPosition:
    def test_open_long(self, trader):
        sig = make_signal(signal_type=SignalType.LONG, confidence=1.0, size_usd=1000)
        record = trader.execute_signal(sig, current_price=2000.0)
        assert record is not None
        assert record.side == "long"
        assert record.symbol == "xyz:GOLD"
        assert "xyz:GOLD" in trader.positions
        assert trader.positions["xyz:GOLD"]["side"] == "long"

    def test_open_short(self, trader):
        sig = make_signal(signal_type=SignalType.SHORT, confidence=1.0, size_usd=1000)
        record = trader.execute_signal(sig, current_price=2000.0)
        assert record.side == "short"
        assert trader.positions["xyz:GOLD"]["side"] == "short"

    def test_confidence_scales_size(self, trader):
        sig = make_signal(confidence=0.5, size_usd=1000)
        record = trader.execute_signal(sig, current_price=2000.0)
        # size_usd = 1000 * 0.5 = 500, size = 500/2000 = 0.25
        assert record.size == pytest.approx(0.25)

    def test_min_confidence_floor(self, trader):
        sig = make_signal(confidence=0.1, size_usd=1000)
        record = trader.execute_signal(sig, current_price=2000.0)
        # confidence clamped to 0.5: size_usd = 1000 * 0.5 = 500, size = 500/2000 = 0.25
        assert record.size == pytest.approx(0.25)

    def test_position_stores_sl_tp(self, trader):
        # SL at 1.5% from entry (within 3.5% cap), TP at 5%
        sig = make_signal(stop_loss=1970.0, take_profit=2100.0, confidence=1.0)
        trader.execute_signal(sig, current_price=2000.0)
        pos = trader.positions["xyz:GOLD"]
        assert pos["stop_loss"] == 1970.0
        assert pos["take_profit"] == 2100.0


# --- Closing Positions ---

class TestClosePosition:
    def test_close_long_profit(self, trader):
        # Open long at 2000
        trader.execute_signal(
            make_signal(signal_type=SignalType.LONG, confidence=1.0, size_usd=2000),
            current_price=2000.0,
        )
        # Close at 2100 (size=1.0, profit = (2100-2000)*1.0 = 100)
        record = trader.execute_signal(
            make_signal(signal_type=SignalType.CLOSE_LONG),
            current_price=2100.0,
        )
        assert record is not None
        assert record.pnl == pytest.approx(100.0)
        assert trader.balance == pytest.approx(10100.0)
        assert "xyz:GOLD" not in trader.positions

    def test_close_long_loss(self, trader):
        trader.execute_signal(
            make_signal(signal_type=SignalType.LONG, confidence=1.0, size_usd=2000),
            current_price=2000.0,
        )
        record = trader.execute_signal(
            make_signal(signal_type=SignalType.CLOSE_LONG),
            current_price=1900.0,
        )
        assert record.pnl == pytest.approx(-100.0)
        assert trader.balance == pytest.approx(9900.0)

    def test_close_short_profit(self, trader):
        trader.execute_signal(
            make_signal(signal_type=SignalType.SHORT, confidence=1.0, size_usd=2000),
            current_price=2000.0,
        )
        record = trader.execute_signal(
            make_signal(signal_type=SignalType.CLOSE_SHORT),
            current_price=1900.0,
        )
        # Short profit = (2000 - 1900) * 1.0 = 100
        assert record.pnl == pytest.approx(100.0)

    def test_close_short_loss(self, trader):
        trader.execute_signal(
            make_signal(signal_type=SignalType.SHORT, confidence=1.0, size_usd=2000),
            current_price=2000.0,
        )
        record = trader.execute_signal(
            make_signal(signal_type=SignalType.CLOSE_SHORT),
            current_price=2100.0,
        )
        assert record.pnl == pytest.approx(-100.0)

    def test_close_nonexistent_returns_none(self, trader):
        record = trader.execute_signal(
            make_signal(signal_type=SignalType.CLOSE_LONG),
            current_price=2000.0,
        )
        assert record is None

    def test_close_wrong_side_returns_none(self, trader):
        trader.execute_signal(
            make_signal(signal_type=SignalType.LONG, confidence=1.0),
            current_price=2000.0,
        )
        record = trader.execute_signal(
            make_signal(signal_type=SignalType.CLOSE_SHORT),
            current_price=2000.0,
        )
        assert record is None


# --- SL/TP ---

class TestStopLossTakeProfit:
    def test_stop_loss_long(self, trader):
        trader.execute_signal(
            make_signal(signal_type=SignalType.LONG, confidence=1.0,
                        size_usd=2000, stop_loss=1950.0, take_profit=2100.0),
            current_price=2000.0,
        )
        triggered = trader.check_sl_tp({"xyz:GOLD": 1940.0})
        assert len(triggered) == 1
        assert triggered[0].exit_reason == "stop_loss"
        assert triggered[0].pnl < 0
        assert "xyz:GOLD" not in trader.positions

    def test_take_profit_long(self, trader):
        trader.execute_signal(
            make_signal(signal_type=SignalType.LONG, confidence=1.0,
                        size_usd=2000, stop_loss=1950.0, take_profit=2100.0),
            current_price=2000.0,
        )
        triggered = trader.check_sl_tp({"xyz:GOLD": 2150.0})
        assert len(triggered) == 1
        assert triggered[0].exit_reason == "take_profit"
        assert triggered[0].pnl > 0

    def test_stop_loss_short(self, trader):
        trader.execute_signal(
            make_signal(signal_type=SignalType.SHORT, confidence=1.0,
                        size_usd=2000, stop_loss=2050.0, take_profit=1900.0),
            current_price=2000.0,
        )
        triggered = trader.check_sl_tp({"xyz:GOLD": 2060.0})
        assert len(triggered) == 1
        assert triggered[0].exit_reason == "stop_loss"

    def test_take_profit_short(self, trader):
        trader.execute_signal(
            make_signal(signal_type=SignalType.SHORT, confidence=1.0,
                        size_usd=2000, stop_loss=2050.0, take_profit=1900.0),
            current_price=2000.0,
        )
        triggered = trader.check_sl_tp({"xyz:GOLD": 1890.0})
        assert len(triggered) == 1
        assert triggered[0].exit_reason == "take_profit"

    def test_no_trigger_between_sl_tp(self, trader):
        trader.execute_signal(
            make_signal(signal_type=SignalType.LONG, confidence=1.0,
                        size_usd=2000, stop_loss=1950.0, take_profit=2100.0),
            current_price=2000.0,
        )
        triggered = trader.check_sl_tp({"xyz:GOLD": 2050.0})
        assert len(triggered) == 0
        assert "xyz:GOLD" in trader.positions

    def test_no_price_skips_check(self, trader):
        trader.execute_signal(
            make_signal(signal_type=SignalType.LONG, confidence=1.0,
                        size_usd=2000, stop_loss=1950.0, take_profit=2100.0),
            current_price=2000.0,
        )
        triggered = trader.check_sl_tp({})  # no price for GOLD
        assert len(triggered) == 0


# --- Account State ---

class TestAccountState:
    def test_returns_hl_format(self, trader):
        trader.execute_signal(
            make_signal(signal_type=SignalType.LONG, confidence=1.0, size_usd=2000),
            current_price=2000.0,
        )
        state = trader.get_account_state()
        assert "marginSummary" in state
        assert "assetPositions" in state
        assert float(state["marginSummary"]["accountValue"]) == trader.balance
        assert len(state["assetPositions"]) == 1
        pos = state["assetPositions"][0]["position"]
        assert pos["coin"] == "xyz:GOLD"
        assert float(pos["szi"]) > 0  # long = positive

    def test_short_has_negative_size(self, trader):
        trader.execute_signal(
            make_signal(signal_type=SignalType.SHORT, confidence=1.0, size_usd=2000),
            current_price=2000.0,
        )
        state = trader.get_account_state()
        pos = state["assetPositions"][0]["position"]
        assert float(pos["szi"]) < 0  # short = negative


# --- Trade History ---

class TestTradeHistory:
    def test_records_all_trades(self, trader):
        trader.execute_signal(
            make_signal(signal_type=SignalType.LONG, confidence=1.0),
            current_price=2000.0,
        )
        trader.execute_signal(
            make_signal(signal_type=SignalType.CLOSE_LONG),
            current_price=2100.0,
        )
        assert len(trader.trade_history) == 2
        assert trader.trade_history[0].pnl is None  # open trade
        assert trader.trade_history[1].pnl is not None  # close trade

    def test_exit_reason_set(self, trader):
        trader.execute_signal(
            make_signal(signal_type=SignalType.LONG, confidence=1.0,
                        stop_loss=1900.0, take_profit=2100.0),
            current_price=2000.0,
        )
        triggered = trader.check_sl_tp({"xyz:GOLD": 1850.0})
        assert triggered[0].exit_reason == "stop_loss"
