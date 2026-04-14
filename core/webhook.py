"""
TradingView Webhook Server — Receives TV alerts and executes trades.

TV is the brain (decides WHEN to trade), HL is the hands (executes).
Runs as a Flask server in a daemon thread inside main.py.

Webhook payload format (set as alert_message in Pine Scripts):
{
    "secret": "<shared secret>",
    "strategy": "momentum",
    "symbol": "xyz:GOLD",
    "action": "entry_long|entry_short|exit_long|exit_short|close_all",
    "price": 4530.50,
    "stop_loss": 4520.00,
    "trail_offset": 2.50
}
"""
import json
import threading
import time
from typing import Optional

import pandas as pd
import ta

from flask import Flask, request, jsonify

from strategies.base import Signal, SignalType
from utils.logger import setup_logger

log = setup_logger("webhook")

# ---- Momentum regime gate (mirrors strategies/momentum_v15.py) ----
# Crypto entries can arrive via TV webhook, bypassing momentum_v15.py.evaluate().
# This duplicates the gate check here so webhook signals are also filtered by the
# validated-on-1307-trades regime rule.
REGIME_GATE_ENABLED = True
REGIME_GATE_SHADOW = True  # True = log only, False = actually block
REGIME_GATE_ADX_MIN = 25.0
REGIME_GATE_SLOPE_MIN = 0.002  # 0.2% absolute EMA50 slope over 20 bars


