import sounddevice as sd
import numpy as np
import wave
from pathlib import Path
import time

import io
import os
from dotenv import load_dotenv

from core.settings import get_settings
from core.audio_devices import input_stream

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

_AUDIO_SETTINGS = get_settings()["audio"]
SAMPLE_RATE = 16000  # Required by the wake-word model and Whisper input.
CHANNELS = 1

OUTPUT_DIR = Path("voice") / "recordings"
OUTPUT_FILE = OUTPUT_DIR / "command.wav"

ENERGY_THRESHOLD = _AUDIO_SETTINGS["energy_threshold"]
# Trailing silence: default 0.45s ensures fast response without clipping
DEFAULT_SILENCE_DURATION = 0.45
MIN_SILENCE_DURATION = 0.40
SILENCE_DURATION = max(MIN_SILENCE_DURATION, _AUDIO_SETTINGS.get("silence_duration_seconds", DEFAULT_SILENCE_DURATION))
MAX_RECORDING_TIME = _AUDIO_SETTINGS["max_recording_seconds"]


from core.timing import timer

# Debug flag: avoid unnecessary disk I/O in production; save only if requested
SAVE_RECORDINGS = os.getenv("JARVIS_SAVE_RECORDINGS", "0") == "1"
_speech_vad = None
_speech_vad_unavailable = False


def _get_speech_vad():
    """Load Silero VAD once so noise must look like speech before recording starts."""
    global _speech_vad, _speech_vad_unavailable
    if _speech_vad is None and not _speech_vad_unavailable:
        try:
            from openwakeword.vad import VAD
            _speech_vad = VAD()
        except Exception as exc:
            _speech_vad_unavailable = True
            print(f"⚠️ Speech detector unavailable; using sustained-energy fallback: {exc}")
    return _speech_vad


