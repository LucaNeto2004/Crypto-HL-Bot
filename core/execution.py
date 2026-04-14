"""
Execution Module — Places and manages orders on HyperLiquid.
Handles both live and paper trading modes.
Paper SL/TP enforcement included.
"""
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from config.settings import BotConfig, INSTRUMENTS
from strategies.base import Signal, SignalType
from utils.logger import setup_logger

log = setup_logger("execution")


@dataclass
class TradeRecord:
    timestamp: datetime
    symbol: str
    side: str
    size: float
    price: float
    strategy: str
    pnl: Optional[float] = None
    order_id: Optional[str] = None
    paper: bool = True
    exit_reason: str = ""  # "signal", "stop_loss", "take_profit"


_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAPER_STATE_FILE = os.path.join(_BASE_DIR, "data", "paper_state.json")
LIVE_STATE_FILE = os.path.join(_BASE_DIR, "data", "live_state.json")
ADAPTIVE_STOPS_FILE = os.path.join(_BASE_DIR, "data", "adaptive_stops.json")


def _load_adaptive_stops(max_age_hours: int = 48) -> dict:
    """Read adaptive_stops.json on every entry (cheap — small file) so the bot
    picks up the nightly profile refresh without a restart. Returns {} if the
    file is missing or stale."""
    try:
        if not os.path.exists(ADAPTIVE_STOPS_FILE):
            return {}
        with open(ADAPTIVE_STOPS_FILE) as f:
            data = json.load(f)
        gen = data.get("generated_at", "")
        if gen:
            try:
                stamp = datetime.fromisoformat(gen.rstrip("Z"))
                if (datetime.utcnow() - stamp).total_seconds() > max_age_hours * 3600:
                    return {}
            except Exception:
                pass
        return data.get("symbols", {}) or {}
    except Exception as e:
        log.warning(f"[ADAPTIVE] Failed to read {ADAPTIVE_STOPS_FILE}: {e}")
        return {}


