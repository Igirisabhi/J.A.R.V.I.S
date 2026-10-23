import json
import os
import shlex
import re
import subprocess
import time
from pathlib import Path

try:
    from rapidfuzz import fuzz, process
except ImportError:
    fuzz = None
    process = None

try:
    import jellyfish
except ImportError:
    jellyfish = None

_START_MENU_DIRS = [
    Path(os.environ.get("ProgramData", r"C:\ProgramData"))
    / "Microsoft"
    / "Windows"
    / "Start Menu"
    / "Programs",

    Path(os.environ.get("APPDATA", ""))
    / "Microsoft"
    / "Windows"
    / "Start Menu"
    / "Programs",
]

_CACHE_FILE = Path(__file__).resolve().parent.parent / "runtime" / "app_cache.json"
_ALIASES_FILE = Path(__file__).resolve().parent.parent / "aliases.json"

_apps_cache = None
_aliases_cache = None
_cache_loaded_printed = False

GENERIC_APP_WORDS = {
    "upload", "download", "file", "files", "document", "documents",
    "app", "application", "program", "software", "window", "tab", "page",
    "item", "stuff", "thing"
}


def load_aliases() -> dict[str, str]:
    """Load aliases mapping misheard words or abbreviations to canonical app/site names."""
    global _aliases_cache
    if _aliases_cache is not None:
        return _aliases_cache

    aliases: dict[str, str] = {}

    # 1. Load root aliases.json
    if _ALIASES_FILE.exists():
        try:
            with _ALIASES_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            for key, val in data.items():
                if str(key).startswith("_"):
                    continue
                k_norm = normalize(str(key))
                if isinstance(val, list):
                    for item in val:
                        aliases[normalize(str(item))] = k_norm
                elif isinstance(val, str):
                    aliases[k_norm] = normalize(str(val))
        except Exception as e:
            _aprint(f"⚠️ Could not load aliases.json: {e}")

    # 2. Load config/command_corrections.json
    corr_file = Path(__file__).resolve().parent.parent / "config" / "command_corrections.json"
    if corr_file.exists():
        try:
            with corr_file.open("r", encoding="utf-8-sig") as f:
                cdata = json.load(f)
            if isinstance(cdata, dict):
                corr = cdata.get("corrections", {})
                if isinstance(corr, dict):
                    for k, v in corr.items():
                        if isinstance(k, str) and isinstance(v, str):
                            aliases[normalize(k)] = normalize(v)
        except Exception:
            pass

    _aliases_cache = aliases
    return aliases


def add_alias(misheard: str, canonical: str) -> bool:
    """Add or update an alias mapping in both aliases.json and command_corrections.json."""
    global _aliases_cache
    m_norm = normalize(misheard)
    c_norm = normalize(canonical)
    if not m_norm or not c_norm:
        return False

    # 1. Update aliases.json
    data = {}
    if _ALIASES_FILE.exists():
        try:
            with _ALIASES_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    data[m_norm] = c_norm
    try:
        with _ALIASES_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        _aprint(f"⚠️ Error saving alias to aliases.json: {e}")
        return False

    # 2. Synchronize to config/command_corrections.json
    corr_file = Path(__file__).resolve().parent.parent / "config" / "command_corrections.json"
    try:
        corr_data = {}
        if corr_file.exists():
            with corr_file.open("r", encoding="utf-8-sig") as f:
                corr_data = json.load(f)
        if not isinstance(corr_data, dict):
            corr_data = {}
        if "corrections" not in corr_data or not isinstance(corr_data["corrections"], dict):
            corr_data["corrections"] = {}
        corr_data["corrections"][misheard.strip()] = canonical.strip()
        with corr_file.open("w", encoding="utf-8") as f:
            json.dump(corr_data, f, indent=2)
    except Exception as e:
        _aprint(f"⚠️ Error synchronizing alias to command_corrections.json: {e}")

    _aliases_cache = None
    load_aliases()

    try:
        from voice.stt_cleanup import reload_corrections
        reload_corrections()
    except Exception:
        pass

    _aprint(f"💾 Added alias: '{m_norm}' -> '{c_norm}'")
    return True


