import os
import ctypes
import subprocess
import re
import webbrowser
import time



# ============================================================
# WINDOWS API
# ============================================================

from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.IsZoomed.argtypes = [wintypes.HWND]
user32.IsZoomed.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_MAXIMIZE = 3
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9

WM_CLOSE = 0x0010
WM_SYSCOMMAND = 0x0112
WM_APPCOMMAND = 0x0319
SC_MINIMIZE = 0xF020
APPCOMMAND_MEDIA_STOP = 13
APPCOMMAND_MEDIA_NEXTTRACK = 11
APPCOMMAND_MEDIA_PREVIOUSTRACK = 12
APPCOMMAND_MEDIA_PLAY = 46
APPCOMMAND_MEDIA_PAUSE = 47
VK_LWIN = 0x5B
VK_D = 0x44
VK_F = 0x46
VK_F11 = 0x7A
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
GA_ROOT = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MONITOR_DEFAULTTONEAREST = 2


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL



# ============================================================
# APP PROCESS NAMES
# ============================================================

APP_PROCESSES = {
    "chrome": ["chrome.exe"],
    "google chrome": ["chrome.exe"],

    "brave": ["brave.exe"],
    "brave browser": ["brave.exe"],

    "edge": ["msedge.exe"],
    "microsoft edge": ["msedge.exe"],

    "vs code": ["Code.exe"],
    "vscode": ["Code.exe"],
    "visual studio code": ["Code.exe"],

    "notepad": ["notepad.exe"],

    "calculator": [
        "CalculatorApp.exe",
        "ApplicationFrameHost.exe",
    ],

    "calc": [
        "CalculatorApp.exe",
        "ApplicationFrameHost.exe",
    ],

    "paint": ["mspaint.exe"],

    "file explorer": ["explorer.exe"],
    "explorer": ["explorer.exe"],

    "spotify": ["Spotify.exe"],
    "discord": ["Discord.exe"],
    "steam": ["steam.exe"],
    "whatsapp": ["WhatsApp.Root.exe", "WhatsApp.exe"],

    # VLC
    "vlc": ["vlc.exe"],
    "vlc media player": ["vlc.exe"],
    "video lan": ["vlc.exe"],
    "videolan": ["vlc.exe"],
}


# ============================================================
# APP OPEN COMMANDS
# ============================================================

APP_OPEN_COMMANDS = {
    "whatsapp": [
        "cmd",
        "/c",
        "start",
        "",
        "whatsapp:",
    ],
    "chrome": [
        "cmd",
        "/c",
        "start",
        "",
        "chrome",
    ],

    "google chrome": [
        "cmd",
        "/c",
        "start",
        "",
        "chrome",
    ],

    "brave": [
        "cmd",
        "/c",
        "start",
        "",
        "brave",
    ],

    "brave browser": [
        "cmd",
        "/c",
        "start",
        "",
        "brave",
    ],

    "edge": [
        "cmd",
        "/c",
        "start",
        "",
        "msedge",
    ],

    "microsoft edge": [
        "cmd",
        "/c",
        "start",
        "",
        "msedge",
    ],

    "vs code": [
        "cmd",
        "/c",
        "start",
        "",
        "code",
    ],

    "vscode": [
        "cmd",
        "/c",
        "start",
        "",
        "code",
    ],

    "visual studio code": [
        "cmd",
        "/c",
        "start",
        "",
        "code",
    ],

    "notepad": [
        "notepad.exe",
    ],

    "calculator": [
        "calc.exe",
    ],

    "calc": [
        "calc.exe",
    ],

    "paint": [
        "mspaint.exe",
    ],

    "file explorer": [
        "explorer.exe",
    ],

    "explorer": [
        "explorer.exe",
    ],

    "spotify": [
        "spotify.exe",
    ],

    "discord": [
        "discord.exe",
    ],

    "steam": [
        "steam.exe",
    ],

    # VLC
    "vlc": [
        "vlc.exe",
    ],

    "vlc media player": [
        "vlc.exe",
    ],

    "video lan": [
        "vlc.exe",
    ],

    "videolan": [
        "vlc.exe",
    ],
}

# These products have useful web clients. Only these named apps may fall back
# to the browser when their desktop launch fails; native-only apps never do.
APP_WEB_FALLBACKS = {
    "whatsapp": "https://web.whatsapp.com/",
    "spotify": "https://open.spotify.com/",
    "discord": "https://discord.com/app",
}


# ============================================================
# WINDOW TITLE KEYWORDS
# ============================================================

WINDOW_KEYWORDS = {
    "chrome": [
        "chrome",
    ],

    "google chrome": [
        "chrome",
    ],

    "brave": [
        "brave",
    ],

    "brave browser": [
        "brave",
    ],

    "edge": [
        "edge",
    ],

    "microsoft edge": [
        "edge",
    ],

    "vs code": [
        "visual studio code",
        "vs code",
    ],

    "vscode": [
        "visual studio code",
        "vs code",
    ],

    "visual studio code": [
        "visual studio code",
        "vs code",
    ],

    "notepad": [
        "notepad",
    ],

    "calculator": [
        "calculator",
    ],

    "calc": [
        "calculator",
    ],

    "paint": [
        "paint",
    ],

    "file explorer": [
        "file explorer",
    ],

    "explorer": [
        "file explorer",
    ],

    "spotify": [
        "spotify",
    ],

    "discord": [
        "discord",
    ],

    "steam": [
        "steam",
    ],

    # VLC
    "vlc": [
        "vlc",
        "vlc media player",
    ],

    "vlc media player": [
        "vlc",
        "vlc media player",
    ],

    "video lan": [
        "vlc",
        "vlc media player",
    ],

    "videolan": [
        "vlc",
        "vlc media player",
    ],

    # YouTube
    "youtube": [
        "youtube",
        "- youtube",
    ],
}


# ============================================================
# NORMALIZE
# ============================================================

