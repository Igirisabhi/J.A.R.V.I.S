"""
Universal Multi-Step Command Execution Engine for Jarvis.

Parses multi-action commands containing conjunctions ('and', 'then',
'after that', 'next', 'followed by', commas) into structured Action chains,
executes them sequentially with wait + verify, maintains session context,
and resolves anaphora ("it", "that", "this").
"""

import os
import re
import time
import json
import sqlite3
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.session_memory import session
from tools.window_tools import (
    close_app_window,
    close_current_window,
    find_any_window,
    force_foreground_window,
    get_foreground_window,
    get_window_title,
    maximize_app,
    maximize_current_window,
    maximize_video,
    minimize_app,
    minimize_current_window,
    normalize,
    open_app,
    get_window_process_name,
    restore_app,
    restore_current_window,
    show_desktop,
    switch_to_app,
    user32,
)

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_K = 0x4B
VK_F = 0x46
VK_F11 = 0x7A
_PENDING_CONFIRMATION = None
_PENDING_CONFIRMATION_AT = 0.0


def _press_key(vk: int):
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.02)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def _type_unicode(text: str):
    time.sleep(0.15)
    for ch in text:
        user32.keybd_event(0, ord(ch), KEYEVENTF_UNICODE, 0)
        user32.keybd_event(0, ord(ch), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0)
        time.sleep(0.01)


# ============================================================
# ACTION DATACLASS
# ============================================================

@dataclass
class Action:
    verb: str
    target: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    raw: str = ""


# Regex to split multi-step commands by standard connectors
_CHAIN_SPLIT_REGEX = re.compile(
    r"(?:,\s*(?:and\s+then|after\s+that|followed\s+by|then|next|and\s+)?|\s+(?:and\s+then|after\s+that|followed\s+by|then|next|and)\s+)",
    flags=re.IGNORECASE,
)

_BROWSER_NAMES = {
    "chrome", "google chrome", "brave", "brave browser", "edge", "microsoft edge",
}
_KNOWN_FILE_EXTENSIONS = {
    ".txt", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".csv", ".py", ".js", ".html", ".css", ".json", ".xml", ".zip",
    ".rar", ".7z", ".mp3", ".wav", ".flac", ".mp4", ".mkv", ".avi",
    ".mov", ".jpg", ".jpeg", ".png", ".gif", ".md",
}

_FILLERS = re.compile(r"\b(?:please|um|uh|can you|jarvis)\b", re.IGNORECASE)


def normalize_voice_text(text: str | None) -> str:
    """Normalize common STT spelling while preserving the existing correction list."""
    value = (text or "").strip().lower()
    try:
        from core.settings import get_settings
        wake_names = {
            "hey_jarvis": "hey jarvis",
            "alexa": "alexa",
            "hey_mycroft": "hey mycroft",
            "hey_rhasspy": "hey rhasspy",
            "timer": "timer",
            "weather": "weather",
        }
        wake_words = get_settings().get("voice", {}).get("wake_words", ["hey_jarvis"])
        prefixes = [wake_names[name] for name in wake_words if name in wake_names]
        for prefix in sorted(prefixes, key=len, reverse=True):
            value = re.sub(r"^" + re.escape(prefix) + r"\b[\s,:;!?-]*", "", value, count=1)
    except (ImportError, OSError, ValueError, AttributeError):
        pass
    try:
        combined: dict[str, str] = {}
        aliases_path = Path(__file__).resolve().parent.parent / "aliases.json"
        if aliases_path.is_file():
            with aliases_path.open("r", encoding="utf-8-sig") as stream:
                adata = json.load(stream)
                for k, v in adata.items():
                    if not str(k).startswith("_"):
                        if isinstance(v, str):
                            combined[k] = v
                        elif isinstance(v, list):
                            for item in v:
                                if isinstance(item, str):
                                    combined[item] = k
        corrections_path = Path(__file__).resolve().parent.parent / "config" / "command_corrections.json"
        if corrections_path.is_file():
            with corrections_path.open("r", encoding="utf-8-sig") as stream:
                correction_data = json.load(stream)
                combined.update(correction_data.get("corrections", {}))
        try:
            from core.database import load_voice_commands
            combined.update(load_voice_commands())
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            pass
        for heard, intended in sorted(combined.items(), key=lambda item: len(item[0]), reverse=True):
            words = [re.escape(w) for w in re.split(r"[\s,]+", heard.strip().lower()) if w]
            if words:
                pat = r"(?<!\w)" + r"[\s,]+".join(words) + r"(?!\w)"
                value = re.sub(pat, intended.lower(), value, flags=re.IGNORECASE)
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    value = _FILLERS.sub(" ", value)
    value = re.sub(r"\b([\w-]+)\s+dot\s+([\w-]+)\b", r"\1.\2", value)
    value = re.sub(r"\b([\w.]+)\s+slash\s+([\w.]+)\b", r"\1/\2", value)
    value = re.sub(r"\b([\w.]+)\s+underscore\s+([\w.]+)\b", r"\1_\2", value)
    value = re.sub(r"\b([\w.]+)\s+dash\s+([\w.]+)\b", r"\1-\2", value)
    return re.sub(r"\s+", " ", value).strip()


def action_to_dsl(action: Action) -> str:
    """Compile an Action into the small internal execution string format."""
    verbs = {"open": "open", "open_file_in": "open", "create_file": "create", "search": "search", "navigate": "tool", "type": "keys", "read_file": "open", "delete": "tool", "rename": "tool", "search_files": "search", "search_folders": "search", "list_files": "tool", "scroll": "tool"}
    verb = verbs.get(action.verb, action.verb if action.verb in {"open", "create", "search", "keys", "launch", "wait", "say", "mode", "tool"} else "tool")
    target = action.target or action.args.get("destination") or ""
    if action.verb == "open" and target and Path(target).suffix.lower() not in _KNOWN_FILE_EXTENSIONS:
        kind, _canonical = _destination_kind(target)
        if kind in {"app", "browser"}:
            verb = "launch"
    if action.verb == "macro":
        target = f"macro {target}"
    elif verb == "tool" and action.verb not in {"navigate", "delete", "rename", "list_files", "macro"}:
        target = f"{action.verb} {target}".strip()
    elif action.verb in {"delete", "rename", "list_files"}:
        target = f"{action.verb} {target}".strip()
    return f"{verb}:{target}".rstrip(":")


def execute_dsl(dsl: str, context: dict[str, Any], action: Action | None = None) -> tuple[bool, str]:
    """Execute a compiled action after applying the launch allowlist."""
    verb, _, target = dsl.partition(":")
    if verb == "launch":
        canonical_target = target.lower().strip()
        from tools.app_tools import load_aliases
        aliases = load_aliases()
        if canonical_target in aliases:
            canonical_target = aliases[canonical_target]
        from tools.browser_profiles import find_profile_across_browsers
        is_profile = bool(find_profile_across_browsers(target))
        allowlist = [a.lower() for a in get_settings().get("app_allowlist", [])]
        if not is_profile and canonical_target not in allowlist and target.lower().strip() not in allowlist:
            return False, f"I can't launch {target}; it isn't in app_allowlist."
        message = open_app(target)
        # Treat None, empty, or whitespace-only results as failure so that a
        # failed launch does not look like success to the command chain.
        if not (message and message.strip()):
            return False, f"I couldn't open {target}."
        success = not _action_report_is_failure(message)
        if success:
            context["current_app"] = target
            session.set_current_app(target)
        return success, message
    if action is None:
        action = Action(verb=verb, target=target)
    return execute_action(action, context)


def get_settings():
    from core.settings import get_settings as load
    return load()


def _execute_macro(name: str, context: dict[str, Any]) -> tuple[bool, str]:
    macros = get_settings().get("macros", {})
    steps = macros.get(name)
    if not isinstance(steps, list):
        return False, f"I couldn't find the {name} macro."
    results = []
    for dsl in steps:
        if not isinstance(dsl, str) or ":" not in dsl:
            return False, f"The {name} macro has an invalid step."
        verb, target = dsl.split(":", 1)
        if verb == "wait":
            try:
                time.sleep(min(30.0, max(0.0, float(target) / 1000)))
            except ValueError:
                return False, f"The {name} macro has an invalid wait duration."
            continue
        if verb == "launch":
            canonical_target = target.lower().strip()
            from tools.app_tools import load_aliases
            aliases = load_aliases()
            if canonical_target in aliases:
                canonical_target = aliases[canonical_target]
            from tools.browser_profiles import find_profile_across_browsers
            is_profile = bool(find_profile_across_browsers(target))
            allowlist = [a.lower() for a in get_settings().get("app_allowlist", [])]
            if not is_profile and canonical_target not in allowlist and target.lower().strip() not in allowlist:
                return False, f"I can't launch {target}; it isn't in app_allowlist."
            message = open_app(target)
            if not (message and message.strip()) or _action_report_is_failure(message):
                return False, message or f"I couldn't open {target}."

            results.append(message)
            continue
        if verb in {"open", "create", "search", "keys"}:
            action_verb = {"open": "open", "create": "create_file", "search": "search_files", "keys": "type"}[verb]
            action = Action(verb=action_verb, target=target)
            success, message = execute_dsl(action_to_dsl(action), context, action)
            if not success:
                return False, message
            results.append(message)
            continue
        if verb == "say":
            from core.events import event_bus
            event_bus.publish("say", text=target)
            results.append(target)
            continue
        if verb == "mode":
            from core.state import state_machine
            state_machine.set_mode(target)
            results.append(f"Mode set to {target}")
            continue
        return False, f"The {name} macro contains an unsupported action: {verb}."
    return True, ". ".join(results) or f"Ran {name}."


def _destination_kind(destination: str | None):
    """Return (kind, canonical name) for a website, browser, or installed app."""
    raw = (destination or "").strip().lower().removesuffix(" website").removesuffix(" site").strip()
    generic_app = raw in {
        "app", "this app", "the app", "current app", "the current app", "active app",
        "application", "this application", "the application", "current application", "active application",
    }
    if not generic_app:
        raw = re.sub(r"^(?:the\s+)?app\s+", "", raw).removesuffix(" app").strip()
    aliases = {"you tube": "youtube", "yt": "youtube", "youtube app": "youtube", "you tube app": "youtube", "twitter x": "twitter", "disney+": "disney plus"}
    raw = aliases.get(raw, raw)
    if generic_app:
        from core.session_memory import session
        raw = (session.current_app or session.last_opened_app or "").strip().lower()
        if not raw:
            try:
                from tools.window_tools import (
                    APP_PROCESSES, WINDOW_KEYWORDS, canonical_app_name, get_foreground_window,
                    get_window_process_name, get_window_title,
                )
                hwnd = get_foreground_window()
                title = get_window_title(hwnd).strip().lower()
                for app_name, keywords in WINDOW_KEYWORDS.items():
                    if any(keyword.lower() in title for keyword in keywords):
                        raw = canonical_app_name(app_name)
                        break
                process = get_window_process_name(hwnd).lower()
                if not raw:
                    for app_name, processes in APP_PROCESSES.items():
                        if process in {name.lower() for name in processes}:
                            raw = canonical_app_name(app_name)
                            break
                if not raw:
                    raw = canonical_app_name(title)
                    if raw in {"jarvis", "python", "pythonw"}:
                        raw = ""
            except Exception:
                pass
        if not raw:
            return None, ""
    if raw in _BROWSER_NAMES:
        return "browser", raw

    from tools.window_tools import APP_OPEN_COMMANDS, APP_PROCESSES, canonical_app_name, find_any_window
    app = canonical_app_name(raw)
    if app in APP_PROCESSES or app in APP_OPEN_COMMANDS or find_any_window(app):
        return "app", app

    # Resolve installed apps before websites, so app searches stay in the
    # requested app instead of silently becoming a browser search.
    try:
        from tools.app_tools import get_installed_apps, normalize as normalize_app
        cached_apps = get_installed_apps()
        for installed_name, _ in cached_apps:
            installed_canonical = canonical_app_name(installed_name)
            if normalize_app(installed_name) == normalize_app(app) or normalize_app(installed_canonical) == normalize_app(app):
                return "app", installed_canonical
    except Exception:
        pass
    from tools.system_tools import WEBSITE_URLS, _website_destination_url
    url, site = _website_destination_url(raw)
    if url:
        return "website", site
    if raw in WEBSITE_URLS:
        return "website", raw
    return None, raw