def _aprint(*args, **kwargs):
    import sys
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        msg = " ".join(str(a) for a in args)
        safe = msg.encode(enc, errors="replace").decode(enc)
        print(safe, **{k: v for k, v in kwargs.items() if k != "end"})


# ============================================================
# NORMALIZE
# ============================================================

def normalize(text: str) -> str:
    if not text:
        return ""

    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9 ._-]+", " ", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


# ============================================================
# SCAN WINDOWS START MENU & STORE APPS
# ============================================================

def scan_installed_apps():
    apps = []

    _aprint("🔎 Scanning installed Windows apps...")

    # 1. Start Menu .lnk shortcuts
    for root in _START_MENU_DIRS:
        if not root.exists():
            continue
        try:
            for shortcut in root.rglob("*.lnk"):
                app_name = shortcut.stem.strip()
                if not app_name:
                    continue
                apps.append((app_name, str(shortcut)))
        except Exception as e:
            _aprint(f"⚠️ App scan warning: {e}")

    # 2. Windows Store / UWP apps via Get-StartApps (PowerShell)
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", "Get-StartApps"],
            text=True,
            timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        for line in out.splitlines()[3:]:
            parts = line.strip().split()
            if len(parts) >= 2:
                appid = parts[-1]
                name = line.strip()[:-len(appid)].strip()
                if name and appid:
                    # Target as shell:AppsFolder URI if UWP AppID
                    target = f"shell:AppsFolder\\{appid}" if ("!" in appid or "{" in appid) else appid
                    apps.append((name, target))
    except Exception as e:
        _aprint(f"⚠️ StartApps scan warning: {e}")

    # Remove duplicates
    seen = set()
    unique_apps = []

    for name, path in apps:
        key = normalize(name)
        if not key or key in seen:
            continue
        seen.add(key)
        unique_apps.append((name, path))

    _aprint(f"📦 Found {len(unique_apps)} Windows apps")
    return unique_apps


# ============================================================
# CACHE
# ============================================================

def save_app_cache(apps):

    try:

        data = [
            {
                "name": name,
                "path": path,
            }
            for name, path in apps
        ]

        with _CACHE_FILE.open(
            "w",
            encoding="utf-8",
        ) as file:

            json.dump(
                data,
                file,
                indent=2,
                ensure_ascii=False,
            )

        _aprint(f"💾 App database saved → {_CACHE_FILE.name}")
        return True

    except Exception as e:
        _aprint(f"⚠️ Could not save app database: {e}")
        return False


def load_app_cache():
    if not _CACHE_FILE.exists():
        return None

    try:
        # Check cache age: rescan if older than 24 hours
        mtime = _CACHE_FILE.stat().st_mtime
        if (time.time() - mtime) > 86400:
            _aprint("⏰ App cache is older than 24 hours; rescanning...")
            return refresh_apps()

        with _CACHE_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        apps = []
        for item in data:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            path = item.get("path")
            if not name or not path:
                continue
            apps.append((name, path))

        if not apps:
            return None

        valid_apps = []
        for name, path in apps:
            if path.startswith("shell:") or path.endswith(":") or Path(path).exists():
                valid_apps.append((name, path))

        if not valid_apps:
            _aprint("⚠️ App cache contains no valid launch targets; rescanning.")
            return None

        global _cache_loaded_printed
        if not _cache_loaded_printed:
            _aprint(f"⚡ Loaded {len(valid_apps)} apps from cache")
            _cache_loaded_printed = True
        return valid_apps

    except Exception as e:
        _aprint(f"⚠️ App cache error: {e}")
        return None


def refresh_apps():
    global _apps_cache
    _apps_cache = scan_installed_apps()
    save_app_cache(_apps_cache)
    return _apps_cache


def get_installed_apps(force_refresh: bool = False):
    global _apps_cache
    if force_refresh:
        return refresh_apps()
    if _apps_cache is not None:
        return _apps_cache

    _apps_cache = load_app_cache()
    if _apps_cache is not None:
        return _apps_cache

    return refresh_apps()


