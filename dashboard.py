"""
Mission Control — View-only web dashboard for the HyperLiquid Crypto Bot.
Reads data from the running main.py bot. Does NOT run its own trading loop.
Serves a live dashboard at http://localhost:5055
"""
import json
import os
import time
import threading
import traceback
from datetime import datetime
from typing import Any

from flask import Flask, jsonify, render_template, request

from config.settings import load_config, INSTRUMENTS
from core.data import DataManager
from core.features import FeatureEngine
from core.risk import RiskGate
from core.execution import PaperTrader
from core.audit import AuditJournal
from utils.logger import setup_logger

log = setup_logger("dashboard")

app = Flask(__name__, static_folder="static", template_folder="templates")

# ── Shared state for the web server ──────────────────────────────────────────
state: dict[str, Any] = {
    "bot_status": "monitoring",
    "mode": "",
    "network": "",
    "cycle": 0,
    "last_cycle_time": None,
    "last_error": None,
    # Account
    "balance": 0.0,
    "starting_balance": 0.0,
    "daily_pnl": 0.0,
    "daily_trades": 0,
    "kill_switch": False,
    # Positions & trades
    "positions": {},
    "open_orders": [],
    "trade_history": [],
    # Prices & indicators
    "prices": {},
    "indicators": {},
    # Config
    "instruments": {},
    "risk_config": {},
    "loop_interval": 60,
    # Risk state
    "consecutive_losses": 0,
    "consecutive_loss_halt": False,
    "account_dd_halt": False,
    "account_peak_balance": 0.0,
    "correlations": {},
}