def normalize(text):

    if not text:
        return ""

    text = text.lower().strip()

    text = re.sub(
        r"[.,!?;:]+$",
        "",
        text,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    text = re.sub(
        r"\bgoto\b",
        "go to",
        text,
        flags=re.IGNORECASE,
    )

    return text.strip()


def canonical_app_name(name: str) -> str:
    """Normalize common spoken app aliases before window/process matching."""
    clean = normalize(name)
    clean = re.sub(r"\s+(?:(?:web\s+)?browser|desktop\s+app|application|app|window)$", "", clean)
    aliases = {
        "google chrome": "chrome",
        "microsoft edge": "edge",
        "visual studio code": "vs code",
        "visual studio": "vs code",
        "windows explorer": "file explorer",
        "file manager": "file explorer",
        "vlc media player": "vlc",
        "vlc player": "vlc",
        "v l c": "vlc",
        "v l c media player": "vlc",
        "video lan": "vlc",
        "videolan": "vlc",
    }
    return aliases.get(clean, clean)


# ============================================================
# GET FOREGROUND WINDOW
# ============================================================

def get_foreground_window():

    return user32.GetForegroundWindow()


# ============================================================
# GET WINDOW TITLE
# ============================================================

def get_window_title(hwnd):

    if not hwnd:
        return ""

    length = user32.GetWindowTextLengthW(
        hwnd
    )

    if length <= 0:
        return ""

    buffer = ctypes.create_unicode_buffer(
        length + 1
    )

    user32.GetWindowTextW(
        hwnd,
        buffer,
        length + 1,
    )

    return buffer.value


def get_window_class(hwnd):
    """Return the Win32 class name without spawning a process."""
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


# ============================================================
# GET WINDOW PROCESS NAME
# ============================================================

def get_window_process_name(hwnd):

    if not hwnd:
        return ""

    process_id = wintypes.DWORD()

    user32.GetWindowThreadProcessId(
        hwnd,
        ctypes.byref(process_id),
    )

    if not process_id.value:
        return ""

    h_proc = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        process_id.value,
    )

    if not h_proc:
        return ""

    buf = ctypes.create_unicode_buffer(512)
    size = wintypes.DWORD(512)

    try:
        if kernel32.QueryFullProcessImageNameW(
            h_proc,
            0,
            buf,
            ctypes.byref(size),
        ):
            return os.path.basename(buf.value).lower()
        return ""
    except Exception:
        return ""
    finally:
        kernel32.CloseHandle(h_proc)



# ============================================================
# FIND APP WINDOWS
# ============================================================

def find_app_windows(name):

    name = normalize(name)

    keywords = WINDOW_KEYWORDS.get(
        name,
        [],
    )

    processes = APP_PROCESSES.get(
        name,
        [],
    )

    found_windows = []

    def enum_callback(hwnd, _):

        if not user32.IsWindowVisible(hwnd):
            return True

        title = get_window_title(hwnd)

        process_name = (
            get_window_process_name(hwnd)
        )

        title_lower = title.lower()

        # ----------------------------------------------------
        # TITLE MATCH
        # ----------------------------------------------------

        for keyword in keywords:

            if keyword in title_lower:

                found_windows.append(hwnd)

                return True

        # ----------------------------------------------------
        # PROCESS MATCH
        # ----------------------------------------------------

        if process_name:

            if process_name in [
                process.lower()
                for process in processes
            ]:

                found_windows.append(hwnd)

                return True

        return True

    CALLBACK = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        wintypes.HWND,
        wintypes.LPARAM,
    )

    callback = CALLBACK(
        enum_callback
    )

    user32.EnumWindows(
        callback,
        0,
    )

    if not found_windows:
        h_desk = user32.OpenDesktopW("default", 0, False, 0x0100)
        if h_desk:
            try:
                user32.EnumDesktopWindows(h_desk, callback, 0)
            finally:
                user32.CloseDesktop(h_desk)

    return found_windows



# ============================================================
# FIND SPECIFIC APP WINDOW
# ============================================================

def find_app_window(name):

    windows = find_app_windows(
        name
    )

    if not windows:
        return None

    keywords = WINDOW_KEYWORDS.get(normalize(name), [])

    def rank(hwnd):
        title = normalize(get_window_title(hwnd))
        process_name = get_window_process_name(hwnd)
        score = 0
        if title:
            score += 5
        if any(keyword in title for keyword in keywords):
            score += 100
        if process_name in [p.lower() for p in APP_PROCESSES.get(normalize(name), [])]:
            score += 10
        if user32.IsWindowVisible(hwnd):
            score += 5
        if not user32.IsIconic(hwnd):
            score += 3
        if user32.GetForegroundWindow() == hwnd:
            score += 1
        # VLC can create a second top-level video-output window. Prefer its
        # titled player window so switch/minimize controls hit the main frame.
        if normalize(name) == "vlc":
            if "vlc media player" in title:
                score += 200
            if "video output" in title or "direct3d output" in title:
                score -= 50
        return score

    return max(windows, key=rank)


def is_window_minimized(hwnd) -> bool:
    return bool(user32.IsIconic(hwnd)) if hwnd else False


def is_window_maximized(hwnd) -> bool:
    return bool(user32.IsZoomed(hwnd)) if hwnd else False


def is_window_foreground(hwnd) -> bool:
    return user32.GetForegroundWindow() == hwnd if hwnd else False


def get_window_state(hwnd) -> str:
    if not hwnd or not user32.IsWindow(hwnd):
        return "closed"
    if user32.IsIconic(hwnd):
        return "minimized"
    if user32.IsZoomed(hwnd):
        return "maximized"
    return "normal"


def force_foreground_window(hwnd):
    """Restore, activate, and verify a window using Windows' foreground rules."""
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    if user32.GetForegroundWindow() == hwnd:
        return True

    current_thread = kernel32.GetCurrentThreadId()
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    foreground_thread = user32.GetWindowThreadProcessId(
        user32.GetForegroundWindow(), None
    )
    attached_threads = []
    for thread_id in (foreground_thread, target_thread):
        if (
            thread_id
            and thread_id != current_thread
            and thread_id not in attached_threads
            and user32.AttachThreadInput(current_thread, thread_id, True)
        ):
            attached_threads.append(thread_id)

    try:
        user32.BringWindowToTop(hwnd)
        activated = bool(user32.SetForegroundWindow(hwnd))
        if not activated:
            # An explicit spoken switch is a user-requested foreground change.
            # Windows permits foreground activation after an Alt-key input.
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
    finally:
        for thread_id in reversed(attached_threads):
            user32.AttachThreadInput(current_thread, thread_id, False)

    deadline = time.monotonic() + 0.75
    while time.monotonic() < deadline:
        foreground = user32.GetForegroundWindow()
        if foreground == hwnd or user32.GetAncestor(foreground, GA_ROOT) == hwnd:
            return True
        if not user32.IsWindow(hwnd):
            return False
        time.sleep(0.02)
    return False


def find_window_for_process(proc_name: str) -> int | None:
    """Find a top-level window belonging to an executable name."""
    if not proc_name:
        return None
    target_proc = proc_name.lower().removesuffix(".exe")
    for hwnd in _enumerate_top_level_windows():
        proc = get_window_process_name(hwnd)
        if proc:
            p_base = proc.lower().removesuffix(".exe")
            if p_base == target_proc or target_proc in p_base:
                return hwnd
    return None


