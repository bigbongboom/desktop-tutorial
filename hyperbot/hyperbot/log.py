"""Logging that stays readable in a terminal and greppable in a file."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_CONFIGURED = False

# Windows consoles choke on non-cp1252 output; the existing Kraken bot in this repo
# hit exactly that. Keep log glyphs ASCII and let notifications carry the colour.
_LEVEL_TAGS = {
    logging.DEBUG: "dbg",
    logging.INFO: "   ",
    logging.WARNING: " ! ",
    logging.ERROR: "!!!",
    logging.CRITICAL: "***",
}


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        tag = _LEVEL_TAGS.get(record.levelno, "   ")
        stamp = self.formatTime(record, "%H:%M:%S")
        name = record.name.replace("hyperbot.", "")
        return f"{stamp} {tag} {name:<18} {record.getMessage()}"


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    # Windows consoles default to cp1252/cp437 and raise UnicodeEncodeError on
    # characters this bot prints ("—", "²", "…"). Same guard as bot/trader.py.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - not a tty, or an old Python; harmless
        pass
    root = logging.getLogger("hyperbot")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(_Formatter())
    root.addHandler(stream)

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")
        )
        root.addHandler(file_handler)

    # These libraries are chatty at DEBUG and say nothing useful.
    for noisy in ("httpx", "httpcore", "websockets", "hyperliquid"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"hyperbot.{name}")