class WebhookServer:
    def __init__(self, bot_ref, port: int = 5051, secret: str = ""):
        self.bot = bot_ref
        self.port = port
        self.secret = secret
        self.app = Flask("tv_webhook")
        # Track TV's position count per symbol — syncs entries/exits so we
        # only close OUR positions, not react to exits for old TV positions we don't have
        self._tv_positions: dict[str, int] = {}
        # Track missed exits — exit arrived but no position to close
        # {symbol: (timestamp, action)} — used to immediately close after entry
        self._missed_exits: dict[str, tuple[float, str]] = {}
        self._setup_routes()

    def _setup_routes(self):
        @self.app.route("/webhook/tv", methods=["POST"])
        def handle_tv_webhook():
            return self._process_webhook()

        @self.app.route("/health", methods=["GET"])
        def health():
            return jsonify({"status": "ok", "positions": len(self.bot.execution.get_open_symbols())})

    def _process_webhook(self):
        """Process incoming TradingView webhook."""
        try:
            data = request.get_json(force=True)
        except Exception:
            log.warning("Webhook: invalid JSON body")
            return jsonify({"error": "invalid JSON"}), 400

        # Validate secret
        if self.secret and data.get("secret") != self.secret:
            log.warning("Webhook: invalid secret")
            return jsonify({"error": "unauthorized"}), 401

        # Validate required fields
        strategy_name = data.get("strategy")
        symbol = data.get("symbol")
        action = data.get("action")

        if not all([strategy_name, symbol, action]):
            log.warning(f"Webhook: missing fields — strategy={strategy_name}, symbol={symbol}, action={action}")
            return jsonify({"error": "missing required fields: strategy, symbol, action"}), 400

        # Validate symbol exists
        if symbol not in self.bot.config.instruments:
            log.warning(f"Webhook: unknown symbol {symbol}")
            return jsonify({"error": f"unknown symbol: {symbol}"}), 400

        # Colored log: green for long/entry, red for short/exit
        GREEN = "\033[92m"
        RED = "\033[91m"
        YELLOW = "\033[93m"
        RESET = "\033[0m"
        is_long = "long" in action
        color = GREEN if is_long else RED
        action_label = f"{color}{action.upper()}{RESET}"
        log.info(f"Webhook: {action_label} {YELLOW}{symbol}{RESET} from {strategy_name} @ {data.get('price', 'N/A')}")

        # Process in background thread — respond to TV immediately to avoid timeout
        import threading
        threading.Thread(
            target=self._process_in_background,
            args=(data.copy(), strategy_name, symbol, action),
            daemon=True,
        ).start()

        return jsonify({"status": "accepted"}), 200

    def _process_in_background(self, data, strategy_name, symbol, action):
        """Process webhook signal in background thread."""
        try:
            with self.bot._signal_lock:
                self._execute_action(data, strategy_name, symbol, action)
        except Exception as e:
            log.error(f"Webhook background processing error: {e}")

    def _execute_action(self, data: dict, strategy_name: str, symbol: str, action: str) -> dict:
        """Convert webhook payload to Signal and execute."""

        # Get real-time price from HL (fast orderbook mid, ~0.5s)
        current_price = self.bot.data.fetch_quick_price(symbol)
        if not current_price:
            log.warning(f"Webhook: HL price unavailable for {symbol} — skipping (no OANDA fallback)")
            return {"status": "error", "reason": "HL price unavailable"}
        current_price = float(current_price)

        # Entry signals
        if action in ("entry_long", "entry_short"):
            signal_type = SignalType.LONG if action == "entry_long" else SignalType.SHORT
            entry_side = "long" if action == "entry_long" else "short"
            opposite_side = "short" if entry_side == "long" else "long"

            # Store position_id from TV (v13+) for exact matching
            position_id = data.get("position_id")
            if position_id:
                self._tv_positions[f"{symbol}:{position_id}"] = "open"
                log.info(f"Webhook: entry {symbol} position_id={position_id}")

            # Auto-close opposite position if we have one
            # TV sometimes sends entry without explicit exit (or exit was missed/skipped)
            open_symbols = self.bot.execution.get_open_symbols()
            if symbol in open_symbols:
                # Check if existing position is opposite direction
                has_opposite = False
                if self.bot.config.paper_trading and hasattr(self.bot.execution, 'paper'):
                    for pid, pos in self.bot.execution.paper.positions.items():
                        if pid.split("#")[0] == symbol and pos["side"] == opposite_side:
                            has_opposite = True
                            break
                elif not self.bot.config.paper_trading and self.bot.execution.live:
                    try:
                        state = self.bot.execution.live.get_account_state()
                        for p in state.get("assetPositions", []):
                            if p["position"]["coin"] == symbol:
                                szi = float(p["position"]["szi"])
                                pos_side = "long" if szi > 0 else "short"
                                if pos_side == opposite_side:
                                    has_opposite = True
                                break
                    except Exception:
                        pass

                if has_opposite:
                    log.info(f"Webhook: auto-closing {opposite_side} {symbol} before entering {entry_side}")
                    close_type = SignalType.CLOSE_SHORT if opposite_side == "short" else SignalType.CLOSE_LONG
                    close_signal = Signal(
                        symbol=symbol,
                        signal_type=close_type,
                        strategy_name=strategy_name,
                        confidence=1.0,
                        reason=f"TV webhook: auto-close {opposite_side} before {entry_side}",
                    )
                    self.bot.process_signal(close_signal, current_price, close_one=False)

            # Recalculate SL and trail using HyperLiquid's own ATR (not OANDA's)
            stop_loss = data.get("stop_loss")
            trail_offset = data.get("trail_offset")

            # Compute ATR from HL candles for accurate SL/trail
            hl_atr = None
            try:
                df = self.bot.data.candle_cache.get(symbol)
                if df is not None and len(df) >= 14:
                    highs = df['high'].astype(float)
                    lows = df['low'].astype(float)
                    closes = df['close'].astype(float)
                    tr = pd.concat([
                        highs - lows,
                        (highs - closes.shift(1)).abs(),
                        (lows - closes.shift(1)).abs()
                    ], axis=1).max(axis=1)
                    hl_atr = tr.rolling(14).mean().iloc[-1]
                    log.info(f"Webhook: HL ATR for {symbol}: {hl_atr:.4f}")
            except Exception as e:
                log.warning(f"Webhook: Could not compute HL ATR: {e}")

            # Recalculate SL and trail from HL ATR if available.
            # Per-symbol ATR multipliers matching the crypto Pine Scripts (ETH, HYPE).
            # Was previously hardcoded with commodity (xyz:GOLD/SILVER/BRENTOIL) keys
            # copy-pasted from the commodities-bot fork — fell through to the 0.8 default
            # for every crypto symbol, which is wider than the spec'd 0.7 and divergent
            # from the live Pine Script.
            ATR_STOP_MULTS = {"ETH": 0.7, "HYPE": 0.7}
            ATR_TRAIL_MULTS = {"ETH": 0.3, "HYPE": 0.3}
            if hl_atr is not None and current_price is not None:
                atr_stop_mult = ATR_STOP_MULTS.get(symbol, 0.7)
                trail_atr_mult = ATR_TRAIL_MULTS.get(symbol, 0.3)
                if "long" in action:
                    stop_loss = current_price - hl_atr * atr_stop_mult
                else:
                    stop_loss = current_price + hl_atr * atr_stop_mult
                trail_offset = hl_atr * trail_atr_mult
                log.info(f"Webhook: Recalculated from HL ATR — SL={stop_loss:.2f}, trail={trail_offset:.4f}")

            # ---- Momentum regime gate ----
            # Compute ADX + EMA50 slope inline and either log (shadow) or
            # return early (enforce) when the gate would block this entry.
            if REGIME_GATE_ENABLED and df is not None and len(df) >= 52:
                try:
                    closes_s = df['close'].astype(float)
                    highs_s = df['high'].astype(float)
                    lows_s = df['low'].astype(float)
                    gate_adx = float(
                        ta.trend.ADXIndicator(highs_s, lows_s, closes_s, window=14).adx().iloc[-1]
                    )
                    ema50_s = ta.trend.ema_indicator(closes_s, window=50)
                    if not pd.isna(ema50_s.iloc[-1]) and not pd.isna(ema50_s.iloc[-21]):
                        gate_slope = float((ema50_s.iloc[-1] - ema50_s.iloc[-21]) / ema50_s.iloc[-21])
                    else:
                        gate_slope = 0.0
                    abs_slope = abs(gate_slope)
                    gate_pass = (gate_adx >= REGIME_GATE_ADX_MIN
                                 and abs_slope >= REGIME_GATE_SLOPE_MIN)
                    if not gate_pass:
                        gate_msg = (f"REGIME_GATE {symbol}: blocked — "
                                    f"ADX={gate_adx:.1f} (need >={REGIME_GATE_ADX_MIN}) "
                                    f"|slope|={abs_slope*100:.3f}% (need >={REGIME_GATE_SLOPE_MIN*100:.2f}%)")
                        if REGIME_GATE_SHADOW:
                            log.info(f"SHADOW {gate_msg}  [shadow mode: NOT blocking]")
                        else:
                            log.info(gate_msg)
                            return {"status": "blocked", "reason": "regime_gate"}
                    else:
                        log.debug(f"REGIME_GATE {symbol}: pass — ADX={gate_adx:.1f} |slope|={abs_slope*100:.3f}%")
                except Exception as e:
                    log.warning(f"REGIME_GATE compute error for {symbol}: {e}")

            signal = Signal(
                symbol=symbol,
                signal_type=signal_type,
                strategy_name=strategy_name,
                confidence=1.0,
                stop_loss=float(stop_loss) if stop_loss else None,
                take_profit=None,  # Trail handles exit
                trail_atr_mult=0.5,  # Marker that trail is active
                reason=f"TV webhook: {action}",
            )

            # Tag signal with TV position_id for tracking
            if position_id:
                signal._tv_position_id = position_id

            # Expose base ATR stop multiplier so execution.py can back-calc the
            # ATR and apply the adaptive_stops.json override. Without this the
            # adaptive SL/TP block silently no-ops on every webhook entry.
            if hl_atr is not None and current_price is not None:
                signal.atr_stop_mult = atr_stop_mult

            # Set trail offset from payload (now using HL values)
            if trail_offset:
                signal._trail_offset_value = float(trail_offset)

            return self.bot.process_signal(signal, current_price)

        # Exit signals — IGNORED: bot manages all exits (trail, SL, signal reversal)
        elif action in ("exit_long", "exit_short", "close_all"):
            log.info(f"Webhook: ignoring {action} {symbol} — bot manages exits locally")
            return {"status": "ignored", "reason": "bot manages exits"}

        else:
            log.warning(f"Webhook: unknown action '{action}'")
            return {"status": "error", "reason": f"unknown action: {action}"}

    def start(self):
        """Start webhook server in a daemon thread."""
        if not self.secret:
            log.warning("TV_WEBHOOK_SECRET not set — webhook server disabled")
            return

        thread = threading.Thread(
            target=lambda: self.app.run(host="0.0.0.0", port=self.port, debug=False, use_reloader=False),
            daemon=True,
            name="tv-webhook",
        )
        thread.start()
        log.info(f"TradingView webhook server started on port {self.port}")
