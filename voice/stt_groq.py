import atexit
import io
import json
import os
import sys
import wave
import logging
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

import httpx
from dotenv import load_dotenv
from groq import Groq
from voice.recorder import record_command
from core.settings import get_settings

# Silence harmless AFC recommendation logs from google-genai
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

load_dotenv()

_speech_settings = get_settings()["speech"]
_online_settings = _speech_settings["online"]

# Persistent connection pool with 5-minute keepalive avoids
# repeated TLS handshake latency (saving ~400-800ms per request).
_http_client = httpx.Client(
    timeout=_online_settings.get("timeout_seconds", 5.0),
    limits=httpx.Limits(
        max_keepalive_connections=_online_settings.get("keepalive_connections", 5),
        max_connections=_online_settings.get("max_connections", 10),
        keepalive_expiry=_online_settings.get("keepalive_expiry_seconds", 300.0),
    ),
)
atexit.register(_http_client.close)

_groq_client = None
_gemini_client = None
_warmed = False


def _get_groq_client():
    global _groq_client
    if _groq_client is None:
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY is not configured.")
        _groq_client = Groq(api_key=api_key, http_client=_http_client)
    return _groq_client


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured.")
        from google import genai
        from google.genai import types as genai_types
        timeout_ms = int(_online_settings.get("timeout_seconds", 5.0) * 1000)
        _gemini_client = genai.Client(
            api_key=api_key,
            http_options=genai_types.HttpOptions(timeout=timeout_ms),
        )
    return _gemini_client


def warm_stt_connection():
    """Warm up the Groq HTTP connection in the background so the first command has zero handshake latency."""
    global _warmed
    if _warmed:
        return
    _warmed = True

    def _warm():
        try:
            if os.getenv("GROQ_API_KEY"):
                _http_client.get("https://api.groq.com/openai/v1/models", timeout=3.0)
        except Exception:
            pass

    import threading
    threading.Thread(target=_warm, name="JarvisSTTWarmup", daemon=True).start()


# Trigger warm-up non-blockingly
warm_stt_connection()


# ============================================================
# WHISPER VOCABULARY PROMPT
# ============================================================
# Concise, targeted prompt under 80 tokens to ensure fast decoding
# and accurate recognition of domain keywords, Bad Apple, and profiles.

def get_whisper_prompt() -> str:
    """Build a concise, targeted Whisper prompt including dynamic browser profiles and keywords."""
    profile_keywords = ["Bakka", "Chrome profile Bakka", "open Bakka", "profile Bakka"]
    try:
        from tools.browser_profiles import get_browser_profiles
        profiles = get_browser_profiles("chrome")
        for p in profiles.values():
            p_name = p.get("name")
            if p_name and p_name not in profile_keywords:
                profile_keywords.append(p_name)
    except Exception:
        pass

    known_base = {
        "jarvis", "bad apple", "notepad", "calculator", "paint", "file explorer",
        "task manager", "chrome", "edge", "brave", "vs code", "spotify", "discord",
        "vlc", "whatsapp", "youtube", "chatgpt", "scroll", "scroll down", "scroll up",
        "scroll to top", "scroll to bottom", "page down", "page up", "maximize",
        "minimize", "restore", "close", "open", "play", "pause", "stop", "search",
        "back", "sleep", "bakka", "youtube music", "scroll down more",
        "scroll down a little", "scroll up more", "scroll up a little",
        "maximize window", "maximize youtube", "minimize window", "restore window",
        "close window", "switch window", "click the first link", "click the 2nd link",
    }
    custom_words = []
    corr_file = Path(__file__).resolve().parent.parent / "config" / "command_corrections.json"
    if corr_file.exists():
        try:
            with corr_file.open("r", encoding="utf-8-sig") as f:
                cdata = json.load(f)
            if isinstance(cdata, dict):
                corr = cdata.get("corrections", {})
                if isinstance(corr, dict):
                    for val in corr.values():
                        if isinstance(val, str):
                            v_clean = val.strip()
                            if v_clean and v_clean.lower() not in known_base and v_clean not in custom_words and len(v_clean) < 30:
                                custom_words.append(v_clean)
        except Exception:
            pass
    try:
        from core.database import load_voice_commands
        for phrase in load_voice_commands():
            if phrase.lower() not in known_base and phrase not in custom_words and len(phrase) < 40:
                custom_words.append(phrase)
    except (OSError, RuntimeError, ValueError):
        pass

    prof_str = ", ".join(profile_keywords)
    extra_str = f" {', '.join(custom_words[:25])}." if custom_words else ""
    return (
        "Transcribe English spoken with an Indian accent accurately. "
        "Jarvis, Hey Jarvis. Bad Apple, play Bad Apple, search Bad Apple. "
        f"Profile, Chrome profile, {prof_str}. "
        "Notepad, Calculator, Paint, File Explorer, Task Manager, Chrome, Edge, Brave, "
        "VS Code, Spotify, Discord, VLC, WhatsApp, YouTube, ChatGPT. "
        "Open, close, switch to, show, minimize, maximize, maximize it, maximize window, restore, play, pause, stop, search. "
        "Scroll, scroll down, scroll up, scroll to top, scroll to bottom, page down, page up."
        f"{extra_str} "
        "What time is it, date, sleep."
    )


