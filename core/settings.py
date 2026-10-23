"""Small, shared settings loader for the Jarvis core."""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = PROJECT_ROOT / "config" / "settings.json"

DEFAULT_SETTINGS = {
    "audio": {
        "tts_volume": 0.40,
        "energy_threshold": 0.015,
        "speech_vad_threshold": 0.35,
        "silence_duration_seconds": 0.75,
        "max_recording_seconds": 10.0,
        "interrupt_threshold": 0.015,
        "interrupt_min_speech_seconds": 0.12,
        "interrupt_silence_seconds": 0.65,
        "max_interrupt_seconds": 10.0,
        "pre_roll_seconds": 0.35,
        "noise_cancellation_enabled": True,
        "noise_reduction_strength": 0.75,
        "post_tts_settle_seconds": 0.05,
        "mic_device": "default",
    },
    "voice": {
        "wake_threshold": 0.24,
        "required_detections": 2,
        "detection_window_seconds": 1.5,
        "post_wake_speech_window": 0.6,
        "wake_words": ["hey_jarvis"],
    },
    "speech": {
        "online": {
            "enabled": True,
            "groq_enabled": True,
            "groq_model": "whisper-large-v3-turbo",
            "gemini_enabled": True,
            "gemini_model": "gemini-3.8-flash",
            "model": "whisper-large-v3-turbo",
            "timeout_seconds": 15.0,
            "keepalive_connections": 5,
            "max_connections": 10,
            "keepalive_expiry_seconds": 300.0,
        },
        "local": {
            "enabled": True,
            "preload_on_startup": False,
            "preload_delay_seconds": 0.6,
            "model": "tiny",
            "device": "cpu",
            "compute_type": "int8",
            "cpu_threads": 2,
            "workers": 1,
            "beam_size": 1,
            "vad_filter": True,
            "vad_min_silence_ms": 300,
        },
        "tts": {
            "voice_profile": "jarvis",
            "online_enabled": True,
            "groq_enabled": True,
            "groq_model": "canopylabs/orpheus-v1-english",
            "groq_voice": "troy",
            "gemini_enabled": True,
            "gemini_model": "gemini-3.8-flash-lite-tts",
            "gemini_voice": "Kore",
            "local_voice": "am_adam",
            "speech_rate": 1.15,
            "timeout_seconds": 10.0,
            "local_fallback_enabled": True,
        },
    },
    "workspace_dir": None,
    "browser_path": "",
    "continuous_mode": True,
    "stop_on_error": True,
    "idle_timeout_ms": 30000,
    "app_allowlist": ["code", "vs code", "visual studio code", "notepad", "calculator", "calc", "paint", "file explorer", "explorer", "chrome", "google chrome", "edge", "microsoft edge", "brave", "brave browser", "spotify", "discord", "steam", "vlc", "vlc media player", "video lan", "videolan", "whatsapp", "task manager", "settings"],
    "macros": {},
    "ui_scale": 1.0,
    "accent": "#4FD8FF",
    "reduce_motion": False,
    "wheel_dismiss_ms": 5000,
    "hud_ms": 1400,
}


def _merge_known(defaults, supplied):
    """Merge only declared settings so typos never silently become controls."""
    merged = copy.deepcopy(defaults)
    if not isinstance(supplied, dict):
        return merged
    for key, value in supplied.items():
        if key == "device" and "mic_device" in defaults:
            merged["mic_device"] = value
        elif key not in defaults:
            LOGGER.warning("Ignoring unknown Jarvis setting: %s", key)
        elif isinstance(defaults[key], dict):
            merged[key] = _merge_known(defaults[key], value)
        else:
            merged[key] = value
    return merged


def _bounded_number(value, fallback, minimum, maximum, integer=False):
    try:
        number = float(value)
        if number != number:  # NaN
            raise ValueError
        number = max(minimum, min(maximum, number))
        return int(number) if integer else number
    except (TypeError, ValueError, OverflowError):
        return fallback


def _boolean(value, fallback):
    return value if isinstance(value, bool) else fallback