# ============================================================
# APP MATCHING (ALIAS -> EXACT -> RAPIDFUZZ -> METAPHONE)
# ============================================================

def score_app(query, app_name):
    query = normalize(query)
    app_name = normalize(app_name)

    if not query or not app_name:
        return 0

    query_words = query.split()
    app_words = app_name.split()

    # Exact
    if query == app_name:
        return 2000

    # Common acronyms / alternate forms
    if query in ("vs code", "vscode", "v s code") and ("visual studio code" in app_name or "vs code" in app_name or app_name == "code"):
        return 1950

    # Multi-word query
    if len(query_words) > 1:
        if not all(word in app_words for word in query_words):
            return 0
        if set(query_words) == set(app_words):
            return 1900
        return 1500

    # Single word
    if query in app_words:
        return 1200

    if app_name.startswith(query):
        return 1000

    if query in app_name:
        return 700

    return 0


def resolve_app(query: str) -> tuple[tuple[str, str] | None, str | None]:
    """
    Resolve an app query through the matching pipeline:
    1. Exact alias lookup (aliases.json)
    2. Generic words filter (e.g. 'upload' -> ask for clarification without rescanning)
    3. Exact / heuristic scoring against app_cache.json
    4. Fuzzy matching (rapidfuzz, threshold >= 80)
    5. Phonetic matching (jellyfish.metaphone)
    6. Closest suggestion ('did you mean X?') if still not found.
    """
    raw_query = str(query or "").strip()
    norm = normalize(raw_query)
    if not norm:
        return None, "Please specify an app name."

    # 1. Check aliases
    aliases = load_aliases()
    target = aliases.get(norm, norm)

    # 2. Handle generic words (e.g. 'upload', 'download') that are not mapped to apps
    if norm in GENERIC_APP_WORDS and target == norm:
        return None, f"What did you mean by '{raw_query}'?"

    apps = get_installed_apps() or []

    # Priority rule: when user says "open cloud", prefer Claude unless a separate cloud app is installed
    if norm == "cloud":
        other_clouds = [
            app_name for app_name, _ in apps
            if "cloud" in app_name.lower() and "claude" not in app_name.lower()
        ]
        if other_clouds:
            return None, f"Did you mean Claude or {other_clouds[0]}?"
        target = "claude"

    # Claude mapping: desktop app if installed, otherwise claude.ai in browser
    if target == "claude":
        for app_name, shortcut_path in apps:
            if "claude" in app_name.lower():
                _aprint(f"🎯 Claude desktop app match → {app_name}")
                return (app_name, shortcut_path), None
        _aprint("🌐 Claude desktop app not installed; using claude.ai")
        return ("Claude", "https://claude.ai"), None

    if not apps:
        return None, "No installed apps are currently available."

    # 3. Exact and heuristic matching
    heuristic_matches = []
    for app_name, shortcut_path in apps:
        s = score_app(target, app_name)
        if s > 0:
            heuristic_matches.append((s, len(app_name), app_name, shortcut_path))

    if heuristic_matches:
        heuristic_matches.sort(key=lambda item: (-item[0], item[1]))
        best = heuristic_matches[0]
        _aprint(f"🎯 App match → {best[2]} (score {best[0]})")
        return (best[2], best[3]), None

    # 4. Fuzzy matching (rapidfuzz, threshold about 80)
    best_fuzzy_app = None
    best_fuzzy_path = None
    best_fuzzy_score = 0.0

    target_clean = normalize(target)
    for app_name, shortcut_path in apps:
        norm_name = normalize(app_name)
        score = 0.0
        if fuzz is not None:
            score = float(fuzz.token_sort_ratio(target_clean, norm_name))
        else:
            import difflib
            score = float(difflib.SequenceMatcher(None, target_clean, norm_name).ratio() * 100)

        if score > best_fuzzy_score:
            best_fuzzy_score = score
            best_fuzzy_app = app_name
            best_fuzzy_path = shortcut_path

    if best_fuzzy_score >= 80 and best_fuzzy_app and best_fuzzy_path:
        _aprint(f"🎯 Fuzzy match → {best_fuzzy_app} (rapidfuzz {best_fuzzy_score:.1f})")
        return (best_fuzzy_app, best_fuzzy_path), None

    # 5. Phonetic matching (jellyfish metaphone as a last resort)
    if jellyfish is not None:
        try:
            target_meta = jellyfish.metaphone(target_clean)
            if target_meta:
                for app_name, shortcut_path in apps:
                    words = normalize(app_name).split()
                    if any(jellyfish.metaphone(w) == target_meta for w in words):
                        _aprint(f"🎯 Phonetic match → {app_name} (metaphone '{target_meta}')")
                        return (app_name, shortcut_path), None
        except Exception as e:
            _aprint(f"⚠️ Phonetic match error: {e}")

    # 6. Fallback suggestion if still not found
    if best_fuzzy_score >= 50 and best_fuzzy_app:
        return None, f"I couldn't find that app, did you mean {best_fuzzy_app}?"

    if norm in GENERIC_APP_WORDS:
        return None, f"What did you mean by '{raw_query}'?"

    return None, f"I couldn't find {raw_query} as an app."