class PaperTrader:
    """Simulates order execution for paper trading. Persists state to disk."""

    def __init__(self):
        self.positions: dict = {}       # pos_id -> {"side", "size", "entry_price", "stop_loss", "take_profit", "strategy"}
        self.trade_history: list[TradeRecord] = []
        self.balance: float = 10000.0   # Starting paper balance
        self._last_mtime: float = 0.0   # Track file modification time
        self._load_state()

    @staticmethod
    def _base_symbol(pos_id: str) -> str:
        """Extract base symbol from position ID (e.g., 'xyz:GOLD#2' -> 'xyz:GOLD')."""
        return pos_id.split('#')[0]

    def _next_pos_id(self, symbol: str) -> str:
        """Generate next unique position ID for pyramiding."""
        if symbol not in self.positions:
            return symbol
        n = 2
        while f"{symbol}#{n}" in self.positions:
            n += 1
        return f"{symbol}#{n}"

    def _positions_for_symbol(self, symbol: str) -> list[tuple[str, dict]]:
        """Find all positions for a base symbol."""
        return [(pid, pos) for pid, pos in self.positions.items()
                if self._base_symbol(pid) == symbol]

    def _save_state(self):
        """Persist paper state to JSON for dashboard to read."""
        try:
            state = {
                "balance": self.balance,
                "positions": self.positions,
                "trade_history": [
                    {
                        "timestamp": t.timestamp.isoformat(),
                        "symbol": t.symbol,
                        "side": t.side,
                        "size": t.size,
                        "price": t.price,
                        "strategy": t.strategy,
                        "pnl": t.pnl,
                        "paper": t.paper,
                        "exit_reason": t.exit_reason,
                    }
                    for t in self.trade_history
                ],
            }
            tmp = PAPER_STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, PAPER_STATE_FILE)
            self._last_mtime = os.path.getmtime(PAPER_STATE_FILE)
        except Exception as e:
            log.error(f"Failed to save paper state: {e}")

    def _load_state(self):
        """Load paper state from disk if it exists."""
        try:
            if os.path.exists(PAPER_STATE_FILE):
                self._last_mtime = os.path.getmtime(PAPER_STATE_FILE)
                with open(PAPER_STATE_FILE) as f:
                    state = json.load(f)
                self.balance = max(state.get("balance", 10000.0), 0.0)
                self.positions = state.get("positions", {})
                self.trade_history = [
                    TradeRecord(
                        timestamp=datetime.fromisoformat(t["timestamp"]),
                        symbol=t["symbol"],
                        side=t["side"],
                        size=t["size"],
                        price=t["price"],
                        strategy=t["strategy"],
                        pnl=t.get("pnl"),
                        paper=t.get("paper", True),
                        exit_reason=t.get("exit_reason", ""),
                    )
                    for t in state.get("trade_history", [])
                ]
                log.info(f"[PAPER] Loaded state: balance=${self.balance:.2f}, {len(self.positions)} positions, {len(self.trade_history)} trades")
        except Exception as e:
            log.warning(f"Could not load paper state: {e} — starting fresh")

    def sync_from_disk(self):
        """Reload state if the file was modified externally (e.g. dashboard manual close)."""
        try:
            if os.path.exists(PAPER_STATE_FILE):
                mtime = os.path.getmtime(PAPER_STATE_FILE)
                if mtime > self._last_mtime:
                    old_positions = set(self.positions.keys())
                    self._load_state()
                    new_positions = set(self.positions.keys())
                    closed = old_positions - new_positions
                    if closed:
                        log.info(f"[PAPER] External close detected: {closed}")
        except Exception as e:
            log.debug(f"sync_from_disk error: {e}")

    def execute_signal(self, signal: Signal, current_price: float, close_one: bool = False) -> Optional[TradeRecord]:
        if signal.signal_type == SignalType.LONG:
            return self._open_position(signal, current_price, "long")
        elif signal.signal_type == SignalType.SHORT:
            return self._open_position(signal, current_price, "short")
        elif signal.signal_type == SignalType.CLOSE_LONG:
            reason = signal.reason if signal.reason else "signal"
            if close_one:
                return self._close_oldest_position(signal, current_price, "long", exit_reason=reason)
            return self._close_position(signal, current_price, "long", exit_reason=reason)
        elif signal.signal_type == SignalType.CLOSE_SHORT:
            reason = signal.reason if signal.reason else "signal"
            if close_one:
                return self._close_oldest_position(signal, current_price, "short", exit_reason=reason)
            return self._close_position(signal, current_price, "short", exit_reason=reason)
        return None

    def _close_oldest_position(self, signal: Signal, price: float, side: str,
                               exit_reason: str = "signal") -> Optional[TradeRecord]:
        """Close only the OLDEST position for a symbol matching side (FIFO).
        Used by TV webhook exits — TV fires one exit per pyramid layer."""
        matching = [(pid, pos) for pid, pos in self.positions.items()
                    if self._base_symbol(pid) == signal.symbol and pos["side"] == side]
        if not matching:
            log.warning(f"[PAPER] No {side} position to close for {signal.symbol}")
            return None

        # Sort by opened_at to get oldest first
        matching.sort(key=lambda x: x[1].get("opened_at", ""))
        pos_id, pos = matching[0]
        return self._close_single_position(pos_id, price, exit_reason=exit_reason)

    def _open_position(self, signal: Signal, price: float, side: str) -> TradeRecord:
        instrument = INSTRUMENTS.get(signal.symbol)

        # Defensive pyramiding check — read from RiskConfig.max_pyramiding so it
        # stays in sync with the actual config (was hardcoded with commodities
        # symbols copy-pasted from the commodities-bot fork).
        from config.settings import load_config
        max_pyr_map = load_config().risk.max_pyramiding or {}
        MAX_PYRAMID = max_pyr_map.get(signal.symbol, load_config().risk.default_max_pyramiding or 3)
        existing = [(pid, pos) for pid, pos in self.positions.items()
                    if self._base_symbol(pid) == signal.symbol and pos["side"] == side]
        if len(existing) >= MAX_PYRAMID:
            log.warning(f"[PAPER] Pyramiding limit ({MAX_PYRAMID}) reached for {signal.symbol} {side} — skipping")
            return None

        # Dynamic position sizing: % of account × confidence
        if signal.size_usd:
            size_usd = signal.size_usd
            log.debug(f"[PAPER] {signal.symbol}: using signal size_usd=${size_usd:.2f}")
        elif self.balance > 0 and instrument:
            size_usd = self.balance * instrument.base_position_pct
            log.debug(
                f"[PAPER] {signal.symbol}: base size=${size_usd:.2f} "
                f"(balance=${self.balance:.2f} × {instrument.base_position_pct:.0%})"
            )
        else:
            size_usd = instrument.default_size if instrument else 1000
            log.debug(f"[PAPER] {signal.symbol}: using default size=${size_usd:.2f}")

        # Scale by confidence (min 50% — matches risk gate min_signal_confidence)
        pre_confidence = size_usd
        size_usd *= max(signal.confidence, 0.5)
        log.debug(
            f"[PAPER] {signal.symbol}: confidence scaling ${pre_confidence:.2f} × "
            f"{max(signal.confidence, 0.5):.2f} = ${size_usd:.2f}"
        )

        size = size_usd / price

        # Fix SL/TP if signal price drifted from execution price
        sl = signal.stop_loss
        tp = signal.take_profit
        _adaptive_applied = False
        _adaptive_trail_offset = None
        _adaptive_trail_activation = None

        # --- ADAPTIVE SL/TP OVERRIDE ---------------------------------------
        # Read the nightly adaptive_stops.json and, if the symbol has a
        # fresh profile, override SL/TP using skew-adjusted multipliers and a
        # regime-aware R:R target. Respects the PAUSE flag. Falls through
        # silently if the file is missing or has no profile for the symbol.
        _adaptive_all = _load_adaptive_stops()
        _adaptive = _adaptive_all.get(signal.symbol) if isinstance(_adaptive_all, dict) else None
        _base_mult = getattr(signal, "atr_stop_mult", None)
        if _adaptive and sl is not None and _base_mult and _base_mult > 0:
            if _adaptive.get("pause", False):
                log.warning(
                    f"[ADAPTIVE] {signal.symbol} PAUSED — skipping {side} entry "
                    f"({_adaptive.get('reason', 'distribution shift')})"
                )
                return None

            _adap_mult = _adaptive.get("long_sl_mult" if side == "long" else "short_sl_mult")
            _target_rr = float(_adaptive.get("target_rr", 2.0))
            _trail_mult = float(_adaptive.get("trail_mult", 0.6))
            _trail_arm_atr = float(_adaptive.get("trail_arm_atr", 1.0))

            if _adap_mult and _adap_mult > 0:
                _sl_distance_old = abs(price - sl)
                _atr_estimate = _sl_distance_old / _base_mult if _base_mult > 0 else 0.0
                _sl_distance_new = _atr_estimate * float(_adap_mult)

                if _sl_distance_new > 0:
                    if side == "long":
                        sl = price - _sl_distance_new
                        tp = price + _sl_distance_new * _target_rr
                    else:
                        sl = price + _sl_distance_new
                        tp = price - _sl_distance_new * _target_rr

                    _adaptive_trail_offset = _atr_estimate * _trail_mult
                    _adaptive_trail_activation = _atr_estimate * _trail_arm_atr
                    _adaptive_applied = True

                    log.info(
                        f"[ADAPTIVE] {signal.symbol} {side}: base_mult={_base_mult} "
                        f"→ {_adap_mult} (sl_dist {_sl_distance_old:.4f}→{_sl_distance_new:.4f}), "
                        f"TP@{_target_rr}R={tp:.4f}, trail {_trail_mult}x arms@{_trail_arm_atr}ATR"
                    )

        if sl is not None:
            inverted = (side == "long" and sl > price) or (side == "short" and sl < price)
            if inverted:
                # Preserve the SL distance % and reapply to execution price
                signal_price = (sl + tp) / 2 if tp else price  # estimate original signal price
                sl_pct = abs(sl - signal_price) / signal_price if signal_price else 0.015
                if side == "long":
                    sl = price * (1 - sl_pct)
                else:
                    sl = price * (1 + sl_pct)
                # Also fix TP
                if tp is not None:
                    tp_pct = abs(tp - signal_price) / signal_price if signal_price else 0.03
                    if side == "long":
                        tp = price * (1 + tp_pct)
                    else:
                        tp = price * (1 - tp_pct)
                log.info(f"Fixed {signal.symbol} {side} SL/TP: price drifted, recalculated from exec price {price:.4f} (SL={sl if sl is not None else 'None'}, TP={tp if tp is not None else 'None'})")

        # Clamp SL to max 3.5% from entry price
        MAX_SL_PCT = 0.035
        if sl is not None:
            if side == "long":
                min_sl = price * (1 - MAX_SL_PCT)
                if sl < min_sl:
                    log.info(f"Clamping {signal.symbol} long SL from {sl:.4f} to {min_sl:.4f} (max {MAX_SL_PCT*100}%)")
                    sl = min_sl
            else:
                max_sl = price * (1 + MAX_SL_PCT)
                if sl > max_sl:
                    log.info(f"Clamping {signal.symbol} short SL from {sl:.4f} to {max_sl:.4f} (max {MAX_SL_PCT*100}%)")
                    sl = max_sl

        position_data = {
            "side": side,
            "size": size,
            "size_usd": size_usd,
            "entry_price": price,
            "stop_loss": sl,
            "take_profit": tp,
            "strategy": signal.strategy_name,
            "opened_at": datetime.now().isoformat(),
        }

        # Trailing stop: store ATR offset and best price for trail tracking
        # Matches TV behavior: trail_points = activation distance, trail_offset = trail distance
        # Both are set to atr * trailATR in TV, so activation = offset
        if signal.trail_atr_mult is not None and signal.trail_atr_mult > 0:
            if _adaptive_applied and _adaptive_trail_offset and _adaptive_trail_activation:
                # Adaptive path: trail arms only after price earns `trail_arm_atr`
                # ATRs of profit, then follows by `trail_mult` ATRs.
                trail_offset = _adaptive_trail_offset
                trail_activation = _adaptive_trail_activation
            else:
                trail_offset = getattr(signal, '_trail_offset_value', None)
                if trail_offset is None:
                    # Estimate from SL distance if no explicit offset
                    if sl is not None:
                        trail_offset = abs(price - sl)
                    else:
                        trail_offset = price * 0.01  # fallback 1%
                trail_activation = trail_offset  # legacy: TV trail_points = trail_offset
            position_data["trail_atr_mult"] = signal.trail_atr_mult
            position_data["trail_offset"] = trail_offset
            position_data["trail_activation"] = trail_activation
            position_data["trail_active"] = False  # Not active until price moves enough
            position_data["best_price"] = price
            log.info(
                f"[PAPER] Trailing stop enabled for {signal.symbol}: "
                f"offset={trail_offset:.4f}, activation={trail_activation:.4f}"
                + (" (adaptive)" if _adaptive_applied else "")
            )

        pos_id = self._next_pos_id(signal.symbol)
        self.positions[pos_id] = position_data

        # Shared SL/TP: match TV behavior — strategy.exit replaces exit for ALL
        # Pyramid entries: each layer keeps its OWN SL/trail state independently.
        # Don't overwrite existing positions — they have their own trail tracking.
        existing = [(pid, pos) for pid, pos in self.positions.items()
                    if self._base_symbol(pid) == signal.symbol and pid != pos_id]
        if existing:
            log.info(
                f"[PAPER] Pyramid: {len(existing)} existing {signal.symbol} positions keep their own SL/trail"
                f" to SL={sl}, TP={tp}"
            )

        record = TradeRecord(
            timestamp=datetime.now(),
            symbol=signal.symbol,
            side=side,
            size=size,
            price=price,
            strategy=signal.strategy_name,
            paper=True,
        )
        self.trade_history.append(record)
        self._save_state()

        from utils.trade_log import log_event
        layer = len(existing) + 1 if existing else 1
        log_event(
            action="add_layer" if existing else "entry",
            symbol=signal.symbol,
            side=side,
            price=price,
            size=size,
            size_usd=size_usd,
            strategy=signal.strategy_name,
            paper=True,
            pyramid_layer=layer,
            stop_price=sl,
            tp_price=tp,
            atr_stop_mult=getattr(signal, "atr_stop_mult", None),
            confidence=signal.confidence,
            equity_after=self.balance,
        )

        log.info(
            f"[PAPER] Opened {side} {signal.symbol}: size={size:.4f} @ {price:.2f} "
            f"(SL={sl}, TP={tp}, conf={signal.confidence:.2f})"
        )
        return record

    def _close_position(self, signal: Signal, price: float, side: str,
                        exit_reason: str = "signal") -> Optional[TradeRecord]:
        """Close ALL positions for a symbol matching side (matches TV strategy.close behavior)."""
        matching = [(pid, pos) for pid, pos in self.positions.items()
                    if self._base_symbol(pid) == signal.symbol and pos["side"] == side]
        if not matching:
            log.warning(f"[PAPER] No {side} position to close for {signal.symbol}")
            return None

        total_pnl = 0.0
        total_size = 0.0
        now = datetime.now()
        record = None
        for pid, pos in matching:
            if side == "long":
                pnl = (price - pos["entry_price"]) * pos["size"]
            else:
                pnl = (pos["entry_price"] - price) * pos["size"]
            total_pnl += pnl
            total_size += pos["size"]
            del self.positions[pid]

            # One trade record per pyramid layer (matches entry/exit 1:1)
            record = TradeRecord(
                timestamp=now,
                symbol=signal.symbol,
                side=f"close_{side}",
                size=pos["size"],
                price=price,
                strategy=signal.strategy_name,
                pnl=pnl,
                paper=True,
                exit_reason=exit_reason,
            )
            self.trade_history.append(record)

            from utils.trade_log import log_event
            log_event(
                action="close",
                symbol=signal.symbol,
                side=side,
                price=price,
                size=pos["size"],
                size_usd=pos.get("size_usd"),
                strategy=signal.strategy_name,
                paper=True,
                pnl_usd=pnl,
                exit_reason=exit_reason,
                entry_price=pos.get("entry_price"),
            )

        self.balance += total_pnl
        self._save_state()
        closed_count = len(matching)
        log.info(f"[PAPER] Closed {closed_count} {side} {signal.symbol} @ {price:.2f} | PnL: ${total_pnl:.2f} | reason: {exit_reason}")
        return record

    def _close_single_position(self, pos_id: str, price: float, exit_reason: str) -> Optional[TradeRecord]:
        """Close a single position by pos_id. Used by SL/TP checks."""
        pos = self.positions.get(pos_id)
        if not pos:
            return None
        symbol = self._base_symbol(pos_id)
        side = pos["side"]
        if side == "long":
            pnl = (price - pos["entry_price"]) * pos["size"]
        else:
            pnl = (pos["entry_price"] - price) * pos["size"]
        self.balance += pnl
        del self.positions[pos_id]
        record = TradeRecord(
            timestamp=datetime.now(),
            symbol=symbol,
            side=f"close_{side}",
            size=pos["size"],
            price=price,
            strategy=pos.get("strategy", "unknown"),
            pnl=pnl,
            paper=True,
            exit_reason=exit_reason,
        )
        self.trade_history.append(record)
        self._save_state()

        from utils.trade_log import log_event
        log_event(
            action="stop_hit" if exit_reason in ("stop_loss", "trail") else "close",
            symbol=symbol,
            side=side,
            price=price,
            size=pos["size"],
            size_usd=pos.get("size_usd"),
            strategy=pos.get("strategy"),
            paper=True,
            pnl_usd=pnl,
            exit_reason=exit_reason,
            entry_price=pos.get("entry_price"),
            equity_after=self.balance,
        )

        log.info(f"[PAPER] Closed {pos_id} {side} @ {price:.2f} | PnL: ${pnl:.2f} | reason: {exit_reason}")
        return record

    def check_sl_tp(self, current_prices: dict[str, float],
                    candle_highs: dict[str, float] = None,
                    candle_lows: dict[str, float] = None) -> list[TradeRecord]:
        """Check all paper positions for SL/TP hits. Returns list of triggered trades.

        When candle_highs/candle_lows are provided (at candle close), uses them to match
        TV behavior: SL checked against candle low (longs) or high (shorts), TP checked
        against candle high (longs) or low (shorts), trail best_price updated from high/low.
        When not provided (inter-candle polling), uses mid-price as fallback.
        """
        triggered = []
        candle_highs = candle_highs or {}
        candle_lows = candle_lows or {}

        # Iterate over a copy since we modify self.positions
        for pos_id, pos in list(self.positions.items()):
            symbol = self._base_symbol(pos_id)
            price = current_prices.get(symbol)
            if not price:
                log.debug(f"[PAPER] No price for {symbol} — skipping SL/TP check")
                continue

            side = pos["side"]
            sl = pos.get("stop_loss")
            tp = pos.get("take_profit")

            # Use live mid price for all checks — no candle high/low inflation
            high = price
            low = price

            # Trailing stop: activate after trail_points distance, then trail at trail_offset
            trail_offset = pos.get("trail_offset")
            if trail_offset is not None:
                entry = pos["entry_price"]
                activation = pos.get("trail_activation", 0)
                trail_active = pos.get("trail_active", False)

                # Check activation using candle high (long) or low (short)
                if not trail_active:
                    if side == "long" and high >= entry + activation:
                        trail_active = True
                        pos["trail_active"] = True
                        pos["best_price"] = high
                        # Set initial trail SL at activation
                        new_sl = high - trail_offset
                        if sl is None or new_sl > sl:
                            pos["stop_loss"] = new_sl
                            sl = new_sl
                        log.info(f"[PAPER] Trail ACTIVATED {pos_id}: high={high:.4f} >= {entry + activation:.4f}, SL→{sl:.4f}")
                    elif side == "short" and low <= entry - activation:
                        trail_active = True
                        pos["trail_active"] = True
                        pos["best_price"] = low
                        # Set initial trail SL at activation
                        new_sl = low + trail_offset
                        if sl is None or new_sl < sl:
                            pos["stop_loss"] = new_sl
                            sl = new_sl
                        log.info(f"[PAPER] Trail ACTIVATED {pos_id}: low={low:.4f} <= {entry - activation:.4f}, SL→{sl:.4f}")

                if trail_active:
                    best = pos.get("best_price", entry)
                    if side == "long" and high > best:
                        pos["best_price"] = high
                        new_sl = high - trail_offset
                        if sl is None or new_sl > sl:
                            pos["stop_loss"] = new_sl
                            sl = new_sl
                            log.debug(f"[PAPER] Trail moved {pos_id} long SL up to {new_sl:.4f} (best={high:.4f})")
                    elif side == "short" and low < best:
                        pos["best_price"] = low
                        new_sl = low + trail_offset
                        if sl is None or new_sl < sl:
                            pos["stop_loss"] = new_sl
                            sl = new_sl
                            log.debug(f"[PAPER] Trail moved {pos_id} short SL down to {new_sl:.4f} (best={low:.4f})")
                self._save_state()

            log.debug(
                f"[PAPER] SL/TP check {pos_id} {side}: price={price:.4f}, "
                f"high={high:.4f}, low={low:.4f}, SL={sl}, TP={tp}, entry={pos['entry_price']:.4f}"
            )

            # Check stop loss — use candle low for longs, candle high for shorts (TV behavior)
            if sl is not None:
                sl_check_price = low if side == "long" else high
                hit = (side == "long" and sl_check_price <= sl) or (side == "short" and sl_check_price >= sl)
                if hit:
                    # Close at SL price (not candle low) — matches TV fill behavior
                    record = self._close_single_position(pos_id, sl, exit_reason="stop_loss")
                    if record:
                        triggered.append(record)
                        log.warning(f"[PAPER] STOP LOSS triggered: {pos_id} @ {sl:.4f} (candle {'low' if side == 'long' else 'high'}={sl_check_price:.4f})")
                    continue

            # Check take profit — use candle high for longs, candle low for shorts (TV behavior)
            if tp is not None:
                tp_check_price = high if side == "long" else low
                hit = (side == "long" and tp_check_price >= tp) or (side == "short" and tp_check_price <= tp)
                if hit:
                    # Close at TP price (not candle high) — matches TV fill behavior
                    record = self._close_single_position(pos_id, tp, exit_reason="take_profit")
                    if record:
                        triggered.append(record)
                        log.info(f"[PAPER] TAKE PROFIT triggered: {pos_id} @ {tp:.4f} (candle {'high' if side == 'long' else 'low'}={tp_check_price:.4f})")
                    continue

        return triggered

    def get_account_state(self, current_prices: dict = None) -> dict:
        """Return paper account state in HyperLiquid format."""
        positions = []
        for pos_id, pos in self.positions.items():
            symbol = self._base_symbol(pos_id)
            szi = pos["size"] if pos["side"] == "long" else -pos["size"]
            # Compute unrealized PnL if current prices available
            upnl = 0.0
            if current_prices and symbol in current_prices:
                cur_price = current_prices[symbol]
                entry = pos["entry_price"]
                size = pos["size"]
                if pos["side"] == "long":
                    upnl = (cur_price - entry) * size
                else:
                    upnl = (entry - cur_price) * size
            positions.append({
                "position": {
                    "coin": pos_id,
                    "symbol": symbol,
                    "szi": str(szi),
                    "entryPx": str(pos["entry_price"]),
                    "unrealizedPnl": str(upnl),
                    "openedAt": pos.get("opened_at", ""),
                }
            })
        return {
            "marginSummary": {"accountValue": str(self.balance)},
            "assetPositions": positions,
        }