def _browser_name(value: str | None):
    if not value:
        return None
    from tools.window_tools import canonical_app_name
    name = canonical_app_name(value)
    return name if name in _BROWSER_NAMES else None


# ============================================================
# ACTION PARSER
# ============================================================

def parse_action(segment: str) -> Action | None:
    """
    Parse an individual command segment into a typed Action.
    Returns None if the segment is not a recognized action.
    """
    if not segment:
        return None

    clean = normalize(normalize_voice_text(segment))
    if not clean:
        return None

    clean = re.sub(r"\bgoto\b", "go to", clean, flags=re.IGNORECASE)

    m = re.match(r"^(?:run|start)\s+macro\s+([a-z0-9_-]+)$", clean)
    if m:
        return Action(verb="macro", target=m.group(1), raw=segment)
    m = re.match(r"^(?:enter|activate|start)\s+([a-z0-9_-]+)\s+mode$", clean)
    if m:
        return Action(verb="mode", target=m.group(1), raw=segment)

    m = re.match(r"^(?:delete|remove)\s+(?:the\s+)?(?:file\s+)?(.+)$", clean)
    if m:
        return Action(verb="delete", target=m.group(1).strip(), raw=segment)
    m = re.match(r"^rename\s+(?:the\s+)?(?:file\s+)?(.+?)\s+to\s+(.+)$", clean)
    if m:
        return Action(verb="rename", target=m.group(1).strip(), args={"new_name": m.group(2).strip()}, raw=segment)
    m = re.match(r"^read\s+(?:the\s+)?(?:file\s+)?(.+)$", clean)
    if m:
        return Action(verb="read_file", target=m.group(1).strip(), raw=segment)
    m = re.match(r"^(?:list|show)(?:\s+all)?\s+files?(?:\s+in\s+(.+))?$", clean)
    if m:
        folder = re.sub(r"\s+(?:folder|directory)$", "", m.group(1) or ".", flags=re.I)
        return Action(verb="list_files", target=folder, raw=segment)
    m = re.match(r"^(?:search|find)\s+files?\s+for\s+(.+)$", clean)
    if m:
        return Action(verb="search_files", target=m.group(1).strip(), raw=segment)
    m = re.match(r"^(?:search|find)\s+folders?\s+for\s+(.+)$", clean)
    if m:
        return Action(verb="search_folders", target=m.group(1).strip(), raw=segment)

    m = re.match(r"^(?:create|make|new)\s+(?:a\s+|the\s+)?file\s+(.+)$", clean)
    if m:
        return Action(verb="create_file", target=m.group(1).strip(), raw=segment)

    file_scope = re.match(r"^open\s+(.+?\.[a-z0-9]{1,8})\s+(?:in|on|from|inside|within|under)\s+(.+)$", clean)
    if file_scope:
        parent = re.sub(r"\s+(?:folder|directory)$", "", file_scope.group(2).strip(), flags=re.I)
        return Action(verb="open_file_in", target=file_scope.group(1).strip(), args={"parent": parent}, raw=segment)

    # Resolve either spoken order while retaining the chosen browser:
    # "open Chrome in Reddit" / "open Reddit in Chrome".
    browser_pair = re.match(
        r"^(?:open|launch|start|go\s+to)\s+(.+?)\s+(?:in|to|with|using)\s+(.+)$",
        clean,
    )
    if browser_pair:
        left, right = browser_pair.group(1).strip(), browser_pair.group(2).strip()
        left_browser, right_browser = _browser_name(left), _browser_name(right)
        if left_browser:
            kind, site = _destination_kind(right)
            from tools.system_tools import WEBSITE_URLS
            if kind in {"website", None} or right.strip().lower().removesuffix(" website").removesuffix(" site").strip() in WEBSITE_URLS:
                return Action(verb="navigate", target=site, args={"browser": left_browser}, raw=segment)
        if right_browser:
            kind, site = _destination_kind(left)
            # The explicit browser phrase establishes that the left side is
            # intended as a web destination (including sites absent from the
            # built-in aliases).
            if kind in {"website", None, "app"}:
                return Action(verb="navigate", target=site, args={"browser": right_browser}, raw=segment)

    # Destination-aware playback, search, and transport commands. This is
    # shared by websites and native apps; app/site capabilities are resolved
    # by the executor so chains preserve one destination consistently.
    m = re.match(r"^(?:play|watch|listen\s+to|listen)\s+(.+?)\s+(?:in|on|from)\s+(.+?)\s+(?:in|with|using)\s+(chrome|google chrome|brave(?: browser)?|edge|microsoft edge)$", clean)
    if m:
        query, destination, browser = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        kind, canonical = _destination_kind(destination)
        if kind == "website":
            if query in {"video", "the video", "current video", "the current video"}:
                return Action(verb="play_current_video", args={"destination": canonical, "browser": _browser_name(browser)}, raw=segment)
            return Action(verb="play", target=query, args={"destination": canonical, "browser": _browser_name(browser)}, raw=segment)

    m = re.match(r"^(?:search|google|look\s+up)(?:\s+for)?\s+(.+?)\s+(?:in|on)\s+(.+?)\s+(?:in|with|using)\s+(chrome|google chrome|brave(?: browser)?|edge|microsoft edge)$", clean)
    if m:
        query, destination, browser = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        kind, canonical = _destination_kind(destination)
        if kind == "website":
            return Action(verb="search", target=query, args={"destination": canonical, "browser": _browser_name(browser)}, raw=segment)

    m = re.match(r"^(?:play|watch|listen\s+to|listen)\s+(.+?)\s+(?:in|on|from|using)\s+(.+)$", clean)
    if m:
        query, destination = m.group(1).strip(), m.group(2).strip()
        if destination not in {"this folder", "that folder", "the same folder", "current folder"} and not destination.endswith((" folder", " directory")):
            kind, canonical = _destination_kind(destination)
            if kind:
                if re.fullmatch(r"^(?:the\s+)?(?:first|1st)\s+video$", query):
                    return Action(verb="play_first_video", raw=segment)
                if query in {"video", "the video", "current video", "the current video"}:
                    return Action(verb="play_current_video", args={"destination": canonical}, raw=segment)
                return Action(verb="play", target=query, args={"destination": canonical}, raw=segment)

    m = re.match(
        r"^(?:search|find)\s+(?:in|inside)\s+(?:(?:the|this|current|active)\s+)?app(?:lication)?\s+(?:for|about)\s+(.+)$",
        clean,
    )
    if m:
        # Do NOT resolve the current app at parse time — session state may be
        # stale.  Keep the "this app" placeholder so execute_action can inspect
        # the actual foreground window at execution time.
        return Action(verb="search", target=m.group(1).strip(), args={"destination": "this app"}, raw=segment)

    m = re.match(r"^(?:search|google|look\s+up)(?:\s+for)?\s+(.+?)\s+(?:in|on|using)\s+(.+)$", clean)
    if m:
        query, destination = m.group(1).strip(), m.group(2).strip()
        if destination in {
            "app", "the app", "this app", "current app", "the current app",
            "active app", "application", "this application", "current application",
        }:
            return Action(verb="search", target=query, args={"destination": "this app"}, raw=segment)
        kind, canonical = _destination_kind(destination)
        if kind == "browser":
            return Action(verb="search", target=query, args={"browser": canonical}, raw=segment)
        if kind:
            return Action(verb="search", target=query, args={"destination": canonical}, raw=segment)
        if destination.endswith((" app", " application")):
            return Action(verb="search", target=query, args={"destination": destination}, raw=segment)

    m = re.match(r"^(?:search|find)\s+(?:in|inside)\s+(?:the\s+)?(.+?)\s+(?:for|about)\s+(.+)$", clean)
    if m:
        destination, query = m.group(1).strip(), m.group(2).strip()
        kind, canonical = _destination_kind(destination)
        if kind:
            return Action(verb="search", target=query, args={"destination": canonical}, raw=segment)
        if destination.endswith((" app", " application")):
            return Action(verb="search", target=query, args={"destination": destination}, raw=segment)

    # VLC fullscreen is controlled by its `F` hotkey, including when the user
    # names VLC directly instead of saying "video in VLC".
    if re.fullmatch(r"(?:maximize|maximise|fullscreen|full\s+screen)\s+(?:in\s+)?(?:vlc|vlc\s+media\s+player)", clean):
        return Action(verb="maximize_video", target="vlc", raw=segment)

    if re.fullmatch(
        r"(?:maximize|maximise|fullscreen|full\s+screen)(?:\s+(?:the\s+|this\s+|current\s+)?(?:video|window))?\s+(?:in|on)\s+(?:vlc|vlc\s+media\s+player)",
        clean,
    ):
        return Action(verb="maximize_video", target="vlc", raw=segment)

    if re.fullmatch(r"(?:play|resume|unpause)\s+(?:in|on|using)\s+(?:vlc|vlc\s+media\s+player)", clean):
        return Action(verb="play", args={"destination": "vlc"}, raw=segment)

    m = re.match(r"^(pause|resume|unpause|stop|next|skip|previous|prev)(?:\s+(?:the\s+)?(?:music|song|track|video|playback))?\s+(?:in|on|using)\s+(.+)$", clean)
    if m:
        verb_text, destination = m.group(1).strip(), m.group(2).strip()
        kind, canonical = _destination_kind(destination)
        if kind:
            verb = {
                "pause": "pause", "resume": "play", "unpause": "play",
                "stop": "stop", "next": "next_track", "skip": "next_track",
                "previous": "prev_track", "prev": "prev_track",
            }[verb_text]
            return Action(verb=verb, args={"destination": canonical}, raw=segment)

    # 1. SHOW DESKTOP
    if clean in {
        "go to desktop", "show desktop", "show me desktop",
        "minimize everything", "minimize all windows", "hide all windows",
    }:
        return Action(verb="show_desktop", raw=segment)

    # 2. MAXIMIZE VIDEO (F in VLC; F11 in other apps)
    m = re.match(r"^(?:maximize|maximise|fullscreen|full\s+screen)\s+(?:the\s+|this\s+|current\s+)?video(?:\s+(?:in|on)\s+(.+))?$", clean)
    if m:
        target = m.group(1).strip() if m.group(1) else None
        return Action(verb="maximize_video", target=target, raw=segment)

    m = re.match(r"^(?:make\s+(?:the\s+|this\s+|current\s+)?video\s+(?:fullscreen|full\s+size|full\s+screen))(?:\s+(?:in|on)\s+(.+))?$", clean)
    if m:
        target = m.group(1).strip() if m.group(1) else None
        return Action(verb="maximize_video", target=target, raw=segment)

    m = re.match(r"^(?:maximize|maximise)\s+(.+?)\s+video$", clean)
    if m:
        return Action(verb="maximize_video", target=m.group(1).strip(), raw=segment)

    if clean in {
        "maximize this video", "maximize the video", "maximize video",
        "maximize current video", "fullscreen video", "full screen video",
        "make video fullscreen", "make the video fullscreen",
    }:
        return Action(verb="maximize_video", target=None, raw=segment)

    # 3. MAXIMIZE THE WINDOW (clicks F11 everywhere)
    if clean in {
        "maximize the window", "maximize this window", "maximize current window",
        "maximize the current window", "maximize that window", "maximize window",
        "maximize", "maximize it", "maximize this",
        "maximise the window", "maximise this window", "maximise current window",
        "maximise the current window", "maximise that window", "maximise window",
        "maximise", "maximise it", "maximise this",
        "maximum", "maximum window", "maximum it", "maximum the window",
        "maximized", "maximised", "maxim", "maxima", "maksimise", "macs mise",
        "make big", "make full", "full window", "fullscreen window",
        "full screen window", "fullscreen the window", "full screen the window",
        "fullscreen", "full screen", "maximize screen", "maximise screen",
        "maximize the screen", "maximise the screen",
    }:
        return Action(verb="maximize_window", target="it", raw=segment)

    # 4. MAXIMIZE SPECIFIC APP OR GENERIC
    m = re.match(r"^(?:maximize|maximise|maximum|maximized|maximised|maksimise)\s+(.+)$", clean)
    if m:
        target = m.group(1).strip()
        if target in {
            "it", "that", "this", "the window", "this window", "current window",
            "the current window", "that window", "window", "the app", "this app",
            "current", "screen", "the screen", "active window", "the active window",
        }:
            return Action(verb="maximize_window", target="it", raw=segment)
        return Action(verb="maximize", target=target, raw=segment)

    m = re.match(r"^(?:make\s+(.+?)\s+(?:full\s+size|bigger|fullscreen)|make\s+it\s+(?:full\s+size|bigger|fullscreen))$", clean)
    if m:
        target = m.group(1).strip() if m.group(1) else "it"
        if target in {"it", "the window", "this window", "current window", "the current window", "that window", "window", "screen", "the screen"}:
            return Action(verb="maximize_window", target="it", raw=segment)
        if "video" in target:
            return Action(verb="maximize_video", target=target, raw=segment)
        return Action(verb="maximize", target=target, raw=segment)

    m = re.match(r"^(?:expand\s+(.+)|expand\s+it)$", clean)
    if m:
        target = m.group(1).strip() if m.group(1) else "it"
        if target in {"it", "the window", "this window", "current window", "the current window", "that window", "window", "screen", "the screen"}:
            return Action(verb="maximize_window", target="it", raw=segment)
        return Action(verb="maximize", target=target, raw=segment)

    # 3. RESTORE
    if clean in {
        "restore the window", "restore this window", "restore current window",
        "restore the current window", "restore that window", "restore window",
        "restore it", "restore", "restore this", "restor", "restored",
        "normal size",
    }:
        return Action(verb="restore", target="it", raw=segment)

    m = re.match(r"^(?:restore|restor|restored)\s+(.+)$", clean)
    if m:
        target = m.group(1).strip()
        if target in {
            "it", "that", "this", "window", "the window", "this window",
            "current window", "the current window", "that window", "the app", "this app", "current",
            "screen", "the screen", "active window", "the active window",
        }:
            return Action(verb="restore", target="it", raw=segment)
        return Action(verb="restore", target=target, raw=segment)

    m = re.match(r"^(?:bring\s+(.+?)\s+back|bring\s+it\s+back)$", clean)
    if m:
        target = m.group(1).strip() if m.group(1) else "it"
        if target in {"it", "that", "this", "window", "the window", "this window", "current window", "the current window", "that window"}:
            return Action(verb="restore", target="it", raw=segment)
        return Action(verb="restore", target=target, raw=segment)

    m = re.match(r"^(?:return\s+(.+?)\s+to\s+normal|return\s+it\s+to\s+normal)$", clean)
    if m:
        target = m.group(1).strip() if m.group(1) else "it"
        if target in {"it", "that", "this", "window", "the window", "this window", "current window", "the current window", "that window"}:
            return Action(verb="restore", target="it", raw=segment)
        return Action(verb="restore", target=target, raw=segment)

    m = re.match(r"^(?:normal\s+size\s+(.+)|normal\s+size)$", clean)
    if m:
        target = m.group(1).strip() if m.group(1) else "it"
        if target in {"it", "that", "this", "window", "the window", "this window", "current window", "the current window", "that window"}:
            return Action(verb="restore", target="it", raw=segment)
        return Action(verb="restore", target=target, raw=segment)

    # 4. MINIMIZE
    if clean in {
        "minimize the window", "minimize this window", "minimize current window",
        "minimize the current window", "minimize that window", "minimize window",
        "minimize it", "minimize", "minimize this",
        "minimise the window", "minimise this window", "minimise current window",
        "minimise the current window", "minimise that window", "minimise window",
        "minimise it", "minimise", "minimise this",
        "minimum", "minimum window", "minimum it",
        "minimized", "minimised", "minmise",
    }:
        return Action(verb="minimize", target="it", raw=segment)

    m = re.match(r"^(?:minimize|minimise|minimum|minimized|minimised|minmise)\s+(.+)$", clean)
    if m:
        target = m.group(1).strip()
        if target in {
            "it", "that", "this", "window", "the window", "this window",
            "current window", "the current window", "that window", "the app", "this app", "current",
            "screen", "the screen", "active window", "the active window",
        }:
            return Action(verb="minimize", target="it", raw=segment)
        return Action(verb="minimize", target=target, raw=segment)

    m = re.match(r"^(?:hide\s+(.+)|hide\s+it)$", clean)
    if m:
        target = m.group(1).strip() if m.group(1) else "it"
        if target in {"it", "that", "this", "window", "the window", "this window", "current window", "the current window", "that window"}:
            return Action(verb="minimize", target="it", raw=segment)
        return Action(verb="minimize", target=target, raw=segment)

    # 5. FOCUS / SWITCH
    m = re.match(r"^(?:switch\s+to|switch\s+over\s+to|switch\s+back\s+to|focus\s+on|focus|activate|bring\s+forward|bring\s+to\s+front|show)\s+(.+)$", clean)
    if m:
        target = m.group(1).strip().removesuffix(" forward").removesuffix(" to front").strip()
        return Action(verb="focus", target=target, raw=segment)

    if clean in {"switch window", "switch windows", "next window"}:
        return Action(verb="switch_window", raw=segment)

    # 5b. SCROLL
    scroll_m = re.match(r"^(?:scroll|page|stroll|slow)\s*(down|up|to\s+top|to\s+the\s+top|to\s+bottom|to\s+the\s+bottom|top|bottom)?$", clean)
    if scroll_m or clean in {"scroll", "page"}:
        raw_dir = (scroll_m.group(1) if scroll_m else "down") or "down"
        raw_dir = raw_dir.strip()
        if "top" in raw_dir:
            direction = "top"
        elif "bottom" in raw_dir:
            direction = "bottom"
        elif "up" in raw_dir:
            direction = "up"
        else:
            direction = "down"
        return Action(verb="scroll", target=direction, raw=segment)

    # 6. CLOSE
    if clean in {
        "close the window", "close this window", "close current window",
        "close the current window", "close current", "close window",
        "close that window", "close it", "close this folder", "close",
        "exit", "quit",
    }:
        return Action(verb="close", target="it", raw=segment)

    m = re.match(r"^(?:close|quit|exit)\s+(.+)$", clean)
    if m:
        target = m.group(1).strip()
        if target in {"down", "up", "top", "bottom"}:
            return Action(verb="scroll", target=target, raw=segment)
        if target in {"it", "that", "this", "window", "the window", "this window", "current window", "the current window", "that window"}:
            return Action(verb="close", target="it", raw=segment)
        return Action(verb="close", target=target, raw=segment)

    # 7a. NESTED FOLDER: "open X folder in Y folder"
    folder_in_m = re.match(
        r"^(?:open|go\s+to|goto)\s+(?:the\s+|my\s+)?(.+?)\s+(?:folder\s+)?(?:in|inside|within|under)\s+(?:the\s+|my\s+)?(.+?)(?:\s+folder|\s+directory)?$",
        clean,
    )
    if folder_in_m:
        child_raw = folder_in_m.group(1).strip()
        parent_raw = folder_in_m.group(2).strip()
        # Strip trailing "folder"/"directory" from both
        child_name = re.sub(r"(?:'s|s)?\s+(?:folder|directory)$", "", child_raw, flags=re.IGNORECASE).strip()
        parent_name = re.sub(r"(?:'s|s)?\s+(?:folder|directory)$", "", parent_raw, flags=re.IGNORECASE).strip()
        if re.fullmatch(r"^(?:the\s+)?(?:first|1st)\s+video$", child_name, flags=re.I) and parent_name.lower() in {"yt", "youtube", "you tube"}:
            return Action(verb="play_first_video", raw=segment)
        # Only treat as nested folder if the parent is NOT a browser/website
        if parent_name.lower() not in _BROWSER_NAMES and parent_name.lower() not in {"yt", "youtube", "you tube"}:
            from tools.system_tools import WEBSITE_URLS
            if parent_name.lower() not in WEBSITE_URLS:
                if Path(child_name).suffix.lower() in _KNOWN_FILE_EXTENSIONS:
                    return Action(verb="open_file_in", target=child_name, args={"parent": parent_name}, raw=segment)
                return Action(verb="open_folder_in", target=child_name, args={"parent": parent_name}, raw=segment)

    # 6b. BROWSER PROFILES (e.g. "open chrome profile bakka", "switch to chrome profile kushal", "bakka profile", "open bakka profile", "bakka", "open bakka")
    from tools.browser_profiles import handle_browser_profile_command
    prof_res = handle_browser_profile_command(clean)
    if prof_res is not None:
        return Action(verb="open_profile", target=clean, raw=segment)

    # 7. OPEN APP / FOLDER / FILE
    m = re.match(r"^(?:open|launch|start|go\s+to|goto)\s+(.+)$", clean)
    if m:
        tgt = m.group(1).strip()
        # "open <site> website" -> navigate
        if tgt.endswith(" website") or tgt.endswith(" site"):
            site = tgt.removesuffix(" website").removesuffix(" site").strip()
            return Action(verb="navigate", target=site, raw=segment)
        tgt_no_yt = re.sub(r"\s+(?:in|on|from)\s+(?:youtube|you tube|yt)$", "", tgt, flags=re.I).strip()
        if re.fullmatch(r"^(?:the\s+)?(?:first|1st)\s+video$", tgt_no_yt):
            return Action(verb="play_first_video", raw=segment)

        from tools.system_tools import WEBSITE_URLS, _website_destination_url
        from tools.window_tools import APP_OPEN_COMMANDS, APP_PROCESSES, canonical_app_name
        is_file_target = Path(tgt).suffix.lower() in _KNOWN_FILE_EXTENSIONS
        site_url, _ = _website_destination_url(tgt) if not is_file_target else (None, None)
        app_name = canonical_app_name(tgt)
        native_app = app_name in APP_PROCESSES or app_name in APP_OPEN_COMMANDS
        if not is_file_target and ((tgt.lower().removesuffix(" website").removesuffix(" site").strip() in WEBSITE_URLS and not native_app) or (site_url and re.match(r"^(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?$", tgt, flags=re.IGNORECASE))):
            return Action(verb="navigate", target=tgt, raw=segment)

        return Action(verb="open", target=tgt, raw=segment)

    # 8. NAVIGATE / BROWSER
    m = re.match(r"^(?:visit|navigate\s+to)\s+(.+)$", clean)
    if m:
        return Action(verb="navigate", target=m.group(1).strip(), raw=segment)

    # 9. PLAY FIRST VIDEO (specialized YouTube / media action)
    clean_no_yt = re.sub(r"\s+(?:in|on|from)\s+(?:youtube|you tube|yt)$", "", clean, flags=re.I).strip()
    if re.fullmatch(r"^(?:(?:play|watch|listen\s+to|open|click)\s+)?(?:the\s+)?(?:first|1st)\s+video$", clean_no_yt):
        return Action(verb="play_first_video", raw=segment)

    if re.fullmatch(r"^(?:play|watch|listen\s+to|open)\s+(?:the\s+)?current\s+video$", clean):
        return Action(verb="play_current_video", raw=segment)

    # 10. PLAY GENERAL MEDIA
    m = re.match(r"^(?:play|watch|listen\s+to|listen)\s+(.+)$", clean)
    if m:
        return Action(verb="play", target=m.group(1).strip(), raw=segment)

    if clean in {"play", "resume", "unpause", "toggle play"}:
        return Action(verb="play", target=None, raw=segment)

    # 11. PAUSE
    if clean in {"pause", "pause music", "pause video", "pause playback"}:
        return Action(verb="pause", raw=segment)

    # 12. STOP
    if clean in {"stop", "stop music", "stop video", "stop playback", "stop playing"}:
        return Action(verb="stop", raw=segment)

    # 13. MEDIA CONTROLS (Next, Previous)
    if clean in {"next", "next track", "next song", "skip"}:
        return Action(verb="next_track", raw=segment)

    if clean in {"previous", "previous track", "previous song"}:
        return Action(verb="prev_track", raw=segment)

    # 14. APP-SPECIFIC FULLSCREEN (f or F11)
    m = re.match(r"^(?:fullscreen|full\s+screen)\s+(.+)$", clean)
    if m:
        return Action(verb="fullscreen", target=m.group(1).strip(), raw=segment)

    if clean in {"fullscreen", "full screen", "toggle fullscreen"}:
        return Action(verb="fullscreen", target="it", raw=segment)

    # 15. TYPING / INPUT
    m = re.match(r"^(?:type|write|enter)\s+(.+)$", clean)
    if m:
        return Action(verb="type", target=m.group(1).strip(), raw=segment)

    # 16. SEARCH
    m = re.match(r"^(?:search|google|look\s+up)(?:\s+for)?\s+(.+)$", clean)
    if m:
        return Action(verb="search", target=m.group(1).strip(), raw=segment)

    return None


