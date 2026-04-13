"""Structured per-trade JSONL logger.

Append-only sink for trade events. Read by `shared/reporting.py` to populate
the autopsy's regime / slippage sections.

A logging failure must NEVER kill a trade.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOG_PATH = os.path.join(_BASE_DIR, "logs", "trades.jsonl")
os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)


def log_event(action: str, **fields: Any) -> None:
    """Append one JSON line for a trade event."""
    try:
        record = {
            "timestamp": datetime.now().isoformat(timespec="microseconds"),
            "action": action,
        }
        record.update({k: v for k, v in fields.items() if v is not None})
        with open(_LOG_PATH, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass
