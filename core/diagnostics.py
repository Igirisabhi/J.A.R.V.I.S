"""Non-destructive runtime diagnostics for Windows deployments."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _check(name: str, ok: bool, detail: str, *, required: bool = True) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail, "required": required}


def run_diagnostics() -> list[dict]:
    results = [
        _check(
            "Python",
            sys.version_info[:2] == (3, 12),
            f"{sys.version.split()[0]} (recommended: 3.12)",
        ),
        _check("Project root", ROOT.is_dir(), str(ROOT)),
        _check("Configuration directory", (ROOT / "config").is_dir(), str(ROOT / "config")),
    ]
    config_dir = ROOT / "config"
    try:
        probe = config_dir / ".jarvis-write-test"
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
        results.append(_check("Configuration writable", True, str(config_dir)))
    except OSError as error:
        results.append(_check("Configuration writable", False, str(error)))
    try:
        log_path = ROOT / "runtime" / "jarvis.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8"):
            pass
        results.append(_check("Log path writable", True, str(log_path)))
    except OSError as error:
        results.append(_check("Log path writable", False, str(error)))

    for relative in (
        "config/settings.json",
        "config/ui_settings.json",
        "config/gesture_settings.json",
        "config/command_corrections.json",
        "aliases.json",
    ):
        path = ROOT / relative
        try:
            with path.open("r", encoding="utf-8-sig") as source:
                value = json.load(source)
            results.append(_check(relative, isinstance(value, (dict, list)), "valid JSON"))
        except (OSError, json.JSONDecodeError) as error:
            results.append(_check(relative, False, str(error)))

    for relative in ("models/gesture_recognizer.task", "models/kokoro/kokoro-v1.0.onnx"):
        path = ROOT / relative
        results.append(_check(relative, path.is_file() and path.stat().st_size > 0, "model present"))

    for package in ("numpy", "sounddevice", "mediapipe", "PySide6"):
        try:
            version = importlib.metadata.version(package)
            results.append(_check(f"Package: {package}", True, version))
        except importlib.metadata.PackageNotFoundError:
            results.append(_check(f"Package: {package}", False, "not installed"))

    try:
        from core.audio_devices import list_input_devices
        inputs = list_input_devices()
        if inputs:
            detail = "; ".join(
                f"{item['name']} ({item['default_samplerate']:.0f} Hz, {item['channels']} ch)"
                for item in inputs[:5]
            )
            if len(inputs) > 5:
                detail += f"; +{len(inputs) - 5} more"
        else:
            detail = "No input devices detected; check Windows privacy and default-device settings."
        results.append(_check("Microphone", bool(inputs), detail))
    except Exception as error:
        results.append(_check("Microphone", False, str(error)))

    try:
        import cv2
        camera = None
        available = False
        frame_read = False
        for backend in (cv2.CAP_DSHOW, cv2.CAP_ANY):
            camera = cv2.VideoCapture(0, backend)
            if camera.isOpened():
                available = True
                frame_read, _ = camera.read()
                camera.release()
                break
            camera.release()
        detail = (
            "default camera available and returning frames"
            if frame_read
            else "camera opened but did not return a frame"
            if available
            else "not available; check camera permissions, index, or another app using it"
        )
        results.append(_check("Camera", frame_read, detail, required=False))
    except ImportError:
        results.append(_check("Camera", False, "OpenCV is not installed", required=False))
    except Exception as error:
        results.append(_check("Camera", False, str(error), required=False))

    for key in ("GROQ_API_KEY", "GEMINI_API_KEY"):
        results.append(_check(f"Environment: {key}", bool(os.environ.get(key)), "configured" if os.environ.get(key) else "not configured", required=False))
    return results


def format_diagnostics(results: list[dict]) -> str:
    lines = ["JARVIS DIAGNOSTICS", "=" * 60]
    for item in results:
        status = "PASS" if item["ok"] else ("WARN" if not item["required"] else "FAIL")
        lines.append(f"[{status:<4}] {item['name']}: {item['detail']}")
    failures = sum(not item["ok"] and item["required"] for item in results)
    warnings = sum(not item["ok"] and not item["required"] for item in results)
    lines.extend(("", f"Summary: {failures} required failure(s), {warnings} warning(s)"))
    return "\n".join(lines)


def diagnostics_exit_code(results: list[dict]) -> int:
    return 1 if any(not item["ok"] and item["required"] for item in results) else 0
