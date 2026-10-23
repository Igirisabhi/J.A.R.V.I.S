import json
import sys

# Force UTF-8 output on Windows
if sys.stdout:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

if sys.stderr:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path

import soundfile as sf
from kokoro_onnx import Kokoro


# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

MODEL = (
    BASE_DIR
    / "models"
    / "kokoro"
    / "kokoro-v1.0.onnx"
)

VOICES = (
    BASE_DIR
    / "models"
    / "kokoro"
    / "voices-v1.0.bin"
)


# ============================================================
# LOAD KOKORO ONCE
# ============================================================

def load_kokoro():

    if not MODEL.exists():

        raise FileNotFoundError(
            f"Kokoro model not found: {MODEL}"
        )

    if not VOICES.exists():

        raise FileNotFoundError(
            f"Kokoro voices file not found: {VOICES}"
        )

    print(
        "⚡ Loading Kokoro model...",
        flush=True,
    )

    kokoro = Kokoro(
        str(MODEL),
        str(VOICES),
    )

    print(
        "✅ Kokoro model loaded.",
        flush=True,
    )

    return kokoro


# ============================================================
# MAIN WORKER
# ============================================================

def main():

    kokoro = load_kokoro()

    # Tell tts.py that the model is ready.
    print(
        json.dumps(
            {
                "type": "ready"
            }
        ),
        flush=True,
    )

    while True:

        line = sys.stdin.readline()

        if not line:
            break

        line = line.strip()

        if not line:
            continue

        try:

            request = json.loads(line)

        except json.JSONDecodeError as e:

            print(
                json.dumps(
                    {
                        "type": "error",
                        "error": f"Invalid JSON: {e}",
                    }
                ),
                flush=True,
            )

            continue

        request_type = request.get(
            "type"
        )

        # ----------------------------------------------------
        # SHUTDOWN
        # ----------------------------------------------------

        if request_type == "shutdown":

            print(
                json.dumps(
                    {
                        "type": "shutdown"
                    }
                ),
                flush=True,
            )

            break

        # ----------------------------------------------------
        # SPEAK
        # ----------------------------------------------------

        if request_type == "speak":

            request_id = request.get(
                "id"
            )

            text = str(
                request.get(
                    "text",
                    "",
                )
            ).strip()

            output_file = request.get(
                "output"
            )

            if not text:

                print(
                    json.dumps(
                        {
                            "type": "error",
                            "id": request_id,
                            "error": "Empty text",
                        }
                    ),
                    flush=True,
                )

                continue

            if not output_file:

                print(
                    json.dumps(
                        {
                            "type": "error",
                            "id": request_id,
                            "error": "Missing output file",
                        }
                    ),
                    flush=True,
                )

                continue

            try:

                output_path = Path(
                    output_file
                )

                output_path.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                # Kokoro is ALREADY loaded.
                # This is the important part.
                samples, sample_rate = (
                    kokoro.create(
                        text,
                        voice=str(request.get("voice") or "am_adam"),
                        speed=float(request.get("speed", 1.15)),
                        lang="en-us",
                    )
                )

                sf.write(
                    str(output_path),
                    samples,
                    sample_rate,
                )

                print(
                    json.dumps(
                        {
                            "type": "done",
                            "id": request_id,
                            "output": str(
                                output_path
                            ),
                        }
                    ),
                    flush=True,
                )

            except Exception as e:

                print(
                    json.dumps(
                        {
                            "type": "error",
                            "id": request_id,
                            "error": str(e),
                        }
                    ),
                    flush=True,
                )

            continue


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        main()

    except Exception as e:

        print(
            json.dumps(
                {
                    "type": "error",
                    "error": str(e),
                }
            ),
            flush=True,
        )

        raise
