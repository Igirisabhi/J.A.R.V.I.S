import os
import re
import sys
import time
import threading
import queue
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import sounddevice as sd
from core.audio_devices import input_stream
import openwakeword
from openwakeword.model import Model
from openwakeword.utils import download_models

ROOT_DIR = Path(__file__).resolve().parent.parent

sys.path.insert(
    0,
    str(ROOT_DIR)
)


import io
import wave
import numpy as np

from voice.recorder import record_command
from voice.stt_groq import transcribe
from voice.stt_cleanup import cleanup_transcription
from voice.tts import speak
from tools.system_tools import handle_command
from core.session_memory import remember_assistant, remember_user
from core.timing import timer
from core.event_logger import emit_event
from core.settings import get_settings
from core.error_log import log_error
from core.events import event_bus
from core.debug_logger import log_voice_pipeline, set_debug_logging


# ============================================================
# CONFIG
# ============================================================

_WAKE_SETTINGS = get_settings()["voice"]
SAMPLE_RATE = 16000  # Required by the openWakeWord model.
BLOCK_SIZE = 1280

WAKE_THRESHOLD = _WAKE_SETTINGS["wake_threshold"]

REQUIRED_DETECTIONS = _WAKE_SETTINGS["required_detections"]

DETECTION_WINDOW = _WAKE_SETTINGS["detection_window_seconds"]

POST_WAKE_SPEECH_WINDOW = _WAKE_SETTINGS.get("post_wake_speech_window", 0.6)
MAX_EMPTY_TRANSCRIPT_RETRIES = 1


SLEEP_PHRASES: set[str] = set()

STOP_PHRASES = {
    "stop",
    "stop it",
    "stop that",
    "stop now",
    "stop talking",
    "stop speaking",
    "shut up",
    "be quiet",
    "silence",
    "quiet",
    "hush",
    "cancel",
    "abort",
    "hold on",
    "stop please",
    "please stop",
    "just stop",
    "jarvis stop",
}