def find_any_window(name: str) -> int | None:
    """
    Universal window resolver.
    Handles app names, explorer folders, browser tabs (YouTube/ChatGPT),
    and pronouns ('it', 'that', 'this').
    """
    if not name:
        return None
    name_clean = canonical_app_name(name)
    if not name_clean:
        return None

    # 1. Pronoun resolution
    if name_clean in {"it", "that", "this", "this window", "the window", "current window", "the app", "this app", "current"}:
        from core.session_memory import session
        target = session.current_app or session.last_opened_app
        if target and target.lower() not in {"it", "that", "this"}:
            hwnd = find_any_window(target)
            if hwnd:
                return hwnd
        fg = user32.GetForegroundWindow()
        if fg and user32.IsWindowVisible(fg):
            return fg
        return None

    # 2. Known app dictionary
    hwnd = find_app_window(name_clean)
    if hwnd:
        return hwnd

    # 3. Explorer folder
    hwnd = find_explorer_window(name_clean)
    if hwnd:
        return hwnd

    # 4. Window title match (e.g. YouTube, ChatGPT, etc.)
    hwnd = find_window_by_title(name_clean)
    if hwnd:
        return hwnd

    # 5. Process match across all top-level windows
    for h in _enumerate_top_level_windows():
        proc = get_window_process_name(h)
        if proc:
            proc_lower = proc.lower().removesuffix(".exe")
            if name_clean == proc_lower or name_clean in proc_lower or proc_lower in name_clean:
                return h

    # 6. Check known process names for this app
    for p in APP_PROCESSES.get(name_clean, []):
        h = find_window_for_process(p)
        if h:
            return h

    return None


def _enumerate_top_level_windows():
    """Return visible top-level windows, including minimized Explorer windows."""
    windows = []
    seen = set()

    def callback(hwnd, _):
        if hwnd not in seen and user32.IsWindowVisible(hwnd) and get_window_title(hwnd):
            seen.add(hwnd)
            windows.append(hwnd)
        return True

    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    cb = callback_type(callback)
    user32.EnumWindows(cb, 0)

    if not windows:
        h_desk = user32.OpenDesktopW("default", 0, False, 0x0100)
        if h_desk:
            try:
                user32.EnumDesktopWindows(h_desk, cb, 0)
            finally:
                user32.CloseDesktop(h_desk)

    return windows



def _explorer_locations():
    """Best-effort Explorer HWND/path inventory; title matching remains a fallback."""
    locations = []
    try:
        import win32com.client
        for window in win32com.client.Dispatch("Shell.Application").Windows():
            try:
                hwnd = int(window.HWND)
                path = str(window.Document.Folder.Self.Path)
                locations.append((hwnd, path, get_window_title(hwnd)))
            except Exception:
                continue
    except Exception:
        pass

    seen = {hwnd for hwnd, _, _ in locations}
    for hwnd in _enumerate_top_level_windows():
        if hwnd not in seen and get_window_class(hwnd) in {"CabinetWClass", "ExploreWClass"}:
            locations.append((hwnd, "", get_window_title(hwnd)))
    return locations


def find_explorer_window(folder_name: str):
    target = normalize(folder_name).removesuffix(" folder").strip()
    if not target:
        return None
    for hwnd, path, title in _explorer_locations():
        if target in path.lower() or target in title.lower():
            return hwnd
    return None


def find_window_by_title(name: str):
    """Find an already-open non-Explorer window by its visible title."""
    target = normalize(name)
    if not target:
        return None
    compact_target = target.replace(" ", "")
    for hwnd in _enumerate_top_level_windows():
        title = normalize(get_window_title(hwnd))
        if target in title or compact_target in title.replace(" ", ""):
            return hwnd
    return None


def _activate_window(hwnd):
    return force_foreground_window(hwnd)


def show_desktop():
    """Use Windows' native Win+D Show Desktop toggle, never a filesystem lookup."""
    user32.keybd_event(VK_LWIN, 0, 0, 0)
    user32.keybd_event(VK_D, 0, 0, 0)
    user32.keybd_event(VK_D, 0, 2, 0)
    user32.keybd_event(VK_LWIN, 0, 2, 0)
    return "Showing the desktop."


def _known_website_name(name):
    clean = normalize(name).removesuffix(" website").removesuffix(" site").strip()
    if re.fullmatch(r"(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/.*)?", clean, flags=re.IGNORECASE):
        return True
    try:
        from tools.system_tools import WEBSITE_URLS
        if clean in WEBSITE_URLS:
            return True
    except Exception:
        pass
    return clean in {
        "samsung", "youtube", "google", "github", "chatgpt", "gmail",
        "reddit", "instagram", "facebook", "twitter", "x", "linkedin",
        "amazon", "wikipedia", "netflix", "spotify", "discord", "whatsapp",
        "twitch", "vimeo", "tiktok", "pinterest", "onedrive", "outlook",
        "google drive", "google docs", "google maps", "slack", "teams", "steam",
        "stackoverflow", "notion", "canva", "figma", "quora", "bing", "yahoo",
    }


def _folder_target(command):
    match = re.match(r"^(?:switch to|switch over to|go to|goto|show|bring|focus|activate|close)\s+(.+?)(?:\s+forward)?$", command)
    if not match:
        return None
    target = normalize(match.group(1)).removesuffix(" folder").strip()
    if target.lower() in {"", "it", "that", "this", "window", "the window", "this window", "current window", "the current window", "that window", "down", "up", "top", "bottom", "left", "right"}:
        return None
    return target or None


# Common STT misheard variants of "ChatGPT" across accents.
_CHATGPT_VARIANTS = {
    "chatgpt", "chat gpt", "chatgbt", "chat gbt",
    "chatgtp", "chat gtp", "chatjpt", "chat jpt",
    "jetgpt", "jet gpt", "jatgpt", "jat gpt",
    "chadgpt", "chad gpt", "chatjbt", "chat jbt",
    "chatgpd", "chat gpd", "chatgp", "chat gp",
    "chatgpt.", "chat gpt.", "chatgbd", "chat gbd",
    "chatgpc", "chat gpc", "chatgpi", "chat gpi",
    "chatgpe", "chat gpe", "chatgpti", "chat gpti",
    "chatjpd", "chat jpd", "chatjpg", "chat jpg",
    "chagpt", "cha gpt", "chatjipt", "chat jipt",
    "chatchipt", "chat chipt", "chatchipty", "chat chipty",
    "chatjipiti", "chat jipiti",
}


def _normalize_chatgpt_name(text):
    """Check if a normalized string is a ChatGPT variant."""
    text = normalize(text).rstrip(".")
    # Direct set lookup
    if text in _CHATGPT_VARIANTS:
        return True
    # Collapse all spaces and check
    compact = text.replace(" ", "")
    if compact in {v.replace(" ", "") for v in _CHATGPT_VARIANTS}:
        return True
    return False