def parse_command_chain(command: str) -> list[Action]:
    """
    Split a command string into Action objects.
    Returns empty list if the command is a single action or contains unparseable segments.
    """
    if not command or not command.strip():
        return []
    text = command.strip()
    full_action = parse_action(text)
    if full_action and full_action.verb in {"open", "open_file_in", "read_file"} and full_action.target:
        try:
            from tools.system_tools import resolve_in_workspace
            if resolve_in_workspace(full_action.target).is_file():
                return []
        except (ImportError, OSError, ValueError):
            pass
    candidates = []
    quote = None
    escaped = False
    i = 0
    connectors = ("and then", "after that", "followed by", "next", "then", "also", ";", ",", "and")
    while i < len(text):
        char = text[i]
        if escaped:
            escaped = False
        elif char == "\\" and quote:
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in {'"', "'"}:
            quote = char
        else:
            for connector in connectors:
                if text[i:i + len(connector)].lower() == connector:
                    left_ok = i == 0 or text[i - 1].isspace()
                    end = i + len(connector)
                    right_ok = end == len(text) or text[end].isspace()
                    if left_ok and right_ok:
                        candidates.append((i, end))
                        i = end - 1
                        break
        i += 1

    def split_from(value: str, offset: int = 0) -> list[Action] | None:
        for start, end in candidates:
            if start < offset:
                continue
            left = text[offset:start].strip()
            right = text[end:].strip()
            if not left or not right:
                continue
            first = parse_action(left)
            if first is None:
                continue
            rest = parse_action(right)
            if rest is not None:
                return [first, rest]
            # Check subsequent connector boundaries recursively so multi-step chains are retained.
            remainder = split_from(right, end)
            if remainder:
                return [first, *remainder]
        return None

    actions = split_from(text)
    if actions:
        return actions
    return []


