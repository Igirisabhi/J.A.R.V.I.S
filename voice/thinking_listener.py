"""
Interruptible Thinking Listener for Jarvis.

Enables barge-in interruption while Jarvis is thinking (waiting for AI/LLM response).
Monitors both microphone energy and keyboard input during AI generation.
If the user speaks or presses a key while Jarvis is thinking, the thinking process
is immediately aborted and the user's new speech is captured.
Supports unlimited interruptions in a single active session.
"""

from __future__ import annotations

import sys
import time
import wave
import threading
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import sounddevice as sd
from core.audio_devices import start_input_stream

from core.settings import get_settings

_AUDIO_SETTINGS = get_settings().get("audio", {})
SAMPLE_RATE = 16000
BLOCK_SIZE = 1024
ENERGY_THRESHOLD = _AUDIO_SETTINGS.get("energy_threshold", 0.015)
SILENCE_DURATION = _AUDIO_SETTINGS.get("silence_duration_seconds", 0.65)
MIN_SPEECH_TIME = _AUDIO_SETTINGS.get("interrupt_min_speech_seconds", 0.12)
MAX_RECORDING_TIME = _AUDIO_SETTINGS.get("max_recording_seconds", 10.0)
PRE_ROLL_SECONDS = _AUDIO_SETTINGS.get("pre_roll_seconds", 0.35)

INTERRUPT_DIR = Path(__file__).resolve().parent / "interrupts"
INTERRUPT_DIR.mkdir(parents=True, exist_ok=True)