class LiveTrader:
    """Executes orders on HyperLiquid."""

    def __init__(self, config: BotConfig):
        self.config = config
        base_url = constants.TESTNET_API_URL if config.testnet else constants.MAINNET_API_URL
        wallet = Account.from_key(config.private_key)
        self.exchange = Exchange(wallet, base_url)
        self.info = Info(base_url, skip_ws=True)
        self.trade_history: list[TradeRecord] = []
        # Trail tracking state per symbol (mirrors paper trader logic)
        self._trail_state: dict[str, dict] = {}
        self._load_state()
        self._set_leverage()

    def _save_state(self):
        """Persist live trail state + trade history to disk so a crash mid-session
        doesn't wipe trailing-stop ratchets or lose trade records. Balances and
        positions themselves live on HL — we only mirror what's bot-local."""
        try:
            state = {
                "trail_state": self._trail_state,
                "trade_history": [
                    {
                        "timestamp": t.timestamp.isoformat(),
                        "symbol": t.symbol,
                        "side": t.side,
                        "size": t.size,
                        "price": t.price,
                        "strategy": t.strategy,
                        "pnl": t.pnl,
                        "order_id": t.order_id,
                        "paper": t.paper,
                        "exit_reason": t.exit_reason,
                    }
                    for t in self.trade_history
                ],
            }
            tmp = LIVE_STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, LIVE_STATE_FILE)
        except Exception as e:
            log.error(f"[LIVE] Failed to save live state: {e}")

    def _load_state(self):
        """Load persisted trail state + trade history. Reconcile with HL happens
        separately on startup — this just restores bot-local memory."""
        try:
            if not os.path.exists(LIVE_STATE_FILE):
                return
            with open(LIVE_STATE_FILE) as f:
                state = json.load(f)
            self._trail_state = state.get("trail_state", {}) or {}
            self.trade_history = [
                TradeRecord(
                    timestamp=datetime.fromisoformat(t["timestamp"]),
                    symbol=t["symbol"],
                    side=t["side"],
                    size=t["size"],
                    price=t["price"],
                    strategy=t["strategy"],
                    pnl=t.get("pnl"),
                    order_id=t.get("order_id"),
                    paper=t.get("paper", False),
                    exit_reason=t.get("exit_reason", ""),
                )
                for t in state.get("trade_history", [])
            ]
            log.info(
                f"[LIVE] Loaded state: {len(self._trail_state)} trail entries, "
                f"{len(self.trade_history)} trades"
            )
        except Exception as e:
            log.warning(f"[LIVE] Could not load live state: {e} — starting fresh")
            self._trail_state = {}
            self.trade_history = []

    def _set_leverage(self):
        """Set leverage per instrument — enough for pyramiding, not more."""
        for symbol, instrument in INSTRUMENTS.items():
            try:
                margin_type = "cross" if instrument.is_cross else "isolated"
                self.exchange.update_leverage(
                    instrument.max_leverage, symbol, is_cross=instrument.is_cross
                )
                log.info(f"[LIVE] Set {symbol} leverage to {instrument.max_leverage}x {margin_type}")
            except Exception as e:
                log.warning(f"[LIVE] Could not set leverage for {symbol}: {e}")

    def execute_signal(self, signal: Signal, current_price: float, account_balance: float = 0.0) -> Optional[TradeRecord]:
        try:
            if signal.signal_type == SignalType.LONG:
                return self._market_open(signal, current_price, is_buy=True, account_balance=account_balance)
            elif signal.signal_type == SignalType.SHORT:
                return self._market_open(signal, current_price, is_buy=False, account_balance=account_balance)
            elif signal.signal_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
                return self._market_close(signal, current_price)
        except Exception as e:
            log.error(f"Execution failed for {signal.symbol}: {e}")
            return None

    def _market_open(self, signal: Signal, price: float, is_buy: bool, account_balance: float = 0.0) -> Optional[TradeRecord]:
        instrument = INSTRUMENTS.get(signal.symbol)

        # Dynamic position sizing: % of account × confidence
        if signal.size_usd:
            size_usd = signal.size_usd
        elif account_balance > 0 and instrument:
            size_usd = account_balance * instrument.base_position_pct
        else:
            size_usd = instrument.default_size if instrument else 1000

        # Scale by confidence (min 50% — matches risk gate min_signal_confidence)
        size_usd *= max(signal.confidence, 0.5)

        raw_size = size_usd / price
        # Round down to instrument's lot_size to avoid exchange rejection
        if instrument and instrument.lot_size > 0:
            size = int(raw_size / instrument.lot_size) * instrument.lot_size
        else:
            size = round(raw_size, 4)

        log.info(f"[LIVE] Opening {'long' if is_buy else 'short'} {signal.symbol}: size={size} @ ~{price:.2f}")

        # Use limit order at mid price for maker fees (0.0029% vs 0.0086% taker)
        result = self._limit_or_market(signal.symbol, is_buy, size, price)

        if result.get("status") == "ok":
            side = "long" if is_buy else "short"
            actual_size = self._extract_filled_size(result, size)
            if actual_size > 0 and abs(actual_size - size) / size > 0.01:
                log.warning(
                    f"[LIVE] Partial fill on {signal.symbol}: requested {size}, "
                    f"filled {actual_size}. SL/TP and trail will be sized to actual fill."
                )
            elif actual_size <= 0:
                log.warning(
                    f"[LIVE] Could not determine filled size for {signal.symbol} "
                    f"from response; assuming full requested size {size}. "
                    f"Reconcile against HL positions if SL/TP look wrong."
                )
                actual_size = size
            size = actual_size

            record = TradeRecord(
                timestamp=datetime.now(),
                symbol=signal.symbol,
                side=side,
                size=size,
                price=price,
                strategy=signal.strategy_name,
                order_id=self._extract_order_id(result),
                paper=False,
            )
            self.trade_history.append(record)

            # Fix SL/TP if signal price drifted from execution price
            side_name = "long" if is_buy else "short"
            sl = signal.stop_loss
            tp = signal.take_profit
            if sl is not None:
                inverted = (is_buy and sl > price) or (not is_buy and sl < price)
                if inverted:
                    signal_price = (sl + tp) / 2 if tp else price
                    sl_pct = abs(sl - signal_price) / signal_price if signal_price else 0.015
                    tp_pct = abs(tp - signal_price) / signal_price if tp and signal_price else 0.03
                    if is_buy:
                        sl = price * (1 - sl_pct)
                        if tp is not None:
                            tp = price * (1 + tp_pct)
                    else:
                        sl = price * (1 + sl_pct)
                        if tp is not None:
                            tp = price * (1 - tp_pct)
                    signal.stop_loss = sl
                    signal.take_profit = tp
                    log.info(f"Fixed {signal.symbol} {side_name} SL/TP: price drifted, recalculated from exec price {price:.4f} (SL={sl:.4f}, TP={tp:.4f})")

            # Clamp SL to max 3.5% from entry price
            MAX_SL_PCT = 0.035
            if signal.stop_loss is not None:
                if is_buy:
                    min_sl = price * (1 - MAX_SL_PCT)
                    if signal.stop_loss < min_sl:
                        log.info(f"Clamping {signal.symbol} long SL from {signal.stop_loss:.4f} to {min_sl:.4f} (max {MAX_SL_PCT*100}%)")
                        signal.stop_loss = min_sl
                else:
                    max_sl = price * (1 + MAX_SL_PCT)
                    if signal.stop_loss > max_sl:
                        log.info(f"Clamping {signal.symbol} short SL from {signal.stop_loss:.4f} to {max_sl:.4f} (max {MAX_SL_PCT*100}%)")
                        signal.stop_loss = max_sl

            # Place stop loss / take profit if specified
            self._place_sl_tp(signal, size, is_buy)

            # Store trail state for live trailing stop updates
            trail_offset = getattr(signal, '_trail_offset_value', None)
            if trail_offset is None and signal.trail_atr_mult and signal.stop_loss:
                trail_offset = abs(price - signal.stop_loss)
            if trail_offset and trail_offset > 0:
                self._trail_state[signal.symbol] = {
                    "side": side,
                    "entry_price": price,
                    "best_price": price,
                    "trail_offset": trail_offset,
                    "trail_activation": trail_offset,
                    "trail_active": False,
                    "current_sl": signal.stop_loss,
                    "size": size,
                }
                log.info(f"[LIVE] Trail tracking enabled for {signal.symbol}: offset={trail_offset:.4f}, activation={trail_offset:.4f}")

            from utils.trade_log import log_event
            log_event(
                action="entry",
                symbol=signal.symbol,
                side=side,
                price=price,
                size=size,
                strategy=signal.strategy_name,
                paper=False,
                stop_price=signal.stop_loss,
                tp_price=signal.take_profit,
                atr_stop_mult=getattr(signal, "atr_stop_mult", None),
                signal_price=getattr(signal, "signal_price", None),
                fill_price=price,
            )

            log.info(f"[LIVE] Order filled: {side} {signal.symbol}")
            self._save_state()
            return record
        else:
            log.error(f"[LIVE] Order failed: {result}")
            return None

    @staticmethod
    def _extract_filled_size(result: dict, requested: float) -> float:
        """Return the size HL reports as actually filled. Falls back to `requested`
        when the response shape is unknown (logs warning so partial fills don't
        silently misrepresent position size).
        """
        try:
            statuses = result.get("response", {}).get("data", {}).get("statuses", []) or []
            for status in statuses:
                filled = status.get("filled")
                if filled and "totalSz" in filled:
                    return float(filled["totalSz"])
        except (KeyError, IndexError, TypeError, ValueError):
            pass
        return requested

    @staticmethod
    def _extract_order_id(result: dict) -> str:
        """Safely extract order ID from HyperLiquid API response."""
        try:
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            if statuses:
                return str(statuses[0].get("resting", {}).get("oid", ""))
        except (KeyError, IndexError, TypeError):
            pass
        return ""

    def _market_close(self, signal: Signal, price: float) -> Optional[TradeRecord]:
        log.info(f"[LIVE] Closing position {signal.symbol}")

        # Get current position size
        positions = self.info.user_state(self.config.account_address).get("assetPositions", [])
        pos_size = 0.0
        for pos in positions:
            p = pos.get("position", {})
            if p.get("coin") == signal.symbol:
                pos_size = abs(float(p.get("szi", 0)))
                break

        if pos_size == 0:
            log.warning(f"[LIVE] No position to close for {signal.symbol}")
            return None

        # Use limit order for maker fees
        is_buy = signal.signal_type == SignalType.CLOSE_SHORT  # Buy to close short
        result = self._limit_or_market(signal.symbol, is_buy, pos_size, price, is_close=True)

        if result.get("status") == "ok":
            record = TradeRecord(
                timestamp=datetime.now(),
                symbol=signal.symbol,
                side=signal.signal_type.value,
                size=pos_size,
                price=price,
                strategy=signal.strategy_name,
                paper=False,
            )
            self.trade_history.append(record)
            # Clear trail state on close
            self._trail_state.pop(signal.symbol, None)
            log.info(f"[LIVE] Closed {signal.symbol}")
            self._save_state()
            return record
        else:
            log.error(f"[LIVE] Close failed: {result}")
            return None

    def _limit_or_market(self, symbol: str, is_buy: bool, size: float, price: float, is_close: bool = False, timeout: float = 5.0) -> dict:
        """Place a limit order at mid price for maker fees. Fall back to market if not filled.

        Strategy: place limit at current mid price → wait up to timeout seconds →
        if filled, we pay 0.0029% maker fee instead of 0.0086% taker.
        If not filled, cancel and use market order as fallback.
        """
        instrument = INSTRUMENTS.get(symbol)
        tick = instrument.tick_size if instrument else 0.01

        # Round price to tick size
        limit_price = round(price / tick) * tick

        try:
            # Place limit order
            order_type = {"limit": {"tpsl": "tp"}} if is_close else {"limit": {"tpsl": "sl"}}
            result = self.exchange.order(
                coin=symbol,
                is_buy=is_buy,
                sz=size,
                limit_px=limit_price,
                order_type={"limit": {"tif": "Gtc"}},
                reduce_only=is_close,
            )

            if result.get("status") != "ok":
                log.warning(f"[LIVE] Limit order failed, falling back to market: {result}")
                if is_close:
                    return self.exchange.market_close(coin=symbol, sz=size)
                else:
                    return self.exchange.market_open(coin=symbol, is_buy=is_buy, sz=size)

            oid = self._extract_order_id(result)
            log.info(f"[LIVE] Limit order placed: {symbol} {'buy' if is_buy else 'sell'} {size} @ {limit_price} (oid={oid})")

            # Wait for fill
            filled = False
            start = time.time()
            while time.time() - start < timeout:
                time.sleep(1)
                # Check if order is still open
                open_orders = self.info.open_orders(self.config.account_address)
                still_open = any(str(o.get("oid", "")) == oid for o in open_orders)
                if not still_open:
                    filled = True
                    log.info(f"[LIVE] Limit order filled (maker fee): {symbol}")
                    break

            if not filled:
                # Cancel and use market — but only if cancel is confirmed.
                # If cancel fails we cannot know whether the limit filled or is
                # still resting; placing a market order blindly risks a 2x fill.
                log.info(f"[LIVE] Limit order not filled in {timeout}s, cancelling → market order")
                cancel_ok = False
                try:
                    cancel_result = self.exchange.cancel(coin=symbol, oid=int(oid))
                    cancel_ok = bool(cancel_result) and cancel_result.get("status") == "ok"
                    if not cancel_ok:
                        log.warning(f"[LIVE] Cancel returned non-ok: {cancel_result}")
                except Exception as cancel_err:
                    log.warning(f"[LIVE] Cancel raised: {cancel_err}")

                if not cancel_ok:
                    try:
                        still_open = any(
                            str(o.get("oid", "")) == oid
                            for o in self.info.open_orders(self.config.account_address)
                        )
                    except Exception as check_err:
                        log.error(
                            f"[LIVE] Cancel failed AND open_orders check failed ({check_err}) — "
                            f"refusing to place market fallback to avoid 2x fill. Manual review needed: oid={oid}"
                        )
                        return {"status": "err", "reason": "cancel_uncertain", "oid": oid}

                    if still_open:
                        try:
                            self.exchange.cancel(coin=symbol, oid=int(oid))
                            cancel_ok = True
                        except Exception:
                            log.error(
                                f"[LIVE] Second cancel failed for oid={oid} — "
                                f"refusing to place market fallback. Position status unknown."
                            )
                            return {"status": "err", "reason": "cancel_failed_twice", "oid": oid}
                    else:
                        log.info(f"[LIVE] Limit order {oid} filled between polls, treating as filled")
                        return result

                if is_close:
                    return self.exchange.market_close(coin=symbol, sz=size)
                else:
                    return self.exchange.market_open(coin=symbol, is_buy=is_buy, sz=size)

            return result

        except Exception as e:
            log.warning(f"[LIVE] Limit order error, falling back to market: {e}")
            if is_close:
                return self.exchange.market_close(coin=symbol, sz=size)
            else:
                return self.exchange.market_open(coin=symbol, is_buy=is_buy, sz=size)

    def _place_sl_tp(self, signal: Signal, size: float, is_buy: bool):
        """Place stop loss and take profit orders, then verify they're actually
        resting on HL. Naked positions (entry filled but SL/TP rejected) are
        catastrophic — we surface a loud error instead of trusting the API result.
        """
        try:
            orders = []
            if signal.stop_loss:
                orders.append({
                    "coin": signal.symbol,
                    "is_buy": not is_buy,
                    "sz": size,
                    "limit_px": signal.stop_loss,
                    "order_type": {"trigger": {"triggerPx": signal.stop_loss, "isMarket": True, "tpsl": "sl"}},
                    "reduce_only": True,
                })
            if signal.take_profit:
                orders.append({
                    "coin": signal.symbol,
                    "is_buy": not is_buy,
                    "sz": size,
                    "limit_px": signal.take_profit,
                    "order_type": {"trigger": {"triggerPx": signal.take_profit, "isMarket": True, "tpsl": "tp"}},
                    "reduce_only": True,
                })

            if not orders:
                return

            result = self.exchange.bulk_orders(orders, grouping="normalTpsl")
            if not result or result.get("status") != "ok":
                log.error(
                    f"[LIVE] SL/TP bulk_orders returned non-ok for {signal.symbol}: {result}. "
                    f"Position is NAKED — manual SL placement required immediately."
                )
                return

            try:
                open_orders = self.info.open_orders(self.config.account_address)
                trigger_orders = [
                    o for o in open_orders
                    if o.get("coin") == signal.symbol and o.get("orderType") == "trigger"
                ]
                expected = len(orders)
                if len(trigger_orders) < expected:
                    log.error(
                        f"[LIVE] SL/TP verification FAILED for {signal.symbol}: "
                        f"expected {expected} trigger orders, found {len(trigger_orders)} on HL. "
                        f"Position may be naked or partially protected. bulk_orders result: {result}"
                    )
                else:
                    log.info(
                        f"[LIVE] SL/TP placed and verified for {signal.symbol}: "
                        f"SL={signal.stop_loss}, TP={signal.take_profit} "
                        f"({len(trigger_orders)} trigger orders resting)"
                    )
            except Exception as verify_err:
                log.warning(
                    f"[LIVE] SL/TP placement for {signal.symbol} returned ok but "
                    f"verification query failed ({verify_err}). Trusting the API response."
                )
        except Exception as e:
            log.error(f"Failed to place SL/TP for {signal.symbol}: {e}")

    def update_trailing_stops(self, current_prices: dict[str, float],
                              candle_highs: dict[str, float] = None,
                              candle_lows: dict[str, float] = None):
        """Update trailing stops on HyperLiquid — mirrors paper trader logic.

        Checks each tracked position, ratchets SL in favour direction,
        cancels old SL order and places new one on HL.
        """
        candle_highs = candle_highs or {}
        candle_lows = candle_lows or {}
        dirty = False

        for symbol, state in list(self._trail_state.items()):
            price = current_prices.get(symbol)
            if not price:
                continue

            side = state["side"]
            entry = state["entry_price"]
            trail_offset = state["trail_offset"]
            activation = state["trail_activation"]
            trail_active = state["trail_active"]
            current_sl = state["current_sl"]

            high = candle_highs.get(symbol, price)
            low = candle_lows.get(symbol, price)

            # Activation check
            if not trail_active:
                if side == "long" and high >= entry + activation:
                    trail_active = True
                    state["trail_active"] = True
                    state["best_price"] = high
                    dirty = True
                    new_sl = high - trail_offset
                    if current_sl is None or new_sl > current_sl:
                        self._update_sl_order(symbol, new_sl, state["size"], is_buy=False)
                        state["current_sl"] = new_sl
                    log.info(f"[LIVE] Trail ACTIVATED {symbol}: high={high:.4f} >= {entry + activation:.4f}, SL->{new_sl:.4f}")
                elif side == "short" and low <= entry - activation:
                    trail_active = True
                    state["trail_active"] = True
                    state["best_price"] = low
                    dirty = True
                    new_sl = low + trail_offset
                    if current_sl is None or new_sl < current_sl:
                        self._update_sl_order(symbol, new_sl, state["size"], is_buy=True)
                        state["current_sl"] = new_sl
                    log.info(f"[LIVE] Trail ACTIVATED {symbol}: low={low:.4f} <= {entry - activation:.4f}, SL->{new_sl:.4f}")

            # Ratchet check
            if trail_active:
                best = state.get("best_price", entry)
                if side == "long" and high > best:
                    state["best_price"] = high
                    dirty = True
                    new_sl = high - trail_offset
                    if current_sl is None or new_sl > current_sl:
                        self._update_sl_order(symbol, new_sl, state["size"], is_buy=False)
                        state["current_sl"] = new_sl
                        log.info(f"[LIVE] Trail moved {symbol} long SL up to {new_sl:.4f} (best={high:.4f})")
                elif side == "short" and low < best:
                    state["best_price"] = low
                    dirty = True
                    new_sl = low + trail_offset
                    if current_sl is None or new_sl < current_sl:
                        self._update_sl_order(symbol, new_sl, state["size"], is_buy=True)
                        state["current_sl"] = new_sl
                        log.info(f"[LIVE] Trail moved {symbol} short SL down to {new_sl:.4f} (best={low:.4f})")

        if dirty:
            self._save_state()

    def _update_sl_order(self, symbol: str, new_sl: float, size: float, is_buy: bool):
        """Cancel existing SL and place new one at updated price."""
        try:
            # Cancel all existing trigger orders (SL) for this symbol
            open_orders = self.info.open_orders(self.config.account_address)
            for order in open_orders:
                if order.get("coin") == symbol and order.get("orderType", "") == "trigger":
                    self.exchange.cancel(symbol, order["oid"])

            # Place new SL using same method as initial placement
            instrument = INSTRUMENTS.get(symbol)
            tick = instrument.tick_size if instrument else 0.01
            sl_price = round(new_sl / tick) * tick

            order = {
                "coin": symbol,
                "is_buy": is_buy,
                "sz": size,
                "limit_px": sl_price,
                "order_type": {"trigger": {"triggerPx": sl_price, "isMarket": True, "tpsl": "sl"}},
                "reduce_only": True,
            }
            self.exchange.bulk_orders([order], grouping="normalTpsl")
            log.debug(f"[LIVE] Updated SL order for {symbol}: {sl_price:.4f}")
        except Exception as e:
            log.error(f"[LIVE] Failed to update SL for {symbol}: {e}")

    def get_account_state(self) -> dict:
        return self.info.user_state(self.config.account_address)

    def reconcile_with_hl(self) -> dict:
        """Pull live HL positions on startup and cross-check against local trail state.

        Returns a summary dict — caller decides whether to warn or halt.
        """
        report = {
            "ok": True,
            "hl_positions": [],
            "tracked_only_locally": [],
            "tracked_only_on_hl": [],
            "mismatches": [],
        }
        try:
            state = self.info.user_state(self.config.account_address)
        except Exception as e:
            log.error(f"[LIVE] Reconcile failed — could not query HL user_state: {e}")
            report["ok"] = False
            report["error"] = str(e)
            return report

        hl_positions = {}
        for ap in state.get("assetPositions", []):
            pos = ap.get("position", {}) or {}
            coin = pos.get("coin")
            size = float(pos.get("szi", 0) or 0)
            if coin and size != 0:
                hl_positions[coin] = {
                    "size": size,
                    "side": "long" if size > 0 else "short",
                    "entry_price": float(pos.get("entryPx", 0) or 0),
                    "unrealised_pnl": float(pos.get("unrealizedPnl", 0) or 0),
                }
        report["hl_positions"] = hl_positions

        local_symbols = set(self._trail_state.keys())
        hl_symbols = set(hl_positions.keys())

        for sym in sorted(local_symbols - hl_symbols):
            report["tracked_only_locally"].append(sym)
            log.warning(
                f"[LIVE] Reconcile: {sym} tracked locally (trail state) but not on HL — "
                f"clearing stale trail state."
            )
            self._trail_state.pop(sym, None)

        for sym in sorted(hl_symbols - local_symbols):
            report["tracked_only_on_hl"].append(sym)
            log.warning(
                f"[LIVE] Reconcile: {sym} open on HL ({hl_positions[sym]['side']} "
                f"{hl_positions[sym]['size']}) but no local trail state. "
                f"This position has no bot-managed SL ratchet. Manual review required."
            )

        for sym in sorted(local_symbols & hl_symbols):
            local = self._trail_state[sym]
            hl = hl_positions[sym]
            if local.get("side") != hl["side"]:
                report["mismatches"].append({
                    "symbol": sym, "local_side": local.get("side"), "hl_side": hl["side"],
                })
                log.error(
                    f"[LIVE] Reconcile: {sym} side mismatch — local {local.get('side')} "
                    f"vs HL {hl['side']}. Halting recommended."
                )
            elif abs(float(local.get("size", 0)) - abs(hl["size"])) > 1e-9:
                report["mismatches"].append({
                    "symbol": sym, "local_size": local.get("size"), "hl_size": hl["size"],
                })
                log.warning(
                    f"[LIVE] Reconcile: {sym} size mismatch — local {local.get('size')} "
                    f"vs HL {abs(hl['size'])}. Updating trail state to HL size."
                )
                self._trail_state[sym]["size"] = abs(hl["size"])

        if not (report["tracked_only_locally"] or report["tracked_only_on_hl"] or report["mismatches"]):
            log.info(f"[LIVE] Reconcile clean: {len(hl_symbols)} HL positions match local state")
        return report


