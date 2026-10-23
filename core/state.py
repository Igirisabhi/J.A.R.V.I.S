"""Small state machine for voice and optional UI state reporting."""

from __future__ import annotations

import threading
import time
from enum import Enum

from core.events import event_bus


class State(str, Enum):
    SLEEPING = "SLEEPING"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    CONFIRMING = "CONFIRMING"
    EXECUTING = "EXECUTING"
    SPEAKING = "SPEAKING"
    IDLE = "IDLE"


class StateMachine:
    def __init__(self):
        self._lock = threading.RLock()
        self._state = State.IDLE
        self._last_activity = time.monotonic()
        self._pending = []
        self._mode = None

    @property
    def state(self):
        with self._lock:
            return self._state

    def transition(self, state: State | str) -> None:
        state = State(state)
        with self._lock:
            previous = self._state
            self._state = state
            self._last_activity = time.monotonic()
        if previous != state:
            event_bus.publish("state_changed", state=state.value, previous=previous.value)

    @property
    def mode(self):
        with self._lock:
            return self._mode

    def set_mode(self, mode: str | None) -> None:
        with self._lock:
            self._mode = (mode or "").strip().lower() or None
        event_bus.publish("mode_changed", mode=self._mode)

    def set_pending(self, actions) -> None:
        with self._lock:
            self._pending = list(actions)
            self._last_activity = time.monotonic()

    def expire(self, idle_timeout_ms: int) -> bool:
        with self._lock:
            expired = bool(self._pending) and (time.monotonic() - self._last_activity) * 1000 >= idle_timeout_ms
            if expired:
                self._pending.clear()
                self._last_activity = time.monotonic()
        if expired:
            event_bus.publish("error", message="Pending command chain expired.")
        return expired


state_machine = StateMachine()