class ThinkingInterruptListener:
    """
    Monitors audio input for speech while an AI query is being processed.
    """

    def __init__(self):
        self.interrupted = False
        self.speech_detected = False
        self.speech_started_at: Optional[float] = None
        self.last_speech_time: Optional[float] = None
        self.audio_chunks: list[np.ndarray] = []
        self.pre_roll_chunks: list[np.ndarray] = []
        self.max_pre_roll = max(1, int(PRE_ROLL_SECONDS * SAMPLE_RATE / BLOCK_SIZE))
        self.running = False
        self.lock = threading.Lock()
        self.stream: Optional[sd.InputStream] = None

    def callback(self, indata, frames, time_info, status):
        if not self.running:
            return

        try:
            from voice.tts import is_speaking
            if is_speaking():
                return
        except Exception:
            pass

        audio = indata[:, 0].copy()
        rms = float(np.sqrt(np.mean(audio * audio)))
        now = time.monotonic()

        self.pre_roll_chunks.append(audio.copy())
        if len(self.pre_roll_chunks) > self.max_pre_roll:
            self.pre_roll_chunks.pop(0)

        live_audio = get_settings()["audio"]
        if rms >= live_audio.get("energy_threshold", ENERGY_THRESHOLD):
            if not self.speech_detected:
                self.speech_detected = True
                self.speech_started_at = now
                with self.lock:
                    for chunk in self.pre_roll_chunks[:-1]:
                        self.audio_chunks.append(chunk.reshape(-1, 1))
                    self.pre_roll_chunks.clear()

            self.last_speech_time = now
            with self.lock:
                self.audio_chunks.append(audio.reshape(-1, 1))

            if self.speech_started_at is not None and (now - self.speech_started_at) >= live_audio.get("interrupt_min_speech_seconds", MIN_SPEECH_TIME):
                self.interrupted = True
        else:
            if self.speech_detected:
                with self.lock:
                    self.audio_chunks.append(audio.reshape(-1, 1))

    def start(self):
        live_audio = get_settings()["audio"]
        self.max_pre_roll = max(1, int(live_audio.get("pre_roll_seconds", PRE_ROLL_SECONDS) * SAMPLE_RATE / BLOCK_SIZE))
        self.running = True
        try:
            self.stream = start_input_stream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=BLOCK_SIZE,
                callback=self.callback,
            )
            return True
        except Exception as e:
            print(f"⚠️ [ThinkingListener] Could not open mic stream: {e}")
            self.running = False
            return False

    def stop(self):
        self.running = False
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def capture_remaining_speech(self) -> Optional[Path]:
        """
        After interrupt is triggered, continue capturing until user stops speaking.
        """
        start = time.monotonic()
        while self.running:
            time.sleep(0.02)
            now = time.monotonic()
            live_audio = get_settings()["audio"]
            if self.last_speech_time and (now - self.last_speech_time >= live_audio.get("interrupt_silence_seconds", SILENCE_DURATION)):
                break
            if self.speech_started_at and (now - self.speech_started_at >= live_audio.get("max_recording_seconds", MAX_RECORDING_TIME)):
                break
            if (now - start) >= 12.0:
                break

        self.stop()

        with self.lock:
            chunks = list(self.audio_chunks)

        if not chunks:
            return None

        try:
            audio = np.concatenate(chunks, axis=0)
            audio = np.clip(audio, -1.0, 1.0)
            audio_int16 = np.int16(audio * 32767)

            target_file = INTERRUPT_DIR / f"thinking_interrupt_{time.time_ns()}.wav"

            with wave.open(str(target_file), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(SAMPLE_RATE)
                wav.writeframes(audio_int16.tobytes())

            return target_file
        except Exception as e:
            print(f"⚠️ [ThinkingListener] Error saving interrupt audio: {e}")
            return None


def route_with_interrupt(
    text: str,
    local_checked: bool = True,
    user_recorded: bool = True,
) -> Tuple[Optional[str], Optional[Path]]:
    """
    Executes route(text) with active barge-in microphone and keyboard interruption.
    Returns (response, None) on success, or (None, interrupted_audio_file) if interrupted.
    """
    from core.router import route
    from core.session_memory import pop_last_user

    ai_result: dict[str, Optional[str]] = {"response": None, "error": None}
    ai_done = threading.Event()
    abort_event = threading.Event()

    def _worker():
        try:
            ai_result["response"] = route(
                text,
                local_checked=local_checked,
                user_recorded=user_recorded,
                abort_event=abort_event,
            )
        except KeyboardInterrupt:
            pass
        except Exception as e:
            ai_result["error"] = str(e)
        finally:
            ai_done.set()

    worker_thread = threading.Thread(target=_worker, name="JarvisAIWorker", daemon=True)
    worker_thread.start()

    listener = ThinkingInterruptListener()
    has_mic = listener.start()

    try:
        while not ai_done.is_set():
            try:
                # Voice-only interrupt: Speech interrupt check
                if has_mic and listener.interrupted:
                    print("\n🛑 INTERRUPTED BY USER SPEECH WHILE THINKING")
                    abort_event.set()
                    pop_last_user()
                    interrupted_file = listener.capture_remaining_speech()
                    return None, interrupted_file

                time.sleep(0.015)
            except KeyboardInterrupt:
                # Ignore Ctrl+C / KeyboardInterrupt during thinking
                continue
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()

    if abort_event.is_set():
        return None, None

    if ai_result["error"]:
        print(f"⚠️ AI error: {ai_result['error']}")
        return "I encountered an error processing that request.", None

    return ai_result["response"], None


def transcribe_with_interrupt(
    audio_source,
) -> Tuple[Optional[str], Optional[Path]]:
    """
    Transcribes audio with active barge-in microphone interruption.
    Returns (transcript, None) on success, or (None, interrupted_audio_file) if interrupted by user speech.
    """
    from voice.stt_groq import transcribe

    stt_result: dict[str, Optional[str]] = {"transcript": None, "error": None}
    stt_done = threading.Event()
    abort_event = threading.Event()

    def _worker():
        try:
            stt_result["transcript"] = transcribe(audio_source)
        except Exception as e:
            stt_result["error"] = str(e)
        finally:
            stt_done.set()

    worker_thread = threading.Thread(target=_worker, name="JarvisSTTWorker", daemon=True)
    worker_thread.start()

    listener = ThinkingInterruptListener()
    has_mic = listener.start()

    try:
        while not stt_done.is_set():
            try:
                if has_mic and listener.interrupted:
                    print("\n🛑 INTERRUPTED BY USER SPEECH WHILE TRANSCRIBING")
                    abort_event.set()
                    interrupted_file = listener.capture_remaining_speech()
                    return None, interrupted_file
                time.sleep(0.015)
            except KeyboardInterrupt:
                continue
    finally:
        listener.stop()

    if abort_event.is_set():
        return None, None

    if stt_result["error"]:
        print(f"⚠️ STT error: {stt_result['error']}")
        return "", None

    return stt_result["transcript"] or "", None

