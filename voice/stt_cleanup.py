"""
Lightweight post-transcription cleanup for Indian English STT.

Fixes common Whisper misrecognitions of app names, technical terms,
and Jarvis commands. User aliases are loaded from a local JSON file.
No LLM call or network request is made.

Rules:
  1. Only correct when the replacement is clearly safe (whole-word
     or phrase-level match, never a substring inside a longer word).
  2. Preserve the rest of the transcript unchanged.
  3. Keep correction fast and deterministic.
"""

import json
import re
import sqlite3
import threading
from pathlib import Path


# ============================================================
# WHOLE-WORD CORRECTIONS
# ============================================================
# key   = misheard text (lowercase)
# value = corrected text (preserves original casing for display)
#
# These are applied as whole-word replacements, so "no pad"
# won't trigger inside "writing no padding".

_WORD_CORRECTIONS: dict[str, str] = {
    # ---- App / service names ----
    "no pad":           "Notepad",
    "not pad":          "Notepad",
    "note pad":         "Notepad",
    "know pad":         "Notepad",

    "you tube":         "YouTube",
    "u tube":           "YouTube",
    "utube":            "YouTube",

    "you tube music":   "YouTube Music",
    "youtube music":    "YouTube Music",

    "v s code":         "VS Code",
    "vs court":         "VS Code",
    "v s court":        "VS Code",
    "vias code":        "VS Code",
    "visa code":        "VS Code",
    "vscode":           "VS Code",

    "chrome":           "Chrome",
    "crome":            "Chrome",
    "krome":            "Chrome",

    "brave":            "Brave",
    "brave browser":    "Brave Browser",
    "microsoft edge":   "Microsoft Edge",
    "microsft edge":    "Microsoft Edge",
    "visual studio code": "Visual Studio Code",
    "file explorer":    "File Explorer",
    "task manager":     "Task Manager",
    "calculator":       "Calculator",

    "what's app":       "WhatsApp",
    "what sapp":        "WhatsApp",
    "whats app":        "WhatsApp",
    "watsapp":          "WhatsApp",

    "spot ify":         "Spotify",
    "spotfy":           "Spotify",
    "spotify":          "Spotify",

    "dis cord":         "Discord",
    "disk cord":        "Discord",

    "git hub":          "GitHub",
    "get hub":          "GitHub",

    "g mail":           "Gmail",
    "ji mail":          "Gmail",
    "g-mail":           "Gmail",

    "samsung":          "Samsung",
    "sam sung":         "Samsung",

    "gemini":           "Gemini",
    "gem ini":          "Gemini",
    "jimini":           "Gemini",
    "jiminy":           "Gemini",

    "groc":             "Groq",

    "vlc":              "VLC",
    "v l c":            "VLC",
    "kgf":              "KGF",
    "kjf":              "KGF",

    "python":           "Python",
    "pie thon":         "Python",
    "pie ton":          "Python",

    "instagram":        "Instagram",
    "insta gram":       "Instagram",
    "facebook":         "Facebook",
    "face book":        "Facebook",
    "linkedin":         "LinkedIn",
    "linked in":        "LinkedIn",

    "reddit":           "Reddit",

    # ---- Anime / media (user-specific) ----
    "black clover":     "Black Clover",
    "blackclover":      "Black Clover",
    "bad apple":        "Bad Apple",
    "pad apple":        "Bad Apple",
    "bad app":          "Bad Apple",
    "bad able":         "Bad Apple",
    "bed apple":        "Bad Apple",
    "bat apple":        "Bad Apple",
    "dad apple":        "Bad Apple",
    "that apple":       "Bad Apple",
    "bad apples":       "Bad Apple",
    "pad apples":       "Bad Apple",

    # ---- Browser Profiles / User names ----
    "backup":           "Bakka",
    "back up":          "Bakka",
    "baka":             "Bakka",
    "baca":             "Bakka",
    "bacca":            "Bakka",
    "baggage":          "Bakka",
    "backup profile":   "Bakka profile",
    "back up profile":  "Bakka profile",
    "open backup":      "open Bakka",
    "open back up":     "open Bakka",

    # ---- Jarvis itself ----
    "jarvis":           "Jarvis",
    "jervis":           "Jarvis",
    "jarves":           "Jarvis",
    "javis":            "Jarvis",
    "jaavis":           "Jarvis",
    "jaaves":           "Jarvis",
    "hey jaavis":       "Hey Jarvis",
    "hey javis":        "Hey Jarvis",

    # ---- ChatGPT accent variants ----
    "chat gpt":         "ChatGPT",
    "chat g p t":       "ChatGPT",
    "chatgbt":          "ChatGPT",
    "chat gbt":         "ChatGPT",
    "chatgtp":          "ChatGPT",
    "chat gtp":         "ChatGPT",
    "jet gpt":          "ChatGPT",
    "jat gpt":          "ChatGPT",
    "chad gpt":         "ChatGPT",
    "chat jpt":         "ChatGPT",
    "chat jbt":         "ChatGPT",
    "chat gpd":         "ChatGPT",
    "chat gp":          "ChatGPT",
    "chatgpt":          "ChatGPT",

    # ---- Stop / Exit commands ----
    "stap":             "stop",
    "stawp":            "stop",

    # ---- Command verbs / navigation ----
    "wopan":            "open",
    "goto":             "go to",
}

