"""Thread-safe event bus used by the optional Jarvis UI."""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Callable


class EventBus:
    def __init__(self):
        self._lock = threading.RLock()
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)

    def subscribe(self, event: str, callback: Callable) -> Callable:
        with self._lock:
            self._subscribers[event].append(callback)
        return callback

    def unsubscribe(self, event: str, callback: Callable) -> None:
        with self._lock:
            if callback in self._subscribers.get(event, []):
                self._subscribers[event].remove(callback)

    def publish(self, event: str, **payload) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers.get(event, ()))
        if event == "state_changed":
            try:
                from core.debug_logger import log_voice_pipeline
                log_voice_pipeline("STATE", "State changed", state=payload.get("state"), detail=payload.get("detail"))
            except ImportError:
                pass
        for callback in subscribers:
            try:
                callback(dict(payload))
            except Exception as error:
                # UI subscribers must not be able to break voice command routing.
                try:
                    from core.error_log import log_error
                    log_error(f"Event subscriber failed: {event}", error)
                except (ImportError, OSError):
                    pass
                continue

    def has_subscribers(self, event: str) -> bool:
        """Return whether any component is currently listening for an event."""
        with self._lock:
            return bool(self._subscribers.get(event))


event_bus = EventBus()