def monitor_loop():
    """Read-only monitor loop — fetches data and indicators but does NOT trade."""
    try:
        config = load_config()
        state["mode"] = "PAPER" if config.paper_trading else "LIVE"
        state["network"] = "TESTNET" if config.testnet else "MAINNET"
        state["loop_interval"] = config.loop_interval_seconds
        state["instruments"] = {
            sym: {"name": inst.name, "group": inst.group, "default_size": inst.default_size}
            for sym, inst in config.instruments.items()
        }
        state["risk_config"] = {
            "max_portfolio_leverage": config.risk.max_portfolio_leverage,
            "max_single_position_pct": f"{config.risk.max_single_position_pct:.0%}",
            "max_group_exposure_pct": f"{config.risk.max_group_exposure_pct:.0%}",
            "max_daily_loss_pct": f"{config.risk.max_daily_loss_pct:.0%}",
            "max_daily_trades": config.risk.max_daily_trades,
            "max_open_positions": config.risk.max_open_positions,
            "min_trade_cooldown": f"{config.risk.min_trade_cooldown_seconds}s",
            "max_consecutive_losses": config.risk.max_consecutive_losses,
            "min_signal_confidence": config.risk.min_signal_confidence,
        }

        data = DataManager(config)
        risk = RiskGate(config)

        state["bot_status"] = "monitoring"

        cycle = 0
        _cached_candles = {}
        _last_candle_fetch = 0  # epoch time of last candle fetch
        _CANDLE_REFRESH = 60    # refresh candles every 60s (they only change every 5min)
        while True:
            try:
                cycle += 1
                state["cycle"] = cycle
                state["last_cycle_time"] = datetime.now().strftime("%H:%M:%S")
                state["last_update_ts"] = datetime.now().timestamp()  # for /health

                # 1. Fetch data & prices
                # Candles only change every 5min — cache them, refresh every 60s
                now_epoch = time.time()
                if not _cached_candles or (now_epoch - _last_candle_fetch) >= _CANDLE_REFRESH:
                    _cached_candles = data.fetch_all_candles()
                    _last_candle_fetch = now_epoch
                candles = _cached_candles
                # Read cached candle prices for all symbols
                data.fetch_mid_prices()
                # Read bot's live prices file (bot writes every 3s when position open)
                # If file is stale (>5s old), fetch our own live price
                try:
                    _price_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "live_prices.json")
                    _paper_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "paper_state.json")
                    with open(_paper_file) as _pf:
                        _pdata = json.load(_pf)
                    _open_syms = list(set(s.split('#')[0] for s in _pdata.get("positions", {}).keys()))

                    if _open_syms:
                        # Try bot's shared file first
                        _used_file = False
                        if os.path.exists(_price_file):
                            _age = time.time() - os.path.getmtime(_price_file)
                            if _age < 5:
                                with open(_price_file) as _pf:
                                    _live = json.load(_pf)
                                data.mid_prices.update(_live)
                                _used_file = True
                        # If file is stale, fetch directly
                        if not _used_file:
                            for sym in _open_syms:
                                try:
                                    p = data.fetch_quick_price(sym)
                                    if p:
                                        data.mid_prices[sym] = p
                                except Exception:
                                    pass
                except Exception:
                    pass
                state["prices"] = dict(data.mid_prices)

                # 2. Compute indicators (read-only) — only when candles refreshed
                _candles_just_refreshed = (now_epoch - _last_candle_fetch) < 2
                if _candles_just_refreshed:
                    feature_engine = FeatureEngine()
                    for symbol, df in candles.items():
                        if df.empty or len(df) < 50:
                            continue
                        # Drop incomplete forming candle to match bot behavior
                        df = df.iloc[:-1] if len(df) > 1 else df
                        df = feature_engine.compute(df, symbol=symbol)
                        latest = df.iloc[-1]
                        state["indicators"][symbol] = {
                            "rsi": round(float(latest.get("rsi", 0)), 1),
                            "adx": round(float(latest.get("adx", 0)), 1),
                            "macd_hist": round(float(latest.get("macd_hist", 0)), 4),
                            "trend": int(latest.get("trend", 0)),
                            "regime": str(latest.get("regime", "?")),
                            "atr_pct": round(float(latest.get("atr_pct", 0)), 2),
                            "bb_width": round(float(latest.get("bb_width", 0)), 4),
                            "ema_9": round(float(latest.get("ema_9", 0)), 2),
                            "ema_21": round(float(latest.get("ema_21", 0)), 2),
                            "stoch_k": round(float(latest.get("stoch_k", 0)), 1),
                            "vol_ratio": round(float(latest.get("vol_ratio", 1)), 2),
                        }

                    # Compute correlations
                    state["correlations"] = feature_engine.compute_correlations(candles)

                # 3. Account state & positions
                paper = PaperTrader()
                state["trade_history"] = [
                    {
                        "time": t.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                        "symbol": t.symbol,
                        "side": t.side,
                        "size": round(t.size, 4),
                        "price": round(t.price, 2),
                        "pnl": round(t.pnl, 2) if t.pnl is not None else None,
                        "strategy": t.strategy,
                        "paper": t.paper,
                        "exit_reason": t.exit_reason or "",
                    }
                    for t in paper.trade_history
                ]

                if config.paper_trading:
                    account_state = paper.get_account_state()
                else:
                    real_account = data.fetch_account_state()
                    has_real_balance = (
                        real_account
                        and float(real_account.get("marginSummary", {}).get("accountValue", 0)) > 0
                    )
                    account_state = real_account if has_real_balance else paper.get_account_state()

                risk.update_portfolio(account_state)

                state["balance"] = risk.portfolio.account_balance
                state["starting_balance"] = risk.portfolio.starting_balance
                state["kill_switch"] = risk.kill_switch
                state["account_dd_halt"] = risk.account_dd_halt
                state["account_peak_balance"] = risk._account_peak_balance

                # Compute consecutive losses from trade history
                consec = 0
                for t in reversed(state["trade_history"]):
                    pnl = t.get("pnl")
                    if pnl is None:
                        continue
                    if pnl < 0:
                        consec += 1
                    else:
                        break
                state["consecutive_losses"] = consec
                # Build enriched positions with SL/TP from paper trader
                enriched_positions = {}
                for pos_id, pos in risk.portfolio.positions.items():
                    symbol = pos.get("symbol", pos_id.split('#')[0])
                    current_price = data.get_current_price(symbol)
                    entry = pos.get("entry_price", 0)
                    side = pos.get("side", "")
                    size = pos.get("size", 0)
                    size_usd = pos.get("size_usd", 0)
                    unrealized_pnl = pos.get("unrealized_pnl", 0)

                    if current_price and entry and unrealized_pnl == 0:
                        if side == "long":
                            unrealized_pnl = (current_price - entry) * size
                        elif side == "short":
                            unrealized_pnl = (entry - current_price) * size

                    pnl_pct = (unrealized_pnl / size_usd * 100) if size_usd else 0.0
                    pp = paper.positions.get(pos_id, {})

                    enriched_positions[pos_id] = {
                        **pos,
                        "symbol": symbol,
                        "current_price": round(current_price, 4) if current_price else None,
                        "unrealized_pnl": round(unrealized_pnl, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "take_profit": round(pp.get("take_profit"), 4) if pp.get("take_profit") else None,
                        "stop_loss": round(pp.get("stop_loss"), 4) if pp.get("stop_loss") else None,
                        "strategy": pp.get("strategy", ""),
                    }

                state["positions"] = enriched_positions

                # Patch SL/TP from paper file as fallback
                try:
                    _paper_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "paper_state.json")
                    with open(_paper_file) as _pf:
                        _pdata = json.load(_pf).get("positions", {})
                    for _pos_id in state["positions"]:
                        if _pos_id in _pdata:
                            if not state["positions"][_pos_id].get("stop_loss"):
                                state["positions"][_pos_id]["stop_loss"] = round(_pdata[_pos_id]["stop_loss"], 4) if _pdata[_pos_id].get("stop_loss") else None
                            if not state["positions"][_pos_id].get("take_profit"):
                                state["positions"][_pos_id]["take_profit"] = round(_pdata[_pos_id]["take_profit"], 4) if _pdata[_pos_id].get("take_profit") else None
                            if not state["positions"][_pos_id].get("strategy"):
                                state["positions"][_pos_id]["strategy"] = _pdata[_pos_id].get("strategy", "")
                except Exception:
                    pass

                # Compute daily PnL and trades
                today_str = datetime.now().strftime("%Y-%m-%d")
                daily_pnl = 0.0
                daily_trades = 0
                for t in state["trade_history"]:
                    if t["time"].startswith(today_str):
                        daily_trades += 1
                        if t["pnl"] is not None:
                            daily_pnl += t["pnl"]
                for pos in enriched_positions.values():
                    daily_pnl += pos.get("unrealized_pnl", 0)
                state["daily_pnl"] = round(daily_pnl, 2)
                state["daily_trades"] = daily_trades

                state["last_error"] = None

            except Exception as e:
                state["last_error"] = str(e)
                log.error(f"Monitor cycle error: {e}", exc_info=True)

            time.sleep(1)  # Dashboard refreshes every 1s

    except Exception as e:
        state["bot_status"] = "error"
        state["last_error"] = str(e)
        log.error(f"Monitor crashed: {e}", exc_info=True)