# Sort by length descending so multi-word patterns are matched first.
_SORTED_CORRECTIONS = sorted(
    _WORD_CORRECTIONS.items(),
    key=lambda kv: len(kv[0]),
    reverse=True,
)

def _compile_word_pattern(text: str) -> re.Pattern:
    """Compile a pattern matching words with flexible inter-word whitespace or commas."""
    words = [re.escape(w) for w in re.split(r"[\s,]+", text.strip()) if w]
    if not words:
        return re.compile(r"$^")
    return re.compile(r"(?<!\w)" + r"[\s,]+".join(words) + r"(?!\w)", flags=re.IGNORECASE)


# Pre-compile regex patterns for each correction.
_COMPILED_CORRECTIONS: list[tuple[re.Pattern, str]] = []
for _wrong, _right in _SORTED_CORRECTIONS:
    _COMPILED_CORRECTIONS.append((_compile_word_pattern(_wrong), _right))


_CUSTOM_CORRECTIONS_FILE = Path(__file__).resolve().parents[1] / "config" / "command_corrections.json"
_ALIASES_FILE = Path(__file__).resolve().parents[1] / "aliases.json"
_VOICE_COMMANDS_DB = Path(__file__).resolve().parents[1] / "config" / "jarvis.db"
_CUSTOM_CORRECTIONS_MTIME_NS = None
_CUSTOM_CORRECTIONS: list[tuple[re.Pattern, str]] = []
_CUSTOM_CORRECTIONS_LOCK = threading.Lock()


def reload_corrections():
    """Force reload of all custom corrections and aliases on the next utterance."""
    global _CUSTOM_CORRECTIONS_MTIME_NS
    with _CUSTOM_CORRECTIONS_LOCK:
        _CUSTOM_CORRECTIONS_MTIME_NS = None


