"""
Base Strategy — Interface all strategies implement.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

import pandas as pd


class SignalType(Enum):
    LONG = "long"
    SHORT = "short"
    CLOSE_LONG = "close_long"
    CLOSE_SHORT = "close_short"
    NONE = "none"


@dataclass
class Signal:
    symbol: str
    signal_type: SignalType
    strategy_name: str
    confidence: float          # 0.0 to 1.0
    size_usd: Optional[float] = None   # Override default size
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    trail_atr_mult: Optional[float] = None  # If set, use trailing stop instead of fixed TP
    reason: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: dict = field(default_factory=dict)


class BaseStrategy(ABC):
    def __init__(self, name: str):
        self.name = name
        self.enabled = True

    @abstractmethod
    def evaluate(self, symbol: str, df: pd.DataFrame, features: dict) -> Optional[Signal]:
        """
        Evaluate the strategy on the given data.
        Returns a Signal if there's a trade, None otherwise.
        """
        pass

    @abstractmethod
    def should_close(self, symbol: str, df: pd.DataFrame, features: dict,
                     position_side: str) -> Optional[Signal]:
        """
        Check if an existing position should be closed.
        position_side is 'long' or 'short'.
        """
        pass