# ── API Routes ──────────────────────────────────────────────────────────────

@app.route("/api/state")
def api_state():
    return jsonify(state)


@app.route("/health")
def health():
    """Liveness probe for external monitors. Returns 200 if monitor loop has
    updated within the last 30s and bot_status is OK; 503 otherwise.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).timestamp()
    last_update = state.get("last_update_ts") or 0
    age = now - float(last_update) if last_update else None
    bot_status = state.get("bot_status", "unknown")

    healthy = (
        bot_status not in ("error", "crashed")
        and age is not None
        and age < 30
    )

    payload = {
        "status": "ok" if healthy else "stale",
        "bot_status": bot_status,
        "last_update_age_seconds": round(age, 2) if age is not None else None,
        "last_error": state.get("last_error"),
        "open_positions": len(state.get("positions", {}) or {}),
        "balance": state.get("balance"),
    }
    return jsonify(payload), (200 if healthy else 503)


@app.route("/api/close_position", methods=["POST"])
def api_close_position():
    """Manually close an open position by pos_id or all positions for a symbol."""
    try:
        req_data = request.get_json()
        pos_id = req_data.get("pos_id") or req_data.get("symbol")
        if not pos_id:
            return jsonify({"ok": False, "error": "No pos_id or symbol provided"}), 400

        import fcntl
        _paper_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "paper_state.json")

        # Atomic read-modify-write with file lock to prevent bot overwriting
        with open(_paper_file, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            d = json.load(f)

            # Find positions to close
            if pos_id in d["positions"]:
                to_close = [(pos_id, d["positions"][pos_id])]
            else:
                to_close = [(pid, pos) for pid, pos in d["positions"].items()
                            if pid.split('#')[0] == pos_id]

            if not to_close:
                fcntl.flock(f, fcntl.LOCK_UN)
                return jsonify({"ok": False, "error": f"No open position for {pos_id}"}), 404

            base_symbol = to_close[0][0].split('#')[0]
            # Get price — use cached state (always available)
            current_price = (state.get("prices") or {}).get(base_symbol)
            if not current_price:
                fcntl.flock(f, fcntl.LOCK_UN)
                return jsonify({"ok": False, "error": f"No price available for {base_symbol}"}), 400

            total_pnl = 0.0
            for pid, pos in to_close:
                entry = pos["entry_price"]
                size = pos["size"]
                side = pos["side"]
                pnl = round((current_price - entry) * size if side == "long" else (entry - current_price) * size, 2)
                total_pnl += pnl

                d["trade_history"].append({
                    "timestamp": datetime.now().isoformat(),
                    "symbol": base_symbol,
                    "side": f"close_{side}",
                    "price": current_price,
                    "size": size,
                    "pnl": pnl,
                    "strategy": pos.get("strategy", "unknown"),
                    "paper": True,
                    "exit_reason": "manual_close",
                })
                del d["positions"][pid]

            total_pnl = round(total_pnl, 2)
            d["balance"] += total_pnl

            f.seek(0)
            f.truncate()
            json.dump(d, f, indent=2)
            fcntl.flock(f, fcntl.LOCK_UN)

        return jsonify({"ok": True, "symbol": base_symbol, "pnl": total_pnl, "price": current_price, "closed": len(to_close)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── n8n Integration Endpoints ────────────────────────────────────────────────

@app.route("/api/report")
def api_report():
    """Full report for n8n daily/weekly workflows.
    Returns balance, P&L, trade stats, positions, and risk state.
    """
    trades = state.get("trade_history", [])
    won = sum(1 for t in trades if t.get("pnl") and t["pnl"] > 0)
    lost = sum(1 for t in trades if t.get("pnl") and t["pnl"] < 0)
    total_pnl = sum(t["pnl"] for t in trades if t.get("pnl")) if trades else 0
    win_rate = (won / (won + lost) * 100) if (won + lost) > 0 else 0

    # P&L by strategy
    pnl_by_strategy: dict[str, float] = {}
    for t in trades:
        strat = t.get("strategy", "unknown")
        if t.get("pnl"):
            pnl_by_strategy[strat] = pnl_by_strategy.get(strat, 0) + t["pnl"]

    # P&L by symbol
    pnl_by_symbol: dict[str, float] = {}
    for t in trades:
        sym = t.get("symbol", "unknown")
        if t.get("pnl"):
            pnl_by_symbol[sym] = pnl_by_symbol.get(sym, 0) + t["pnl"]

    return jsonify({
        "timestamp": datetime.now().isoformat(),
        "balance": state.get("balance", 0),
        "daily_pnl": state.get("daily_pnl", 0),
        "session_pnl": round(total_pnl, 2),
        "total_trades": len(trades),
        "won": won,
        "lost": lost,
        "win_rate": round(win_rate, 1),
        "open_positions": state.get("positions", {}),
        "pnl_by_strategy": {k: round(v, 2) for k, v in pnl_by_strategy.items()},
        "pnl_by_symbol": {k: round(v, 2) for k, v in pnl_by_symbol.items()},
        "consecutive_losses": state.get("consecutive_losses", 0),
        "consecutive_loss_halt": state.get("consecutive_loss_halt", False),
        "kill_switch": state.get("kill_switch", False),
        "account_dd_halt": state.get("account_dd_halt", False),
        "account_peak_balance": state.get("account_peak_balance", 0.0),
        "mode": state.get("mode", ""),
        "network": state.get("network", ""),
        "cycle": state.get("cycle", 0),
    })


@app.route("/api/attribution")
def api_attribution():
    """Performance attribution — P&L breakdown by strategy, symbol, time period, and exit reason."""
    trades = state.get("trade_history", [])
    pnl_trades = [t for t in trades if t.get("pnl") is not None]

    # Optional date filter: ?date=today
    date_filter = request.args.get("date", "all")
    if date_filter == "today":
        today_str = datetime.now().strftime("%Y-%m-%d")
        pnl_trades = [t for t in pnl_trades if t.get("time", "").startswith(today_str)]

    # By strategy
    by_strategy: dict[str, dict] = {}
    for t in pnl_trades:
        s = t.get("strategy", "unknown")
        if s not in by_strategy:
            by_strategy[s] = {"pnl": 0, "trades": 0, "won": 0, "lost": 0, "gross_profit": 0, "gross_loss": 0}
        by_strategy[s]["trades"] += 1
        by_strategy[s]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            by_strategy[s]["won"] += 1
            by_strategy[s]["gross_profit"] += t["pnl"]
        elif t["pnl"] < 0:
            by_strategy[s]["lost"] += 1
            by_strategy[s]["gross_loss"] += abs(t["pnl"])
    for s in by_strategy:
        d = by_strategy[s]
        d["pnl"] = round(d["pnl"], 2)
        d["gross_profit"] = round(d["gross_profit"], 2)
        d["gross_loss"] = round(d["gross_loss"], 2)
        d["win_rate"] = round(d["won"] / d["trades"] * 100, 1) if d["trades"] else 0
        d["profit_factor"] = round(d["gross_profit"] / d["gross_loss"], 2) if d["gross_loss"] > 0 else 999.99
        d["avg_win"] = round(d["gross_profit"] / d["won"], 2) if d["won"] else 0
        d["avg_loss"] = round(d["gross_loss"] / d["lost"], 2) if d["lost"] else 0

    # By symbol
    by_symbol: dict[str, dict] = {}
    for t in pnl_trades:
        s = t.get("symbol", "unknown")
        if s not in by_symbol:
            by_symbol[s] = {"pnl": 0, "trades": 0, "won": 0, "lost": 0}
        by_symbol[s]["trades"] += 1
        by_symbol[s]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            by_symbol[s]["won"] += 1
        elif t["pnl"] < 0:
            by_symbol[s]["lost"] += 1
    for s in by_symbol:
        by_symbol[s]["pnl"] = round(by_symbol[s]["pnl"], 2)
        by_symbol[s]["win_rate"] = round(by_symbol[s]["won"] / by_symbol[s]["trades"] * 100, 1) if by_symbol[s]["trades"] else 0

    # By exit reason
    by_exit: dict[str, dict] = {}
    for t in pnl_trades:
        reason = t.get("exit_reason", "signal") or "signal"
        if reason not in by_exit:
            by_exit[reason] = {"pnl": 0, "count": 0}
        by_exit[reason]["count"] += 1
        by_exit[reason]["pnl"] += t["pnl"]
    for r in by_exit:
        by_exit[r]["pnl"] = round(by_exit[r]["pnl"], 2)

    # By day
    by_day: dict[str, dict] = {}
    for t in pnl_trades:
        day = t.get("time", "")[:10]
        if day not in by_day:
            by_day[day] = {"pnl": 0, "trades": 0, "won": 0, "lost": 0}
        by_day[day]["trades"] += 1
        by_day[day]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            by_day[day]["won"] += 1
        elif t["pnl"] < 0:
            by_day[day]["lost"] += 1
    for d in by_day:
        by_day[d]["pnl"] = round(by_day[d]["pnl"], 2)

    # Overall stats
    total_pnl = sum(t["pnl"] for t in pnl_trades) if pnl_trades else 0
    gross_profit = sum(t["pnl"] for t in pnl_trades if t["pnl"] > 0)
    gross_loss = sum(abs(t["pnl"]) for t in pnl_trades if t["pnl"] < 0)
    best = max((t["pnl"] for t in pnl_trades), default=0)
    worst = min((t["pnl"] for t in pnl_trades), default=0)

    return jsonify({
        "total_pnl": round(total_pnl, 2),
        "total_trades": len(pnl_trades),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "best_trade": round(best, 2),
        "worst_trade": round(worst, 2),
        "by_strategy": by_strategy,
        "by_symbol": by_symbol,
        "by_exit_reason": by_exit,
        "by_day": dict(sorted(by_day.items())),
    })


@app.route("/api/deployed")
def api_deployed():
    """List deployed strategy configs. Used by n8n to check deployment status."""
    deploy_dir = os.path.join(os.path.dirname(__file__), "config", "deployed")
    deployed = []
    if os.path.isdir(deploy_dir):
        for f in sorted(os.listdir(deploy_dir)):
            if f.endswith(".json"):
                try:
                    with open(os.path.join(deploy_dir, f)) as fp:
                        deployed.append(json.load(fp))
                except Exception:
                    pass
    return jsonify(deployed)


# ── Background jobs for n8n-triggered research ───────────────────────────────

_jobs: dict[str, dict] = {}


@app.route("/api/trigger/research", methods=["POST"])
def api_trigger_research():
    """Trigger a research run (optimization + backtest) from n8n.
    Body (optional): {"symbol": "xyz:GOLD", "auto_deploy": true}
    Returns: {"job_id": "...", "status": "queued"}
    """
    body = request.get_json(silent=True) or {}
    symbol = body.get("symbol")
    auto_deploy = body.get("auto_deploy", False)

    if symbol and symbol not in INSTRUMENTS:
        return jsonify({"error": f"Unknown symbol: {symbol}"}), 400

    import uuid
    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {
        "type": "research",
        "status": "queued",
        "symbol": symbol,
        "auto_deploy": auto_deploy,
        "created_at": datetime.now().isoformat(),
        "result": None,
        "error": None,
    }

    thread = threading.Thread(
        target=_run_research_job,
        args=(job_id, symbol, auto_deploy),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id, "status": "queued"}), 202


@app.route("/api/job/<job_id>")
def api_job(job_id):
    """Poll job status. Used by n8n to wait for research completion."""
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": f"Job {job_id} not found"}), 404
    return jsonify(job)


def _run_research_job(job_id, symbol, auto_deploy):
    """Run research pipeline in background thread.
    Note: auto_deploy is accepted but ignored — results always go to
    pending review. Use /api/research/approve to deploy after review.
    """
    try:
        _jobs[job_id]["status"] = "running"
        import subprocess
        cmd = [
            "python", "-m", "research.run_research",
        ]
        if symbol:
            cmd += ["--symbol", symbol]
        # Never auto-deploy — always save for review
        # Results are saved to data/pending_optimization.json

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            cwd=os.path.dirname(__file__),
        )

        _jobs[job_id]["status"] = "completed" if result.returncode == 0 else "failed"
        _jobs[job_id]["result"] = {
            "stdout": result.stdout[-2000:] if result.stdout else "",
            "stderr": result.stderr[-1000:] if result.stderr else "",
            "returncode": result.returncode,
        }

        # Load pending results summary for the job response
        results_file = os.path.join(os.path.dirname(__file__), "data", "pending_optimization.json")
        if os.path.exists(results_file):
            with open(results_file) as f:
                _jobs[job_id]["pending_results"] = json.load(f)
    except subprocess.TimeoutExpired:
        _jobs[job_id]["status"] = "failed"
        _jobs[job_id]["error"] = "Research timed out after 10 minutes"
    except Exception as e:
        _jobs[job_id]["status"] = "failed"
        _jobs[job_id]["error"] = str(e)
        _jobs[job_id]["traceback"] = traceback.format_exc()
        log.error(f"Research job {job_id} failed: {e}", exc_info=True)


@app.route("/api/research/results")
def api_research_results():
    """Get pending optimization results for review."""
    results_file = os.path.join(os.path.dirname(__file__), "data", "pending_optimization.json")
    if not os.path.exists(results_file):
        return jsonify({"status": "no_results", "results": []})
    try:
        with open(results_file) as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/research/approve", methods=["POST"])
def api_research_approve():
    """Deploy approved optimization results.
    Body: {"approve_all": true} or {"approve": ["mean_reversion:xyz:GOLD", ...]}
    """
    from research.deployer import StrategyDeployer
    from research.optimizer import OptimizationResult
    from research.backtester import BacktestResult

    results_file = os.path.join(os.path.dirname(__file__), "data", "pending_optimization.json")
    if not os.path.exists(results_file):
        return jsonify({"error": "No pending results to approve"}), 404

    with open(results_file) as f:
        pending = json.load(f)

    if pending.get("status") == "deployed":
        return jsonify({"error": "Results already deployed"}), 400

    body = request.get_json(silent=True) or {}
    approve_all = body.get("approve_all", False)
    approve_list = body.get("approve", [])

    deployer = StrategyDeployer(min_grade="B")
    deployed = []
    skipped = []

    for r in pending.get("results", []):
        key = f"{r['strategy']}:{r['symbol']}"

        if not approve_all and key not in approve_list:
            skipped.append(key)
            continue

        if not r.get("deployable"):
            skipped.append(f"{key} (not deployable: {r.get('deploy_reason')})")
            continue

        # Build a minimal OptimizationResult for the deployer
        bt = BacktestResult(
            symbol=r["symbol"],
            strategy_name=r["strategy"],
            _metric_overrides={
                "sharpe": r["sharpe"],
                "win_rate": r["win_rate"],
                "profit_factor": r["profit_factor"],
                "max_drawdown": r["max_drawdown"],
                "num_trades": r["num_trades"],
                "total_return_pct": r["total_return_pct"],
            },
        )

        opt = OptimizationResult(
            symbol=r["symbol"],
            strategy_name=r["strategy"],
            best_params=r["params"],
            best_score=r["score"],
            best_grade=r["grade"],
            best_result=bt,
            all_results=[],
        )
        if deployer.deploy(opt, force=True):
            deployed.append(key)
        else:
            skipped.append(f"{key} (deploy failed)")

    # Mark as deployed
    pending["status"] = "deployed"
    pending["deployed_at"] = datetime.now().isoformat()
    pending["deployed_keys"] = deployed
    with open(results_file, "w") as f:
        json.dump(pending, f, indent=2)

    return jsonify({
        "status": "deployed",
        "deployed": deployed,
        "skipped": skipped,
    })


@app.route("/prototype/charts")
def prototype_charts():
    return render_template("prototype_charts.html")


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    os.makedirs("logs", exist_ok=True)
    os.makedirs("data", exist_ok=True)

    # Start monitor in background thread (read-only, no trading)
    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()
    log.info("Monitor thread started (view-only, no trading)")

    # Start Flask server
    log.info("Dashboard starting at http://localhost:5055")
    app.run(host="0.0.0.0", port=5055, debug=False)