def _load_custom_corrections():
    """Reload user aliases from both config/command_corrections.json and aliases.json when changed."""
    global _CUSTOM_CORRECTIONS_MTIME_NS, _CUSTOM_CORRECTIONS
    mtimes = []
    for f in (_CUSTOM_CORRECTIONS_FILE, _ALIASES_FILE, _VOICE_COMMANDS_DB):
        try:
            mtimes.append(f.stat().st_mtime_ns)
        except OSError:
            mtimes.append(0)
    current_mtime = tuple(mtimes)

    if current_mtime == _CUSTOM_CORRECTIONS_MTIME_NS:
        return _CUSTOM_CORRECTIONS

    with _CUSTOM_CORRECTIONS_LOCK:
        try:
            aliases_dict: dict[str, str] = {}

            # 1. Load root aliases.json first (lower precedence than custom corrections)
            if _ALIASES_FILE.is_file():
                try:
                    with _ALIASES_FILE.open("r", encoding="utf-8-sig") as stream:
                        data = json.load(stream)
                    if isinstance(data, dict):
                        for k, v in data.items():
                            if str(k).startswith("_"):
                                continue
                            if isinstance(v, str):
                                aliases_dict[k.strip()] = v.strip()
                            elif isinstance(v, list):
                                for item in v:
                                    if isinstance(item, str):
                                        aliases_dict[item.strip()] = k.strip()
                except Exception:
                    pass

            # 2. Incorporate profile phonetic aliases dynamically (e.g. "Bakka" -> backup, back up, baka)
            aliases_dict.setdefault("backup", "Bakka")
            aliases_dict.setdefault("back up", "Bakka")
            aliases_dict.setdefault("baka", "Bakka")
            aliases_dict.setdefault("baca", "Bakka")
            aliases_dict.setdefault("backup profile", "Bakka profile")
            aliases_dict.setdefault("back up profile", "Bakka profile")

            # 3. Load config/command_corrections.json (takes precedence over aliases.json)
            if _CUSTOM_CORRECTIONS_FILE.is_file():
                try:
                    with _CUSTOM_CORRECTIONS_FILE.open("r", encoding="utf-8-sig") as stream:
                        data = json.load(stream)
                    if isinstance(data, dict):
                        corr = data.get("corrections", {})
                        if isinstance(corr, dict):
                            for heard, intended in corr.items():
                                if isinstance(heard, str) and isinstance(intended, str) and heard.strip() and intended.strip():
                                    aliases_dict[heard.strip()] = intended.strip()
                except Exception:
                    pass
            try:
                from core.database import load_voice_commands
                aliases_dict.update(load_voice_commands())
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                pass

            compiled = []
            for heard, intended in aliases_dict.items():
                if isinstance(heard, str) and heard.strip() and isinstance(intended, str) and intended.strip():
                    compiled.append((
                        _compile_word_pattern(heard),
                        intended.strip(),
                    ))
            compiled.sort(key=lambda entry: len(entry[0].pattern), reverse=True)
            _CUSTOM_CORRECTIONS = compiled
            _CUSTOM_CORRECTIONS_MTIME_NS = current_mtime
        except Exception:
            pass
    return _CUSTOM_CORRECTIONS


# ============================================================
# PHRASE-LEVEL FIXES
# ============================================================
# These are context-aware: they only fire when the overall
# command structure matches.

_PHRASE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # "search bad apple" / "search pad apple" / "search bad app" -> "search Bad Apple"
    (
        re.compile(
            r"\b(search|find|google|look\s+up)[\s,]+(?:for[\s,]+)?(?:pad|that|bad|bed|bat|dad)[\s,]+(?:apple|app|able|apples)\b",
            re.IGNORECASE,
        ),
        r"\1 Bad Apple",
    ),
    # "open backup [profile]" / "switch to backup [profile]" -> "open Bakka profile"
    (
        re.compile(
            r"\b(open|switch\s+to|go\s+to|launch|start)[\s,]+(?:the[\s,]+)?(?:back\s*up|baka|baca|bacca|bakka)(?:[\s,]+profile)?\b",
            re.IGNORECASE,
        ),
        r"\1 Bakka profile",
    ),
    # "backup profile" alone -> "Bakka profile"
    (
        re.compile(r"^\s*(?:back\s*up|baka|baca|bacca|bakka)[\s,]+profile\s*$", re.IGNORECASE),
        "Bakka profile",
    ),
    # Recover common first-word omissions/mishears in media commands.
    # Only rewrite "like" when the sentence clearly ends in a YouTube target.
    (
        re.compile(r"^\s*like\s*,?\s+(.+?)\s+(?:in|on)\s+you\s*tube\s*$", re.IGNORECASE),
        r"play \1 in YouTube",
    ),
    # Barge-in often starts while Jarvis is still speaking, so Whisper may
    # capture only "over to YouTube..." and omit the leading "switch".
    (
        re.compile(r"^\s*over\s+to\s+you\s*tube\s+and\s+play\b", re.IGNORECASE),
        "switch over to YouTube and play",
    ),
    # Treat a common Whisper expansion of "maximize, then play" as two steps.
    (
        re.compile(r"\b(maximize|maximise)\s*,?\s*(?:twice\s+)?on\s+play\b", re.IGNORECASE),
        r"\1 and then play",
    ),
    # "open no pad" → "open Notepad"
    (
        re.compile(
            r"\b(open|close|launch|start|switch\s+to|go\s+to)[\s,]+no[\s,]+pad\b",
            re.IGNORECASE,
        ),
        r"\1 Notepad",
    ),
    # "go to you tube" → "go to YouTube"
    (
        re.compile(
            r"\b(open|go\s+to|switch\s+to|visit)[\s,]+you[\s,]+tube\b",
            re.IGNORECASE,
        ),
        r"\1 YouTube",
    ),
    # "read it" is ordinary English too, so only correct it in a site-open intent.
    (
        re.compile(r"\b(open|go\s+to|visit)[\s,]+read[\s,]+it\b", re.IGNORECASE),
        r"\1 Reddit",
    ),
    # "play X on you tube" → "play X on YouTube"
    (
        re.compile(r"\bon[\s,]+you[\s,]+tube\b", re.IGNORECASE),
        "on YouTube",
    ),
    # "black clover folder" with various spacings
    (
        re.compile(
            r"\bblack[\s,]+clover[\s,]+folder\b",
            re.IGNORECASE,
        ),
        "Black Clover folder",
    ),
]