def record_command(timeout=None):
    audio_settings = get_settings()["audio"]
    silence_timeout = max(
        MIN_SILENCE_DURATION,
        audio_settings.get("silence_duration_seconds", DEFAULT_SILENCE_DURATION)
    )
    max_recording_time = audio_settings.get("max_recording_seconds", MAX_RECORDING_TIME)
    print("🎤 Waiting for speech...")
    timer.start_command()
    timer.start("listening")

    try:
        from voice.tts import is_speaking
        wait_start = time.monotonic()
        while is_speaking() and time.monotonic() - wait_start < 4.0:
            time.sleep(0.04)
    except Exception:
        pass

    audio_chunks = []
    onset_chunks = []
    noise_chunks = []
    speech_vad = _get_speech_vad()
    try:
        device_info = sd.query_devices(kind="input")
        print(f"🎙️ Using Windows default microphone: {device_info['name']}")
    except (sd.PortAudioError, ValueError, TypeError) as error:
        print(f"⚠️ Could not identify the selected microphone: {error}")
    if speech_vad is not None:
        speech_vad.reset_states()
    consecutive_speech_frames = 0
    recording = False
    silence_start = None
    start_time = None
    # Monotonic time avoids clock corrections from extending or shortening a recording.
    stream_start = time.monotonic()

    def callback(indata, frames, time_info, status):
        nonlocal recording, silence_start, start_time, consecutive_speech_frames, speech_vad

        if status:
            print(f"⚠️ Microphone stream: {status}")

        audio = indata[:, 0].copy()
        volume = float(np.sqrt(np.mean(audio * audio)))
        try:
            from core.events import event_bus
            event_bus.publish("audio_level", level=max(0.0, min(1.0, volume * 12.0)))
        except ImportError:
            pass
        looks_like_speech = volume > audio_settings["energy_threshold"]
        if looks_like_speech and speech_vad is not None:
            pcm = np.int16(np.clip(audio, -1.0, 1.0) * 32767)
            try:
                vad_threshold = audio_settings.get("speech_vad_threshold", 0.35)
                looks_like_speech = speech_vad.predict(pcm) >= vad_threshold
            except Exception:
                speech_vad = None

        if not recording:
            onset_chunks.append(indata.copy())
            if len(onset_chunks) > 10:  # 300 ms at the 30 ms callback size
                onset_chunks.pop(0)

            if not looks_like_speech:
                noise_chunks.append(audio.copy())
                if len(noise_chunks) > 20:  # Keep at most 600 ms of recent room noise.
                    noise_chunks.pop(0)

            consecutive_speech_frames = consecutive_speech_frames + 1 if looks_like_speech else 0
            if consecutive_speech_frames < 3:
                return

            print("🟢 Speech detected...")
            try:
                from core.debug_logger import log_voice_pipeline
                log_voice_pipeline("VAD", "Speech start detected", volume=round(volume, 4), threshold=audio_settings["energy_threshold"])
            except ImportError:
                pass
            timer.stop("listening")
            timer.start("recording")
            recording = True
            start_time = time.monotonic()
            audio_chunks.extend(onset_chunks)
            onset_chunks.clear()
            silence_start = None
            return

        audio_chunks.append(indata.copy())
        if looks_like_speech:
            silence_start = None
        elif silence_start is None:
            silence_start = time.monotonic()
        if not looks_like_speech:
            noise_chunks.append(audio.copy())
            if len(noise_chunks) > 40:
                noise_chunks.pop(0)

    try:
        with input_stream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            callback=callback,
            blocksize=480,
        ):
            while True:
                time.sleep(0.02)

                now = time.monotonic()
                if not recording and timeout is not None and now - stream_start >= max(0.0, timeout):
                    try:
                        from core.debug_logger import log_voice_pipeline
                        log_voice_pipeline("VAD", "Listen window expired without speech", timeout=timeout)
                    except ImportError:
                        pass
                    break

                if recording:
                    current_silence_timeout = silence_timeout
                    if start_time is not None and (now - start_time) >= 0.5:
                        current_silence_timeout = min(silence_timeout, 0.40)
                    if silence_start is not None and now - silence_start >= current_silence_timeout:
                        try:
                            from core.debug_logger import log_voice_pipeline
                            log_voice_pipeline("VAD", "Silence timeout reached", silence_duration=round(now - silence_start, 3), timeout=current_silence_timeout)
                        except ImportError:
                            pass
                        break

                    if start_time is not None and now - start_time >= max_recording_time:
                        try:
                            from core.debug_logger import log_voice_pipeline
                            log_voice_pipeline("VAD", "Max recording time reached", duration=round(now - start_time, 3), max_allowed=max_recording_time)
                        except ImportError:
                            pass
                        break
    except sd.PortAudioError as error:
        print(f"⚠️ Microphone unavailable: {error}")
        try:
            from core.events import event_bus
            event_bus.publish("error", message=f"Microphone unavailable: {error}")
        except ImportError:
            pass
        return None
    finally:
        # Stop both stages even if the microphone device errors or a timed
        # standby listen returns without speech.
        timer.stop("listening")
        timer.stop("recording")

    if not audio_chunks:
        print("❌ No speech detected")
        try:
            from core.debug_logger import log_voice_pipeline
            log_voice_pipeline("VAD", "No speech detected in listening session")
        except ImportError:
            pass
        return None

    audio = np.concatenate(
        audio_chunks
    )

    # --------------------------------------------------------
    # Audio preprocessing for better STT accuracy
    # --------------------------------------------------------

    noise_profile = np.concatenate(noise_chunks) if noise_chunks else None
    audio = _preprocess_audio(audio, noise_profile=noise_profile)

    audio_int16 = np.int16(
        audio * 32767
    )

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(
            audio_int16.tobytes()
        )
    buffer.seek(0)
    buffer.name = "command.wav"

    if SAVE_RECORDINGS:
        try:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            with open(OUTPUT_FILE, "wb") as f:
                f.write(buffer.getvalue())
            print(f"💾 Saved: {OUTPUT_FILE}")
        except Exception:
            pass

    return buffer


# ============================================================
# AUDIO PREPROCESSING
# ============================================================
# Gentle normalization and silence trimming to help Whisper.
# Avoids aggressive noise suppression that destroys speech
# frequencies — especially important for accented speech
# where consonant energy patterns differ from American English.

