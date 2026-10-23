import queue
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.audio_devices import input_stream
from voice.wake_word import BLOCK_SIZE, SAMPLE_RATE, WAKEWORD_MODEL_NAME, wake_model

print("🎤 Say: Hey Jarvis")
print("Press Ctrl+C to stop\n")

audio_queue = queue.Queue(maxsize=8)


def receive_audio(indata, _frames, _time_info, status):
    if status:
        print(f"\nMicrophone stream: {status}")
    audio = indata[:, 0].copy()
    try:
        audio_queue.put_nowait(audio)
    except queue.Full:
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
        try:
            audio = audio_queue.get(timeout=0.25)
        except queue.Empty:
            continue

        prediction = wake_model.predict(audio)
        score = prediction.get(WAKEWORD_MODEL_NAME, 0)
        print(f"\rWake score: {score:.4f}", end="")
