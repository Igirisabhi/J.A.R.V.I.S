"""
Browser Profile Discovery and Management for Jarvis.

Supports discovery and launching of profiles for Google Chrome, Microsoft Edge,
and Brave Browser from their respective Local State profile caches.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time as _time
from pathlib import Path
from typing import Any

from core.session_memory import session

_PROFILE_LAUNCH_LOCK = threading.Lock()
_RECENT_PROFILE_LAUNCHES: dict[tuple[str, str], float] = {}


def _get_local_state_path(browser: str = "chrome") -> Path | None:
    """Return the Local State JSON path for the specified browser."""
    browser_clean = (browser or "").lower().strip()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None

    paths = {
        "chrome": Path(local_app_data) / "Google" / "Chrome" / "User Data" / "Local State",
        "google chrome": Path(local_app_data) / "Google" / "Chrome" / "User Data" / "Local State",
        "edge": Path(local_app_data) / "Microsoft" / "Edge" / "User Data" / "Local State",
        "microsoft edge": Path(local_app_data) / "Microsoft" / "Edge" / "User Data" / "Local State",
        "brave": Path(local_app_data) / "BraveSoftware" / "Brave-Browser" / "User Data" / "Local State",
        "brave browser": Path(local_app_data) / "BraveSoftware" / "Brave-Browser" / "User Data" / "Local State",
    }
    path = paths.get(browser_clean)
    if path:
        try:
            if path.is_file():
                return path
        except OSError:
            # Profile discovery is optional; inaccessible browser data must not
            # prevent unrelated voice commands from being parsed.
            return None
    return None


def get_browser_profiles(browser: str = "chrome") -> dict[str, dict[str, Any]]:
    """
    Return a dictionary mapping directory name (e.g. 'Default', 'Profile 1')
    to profile details for the given browser.
    """
    path = _get_local_state_path(browser)
    if not path:
        return {}

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}

    cache = data.get("profile", {}).get("info_cache", {})
    profiles: dict[str, dict[str, Any]] = {}

    for dir_name, info in cache.items():
        if not isinstance(info, dict):
            continue
        display_name = info.get("name") or info.get("shortcut_name") or dir_name
        user_name = info.get("user_name", "")
        gaia_name = info.get("gaia_name", "")
        gaia_given = info.get("gaia_given_name", "")
        shortcut_name = info.get("shortcut_name", "")

        aliases = {
            dir_name.lower(),
            display_name.lower(),
        }
        if user_name:
            aliases.add(user_name.lower())
            aliases.add(user_name.split("@")[0].lower())
        if gaia_name:
            aliases.add(gaia_name.lower())
            for part in gaia_name.lower().split():
                clean_part = re.sub(r"[()]", "", part).strip()
                if len(clean_part) >= 2:
                    aliases.add(clean_part)
        if gaia_given:
            aliases.add(gaia_given.lower())
        if shortcut_name:
            aliases.add(shortcut_name.lower())

        # Phonetic mishearings for profiles (e.g. "Bakka" heard as "backup", "back up", "baka", "baca")
        if "bakka" in {display_name.lower(), gaia_given.lower(), dir_name.lower()} or "bakka" in aliases:
            aliases.update({"bakka", "backup", "back up", "baka", "baca", "bacca", "baggage"})

        if dir_name == "Default":
            aliases.add("default")
            aliases.add("profile 0")
            aliases.add("0")
        elif dir_name.lower().startswith("profile "):
            num = dir_name.split()[-1]
            aliases.add(num)
            aliases.add(f"profile {num}")

        profiles[dir_name] = {
            "dir": dir_name,
            "name": display_name,
            "user_name": user_name,
            "gaia_name": gaia_name,
            "gaia_given_name": gaia_given,
            "aliases": aliases,
        }

    return profiles


def match_browser_profile(query: str, browser: str = "chrome") -> tuple[str, str] | None:
    """
    Match a spoken query to a browser profile.
    Returns (profile_dir, profile_display_name) or None.
    """
    if not query:
        return None

    clean_q = query.lower().strip()
    clean_q = re.sub(r"^(?:the\s+)?profile\s+", "", clean_q).strip()
    clean_q = re.sub(r"\s+profile$", "", clean_q).strip()
    clean_q = re.sub(r"^(?:the\s+)?account\s+", "", clean_q).strip()
    clean_q = re.sub(r"\s+account$", "", clean_q).strip()

    profiles = get_browser_profiles(browser)
    if not profiles:
        return None

    # 1. Exact alias match
    for dir_name, data in profiles.items():
        if clean_q in data.get("aliases", set()):
            return dir_name, data.get("name", dir_name)

    # 2. Substring in display name or gaia name
    for dir_name, data in profiles.items():
        name_lower = data.get("name", "").lower()
        gaia_lower = data.get("gaia_name", "").lower()
        if (name_lower and clean_q in name_lower) or (gaia_lower and clean_q in gaia_lower):
            return dir_name, data.get("name", dir_name)

    # 3. Any word in query matches any alias
    query_words = set(clean_q.split())
    for dir_name, data in profiles.items():
        for alias in data.get("aliases", set()):
            if alias in query_words and len(alias) >= 3:
                return dir_name, data.get("name", dir_name)

    return None


def find_profile_across_browsers(query: str) -> tuple[str, str, str] | None:
    """
    Search for a profile query across supported browsers (Chrome, Edge, Brave).
    Returns (browser_name, profile_dir, profile_display_name) or None.
    """
    if not query or not query.strip():
        return None
    for b in ("chrome", "edge", "brave"):
        matched = match_browser_profile(query, b)
        if matched:
            return b, matched[0], matched[1]
    return None


def _find_browser_executable(browser: str = "chrome") -> str | None:
    """Find executable path for Chrome, Edge, or Brave."""
    browser_clean = (browser or "").lower().strip()
    if browser_clean in {"chrome", "google chrome"}:
        from tools.browser_controller import find_chrome_executable
        exe = find_chrome_executable()
        if exe:
            return exe
        return "chrome.exe"
    elif browser_clean in {"edge", "microsoft edge"}:
        for p in [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]:
            if os.path.isfile(p):
                return p
        return "msedge.exe"
    elif browser_clean in {"brave", "brave browser"}:
        for p in [
            r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        ]:
            if os.path.isfile(p):
                return p
        return "brave.exe"
    return None


def open_browser_with_profile(browser: str = "chrome", profile_query: str | None = None) -> str:
    """
    Open or switch to a specific browser profile.
    If profile_query is empty, lists available profiles.
    """
    browser_clean = "chrome" if "chrome" in (browser or "").lower() else ("edge" if "edge" in (browser or "").lower() else "brave")
    profiles = get_browser_profiles(browser_clean)

    if not profiles:
        # Fallback to standard app open if no profiles exist in Local State
        from tools.window_tools import open_app
        return open_app(browser_clean)

    profile_names = [data["name"] for data in profiles.values()]

    if not profile_query or not profile_query.strip():
        if len(profile_names) == 1:
            # Only one profile exists, open it directly
            dir_name = list(profiles.keys())[0]
            name = profile_names[0]
            return _launch_profile(browser_clean, dir_name, name)
        names_str = " and ".join(profile_names) if len(profile_names) == 2 else ", ".join(profile_names)
        return f"You have {len(profile_names)} {browser_clean.title()} profiles: {names_str}. Which one would you like to open?"

    matched = match_browser_profile(profile_query, browser_clean)
    if not matched:
        names_str = ", ".join(profile_names)
        return f"I couldn't find a profile named '{profile_query}'. Available profiles are: {names_str}."

    profile_dir, profile_name = matched
    return _launch_profile(browser_clean, profile_dir, profile_name)


def _launch_profile(browser: str, profile_dir: str, profile_name: str) -> str:
    """Launch the browser with the requested profile directory."""
    exe = _find_browser_executable(browser)
    if not exe:
        return f"I couldn't find the {browser.title()} executable."

    launch_key = (browser.lower(), profile_dir.lower())
    now = _time.monotonic()
    with _PROFILE_LAUNCH_LOCK:
        previous = _RECENT_PROFILE_LAUNCHES.get(launch_key, 0.0)
        if now - previous < 5.0:
            session.set_current_app(browser)
            return f"{browser.title()} profile {profile_name} is already opening."
        _RECENT_PROFILE_LAUNCHES[launch_key] = now

    cmd = [exe, f"--profile-directory={profile_dir}"]
    try:
        creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0)
        executable_path = Path(exe)
        working_directory = str(executable_path.parent) if executable_path.is_file() else None
        subprocess.Popen(
            cmd,
            cwd=working_directory,
            creationflags=creation_flags,
            close_fds=True,
        )
        session.set_current_app(browser)
        # Give Windows a moment to spawn/focus the profile window
        try:
            _time.sleep(0.2)
            from tools.window_tools import find_any_window, force_foreground_window
            hwnd = find_any_window(browser)
            if hwnd:
                force_foreground_window(hwnd)
        except Exception:
            pass
        return f"Opening {browser.title()} with profile {profile_name}."
    except Exception as e:
        with _PROFILE_LAUNCH_LOCK:
            _RECENT_PROFILE_LAUNCHES.pop(launch_key, None)
        return f"I couldn't open {browser.title()} with profile {profile_name}: {e}"


def handle_browser_profile_command(command: str) -> str | None:
    """
    Handle spoken commands related to browser profiles.
    Examples:
      - "open chrome profile bakka"
      - "open chrome with profile bakka"
      - "open chrome in profile bakka"
      - "open chrome bakka profile"
      - "open profile bakka in chrome"
      - "open chrome bakka" (if bakka matches a profile)
      - "open bakka profile" / "bakka profile" / "profile bakka"
      - "open bakka" / "bakka"
      - "tell it to open bakka profile"
      - "switch to bakka profile" / "switch to bakka"
      - "list chrome profiles" / "show chrome profiles" / "list profiles"
    """
    if not command:
        return None

    clean = command.strip().lower()
    clean = re.sub(r"[,:;!?]+", " ", clean).strip()
    clean = re.sub(r"\s+", " ", clean)

    # Strip indirect spoken directive prefixes: e.g. "tell it to", "tell jarvis to", "please"
    clean = re.sub(r"^(?:please\s+)?(?:tell\s+(?:it|jarvis|him|her)\s+to\s+)?", "", clean).strip()

    browser_kw = r"(chrome|google chrome|edge|microsoft edge|brave|brave browser)"

    # 1. Listing / asking for profiles:
    # "list chrome profiles", "show chrome profiles", "what chrome profiles do i have", "which chrome profiles"
    list_match = re.search(
        rf"^(?:list|show|what(?:\s+are)?|which)\s+(?:all\s+)?{browser_kw}\s+profiles?$",
        clean,
        re.IGNORECASE,
    )
    if list_match:
        browser = list_match.group(1)
        return open_browser_with_profile(browser, profile_query="")

    # Generic: "list profiles", "show profiles", "what profiles"
    if re.fullmatch(r"^(?:list|show|what(?:\s+are)?|which)\s+(?:all\s+)?(?:browser\s+)?profiles?$", clean):
        return open_browser_with_profile("chrome", profile_query="")

    # 2. "open/launch/start/switch to profile <name> in/on/with <browser>"
    m = re.match(
        rf"^(?:open|launch|start|switch\s+to)\s+profile\s+(.+?)\s+(?:in|on|with)\s+{browser_kw}$",
        clean,
        re.IGNORECASE,
    )
    if m:
        profile_query = m.group(1).strip()
        browser = m.group(2).strip()
        return open_browser_with_profile(browser, profile_query=profile_query)

    # 3. "open/launch/start/switch to <browser> [with/in] profile [<name>]"
    m = re.match(
        rf"^(?:open|launch|start|switch\s+to)\s+{browser_kw}\s+(?:with\s+|in\s+)?profile(?:\s+(.+))?$",
        clean,
        re.IGNORECASE,
    )
    if m:
        browser = m.group(1).strip()
        profile_query = (m.group(2) or "").strip()
        return open_browser_with_profile(browser, profile_query=profile_query)

    # 4. "open/launch/start/switch to <browser> <name> profile"
    m = re.match(
        rf"^(?:open|launch|start|switch\s+to)\s+{browser_kw}\s+(.+?)\s+profile$",
        clean,
        re.IGNORECASE,
    )
    if m:
        browser = m.group(1).strip()
        profile_query = m.group(2).strip()
        return open_browser_with_profile(browser, profile_query=profile_query)

    # 5. Direct profile name with browser: "open <browser> <target>"
    m = re.match(
        rf"^(?:open|launch|start|switch\s+to)\s+{browser_kw}\s+(.+)$",
        clean,
        re.IGNORECASE,
    )
    if m:
        browser = m.group(1).strip()
        target = m.group(2).strip()
        non_profile = {"new tab", "incognito", "private", "window", "website", "tab", "site", "page", "browser"}
        if target not in non_profile and not any(target.endswith(" " + s) for s in ("website", "site", "page", "tab", "window")):
            matched = match_browser_profile(target, browser)
            if matched:
                return open_browser_with_profile(browser, profile_query=target)

    # 6. Profile mentioned without browser (e.g. "open bakka profile", "bakka profile", "profile bakka")
    m_no_b = re.match(
        r"^(?:open|launch|start|switch\s+to)?\s*(?:the\s+)?(?:profile\s+)?(.+?)(?:\s+profile)?$",
        clean,
        re.IGNORECASE,
    )
    if m_no_b:
        candidate_name = m_no_b.group(1).strip()
        reserved = {
            "", "the", "a", "an", "it", "this", "that", "all", "app", "apps",
            "file", "files", "folder", "folders", "window", "windows",
            "computer", "pc", "desktop", "browser", "internet", "google",
            "chrome", "edge", "brave", "youtube", "yt", "vlc", "notepad",
            "calculator", "calc", "cmd", "terminal", "task manager", "settings",
            "camera", "photos", "pictures", "music", "videos", "downloads", "documents",
        }
        if candidate_name.lower() not in reserved:
            found = find_profile_across_browsers(candidate_name)
            if found:
                browser_name, p_dir, p_name = found
                return open_browser_with_profile(browser_name, profile_query=candidate_name)

    return None