def _is_chatgpt_navigation(command):
    """Accept common speech-to-text variants: ChatGPT, chat GPT, chat gpt, jet gpt, etc."""
    match = re.match(
        r"^(?:go to|switch to|open|show|bring|focus|activate|launch|start)\s+(.+?)(?:\s+forward)?$",
        command,
    )
    return _normalize_chatgpt_name(match.group(1)) if match else False


def open_chatgpt_in_chrome():
    """Open ChatGPT in Chrome only when there is no existing ChatGPT window."""
    # Always check for an existing ChatGPT window first.
    hwnd = find_window_by_title("chatgpt")
    if hwnd:
        if not _activate_window(hwnd):
            return "I couldn't bring ChatGPT to the foreground."
        return "Switched to ChatGPT."
    try:
        from tools.browser_controller import navigate_to_url
        if navigate_to_url("https://chatgpt.com/"):
            return "Opening ChatGPT in Chrome."
    except Exception:
        pass
    try:
        from tools.system_tools import open_url
        if open_url("https://chatgpt.com/"):
            return "Opening ChatGPT in Chrome."
    except Exception:
        pass
    return "Opening ChatGPT in Chrome."


# ============================================================
# OPEN APP
# ============================================================

def _has_registered_url_protocol(protocol: str) -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, protocol) as key:
            try:
                winreg.QueryValueEx(key, "URL Protocol")
                return True
            except OSError:
                return False
    except (ImportError, OSError):
        return False

def open_app(name):
    name_clean = canonical_app_name(name)
    if not name_clean:
        return "Please specify an application to open."

    # Route browser profile commands before single-instance check
    try:
        from tools.browser_profiles import handle_browser_profile_command
        prof_res = handle_browser_profile_command(name) or (handle_browser_profile_command(name_clean) if name_clean != name else None)
        if prof_res is not None:
            return prof_res
    except Exception:
        pass

    # Single-instance enforcement: if already open, bring existing instance to front
    hwnd = find_any_window(name_clean) or find_window_for_process(name_clean)
    if not hwnd:
        for p in APP_PROCESSES.get(name_clean, []):
            hwnd = find_window_for_process(p)
            if hwnd:
                break

    if hwnd:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        if not force_foreground_window(hwnd):
            return f"{name_clean.title()} is already open, but I couldn't bring it to the front."
        from core.session_memory import session
        session.set_current_app(name_clean)
        return f"{name_clean.title()} is already open."

    command = APP_OPEN_COMMANDS.get(name_clean)
    if name_clean == "whatsapp" and not _has_registered_url_protocol("whatsapp"):
        # Let the Start Menu shortcut resolver try first, then use the
        # allowlisted web client if no desktop install can be launched.
        command = None

    if not command:
        # Fallback to scanning installed Windows apps
        try:
            from tools.app_tools import open_installed_app
            res = open_installed_app(name_clean)
            if res and not any(
                phrase in res.lower()
                for phrase in ("couldn't", "could not", "not found", "failed")
            ):
                from core.session_memory import session
                session.set_current_app(name_clean)
                return res
        except Exception:
            pass
        fallback_url = APP_WEB_FALLBACKS.get(name_clean)
        if fallback_url:
            from tools.system_tools import open_url
            if open_url(fallback_url):
                return f"{name_clean.title()} isn't installed. Opening its web version."
        return f"I couldn't find or open {name_clean}."

    try:
        subprocess.Popen(
            command,
            shell=False,
        )
        from core.session_memory import session
        session.set_current_app(name_clean)
        return f"Opening {name_clean.title()}."

    except Exception as e:
        print(f"Open app error: {e}")
        # A few installed apps are not on PATH, so retry their Start Menu
        # shortcut before deciding that the desktop app is unavailable.
        try:
            from tools.app_tools import open_installed_app
            installed_result = open_installed_app(name_clean)
            if installed_result and not any(
                phrase in installed_result.lower()
                for phrase in ("couldn't", "could not", "not found", "failed")
            ):
                from core.session_memory import session
                session.set_current_app(name_clean)
                return installed_result
        except Exception:
            pass
        fallback_url = APP_WEB_FALLBACKS.get(name_clean)
        if fallback_url:
            from tools.system_tools import open_url
            if open_url(fallback_url):
                return f"{name_clean.title()} isn't installed. Opening its web version."
        return f"I couldn't open {name_clean}."


# ============================================================
# CLOSE APP
# ============================================================

def close_app(name):

    name = normalize(name)

    processes = APP_PROCESSES.get(
        name
    )

    if not processes:

        return (
            f"I don't know how to close {name}"
        )

    closed = False

    for process in processes:

        result = subprocess.run(
            [
                "taskkill",
                "/IM",
                process,
                "/F",
            ],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        if result.returncode == 0:
            closed = True

    if closed:

        return f"Closed {name}"

    return f"{name} isn't running"


# ============================================================
# MINIMIZE CURRENT WINDOW
# ============================================================

def _is_desktop_or_jarvis_window(hwnd):
    if not hwnd:
        return True
    window_class = get_window_class(hwnd).lower()
    process_name = get_window_process_name(hwnd)
    return (
        window_class in {
            "progman", "workerw", "shell_traywnd", "shell_secondarytraywnd",
            "consolewindowclass",
        }
        or process_name in {
            "python.exe", "pythonw.exe", "cmd.exe", "powershell.exe", "conhost.exe",
            "shellexperiencehost.exe", "startmenuexperiencehost.exe",
            "searchhost.exe", "textinputhost.exe", "applicationframehost.exe",
        }
        or get_window_title(hwnd).strip().casefold() in {
            "quick settings", "notification center", "start menu", "search",
        }
    )


def _minimize_and_verify(hwnd):
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    if user32.IsIconic(hwnd):
        return True

    user32.ShowWindow(hwnd, SW_MINIMIZE)
    deadline = time.monotonic() + 0.35
    while time.monotonic() < deadline:
        if user32.IsIconic(hwnd):
            return True
        time.sleep(0.02)

    # Some applications only honor minimization through their system menu.
    user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_MINIMIZE, 0)
    deadline = time.monotonic() + 0.35
    while time.monotonic() < deadline:
        if user32.IsIconic(hwnd):
            return True
        time.sleep(0.02)
    return False


def _maximize_and_verify(hwnd):
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    if user32.IsZoomed(hwnd):
        return True

    user32.ShowWindow(hwnd, SW_MAXIMIZE)
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        if user32.IsZoomed(hwnd):
            return True
        time.sleep(0.02)
    return False


