"""Stage-by-stage timing tracker for Jarvis voice pipeline and command execution."""

import time
from typing import Dict, Optional


class StageTimer:
    def __init__(self):
        self.timings: Dict[str, float] = {}
        self._starts: Dict[str, float] = {}
        self.command_start: Optional[float] = None

    def start_command(self):
        self.timings.clear()
        self._starts.clear()
        self.command_start = time.perf_counter()

    def start(self, stage: str):
        self.timings.pop(stage, None)
        self._starts[stage] = time.perf_counter()

    def stop(self, stage: str) -> float:
        started = self._starts.pop(stage, None)
        if started is not None:
            duration = time.perf_counter() - started
            self.timings[stage] = duration
            return duration
        return 0.0

    def record(self, stage: str, duration: float):
        self.timings[stage] = duration

    def total(self) -> float:
        if self.command_start is not None:
            return time.perf_counter() - self.command_start
        return sum(self.timings.values())

    def print_summary(self):
        stage_names = {
            "recording": "Recording",
            "stt": "Transcription",
            "transcription": "Transcription",
            "correction": "Correction",
            "local_routing": "Local routing",
            "routing": "Local routing",
            "ai": "AI",
            "listening": "Listening",
        }
        for stage, duration in self.timings.items():
            name = stage_names.get(stage, stage.replace("_", " ").title())
            ms = duration * 1000
            try:
                print(f"⏱️ {name}: {ms:.0f} ms ({duration:.2f}s)")
            except UnicodeEncodeError:
                print(f"[TIME] {name}: {ms:.0f} ms ({duration:.2f}s)")
        total_dur = self.total()
        try:
            print(f"⏱️ Total: {total_dur * 1000:.0f} ms ({total_dur:.2f}s)")
        except UnicodeEncodeError:
            print(f"[TIME] Total: {total_dur * 1000:.0f} ms ({total_dur:.2f}s)")



# Global singleton
timer = StageTimer()