def _clean_control_text(text):
    """Normalize punctuation and an optional spoken wake-name for control intents."""
    cleaned = (text or "").lower().strip()
    cleaned = re.sub(r"[,:;!?]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(
        r"^(?:(?:hey|okay|ok)\s+)?jarvis(?:\s+please)?\b\s*",
        "",
        cleaned,
    )
    cleaned = re.sub(r"^please\s+", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


# ============================================================
# WAKE MODEL
# ============================================================

WAKEWORD_MODEL_NAMES = {
    "hey_jarvis": "hey_jarvis_v0.1",
    "alexa": "alexa_v0.1",
    "hey_mycroft": "hey_mycroft_v0.1",
    "hey_rhasspy": "hey_rhasspy_v0.1",
    "timer": "timer_v0.1",
    "weather": "weather_v0.1",
}


def _normalize_wake_words(words) -> list[str]:
    if not isinstance(words, list):
        words = ["hey_jarvis"]
    selected = list(dict.fromkeys(
        str(word).strip().casefold() for word in words
        if str(word).strip().casefold() in WAKEWORD_MODEL_NAMES
    ))
    return selected or ["hey_jarvis"]


def _load_wake_model(wake_words=None) -> Model:
    """Load the selected bundled ONNX wake models, downloading missing assets."""
    selected = _normalize_wake_words(wake_words)
    paths = []
    for wake_word in selected:
        model_name = WAKEWORD_MODEL_NAMES[wake_word]
        model_path = Path(openwakeword.MODELS[wake_word]["model_path"]).with_suffix(".onnx")
        if not model_path.is_file():
            print(f"[WakeWord] Downloading {wake_word.replace('_', ' ').title()} ONNX model...")
            download_models(model_names=[model_name])
        if not model_path.is_file():
            raise FileNotFoundError(f"Wake-word model not found: {model_path}")
        paths.append(str(model_path))
    return Model(wakeword_models=paths, inference_framework="onnx")


_active_wake_words = _normalize_wake_words(_WAKE_SETTINGS.get("wake_words"))
wake_model = _load_wake_model(_active_wake_words)


# ============================================================
# FOLLOW-ON SPEECH CAPTURE (PRESERVE FIRST UTTERANCE)
# ============================================================

def _capture_follow_on_speech(audio_queue: queue.Queue, initial_chunk=None) -> io.BytesIO | None:
    """
    Check if speech continues immediately after wake word detection.
    If speech is detected, record the full command from the active stream
    and return it as a WAV BytesIO buffer so the first utterance is never swallowed.
    If no follow-on speech is detected within POST_WAKE_SPEECH_WINDOW, return None.
    """
    log_voice_pipeline("WAKE", "Checking for follow-on command speech in active stream")
    audio_settings = get_settings().get("audio", {})
    energy_threshold = audio_settings.get("energy_threshold", 0.015)
    silence_duration = max(0.65, audio_settings.get("silence_duration_seconds", 0.75))
    max_duration = audio_settings.get("max_recording_seconds", 10.0)

    speech_chunks = []
    onset_chunks = []
    if initial_chunk is not None:
        onset_chunks.append(initial_chunk)

    start_wait = time.monotonic()
    speech_detected = False
    speech_started_at = None
    silence_start = None

    while True:
        try:
            chunk = audio_queue.get(timeout=0.08)
        except queue.Empty:
            chunk = None

        now = time.monotonic()

        if chunk is not None:
            chunk_float = chunk.astype("float32") / 32768.0
            volume = float(np.sqrt(np.mean(chunk_float * chunk_float)))
            is_speech = volume >= energy_threshold

            if not speech_detected:
                onset_chunks.append(chunk)
                if len(onset_chunks) > 8:
                    onset_chunks.pop(0)

                if is_speech:
                    speech_detected = True
                    speech_started_at = now
                    speech_chunks.extend(onset_chunks)
                    onset_chunks.clear()
                    silence_start = None
                    log_voice_pipeline("VAD", "Follow-on speech start detected", volume=round(volume, 4))
                else:
                    if now - start_wait >= POST_WAKE_SPEECH_WINDOW:
                        log_voice_pipeline("VAD", "No follow-on speech; user paused after wake word")
                        return None
            else:
                speech_chunks.append(chunk)
                if is_speech:
                    silence_start = None
                elif silence_start is None:
                    silence_start = now

                if silence_start is not None and (now - silence_start >= silence_duration):
                    log_voice_pipeline("VAD", "Follow-on speech end detected (silence timeout)")
                    break

                if speech_started_at is not None and (now - speech_started_at >= max_duration):
                    log_voice_pipeline("VAD", "Follow-on speech max duration reached")
                    break
        else:
            if not speech_detected and now - start_wait >= POST_WAKE_SPEECH_WINDOW:
                log_voice_pipeline("VAD", "No follow-on speech; wait window expired")
                return None
            if speech_detected and silence_start is not None and (now - silence_start >= silence_duration):
                break

    if not speech_chunks:
        return None

    audio = np.concatenate(speech_chunks)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(audio.tobytes())
    buffer.seek(0)
    buffer.name = "command.wav"
    log_voice_pipeline("WAKE", "Follow-on command utterance captured successfully", frames=len(audio))
    return buffer


# ============================================================
# WAIT FOR WAKE WORD
# ============================================================

def wait_for_wake_word():
    global wake_model

    from core.events import event_bus
    wake_word_label = ", ".join(word.replace("_", " ").title() for word in _active_wake_words)
    event_bus.publish("state_changed", state="LISTENING", detail=f"Listening for {wake_word_label}")

    print(
        "\n🎙️ JARVIS IS LISTENING FOR THE WAKE WORD"
    )

    print(f"🎙️ Say a wake word: {wake_word_label}")

    emit_event("VOX", f"WAITING FOR '{wake_word_label.upper()}'", state="LISTENING")


    detections = 0

    first_detection_time = None
    last_mic_error = None
    last_mic_error_log_time = 0.0


    while True:

        try:
            try:
                device_info = sd.query_devices(kind="input")
                print(f"🎙️ Wake microphone: {device_info['name']} (Windows default)")
            except (sd.PortAudioError, ValueError, TypeError) as error:
                print(f"⚠️ Could not identify the wake microphone: {error}")

            audio_queue = queue.Queue(maxsize=8)

            def receive_audio(indata, _frames, _time_info, status):
                if status:
                    print(f"⚠️ Wake microphone stream: {status}")
                audio = indata[:, 0].copy()
                try:
                    audio_queue.put_nowait(audio)
                except queue.Full:
                    # Stay close to live audio if wake inference briefly falls behind.
                    try:
                        audio_queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        audio_queue.put_nowait(audio)
                    except queue.Full:
                        pass

            with input_stream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=BLOCK_SIZE,
                callback=receive_audio,
            ):

                while True:

                    if _MIC_RECONNECT_EVENT.is_set():
                        _MIC_RECONNECT_EVENT.clear()
                        break
                    try:
                        audio = audio_queue.get(timeout=0.25)
                    except queue.Empty:
                        continue
                    level = float((audio.astype("float32") ** 2).mean() ** 0.5) / 32768.0
                    event_bus.publish("audio_level", level=max(0.0, min(1.0, level * 18.0)))

                    prediction = wake_model.predict(audio)
                    settings = get_settings()  # Refresh wake thresholds without restarting the listener.
                    requested_wake_words = _normalize_wake_words(settings.get("voice", {}).get("wake_words"))
                    if requested_wake_words != _active_wake_words:
                        wake_model = _load_wake_model(requested_wake_words)
                        _active_wake_words[:] = requested_wake_words
                        wake_word_label = ", ".join(word.replace("_", " ").title() for word in _active_wake_words)
                        print(f"[WakeWord] Active wake words updated: {wake_word_label}")
                    score = max(
                        (float(prediction.get(WAKEWORD_MODEL_NAMES[word], 0) or 0) for word in _active_wake_words),
                        default=0.0,
                    )


                    if score >= _WAKE_SETTINGS["wake_threshold"]:

                        current_time = (
                            time.time()
                        )


                        if (
                            first_detection_time
                            is None
                        ):

                            first_detection_time = (
                                current_time
                            )

                            detections = 1

                        elif (
                            current_time -
                            first_detection_time
                            <= _WAKE_SETTINGS["detection_window_seconds"]
                        ):

                            detections += 1

                        else:

                            first_detection_time = (
                                current_time
                            )

                            detections = 1


                        print(
                            f"\r🎯 Wake: "
                            f"{score:.2f}"
                            f" | detections: "
                            f"{detections}",
                            end=""
                        )


                        if (
                            detections >=
                            _WAKE_SETTINGS["required_detections"]
                        ):

                            print(
                                "\n\n🔥 "
                                f"{wake_word_label.upper()} DETECTED!"
                            )

                            log_voice_pipeline("WAKE", "Activation detected", score=round(float(score), 3), detections=detections)
                            emit_event("VOX", f"{wake_word_label.upper()} DETECTED", state="PROCESSING")
                            event_bus.publish("state_changed", state="THINKING")

                            # First utterance preservation:
                            # Check if the user spoke the command immediately with the wake word
                            # (e.g. "Hey Jarvis, open Chrome") instead of closing the mic and dropping it.
                            follow_on_audio = _capture_follow_on_speech(
                                audio_queue,
                                initial_chunk=audio,
                            )
                            return follow_on_audio


                    if (
                        first_detection_time
                        is not None
                        and
                        time.time() -
                        first_detection_time
                        > _WAKE_SETTINGS["detection_window_seconds"]
                    ):

                        detections = 0

                        first_detection_time = None


        except Exception as e:

            print(
                f"\n⚠️ Microphone error: {e}"
            )

            print(
                "🔄 Retrying microphone..."
            )
            error_text = str(e)
            if error_text != last_mic_error or time.time() - last_mic_error_log_time >= 30:
                log_error("Wake-word microphone stream failed", e)
                last_mic_error = error_text
                last_mic_error_log_time = time.time()
            event_bus.publish("error", message=f"Microphone unavailable; retrying: {e}")
            event_bus.publish("state_changed", state="ERROR", detail="Microphone unavailable; retrying")

            time.sleep(2)


