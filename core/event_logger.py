"""Small in-memory event log for Jarvis backend diagnostics."""

import threading
import time
from datetime import datetime

_MAX_EVENTS = 30
_events_buffer: list[dict[str, str]] = []
_current_state = "STANDBY"
_last_command = "NONE"
_execution_cycles = 0
_state_lock = threading.RLock()


def get_live_state():
    """Return the current in-memory backend state and recent events."""
    with _state_lock:
        return {
            "state": _current_state,
            "last_command": _last_command,
            "cycles": _execution_cycles,
            "events": list(_events_buffer),
            "timestamp": time.time(),
        }


def emit_event(
    cat: str,
    msg: str,
    state: str | None = None,
    last_command: str | None = None,
    inc_cycles: bool = False,
):
    """Record a backend event and update the in-memory assistant state."""
    global _current_state, _last_command, _execution_cycles

    with _state_lock:
        if state:
            _current_state = state
        if last_command:
            _last_command = last_command
        if inc_cycles:
            _execution_cycles += 1

        _events_buffer.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "cat": str(cat).upper(),
            "msg": str(msg),
        })
        if len(_events_buffer) > _MAX_EVENTS:
            del _events_buffer[:-_MAX_EVENTS]


emit_event("SYS", "CORE ENGINE INITIALIZED", state="STANDBY", last_command="IDLE_MONITOR")