def _normalize(settings):
    workspace = settings.get("workspace_dir")
    settings["workspace_dir"] = os.path.expandvars(os.path.expanduser(workspace.strip())) if isinstance(workspace, str) and workspace.strip() else None
    browser_path = settings.get("browser_path")
    settings["browser_path"] = os.path.expandvars(os.path.expanduser(browser_path.strip())) if isinstance(browser_path, str) and browser_path.strip() else ""
    settings["continuous_mode"] = _boolean(settings.get("continuous_mode"), True)
    settings["stop_on_error"] = _boolean(settings.get("stop_on_error"), True)
    settings["idle_timeout_ms"] = _bounded_number(settings.get("idle_timeout_ms", 30000), 30000, 1000, 300000, True)
    if not isinstance(settings.get("app_allowlist"), list):
        settings["app_allowlist"] = copy.deepcopy(DEFAULT_SETTINGS["app_allowlist"])
    settings["app_allowlist"] = [str(app).strip().lower() for app in settings["app_allowlist"] if str(app).strip()]
    if not isinstance(settings.get("macros"), dict):
        settings["macros"] = {}
    settings["ui_scale"] = _bounded_number(settings.get("ui_scale", 1.0), 1.0, 0.5, 2.0)
    settings["accent"] = settings.get("accent") if isinstance(settings.get("accent"), str) and settings["accent"].startswith("#") else "#4FD8FF"
    settings["reduce_motion"] = _boolean(settings.get("reduce_motion"), False)
    settings["wheel_dismiss_ms"] = _bounded_number(settings.get("wheel_dismiss_ms", 5000), 5000, 500, 30000, True)
    settings["hud_ms"] = _bounded_number(settings.get("hud_ms", 1400), 1400, 250, 10000, True)
    audio = settings["audio"]
    audio["mic_device"] = str(audio.get("mic_device", "default") or "default").strip()
    audio["noise_cancellation_enabled"] = _boolean(audio.get("noise_cancellation_enabled"), True)
    for key, default, low, high in (
        ("tts_volume", 0.40, 0.0, 1.0),
        ("energy_threshold", 0.015, 0.001, 0.5),
        ("speech_vad_threshold", 0.35, 0.05, 0.95),
        ("silence_duration_seconds", 0.65, 0.1, 5.0),
        ("max_recording_seconds", 10.0, 1.0, 60.0),
        ("interrupt_threshold", 0.015, 0.001, 0.5),
        ("interrupt_min_speech_seconds", 0.12, 0.02, 2.0),
        ("interrupt_silence_seconds", 0.65, 0.1, 5.0),
        ("max_interrupt_seconds", 10.0, 1.0, 60.0),
        ("pre_roll_seconds", 0.35, 0.0, 2.0),
        ("noise_reduction_strength", 0.75, 0.0, 1.0),
        ("post_tts_settle_seconds", 0.05, 0.0, 2.0),
    ):
        audio[key] = _bounded_number(audio[key], default, low, high)

    voice = settings["voice"]
    voice["wake_threshold"] = _bounded_number(voice["wake_threshold"], 0.24, 0.01, 1.0)
    voice["required_detections"] = _bounded_number(voice["required_detections"], 2, 1, 10, True)
    voice["detection_window_seconds"] = _bounded_number(
        voice["detection_window_seconds"], 1.5, 0.1, 10.0
    )
    voice["post_wake_speech_window"] = _bounded_number(
        voice.get("post_wake_speech_window", 0.6), 0.6, 0.1, 5.0
    )
    supported_wake_words = {"hey_jarvis", "alexa", "hey_mycroft", "hey_rhasspy", "timer", "weather"}
    wake_words = voice.get("wake_words", ["hey_jarvis"])
    if not isinstance(wake_words, list):
        wake_words = ["hey_jarvis"]
    voice["wake_words"] = list(dict.fromkeys(
        str(word).strip().casefold() for word in wake_words
        if str(word).strip().casefold() in supported_wake_words
    )) or ["hey_jarvis"]

    online = settings["speech"]["online"]
    online["enabled"] = _boolean(online["enabled"], True)
    if not isinstance(online["model"], str) or not online["model"].strip():
        online["model"] = "whisper-large-v3-turbo"
    else:
        online["model"] = online["model"].strip()
    online["timeout_seconds"] = _bounded_number(online["timeout_seconds"], 15.0, 1.0, 120.0)
    online["keepalive_connections"] = _bounded_number(
        online["keepalive_connections"], 5, 1, 50, True
    )
    online["max_connections"] = _bounded_number(online["max_connections"], 10, 1, 100, True)
    online["keepalive_expiry_seconds"] = _bounded_number(
        online["keepalive_expiry_seconds"], 300.0, 1.0, 3600.0
    )

    local = settings["speech"]["local"]
    local["enabled"] = _boolean(local["enabled"], True)
    local["preload_on_startup"] = _boolean(local["preload_on_startup"], False)
    local["preload_delay_seconds"] = _bounded_number(
        local["preload_delay_seconds"], 0.6, 0.0, 60.0
    )
    local["model"] = local["model"] if isinstance(local["model"], str) and local["model"] in {
        "tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"
    } else "base"
    local["device"] = local["device"] if isinstance(local["device"], str) and local["device"] in {
        "cpu", "cuda", "auto"
    } else "cpu"
    local["compute_type"] = local["compute_type"] if isinstance(local["compute_type"], str) and local["compute_type"] in {
        "auto", "int8", "int8_float32", "int8_float16", "float16", "float32"
    } else "int8"
    local["cpu_threads"] = _bounded_number(local["cpu_threads"], 2, 1, 64, True)
    local["workers"] = _bounded_number(local["workers"], 1, 1, 8, True)
    local["beam_size"] = _bounded_number(local["beam_size"], 1, 1, 5, True)
    local["vad_filter"] = _boolean(local["vad_filter"], True)
    local["vad_min_silence_ms"] = _bounded_number(
        local["vad_min_silence_ms"], 300, 50, 3000, True
    )

    tts = settings["speech"]["tts"]
    tts["voice_profile"] = (
        tts["voice_profile"].strip().casefold()
        if isinstance(tts.get("voice_profile"), str)
        else "jarvis"
    )
    if tts["voice_profile"] not in {"jarvis", "friday", "us_navy"}:
        tts["voice_profile"] = "jarvis"
    tts["online_enabled"] = _boolean(tts["online_enabled"], True)
    tts["groq_enabled"] = _boolean(tts["groq_enabled"], True)
    tts["gemini_enabled"] = _boolean(tts["gemini_enabled"], True)
    tts["local_fallback_enabled"] = _boolean(
        tts["local_fallback_enabled"], True
    )
    for key, default_text in (
        ("groq_model", "canopylabs/orpheus-v1-english"),
        ("groq_voice", "troy"),
        ("gemini_model", "gemini-3.8-flash-lite-tts"),
        ("gemini_voice", "Kore"),
        ("local_voice", "am_adam"),
    ):
        if not isinstance(tts[key], str) or not tts[key].strip():
            tts[key] = default_text
        else:
            tts[key] = tts[key].strip()
    tts["timeout_seconds"] = _bounded_number(
        tts["timeout_seconds"], 10.0, 1.0, 120.0
    )
    tts["speech_rate"] = _bounded_number(tts["speech_rate"], 1.15, 0.75, 1.5)

    return settings