WHISPER_PROMPT = get_whisper_prompt()


def _format_error(e: Exception) -> str:
    status = getattr(e, "status_code", None)
    body = getattr(e, "body", None)
    detail = getattr(e, "message", None)
    if not detail and isinstance(body, dict):
        error_body = body.get("error", body)
        if isinstance(error_body, dict):
            detail = error_body.get("message")
    diagnostic = f"HTTP {status}" if status else e.__class__.__name__
    if detail:
        diagnostic += f": {' '.join(str(detail).split())[:200]}"
    return diagnostic


def _ensure_wav_bytes(audio_data: bytes) -> bytes:
    """Ensure audio bytes contain a valid RIFF/WAV header (16kHz mono 16-bit)."""
    if audio_data.startswith(b"RIFF"):
        return audio_data
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(audio_data)
    return buf.getvalue()


# ============================================================
# GROQ TRANSCRIBE
# ============================================================

def _transcribe_groq(audio_data: bytes, filename: str) -> str:
    groq_client = _get_groq_client()
    model = _online_settings.get("groq_model") or _online_settings.get("model", "whisper-large-v3-turbo")
    print(f"⚡ STT → Groq ({model})")
    result = groq_client.audio.transcriptions.create(
        file=(filename, audio_data),
        model=model,
        language="en",
        temperature=0,
        response_format="text",
        prompt=get_whisper_prompt(),
    )
    if isinstance(result, str):
        return result.strip()
    return getattr(result, "text", str(result)).strip()


# ============================================================
# GEMINI TRANSCRIBE
# ============================================================

def _transcribe_gemini(audio_data: bytes, filename: str) -> str:
    gemini_client = _get_gemini_client()
    from google.genai import types as genai_types

    wav_data = _ensure_wav_bytes(audio_data)
    model = _online_settings.get("gemini_model", "gemini-2.0-flash")
    print(f"🧠 STT → Gemini ({model})")
    response = gemini_client.models.generate_content(
        model=model,
        contents=[
            genai_types.Part.from_bytes(data=wav_data, mime_type="audio/wav"),
            (
                "Transcribe the spoken English words in this audio exactly. "
                "Output ONLY the transcribed words. "
                "Do not add quotes, markdown formatting, commentary, or conversational filler. "
                "If there is no speech or only silence/noise, output empty string."
            ),
        ],
        config={
            "temperature": 0.0,
        },
    )
    raw = getattr(response, "text", "") or ""
    text = raw.strip()
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1].strip()
    lower_text = text.lower().rstrip(".,!?")
    if lower_text in ("none", "silent", "silence", "no speech", "no speech detected", "unintelligible", "empty", "no audio"):
        return ""
    return text