class ExecutionEngine:
    """Unified execution interface — routes to paper or live trader."""

    def __init__(self, config: BotConfig):
        self.config = config
        self.paper = PaperTrader()
        self.live = None
        self.is_paper = config.paper_trading

        if not self.is_paper and config.private_key and "your_" not in config.private_key:
            try:
                self.live = LiveTrader(config)
            except Exception as e:
                log.error(f"LiveTrader init failed: {e} — falling back to paper")
                self.is_paper = True

    def execute(self, signal: Signal, current_price: float, account_balance: float = 0.0, close_one: bool = False) -> Optional[TradeRecord]:
        log.debug(
            f"Executing {signal.signal_type.value} {signal.symbol} @ {current_price:.2f} "
            f"(strategy={signal.strategy_name}, confidence={signal.confidence:.2f}, "
            f"SL={signal.stop_loss}, TP={signal.take_profit})"
        )
        if self.is_paper:
            return self.paper.execute_signal(signal, current_price, close_one=close_one)
        elif self.live:
            return self.live.execute_signal(signal, current_price, account_balance=account_balance)
        else:
            log.error("No trader available — check your private key config")
            return None

    def check_sl_tp(self, current_prices: dict[str, float],
                    candle_highs: dict[str, float] = None,
                    candle_lows: dict[str, float] = None) -> list[TradeRecord]:
        """Check SL/TP. Paper: full SL/TP/trail check. Live: update trailing stops."""
        if self.is_paper:
            return self.paper.check_sl_tp(current_prices, candle_highs, candle_lows)
        elif self.live:
            # Live: SL/TP are on the exchange, but trail needs active updating
            self.live.update_trailing_stops(current_prices, candle_highs, candle_lows)
        return []

    def get_account_state(self) -> dict:
        if self.is_paper:
            return self.paper.get_account_state()
        elif self.live:
            return self.live.get_account_state()
        return {}

    def reconcile_with_hl(self) -> dict:
        """Startup reconcile — only meaningful in live mode."""
        if self.is_paper or not self.live:
            return {"ok": True, "skipped": "paper mode"}
        return self.live.reconcile_with_hl()

    def get_open_symbols(self) -> set[str]:
        """Return set of base symbols with open positions."""
        if self.is_paper:
            return {PaperTrader._base_symbol(pid) for pid in self.paper.positions}
        elif self.live:
            try:
                state = self.live.get_account_state()
                positions = state.get("assetPositions", [])
                return {p["position"]["coin"] for p in positions
                        if float(p["position"]["szi"]) != 0}
            except Exception as e:
                log.warning(f"Failed to get live open symbols: {e}")
                return set()
        return set()

    def get_trade_history(self) -> list[TradeRecord]:
        if self.is_paper:
            return self.paper.trade_history
        elif self.live:
            return self.live.trade_history
        return []