def find_app(query):
    match, _ = resolve_app(query)
    return match


# ============================================================
# SPECIAL WINDOWS APPS
# ============================================================

def open_special_windows_app(query):

    query = normalize(query)

    special_apps = {

        "microsoft store":
            "ms-windows-store:",

        "microsoft store app":
            "ms-windows-store:",

        "store":
            "ms-windows-store:",
    }

    uri = special_apps.get(query)

    if not uri:
        return None

    try:

        os.startfile(uri)

        return "Opening Microsoft Store"

    except Exception as e:

        _aprint(
            f"❌ Microsoft Store failed: {e}"
        )

        return "I couldn't open Microsoft Store"


def launch_shortcut(shortcut_path):
    if shortcut_path.startswith(("http://", "https://")):
        _aprint(f"🌐 Target URL → {shortcut_path}")
        from tools.browser_controller import navigate_to_url
        navigate_to_url(shortcut_path, new_tab=False)
        return

    if shortcut_path.startswith("shell:"):
        _aprint(f"🎯 Target UWP → {shortcut_path}")
        subprocess.Popen(["cmd", "/c", "start", "", "explorer.exe", shortcut_path], shell=False)
        return

    if shortcut_path.endswith(":") and not ("\\" in shortcut_path or "/" in shortcut_path):
        _aprint(f"🎯 Target Protocol → {shortcut_path}")
        subprocess.Popen(["cmd", "/c", "start", "", shortcut_path], shell=False)
        return

    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(shortcut_path)
        target = shortcut.TargetPath
        arguments = shortcut.Arguments
        working_dir = shortcut.WorkingDirectory

        _aprint(f"🎯 Target → {target}")
        if target:
            command = [target]
            if arguments:
                command.extend(shlex.split(arguments, posix=False))
            subprocess.Popen(
                command,
                cwd=working_dir if working_dir else None,
                shell=False,
            )
            return
    except Exception:
        pass

    # Direct launch fallback
    os.startfile(shortcut_path)


# ============================================================
def open_app(query, allow_refresh=False):
    match, feedback = resolve_app(query)

    app_name, shortcut_path = match

    # Single-instance reuse: if already open, bring existing instance to front
    try:
        from tools.window_tools import (
            canonical_app_name, find_any_window, find_window_for_process,
            force_foreground_window, user32, SW_RESTORE, APP_PROCESSES
        )
        clean_name = canonical_app_name(app_name)
        hwnd = find_any_window(clean_name) or find_any_window(app_name) or find_any_window(query)
        if not hwnd and shortcut_path:
            target_exe = Path(shortcut_path).name
            hwnd = find_window_for_process(target_exe)
        if not hwnd:
            for p in APP_PROCESSES.get(clean_name, []):
                hwnd = find_window_for_process(p)
                if hwnd:
                    break
        if hwnd:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
            if force_foreground_window(hwnd):
                from core.session_memory import session
                session.set_current_app(clean_name or app_name)
                return f"{app_name.title()} is already open."
    except Exception:
        pass

    _aprint(f"🚀 Opening: {app_name}")

    try:
        launch_shortcut(shortcut_path)
        return f"Opening {app_name}."
    except Exception as e:
        _aprint(f"❌ Shortcut launch failed: {e}")
        try:
            os.startfile(shortcut_path)
            return f"Opening {app_name}."
        except Exception as fallback_error:
            _aprint(f"❌ Fallback failed: {fallback_error}")
            return f"I couldn't open {app_name}."