# ============================================================
# COMMAND CHECKS (SLEEP / EXIT / STOP)
# ============================================================

def should_sleep(text):
    # Saying only the wake name is the explicit standby request. Do not put
    # Jarvis to sleep merely because the last app or Explorer window closed.
    return bool(re.fullmatch(r"(?:(?:hey|okay|ok)\s+)?jarvis(?:\s+please)?[.!]?", (text or "").strip(), re.IGNORECASE))


def should_exit(text):
    """Only the explicit standalone word 'sleep' may terminate Jarvis."""
    if not text:
        return False
    return _clean_control_text(text).strip(".\"'").casefold() == "sleep"


def has_open_user_window():
    """Return whether a visible user file, folder, or app window is open."""
    try:
        from tools.window_tools import _enumerate_top_level_windows, get_window_process_name
        ignored = {"explorer.exe"}  # Explorer desktop/taskbar is not a folder window.
        for hwnd in _enumerate_top_level_windows():
            process = (get_window_process_name(hwnd) or "").lower()
            if process and process not in ignored:
                return True
            if process == "explorer.exe":
                from tools.window_tools import _explorer_locations
                if any(window_hwnd == hwnd for window_hwnd, _, _ in _explorer_locations()):
                    return True
    except Exception as exc:
        print(f"⚠️ Could not check open windows: {exc}")
        return True  # Keep listening if window state could not be determined.
    return False


def should_stop(text):
    if not text:
        return False
    t = _clean_control_text(text).strip(".\"'")
    if t in STOP_PHRASES:
        return True
    return bool(
        re.fullmatch(
            r"^(?:hey\s+)?(?:jarvis\s+)?(?:please\s+)?(?:stop|shut up|be quiet|silence|cancel|abort|hush)(?:\s+(?:talking|speaking|it|that|now|please))?$",
            t,
        )
    )


# ============================================================
# TOOL RESULT SPEECH FILTER
# ============================================================
# Routine window/media actions are executed silently without TTS chatter.
# TTS is reserved for AI responses, important notifications, and errors.