# ============================================================
# ACTION EXECUTION ENGINE
# ============================================================

def resolve_target(target: str | None, context: dict[str, Any]) -> str | None:
    """Resolve anaphora ('it', 'that', 'this', 'window', 'the window') using session or active window."""
    clean = (target or "").lower().strip()
    is_generic = not target or clean in {
        "it", "that", "this", "window", "the window", "this window",
        "current window", "the current window", "that window", "the app", "this app", "current",
    }
    if is_generic:
        # 1. Check session / context
        app = context.get("current_app") or session.current_app or session.last_opened_app
        if app and app.lower() not in {"it", "that", "this", "window", "the window"}:
            return app
        # 2. Check active foreground window
        fg = user32.GetForegroundWindow()
        if fg and user32.IsWindowVisible(fg):
            title = get_window_title(fg)
            if title:
                return title
        return None
    return target


def _action_report_is_failure(message: str) -> bool:
    """Recognize local-tool failure reports so a command chain won't continue."""
    lowered = (message or "").lower()
    failure_phrases = (
        "isn't open", "is not open", "isn't running", "not running",
        "couldn't", "could not", "unable to", "failed", "not found",
        "no active window", "no active video window", "no video window", "i don't know how",
        "please specify",
    )
    return any(phrase in lowered for phrase in failure_phrases)


def _activate_destination(destination, context, browser=None):
    """Prepare a website/browser/app destination and retain it for this chain."""
    generic_app_names = {
        "app", "this app", "the app", "current app", "the current app", "active app",
        "application", "this application", "the application", "current application", "active application",
    }
    if (destination or "").strip().lower() in generic_app_names:
        destination = context.get("current_app") or session.current_app or session.last_opened_app
        if not destination:
            return None, None, "I couldn't identify the current app to search in."

    kind, canonical = _destination_kind(destination)
    if not kind:
        if (destination or "").strip().lower() in {
            "app", "this app", "the app", "current app", "the current app", "active app",
            "application", "this application", "the application", "current application", "active application",
        }:
            return None, None, "I couldn't identify the current app to search in."
        return None, None, f"I couldn't identify {destination} as an app or website."
    from tools.system_tools import _website_destination_url
    from tools.window_tools import (
        canonical_app_name, find_any_window, force_foreground_window, open_app,
    )

    if kind == "website":
        url, site_name = _website_destination_url(canonical)
        if not url:
            return None, None, f"I couldn't open {canonical}."
        browser = _browser_name(browser or context.get("browser") or context.get("current_app"))
        context["website"] = site_name
        context["destination"] = site_name
        context["browser"] = browser
        context["current_app"] = browser
        session.last_opened_website = site_name
        if browser:
            session.set_current_app(browser)
        return "website", site_name, None

    app = canonical_app_name(canonical)
    hwnd = find_any_window(app)
    if hwnd:
        if not force_foreground_window(hwnd):
            return None, None, f"I couldn't bring {app.title()} to the front."
    else:
        message = open_app(app)
        if not message or _action_report_is_failure(message):
            return None, None, message or f"I couldn't open {app.title()}."
        # open_app can use an allowlisted web client when the desktop app is
        # unavailable; preserve that as a website destination.
        if "web version" in message.lower():
            site_url, site_name = _website_destination_url(app)
            context["website"] = site_name or app
            context["destination"] = site_name or app
            context["browser"] = _browser_name(browser or context.get("browser"))
            session.last_opened_website = site_name or app
            return "website", site_name or app, None
        # App launch is asynchronous. Never send search keys until the requested
        # app window exists and has focus, or the previous app (often Chrome)
        # could receive them instead.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            hwnd = find_any_window(app)
            if hwnd and user32.IsWindowVisible(hwnd):
                break
            time.sleep(0.1)
        if not hwnd or not user32.IsWindowVisible(hwnd):
            return None, None, f"{app.title()} didn't open, so I didn't send the search to another app."
        if not force_foreground_window(hwnd):
            return None, None, f"I couldn't bring {app.title()} to the front, so I didn't send the search elsewhere."
    context["current_app"] = app
    context["destination"] = app
    if app in _BROWSER_NAMES:
        context["browser"] = app
        session.set_current_app(app)
        return "browser", app, None
    context["app"] = app
    session.set_current_app(app)
    return "app", app, None


def _focused_browser_for(action, context):
    return _browser_name(action.args.get("browser") or context.get("browser") or context.get("current_app"))


