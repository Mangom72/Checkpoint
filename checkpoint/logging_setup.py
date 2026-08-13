"""Console logging with a compact, thread-aware format."""

from __future__ import annotations

import logging
import sys


class _Formatter(logging.Formatter):
    COLORS = {
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[1;31m",
        "DEBUG": "\033[90m",
    }
    RESET = "\033[0m"

    def __init__(self, color: bool) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if self.color and record.levelname in self.COLORS:
            return f"{self.COLORS[record.levelname]}{text}{self.RESET}"
        return text


def setup_logging(level: str = "INFO", log_filter: logging.Filter | None = None) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_Formatter(color=sys.stderr.isatty()))
    if log_filter is not None:
        handler.addFilter(log_filter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logging.getLogger("urllib3").setLevel(logging.WARNING)