def minimize_current_window():
    hwnd = get_foreground_window()

    from core.session_memory import session
    target_app = session.current_app or session.last_opened_app

    if (
        not hwnd
        or not user32.IsWindowVisible(hwnd)
        or _is_desktop_or_jarvis_window(hwnd)
    ):
        if target_app:
            return minimize_app(target_app)
        return "I couldn't identify an active app window to minimize."

    if _is_vlc_window(hwnd) and _is_fullscreen_window(hwnd):
        _send_vlc_fullscreen_key(hwnd)
        time.sleep(0.06)

    if _minimize_and_verify(hwnd):
        title = get_window_title(hwnd)
        return f"Minimized {title}." if title else "Window minimized."

    if target_app:
        target_hwnd = find_any_window(target_app)
        if target_hwnd and target_hwnd != hwnd and _minimize_and_verify(target_hwnd):
            return f"Minimized {target_app.title()}."

    title = get_window_title(hwnd)
    label = title or "the active window"
    return f"I couldn't minimize {label}."


# ============================================================
# MAXIMIZE CURRENT WINDOW
# ============================================================

def _send_key(vk: int):
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def _is_vlc_window(hwnd) -> bool:
    """Only send VLC's `F` hotkey to a real VLC process window."""
    return bool(hwnd) and get_window_process_name(hwnd) == "vlc.exe"


def control_vlc_playback(command: str) -> str:
    """Send a discrete media command to VLC without activating its window."""
    commands = {
        "play": (APPCOMMAND_MEDIA_PLAY, "Playing in VLC."),
        "pause": (APPCOMMAND_MEDIA_PAUSE, "Paused in VLC."),
        "stop": (APPCOMMAND_MEDIA_STOP, "Stopped VLC playback."),
        "next_track": (APPCOMMAND_MEDIA_NEXTTRACK, "Skipping to the next VLC track."),
        "prev_track": (APPCOMMAND_MEDIA_PREVIOUSTRACK, "Going to the previous VLC track."),
    }
    command_id, message = commands.get(command, (None, None))
    if command_id is None:
        return f"Unsupported VLC playback command: {command}."

    hwnd = find_any_window("vlc")
    if not hwnd or not _is_vlc_window(hwnd):
        return "VLC isn't open."

    # WM_APPCOMMAND carries distinct play and pause operations and does not
    # bring VLC or Jarvis to the foreground.
    lparam = command_id << 16
    if not user32.PostMessageW(hwnd, WM_APPCOMMAND, hwnd, lparam):
        return f"I couldn't send the {command.replace('_', ' ')} command to VLC."
    return message


def _send_vlc_fullscreen_key(hwnd) -> bool:
    """Send VLC's `F` hotkey only after verifying the foreground process is VLC."""
    if not _is_vlc_window(hwnd) or not force_foreground_window(hwnd):
        return False
    time.sleep(0.03)
    foreground = get_foreground_window()
    if not _is_vlc_window(foreground):
        return False
    _send_key(VK_F)
    return True


def _is_fullscreen_window(hwnd) -> bool:
    """Detect borderless fullscreen windows by comparing them to their monitor."""
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not monitor:
        return False
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return False
    target = info.rcMonitor
    # GetWindowRect may include a few invisible resize-border pixels.
    tolerance = 12
    return all((
        abs(rect.left - target.left) <= tolerance,
        abs(rect.top - target.top) <= tolerance,
        abs(rect.right - target.right) <= tolerance,
        abs(rect.bottom - target.bottom) <= tolerance,
    ))


def maximize_current_window():
    hwnd = get_foreground_window()
    from core.session_memory import session
    target_app = session.current_app or session.last_opened_app

    if not hwnd or not user32.IsWindowVisible(hwnd) or _is_desktop_or_jarvis_window(hwnd):
        hwnd = find_any_window(target_app) if target_app else None
    if not hwnd:
        return "I couldn't identify an active app window to maximize."

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    if not _maximize_and_verify(hwnd):
        title = get_window_title(hwnd)
        return f"I couldn't maximize {title or 'the active window'}."
    force_foreground_window(hwnd)
    title = get_window_title(hwnd)
    return f"Maximized {title}." if title else "Window maximized."


def maximize_video(target_app: str | None = None) -> str:
    """Toggle video fullscreen, reserving VLC's `F` hotkey for VLC only."""
    from core.session_memory import session

    target_clean = canonical_app_name(target_app or "")
    fg = get_foreground_window()
    hwnd = None

    if target_clean in {"vlc", "vlc media player", "video lan"}:
        hwnd = find_any_window("vlc")
        if not hwnd:
            return "VLC isn't open."
    elif target_clean:
        hwnd = find_any_window(target_clean)
        if not hwnd and target_clean in {"youtube", "you tube", "yt"}:
            hwnd = find_any_window("youtube")
            if not hwnd:
                for browser in ("chrome", "brave", "edge"):
                    hwnd = find_any_window(browser)
                    if hwnd:
                        break
    else:
        # Keep the action on the foreground/session app; never steal focus for
        # an unrelated VLC or YouTube window just because it happens to exist.
        if fg and user32.IsWindowVisible(fg) and not _is_desktop_or_jarvis_window(fg):
            hwnd = fg
        else:
            current = canonical_app_name(session.current_app or session.last_opened_app or "")
            hwnd = find_any_window(current) if current else None

    if not hwnd:
        return f"{target_clean.title()} isn't open." if target_clean else "No active video window found."
    if not force_foreground_window(hwnd):
        return "I couldn't bring the target window to the foreground."
    time.sleep(0.06)

    if _is_vlc_window(hwnd):
        if not _is_fullscreen_window(hwnd):
            if not _send_vlc_fullscreen_key(hwnd):
                return "I couldn't safely send VLC's fullscreen key."
        session.set_current_app("vlc")
        return "Video is fullscreen in VLC."

    if not _is_fullscreen_window(hwnd):
        _send_key(VK_F11)
    app_label = target_clean or session.current_app or "the active app"
    if "youtube" in app_label or "yt" == app_label:
        session.set_current_app("youtube")
        return "Fullscreen enabled for YouTube."
    return f"Fullscreen enabled for {app_label.title()}."


# ============================================================
# RESTORE CURRENT WINDOW
# ============================================================