def _route_search(query, action, context, destination=None):
    from tools.system_tools import google_search, search_website_destination

    browser = _focused_browser_for(action, context)
    if destination:
        youtube_destination = destination.strip().lower().removesuffix(" app").removesuffix(" website")
        if youtube_destination in {"youtube", "you tube", "yt"}:
            from tools.system_tools import search_youtube
            result = search_youtube(query, browser="chrome")
            return bool(result) and not _action_report_is_failure(result), result or f"I couldn't search YouTube for {query}."
        kind, canonical, error = _activate_destination(destination, context, browser=browser)
        if error:
            return False, error
        if kind == "website":
            result = search_website_destination(canonical, query, browser=browser)
            return (bool(result), result or f"I couldn't search {canonical}.")
        if kind == "browser":
            result = google_search(query) if not browser else None
            if browser:
                from tools.system_tools import open_url
                url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
                result = f"Searching Google for {query}." if open_url(url, browser=browser) else None
            return (bool(result), result or f"I couldn't search in {canonical}.")
        if canonical == "spotify":
            from tools.system_tools import open_url
            uri = "spotify:search:" + urllib.parse.quote(query)
            if open_url(uri):
                return True, f"Opened Spotify search for {query}."
        # Ctrl+F is the common in-app search shortcut for native app windows.
        user32.keybd_event(0x11, 0, 0, 0)
        _press_key(VK_F)
        user32.keybd_event(0x11, 0, KEYEVENTF_KEYUP, 0)
        _type_unicode(query)
        _press_key(0x0D)
        return True, f"Searching in {canonical.title()} for {query}."

    if action.args.get("browser") or browser:
        from tools.system_tools import open_url
        encoded = urllib.parse.quote_plus(query)
        url = "https://www.google.com/search?q=" + encoded
        browser = _browser_name(action.args.get("browser") or browser)
        if browser and open_url(url, browser=browser):
            context["browser"] = browser
            context["current_app"] = browser
            session.set_current_app(browser)
            return True, f"Searching Google for {query} in {browser.title()}."
        return False, f"I couldn't search Google in {action.args['browser']} ."

    if context.get("website"):
        result = search_website_destination(context["website"], query, browser=browser)
        if result:
            return True, result
    result = google_search(query)
    return bool(result), result or f"I couldn't search for {query}."


def _route_play(action, context, destination=None):
    from tools.system_tools import handle_media_command, handle_play_command, play_youtube, search_website_destination

    query = (action.target or "").strip()
    browser = _focused_browser_for(action, context)
    if destination:
        kind, canonical = _destination_kind(destination)[:2]
        if canonical == "vlc" and query.lower() in {
            "", "play", "video", "the video", "current video", "the current video",
            "music", "song", "playback", "it", "that", "this",
        }:
            from tools.window_tools import control_vlc_playback
            result = control_vlc_playback("play")
            return not _action_report_is_failure(result), result
        kind, canonical, error = _activate_destination(destination, context, browser=browser)
        if error:
            return False, error
        if kind == "website":
            if canonical == "youtube":
                if query.lower() in {"", "play", "video", "the video", "current video", "the current video", "music", "song", "playback", "it", "that", "this"}:
                    from tools.system_tools import _play_current_youtube_video
                    return True, _play_current_youtube_video(browser=browser)
                result = play_youtube(query, browser=browser)
                return bool(result) and not _action_report_is_failure(result), result or f"I couldn't play {query} on YouTube."
            if query.lower() in {"", "play", "video", "the video", "current video", "the current video", "music", "song", "playback", "it", "that", "this"}:
                result = handle_media_command("play")
                return bool(result), result or f"Sent play to {canonical}."
            result = search_website_destination(canonical, query, browser=browser)
            if result:
                return True, f"Opened {canonical} results for {query}. Select a result to start playback."
            return False, f"I couldn't find a search page for {canonical}."
        if kind == "browser":
            if query.lower() in {"", "play", "video", "the video", "current video", "the current video", "music", "song", "playback", "it", "that", "this"}:
                result = handle_media_command("play")
                return True, result or f"Sent play to {canonical.title()}."
            # A browser alone does not identify which streaming site/video.
            return False, f"I opened {canonical.title()}. Name a website, such as YouTube, to choose where to play {query}."
        if canonical == "vlc":
            result = handle_play_command(f"play {query}", player="vlc")
            ok = bool(result) and not _action_report_is_failure(result)
            return ok, result or f"I couldn't find {query} to play in VLC."
        if canonical == "spotify" and query.lower() not in {"", "play", "music", "song", "playback", "it", "that", "this"}:
            from tools.system_tools import open_url
            spotify_uri = "spotify:search:" + urllib.parse.quote(query)
            if open_url(spotify_uri):
                return True, f"Opened Spotify search for {query}. Choose a result to play."
        if query.lower() not in {"", "play", "music", "song", "video", "playback", "it", "that", "this"}:
            return False, f"{canonical.title()} is open, but it doesn't support media search through Jarvis."
        result = handle_media_command("play")
        return True, result or f"Sent play to {canonical.title()}."

    if action.args.get("youtube") or context.get("website") == "youtube":
        if query.lower() in {"", "play", "video", "the video", "current video", "the current video", "music", "song", "playback", "it", "that", "this"}:
            from tools.system_tools import _play_current_youtube_video
            return True, _play_current_youtube_video(browser=browser)
        result = play_youtube(query, browser=browser)
        return bool(result) and not _action_report_is_failure(result), result or f"I couldn't play {query} on YouTube."
    result = handle_play_command(f"play {query}") if query else None
    if result:
        return not _action_report_is_failure(result), result
    result = handle_media_command("play")
    return True, result or "Playing."


def _route_transport(verb, action, context, destination=None):
    from tools.system_tools import handle_media_command

    browser = _focused_browser_for(action, context)
    site = None
    if destination:
        kind, canonical = _destination_kind(destination)[:2]
        if canonical == "vlc":
            from tools.window_tools import control_vlc_playback
            result = control_vlc_playback(verb)
            return not _action_report_is_failure(result), result
        kind, canonical, error = _activate_destination(destination, context, browser=browser)
        if error:
            return False, error
        if kind == "website":
            site = canonical
            from tools.system_tools import _website_destination_url, open_url
            from tools.window_tools import find_any_window, force_foreground_window
            hwnd = find_any_window(site)
            if hwnd:
                if not force_foreground_window(hwnd):
                    return False, f"I couldn't bring {site.title()} to the foreground."
            else:
                url, _ = _website_destination_url(site)
                if not url or not open_url(url, browser=browser):
                    return False, f"I couldn't open {site}."
                return False, f"I couldn't find an open {site} player, so I opened {site}. Repeat the playback command after it loads."
    elif context.get("website"):
        site = context["website"]

    if site == "youtube" and verb in {"play", "pause"}:
        # K is YouTube's play/pause toggle; media keys are used everywhere else.
        from tools.system_tools import press_key
        if not press_key(VK_K):
            return False, f"I couldn't send the {verb} command to YouTube."
        return True, "Playing on YouTube." if verb == "play" else "Paused YouTube."

    phrase = {"play": "play", "pause": "pause", "stop": "stop music", "next_track": "next", "prev_track": "previous"}.get(verb, verb)
    result = handle_media_command(phrase)
    return True, result or {
        "play": "Playing.", "pause": "Paused.", "stop": "Stopped.",
        "next_track": "Skipping to next track.", "prev_track": "Going to previous track.",
    }.get(verb, "Done.")


