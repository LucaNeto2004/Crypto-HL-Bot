"""Kill switch sanity test.

Verifies the daily-loss kill switch fires at the configured threshold,
locks out new entries once tripped, and resets on a new day.

Run:
    .venv/bin/python scripts/test_kill_switch.py
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import load_config
from core.risk import RiskGate
from strategies.base import Signal, SignalType


def make_signal(confidence: float = 0.8) -> Signal:
    return Signal(
        symbol="ETH",
        signal_type=SignalType.LONG,
        strategy_name="momentum_v15",
        confidence=confidence,
        size_usd=1000.0,
        stop_loss=3800.0,
        take_profit=4200.0,
        timestamp=datetime.now(),
    )


def fresh_gate(starting_balance: float = 10_000.0) -> RiskGate:
    config = load_config()
    gate = RiskGate(config)
    gate.portfolio.starting_balance = starting_balance
    gate.portfolio.account_balance = starting_balance
    gate.portfolio.daily_pnl = 0.0
    gate.portfolio.daily_date = date.today()
    gate.kill_switch = False
    gate.account_dd_halt = False
    return gate


def ok(msg: str) -> None:
    print(f"  \033[92m✓\033[0m {msg}")


def fail(msg: str) -> None:
    print(f"  \033[91m✗ FAIL\033[0m {msg}")


def section(title: str) -> None:
    print(f"\n[{title}]")


def main() -> None:
    print("=" * 70)
    print("KILL SWITCH SANITY TEST")
    print("=" * 70)

    passed = failed = 0

    def assert_eq(label: str, actual, expected):
        nonlocal passed, failed
        if actual == expected:
            ok(f"{label}: {actual}")
            passed += 1
        else:
            fail(f"{label}: got {actual!r}, expected {expected!r}")
            failed += 1

    section("1. Fresh gate — kill switch inactive, signal allowed")
    gate = fresh_gate()
    max_loss_pct = gate.rules.max_daily_loss_pct
    print(f"  max_daily_loss_pct = {max_loss_pct:.1%}")
    allowed, reason = gate._check_kill_switch(make_signal())
    assert_eq("kill_switch check (no loss)", allowed, True)
    assert_eq("kill_switch flag", gate.kill_switch, False)

    section("2. Loss JUST BELOW threshold — should still allow")
    gate = fresh_gate()
    gate.portfolio.daily_pnl = -(max_loss_pct - 0.001) * 10_000
    print(f"  daily_pnl = ${gate.portfolio.daily_pnl:.2f} ({gate.portfolio.daily_pnl/100:.2f}%)")
    allowed, reason = gate._check_kill_switch(make_signal())
    assert_eq("kill_switch check (below threshold)", allowed, True)
    assert_eq("kill_switch flag", gate.kill_switch, False)

    section("3. Loss AT threshold — should trip kill switch")
    gate = fresh_gate()
    gate.portfolio.daily_pnl = -max_loss_pct * 10_000
    print(f"  daily_pnl = ${gate.portfolio.daily_pnl:.2f} ({max_loss_pct:.1%} of ${gate.portfolio.starting_balance:.0f})")
    allowed, reason = gate._check_kill_switch(make_signal())
    assert_eq("kill_switch check (at threshold)", allowed, False)
    assert_eq("kill_switch flag set", gate.kill_switch, True)
    print(f"  reason: {reason}")

    section("4. Loss ABOVE threshold (7%) — should trip and stay tripped")
    gate = fresh_gate()
    gate.portfolio.daily_pnl = -700.0
    allowed, reason = gate._check_kill_switch(make_signal())
    assert_eq("first check rejects", allowed, False)
    assert_eq("kill_switch flag set", gate.kill_switch, True)
    # Second check via top-level check() — should be rejected by the early kill_switch guard
    allowed2, reason2 = gate.check(make_signal())
    assert_eq("top-level check() rejects when flag set", allowed2, False)
    assert_eq("reason mentions KILL SWITCH", "KILL SWITCH" in reason2, True)

    section("5. POSITIVE daily PnL — kill switch must not trip")
    gate = fresh_gate()
    gate.portfolio.daily_pnl = +2_000.0  # +20% win, should never trip
    allowed, _ = gate._check_kill_switch(make_signal())
    assert_eq("kill_switch check (positive pnl)", allowed, True)
    assert_eq("kill_switch flag", gate.kill_switch, False)

    section("6. CLOSE signals — must still be allowed after kill switch tripped")
    gate = fresh_gate()
    gate.portfolio.daily_pnl = -600.0
    gate._check_kill_switch(make_signal())  # trip it
    assert gate.kill_switch, "precondition: kill switch should be tripped"
    close_long = make_signal()
    close_long.signal_type = SignalType.CLOSE_LONG
    allowed, reason = gate.check(close_long)
    assert_eq("CLOSE_LONG allowed while halted", allowed, True)
    close_short = make_signal()
    close_short.signal_type = SignalType.CLOSE_SHORT
    allowed, reason = gate.check(close_short)
    assert_eq("CLOSE_SHORT allowed while halted", allowed, True)
    # Verify entries are still blocked
    allowed, _ = gate.check(make_signal())
    assert_eq("LONG entry still blocked while halted", allowed, False)

    section("7. New day resets the kill switch")
    gate = fresh_gate()
    gate.portfolio.daily_pnl = -700.0
    gate._check_kill_switch(make_signal())
    assert gate.kill_switch, "precondition"
    # Simulate day rollover
    gate.portfolio.daily_date = date.today() - timedelta(days=1)
    gate._reset_daily_counters()
    assert_eq("daily_pnl reset", gate.portfolio.daily_pnl, 0.0)
    assert_eq("kill_switch cleared", gate.kill_switch, False)

    print("\n" + "=" * 70)
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("=" * 70)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