# ============================================================
# TRANSCRIBE (HIERARCHY: GROQ -> GEMINI -> OFFLINE)
# ============================================================

def transcribe(audio_source) -> str:
    """
    STT Hierarchy:
    1. Online Primary: Groq Whisper
    2. Online Secondary: Gemini STT
    3. Offline Fallback: faster-whisper (local CPU/int8)

    Supports file path or direct in-memory BytesIO/bytes to avoid disk I/O.
    """
    get_settings()  # Refresh online model, enablement, and timeout values after edits.
    _http_client.timeout = _online_settings.get("timeout_seconds", 15.0)
    if isinstance(audio_source, io.BytesIO):
        audio_data = audio_source.getvalue()
        filename = getattr(audio_source, "name", "command.wav")
    elif isinstance(audio_source, (bytes, bytearray)):
        audio_data = bytes(audio_source)
        filename = "command.wav"
    else:
        path = Path(audio_source)
        with path.open("rb") as file:
            audio_data = file.read()
        filename = path.name

    try:
        from core.debug_logger import log_voice_pipeline
        log_voice_pipeline("STT", "Audio sent to STT", bytes=len(audio_data), filename=filename)
    except ImportError:
        pass

    if _online_settings.get("enabled", True):
        # Tier 1: Groq Whisper
        if _online_settings.get("groq_enabled", True) and os.getenv("GROQ_API_KEY"):
            try:
                res = _transcribe_groq(audio_data, filename)
                try:
                    from core.debug_logger import log_voice_pipeline
                    log_voice_pipeline("STT", "Groq transcript received", transcript=res, is_empty=not bool(res))
                except ImportError:
                    pass
                if res:
                    return res
            except Exception as e:
                diag = _format_error(e)
                try:
                    from core.debug_logger import log_voice_pipeline
                    log_voice_pipeline("STT", f"Groq STT failed: {diag}; falling back to Gemini")
                except ImportError:
                    pass
                print(f"⚠️ Groq STT failed ({diag}), falling back to Gemini...")

        # Tier 2: Gemini STT
        if _online_settings.get("gemini_enabled", True) and os.getenv("GEMINI_API_KEY"):
            try:
                res = _transcribe_gemini(audio_data, filename)
                try:
                    from core.debug_logger import log_voice_pipeline
                    log_voice_pipeline("STT", "Gemini transcript received", transcript=res, is_empty=not bool(res))
                except ImportError:
                    pass
                if res:
                    return res
            except Exception as e:
                diag = _format_error(e)
                try:
                    from core.debug_logger import log_voice_pipeline
                    log_voice_pipeline("STT", f"Gemini STT failed: {diag}; falling back to offline STT")
                except ImportError:
                    pass
                print(f"⚠️ Gemini STT failed ({diag}), falling back to offline STT...")

    # Tier 3: Local Offline faster-whisper Fallback
    print("📦 STT → Offline (faster-whisper)")
    try:
        from core.debug_logger import log_voice_pipeline
        log_voice_pipeline("STT", "Audio sent to offline faster-whisper")
    except ImportError:
        pass
    from voice.stt_offline import transcribe_offline
    if not _speech_settings.get("local", {}).get("enabled", True):
        return ""
    if isinstance(audio_source, io.BytesIO):
        audio_source.seek(0)
    res = transcribe_offline(audio_source)
    try:
        from core.debug_logger import log_voice_pipeline
        log_voice_pipeline("STT", "Offline transcript received", transcript=res, is_empty=not bool(res))
    except ImportError:
        pass
    return res


# ============================================================
# SELF-TEST
# ============================================================

if __name__ == "__main__":
    audio = record_command()

    print("🧠 Transcribing...")

    text = transcribe(audio)

    print(f"\n🗣️ You said: {text}")