def restore_current_window():

    hwnd = get_foreground_window()

    from core.session_memory import session
    target_app = session.current_app or session.last_opened_app

    if not hwnd or not user32.IsWindowVisible(hwnd) or _is_desktop_or_jarvis_window(hwnd):
        hwnd = find_any_window(target_app) if target_app else None
    if not hwnd:
        return "I couldn't identify an app window to restore."

    if hwnd:
        if not force_foreground_window(hwnd):
            return "I couldn't bring the current window to the foreground."
        time.sleep(0.05)
        title = get_window_title(hwnd)
        app_name = title if title else "Window"

        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            return f"Restored {app_name}."

        if user32.IsZoomed(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            return f"Restored {app_name} to normal size."

        if _is_fullscreen_window(hwnd):
            if _is_vlc_window(hwnd):
                _send_vlc_fullscreen_key(hwnd)
            else:
                _send_key(VK_F11)
        user32.ShowWindow(hwnd, SW_RESTORE)
        return f"Restored {app_name} to normal size."

    if target_app:
        return restore_app(target_app)

    return (
        "I couldn't find the current window"
    )


# ============================================================
# CLOSE CURRENT WINDOW
# ============================================================

def close_current_window():

    hwnd = get_foreground_window()

    from core.session_memory import session
    target_app = session.current_app or session.last_opened_app

    if not hwnd or not user32.IsWindowVisible(hwnd) or (target_app and get_window_process_name(hwnd) in {"cmd.exe", "powershell.exe", "conhost.exe"}):
        if target_app:
            return close_app_window(target_app)

    if hwnd:
        title = get_window_title(hwnd)
        user32.PostMessageW(
            hwnd,
            WM_CLOSE,
            0,
            0,
        )
        return f"Closed {title}." if title else "Window closed."

    if target_app:
        return close_app_window(target_app)

    return (
        "I couldn't find the current window"
    )


# ============================================================
# SWITCH TO SPECIFIC APP
# ============================================================

def switch_to_app(name):
    name_clean = canonical_app_name(name)
    hwnd = find_any_window(name_clean)

    from core.session_memory import session

    if not hwnd:
        if _known_website_name(name_clean):
            from tools.system_tools import handle_open_website
            result = handle_open_website(f"open {name_clean} website")
            if result:
                session.set_current_app(name_clean)
                session.last_opened_website = name_clean
                return result

        from tools.system_tools import find_pc_folder, open_path_safe, set_active_folder
        f = find_pc_folder(name_clean)
        if f and os.path.isdir(f):
            if not open_path_safe(f):
                return f"I couldn't open the {os.path.basename(f)} folder."
            set_active_folder(f)
            bname = os.path.basename(f)
            session.set_current_app(bname)
            return f"Switched to {bname}."
        return f"{name_clean.title()} isn't open."

    if not force_foreground_window(hwnd):
        return f"I couldn't bring {name_clean.title()} to the foreground."

    session.set_current_app(name_clean)
    from tools.system_tools import find_pc_folder, set_active_folder
    f = find_pc_folder(name_clean)
    if f and os.path.isdir(f):
        set_active_folder(f)

    return f"Switched to {name_clean.title()}."


def maximize_app(name):

    clean = normalize(name).lower().strip()
    if clean in {
        "", "it", "that", "this", "window", "the window", "this window",
        "current window", "the current window", "that window", "the app",
        "this app", "current", "screen", "the screen", "active window",
        "the active window",
    }:
        return maximize_current_window()

    name_clean = canonical_app_name(name)
    if name_clean == "vlc":
        return maximize_video("vlc")
    hwnd = find_any_window(name_clean)

    if not hwnd:
        return f"{name_clean.title()} isn't open."

    from core.session_memory import session
    session.set_current_app(name_clean)

    if user32.IsZoomed(hwnd):
        force_foreground_window(hwnd)
        return f"{name_clean.title()} is already maximized."

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)

    if not _maximize_and_verify(hwnd):
        return f"I couldn't maximize {name_clean.title()}."
    force_foreground_window(hwnd)
    return f"Maximized {name_clean.title()}."


def minimize_app(name):

    clean = normalize(name).lower().strip()
    if clean in {
        "", "it", "that", "this", "window", "the window", "this window",
        "current window", "the current window", "that window", "the app",
        "this app", "current", "screen", "the screen", "active window",
        "the active window",
    }:
        return minimize_current_window()

    name_clean = canonical_app_name(name)
    hwnd = find_any_window(name_clean)

    if not hwnd:
        return f"{name_clean.title()} isn't open."

    from core.session_memory import session
    session.set_current_app(name_clean)

    if user32.IsIconic(hwnd):
        return f"{name_clean.title()} is already minimized."

    if _is_vlc_window(hwnd) and _is_fullscreen_window(hwnd):
        _send_vlc_fullscreen_key(hwnd)
        time.sleep(0.06)

    if not _minimize_and_verify(hwnd):
        return f"I couldn't minimize {name_clean.title()}."
    return f"Minimized {name_clean.title()}."


