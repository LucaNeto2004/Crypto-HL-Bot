"""
Audit Journal — Append-only JSONL log of every signal and risk decision.
Used for post-trade analysis and dashboard display.
"""
import json
import os
from datetime import datetime
from typing import Optional

from strategies.base import Signal
from core.execution import TradeRecord
from utils.logger import setup_logger

log = setup_logger("audit")

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT_FILE = os.path.join(_BASE_DIR, "data", "audit_journal.jsonl")


class AuditJournal:
    def __init__(self):
        os.makedirs(os.path.dirname(AUDIT_FILE), exist_ok=True)

    def log_signal(
        self,
        signal: Signal,
        risk_passed: bool,
        risk_reason: str,
        trade: Optional[TradeRecord] = None,
    ):
        """Log a signal with its risk decision and execution result."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "symbol": signal.symbol,
            "signal_type": signal.signal_type.value,
            "strategy": signal.strategy_name,
            "confidence": round(signal.confidence, 3),
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "reason": signal.reason,
            "regime": signal.metadata.get("regime", "unknown"),
            "risk_passed": risk_passed,
            "risk_reason": risk_reason,
            "executed": trade is not None,
            "execution": None,
        }

        if trade:
            entry["execution"] = {
                "side": trade.side,
                "price": trade.price,
                "size": trade.size,
                "pnl": trade.pnl,
                "exit_reason": trade.exit_reason,
            }

        try:
            with open(AUDIT_FILE, "a") as f:
                f.write(json.dumps(entry) + "\n")
            log.debug(
                f"Audit: {signal.symbol} {signal.signal_type.value} "
                f"risk={'PASS' if risk_passed else 'FAIL'} exec={'YES' if trade else 'NO'}"
            )
        except Exception as e:
            log.error(f"Failed to write audit entry: {e}")

    def log_sl_tp_trigger(self, trade: TradeRecord):
        """Log a SL/TP triggered close as an audit entry."""
        entry = {
            "timestamp": trade.timestamp.isoformat(),
            "symbol": trade.symbol,
            "signal_type": trade.side,
            "strategy": trade.strategy,
            "confidence": 1.0,
            "stop_loss": None,
            "take_profit": None,
            "reason": f"SL/TP trigger: {trade.exit_reason}",
            "regime": "n/a",
            "risk_passed": True,
            "risk_reason": "sl_tp_auto",
            "executed": True,
            "execution": {
                "side": trade.side,
                "price": trade.price,
                "size": trade.size,
                "pnl": trade.pnl,
                "exit_reason": trade.exit_reason,
            },
        }

        try:
            with open(AUDIT_FILE, "a") as f:
                f.write(json.dumps(entry) + "\n")
            log.debug(f"Audit SL/TP: {trade.symbol} {trade.exit_reason} pnl=${trade.pnl:.2f}")
        except Exception as e:
            log.error(f"Failed to write audit entry: {e}")

    def read_recent(self, n: int = 50) -> list[dict]:
        """Read the last N audit entries."""
        try:
            if not os.path.exists(AUDIT_FILE):
                return []
            with open(AUDIT_FILE) as f:
                lines = f.readlines()
            entries = []
            for line in lines[-n:]:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
            return entries
        except Exception as e:
            log.error(f"Failed to read audit journal: {e}")
            return []