_SILENT_PHRASE_PATTERNS = [
    # Window operations
    r"^(?:window\s+)?(?:maximized|maximised)\.?$",
    r"^(?:maximized|maximise)\s+.+\.?$",
    r"^(?:window\s+)?(?:minimized|minimise)\.?$",
    r"^(?:minimized|minimise)\s+.+\.?$",
    r"^(?:window\s+)?(?:restored|restore)(?:\s+to\s+normal\s+size)?\.?$",
    r"^restored\s+.+\.?$",
    r"^(?:window\s+)?(?:closed|close)\.?$",
    r"^closed\s+.+\.?$",
    r"^switched(?:\s+over)?\s+to\s+.+\.?$",
    r"^switched\s+window\.?$",
    r"^desktop\s+shown\.?$",
    r"^showing\s+desktop\.?$",
    r"^folder\s+window\s+closed\.?$",
    r"^toggled\s+fullscreen\.?$",
    r"^typed\s+.+\.?$",
    r".+\s+is\s+already\s+(?:maximized|minimized|open|in\s+normal\s+window\s+size)\.?$",

    # App & folder launches
    r"^opened\s+.+\.?$",
    r"^opening\s+.+\.?$",
    r"^active\s+folder(?:\s+context)?\s+cleared\.?$",

    # Media playback controls
    r"^media\s+playback\s+(?:toggled|stopped)\.?$",
    r"^(?:playing|paused|stopped)\.?$",
    r"^skipping\s+to\s+(?:the\s+)?next\s+track\.?$",
    r"^going\s+to\s+previous\s+track\.?$",
    r"^playing\s+(?:current|the\s+first)\s+video\.?$",

    # Volume & System controls
    r"^volume\s+set\s+to\s+.+\.?$",
    r"^(?:muted|unmuted)\.?$",
    r"^screenshot\s+saved\.?$",
    r"^screen\s+locked\.?$",
    r"^cleared\s+recycle\s+bin\.?$",

    # Scrolling & Page Navigation
    r"^scrolled\s+(?:down|up|to\s+(?:the\s+)?top|to\s+(?:the\s+)?bottom)(?:\s+a\s+(?:little|bit|lot))?\.?$",
    r"^scrolled\.?$",
    r"^already\s+at\s+the\s+(?:top|bottom)\s+of\s+the\s+page\.?$",
]


def should_speak_tool_result(result: str) -> bool:
    """
    Determine whether a local tool result should be spoken aloud.
    Silent for routine window/media actions (maximize, minimize, restore, etc.).
    Spoken for errors, not found notices, and important info.
    """
    if not result:
        return False

    r = result.strip()
    if not r or r.startswith("__"):
        return False

    lower = r.lower()

    # Always speak errors, warnings, and missing targets so the user gets feedback
    important_keywords = (
        "isn't open",
        "is not open",
        "couldn't",
        "could not",
        "couldn't find",
        "could not find",
        "i couldn't identify",
        "no active window",
        "no video window found",
        "failed",
        "error",
        "not found",
        "unable to",
        "doesn't exist",
        "does not exist",
        "battery",
        "the current time is",
        "the time is",
        "today is",
        "message sent",
        "sent message",
        "unread message",
        "cpu usage",
        "ram usage",
        "memory usage",
    )
    if any(k in lower for k in important_keywords):
        return True

    # Split multi-step sentences and check if all are routine silent actions
    sentences = [s.strip() for s in re.split(r"[.!?]\s*", r) if s.strip()]
    if not sentences:
        return False

    for sentence in sentences:
        s_lower = sentence.lower()
        is_silent = any(
            re.match(pattern, s_lower, re.IGNORECASE)
            for pattern in _SILENT_PHRASE_PATTERNS
        )
        if not is_silent:
            return True

    return False


# ============================================================
# TRANSCRIBE FILE
# ============================================================

def _transcribe_file_with_interrupt_core(audio_file):
    """
    Transcribe audio with barge-in microphone interruption.
    Returns (text, None) on completion, or ("", interrupted_file) if interrupted.
    """
    if audio_file is None:
        return "", None

    try:
        log_voice_pipeline("STT", "Transcribing audio", source=type(audio_file).__name__)
        print(
            "🧠 Transcribing..."
        )

        timer.start("stt")
        stt_start = time.perf_counter()
        try:
            from voice.thinking_listener import transcribe_with_interrupt
            text, interrupted_file = transcribe_with_interrupt(audio_file)
        finally:
            timer.stop("stt")
            stt_duration = time.perf_counter() - stt_start

        if interrupted_file:
            log_voice_pipeline("STT", "Transcription interrupted by user speech", latency_ms=round(stt_duration * 1000, 1))
            return "", interrupted_file

        raw = (text or "").strip()
        log_voice_pipeline(
            "STT",
            "Transcript received",
            raw_text=raw,
            latency_ms=round(stt_duration * 1000, 1),
            is_empty=not bool(raw),
        )

        if raw:
            # Lightweight post-transcription cleanup:
            # fixes common misrecognitions of app names
            # and commands (no LLM call, < 1 ms).
            timer.start("correction")
            try:
                text = cleanup_transcription(raw)
            finally:
                timer.stop("correction")

            cleaned_raw = re.sub(r"[^\w\s]", "", raw.lower()).strip()
            cleaned_text = re.sub(r"[^\w\s]", "", text.lower()).strip()
            if cleaned_raw != cleaned_text and text != raw:
                print(
                    f"🗣️ You said: {raw}"
                )
                print(
                    f"✏️  Corrected: {text}"
                )
            else:
                print(
                    f"🗣️ You said: {text}"
                )

            from core.command_engine import normalize_voice_text
            event_bus.publish("heard", text=raw, normalized=normalize_voice_text(text))

            return text, None

        return "", None

    except Exception as e:
        log_voice_pipeline("STT", "STT error occurred", error=str(e))
        print(
            f"⚠️ STT error: {e}"
        )
        return "", None