def execute_action(action: Action, context: dict[str, Any]) -> tuple[bool, str]:
    """
    Execute a single parsed action with verification and state updates.
    Returns (success: bool, report: str).
    """
    verb = action.verb

    if verb == "macro":
        return _execute_macro(action.target or "", context)
    if verb == "mode":
        from core.state import state_machine
        state_machine.set_mode(action.target)
        return True, f"Mode set to {action.target}."

    if verb == "open_profile":
        from tools.browser_profiles import handle_browser_profile_command
        msg = handle_browser_profile_command(action.target or action.raw)
        if not msg:
            return False, "I couldn't open that browser profile."
        failed = any(err in msg.lower() for err in ("couldn't", "could not", "failed", "can't"))
        return not failed, msg

    # ---- 1. SHOW DESKTOP ----
    if verb == "show_desktop":
        msg = show_desktop()
        return True, msg

    # ---- MAXIMIZE VIDEO ----
    if verb == "maximize_video":
        target = action.target or resolve_target(None, context)
        msg = maximize_video(target)
        return not _action_report_is_failure(msg), msg

    # ---- MAXIMIZE WINDOW ----
    if verb == "maximize_window":
        msg = maximize_current_window()
        return not _action_report_is_failure(msg), msg

    # ---- 2. MAXIMIZE SPECIFIC APP ----
    if verb == "maximize":
        clean_target = (action.target or "").lower().strip()
        if clean_target in {"", "it", "that", "this", "window", "the window", "this window", "current window", "the current window", "that window", "the app", "this app", "current"}:
            msg = maximize_current_window()
            return not _action_report_is_failure(msg), msg
        target = resolve_target(action.target, context)
        if not target:
            msg = maximize_current_window()
            return not _action_report_is_failure(msg), msg
        res = maximize_app(target)
        if _action_report_is_failure(res):
            return False, res
        context["current_app"] = target
        session.set_current_app(target)
        return True, res

    # ---- 3. RESTORE ----
    if verb == "restore":
        clean_target = (action.target or "").lower().strip()
        if clean_target in {"", "it", "that", "this", "window", "the window", "this window", "current window", "the current window", "that window", "the app", "this app", "current"}:
            msg = restore_current_window()
            return not _action_report_is_failure(msg), msg
        target = resolve_target(action.target, context)
        if not target:
            msg = restore_current_window()
            return not _action_report_is_failure(msg), msg
        res = restore_app(target)
        if _action_report_is_failure(res):
            return False, res
        context["current_app"] = target
        session.set_current_app(target)
        return True, res

    # ---- 4. MINIMIZE ----
    if verb == "minimize":
        clean_target = (action.target or "").lower().strip()
        if clean_target in {"", "it", "that", "this", "window", "the window", "this window", "current window", "the current window", "that window", "the app", "this app", "current"}:
            msg = minimize_current_window()
            return not _action_report_is_failure(msg), msg
        target = resolve_target(action.target, context)
        if not target:
            msg = minimize_current_window()
            return not _action_report_is_failure(msg), msg
        res = minimize_app(target)
        if _action_report_is_failure(res):
            return False, res
        context["current_app"] = target
        session.set_current_app(target)
        return True, res

    # ---- 5. FOCUS / SWITCH ----
    if verb == "focus":
        target = resolve_target(action.target, context)
        if not target:
            return False, "I couldn't identify which window to switch to."
        res = switch_to_app(target)
        if _action_report_is_failure(res):
            # Only retry as a folder when the app itself is absent. A failed
            # Windows foreground activation must stay a failure, not turn into
            # a misleading folder-open success.
            if "isn't open" not in res.lower() and "is not open" not in res.lower():
                return False, res
            from tools.system_tools import find_pc_folder, open_path_safe, set_active_folder
            f = find_pc_folder(target)
            if f and os.path.isdir(f):
                if not open_path_safe(f):
                    return False, f"I couldn't open the {os.path.basename(f)} folder."
                set_active_folder(f)
                bname = os.path.basename(f)
                context["current_app"] = bname
                session.set_current_app(bname)
                return True, f"Switched to {bname}."
            return False, res
        context["current_app"] = target
        session.set_current_app(target)
        from tools.system_tools import find_pc_folder, set_active_folder
        f = find_pc_folder(target)
        if f and os.path.isdir(f):
            set_active_folder(f)
        return True, res

    # ---- SCROLL ----
    if verb == "scroll":
        from tools.browser_controller import scroll_page
        scroll_dir = (action.target or "down").lower().strip()
        success, msg = scroll_page(scroll_dir)
        return success, msg

    # ---- 6. CLOSE ----
    if verb == "close":
        clean_target = (action.target or "").lower().strip()
        if clean_target in {"down", "up", "top", "bottom", "left", "right"}:
            from tools.browser_controller import scroll_page
            success, msg = scroll_page(clean_target)
            return success, msg
        if clean_target in {"", "it", "that", "this", "window", "the window", "this window", "current window", "the current window", "that window", "the app", "this app", "current"}:
            msg = close_current_window()
            return not _action_report_is_failure(msg), msg
        target = resolve_target(action.target, context)
        if not target:
            msg = close_current_window()
            return not _action_report_is_failure(msg), msg
        res = close_app_window(target)
        success = not _action_report_is_failure(res)
        if success and context.get("current_app") and context["current_app"].lower() == target.lower():
            context["current_app"] = None
        return success, res

    # ---- 7a. OPEN SUBFOLDER INSIDE PARENT FOLDER ----
    if verb == "create_file":
        target = action.target or ""
        if not target:
            return False, "I need a file name to create."
        from tools.system_tools import resolve_in_workspace, WorkspaceError
        try:
            path = resolve_in_workspace(target, "write")
        except WorkspaceError as error:
            return False, str(error)
        try:
            with path.open("x", encoding="utf-8"):
                pass
        except FileExistsError:
            return False, f"The file {path.name} already exists."
        except OSError as error:
            return False, f"I couldn't create {path.name}: {error}."
        path = path.resolve()
        context["current_app"] = str(path)
        session.last_opened_file = str(path)
        return True, f"Created {path.name}."

    if verb == "open_file_in":
        target = action.target or ""
        parent_name = action.args.get("parent", "")
        if not target or not parent_name:
            return False, "I need both a file name and a folder to search in."
        from tools.system_tools import get_open_explorer_folder, get_active_folder, open_path_safe
        open_folder = get_open_explorer_folder() or get_active_folder()
        if not open_folder:
            return False, "Open the folder in Explorer first; I only open files from an open folder."
        parent_folder = Path(open_folder).resolve()
        requested_parent = parent_name.strip().lower().removesuffix(" folder").removesuffix(" directory")
        if requested_parent not in {"workspace", "this folder", parent_folder.name.lower()}:
            return False, f"Open {parent_name} in Explorer first; I only open files from the folder currently open there."
        try:
            file_path = (parent_folder / target).resolve()
            file_path.relative_to(parent_folder)
        except (OSError, ValueError):
            return False, "I can only open a file inside the folder currently open in Explorer."
        if not file_path.is_file():
            return False, f"I couldn't find {target} in the folder currently open in Explorer."
        if not open_path_safe(str(file_path)):
            return False, f"I couldn't open {file_path.name}."
        session.last_opened_file = str(file_path)
        return True, f"Opened {file_path.name}."

    if verb == "open_folder_in":
        child_name = action.target or ""
        parent_name = action.args.get("parent", "")
        if not child_name or not parent_name:
            return False, "I need both a folder name and a parent folder to search in."
        from tools.system_tools import find_pc_folder, find_subfolder, open_path_safe, set_active_folder
        parent_folder = find_pc_folder(parent_name)
        if not parent_folder or not os.path.isdir(str(parent_folder)):
            return False, f"I couldn't find the {parent_name} folder on your PC."
        child_folder = find_subfolder(parent_folder, child_name)
        if child_folder and os.path.isdir(str(child_folder)):
            child_str = str(child_folder)
            if not open_path_safe(child_str):
                return False, f"I couldn't open the {os.path.basename(child_str)} folder."
            set_active_folder(child_str)
            bname = os.path.basename(child_str)
            context["current_app"] = bname
            session.set_current_app(bname)
            return True, f"Opened {bname} folder in {os.path.basename(str(parent_folder))}."
        return False, f"I couldn't find a '{child_name}' folder inside {os.path.basename(str(parent_folder))}."

    # ---- 7. OPEN APP / FOLDER / FILE ----
    if verb == "open":
        target = resolve_target(action.target, context) or action.target or ""
        from tools.browser_profiles import handle_browser_profile_command
        prof_res = handle_browser_profile_command(target) or handle_browser_profile_command(action.raw)
        if prof_res is not None:
            failed = any(err in prof_res.lower() for err in ("couldn't", "could not", "failed", "can't"))
            return not failed, prof_res
        from tools.system_tools import resolve_in_workspace, WorkspaceError
        if re.fullmatch(r"(?:the\s+)?windows(?:\s+folder)?", target.strip(), re.I):
            from tools.system_tools import open_path_safe
            windows_path = os.environ.get("WINDIR", r"C:\Windows")
            if not open_path_safe(windows_path):
                return False, "I couldn't open the Windows folder."
            return True, "Opened the Windows folder in Explorer."
        target_path = Path(target).expanduser()
        is_file_target = target_path.suffix.lower() in _KNOWN_FILE_EXTENSIONS
        if is_file_target:
            from tools.system_tools import get_open_explorer_folder, get_active_folder, open_path_safe, is_desktop_path
            open_folder = get_open_explorer_folder() or get_active_folder()
            if not open_folder:
                return False, "Open the folder in Explorer first; I only open files from an open folder."
            if is_desktop_path(open_folder):
                # When in desktop, only direct files on Desktop may be opened, never into subfolders
                direct_file = Path(open_folder) / target_path.name
                if direct_file.is_file() and open_path_safe(str(direct_file)):
                    session.last_opened_file = str(direct_file)
                    return True, f"Opened {direct_file.name}."
                return False, f"I couldn't find {target_path.name} on the desktop. Open the folder first."
            try:
                folder_path = Path(open_folder).resolve()
                file_path = target_path.resolve() if target_path.is_absolute() else (folder_path / target_path).resolve()
                file_path.relative_to(folder_path)
            except (OSError, ValueError):
                return False, "I can only open a file inside the folder currently open in Explorer."
            if not file_path.is_file():
                return False, f"I couldn't find {file_path.name} in the folder currently open in Explorer."
            if not open_path_safe(str(file_path)):
                return False, f"I couldn't open {file_path.name}."
            session.last_opened_file = str(file_path)
            return True, f"Opened {file_path.name}."
        if action.args.get("workspace") or is_file_target or re.search(r"[\\/]|\.", target):
            try:
                target_path = resolve_in_workspace(target)
            except WorkspaceError as error:
                return False, str(error)
            if target_path.is_dir():
                from tools.system_tools import open_path_safe, set_active_folder
                if not open_path_safe(str(target_path)):
                    return False, f"I couldn't open the {target_path.name} folder."
                set_active_folder(str(target_path))
                return True, f"Opened the {target_path.name} folder."
            if not target_path.exists():
                from tools.system_tools import find_workspace_matches
                # Fuzzy fallback may open a directory, but it must never guess
                # a file from elsewhere in the workspace.
                matches = [path for path in find_workspace_matches(target) if path.is_dir()]
                if len(matches) > 1:
                    return False, "I found multiple workspace matches: " + ", ".join(str(path.name) for path in matches) + ". Which one should I open?"
                if len(matches) == 1:
                    target_path = matches[0]
                else:
                    return False, f"I couldn't find {target_path.name}."
        is_file_target = target_path.suffix.lower() in _KNOWN_FILE_EXTENSIONS
        if is_file_target and target_path.is_file():
            from tools.system_tools import open_path_safe, set_active_folder
            if not open_path_safe(str(target_path)):
                return False, f"I couldn't open {target_path.name}."
            opened_file_path = str(target_path.resolve())
            session.last_opened_file = opened_file_path
            set_active_folder(str(target_path.resolve().parent))
            context["current_app"] = opened_file_path
            return True, f"Opened {target_path.name}."
        if is_file_target:
            return False, f"I couldn't find {target_path.name}."
        if action.args.get("workspace") and target_path.is_file():
            from tools.system_tools import get_open_explorer_folder, open_path_safe, set_active_folder
            open_folder = get_open_explorer_folder()
            if not open_folder:
                return False, "Open the folder in Explorer first; I only open files from an open folder."
            try:
                target_path.resolve().relative_to(Path(open_folder).resolve())
            except (OSError, ValueError):
                return False, "I can only open a file inside the folder currently open in Explorer."
            if not open_path_safe(str(target_path)):
                return False, f"I couldn't open {target_path.name}."
            session.last_opened_file = str(target_path)
            set_active_folder(str(target_path.parent))
            context["current_file"] = str(target_path)
            return True, f"Opened {target_path.name}."
        if target.lower().endswith((" folder", " directory")):
            from tools.system_tools import open_path_safe, set_active_folder
            folder_name = re.sub(r"\s+(?:folder|directory)$", "", target, flags=re.I)
            try:
                folder_path = resolve_in_workspace(folder_name)
            except WorkspaceError as error:
                return False, str(error)
            if not folder_path.is_dir():
                return False, f"I couldn't find the {folder_name} folder in the workspace."
            if not open_path_safe(str(folder_path)):
                return False, f"I couldn't open the {folder_name} folder."
            set_active_folder(str(folder_path))
            return True, f"Opened the {folder_path.name} folder."
        # Resolve standard Windows folders before any active-folder file
        # lookup. Otherwise a request such as "open Downloads folder" can
        # accidentally match a similarly named file (for example downloads.png).
        folder_target = re.sub(r"\s+(?:folder|directory)$", "", target.strip(), flags=re.IGNORECASE)
        folder_target = re.sub(r"^(?:the|my)\s+", "", folder_target, flags=re.IGNORECASE).strip().lower()
        standard_folders = {
            "desktop": "Desktop",
            "downloads": "Downloads",
            "documents": "Documents",
            "pictures": "Pictures",
            "photos": "Pictures",
            "videos": "Videos",
            "music": "Music",
            "home": "",
            "user folder": "",
        }
        if folder_target in standard_folders:
            from tools.system_tools import open_path_safe, set_active_folder, resolve_in_workspace, WorkspaceError
            try:
                folder_path = resolve_in_workspace(folder_target)
            except WorkspaceError as error:
                return False, str(error)
            if not folder_path.is_dir():
                return False, f"I couldn't find a {folder_target} folder inside the workspace."
            folder_str = str(folder_path)
            if not open_path_safe(folder_str):
                return False, f"I couldn't open the {folder_target} folder."
            set_active_folder(folder_str)
            bname = folder_path.name or str(folder_path)
            context["current_app"] = bname
            session.set_current_app(bname)
            return True, f"Opened {bname} folder."

        from tools.system_tools import WEBSITE_URLS, _website_destination_url
        site_url, canonical_site = _website_destination_url(target)
        is_domain = bool(re.match(r"^(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?$", target, flags=re.IGNORECASE))
        if is_domain:
            return execute_action(Action(verb="navigate", target=canonical_site or target, raw=action.raw), context)
        # 1. Check if target is a PC folder first (exclude web domains)
        known_sites = set(WEBSITE_URLS)
        if target.lower() not in known_sites:
            from tools.system_tools import find_pc_folder, open_path_safe, set_active_folder
            folder_path = find_pc_folder(target)
            if folder_path and os.path.isdir(str(folder_path)):
                if not open_path_safe(str(folder_path)):
                    return False, f"I couldn't open the {os.path.basename(str(folder_path))} folder."
                set_active_folder(str(folder_path))
                bname = os.path.basename(str(folder_path))
                context["current_app"] = bname
                session.set_current_app(bname)
                return True, f"Opened {bname} folder."

        # 2. Check if already open window (single-instance enforcement)
        from tools.window_tools import canonical_app_name, SW_RESTORE
        clean_target = canonical_app_name(target)
        # Let open_app perform process-level reuse after the explicit title
        # lookup. Duplicating process detection here can mistake an unrelated
        # background process for the requested app and bypass launch errors.
        hwnd = find_any_window(clean_target) or find_any_window(target)
        if hwnd:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
            if not force_foreground_window(hwnd):
                return False, f"I couldn't bring {target.title()} to the foreground."
            context["current_app"] = clean_target or target
            if _browser_name(clean_target or target):
                context["browser"] = _browser_name(clean_target or target)
            session.set_current_app(clean_target or target)
            return True, f"{target.title()} is already open."

        # 2b. Check if target is a file in the active folder hierarchy
        # Avoids slow app cache refreshes for files/episodes (e.g. "163", "asta", "ep 163")
        from tools.system_tools import find_file_in_hierarchy, get_active_folder, _open_media_file, _MEDIA_EXT, open_path_safe, set_active_folder
        from tools.window_tools import APP_OPEN_COMMANDS, APP_PROCESSES, canonical_app_name
        target_lower = target.lower().strip()
        active = get_active_folder()
        known_apps = set(APP_OPEN_COMMANDS) | set(APP_PROCESSES)
        if (target.isdigit() or canonical_app_name(target_lower) not in known_apps) and active:
            file_hit = find_file_in_hierarchy(target, active)
            if file_hit and os.path.isfile(file_hit):
                ext = os.path.splitext(file_hit)[1].lower()
                bname = os.path.basename(file_hit)
                if ext in _MEDIA_EXT:
                    if _open_media_file(file_hit):
                        session.last_played_media = file_hit
                        set_active_folder(os.path.dirname(file_hit))
                        context["current_app"] = bname
                        session.set_current_app(bname)
                        return True, f"Playing {bname}."
                else:
                    if open_path_safe(file_hit):
                        session.last_opened_file = file_hit
                        set_active_folder(os.path.dirname(file_hit))
                        context["current_app"] = bname
                        session.set_current_app(bname)
                        return True, f"Opened {bname}."

        res = open_app(target)
        if not res or _action_report_is_failure(res):
            # A known app failure must remain a failure. Do not let a
            # same-named workspace/index file turn it into a false success.
            if canonical_app_name(target_lower) in known_apps:
                return False, res or f"I couldn't open {target}."
            # Check if target matches a file in active folder hierarchy or index
            from tools.system_tools import find_file_in_hierarchy, get_active_folder, _open_media_file, _MEDIA_EXT, open_path_safe, set_active_folder
            active = get_active_folder()
            file_hit = find_file_in_hierarchy(target, active)
            if file_hit and os.path.isfile(file_hit):
                ext = os.path.splitext(file_hit)[1].lower()
                bname = os.path.basename(file_hit)
                if ext in _MEDIA_EXT:
                    if _open_media_file(file_hit):
                        session.last_played_media = file_hit
                        set_active_folder(os.path.dirname(file_hit))
                        context["current_app"] = bname
                        session.set_current_app(bname)
                        return True, f"Playing {bname}."
                else:
                    if open_path_safe(file_hit):
                        session.last_opened_file = file_hit
                        set_active_folder(os.path.dirname(file_hit))
                        context["current_app"] = bname
                        session.set_current_app(bname)
                        return True, f"Opened {bname}."
            return False, res or f"I couldn't open {target}."
        context["current_app"] = target
        if _browser_name(target):
            context["browser"] = _browser_name(target)
        session.set_current_app(target)

        # Wait + verify: poll up to 1.5s for the window to appear
        start = time.time()
        while time.time() - start < 1.5:
            time.sleep(0.1)
            h = find_any_window(target)
            if h and user32.IsWindowVisible(h):
                force_foreground_window(h)
                break

        return True, res

    # ---- 8. NAVIGATE / WEBSITE ----
    if verb == "navigate":
        site = action.target or ""
        requested_browser = _browser_name(action.args.get("browser") or context.get("browser"))
        current_app = (context.get("current_app") or session.current_app or "").lower()
        if not requested_browser:
            requested_browser = _browser_name(current_app)
        from tools.system_tools import _website_destination_url, open_url
        site_url, canonical_site = _website_destination_url(site)
        if site_url:
            if not open_url(site_url, browser=requested_browser):
                return False, f"I couldn't open {canonical_site}."
            context["website"] = canonical_site
            context["destination"] = canonical_site
            context["current_app"] = requested_browser
            if requested_browser:
                context["browser"] = requested_browser
                session.set_current_app(requested_browser)
            session.last_opened_website = canonical_site
            return True, f"Opening {canonical_site}" + (f" in {requested_browser.title()}." if requested_browser else ".")
        # Safeguard: if target explicitly mentions folder or is a local folder, open it!
        if re.search(r"\b(?:folder|directory)\b", site, re.IGNORECASE) or "\\" in site or "/" in site:
            from tools.system_tools import find_pc_folder, open_path_safe, set_active_folder
            folder_path = find_pc_folder(site)
            if folder_path and os.path.isdir(str(folder_path)):
                open_path_safe(str(folder_path))
                set_active_folder(str(folder_path))
                bname = os.path.basename(str(folder_path))
                context["current_app"] = bname
                session.set_current_app(bname)
                return True, f"Opened {bname} folder."

        # If a browser window showing this website is already open, focus it!
        hwnd = find_any_window(site)
        if hwnd:
            force_foreground_window(hwnd)
            context["current_app"] = site
            session.set_current_app(site)
            session.last_opened_website = site
            return True, f"Switched to {site.title()}."

        # Otherwise open it via the website router
        from tools.system_tools import handle_open_website
        res = handle_open_website(f"open {site} website", browser=requested_browser) or handle_open_website(f"open {site}", browser=requested_browser)
        if not res:
            return False, f"I couldn't open {site}."
        session.last_opened_website = site
        context["website"] = canonical_site or site
        context["destination"] = canonical_site or site
        context["current_app"] = requested_browser
        if requested_browser:
            context["browser"] = requested_browser
        return True, res or f"Opening {site.title()}."

    # ---- 9. PLAY FIRST VIDEO ----
    if verb == "play_first_video":
        if not session.youtube_search_query:
            return False, "Search YouTube first, then say 'play first video'."
        from tools.system_tools import handle_youtube_command
        youtube_result = handle_youtube_command("play first video")
        if youtube_result and "search for a youtube video first" not in youtube_result.lower():
            return not _action_report_is_failure(youtube_result), youtube_result
        if session.youtube_search_query:
            return False, "I couldn't fetch the first YouTube result. The search results are open; select a result and try again."
        return False, "Search YouTube first, then say 'play first video'."

    # ---- 10. PLAY CURRENT VIDEO / RESUME ----
    if verb == "play_current_video":
        if action.args.get("destination"):
            return _route_play(Action(verb="play", target="video", args=action.args, raw=action.raw), context, action.args["destination"])
        if action.args.get("youtube"):
            from tools.system_tools import _play_current_youtube_video
            browser = action.args.get("browser") or context.get("browser") or context.get("current_app")
            return True, _play_current_youtube_video(browser=browser)
        from tools.system_tools import handle_media_command
        res = handle_media_command("play")
        message = res or "Playing current video."
        return not _action_report_is_failure(message), message

    # ---- 11. PLAY GENERAL ----
    if verb == "play":
        destination = action.args.get("destination") or context.get("website")
        current_app = context.get("current_app") or session.current_app or ""
        if not destination and current_app.lower() in {"vlc", "spotify"}:
            if current_app.lower() != "vlc" or find_any_window("vlc"):
                destination = current_app
        if not destination and get_window_process_name(get_foreground_window()).lower() == "vlc.exe":
            destination = "vlc"
        if destination or action.args.get("browser"):
            if action.args.get("browser") and not destination:
                # A browser is a transport destination, but a named media
                # website is needed to select a particular online video.
                context["browser"] = _browser_name(action.args["browser"])
            return _route_play(action, context, destination)
        if action.target:
            current_app = (context.get("current_app") or session.current_app or "").lower()
            last_website = (session.last_opened_website or "").lower()
            foreground_title = get_window_title(user32.GetForegroundWindow()).lower()
            generic_targets = {"music", "song", "video", "playback", "it", "that", "this"}
            browser = action.args.get("browser") or context.get("browser")
            if not browser and current_app in {"chrome", "google chrome", "brave", "brave browser", "edge", "microsoft edge"}:
                browser = current_app
            is_youtube_context = (
                action.args.get("youtube")
                or "youtube" in current_app
                or "youtube" in last_website
                or "youtube" in foreground_title
            )
            if is_youtube_context:
                if action.target.lower().strip() in generic_targets:
                    from tools.system_tools import _play_current_youtube_video
                    return True, _play_current_youtube_video(browser=browser)
                from tools.system_tools import play_youtube
                result = play_youtube(action.target, browser=browser)
                if isinstance(result, tuple):
                    ok, msg = result
                else:
                    msg = result or f"I couldn't open YouTube search for {action.target}."
                    ok = bool(result) and not _action_report_is_failure(msg)
                if ok:
                    session.last_played_media = action.target
                return ok, msg

            from tools.system_tools import handle_play_command
            res = handle_play_command(f"play {action.target}")
            if res:
                return not _action_report_is_failure(res), res

        # General playback toggle
        from tools.system_tools import handle_media_command
        res = handle_media_command("play")
        message = res or "Playing."
        return not _action_report_is_failure(message), message

    # ---- 12. PAUSE ----
    if verb == "pause":
        explicit_destination = action.args.get("destination")
        destination = explicit_destination or context.get("website") or context.get("current_app") or session.current_app
        if not explicit_destination and destination and destination.lower() == "vlc" and not find_any_window("vlc"):
            destination = None
        if not destination and get_window_process_name(get_foreground_window()).lower() == "vlc.exe":
            destination = "vlc"
        if destination:
            return _route_transport(verb, action, context, destination)
        from tools.system_tools import handle_media_command
        res = handle_media_command("pause")
        message = res or "Paused."
        return not _action_report_is_failure(message), message

    # ---- 13. STOP ----
    if verb == "stop":
        destination = action.args.get("destination") or context.get("website") or context.get("current_app")
        if destination:
            return _route_transport(verb, action, context, destination)
        from tools.system_tools import handle_media_command
        res = handle_media_command("stop music")
        message = res or "Stopped."
        return not _action_report_is_failure(message), message

    # ---- 14. NEXT / PREV TRACK ----
    if verb == "next_track":
        destination = action.args.get("destination") or context.get("website") or context.get("current_app")
        if destination:
            return _route_transport(verb, action, context, destination)
        from tools.system_tools import handle_media_command
        res = handle_media_command("next")
        message = res or "Skipping to next track."
        return not _action_report_is_failure(message), message

    if verb == "prev_track":
        destination = action.args.get("destination") or context.get("website") or context.get("current_app")
        if destination:
            return _route_transport(verb, action, context, destination)
        from tools.system_tools import handle_media_command
        res = handle_media_command("previous")
        message = res or "Going to previous track."
        return not _action_report_is_failure(message), message

    # ---- 15. APP-SPECIFIC FULLSCREEN ----
    if verb == "fullscreen":
        target = resolve_target(action.target, context)
        if target:
            switch_result = switch_to_app(target)
            if _action_report_is_failure(switch_result):
                return False, switch_result
            time.sleep(0.1)
        fg = user32.GetForegroundWindow()
        process_name = get_window_process_name(fg)
        if target and "vlc" in target.lower() and process_name != "vlc.exe":
            return False, "VLC isn't the active window, so I didn't send a fullscreen key."
        if target and target.lower().strip() in {"youtube", "you tube", "yt", "youtube app"}:
            if "youtube" not in get_window_title(fg).lower():
                return True, "YouTube is opening. Try fullscreen again after its video appears."
        if process_name == "vlc.exe":
            _press_key(VK_F)
        else:
            _press_key(VK_F11)
        return True, "Toggled fullscreen."

    # ---- 16. TYPE / WRITE ----
    if verb == "type":
        text_to_type = action.target or ""
        _type_unicode(text_to_type)
        return True, f"Typed {text_to_type}."

    # ---- 17. SEARCH ----
    if verb == "search":
        destination = action.args.get("destination") or context.get("website")
        if not destination and context.get("current_app") and not _browser_name(context.get("current_app")):
            destination = context["current_app"]
        return _route_search(action.target or "", action, context, destination)

    if verb in {"delete", "rename", "read_file", "search_files", "search_folders", "list_files"}:
        from tools.system_tools import resolve_in_workspace, WorkspaceError
        try:
            if verb == "list_files":
                folder = resolve_in_workspace(action.target or ".", "list")
                from tools.system_tools import is_safe_path
                names = [item.name for item in folder.iterdir() if is_safe_path(str(item), "list")]
                return True, ", ".join(names) or "The workspace is empty."
            if verb == "search_files":
                root = resolve_in_workspace(".", "search")
                query = Path(action.target or "").name.lower()
                from tools.system_tools import is_safe_path
                matches = []
                for current, dirs, files in os.walk(root):
                    if not is_safe_path(current, "search"):
                        dirs.clear()
                        continue
                    dirs[:] = [name for name in dirs if is_safe_path(os.path.join(current, name), "search")]
                    matches.extend(str((Path(current) / name).relative_to(root)) for name in files if query in name.lower() and is_safe_path(str(Path(current) / name), "search"))
                return (True, ", ".join(matches)) if matches else (False, f"I couldn't find a file matching {action.target}.")
            if verb == "search_folders":
                root = resolve_in_workspace(".", "search")
                query = Path(action.target or "").name.lower()
                matches = []
                from tools.system_tools import is_safe_path
                for current, dirs, _files in os.walk(root):
                    if not is_safe_path(current, "search"):
                        dirs.clear()
                        continue
                    dirs[:] = [name for name in dirs if is_safe_path(os.path.join(current, name), "search")]
                    matches.extend(str((Path(current) / name).relative_to(root)) for name in dirs if query in name.lower())
                return (True, ", ".join(matches)) if matches else (False, f"I couldn't find a folder matching {action.target}.")
            path = resolve_in_workspace(action.target or "", "read" if verb == "read_file" else "write")
            if verb == "read_file":
                return True, path.read_text(encoding="utf-8")[:4000]
            if verb == "delete":
                if not path.exists():
                    return False, f"I couldn't find {path.name}."
                path.unlink() if path.is_file() else path.rmdir()
                return True, f"Deleted {path.name}."
            new_path = resolve_in_workspace(str(path.parent / action.args.get("new_name", "")), "write")
            if not path.exists():
                return False, f"I couldn't find {path.name}."
            if new_path.exists():
                return False, f"{new_path.name} already exists."
            path.rename(new_path)
            return True, f"Renamed {path.name} to {new_path.name}."
        except WorkspaceError as error:
            return False, str(error)
        except (OSError, UnicodeError) as error:
            return False, f"I couldn't complete that workspace action: {error}."

    return False, f"Unknown action: {verb}"


