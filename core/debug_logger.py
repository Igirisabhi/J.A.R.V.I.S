"""Timestamped debug logging for the Jarvis voice pipeline."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

_DEBUG_ENABLED = (
    os.getenv("JARVIS_DEBUG", "0") in ("1", "true", "True")
    or os.getenv("JARVIS_VOICE_DEBUG", "0") in ("1", "true", "True")
)


def set_debug_logging(enabled: bool) -> None:
    global _DEBUG_ENABLED
    _DEBUG_ENABLED = enabled


def is_debug_logging() -> bool:
    return _DEBUG_ENABLED


def log_voice_pipeline(stage: str, event: str, **kwargs: Any) -> None:
    """
    Temporary / debug-flagged pipeline tracing with timestamps.
    Traces wake word detection, audio capture, VAD, STT, command parsing,
    execution, and state transitions.
    """
    if not _DEBUG_ENABLED:
        return
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    details = ""
    if kwargs:
        details = " | " + ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
    print(f"[VOICE_DEBUG {timestamp}] [{stage.upper()}] {event}{details}", flush=True)