def restore_app(name):

    clean = normalize(name).lower().strip()
    if clean in {
        "", "it", "that", "this", "window", "the window", "this window",
        "current window", "the current window", "that window", "the app",
        "this app", "current", "screen", "the screen", "active window",
        "the active window",
    }:
        return restore_current_window()

    name_clean = canonical_app_name(name)
    hwnd = find_any_window(name_clean)

    if not hwnd:
        return f"{name_clean.title()} isn't open."

    from core.session_memory import session
    session.set_current_app(name_clean)

    force_foreground_window(hwnd)
    time.sleep(0.05)

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        force_foreground_window(hwnd)
        return f"Restored {name_clean.title()}."

    if user32.IsZoomed(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        force_foreground_window(hwnd)
        return f"Restored {name_clean.title()} to normal size."

    # Only exit a detected fullscreen state. F is reserved for VLC; browsers
    # use F11, and ordinary windows are restored through the Windows API.
    if _is_fullscreen_window(hwnd):
        if _is_vlc_window(hwnd):
            _send_vlc_fullscreen_key(hwnd)
        else:
            _send_key(VK_F11)
    user32.ShowWindow(hwnd, SW_RESTORE)
    force_foreground_window(hwnd)
    return f"Restored {name_clean.title()} to normal size."


def close_app_window(name):

    clean = normalize(name).lower().strip()
    if clean in {"down", "up", "top", "bottom", "left", "right"}:
        from tools.browser_controller import scroll_page
        _ok, msg = scroll_page(clean)
        return msg

    if clean in {"", "it", "that", "this", "window", "the window", "this window", "current window", "the current window", "that window", "the app", "this app", "current"}:
        return close_current_window()

    name_clean = canonical_app_name(name)
    hwnd = find_any_window(name_clean)

    if not hwnd:
        return close_app(name_clean)

    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    return f"Closed {name_clean.title()}."


# ============================================================
# SWITCH TO NEXT WINDOW
# ============================================================

def switch_window():

    user32.keybd_event(
        0x12,
        0,
        0,
        0,
    )

    user32.keybd_event(
        0x09,
        0,
        0,
        0,
    )

    user32.keybd_event(
        0x09,
        0,
        2,
        0,
    )

    user32.keybd_event(
        0x12,
        0,
        2,
        0,
    )

    return "Switched window"


# ============================================================
# HANDLE WINDOW COMMAND
# ============================================================

def handle_window_command(command):
    try:
        from core.command_engine import normalize_voice_text
        command = normalize_voice_text(command)
    except Exception:
        pass

    command = normalize(
        command
    )

    if not command:
        return None

    # Directional commands following close/quit/exit are scrolling mishearings
    if command.startswith(("close ", "quit ", "exit ")):
        target_dir = command.split(None, 1)[1].strip().lower()
        if target_dir in {"down", "up", "top", "bottom", "left", "right"}:
            from tools.browser_controller import scroll_page
            _ok, msg = scroll_page(target_dir)
            return msg

    # Let the browser-search route handle "go to Chrome and search ...".
    if re.match(
        r"^go to (?:chrome|google chrome|brave|brave browser|edge|microsoft edge)\s+(?:and\s+)?(?:search|google|look up)\s+.+$",
        command,
    ):
        return None

    if command in {
        "go to desktop", "show desktop", "show me desktop",
        "minimize everything", "minimize all windows", "hide all windows",
    }:
        return show_desktop()

    # A spoken "go to ChatGPT" means activate its app/browser window first.
    # Only open Chrome when no existing ChatGPT window can be found.
    if _is_chatgpt_navigation(command):
        hwnd = find_window_by_title("chatgpt")
        if hwnd:
            if not _activate_window(hwnd):
                return "I couldn't bring ChatGPT to the foreground."
            return "Switched to ChatGPT."
        return open_chatgpt_in_chrome()

    # Explorer folder operations precede generic app commands.
    if command == "close this folder":
        hwnd = get_foreground_window()
        if get_window_process_name(hwnd) != "explorer.exe":
            try:
                from tools.system_tools import get_active_folder
                hwnd = find_explorer_window(get_active_folder() or "")
            except Exception:
                hwnd = None
        if hwnd:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            return "Folder window closed."
        return "That folder isn't open."

    target = _folder_target(command)
    folder_command = bool(target) and (
        command.startswith(("switch to ", "switch over to ", "show ", "bring ", "focus ", "activate ", "close "))
        or (command.startswith(("go to ", "goto ")) and not _known_website_name(target))
    )
    # Only "go to <known site>" is a web request; "switch to ChatGPT" is a window request.
    # Known apps must use the process-aware resolver below. The old title-only
    # fallback could select a VLC video-output window and report success even
    # when Windows kept another window in front.
    known_app = canonical_app_name(target) in APP_PROCESSES
    website_navigation = (
        _known_website_name(target)
        and not re.search(r"\b(?:folder|directory)\b", command)
    )
    if (
        folder_command
        and not known_app
        and not website_navigation
        and (not _known_website_name(target) or not command.startswith(("go to ", "goto ")))
    ):
        hwnd = find_explorer_window(target)
        # Browser titles are a fallback only for activate/switch requests.
        # Close-folder commands must never close a non-Explorer window.
        if not hwnd and not command.startswith("close "):
            hwnd = find_window_by_title(target)
        if hwnd:
            if command.startswith("close "):
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                return f"Closed {target}."
            if not _activate_window(hwnd):
                return f"I couldn't bring {target.title()} to the foreground."
            return f"Switched to {target}."
        if not command.startswith(("go to ", "goto ")):
            return f"{target} isn't open."

    # ========================================================
    # CLOSE CURRENT WINDOW
    # ========================================================

    if command in {
        "exit",
        "exit folder",
        "exit this folder",
        "exit current folder",
        "exit file",
        "exit this file",
        "exit current file",
        "close current window",
        "close this window",
        "close the current window",
        "close current",
        "close window",
        "close the window",
        "close that window",
        "close it",
    }:

        return close_current_window()

    # ========================================================
    # MINIMIZE CURRENT WINDOW
    # ========================================================

    if command in {
        "minimize the window",
        "minimize this window",
        "minimize current window",
        "minimize the current window",
        "minimize that window",
        "minimize window",
        "minimize it",
        "minimize",
        "minimize this",
        "minimise the window",
        "minimise this window",
        "minimise current window",
        "minimise the current window",
        "minimise that window",
        "minimise window",
        "minimise it",
        "minimise",
        "minimise this",
        "minimum",
        "minimum window",
        "minimum it",
        "minimized",
        "minimised",
        "minmise",
        "hide window",
        "small window",
    }:

        return minimize_current_window()

    # ========================================================
    # MAXIMIZE VIDEO (F in VLC; F11 outside VLC)
    # ========================================================
    # "maximize this video in vlc" -> uses VLC's F fullscreen hotkey
    # Other video windows use F11; F is never sent outside VLC.
    m = re.match(r"^(?:maximize|maximise|fullscreen|full\s+screen)\s+(?:the\s+|this\s+|current\s+)?video(?:\s+(?:in|on)\s+(.+))?$", command)
    if m:
        target = m.group(1).strip() if m.group(1) else None
        return maximize_video(target)

    m = re.match(r"^(?:make\s+(?:the\s+|this\s+|current\s+)?video\s+(?:fullscreen|full\s+size|full\s+screen))(?:\s+(?:in|on)\s+(.+))?$", command)
    if m:
        target = m.group(1).strip() if m.group(1) else None
        return maximize_video(target)

    m = re.match(r"^(?:maximize|maximise)\s+(.+?)\s+video$", command)
    if m:
        return maximize_video(m.group(1).strip())

    if command in {
        "maximize this video", "maximize the video", "maximize video",
        "maximize current video", "fullscreen video", "full screen video",
        "make video fullscreen", "make the video fullscreen",
    }:
        return maximize_video()

    # ========================================================
    # MAXIMIZE THE WINDOW (VLC uses F; other windows use F11)
    # ========================================================

    if command in {
        "maximize the window",
        "maximize this window",
        "maximize current window",
        "maximize the current window",
        "maximize that window",
        "maximize window",
        "maximize it",
        "maximize",
        "maximize this",
        "maximise the window",
        "maximise this window",
        "maximise current window",
        "maximise the current window",
        "maximise that window",
        "maximise window",
        "maximise it",
        "maximise",
        "maximise this",
        "maximum",
        "maximum window",
        "maximum it",
        "maximum the window",
        "maximized",
        "maximised",
        "maxim",
        "maxima",
        "maksimise",
        "macs mise",
        "make big",
        "make full",
        "full window",
        "fullscreen window",
        "full screen window",
        "fullscreen the window",
        "full screen the window",
        "fullscreen",
        "full screen",
        "maximize screen",
        "maximise screen",
        "maximize the screen",
        "maximise the screen",
    }:

        return maximize_current_window()

    # ========================================================
    # RESTORE CURRENT WINDOW
    # ========================================================

    if command in {
        "restore the window",
        "restore this window",
        "restore current window",
        "restore the current window",
        "restore that window",
        "restore window",
        "restore it",
        "restore",
        "restore this",
        "restor",
        "restored",
        "normal size",
    }:

        return restore_current_window()

    # ========================================================
    # MAXIMIZE (WINDOW / SPECIFIC APP / NATURAL PHRASES)
    # ========================================================

    m = re.match(r"^(?:maximize|maximise|maximum|maximized|maximised|maksimise)\s+(.+)$", command)
    if m:
        target = m.group(1).strip()
        if target in {
            "it", "that", "this", "the window", "this window", "current window",
            "the current window", "that window", "window", "active app", "current app",
            "screen", "the screen", "active window", "the active window",
        }:
            return maximize_current_window()
        return maximize_app(target)

    m = re.match(r"^(?:make\s+(.+?)\s+(?:full\s+size|bigger|fullscreen)|make\s+it\s+(?:full\s+size|bigger|fullscreen))$", command)
    if m:
        target = m.group(1).strip() if m.group(1) else "it"
        if target in {"it", "the window", "this window", "current window", "the current window", "that window", "window"}:
            return maximize_current_window()
        if "video" in target:
            return maximize_video(target)
        return maximize_app(target)

    m = re.match(r"^(?:expand\s+(.+)|expand\s+it)$", command)
    if m:
        target = m.group(1).strip() if m.group(1) else "it"
        if target in {"it", "the window", "this window", "current window", "the current window", "that window", "window"}:
            return maximize_current_window()
        return maximize_app(target)

    # ========================================================
    # RESTORE (WINDOW / SPECIFIC APP / NATURAL PHRASES)
    # ========================================================

    m = re.match(r"^(?:restore|restor|restored)\s+(.+)$", command)
    if m:
        target = m.group(1).strip()
        if target in {
            "it", "that", "this", "window", "the window", "this window",
            "current window", "the current window", "that window", "active app",
            "current app", "screen", "the screen", "active window", "the active window",
        }:
            return restore_current_window()
        return restore_app(target)

    m = re.match(r"^(?:bring\s+(.+?)\s+back|bring\s+it\s+back)$", command)
    if m:
        target = m.group(1).strip() if m.group(1) else "it"
        if target in {"it", "that", "this", "window", "the window", "this window", "current window", "the current window", "that window"}:
            return restore_current_window()
        return restore_app(target)

    m = re.match(r"^(?:return\s+(.+?)\s+to\s+normal|return\s+it\s+to\s+normal)$", command)
    if m:
        target = m.group(1).strip() if m.group(1) else "it"
        if target in {"it", "that", "this", "window", "the window", "this window", "current window", "the current window", "that window"}:
            return restore_current_window()
        return restore_app(target)

    m = re.match(r"^(?:normal\s+size\s+(.+)|normal\s+size)$", command)
    if m:
        target = m.group(1).strip() if m.group(1) else "it"
        if target in {"it", "that", "this", "window", "the window", "this window", "current window", "the current window", "that window"}:
            return restore_current_window()
        return restore_app(target)

    # ========================================================
    # MINIMIZE (WINDOW / SPECIFIC APP / NATURAL PHRASES)
    # ========================================================

    m = re.match(r"^(?:minimize|minimise|minimum|minimized|minimised|minmise)\s+(.+)$", command)
    if m:
        target = m.group(1).strip()
        if target in {
            "it", "that", "this", "window", "the window", "this window",
            "current window", "the current window", "that window", "active app",
            "current app", "screen", "the screen", "active window", "the active window",
        }:
            return minimize_current_window()
        return minimize_app(target)

    m = re.match(r"^(?:hide\s+(.+)|hide\s+it)$", command)
    if m:
        target = m.group(1).strip() if m.group(1) else "it"
        if target in {"it", "that", "this", "window", "the window", "this window", "current window", "the current window", "that window"}:
            return minimize_current_window()
        return minimize_app(target)

    # ========================================================
    # SWITCH WINDOW
    # ========================================================

    if command in {
        "switch window",
        "switch windows",
        "switch to another window",
        "next window",
        "switch window please",
    }:

        return switch_window()

    # ========================================================
    # SWITCH / FOCUS / BRING FORWARD
    # ========================================================

    m = re.match(
        r"^(?:switch\s+to|switch\s+over\s+to|switch\s+back\s+to|focus\s+on|focus|activate|bring\s+forward|bring\s+to\s+front|show)\s+(.+)$",
        command,
    )
    if m:
        name = m.group(1).strip().removesuffix(" forward").removesuffix(" to front").strip()
        if command.startswith("go to ") and _known_website_name(name):
            return None
        return switch_to_app(name)

    # ========================================================
    # OPEN APP
    # ========================================================

    match = re.match(
        r"^(?:open|launch|start)\s+(.+)$",
        command,
    )

    if match:
        name = match.group(1).strip()
        if name.endswith((" website", " site", " folder", " directory")):
            return None
        from tools.system_tools import find_pc_folder, find_file_in_hierarchy, get_active_folder
        if find_pc_folder(name):
            return None
        active = get_active_folder()
        if active and find_file_in_hierarchy(name, active):
            return None
        if name.isdigit():
            return None
        return open_app(name)

    # ========================================================
    # CLOSE APP / WINDOW
    # ========================================================

    match = re.match(
        r"^(?:close|quit|exit)\s+(.+)$",
        command,
    )

    if match:
        name = match.group(1).strip()
        if name.lower() in {"down", "up", "top", "bottom", "left", "right"}:
            from tools.browser_controller import scroll_page
            _ok, msg = scroll_page(name.lower())
            return msg
        if name in {"it", "that", "this", "window", "the window", "this window", "current window", "the current window", "that window"}:
            return close_current_window()
        return close_app_window(name)

    return None


# ============================================================
# TEST
# ============================================================

if __name__ == "__main__":

    print("=" * 60)
    print(
        "       JARVIS WINDOW TOOLS TEST"
    )
    print("=" * 60)

    tests = [
        "switch to vlc",
        "switch to vlc media player",
        "maximize vlc",
        "minimize vlc",
        "restore vlc",
        "close vlc",
        "switch to chrome",
        "switch to notepad",
        "maximize notepad",
        "minimize notepad",
    ]

    for command in tests:

        print()
        print(
            f"Command: {command}"
        )

        result = handle_window_command(
            command
        )

        print(
            f"Result: {result}"
        )
