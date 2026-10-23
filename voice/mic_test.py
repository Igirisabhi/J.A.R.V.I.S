import numpy as np
import sounddevice as sd
import time
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.audio_devices import input_stream

SAMPLE_RATE = 16000
DURATION = 5

device_info = sd.query_devices(kind="input")

print(f"🎙️ Windows default microphone: {device_info['name']}")
print(f"🔊 Channels: {device_info['max_input_channels']}")
print(f"🎚️ Sample rate: {device_info['default_samplerate']}")

print("\n🎤 Speak for 5 seconds...")

try:
    audio_chunks = []

    def receive_audio(indata, _frames, _time_info, status):
        if status:
            print(f"⚠️ Microphone stream: {status}")
        audio_chunks.append(indata[:, 0].copy())

    with input_stream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        callback=receive_audio,
    ):
        time.sleep(DURATION)
except sd.PortAudioError as error:
    raise SystemExit(f"❌ Microphone could not be opened: {error}") from error

if not audio_chunks:
    raise SystemExit("❌ Microphone produced no audio frames.")

audio = np.concatenate(audio_chunks)
volume = np.max(np.abs(audio))

print(f"Peak volume: {volume:.4f}")

if volume > 0.01:
    print("✅ Microphone is working.")
else:
    print("❌ Very little audio detected.")
