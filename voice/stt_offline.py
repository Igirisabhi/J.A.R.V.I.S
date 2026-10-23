"""
Offline STT fallback using faster-whisper (CTranslate2).

Used when online transcription is disabled or unavailable. The model warms in
a background thread after the voice core starts; if preload is disabled or
fails, it loads on demand during the first local transcription.

Model choice: 'base' with int8 quantization gives ~1-2s latency on
CPU for short voice commands while still handling Indian-accented
English well enough for command recognition.  The stt_cleanup module
fixes residual misrecognitions afterwards.
"""

import io
import logging
import threading
import time
from pathlib import Path

from core.settings import get_settings

logger = logging.getLogger(__name__)

# ============================================================
# LAZY MODEL SINGLETON
# ============================================================
# Avoids importing faster_whisper at module level so Jarvis
# starts instantly even if the package is installed.

_model = None
_model_lock = threading.Lock()
_warmup_thread = None
_LOCAL_SETTINGS = get_settings()["speech"]["local"]
_MODEL_CONFIG = None


def _get_model():
    """Load the configured local model once, with CPU recovery if CUDA fails."""
    global _model, _MODEL_CONFIG
    get_settings()
    requested_config = (
        _LOCAL_SETTINGS["model"], _LOCAL_SETTINGS["device"],
        _LOCAL_SETTINGS["compute_type"], _LOCAL_SETTINGS["cpu_threads"],
        _LOCAL_SETTINGS["workers"],
    )
    if _model is None or requested_config != _MODEL_CONFIG:
        with _model_lock:
            if _model is not None and requested_config == _MODEL_CONFIG:
                return _model

            print("📦 Loading offline STT model in the background...")
            from faster_whisper import WhisperModel

            model_name = _LOCAL_SETTINGS["model"]
            device = _LOCAL_SETTINGS["device"]
            compute_type = _LOCAL_SETTINGS["compute_type"]
            options = {
                "device": device,
                "compute_type": compute_type,
                "cpu_threads": _LOCAL_SETTINGS["cpu_threads"],
                "num_workers": _LOCAL_SETTINGS["workers"],
            }
            try:
                _model = WhisperModel(model_name, **options)
            except Exception as first_error:
                if device != "cpu" or compute_type != "int8":
                    print(
                        f"⚠️ Local STT device '{device}' or compute mode unsupported "
                        f"({first_error.__class__.__name__}); retrying on CPU int8."
                    )
                    options["device"] = "cpu"
                    options["compute_type"] = "int8"
                    _model = WhisperModel(model_name, **options)
                else:
                    raise
            print("✅ Offline STT model ready.")
            _MODEL_CONFIG = requested_config
    return _model


def warmup_offline_model():
    """Start a non-blocking model warmup once the voice core has started."""
    global _warmup_thread
    if not _LOCAL_SETTINGS["enabled"] or not _LOCAL_SETTINGS["preload_on_startup"]:
        return None
    if _model is not None or (_warmup_thread and _warmup_thread.is_alive()):
        return _warmup_thread

    def _warm():
        delay = _LOCAL_SETTINGS["preload_delay_seconds"]
        if delay:
            time.sleep(delay)
        try:
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadPriority(
                    ctypes.windll.kernel32.GetCurrentThread(), -1
                )  # THREAD_PRIORITY_BELOW_NORMAL
            except (AttributeError, OSError):
                pass
            _get_model()
        except Exception:
            logger.exception("Offline STT warmup failed; fallback will retry on demand")

    _warmup_thread = threading.Thread(
        target=_warm,
        name="JarvisOfflineSTTWarmup",
        daemon=True,
    )
    _warmup_thread.start()
    return _warmup_thread


# ============================================================
# TRANSCRIBE
# ============================================================

def transcribe_offline(audio_source) -> str:
    """
    Transcribe audio locally using faster-whisper.

    Accepts the same inputs as stt_groq.transcribe():
      - io.BytesIO  (in-memory WAV from recorder)
      - bytes / bytearray
      - str / Path   (file path)

    Returns the transcript text (stripped), or "" on failure.
    """
    get_settings()
    from voice.stt_groq import WHISPER_PROMPT

    try:
        if not _LOCAL_SETTINGS["enabled"]:
            return ""
        model = _get_model()

        source = _decode_recording(audio_source)

        segments, _info = model.transcribe(
            source,
            language="en",
            beam_size=_LOCAL_SETTINGS["beam_size"],
            best_of=1,
            temperature=0.0,
            initial_prompt=WHISPER_PROMPT,
            vad_filter=_LOCAL_SETTINGS["vad_filter"],
            vad_parameters=dict(
                min_silence_duration_ms=_LOCAL_SETTINGS["vad_min_silence_ms"],
            ),
        )

        # segments is a generator — consume it
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return text

    except Exception as e:
        from core.error_log import log_error
        log_error("Offline STT failed; returning empty transcript so the voice loop can continue", e)
        print(f"⚠️ Offline STT also failed: {e}")
        return ""


def _decode_recording(audio_source):
    """Decode recorder WAV data to the 16 kHz float array Whisper accepts.

    Jarvis records mono WAV at 16 kHz. Decoding these in-memory recordings with
    soundfile avoids faster-whisper's PyAV path, which is incompatible with
    PyAV 19's removal of the ``metadata_errors`` argument.
    """
    import numpy as np
    import soundfile as sf

    file_to_read: io.BytesIO | str
    if isinstance(audio_source, io.BytesIO):
        audio_source.seek(0)
        file_to_read = audio_source
    elif isinstance(audio_source, (bytes, bytearray)):
        file_to_read = io.BytesIO(audio_source)
    else:
        file_to_read = str(Path(audio_source))

    samples, sample_rate = sf.read(file_to_read, dtype="float32", always_2d=True)
    if samples.size == 0:
        raise ValueError("The recorded audio is empty.")

    # The live recorder produces 16 kHz WAV. Reject unexpected rates rather
    # than silently sending Whisper samples with an incorrect time scale.
    if sample_rate != 16000:
        raise ValueError(
            f"Expected 16 kHz recorded audio, received {sample_rate} Hz."
        )

    mono = samples.mean(axis=1, dtype=np.float32)
    return np.ascontiguousarray(mono, dtype=np.float32)


# ============================================================
# SELF-TEST
# ============================================================

if __name__ == "__main__":
    from voice.recorder import record_command

    print("🎤 Record a command to test offline STT...")
    audio = record_command()
    if audio:
        print("🧠 Transcribing offline...")
        result = transcribe_offline(audio)
        print(f"\n🗣️ You said: {result}")
    else:
        print("❌ No audio recorded.")