# ============================================================
# PUBLIC ENGINE ENTRY POINT
# ============================================================

def execute_command_chain(command: str, confirm_callback=None) -> str | None:
    """
    Execute a multi-step command chain if detected.
    Returns the combined user-facing response string, or None if the command
    is not a multi-step chain.
    """
    actions = parse_command_chain(command)
    if not actions:
        return None

    from core.events import event_bus
    from core.state import State, state_machine
    context: dict[str, Any] = {
        "current_app": session.current_app or session.last_opened_app,
        "current_file": None,
        "website": None,
    }
    context["browser"] = _browser_name(context.get("current_app"))

    dsl_actions = [action_to_dsl(action) for action in actions]
    event_bus.publish("parsed", actions=dsl_actions, raw=command)
    state_machine.set_pending(dsl_actions)
    timeout = get_settings().get("idle_timeout_ms", 30000)
    if state_machine.expire(timeout):
        return "The command chain expired before it started."

    destructive = [action for action in actions if action.verb in {"delete", "rename"}]
    if destructive:
        state_machine.transition(State.CONFIRMING)
        event_bus.publish("confirmation_requested", actions=[action_to_dsl(a) for a in destructive])
        approved = bool(confirm_callback and confirm_callback(destructive))
        if not approved:
            global _PENDING_CONFIRMATION, _PENDING_CONFIRMATION_AT
            _PENDING_CONFIRMATION = command
            _PENDING_CONFIRMATION_AT = time.monotonic()
            destructive_names = ", ".join(action_to_dsl(item) for item in destructive)
            return f"Please confirm these destructive steps: {destructive_names}. Say yes or no."
        _PENDING_CONFIRMATION = None

    results: list[str] = []
    completed = []
    failures = []
    skipped = []
    stop_on_error = get_settings().get("stop_on_error", True)

    for index, action in enumerate(actions):
        state_machine.transition(State.EXECUTING)
        event_bus.publish("step_started", index=index, action=dsl_actions[index], total=len(actions))
        if action.verb == "open" and not action.args.get("workspace"):
            kind, _canonical = _destination_kind(action.target)
            if kind not in {"app", "browser", "website"}:
                action.args["workspace"] = True
        if action.verb in {"open", "open_file_in", "create_file", "read_file", "delete", "rename", "search_files", "search_folders"} and (action.target or "").lower().strip() in {"it", "that", "that file", "this file", "the file", "the same file"}:
            if context.get("current_file"):
                action.target = context["current_file"]
            else:
                success, message = False, "There is no previous file for that pronoun to refer to."
                failures.append((action.raw or dsl_actions[index], message))
                event_bus.publish("step_result", index=index, action=dsl_actions[index], success=False, message=message)
                if stop_on_error:
                    skipped = [item.raw or action_to_dsl(item) for item in actions[index + 1:]]
                    break
                continue
        try:
            success, message = execute_dsl(dsl_actions[index], context, action)
        except Exception as error:
            from core.error_log import log_error
            log_error("Command chain step failed", error)
            success, message = False, "I couldn't complete that step."
        if success and _action_report_is_failure(message):
            success = False
        if message:
            results.append(message)
        if success:
            completed.append(action.raw or dsl_actions[index])
            is_file_action = action.verb in {"create_file", "open_file_in", "read_file", "delete", "rename", "search_files", "search_folders"} or (action.verb == "open" and action.args.get("workspace"))
            if is_file_action:
                candidate = action.target or ""
                if candidate:
                    try:
                        from tools.system_tools import resolve_in_workspace
                        context["current_file"] = str(resolve_in_workspace(candidate))
                    except (OSError, ValueError):
                        pass
        event_bus.publish("step_result", index=index, action=dsl_actions[index], success=success, message=message)
        if not success:
            failures.append((action.raw or dsl_actions[index], message))
            if stop_on_error:
                skipped = [item.raw or action_to_dsl(item) for item in actions[index + 1:]]
                for skipped_index in range(index + 1, len(actions)):
                    event_bus.publish("step_result", index=skipped_index, action=dsl_actions[skipped_index], success=False, skipped=True, message="Skipped after an earlier failure.")
                break

    state_machine.transition(State.IDLE)
    state_machine.set_pending([])
    if not results:
        return None
    summary = ". ".join(r.rstrip(".") for r in results if r)
    if failures:
        summary += f". Completed steps: {', '.join(completed) if completed else 'none'}"
        failure_text = "; ".join(f"'{step}': {reason}" for step, reason in failures)
        summary += f". Failed step{'s' if len(failures) > 1 else ''}: {failure_text}"
        if skipped:
            summary += f". Skipped: {', '.join(skipped)}"
    return summary + ("." if not summary.endswith(".") else "")