def open_app_action(query: str, allow_refresh: bool = False) -> tuple[bool, str]:
    """Execute open_app returning explicit (success, message)."""
    res = open_app(query, allow_refresh=allow_refresh)
    if not res:
        return False, f"I couldn't find or open {query}."
    failed = any(err in res.lower() for err in ("couldn't", "could not", "not found", "failed", "can't", "what did you mean"))
    return not failed, res


# Public aliases
open_installed_app = open_app


# ============================================================
# COMMAND EXTRACTION
# ============================================================

def extract_open_command(command):

    command = normalize(command)

    patterns = [

        r"^(?:open|launch|start)\s+(.+)$",

        r"^(?:switch to)\s+(.+)$",

        r"^(?:go to)\s+(.+)$",

    ]

    for pattern in patterns:

        match = re.match(
            pattern,
            command,
        )

        if match:

            return match.group(1).strip()

    return None


# ============================================================
# APP COMMAND HANDLER
# ============================================================

def handle_app_command(command):

    norm = normalize(command)

    # Explicit app refresh command
    if norm in {"refresh apps", "refresh app", "rescan apps", "rescan app", "reload apps", "reload app"}:
        refresh_apps()
        return "App database refreshed."

    app_name = extract_open_command(
        command
    )

    if not app_name:
        return None

    # Windows special apps
    special = open_special_windows_app(
        app_name
    )

    if special:
        return special

    # Explicit filesystem path
    if (
        "\\" in app_name
        or "/" in app_name
        or ":" in app_name
    ):
        return None

    # Don't steal file commands
    file_extensions = {

        ".py",
        ".pyw",
        ".cpp",
        ".c",
        ".h",
        ".hpp",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".java",
        ".cs",
        ".rs",
        ".go",

        ".html",
        ".css",
        ".json",
        ".xml",
        ".yaml",
        ".yml",

        ".txt",
        ".md",
        ".pdf",
        ".docx",
        ".xlsx",
        ".pptx",
        ".csv",

        ".zip",
        ".rar",
        ".7z",

        ".mp3",
        ".wav",
        ".flac",
        ".mp4",
        ".mkv",
        ".avi",
        ".mov",
        ".webm",

        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",

        ".exe",
        ".msi",
    }

    query_lower = app_name.lower()

    if any(
        query_lower.endswith(ext)
        for ext in file_extensions
    ):
        return None

    # Websites should go to web handler
    web_names = {

        "youtube",
        "google",
        "gmail",
        "github",
        "chatgpt",
        "reddit",
        "instagram",
        "facebook",
        "twitter",
        "x",
    }

    aliases = load_aliases()
    target_alias = aliases.get(query_lower, query_lower)

    if query_lower in web_names or target_alias in web_names:
        return None

    return open_app(
        app_name,
        allow_refresh=False,
    )


# ============================================================
# TEST
# ============================================================

if __name__ == "__main__":

    print("=" * 60)
    print("       JARVIS APP DATABASE TEST")
    print("=" * 60)
    print()

    apps = get_installed_apps()

    print(
        f"\n📦 Apps available: {len(apps)}"
    )

    print(
        f"💾 Cache location: {_CACHE_FILE}"
    )

    print()

    while True:

        try:

            command = input(
                "Command: "
            ).strip()

            if command.lower() in {
                "exit",
                "quit",
            }:

                print("Exiting.")

                break

            result = handle_app_command(
                command
            )

            print(
                f"Result: {result}"
            )

            print()

        except KeyboardInterrupt:

            print(
                "\nExiting."
            )

            break

        except Exception as e:

            print(
                f"❌ Error: {e}"
            )