def _default_transcribe_file(audio_file):
    text, _ = _transcribe_file_with_interrupt_core(audio_file)
    return text


transcribe_file = _default_transcribe_file


def transcribe_file_with_interrupt(audio_file):
    # If transcribe_file was patched/mocked by a test or caller, honor it directly
    if transcribe_file is not _default_transcribe_file:
        return transcribe_file(audio_file), None
    return _transcribe_file_with_interrupt_core(audio_file)



# ============================================================
# PROCESS TERMINATION & EXIT-SLEEP STATE
# ============================================================
_exit_sleep_primed = False
_exit_sleep_time = 0.0
DEBUG_MODE = False
_MIC_RECONNECT_EVENT = threading.Event()


def terminate_jarvis_processes():
    """Stop audio and close Jarvis without abruptly terminating the UI."""
    print("\n🛑 SHUTTING DOWN JARVIS...")
    # Gesture actions can request shutdown from the camera worker thread.
    # Ask Qt to close its main loop before doing any cleanup that might block.
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
    except ImportError:
        app = None

    if app is not None:
        try:
            from PySide6.QtCore import QMetaObject, Qt
            queued = QMetaObject.invokeMethod(
                app,
                "quit",
                Qt.ConnectionType.QueuedConnection,
            )
            if not queued:
                app.quit()
        except Exception:
            try:
                app.quit()
            except RuntimeError:
                pass

    # sounddevice may wait for an audio callback. Let process shutdown close
    # the stream if that callback is stuck rather than blocking the exit path.
    try:
        threading.Thread(
            target=sd.stop,
            name="JarvisAudioShutdown",
            daemon=True,
        ).start()
    except Exception:
        pass

    # This cleanup has bounded waits and terminates the helper if it does not
    # respond, so it cannot prevent the UI from closing indefinitely.
    try:
        from voice.tts import _stop_tts_worker
        _stop_tts_worker()
    except Exception:
        pass

    if app is not None:
        return

    try:
        jarvis_pid_file = ROOT_DIR / "jarvis.pid"
        jarvis_pid_file.unlink(missing_ok=True)
    except Exception as e:
        print(f"⚠️ Error removing Jarvis PID file: {e}")

    try:
        os._exit(0)
    except Exception:
        sys.exit(0)


# ============================================================
# EXECUTE COMMAND
# ============================================================

