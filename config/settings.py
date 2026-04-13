import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Instrument:
    symbol: str           # HyperLiquid symbol
    name: str             # Human-readable name
    group: str            # Asset group (e.g. "crypto")
    tick_size: float      # Min price increment
    lot_size: float       # Min size increment
    max_leverage: int     # Max leverage allowed by risk gate
    default_size: float   # Default position size in USD (fallback if balance unavailable)
    base_position_pct: float = 0.20  # Max position as % of account (scaled by confidence)
    is_cross: bool = True  # True = cross margin, False = isolated (HL restriction)


# Crypto definitions — HyperLiquid native perps
INSTRUMENTS = {
    "ETH": Instrument(
        symbol="ETH",
        name="Ethereum",
        group="crypto",
        tick_size=0.01,
        lot_size=0.001,
        max_leverage=5,
        default_size=1000.0,
        base_position_pct=0.20,  # Momentum v15: Qty=20%, Pyr=3
    ),
    "HYPE": Instrument(
        symbol="HYPE",
        name="HyperLiquid",
        group="crypto",
        tick_size=0.01,
        lot_size=0.1,
        max_leverage=5,
        default_size=1000.0,
        base_position_pct=0.20,  # Momentum v15: Qty=20%, Pyr=3
    ),
}


@dataclass
class RiskConfig:
    max_portfolio_leverage: float = 5.0         # Allow pyramiding (TV uses up to 3x per instrument)
    max_single_position_pct: float = 1.0       # No per-position cap — pyramiding needs full sizing
    max_group_exposure_pct: float = 5.0        # Disabled — TV has no group exposure limit
    max_daily_loss_pct: float = 0.05           # 5% daily drawdown → kill switch (safety)
    max_daily_trades: int = 999                # Unlimited — let TV strategy control trade frequency
    max_open_positions: int = 8                # ETH 4 + HYPE 4 pyramids
    max_correlation_exposure: float = 1.0      # Disabled — both crypto
    # Per-symbol pyramiding limits (TV pyramiding=3 → max 4 positions)
    max_pyramiding: dict = field(default_factory=lambda: {
        "ETH": 4,      # Momentum v15: pyramiding=3 → max 4 positions
        "HYPE": 4,     # Momentum v15: pyramiding=3 → max 4 positions
    })
    default_max_pyramiding: int = 1  # Default for unlisted symbols
    # Per-symbol drawdown protection — disabled (match TV exactly, no DD protection in Pine Scripts)
    symbol_max_drawdown: dict = field(default_factory=dict)
    # Account-level peak-to-trough drawdown protection
    # Tracks highest account balance ever reached; halts all trading if balance drops X% from peak
    max_account_drawdown_pct: float = 0.15  # 15% from peak → halt everything
    # Trading hours — crypto is 24/7
    paper_trading_start_hour: int = 0
    paper_trading_start_minute: int = 0
    paper_trading_end_hour: int = 23
    paper_trading_end_minute: int = 59
    trading_start_hour: int = 0
    trading_start_minute: int = 0
    trading_end_hour: int = 23
    trading_end_minute: int = 59
    min_trade_cooldown_seconds: int = 0            # No cooldown — match TV backtest
    max_consecutive_losses: int = 5              # Halt after N consecutive losses (manual reset)
    min_signal_confidence: float = 0.3           # Match strategy minimum confidence
    min_hold_seconds: int = 0                      # No min hold — match TV backtest (trailing stop handles Silver exits)
    cross_close_delay_seconds: int = 0               # No cross-close delay — single strategy per instrument


@dataclass
class BotConfig:
    # HyperLiquid
    private_key: str = field(default_factory=lambda: os.getenv("HL_PRIVATE_KEY", ""))
    account_address: str = field(default_factory=lambda: os.getenv("HL_ACCOUNT_ADDRESS", ""))
    testnet: bool = field(default_factory=lambda: os.getenv("HL_TESTNET", "true").lower() == "true")

    # Trading
    instruments: dict = field(default_factory=lambda: INSTRUMENTS)
    risk: RiskConfig = field(default_factory=RiskConfig)
    loop_interval_seconds: int = 30            # Main loop interval — scan every 30s
    candle_interval: str = "5m"                # Default candle timeframe
    lookback_candles: int = 200                # How many candles to fetch for indicators

    # Paper trading
    paper_trading: bool = True                 # Start in paper mode

    # Discord
    discord_webhook_trades: str = field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_TRADES", ""))
    discord_webhook_alerts: str = field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_ALERTS", ""))
    discord_webhook_reports: str = field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_REPORTS", ""))

    # n8n workflow automation
    n8n_webhook_url: str = field(default_factory=lambda: os.getenv("N8N_WEBHOOK_URL", ""))
    n8n_api_key: str = field(default_factory=lambda: os.getenv("N8N_API_KEY", ""))

    # TradingView Webhook
    tv_webhook_secret: str = field(default_factory=lambda: os.getenv("TV_WEBHOOK_SECRET", ""))
    tv_webhook_port: int = 5061
    tv_managed: dict = field(default_factory=lambda: {
        "momentum/ETH": True,
        "momentum/HYPE": True,
    })

    # Logging
    log_level: str = "INFO"


def load_config() -> BotConfig:
    return BotConfig()
