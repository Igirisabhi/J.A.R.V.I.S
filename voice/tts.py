import atexit
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import sounddevice as sd
from core.audio_devices import start_input_stream
import soundfile as sf
import numpy as np
import wave
from core.settings import get_settings
from core.timing import timer
from dotenv import load_dotenv

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


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# Use the interpreter running Jarvis so the worker doesn't depend on a
# virtual environment tied to a Python installation that may have moved.
TTS_PYTHON = Path(sys.executable).with_name("python.exe")
if not TTS_PYTHON.exists():
    TTS_PYTHON = Path(sys.executable)
TTS_SITE_PACKAGES = BASE_DIR / ".ttsenv" / "Lib" / "site-packages"
TTS_WORKER = BASE_DIR / "tts_worker.py"

INTERRUPT_DIR = BASE_DIR / "voice" / "interrupts"
INTERRUPT_DIR.mkdir(parents=True, exist_ok=True)

SAMPLE_RATE = 16000
BLOCK_SIZE = 512

_AUDIO_SETTINGS = get_settings()["audio"]
_TTS_SETTINGS = get_settings()["speech"]["tts"]
TTS_SPEECH_RATE = _TTS_SETTINGS["speech_rate"]
VOICE_THRESHOLD = _AUDIO_SETTINGS["interrupt_threshold"]
MIN_SPEECH_TIME = _AUDIO_SETTINGS["interrupt_min_speech_seconds"]
INTERRUPT_SILENCE = _AUDIO_SETTINGS["interrupt_silence_seconds"]
MAX_INTERRUPT_TIME = _AUDIO_SETTINGS["max_interrupt_seconds"]
PRE_ROLL_TIME = _AUDIO_SETTINGS["pre_roll_seconds"]
POST_TTS_SETTLE_SECONDS = min(0.1, max(0.05, _AUDIO_SETTINGS.get("post_tts_settle_seconds", 0.1)))

VOICE_PROFILES = {
    "jarvis": {
        "provider": "groq",
        "groq_voice": "troy",
        "gemini_voice": "Charon",
        "local_voice": "bm_george",
        "style": "professionally",
    },
    "friday": {
        "provider": "gemini",
        "groq_voice": "diana",
        "gemini_voice": "Kore",
        "local_voice": "bf_emma",
        "style": "A clear, composed, confident female assistant with a calm, precise delivery",
    },
    "us_navy": {
        "provider": "groq",
        "groq_voice": "daniel",
        "gemini_voice": "Charon",
        "local_voice": "am_michael",
        "style": "authoritatively",
    },
    # Keep the former profile IDs working for saved settings and callers.
    "christopher": {
        "provider": "groq",
        "groq_voice": "troy",
        "gemini_voice": "Puck",
        "local_voice": "bm_george",
        "style": "professionally",
    },
    "guy": {
        "provider": "groq",
        "groq_voice": "austin",
        "gemini_voice": "Fenrir",
        "local_voice": "am_adam",
        "style": "authoritatively",
    },
    "sonia": {
        "provider": "gemini",
        "groq_voice": "diana",
        "gemini_voice": "Kore",
        "local_voice": "bf_emma",
        "style": "A clear, composed, confident female assistant with a calm, precise delivery",
    },
}


# ============================================================
# PERSISTENT KOKORO WORKER
# ============================================================

_worker_process = None
_worker_ready = threading.Event()
_worker_lock = threading.Lock()
_worker_start_lock = threading.Lock()
_worker_request_id = 0
_pending_events: dict[int, threading.Event] = {}
_request_errors: dict[int, str] = {}
_online_client_lock = threading.Lock()
_groq_tts_client = None
_gemini_tts_client = None
_online_tts_failed_this_speak = False
_is_speaking = False


def is_speaking() -> bool:
    """Return True if Jarvis TTS is currently speaking or playing audio."""
    return _is_speaking


def _tts_worker_environment():
    """Reuse installed Kokoro packages without relying on .ttsenv's Python."""
    environment = os.environ.copy()
    if TTS_SITE_PACKAGES.is_dir():
        existing_pythonpath = environment.get("PYTHONPATH")
        python_paths = [str(TTS_SITE_PACKAGES)]
        if existing_pythonpath:
            python_paths.append(existing_pythonpath)
        environment["PYTHONPATH"] = os.pathsep.join(python_paths)
    return environment