def execute_command(initial_text):
    global _exit_sleep_primed

    current_text = initial_text

    while current_text:
        text = current_text
        current_text = None

        if not text:
            return True

        remember_user(text)
        from core.events import event_bus
        event_bus.publish("state_changed", state="THINKING")
        from core.command_engine import parse_action, parse_command_chain, normalize_voice_text
        parsed_actions = parse_command_chain(text)
        normalized = normalize_voice_text(text)
        parsed = parsed_actions or parse_action(text)
        log_voice_pipeline("PARSER", "Raw text in", text=text, normalized=normalized, parsed=str(parsed))
        if DEBUG_MODE:
            print(f"[DEBUG] Heard: {text}")
            print(f"[DEBUG] Normalized: {normalized}")
            print(f"[DEBUG] Parsed: {parsed}")

        # --------------------------------------------------------
        # 0. IMMEDIATE STOP (Interrupt / Stop Speech / Cancel)
        # --------------------------------------------------------

        if should_stop(text):
            log_voice_pipeline("PARSER", "Command matched stop phrase", text=text)
            print(
                "\n🛑 Stop command received."
            )
            emit_event("CMD", "STOP SPEECH REQUESTED", state="LISTENING")
            sd.stop()
            return True

        # --------------------------------------------------------
        # 1. EXPLICIT GOODBYE
        # --------------------------------------------------------

        if should_exit(text):
            log_voice_pipeline("PARSER", "Command matched sleep exit phrase", text=text)
            print(
                "\n👋 Exit assistant command detected. Shutting down cleanly..."
            )
            emit_event("SYS", "EXIT ASSISTANT COMMAND", state="SHUTTING_DOWN")
            sd.stop()
            try:
                speak("Entering sleep mode, sir.")
            except Exception:
                pass
            terminate_jarvis_processes()

        # --------------------------------------------------------
        # 2. SLEEP / STANDBY
        # --------------------------------------------------------

        if should_sleep(text):
            if has_open_user_window():
                log_voice_pipeline("PARSER", "Sleep rejected; user window open")
                print("\n⚠️ Close the open app or folder before putting Jarvis to sleep.")
                speak("An app or folder is still open. Close it first, then say Jarvis again.")
                return True
            log_voice_pipeline("PARSER", "Command matched sleep phrase", text=text)
            print(
                "\n💤 Sleep command detected."
            )
            emit_event("SLEEP", "ENTERING SLEEP MODE", state="SLEEPING")
            _exit_sleep_primed = False
            sd.stop()
            speak(
                "Standing by."
            )
            return False

        # Reset exit primed flag on any normal interactive command
        _exit_sleep_primed = False

        # --------------------------------------------------------
        # LOCAL COMMAND
        # --------------------------------------------------------

        print(
            "🔧 Checking local commands..."
        )
        emit_event("TOOL", f"EVALUATING: {text[:32]}", state="PROCESSING")
        log_voice_pipeline("EXEC", "Execution started", command=text)

        timer.start("local_routing")
        exec_start = time.perf_counter()
        try:
            tool_result = (
                handle_command(
                    text
                )
            )
            if DEBUG_MODE:
                print(f"[DEBUG] Executed: {tool_result}")
        except Exception as e:
            log_error("Local command failed", e)
            print(
                f"⚠️ Tool error: {e}"
            )
            tool_result = "I couldn't process that command."
        finally:
            timer.stop("local_routing")
            exec_duration = time.perf_counter() - exec_start
            log_voice_pipeline("EXEC", "Execution completed", result=str(tool_result)[:64], latency_ms=round(exec_duration * 1000, 1))

        if tool_result is not None:
            timer.print_summary()

            # --------------------------------------------
            # STOP TTS
            # --------------------------------------------

            if tool_result == "__STOP_TTS__":
                print(
                    "🛑 Stop command received."
                )
                emit_event("TOOL", "STOP TTS RECEIVED", state="LISTENING")
                sd.stop()
                return True

            if tool_result == "__GOODBYE_EXIT__":
                print("\n💤 Sleep command detected. Shutting down cleanly...")
                emit_event("SYS", "GOODBYE EXIT", state="SHUTTING_DOWN")
                sd.stop()
                try:
                    speak("Entering sleep mode, sir.")
                except Exception:
                    pass
                terminate_jarvis_processes()

            remember_assistant(tool_result)

            print(
                f"🛠️ Tool: {tool_result}"
            )
            emit_event("TOOL", f"RESULT: {str(tool_result)[:32]}", state="SPEAKING" if should_speak_tool_result(tool_result) else "LISTENING")

            if should_speak_tool_result(tool_result):
                interrupted_file = speak(
                    tool_result
                )
            else:
                interrupted_file = None

            # If user spoke while Jarvis was talking, handle interrupt
            if interrupted_file:
                print(
                    "\n🎙️ User interrupted Jarvis."
                )
                new_text = (
                    transcribe_file(
                        interrupted_file
                    )
                )
                if new_text:
                    if should_exit(new_text):
                        print("\n👋 Exit assistant command detected during interrupt. Shutting down cleanly...")
                        sd.stop()
                        try:
                            speak("Entering sleep mode, sir.")
                        except Exception:
                            pass
                        terminate_jarvis_processes()
                    clean_new = new_text.strip().lower().rstrip(".,!?")
                    if should_stop(new_text) or clean_new in STOP_PHRASES or clean_new in ["stop", "cancel", "nevermind", "never mind", "quiet", "shut up", "wait", "abort", "exit"]:
                        print("🛑 Speech stopped by user.")
                        return True
                    current_text = new_text
                    continue

            return True

        # --------------------------------------------------------
        # AI
        # --------------------------------------------------------

        print(
            "🤖 Sending to AI..."
        )
        emit_event("AI", "THINKING (GROQ LLM)...", state="PROCESSING")

        timer.start("ai")
        try:
            from voice.thinking_listener import route_with_interrupt

            response, thinking_interrupt_file = route_with_interrupt(text, local_checked=True, user_recorded=True)

            timer.stop("ai")
            timer.print_summary()

            # If user interrupted while Jarvis was thinking
            if thinking_interrupt_file:
                print("\n🎙️ User interrupted Jarvis while thinking.")
                emit_event("AI", "INTERRUPTED WHILE THINKING", state="LISTENING")
                new_text = transcribe_file(thinking_interrupt_file)
                if new_text:
                    if should_exit(new_text):
                        print("\n👋 Exit assistant command detected during thinking interrupt. Shutting down cleanly...")
                        sd.stop()
                        try:
                            speak("Entering sleep mode, sir.")
                        except Exception:
                            pass
                        terminate_jarvis_processes()
                    clean_new = new_text.strip().lower().rstrip(".,!?")
                    if should_stop(new_text) or clean_new in STOP_PHRASES or clean_new in ["stop", "cancel", "nevermind", "never mind", "quiet", "shut up", "wait", "abort", "exit"]:
                        print("🛑 Operation cancelled by user.")
                        return True
                    current_text = new_text
                    continue
                return True
            elif response is None:
                # User interrupted via keyboard or thinking cancelled
                print("🛑 Thinking aborted by user.")
                emit_event("AI", "THINKING ABORTED", state="LISTENING")
                return True

            print(
                f"\n🤖 Jarvis: {response}"
            )
            emit_event("AI", f"RESPONSE: {str(response)[:32]}", state="SPEAKING")

            interrupted_file = speak(
                response
            )

            # ----------------------------------------------------
            # USER SPOKE DURING TTS
            # ----------------------------------------------------

            if interrupted_file:
                print(
                    "\n🎙️ User interrupted Jarvis."
                )
                new_text = (
                    transcribe_file(
                        interrupted_file
                    )
                )
                if new_text:
                    if should_exit(new_text):
                        print("\n👋 Exit assistant command detected during interrupt. Shutting down cleanly...")
                        sd.stop()
                        try:
                            speak("Entering sleep mode, sir.")
                        except Exception:
                            pass
                        terminate_jarvis_processes()
                    clean_new = new_text.strip().lower().rstrip(".,!?")
                    if should_stop(new_text) or clean_new in STOP_PHRASES or clean_new in ["stop", "cancel", "nevermind", "never mind", "quiet", "shut up", "wait", "abort", "exit"]:
                        print("🛑 Speech stopped by user.")
                        return True
                    current_text = new_text
                    continue

        except Exception as e:
            print(
                f"⚠️ AI error: {e}"
            )
            speak(
                "I encountered an error "
                "processing that command."
            )

        return True

    return True