# ============================================================
# TRAILING NOISE CLEANUP
# ============================================================
# Whisper sometimes appends filler / hallucinated text at the
# end of short voice commands.  Strip common noise patterns.

_TRAILING_NOISE = re.compile(
    r"[\s.!?,;:]+$"
    r"|(?:\s+(?:thank you|thanks|please|okay|um|uh|ah))[.!?,]*$",
    re.IGNORECASE,
)


# ============================================================
# PUBLIC API
# ============================================================

def cleanup_transcription(text: str) -> str:
    """
    Apply lightweight corrections to a Whisper transcript.

    Returns the cleaned text.  Never adds latency visible to the
    user (pure regex, no network calls, no model inference).
    """
    if not text or not text.strip():
        return ""

    result = text.strip()

    # 0. Normalize commas directly following command verbs:
    # "open, WhatsApp" -> "open WhatsApp", "scroll, down" -> "scroll down"
    result = re.sub(
        r"\b(open|launch|start|run|switch\s+to|switch|go\s+to|goto|visit|play|watch|listen\s+to|close|show|bring|focus|maximize|maximise|restore|minimize|minimise|search|scroll|page)\s*,\s*",
        r"\1 ",
        result,
        flags=re.IGNORECASE,
    )

    # 1. Phrase-level fixes first (higher priority).
    for pattern, replacement in _PHRASE_PATTERNS:
        result = pattern.sub(replacement, result)

    # 2. User-maintained custom corrections (highest word/phrase priority over built-ins).
    for pattern, replacement in _load_custom_corrections():
        result = pattern.sub(replacement, result)

    # 3. Whole-word default corrections.
    for pattern, replacement in _COMPILED_CORRECTIONS:
        result = pattern.sub(replacement, result)

    # 4. Trim trailing noise / filler.
    result = _TRAILING_NOISE.sub("", result).strip()

    # 5. Collapse multiple spaces.
    result = re.sub(r"\s{2,}", " ", result)

    return result


# ============================================================
# SELF-TEST
# ============================================================

if __name__ == "__main__":
    tests = [
        ("open no pad", "open Notepad"),
        ("go to you tube", "go to YouTube"),
        ("play black clover on you tube", "play Black Clover on YouTube"),
        ("open black clover folder", "open Black Clover folder"),
        ("switch to v s code", "switch to VS Code"),
        ("open chat gpt", "open ChatGPT"),
        ("go to jet gpt", "go to ChatGPT"),
        ("open spot ify", "open Spotify"),
        ("search pie thon tutorial", "search Python tutorial"),
        ("go to git hub", "go to GitHub"),
        ("open g mail", "open Gmail"),
        ("hey jarvis open chrome", "hey Jarvis open Chrome"),
        ("close dis cord", "close Discord"),
        ("go to read it", "go to Reddit"),
        ("open sam sung website", "open Samsung website"),
    ]

    print("=" * 60)
    print("       STT CLEANUP SELF-TEST")
    print("=" * 60)

    passed = 0

    for raw, expected in tests:
        got = cleanup_transcription(raw)
        ok = got == expected
        passed += ok
        status = "PASS" if ok else "FAIL"
        print(f"\n[{status}] Input:    {raw!r}")
        print(f"       Expected: {expected!r}")
        if not ok:
            print(f"       Got:      {got!r}")

    print(f"\n{'=' * 60}")
    print(f"   {passed}/{len(tests)} tests passed")
    print(f"{'=' * 60}")