def _read_worker_output(process):
    if process is None:
        return

    try:
        for line in iter(process.stdout.readline, ""):
            if not line:
                break
            if _worker_process is not process:
                break

            line = line.strip()

            if not line:
                continue

            try:
                message = json.loads(line)
                mtype = message.get("type")

                if mtype == "ready":
                    _worker_ready.set()

                elif mtype == "done":
                    req_id = message.get("id")
                    if req_id in _pending_events:
                        _pending_events[req_id].set()

                elif mtype == "error":
                    req_id = message.get("id")
                    err_msg = message.get("error", "Unknown error")
                    print(
                        f"❌ Kokoro worker error: {err_msg}"
                    )
                    if req_id in _pending_events:
                        _request_errors[req_id] = err_msg
                        _pending_events[req_id].set()

            except json.JSONDecodeError:
                print(f"⚠️ TTS worker: {line}")

    except Exception as e:
        print(f"⚠️ TTS worker reader stopped: {e}")

    finally:
        # A stale reader from a crashed worker must not clear the ready signal
        # for a replacement worker that has already started.
        if _worker_process is process:
            _worker_ready.clear()



def start_tts_worker(wait=True):
    global _worker_process

    with _worker_start_lock:

        if (
            _worker_process is not None
            and _worker_process.poll() is None
        ):
            if wait and not _worker_ready.wait(timeout=60):
                print("❌ Kokoro worker is running but did not become ready.")
                return None

            return _worker_process

        if not TTS_PYTHON.exists():
            print(
                f"❌ TTS Python not found: {TTS_PYTHON}"
            )
            return None

        if not TTS_WORKER.exists():
            print(
                f"❌ TTS worker not found: {TTS_WORKER}"
            )
            return None

        print("⚡ Starting persistent Kokoro worker...")

        try:

            _worker_ready.clear()

            _worker_process = subprocess.Popen(
                [
                    str(TTS_PYTHON),
                    str(TTS_WORKER),
                ],
                env=_tts_worker_environment(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )

            threading.Thread(
                target=_read_worker_output,
                args=(_worker_process,),
                daemon=True,
            ).start()

            if wait:

                if not _worker_ready.wait(timeout=60):

                    print(
                        "❌ Kokoro worker did not become ready."
                    )

                    try:
                        _worker_process.kill()
                    except Exception:
                        pass

                    _worker_process = None

                    return None

                print("✅ Kokoro worker ready.")

            return _worker_process

        except Exception as e:

            print(
                f"❌ Failed to start Kokoro worker: {e}"
            )

            _worker_process = None

            return None


def _stop_tts_worker():
    """Stop the TTS helper without allowing a blocked pipe to hang shutdown."""
    global _worker_process
    process = _worker_process
    if process is None:
        return

    try:
        if process.poll() is None:
            # Writing a shutdown message to stdin can block when the pipe is
            # full. Terminate directly, with a bounded wait and kill fallback.
            process.terminate()
            try:
                process.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
    except Exception:
        pass
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        _worker_process = None
        _worker_ready.clear()

atexit.register(_stop_tts_worker)


# ============================================================
# TEXT CLEANING
# ============================================================

def clean_tts_text(text: str) -> str:

    if not text:
        return ""

    text = text.replace("**", "")
    text = text.replace("__", "")
    text = text.replace("*", "")

    text = text.replace("`", "")
    text = text.replace("#", "")
    text = text.replace("•", "")

    text = re.sub(
        r"\[([^\]]+)\]\([^)]+\)",
        r"\1",
        text,
    )

    text = re.sub(
        r"https?://\S+",
        "",
        text,
    )

    text = re.sub(
        r"[_~|<>]+",
        " ",
        text,
    )

    text = re.sub(
        r"[{}\[\]]",
        " ",
        text,
    )

    text = " ".join(text.split())

    return text.strip()


# ============================================================
# TEXT SPLITTER
# ============================================================

def split_text(text, max_chars=600):

    sentences = re.split(
        r"(?<=[.!?])\s+",
        text,
    )

    chunks = []
    current = ""

    for sentence in sentences:

        sentence = sentence.strip()

        if not sentence:
            continue

        if len(current) + len(sentence) + 1 <= max_chars:

            if current:
                current += " "

            current += sentence

        else:

            if current:
                chunks.append(current)

            current = sentence

    if current:
        chunks.append(current)

    return chunks


# ============================================================
# GENERATE AUDIO USING PERSISTENT WORKER
# ============================================================

def _generate_with_worker(text, output_file, voice=None, speed=None):

    global _worker_request_id

    worker = start_tts_worker(wait=True)

    if worker is None:
        return False

    with _worker_lock:

        _worker_request_id += 1

        request_id = _worker_request_id
        done_event = threading.Event()
        _pending_events[request_id] = done_event

        try:

            if worker.poll() is not None:

                print(
                    "⚠️ Kokoro worker stopped. Restarting..."
                )

                _worker_ready.clear()

                worker = start_tts_worker(
                    wait=True
                )

                if worker is None:
                    return False

            use_voice = voice or _TTS_SETTINGS["local_voice"]
            voice_lower = str(use_voice).lower()
            if voice_lower in ("bm_george", "christopher", "troy"):
                use_voice = "bm_george"
            elif voice_lower in ("am_adam", "guy", "austin"):
                use_voice = "am_adam"
            elif voice_lower in ("bf_emma", "sonia", "diana"):
                use_voice = "bf_emma"
            use_speed = speed if speed is not None else _TTS_SETTINGS["speech_rate"]

            request = {
                "type": "speak",
                "id": request_id,
                "text": text,
                "output": str(output_file),
                "speed": use_speed,
                "voice": use_voice,
            }

            worker.stdin.write(
                json.dumps(request)
                + "\n"
            )

            worker.stdin.flush()

            signaled = done_event.wait(timeout=60)

            if not signaled:
                print(
                    "❌ TTS generation timed out."
                )
                return False

            if request_id in _request_errors:
                return False

            return output_file.exists() and output_file.stat().st_size > 44

        except Exception as e:

            print(
                f"❌ Persistent TTS error: {e}"
            )

            return False

        finally:
            _pending_events.pop(request_id, None)
            _request_errors.pop(request_id, None)


def _get_groq_tts_client():
    global _groq_tts_client
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not configured.")
    with _online_client_lock:
        if _groq_tts_client is None:
            from groq import Groq

            _groq_tts_client = Groq(
                api_key=api_key,
                timeout=_TTS_SETTINGS["timeout_seconds"],
            )
    return _groq_tts_client


def _get_gemini_tts_client():
    global _gemini_tts_client
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured.")
    with _online_client_lock:
        if _gemini_tts_client is None:
            from google import genai

            _gemini_tts_client = genai.Client(api_key=api_key)
    return _gemini_tts_client


def _validate_generated_audio(output_file):
    output_file = Path(output_file)
    if not output_file.exists() or output_file.stat().st_size <= 44:
        raise RuntimeError("The online TTS provider returned an empty audio file.")
    if sf.info(str(output_file)).frames <= 0:
        raise RuntimeError("The online TTS provider returned audio with no frames.")
    return output_file


def _generate_with_groq(text, output_file, voice=None, speed=None, style=None):
    use_voice = voice or _TTS_SETTINGS["groq_voice"]
    voice_lower = str(use_voice).lower()
    # Preserve the three supported Orpheus voices used by the profiles. Only
    # translate historical profile names; never collapse Daniel into Austin.
    legacy_voice_aliases = {
        "guy": "austin",
        "sonia": "diana",
        "christopher": "troy",
    }
    use_voice = legacy_voice_aliases.get(voice_lower, voice_lower)
    if use_voice not in ("autumn", "diana", "hannah", "austin", "daniel", "troy"):
        use_voice = "troy"
    use_speed = speed if speed is not None else _TTS_SETTINGS["speech_rate"]
    print(f"⚡ TTS → Groq (voice: {use_voice})")
    if style:
        text = f"[{style}] {text}"
    response = _get_groq_tts_client().audio.speech.create(
        model=_TTS_SETTINGS["groq_model"],
        voice=use_voice,
        input=text,
        speed=use_speed,
        response_format="wav",
    )
    response.write_to_file(str(output_file))
    return _validate_generated_audio(output_file)


def _generate_with_gemini(text, output_file, voice=None, speed=None, style=None):
    use_voice = voice or _TTS_SETTINGS["gemini_voice"]
    voice_lower = str(use_voice).lower()
    if voice_lower in ("puck", "christopher", "troy"):
        use_voice = "Puck"
    elif voice_lower in ("fenrir", "guy", "austin"):
        use_voice = "Fenrir"
    elif voice_lower in ("kore", "sonia", "diana", "aoede"):
        use_voice = "Kore"
    use_speed = speed if speed is not None else _TTS_SETTINGS["speech_rate"]
    print(f"🧠 TTS → Gemini (voice: {use_voice})")
    rate_delta = round((use_speed - 1.0) * 100)
    if rate_delta >= 5:
        pace = f"a brisk, even conversational pace, about {rate_delta}% faster than normal"
    elif rate_delta <= -5:
        pace = f"a steady conversational pace, about {abs(rate_delta)}% slower than normal"
    else:
        pace = "a natural, steady conversational pace"
    interaction = _get_gemini_tts_client().interactions.create(
        model=_TTS_SETTINGS["gemini_model"],
        input=[
            {
                "type": "user_input",
                "content": [
                    {
                        "type": "text",
                        # Gemini TTS treats this field as the verbatim text to
                        # speak. Keep delivery directions in speech_metadata so
                        # they never leak into the spoken answer.
                        "text": text,
                        "annotations": [
                            {
                                "type": "speech_metadata",
                                "style": f"{style or 'calm, clear, confident assistant voice'}; {pace}; consistent speed and short natural pauses",
                            }
                        ],
                    }
                ],
            }
        ],
        response_format={"type": "audio"},
        generation_config={
            "speech_config": [{"voice": use_voice}]
        },
        timeout=_TTS_SETTINGS["timeout_seconds"],
    )

    try:
        audio_data = getattr(getattr(interaction, "output_audio", None), "data", None)
    except AttributeError:
        audio_data = None
    if not audio_data:
        raise RuntimeError("Gemini returned no speech audio.")
    output_file.write_bytes(base64.b64decode(audio_data))
    return _validate_generated_audio(output_file)


def _generate_online_tts(
    text,
    output_file,
    groq_voice=None,
    gemini_voice=None,
    speech_rate=None,
    provider=None,
    profile_style=None,
):
    if not _TTS_SETTINGS["online_enabled"]:
        return False

    providers = (
        ("Groq", "groq", "groq_enabled", "GROQ_API_KEY", lambda t, out: _generate_with_groq(t, out, voice=groq_voice, speed=speech_rate, style=profile_style)),
        ("Gemini", "gemini", "gemini_enabled", "GEMINI_API_KEY", lambda t, out: _generate_with_gemini(t, out, voice=gemini_voice, speed=speech_rate, style=profile_style)),
    )
    if provider in {"groq", "gemini"}:
        providers = tuple(item for item in providers if item[1] == provider)
    for name, provider_key, enabled_key, env_key, generate in providers:
        provider_selected_by_profile = provider == provider_key
        if (not _TTS_SETTINGS[enabled_key] and not provider_selected_by_profile) or not os.getenv(env_key):
            continue
        try:
            generate(text, output_file)
            print(f"✅ {name} online TTS ready.")
            return True
        except Exception as error:
            status = getattr(error, "status_code", None)
            diagnostic = f"HTTP {status}" if status else error.__class__.__name__
            detail = getattr(error, "message", None)
            if detail:
                diagnostic += f": {' '.join(str(detail).split())[:200]}"
            print(f"⚠️ {name} online TTS failed ({diagnostic}).")
    return False


def _generate_speech(
    text,
    output_file,
    *,
    groq_voice=None,
    gemini_voice=None,
    local_voice=None,
    speech_rate=None,
    provider=None,
    profile_style=None,
):
    global _online_tts_failed_this_speak
    if _TTS_SETTINGS["online_enabled"] and not _online_tts_failed_this_speak:
        if _generate_online_tts(
            text,
            output_file,
            groq_voice=groq_voice,
            gemini_voice=gemini_voice,
            speech_rate=speech_rate,
            provider=provider,
            profile_style=profile_style,
        ):
            return True
        _online_tts_failed_this_speak = True

    if _TTS_SETTINGS["local_fallback_enabled"]:
        print("🔄 Online TTS unavailable; using local Kokoro fallback...")
        return _generate_with_worker(text, output_file, voice=local_voice, speed=speech_rate)

    print("❌ Online TTS unavailable and local Kokoro fallback is disabled.")
    return False



# ============================================================
# SAVE INTERRUPTED AUDIO
# ============================================================

def save_interrupt_audio(chunks):
    if not chunks:
        return None

    try:
        audio = np.concatenate(chunks, axis=0)
        audio = np.clip(audio, -1.0, 1.0)
        audio_int16 = np.int16(audio * 32767)

        output_file = INTERRUPT_DIR / f"interrupt_command_{time.time_ns()}.wav"

        with wave.open(str(output_file), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(audio_int16.tobytes())

        return output_file
    except Exception as e:
        print(f"⚠️ Error saving interrupt audio: {e}")
        return None


# ============================================================
# FAST BARGE-IN LISTENER
# ============================================================

class BargeInListener:

    def __init__(self):
        self.running = False
        self.interrupted = False
        self.speech_detected = False
        self.speech_started_at = None
        self.last_speech_time = None
        self.chunks = []
        self.pre_roll = []
        self.lock = threading.Lock()
        self.stream = None
        self.thread = None
        self.ready = threading.Event()
        self.start_error = None
        self.playback_reference = None
        self.playback_started_at = None
        self.max_pre_roll_chunks = max(
            1,
            int(_AUDIO_SETTINGS["pre_roll_seconds"] * SAMPLE_RATE / BLOCK_SIZE)
        )

    def set_playback_reference(self, audio, sample_rate):
        """Prepare a mono 16 kHz reference so speaker echo won't trigger barge-in."""
        reference = np.asarray(audio, dtype=np.float32)
        if reference.ndim > 1:
            reference = np.mean(reference, axis=1)
        if sample_rate != SAMPLE_RATE and reference.size:
            output_length = max(1, int(round(reference.size * SAMPLE_RATE / sample_rate)))
            source_positions = np.arange(output_length, dtype=np.float32) * (
                sample_rate / SAMPLE_RATE
            )
            reference = np.interp(
                source_positions,
                np.arange(reference.size, dtype=np.float32),
                reference,
            ).astype(np.float32)
        self.playback_reference = reference
        self.playback_started_at = time.monotonic()

    def _is_playback_echo(self, audio, now):
        reference = self.playback_reference
        started_at = self.playback_started_at
        if reference is None or started_at is None or audio.size == 0:
            return False

        block_length = audio.size
        expected_start = int((now - started_at) * SAMPLE_RATE)
        latest_start = min(expected_start, reference.size - block_length)
        # Output and input devices have independent buffering. Room echo can
        # arrive well after the nominal playback position, so search a wider
        # recent segment before classifying this block as user speech.
        earliest_start = max(0, latest_start - int(1.0 * SAMPLE_RATE))
        if latest_start < earliest_start:
            return False

        starts = np.arange(earliest_start, latest_start + 1, 64, dtype=np.int64)
        if starts.size == 0:
            return False
        offsets = starts[:, None] + np.arange(block_length, dtype=np.int64)[None, :]
        windows = reference[offsets]
        windows -= np.mean(windows, axis=1, keepdims=True)
        mic = audio - np.mean(audio)
        mic_energy = float(np.dot(mic, mic))
        if mic_energy <= 1e-8:
            return False

        ref_energy = np.einsum("ij,ij->i", windows, windows)
        valid = ref_energy > 1e-8
        if not np.any(valid):
            return False
        correlations = np.abs(windows[valid] @ mic) / np.sqrt(ref_energy[valid] * mic_energy)
        # A speaker/room path changes phase and amplitude enough to lower the
        # raw waveform correlation. A moderate match is still strong evidence
        # that the mic is hearing the active TTS playback.
        return bool(correlations.size and float(np.max(correlations)) >= 0.38)

    def callback(self, indata, frames, time_info, status):
        if not self.running:
            return

        audio = indata[:, 0].copy()
        rms = float(np.sqrt(np.mean(audio * audio)))
        now = time.monotonic()

        # The mic hears Jarvis through the speakers. Suppress blocks that
        # strongly match the audio currently being played, while allowing
        # different speech to interrupt normally.
        if rms >= _AUDIO_SETTINGS["interrupt_threshold"] and self._is_playback_echo(audio, now):
            return

        self.pre_roll.append(audio.copy())
        if len(self.pre_roll) > self.max_pre_roll_chunks:
            self.pre_roll.pop(0)

        if rms >= _AUDIO_SETTINGS["interrupt_threshold"]:
            if not self.speech_detected:
                # A barge-in starts a fresh voice turn. Don't let its timing
                # summary include the prior AI reply and playback duration.
                timer.start_command()
                timer.start("recording")
                self.speech_detected = True
                self.speech_started_at = now
                with self.lock:
                    # The latest pre-roll sample is this callback's block;
                    # append it once below, after the earlier lead-in blocks.
                    for chunk in self.pre_roll[:-1]:
                        self.chunks.append(chunk.reshape(-1, 1))
                    self.pre_roll.clear()

            self.last_speech_time = now
            with self.lock:
                self.chunks.append(audio.reshape(-1, 1))

            if self.speech_started_at is not None and now - self.speech_started_at >= _AUDIO_SETTINGS["interrupt_min_speech_seconds"]:
                self.interrupted = True
        else:
            if self.speech_detected:
                with self.lock:
                    self.chunks.append(audio.reshape(-1, 1))

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        if not self.ready.wait(timeout=1.0):
            print("⚠️ Interrupt microphone did not become ready in time.")
            self.stop()
            return False
        return self.start_error is None and self.stream is not None

    def _run(self):
        try:
            self.stream = start_input_stream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=BLOCK_SIZE,
                callback=self.callback,
            )
            self.ready.set()

            while self.running:
                time.sleep(0.01)

        except Exception as e:
            self.start_error = e
            self.ready.set()
            print(f"\n⚠️ Interrupt microphone error: {e}")
        finally:
            self.ready.set()
            try:
                if self.stream:
                    self.stream.stop()
                    self.stream.close()
            except Exception:
                pass

    def wait_for_command_end(self):
        start = time.monotonic()

        while self.running:
            time.sleep(0.02)
            now = time.monotonic()

            if self.speech_detected:
                if (
                    self.last_speech_time
                    and now - self.last_speech_time >= _AUDIO_SETTINGS["interrupt_silence_seconds"]
                ):
                    break

                if self.speech_started_at is not None and now - self.speech_started_at >= _AUDIO_SETTINGS["max_interrupt_seconds"]:
                    break

            if now - start >= 15:
                break

        timer.stop("recording")
        self.stop()

    def stop(self):
        self.running = False
        try:
            if self.stream:
                self.stream.stop()
                self.stream.close()
        except Exception:
            pass
        self.stream = None

        if self.thread and self.thread != threading.current_thread():
            self.thread.join(timeout=0.5)

    def get_audio_file(self):
        with self.lock:
            chunks = list(self.chunks)

        if not chunks:
            return None

        return save_interrupt_audio(chunks)


# ============================================================
# PLAY AUDIO WHILE LISTENING (BARGE-IN)
# ============================================================

def play_audio_interruptible(file_path, *, volume: float | None = None):
    try:
        audio, sample_rate = sf.read(
            str(file_path),
            dtype="float32",
        )
        tts_volume = _AUDIO_SETTINGS["tts_volume"] if volume is None else volume
        if tts_volume != 1.0:
            audio = np.clip(audio * tts_volume, -1.0, 1.0)
    except Exception as e:
        print(f"⚠️ Error reading audio file {file_path}: {e}")
        return None

    interrupted = False
    voice_interrupted = False
    listener = BargeInListener()
    listener_ready = listener.start()
    if listener_ready:
        listener.set_playback_reference(audio, sample_rate)
    else:
        print("⚠️ Voice interruption is unavailable; audio will still play.")

    try:
        sd.play(
            audio,
            samplerate=sample_rate,
            blocking=False,
        )

        playback_started = time.monotonic()
        last_level_update = -1.0
        while True:
            try:
                time.sleep(0.01)
                elapsed = time.monotonic() - playback_started
                if elapsed - last_level_update >= 0.1:
                    start = min(len(audio), int(elapsed * sample_rate))
                    end = min(len(audio), start + max(1, int(sample_rate * 0.1)))
                    if end > start:
                        level = float(np.sqrt(np.mean(np.square(audio[start:end]))))
                        from core.events import event_bus
                        event_bus.publish("audio_level", level=max(0.0, min(1.0, level * 3.0)))
                    last_level_update = elapsed

                # Voice-only interrupt: only sustained spoken stop/speech can interrupt TTS
                if listener_ready and listener.interrupted:
                    interrupted = True
                    voice_interrupted = True
                    print("\n🛑 INTERRUPTED BY USER SPEECH")
                    try:
                        from core.debug_logger import log_voice_pipeline
                        log_voice_pipeline("VAD", "TTS playback interrupted by user speech")
                    except ImportError:
                        pass
                    sd.stop()
                    break

                try:
                    stream = sd.get_stream()
                    if stream is None or not stream.active:
                        break
                except Exception:
                    break
            except KeyboardInterrupt:
                # Ignore Ctrl+C / KeyboardInterrupt during TTS playback
                continue

    except Exception as e:
        print(f"\n⚠️ Playback error: {e}")
        try:
            sd.stop()
        except Exception:
            pass
    finally:
        if listener_ready and (voice_interrupted or listener.speech_detected):
            try:
                sd.stop()
            except Exception:
                pass
            # Keep recording through the user's phrase if an actual barge-in occurred.
            try:
                from core.debug_logger import log_voice_pipeline
                log_voice_pipeline("VAD", "Capturing complete user utterance following playback")
            except ImportError:
                pass
            listener.wait_for_command_end()
        else:
            listener.stop()

    if not voice_interrupted and not (listener_ready and listener.speech_detected):
        time.sleep(POST_TTS_SETTLE_SECONDS)
        return None

    return listener.get_audio_file() if (listener_ready and (voice_interrupted or listener.speech_detected)) else None


# ============================================================
# SPEAK CHUNK
# ============================================================

def speak_chunk(
    text,
    *,
    groq_voice=None,
    gemini_voice=None,
    local_voice=None,
    speech_rate=None,
    volume=None,
    provider=None,
    profile_style=None,
):
    temp_file = (
        Path(tempfile.gettempdir())
        / "jarvis_tts.wav"
    )

    if temp_file.exists():
        try:
            temp_file.unlink()
        except Exception:
            pass

    try:
        success = _generate_speech(
            text,
            temp_file,
            groq_voice=groq_voice,
            gemini_voice=gemini_voice,
            local_voice=local_voice,
            speech_rate=speech_rate,
            provider=provider,
            profile_style=profile_style,
        )

        if not success:
            print(f"❌ TTS audio generation failed for: '{text}'")
            return None

        if not temp_file.exists():
            print(f"❌ TTS audio file was not created for: '{text}'")
            return None

        return play_audio_interruptible(temp_file, volume=volume)

    except Exception as e:
        print(f"❌ TTS error: {e}")
        return None

    finally:
        try:
            if temp_file.exists():
                temp_file.unlink()
        except Exception:
            pass


# ============================================================
# SPEAK
# ============================================================

def speak(
    text: str,
    *,
    groq_voice: str | None = None,
    gemini_voice: str | None = None,
    local_voice: str | None = None,
    speech_rate: float | None = None,
    volume: float | None = None,
    voice_profile: str | None = None,
):
    global _online_tts_failed_this_speak, _is_speaking
    settings = get_settings()
    text = clean_tts_text(text)

    if not text:
        return None

    # Resolve the selected profile for normal replies as well as test synthesis.
    profile_key = str(
        voice_profile or settings["speech"]["tts"].get("voice_profile", "jarvis")
    ).lower().strip()
    defaults = VOICE_PROFILES.get(profile_key, VOICE_PROFILES["jarvis"])
    if groq_voice is None:
        groq_voice = defaults["groq_voice"]
    if gemini_voice is None:
        gemini_voice = defaults["gemini_voice"]
    if local_voice is None:
        local_voice = defaults["local_voice"]
    provider = defaults["provider"]
    profile_style = defaults["style"]

    _is_speaking = True
    try:
        from core.events import event_bus
        event_bus.publish("state_changed", state="SPEAKING")

        # Try cloud TTS again for each new reply, but don't repeat a failed network
        # request for every chunk of the same long reply.
        _online_tts_failed_this_speak = False
        print("🔊 Speaking...")

        # Groq Orpheus accepts up to 200 characters per request.
        chunks = split_text(text, max_chars=180 if provider == "groq" else 600)

        for chunk in chunks:
            interrupted_file = speak_chunk(
                chunk,
                groq_voice=groq_voice,
                gemini_voice=gemini_voice,
                local_voice=local_voice,
                speech_rate=speech_rate,
                volume=volume,
                provider=provider,
                profile_style=profile_style,
            )

            if interrupted_file:
                event_bus.publish("state_changed", state="IDLE")
                return interrupted_file
        event_bus.publish("state_changed", state="IDLE")
        return None
    finally:
        _is_speaking = False