# ============================================================
# GET NORMAL COMMAND
# ============================================================

def get_command(max_empty_retries=MAX_EMPTY_TRANSCRIPT_RETRIES):
    from core.events import event_bus
    retries_left = max_empty_retries

    while True:
        event_bus.publish("state_changed", state="LISTENING", detail="Listening for your command")
        emit_event("MIC", "LISTENING FOR SPEECH...", state="LISTENING")

        try:
            audio_file = record_command()
        except Exception as error:
            log_error("Command microphone recording failed", error)
            event_bus.publish("error", message=f"Microphone unavailable: {error}")
            event_bus.publish("state_changed", state="ERROR", detail="Microphone unavailable; check input device and permissions")
            return None

        if audio_file is None:
            return None

        emit_event("STT", "TRANSCRIBING AUDIO...", state="PROCESSING")
        log_voice_pipeline("STT", "Transcribing recorded audio")
        text, interrupted_file = transcribe_file_with_interrupt(
            audio_file
        )

        while interrupted_file:
            print("\n🎙️ User interrupted Jarvis while transcribing.")
            emit_event("STT", "INTERRUPTED WHILE TRANSCRIBING", state="LISTENING")
            text, interrupted_file = transcribe_file_with_interrupt(interrupted_file)
            if text:
                clean_new = text.strip().lower().rstrip(".,!?")
                if should_stop(text) or clean_new in STOP_PHRASES or clean_new in ["stop", "cancel", "nevermind", "never mind", "quiet", "shut up", "wait", "abort"]:
                    print("🛑 Operation cancelled by user.")
                    return ""
                if should_exit(text):
                    print("\n👋 Exit assistant command detected during transcription interrupt. Shutting down cleanly...")
                    sd.stop()
                    try:
                        speak("Entering sleep mode, sir.")
                    except Exception:
                        pass
                    terminate_jarvis_processes()

        if text:
            emit_event("CMD", f"HEARD: \"{text}\"", state="PROCESSING", last_command=text, inc_cycles=True)
            return text

        # Speech was detected by VAD / energy, but STT returned an empty transcript.
        log_voice_pipeline("STT", "Empty transcript for detected speech", retries_left=retries_left)
        if retries_left > 0:
            retries_left -= 1
            log_voice_pipeline("STT", "Re-listening automatically after empty transcript")
            event_bus.publish("state_changed", state="LISTENING", detail="Speech unclear, listening again...")
            emit_event("STT", "EMPTY TRANSCRIPT - RE-LISTENING", state="LISTENING")
            continue

        return ""


# ============================================================
# ACTIVE MODE
# ============================================================

def active_mode(initial_audio=None):

    print(
        "\n================================"
    )

    print(
        "       🟢 JARVIS ACTIVE"
    )

    print(
        "================================"
    )

    print(
        "🎙️ Wake word is NOT required."
    )

    print("💡 Say 'exit' to close the current window. Close all apps and folders, then say 'Jarvis' to stand by.")

    emit_event("SYS", "ACTIVE CONVERSATION MODE", state="LISTENING")

    if initial_audio:
        text, interrupted_file = transcribe_file_with_interrupt(initial_audio)
        while interrupted_file:
            print("\n🎙️ User interrupted Jarvis while transcribing.")
            text, interrupted_file = transcribe_file_with_interrupt(interrupted_file)
        if text:
            keep_active = execute_command(text)
            if not keep_active:
                print(
                    "\n💤 Returning to sleep..."
                )
                return
        else:
            log_voice_pipeline("STT", "Initial audio produced empty transcript; transitioning to active listening")

    while True:

        try:

            print(
                "\n🎙️ Listening..."
            )

            text = get_command()

            if not text:
                continue

            keep_active = (
                execute_command(
                    text
                )
            )

            if not keep_active:

                print(
                    "\n💤 Returning "
                    "to sleep..."
                )

                return

        except KeyboardInterrupt:

            print(
                "\n🛑 Keyboard interrupt ignored (voice-only operation). Use 'stop' or 'sleep'."
            )
            continue


        except Exception as e:
            log_error("Active-mode command failed", e)

            print(
                f"\n⚠️ Active-mode error: "
                f"{e}"
            )

            print(
                "🔄 Staying active..."
            )

            time.sleep(1)