def handle_pending_confirmation(command: str) -> str | None:
    """Continue or discard a destructive chain after a short spoken yes/no."""
    global _PENDING_CONFIRMATION
    from core.state import State, state_machine
    pending = _PENDING_CONFIRMATION
    if not pending:
        return None
    if (time.monotonic() - _PENDING_CONFIRMATION_AT) * 1000 > get_settings().get("idle_timeout_ms", 30000):
        _PENDING_CONFIRMATION = None
        state_machine.set_pending([])
        state_machine.transition(State.IDLE)
        return "That confirmation expired."
    if command in {"no", "cancel", "cancel chain"}:
        _PENDING_CONFIRMATION = None
        state_machine.set_pending([])
        state_machine.transition(State.IDLE)
        return "Cancelled the command chain."
    if command in {"yes", "confirm", "confirm chain"}:
        _PENDING_CONFIRMATION = None
        return execute_command_chain(pending, confirm_callback=lambda _actions: True)
    return None


def execute_scoped_action(command: str) -> str | None:
    """Execute one explicitly destination-scoped command outside a chain."""
    action = parse_action(command)
    if not action or not (
        action.args.get("destination")
        or action.args.get("browser")
        or action.verb in {"create_file", "open_file_in"}
        or (action.verb == "open" and Path(action.target or "").suffix.lower() in _KNOWN_FILE_EXTENSIONS)
    ):
        return None
    context: dict[str, Any] = {
        "current_app": session.current_app or session.last_opened_app,
        "browser": _browser_name(session.current_app or session.last_opened_app),
        "website": None,
    }
    _success, message = execute_dsl(action_to_dsl(action), context, action)
    if not message:
        return None
    return message
