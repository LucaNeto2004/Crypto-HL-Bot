import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOG_DIR = os.path.join(_BASE_DIR, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)

# Cap each log handler so logs/ doesn't grow unbounded
LIVE_LOG_MAX_BYTES = 50 * 1024 * 1024   # 50 MB per file
LIVE_LOG_BACKUPS = 5                     # bot_live.log, .1, ..., .5 → 300 MB total cap

# ANSI color codes
COLORS = {
    "DEBUG":    "\033[36m",   # cyan
    "INFO":     "\033[92m",   # green
    "WARNING":  "\033[93m",   # yellow
    "ERROR":    "\033[91m",   # red
    "CRITICAL": "\033[95m",   # magenta
}
RESET = "\033[0m"
DIM = "\033[2m"


class ColorFormatter(logging.Formatter):
    """Colored console output — ANSI codes, no dependencies."""

    def __init__(self, fmt, datefmt=None):
        super().__init__(fmt, datefmt=datefmt)

    def format(self, record):
        color = COLORS.get(record.levelname, "")
        # Color the level name
        record.levelname_color = f"{color}{record.levelname:<8}{RESET}"
        # Dim the timestamp and module name
        record.asctime_dim = f"{DIM}{self.formatTime(record, self.datefmt)}{RESET}"
        record.name_dim = f"{DIM}{record.name:<12}{RESET}"
        # Format the message
        msg = record.getMessage()
        # Highlight key words in messages
        msg = msg.replace("APPROVED", f"\033[92m\033[1mAPPROVED{RESET}")
        msg = msg.replace("REJECTED", f"\033[91m\033[1mREJECTED{RESET}")
        msg = msg.replace("KILL SWITCH", f"\033[91m\033[1mKILL SWITCH{RESET}")
        msg = msg.replace("ENTRY signal", f"\033[96mENTRY signal{RESET}")
        msg = msg.replace("EXIT signal", f"\033[93mEXIT signal{RESET}")
        msg = msg.replace("[PAPER]", f"\033[33m[PAPER]{RESET}")
        msg = msg.replace("[LIVE]", f"\033[91m\033[1m[LIVE]{RESET}")
        msg = msg.replace("Blocking momentum", f"\033[93m\033[1mBlocking momentum{RESET}")
        msg = msg.replace("Long blocked", f"\033[93m\033[1mLong blocked{RESET}")
        msg = msg.replace("Short blocked", f"\033[93m\033[1mShort blocked{RESET}")
        record.msg_colored = msg

        return f"{record.asctime_dim} | {record.levelname_color} | {record.name_dim} | {record.msg_colored}"


def setup_logger(name: str = "bot", level: str = "") -> logging.Logger:
    # Priority: explicit arg > LOG_LEVEL env var > default INFO
    if not level:
        level = os.environ.get("LOG_LEVEL", "INFO")
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not logger.handlers:
        # Console handler (colored)
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(ColorFormatter(
            "%(asctime)s | %(levelname)-8s | %(name)-12s | %(message)s",
            datefmt="%H:%M:%S",
        ))
        logger.addHandler(console)

        # Daily file handler (no color)
        file_handler = logging.FileHandler(
            os.path.join(_LOG_DIR, f"bot_{datetime.now().strftime('%Y%m%d')}.log")
        )
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)-12s | %(message)s",
        ))
        logger.addHandler(file_handler)

        # Live file handler — rotated to stop unbounded growth
        live_handler = RotatingFileHandler(
            os.path.join(_LOG_DIR, "bot_live.log"),
            maxBytes=LIVE_LOG_MAX_BYTES,
            backupCount=LIVE_LOG_BACKUPS,
        )
        live_handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)-12s | %(message)s",
            datefmt="%H:%M:%S",
        ))
        logger.addHandler(live_handler)

    return logger