# ============================================================
# SLEEP STANDBY LISTENER (FOLLOWING EXIT COMMAND)
# ============================================================

def exit_sleep_standby(timeout_seconds=25):
    """
    Short standby mode used after explicit sleep command.
    Listens for speech without requiring the wake word:
    - The explicit sleep command puts Jarvis in standby.
    - If user says 'hey jarvis' or wake word: wakes up and returns 'active'.
    - If silence / timeout expires: returns 'deep_sleep' to transition to standard openWakeWord.
    """
    global _exit_sleep_primed
    print(f"\n💤 In sleep mode (standby window {timeout_seconds}s active)...")
    print("💡 Say 'Hey Jarvis' to wake.")
    emit_event("SLEEP", f"STANDBY WINDOW ({timeout_seconds}s)", state="SLEEPING")

    start_t = time.time()
    while time.time() - start_t < timeout_seconds:
        remaining = timeout_seconds - (time.time() - start_t)
        if remaining <= 0:
            break

        audio_file = record_command(timeout=min(remaining, 5.0))
        if not audio_file:
            continue

        text = transcribe_file(audio_file)
        if not text:
            continue

        print(f"🎙️ Heard in sleep standby: '{text}'")

        if should_exit(text):
            print("\n💤 Jarvis remains in standby.")
            emit_event("SLEEP", "GOODBYE WHILE STANDBY", state="SLEEPING")
            sd.stop()
            return "deep_sleep"

        t_clean = text.lower().strip()
        if "jarvis" in t_clean or "hey" in t_clean:
            print("\n🔥 Wake detected in standby!")
            emit_event("VOX", "WAKE DETECTED IN STANDBY", state="LISTENING")
            _exit_sleep_primed = False
            speak("Yes, sir.")
            return "active"

        # Any other command
        _exit_sleep_primed = False
        keep_active = execute_command(text)
        if keep_active:
            return "active"
        else:
            return "deep_sleep"

    # The confirmation window expired without another exit command. Clear the
    # armed flag so main() returns to the normal wake-word listener instead of
    # reopening the standby window forever.
    _exit_sleep_primed = False
    print("💤 Transitioning to standard wake-word sleep mode.")
    return "deep_sleep"


# ============================================================
# MAIN
# ============================================================

def main(debug=False):
    global DEBUG_MODE
    DEBUG_MODE = debug
    if debug:
        set_debug_logging(True)
    import signal
    try:
        signal.signal(signal.SIGINT, lambda s, f: None)
    except Exception:
        pass
    event_bus.subscribe("reconnect_mic", lambda _payload: _MIC_RECONNECT_EVENT.set())
    event_bus.subscribe("jarvis_exit_requested", lambda _payload: terminate_jarvis_processes())

    # Warm the offline fallback asynchronously so the first online STT outage
    # does not also pay the model startup cost. This never delays wake listening.
    from voice.stt_offline import warmup_offline_model
    warmup_offline_model()

    print(
        "\n================================"
    )

    print(
        "       🤖 JARVIS ONLINE"
    )

    print(
        "================================"
    )

    print(
        "Say 'Hey Jarvis' once."
    )

    emit_event("SYS", "JARVIS CORE ONLINE", state="STANDBY", last_command="IDLE_MONITOR")

    while True:

        try:

            # --------------------------------------------
            # SLEEPING
            # --------------------------------------------

            # If exit was just spoken, listen in standby window first
            if _exit_sleep_primed:
                standby_res = exit_sleep_standby(timeout_seconds=25)
                if standby_res == "active":
                    active_mode()
                    continue

            initial_audio = wait_for_wake_word()

            if initial_audio is not None:
                # First utterance was spoken with wake word: "Hey Jarvis, open Chrome"
                # Directly enter active mode with the captured utterance — do not speak "Yes, sir." over it!
                active_mode(initial_audio=initial_audio)
            else:
                emit_event("TTS", "ACKNOWLEDGED", state="SPEAKING")
                interrupted_file = speak("Yes, sir.")
                active_mode(initial_audio=interrupted_file)


        except KeyboardInterrupt:

            print(
                "\n🛑 Keyboard interrupt ignored (voice-only operation). Use 'stop' or 'sleep'."
            )
            continue


        except Exception as e:
            log_error("Main voice loop failed", e)

            print(
                f"\n⚠️ Main error: {e}"
            )

            print(
                "🔄 Restarting..."
            )

            time.sleep(2)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()
