"""Small timestamped error logger shared by Jarvis entry points."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


_RUNTIME_DIR = Path(__file__).resolve().parent.parent / "runtime"
_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
_LOGGER = logging.getLogger("jarvis")
if not _LOGGER.handlers:
    _handler = RotatingFileHandler(
        _RUNTIME_DIR / "jarvis.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _LOGGER.addHandler(_handler)
    _LOGGER.setLevel(logging.ERROR)


def log_error(context, error):
    """Write an unexpected error and its active traceback to jarvis.log."""
    _LOGGER.exception("%s: %s", context, error)
