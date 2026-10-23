"""Preview the HUD with synthetic events and no microphone or API keys."""

from PySide6.QtCore import QTimer


def start_demo(bridge):
    states = ["SLEEPING", "LISTENING", "THINKING", "CONFIRMING", "EXECUTING", "SPEAKING", "IDLE"]
    samples: list[tuple[str, dict[str, object]]] = [
        ("state_changed", {"state": state}) for state in states
    ]
    samples.extend([
        ("heard", {"text": "open notes.txt then search main"}),
        ("parsed", {"actions": ["open:notes.txt", "search:main"]}),
        ("step_started", {"index": 0}),
        ("step_result", {"index": 0, "success": True, "message": "Opened notes.txt"}),
        ("step_started", {"index": 1}),
        ("step_result", {"index": 1, "success": False, "message": "Rejected: outside workspace"}),
        ("audio_level", {"level": 0.75}),
        ("workspace_changed", {"path": "C:/JarvisWorkspace"}),
        ("error", {"message": "Demo error sample"}),
    ])
    index: dict[str, int] = {"value": 0}
    timer = QTimer(bridge)
    timer.setInterval(1200)
    def next_event():
        name, payload = samples[index["value"] % len(samples)]
        bridge._apply_event(name, payload)
        index["value"] += 1
    timer.timeout.connect(next_event)
    timer.start()
    bridge._demo_timer = timer


if __name__ == "__main__":
    from ui.main import run_ui
    run_ui(debug=True, demo=True)
