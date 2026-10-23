"""Manage provider API keys stored in the project's ignored .env file."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import get_key, set_key, unset_key


ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
API_STORAGE_NOTE_FILE = Path(__file__).resolve().parent.parent / ".api_storage.txt"
PROVIDER_KEYS = {
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
}
MAX_STORAGE_BYTES = 5 * 1024 * 1024


def _stored_values() -> dict[str, str]:
    return {
        provider: str(os.getenv(env_name) or get_key(str(ENV_FILE), env_name) or "")
        for provider, env_name in PROVIDER_KEYS.items()
    }


def key_status() -> dict[str, bool]:
    """Return configured flags only; never return secret values to the UI."""
    return {
        provider: bool(os.getenv(env_name) or get_key(str(ENV_FILE), env_name))
        for provider, env_name in PROVIDER_KEYS.items()
    }


def storage_info() -> dict[str, object]:
    """Return shared API vault and editable note usage without exposing keys."""
    sizes = {provider: len(value.encode("utf-8")) for provider, value in _stored_values().items()}
    note_bytes = len(read_storage_note().encode("utf-8"))
    return {
        "providers": sizes,
        "note_bytes": note_bytes,
        "used_bytes": sum(sizes.values()) + note_bytes,
        "max_bytes": MAX_STORAGE_BYTES,
    }


def read_storage_note() -> str:
    """Read the user's editable plain-text API storage note."""
    try:
        return API_STORAGE_NOTE_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def save_storage_note(text: str) -> dict[str, object]:
    """Persist a note, enforcing the same 5 MB allocation as provider keys."""
    value = str(text or "")
    key_bytes = sum(len(item.encode("utf-8")) for item in _stored_values().values())
    if key_bytes + len(value.encode("utf-8")) > MAX_STORAGE_BYTES:
        raise ValueError("API storage limit is 5 MB.")
    if value:
        API_STORAGE_NOTE_FILE.write_text(value, encoding="utf-8", newline="")
    else:
        API_STORAGE_NOTE_FILE.unlink(missing_ok=True)
    return storage_info()


def update_api_keys(updates: dict[str, str], clear: list[str] | None = None) -> dict[str, bool]:
    """Save submitted provider keys and refresh cached API clients in this process."""
    clear_set = {str(provider).strip().lower() for provider in (clear or [])}
    current_values = _stored_values()
    next_values = dict(current_values)
    for provider in PROVIDER_KEYS:
        if provider in clear_set:
            next_values[provider] = ""
        else:
            value = str(updates.get(provider, "") or "").strip()
            if value:
                next_values[provider] = value
    note_bytes = len(read_storage_note().encode("utf-8"))
    if sum(len(value.encode("utf-8")) for value in next_values.values()) + note_bytes > MAX_STORAGE_BYTES:
        raise ValueError("API storage limit is 5 MB.")

    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)

    for provider, env_name in PROVIDER_KEYS.items():
        if provider in clear_set:
            unset_key(str(ENV_FILE), env_name)
            os.environ.pop(env_name, None)
            continue
        value = str(updates.get(provider, "") or "").strip()
        if not value:
            continue
        set_key(str(ENV_FILE), env_name, value, quote_mode="always")
        os.environ[env_name] = value

    _refresh_cached_clients()
    return key_status()


def _refresh_cached_clients() -> None:
    """Drop clients that captured the previous environment key."""
    ai = sys.modules.get("core.ai")
    if ai is not None:
        ai.gemini = None
        ai.groq = None

    stt = sys.modules.get("voice.stt_groq")
    if stt is not None:
        stt._groq_client = None
        stt._gemini_client = None

    tts = sys.modules.get("voice.tts")
    if tts is not None:
        with tts._online_client_lock:
            tts._groq_tts_client = None
            tts._gemini_tts_client = None