def _preprocess_audio(audio: np.ndarray, noise_profile: np.ndarray | None = None) -> np.ndarray:
    """
    Lightweight audio preprocessing for better STT accuracy.

    Removes steady background noise using a profile collected while waiting for
    speech, then trims silence and gently normalizes the remaining speech.
    """
    if audio.size == 0:
        return audio

    # Flatten to 1D if needed.
    if audio.ndim > 1:
        audio = audio[:, 0]

    # 1. Remove DC offset (mean subtraction).
    audio = audio - np.mean(audio)

    audio_settings = get_settings().get("audio", {})
    if audio_settings.get("noise_cancellation_enabled", True) and noise_profile is not None:
        audio = _reduce_background_noise(
            audio,
            noise_profile,
            strength=audio_settings.get("noise_reduction_strength", 0.75),
        )

    # 2. Trim leading and trailing silence.
    audio = _trim_silence(audio, threshold=0.008)

    if audio.size == 0:
        return audio

    # 3. Gentle peak normalization to -3 dB (≈ 0.707).
    #    This ensures consistent volume without clipping.
    peak = np.max(np.abs(audio))
    if peak > 0.001:
        target_peak = 0.707  # -3 dB
        audio = audio * (target_peak / peak)

    # Clip to [-1, 1] as a safety net.
    audio = np.clip(audio, -1.0, 1.0)

    return audio


def _reduce_background_noise(
    audio: np.ndarray,
    noise_profile: np.ndarray,
    strength: float = 0.75,
) -> np.ndarray:
    """Apply gentle spectral gating using recent non-speech microphone frames."""
    frame_size = 512
    hop_size = 128
    if audio.size < frame_size or noise_profile.size < frame_size // 2:
        return audio

    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    noise_profile = np.asarray(noise_profile, dtype=np.float32).reshape(-1)
    strength = float(np.clip(strength, 0.0, 1.0))
    if strength <= 0.0:
        return audio
    noise_profile = noise_profile - np.mean(noise_profile)

    window = np.hanning(frame_size).astype(np.float32)
    if noise_profile.size < frame_size:
        noise_profile = np.pad(noise_profile, (0, frame_size - noise_profile.size))
    noise_frames = np.stack([
        noise_profile[start:start + frame_size] * window
        for start in range(0, noise_profile.size - frame_size + 1, hop_size)
    ])
    noise_magnitude = np.mean(np.abs(np.fft.rfft(noise_frames, axis=1)), axis=0)

    pad = frame_size // 2
    padded = np.pad(audio, (pad, pad + hop_size), mode="constant")
    output = np.zeros_like(padded)
    weights = np.zeros_like(padded)
    for start in range(0, padded.size - frame_size + 1, hop_size):
        frame = padded[start:start + frame_size]
        spectrum = np.fft.rfft(frame * window)
        magnitude = np.abs(spectrum)
        gain = np.maximum(1.0 - strength * noise_magnitude / (magnitude + 1e-8), 0.18)
        filtered = np.fft.irfft(spectrum * gain, n=frame_size).astype(np.float32) * window
        output[start:start + frame_size] += filtered
        weights[start:start + frame_size] += window * window

    denoised = output / np.maximum(weights, 1e-6)
    return denoised[pad:pad + audio.size]


def _trim_silence(
    audio: np.ndarray,
    threshold: float = 0.008,
    frame_size: int = 512,
    min_speech_frames: int = 3,
) -> np.ndarray:
    """
    Trim leading and trailing silence from audio.

    Uses a sliding window of `frame_size` samples and considers
    frames with RMS energy above `threshold` as speech.
    Requires `min_speech_frames` consecutive speech frames before
    marking the start of speech (avoids triggering on clicks/pops).

    Keeps a small margin (1 frame) before the first and after the
    last speech frame to avoid cutting off plosives.
    """
    if audio.size < frame_size:
        return audio

    n_frames = len(audio) // frame_size
    energies = np.array([
        np.sqrt(np.mean(
            audio[i * frame_size : (i + 1) * frame_size] ** 2
        ))
        for i in range(n_frames)
    ])

    speech_mask = energies > threshold

    # Find first sustained speech region.
    start_frame = 0
    consecutive = 0
    for i, is_speech in enumerate(speech_mask):
        if is_speech:
            consecutive += 1
            if consecutive >= min_speech_frames:
                start_frame = max(0, i - min_speech_frames)
                break
        else:
            consecutive = 0

    # Find last speech frame.
    end_frame = n_frames
    consecutive = 0
    for i in range(len(speech_mask) - 1, -1, -1):
        if speech_mask[i]:
            consecutive += 1
            if consecutive >= 1:
                end_frame = min(n_frames, i + 2)  # +1 margin
                break
        else:
            consecutive = 0

    start_sample = start_frame * frame_size
    end_sample = min(len(audio), end_frame * frame_size)

    if end_sample <= start_sample:
        return audio  # Don't trim if result would be empty.

    return audio[start_sample:end_sample]