def load_settings():
    """Load editable project settings; fall back safely when the JSON is damaged."""
    try:
        with SETTINGS_FILE.open("r", encoding="utf-8-sig") as stream:
            supplied = json.load(stream)
        settings = _merge_known(DEFAULT_SETTINGS, supplied)
    except FileNotFoundError:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        LOGGER.warning("Settings file missing; using defaults: %s", SETTINGS_FILE)
    except (OSError, json.JSONDecodeError) as exc:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        LOGGER.error("Could not read settings file; using defaults: %s", exc)

    return _normalize(settings)


_SETTINGS = load_settings()
_SETTINGS_MTIME_NS = SETTINGS_FILE.stat().st_mtime_ns if SETTINGS_FILE.exists() else None
_SETTINGS_LOCK = threading.RLock()
_SETTINGS_LAST_CHECK = 0.0


def _update_in_place(target, source):
    """Refresh shared settings dictionaries without invalidating cached references."""
    for key in tuple(target):
        if key not in source:
            del target[key]
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _update_in_place(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def get_settings():
    """Return current settings, reloading valid file edits without restarting."""
    global _SETTINGS_MTIME_NS, _SETTINGS_LAST_CHECK
    now = time.monotonic()
    if now - _SETTINGS_LAST_CHECK < 0.25:
        return _SETTINGS
    _SETTINGS_LAST_CHECK = now
    try:
        mtime_ns = SETTINGS_FILE.stat().st_mtime_ns
    except OSError:
        return _SETTINGS

    if mtime_ns != _SETTINGS_MTIME_NS:
        with _SETTINGS_LOCK:
            try:
                mtime_ns = SETTINGS_FILE.stat().st_mtime_ns
                if mtime_ns != _SETTINGS_MTIME_NS:
                    with SETTINGS_FILE.open("r", encoding="utf-8-sig") as stream:
                        supplied = json.load(stream)
                    if not isinstance(supplied, dict):
                        raise ValueError("The root of settings.json must be an object.")
                    refreshed = _normalize(_merge_known(DEFAULT_SETTINGS, supplied))
                    _update_in_place(_SETTINGS, refreshed)
                    _SETTINGS_MTIME_NS = mtime_ns
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                LOGGER.warning("Settings edit is not valid yet; keeping current settings: %s", exc)
    return _SETTINGS


def save_settings(updates: dict) -> dict:
    """Persist setting updates atomically and refresh shared settings in place."""
    global _SETTINGS_MTIME_NS
    with _SETTINGS_LOCK:
        merged = copy.deepcopy(_SETTINGS)
        merged.update(copy.deepcopy(updates))
        merged = _normalize(_merge_known(DEFAULT_SETTINGS, merged))
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="settings-", suffix=".json", dir=SETTINGS_FILE.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(merged, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary, SETTINGS_FILE)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        _update_in_place(_SETTINGS, merged)
        _SETTINGS_MTIME_NS = SETTINGS_FILE.stat().st_mtime_ns
        return _SETTINGS
