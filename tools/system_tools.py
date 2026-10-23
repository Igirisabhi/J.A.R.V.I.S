import os
import sys

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

import re
import difflib
import subprocess
import webbrowser
import shutil
import ctypes
import urllib.parse
import urllib.request
import sqlite3
import threading
import time
import ast
import operator
import shlex
from pathlib import Path
from datetime import datetime
from typing import Any, Optional
from tools.window_tools import handle_window_command
from tools.file_tools import KNOWN_FOLDERS, handle_file_system_command
from tools.app_actions.whatsapp import handle_whatsapp_command
from core.session_memory import session
from core.settings import get_settings, save_settings
from core.events import event_bus


_ARITHMETIC_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_calculate(expression: str):
    if len(expression) > 200:
        raise ValueError("Expression is too long.")
    tree = ast.parse(expression, mode="eval")

    def evaluate(node):
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            if abs(node.value) > 10**12:
                raise ValueError("Number is too large.")
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _ARITHMETIC_OPERATORS:
            left = evaluate(node.left)
            right = evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 10:
                raise ValueError("Exponent is too large.")
            value = _ARITHMETIC_OPERATORS[type(node.op)](left, right)
            if abs(value) > 10**12:
                raise ValueError("Result is too large.")
            return value
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ARITHMETIC_OPERATORS:
            return _ARITHMETIC_OPERATORS[type(node.op)](evaluate(node.operand))
        raise ValueError("Only basic arithmetic is supported.")

    return evaluate(tree)


class WorkspaceError(ValueError):
    """Raised when a file path is absent from or escapes the configured workspace."""


UNSAFE_PATH_MESSAGE = (
    "I can't operate on system-protected paths like the entire C: drive. "
    "Please specify a folder inside your user directories."
)


def _safe_user_roots(extra_workspace: str | None = None) -> list[Path]:
    """Return canonical user folders and the configured Jarvis workspace."""
    roots = [
        KNOWN_FOLDERS[name]
        for name in ("desktop", "documents", "downloads", "pictures", "videos", "music")
    ]
    roots.append(Path(__file__).resolve().parent.parent)
    workspace = extra_workspace or get_settings().get("workspace_dir")
    if workspace:
        roots.append(Path(os.path.expandvars(os.path.expanduser(workspace))))
    canonical = []
    for root in roots:
        try:
            resolved = root.resolve(strict=False)
            if resolved not in canonical:
                canonical.append(resolved)
        except (OSError, RuntimeError, ValueError):
            continue
    return canonical


def is_safe_path(path: str, operation: str = "read", *, allow_windows_navigation: bool = False, allow_workspace: bool = False) -> bool:
    """Validate a canonical path against user roots and system-protected paths.

    Read-only access to descendants of C:\\Windows is allowed for explicit
    listing/search requests. The Windows directory itself can only be opened
    in Explorer through the dedicated navigation exception.
    """
    try:
        raw = os.path.expandvars(os.path.expanduser(str(path or "").strip().strip('"')))
        if not raw:
            return False
        candidate = Path(raw).resolve(strict=False)
        text = os.path.normcase(os.path.normpath(str(candidate))).rstrip("\\/")
        drive, tail = os.path.splitdrive(str(candidate))
        if drive and not tail.strip("\\/"):
            return False
        blacklist = {os.path.normcase(os.path.normpath(item)).rstrip("\\/") for item in _SCAN_BLACKLIST}
        if text in blacklist:
            return bool(allow_windows_navigation and text == os.path.normcase(r"c:\windows"))
        for protected in blacklist:
            try:
                protected_path = Path(protected)
                candidate.relative_to(protected_path)
                is_windows_descendant = protected == os.path.normcase(r"c:\windows") and candidate != protected_path
                if not is_windows_descendant or operation.casefold() not in {"read", "list", "search", "open_readonly"}:
                    return False
                return True
            except ValueError:
                try:
                    protected_path.relative_to(candidate)
                    return False
                except ValueError:
                    pass
        for root in _safe_user_roots(str(candidate) if allow_workspace else None):
            try:
                candidate.relative_to(root)
                return True
            except ValueError:
                continue
        return False
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def is_desktop_path(path: Any) -> bool:
    """Return True if path is the user's Desktop directory."""
    if not path:
        return False
    try:
        raw = os.path.normcase(os.path.normpath(str(path))).rstrip("\\/")
        desktop_candidates = set()
        desk_kf = KNOWN_FOLDERS.get("desktop")
        if desk_kf:
            desktop_candidates.add(os.path.normcase(os.path.normpath(str(desk_kf))).rstrip("\\/"))
        user_prof = os.environ.get("USERPROFILE")
        if user_prof:
            desktop_candidates.add(os.path.normcase(os.path.normpath(os.path.join(user_prof, "Desktop"))).rstrip("\\/"))
        public_prof = os.environ.get("PUBLIC")
        if public_prof:
            desktop_candidates.add(os.path.normcase(os.path.normpath(os.path.join(public_prof, "Desktop"))).rstrip("\\/"))
        return raw in desktop_candidates
    except Exception:
        return False


def is_desktop_active() -> bool:
    """Return True if the user is currently on the Desktop (foreground window or active Explorer)."""
    try:
        import win32gui
        hwnd = win32gui.GetForegroundWindow()
        if hwnd:
            cls = win32gui.GetClassName(hwnd).lower()
            if cls in ("progman", "workerw"):
                return True
    except Exception:
        pass
    active_exp = get_open_explorer_folder()
    if active_exp and is_desktop_path(active_exp):
        return True
    active_folder = get_active_folder()
    if active_folder and is_desktop_path(active_folder) and not active_exp:
        return True
    return False


def resolve_in_workspace(name, operation="read"):
    """Resolve a user path and prove its final target remains within the workspace."""
    workspace = get_settings().get("workspace_dir")
    if not workspace:
        raise WorkspaceError("Set a workspace first with 'set workspace to <path>'.")
    root = Path(os.path.expandvars(os.path.expanduser(workspace))).resolve(strict=False)
    raw = os.path.expandvars(os.path.expanduser(str(name or "").strip().strip('"')))
    if not raw:
        raise WorkspaceError("I need a file or folder name.")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    final = candidate.resolve(strict=False)
    try:
        final.relative_to(root)
    except ValueError as error:
        raise WorkspaceError(f"I can only use files inside the workspace ({root}).") from error
    if not is_safe_path(str(root), operation) or not is_safe_path(str(final), operation):
        raise WorkspaceError(UNSAFE_PATH_MESSAGE)
    return final


def find_workspace_matches(name, limit=5):
    """Return close filename matches from the configured workspace only."""
    root = resolve_in_workspace(".")
    query = Path(str(name or "")).name.lower()
    if not query:
        return []
    candidates = []
    for current, dirs, files in os.walk(root):
        if not _prune_unsafe_walk_dirs(current, dirs, "search"):
            continue
        for filename in files:
            item = Path(current) / filename
            if not is_safe_path(str(item), "search"):
                continue
            try:
                safe = resolve_in_workspace(str(item.relative_to(root)))
            except WorkspaceError:
                continue
            score = difflib.SequenceMatcher(None, query, item.name.lower()).ratio()
            if score >= 0.72:
                candidates.append((score, safe))
    candidates.sort(key=lambda result: (-result[0], str(result[1]).lower()))
    return [path for _score, path in candidates[:limit]]


def _set_workspace(path):
    expanded = Path(os.path.expandvars(os.path.expanduser(path.strip().strip('"')))).resolve(strict=False)
    if not is_safe_path(str(expanded), "write", allow_workspace=True):
        return UNSAFE_PATH_MESSAGE
    if not expanded.is_dir():
        return f"I couldn't find that workspace folder: {expanded}"
    save_settings({"workspace_dir": str(expanded)})
    event_bus.publish("workspace_changed", path=str(expanded))
    return f"Workspace set to {expanded}."


def _workspace_command(command):
    """Handle commands that touch files before legacy global-search handlers."""
    text = (command or "").strip()
    lower = text.lower()
    if not text:
        return None
    if re.match(r"^(?:lock|protect)\s+(?:the\s+)?windows(?:\s+folder)?$", text, re.I):
        return "I can't lock the system Windows folder. Would you like me to lock a specific folder in your Documents instead?"
    if re.match(r"^(?:scan|index|search|lock|protect|delete|remove|modify|change|copy|move|rename|create)\b", text, re.I) and re.search(r"(?:\bc\s*drive\b|\bc:(?:\\|\s|$)|\bwindows\s+folder\b|\bprogram files\b|\bsystem volume information\b)", text, re.I):
        return UNSAFE_PATH_MESSAGE
    if re.match(r"^(?:open|go to|goto|navigate to)\s+(?:the\s+)?windows(?:\s+folder)?$", text, re.I):
        windows_path = os.environ.get("WINDIR", r"C:\Windows")
        return "Opened the Windows folder in Explorer." if open_path_safe(windows_path) else UNSAFE_PATH_MESSAGE
    known_folder = re.fullmatch(r"(?:open|go to|goto|navigate to)\s+(?:(?:my|the)\s+)?(desktop|documents|downloads?|pictures|videos|music)(?:\s+(?:folder|directory))?", text, re.I)
    if known_folder:
        folder_name = known_folder.group(1).lower()
        folder_name = {"download": "downloads"}.get(folder_name, folder_name)
        folder_path = KNOWN_FOLDERS.get(folder_name)
        if folder_path is None:
            return f"I couldn't find the {folder_name.title()} folder."
        if is_safe_path(str(folder_path), "open_readonly") and folder_path.is_dir():
            return "Opened " + folder_name.title() + " folder." if open_path_safe(str(folder_path)) else "I couldn't open that folder."
        return f"I couldn't find the {folder_name.title()} folder."
    direct_open = re.match(r"^(?:open|go to|goto|navigate to)\s+(.+)$", text, re.I)
    if direct_open:
        requested = direct_open.group(1).strip().strip('"')
        windows_path = os.path.normcase(os.path.normpath(os.environ.get("WINDIR", r"C:\Windows"))).rstrip("\\/")
        requested_path = os.path.normcase(os.path.normpath(os.path.expandvars(os.path.expanduser(requested)))).rstrip("\\/")
        if requested_path == windows_path:
            return "Opened the Windows folder in Explorer." if open_path_safe(requested) else UNSAFE_PATH_MESSAGE
    if re.match(r"^(?:open|go to|goto|navigate to)\b", text, re.I) and re.search(r"(?:\bc\s*drive\b|\bc:(?:\\|\s|$)|\bprogram files\b|\bsystem volume information\b)", text, re.I):
        return UNSAFE_PATH_MESSAGE
    match = re.match(r"^(?:set workspace to|change workspace to|use workspace)\s+(.+)$", text, re.I)
    if match:
        return _set_workspace(match.group(1))
    if lower in {"use this folder as workspace", "set this folder as workspace"}:
        return _set_workspace(get_active_folder() or str(Path.cwd()))

    if re.match(r"^(?:run|start)\s+macro\s+[a-z0-9_-]+$", text, re.I):
        from core.command_engine import parse_action, action_to_dsl, execute_dsl
        action = parse_action(text)
        if action:
            _success, result = execute_dsl(action_to_dsl(action), {"current_file": None, "current_app": None}, action)
            return result

    settings = get_settings()
    workspace = settings.get("workspace_dir")
    file_intent = bool(re.match(
        r"^(?:create|make|new)\s+(?:a\s+|the\s+)?file\b|^(?:read|delete|remove|rename|list|show files|search files|search folders|find file|find folder)\b|^(?:open|launch|start|go to|goto)\s+.+(?:\.[A-Za-z0-9]{1,8}|\s+(?:file|folder|directory)\b)|\b(?:in|inside|within|under)\s+(?:workspace|this folder|desktop|downloads|documents|[A-Za-z]:\\)",
        text, re.I,
    ) or bool(re.search(r"\b(?:file|folder|directory|workspace)\b", text, re.I) and re.search(r"\b(?:then|and then|after that|next|also|;|,)\b", text, re.I)))
    open_match = re.match(r"^(?:open|launch|start|go to|goto)\s+(.+)$", text, re.I)
    if open_match and not file_intent:
        target = re.sub(r"\s+(?:folder|directory)$", "", open_match.group(1), flags=re.I)
        try:
            from core.command_engine import _destination_kind
            file_intent = _destination_kind(target)[0] not in {"app", "browser", "website"}
        except (ImportError, AttributeError):
            file_intent = bool(re.search(r"[\\/]|\.[A-Za-z0-9]{1,8}$", target))
    try:
        from core.command_engine import parse_command_chain
        chained = parse_command_chain(text)
        file_intent = file_intent or any(action.verb in {"create_file", "open_file_in", "read_file", "delete", "rename", "search_files", "search_folders", "list_files"} for action in chained)
    except ImportError:
        pass
    if not file_intent:
        return None
    if not workspace:
        try:
            from core.command_engine import parse_action
            action = parse_action(text)
            if action and action.verb in {"open", "open_file_in"}:
                # Opening is read-only and the executor requires the target to
                # be inside the folder currently open in Explorer.
                return None
        except ImportError:
            pass
        return "Set a workspace first with 'set workspace to <path>'."

    # Explicit qualifier policy: only workspace-relative folders are accepted.
    qualifier = re.search(r"\s+(?:in|on|from|inside|within|under)\s+(.+)$", text, re.I)
    if qualifier and lower.startswith(("open ", "go to ", "goto ")):
        parent = qualifier.group(1).strip().strip('"')
        if parent.lower() in {"desktop", "downloads", "documents"}:
            root = get_settings().get("workspace_dir")
            return f"I can only open files inside the workspace ({root}). Say 'open notes.txt' to open it from here."
        if parent.lower() not in {"workspace", "this folder"}:
            try:
                parent_path = resolve_in_workspace(parent)
                if not parent_path.is_dir():
                    raise WorkspaceError("folder is not a workspace subfolder")
            except WorkspaceError:
                root = get_settings().get("workspace_dir")
                return f"I can only open files inside the workspace ({root}). Say 'open notes.txt' to open it from here."

    try:
        from core.command_engine import parse_action, parse_command_chain, action_to_dsl, execute_dsl
        actions = parse_command_chain(text)
        action = actions[0] if len(actions) == 1 else (parse_action(text) if not actions else None)
        if actions:
            from core.command_engine import execute_command_chain
            return execute_command_chain(text)
        if action and action.verb in {"open", "open_file_in", "create_file", "search", "read_file", "rename", "delete", "list_files", "search_files", "search_folders"}:
            if action.verb == "open":
                action.args["workspace"] = True
            action_dsl = action_to_dsl(action)
            event_bus.publish("parsed", actions=[action_dsl], raw=text)
            context = {"current_file": None, "current_app": None, "website": None}
            success, result = execute_dsl(action_to_dsl(action), context, action)
            event_bus.publish("step_result", index=0, action=action_dsl, success=success, message=result)
            return result
    except WorkspaceError as error:
        return str(error)
    except (OSError, ValueError) as error:
        return f"I couldn't complete that workspace action: {error}."

    # Legacy folder/file search commands are blocked here to prevent global-index escapes.
    if re.match(r"^(?:open|go to|goto)\s+.+(?:\s+folder|\s+directory)$", text, re.I) or re.match(r"^(?:search|find)\s+(?:files|file)\b", text, re.I):
        return "Use a path inside the workspace for file and folder commands."
    return None




# ============================================================
# SAFE PRINT (emoji-safe on CP1252 / Windows terminals)
# ============================================================

def _jprint(*args, **kwargs):
    """
    Print helper that gracefully degrades emoji on Windows CP1252
    terminals instead of crashing with UnicodeEncodeError.
    """
    import sys
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        msg = " ".join(str(a) for a in args)
        safe = msg.encode(enc, errors="replace").decode(enc)
        print(safe, **{k: v for k, v in kwargs.items() if k != "end"})

# ============================================================
# PERSISTENT FILE INDEX CONFIGURATION
# ============================================================

# Optional manual fallback roots (mapped drives are auto-detected).
# Add UNC paths here only if they are NOT already mapped as drive letters.
NETWORK_ROOTS: list[str] = [
    # r"\\\\192.168.1.2\\Shared",
]

# Local user folders to index (safe, user-owned locations only).
_LOCAL_INDEX_ROOTS = [
    KNOWN_FOLDERS[name]
    for name in ("desktop", "documents", "downloads", "pictures", "videos", "music")
]

# Paths that must never be recursively scanned.
_SCAN_BLACKLIST = {
    "c:\\windows",
    "c:\\program files",
    "c:\\program files (x86)",
    "c:\\$recycle.bin",
    "c:\\system volume information",
}

# Skip generated dependency and cache trees during indexing and hierarchy
# searches. This also prevents package-internal folders (such as a yt-dlp
# folder named "youtube") from being treated as user folders.
_SEARCH_SKIP_DIR_NAMES = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "virtualenv",
    "node_modules", "__pycache__", ".cache", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox",
}


def _is_ignored_search_path(path: str | os.PathLike) -> bool:
    try:
        return any(part.casefold() in _SEARCH_SKIP_DIR_NAMES for part in Path(path).parts)
    except (OSError, TypeError, ValueError):
        return False


def _prune_search_dirs(dirs: list[str]) -> None:
    dirs[:] = [name for name in dirs if name.casefold() not in _SEARCH_SKIP_DIR_NAMES]


def _prune_unsafe_walk_dirs(current: str, dirs: list[str], operation: str = "search") -> bool:
    """Reject a walk before listing a directory and prune unsafe children."""
    if not is_safe_path(current, operation):
        dirs.clear()
        return False
    dirs[:] = [name for name in dirs if is_safe_path(os.path.join(current, name), operation)]
    return True

# SQLite database path (created automatically).
_INDEX_DB_PATH = Path(__file__).parent.parent / "runtime" / "jarvis_file_index.db"

# Background refresh interval in seconds.
_INDEX_REFRESH_INTERVAL = 1800  # 30 minutes


# ============================================================
# NETWORK DRIVE DETECTION
# ============================================================

def _detect_network_drives():
    """
    Dynamically detect all Windows mapped/network drives using
    the standard library only.  Returns a list of drive root
    strings such as ['X:\\', 'Y:\\', 'Z:\\']
    """
    network_drives = []
    try:
        result = subprocess.run(
            ["net", "use"],
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                # Lines look like:  OK  X:  \\server\share  ...
                match = re.search(
                    r"\b([A-Za-z]):",
                    line,
                )
                if match:
                    letter = match.group(1).upper()
                    drive = f"{letter}:\\\\"
                    path = Path(drive)
                    try:
                        if path.exists():
                            network_drives.append(drive)
                    except (OSError, PermissionError):
                        pass
    except Exception:
        pass

    # Fallback: also check GetLogicalDriveStrings via ctypes
    try:
        import ctypes as _ctypes
        buf = _ctypes.create_unicode_buffer(256)
        _ctypes.windll.kernel32.GetLogicalDriveStringsW(255, buf)
        all_drives = buf.value.split("\x00")
        DRIVE_REMOTE = 4
        for d in all_drives:
            if not d:
                continue
            drive_type = _ctypes.windll.kernel32.GetDriveTypeW(d)

            if drive_type == DRIVE_REMOTE:
                root = d if d.endswith("\\") else d + "\\"
                if root not in network_drives:
                    try:
                        if Path(root).exists():
                            network_drives.append(root)
                    except (OSError, PermissionError):
                        pass
    except Exception:
        pass

    return list(dict.fromkeys(network_drives))  # deduplicate, preserve order


# ============================================================
# SAFE PATH OPEN (no resolve() for network paths)
# ============================================================

def open_path_safe(path_str):
    """
    Open a path using os.startfile() without calling Path.resolve(),
    which can corrupt network / UNC paths.
    """
    path_str = str(path_str)
    try:
        if not is_safe_path(path_str, "open_readonly", allow_windows_navigation=True):
            _jprint(UNSAFE_PATH_MESSAGE)
            return False
        if os.path.exists(path_str):
            if os.path.isfile(path_str) and _is_desktop_non_app_file(path_str):
                _jprint(f"[INDEX] Desktop file access is limited to app shortcuts: {path_str}")
                return False
            _jprint(f"[INDEX] Opening -> {path_str}")
            os.startfile(path_str)
            if os.path.isdir(path_str) and not os.path.normcase(os.path.normpath(path_str)).rstrip("\\/") == os.path.normcase(r"c:\windows"):
                set_active_folder(path_str)
            else:
                session.last_opened_file = path_str
            return True
        else:
            _jprint(f"[INDEX] Path not accessible: {path_str}")
            return False
    except Exception as e:
        _jprint(f"[INDEX] Could not open path: {e}")
        return False


def _is_desktop_non_app_file(path_str):
    """Desktop commands may open folders and app shortcuts, but not loose files directly on the desktop."""
    try:
        path = Path(os.path.abspath(path_str))
        if not path.is_file():
            return False
        # Only files located directly on the Desktop root are loose desktop files.
        # Files inside subfolders (e.g. Desktop\MyFolder\table.py) belong to that folder.
        if not is_desktop_path(path.parent):
            return False
        return path.suffix.lower() not in {".lnk", ".url", ".exe", ".appref-ms"}
    except (OSError, ValueError):
        return False


# ============================================================
# PERSISTENT FILE INDEX (SQLite + background thread)
# ============================================================

class FileIndex:
    """
    Persistent, searchable index of local and network files/folders.
    Uses SQLite (stdlib) with a background indexing thread so that
    voice commands are never blocked.
    """

    def __init__(self, db_path: Path):
        self._db_path = db_path
        if not is_safe_path(str(self._db_path), "write"):
            raise WorkspaceError(UNSAFE_PATH_MESSAGE)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._indexing = False
        self._last_updated: str | None = None
        self._init_db()
        self._print_status()
        self._start_background_refresh()

    # ----------------------------------------------------------
    # DB helpers (each caller gets its own connection)
    # ----------------------------------------------------------

    def _get_conn(self):
        """Return a new thread-local SQLite connection."""
        if not is_safe_path(str(self._db_path), "write"):
            raise WorkspaceError(UNSAFE_PATH_MESSAGE)
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=30,
            check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        """Create the schema if it does not exist."""
        conn = self._get_conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS file_index (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    full_path     TEXT    NOT NULL UNIQUE,
                    name          TEXT    NOT NULL,
                    name_lower    TEXT    NOT NULL,
                    is_directory  INTEGER NOT NULL DEFAULT 0,
                    extension     TEXT    NOT NULL DEFAULT '',
                    modified_time REAL    NOT NULL DEFAULT 0,
                    size_bytes    INTEGER NOT NULL DEFAULT 0,
                    drive_root    TEXT    NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_name_lower
                    ON file_index(name_lower);
                CREATE INDEX IF NOT EXISTS idx_drive_root
                    ON file_index(drive_root);
            """)
            conn.commit()
        finally:
            conn.close()

    # ----------------------------------------------------------
    # Status
    # ----------------------------------------------------------

    def _print_status(self):
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM file_index"
            ).fetchone()
            count = row[0] if row else 0
        finally:
            conn.close()
        _jprint(f"[INDEX] File index loaded: {count:,} items")

    def get_stats(self):
        conn = self._get_conn()
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM file_index"
            ).fetchone()[0]
            dirs = conn.execute(
                "SELECT COUNT(*) FROM file_index WHERE is_directory=1"
            ).fetchone()[0]
            files = total - dirs
        finally:
            conn.close()
        net_drives = _detect_network_drives()
        return {
            "total": total,
            "files": files,
            "directories": dirs,
            "network_drives": net_drives,
            "last_updated": self._last_updated,
            "indexing": self._indexing,
        }

    # ----------------------------------------------------------
    # Background indexing
    # ----------------------------------------------------------

    def _start_background_refresh(self):
        t = threading.Thread(
            target=self._refresh_loop,
            daemon=True,
            name="JarvisFileIndexer",
        )
        t.start()

    def _refresh_loop(self):
        """Background thread: index immediately, then periodically."""
        self._run_full_index()
        while True:
            time.sleep(_INDEX_REFRESH_INTERVAL)
            self._run_full_index()

    def _run_full_index(self):
        with self._lock:
            if self._indexing:
                return
            self._indexing = True
        try:
            self._do_index()
        finally:
            with self._lock:
                self._indexing = False

    def _do_index(self):
        _jprint("[INDEX] Updating file index...")

        # ---- Local roots ----------------------------------------
        local_roots = []
        for p in _LOCAL_INDEX_ROOTS:
            try:
                if p.exists() and is_safe_path(str(p), "index"):
                    local_roots.append(str(p))
            except Exception:
                pass

        # Indexing is limited to the six approved user folders.
        available_net: list[str] = []
        all_roots = local_roots

        # ---- Walk and upsert -----------------------------------
        conn = self._get_conn()
        try:
            total_added = 0
            seen_paths: set[str] = set()

            for root_str in all_roots:
                is_network = root_str.upper() in [
                    d.upper() for d in available_net
                ]
                if is_network:
                    _jprint(f"[INDEX] Indexing {root_str}")
                count = self._walk_and_upsert(
                    conn, root_str, seen_paths, is_network
                )
                total_added += count

            # ---- Remove stale entries --------------------------
            self._remove_stale(conn, seen_paths)

            conn.commit()
            self._last_updated = datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            row = conn.execute(
                "SELECT COUNT(*) FROM file_index"
            ).fetchone()
            _jprint(
                f"[INDEX] File index updated: "
                f"{row[0]:,} items"
            )
        except Exception as e:
            _jprint(f"[INDEX] ERROR: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()

    def _walk_and_upsert(
        self,
        conn: sqlite3.Connection,
        root_str: str,
        seen_paths: set,
        is_network: bool,
    ) -> int:
        """
        Recursively walk root_str using os.scandir() (fast).
        Upserts each file/folder into the DB.
        Returns count of items processed.
        """
        count = 0
        # Determine drive root label
        drive_root = os.path.splitdrive(root_str)[0].upper()
        if not drive_root:
            drive_root = root_str.split("\\")[2] if root_str.startswith("\\\\") else root_str

        stack = [root_str]
        visited_inodes = set()  # guard against symlink loops

        while stack:
            current = stack.pop()

            # Validate each directory before opening it, before discovering entries.
            if not is_safe_path(current, "index"):
                continue

            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            full_path = entry.path
                            is_dir = entry.is_dir(follow_symlinks=False)
                            if not is_safe_path(full_path, "index"):
                                continue

                            # Skip trash/system trees on every drive, including
                            # mapped network drives where the full path differs.
                            if entry.name.lower() in {
                                "$recycle.bin", "system volume information",
                                "windows", "program files", "program files (x86)",
                            } or (is_dir and entry.name.casefold() in _SEARCH_SKIP_DIR_NAMES):
                                continue

                            # Guard against infinite loops via junctions/symlinks
                            if is_dir:
                                if is_desktop_path(root_str) or is_desktop_path(current):
                                    # Desktop barrier: Never recursively index files inside subfolders on Desktop
                                    pass
                                else:
                                    try:
                                        stat = entry.stat(follow_symlinks=False)
                                        inode_key = (stat.st_dev, stat.st_ino)
                                        if inode_key in visited_inodes and inode_key != (0, 0):
                                            continue
                                        visited_inodes.add(inode_key)
                                    except Exception:
                                        pass
                                    stack.append(full_path)

                            # Gather metadata
                            name = entry.name
                            ext = (
                                os.path.splitext(name)[1].lower()
                                if not is_dir
                                else ""
                            )
                            try:
                                st = entry.stat(follow_symlinks=False)
                                mtime = st.st_mtime
                                size = st.st_size if not is_dir else 0
                            except Exception:
                                mtime = 0.0
                                size = 0

                            seen_paths.add(full_path)

                            # Upsert
                            conn.execute(
                                """
                                INSERT INTO file_index
                                    (full_path, name, name_lower,
                                     is_directory, extension,
                                     modified_time, size_bytes, drive_root)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT(full_path) DO UPDATE SET
                                    name=excluded.name,
                                    name_lower=excluded.name_lower,
                                    is_directory=excluded.is_directory,
                                    extension=excluded.extension,
                                    modified_time=excluded.modified_time,
                                    size_bytes=excluded.size_bytes,
                                    drive_root=excluded.drive_root
                                WHERE file_index.name IS NOT excluded.name
                                   OR file_index.name_lower IS NOT excluded.name_lower
                                   OR file_index.is_directory IS NOT excluded.is_directory
                                   OR file_index.extension IS NOT excluded.extension
                                   OR file_index.modified_time IS NOT excluded.modified_time
                                   OR file_index.size_bytes IS NOT excluded.size_bytes
                                   OR file_index.drive_root IS NOT excluded.drive_root
                                """,
                                (
                                    full_path,
                                    name,
                                    name.lower(),
                                    1 if is_dir else 0,
                                    ext,
                                    mtime,
                                    size,
                                    drive_root,
                                ),
                            )
                            count += 1

                            # Batch commit every 5000 rows
                            if count % 5000 == 0:
                                conn.commit()

                        except (PermissionError, FileNotFoundError, OSError):
                            continue
                        except Exception:
                            continue

            except (PermissionError, FileNotFoundError, OSError):
                continue
            except Exception:
                continue

        return count

    def _remove_stale(self, conn: sqlite3.Connection, seen_paths: set):
        """
        Remove DB entries whose paths no longer exist on disk.
        Only operate on paths that were in the scan scope.
        """
        try:
            rows = conn.execute(
                "SELECT full_path FROM file_index"
            ).fetchall()
            stale = [
                row[0] for row in rows
                if row[0] not in seen_paths
                and (not is_safe_path(row[0], "index") or _is_ignored_search_path(row[0]) or not os.path.exists(row[0]))
            ]
            if stale:
                conn.executemany(
                    "DELETE FROM file_index WHERE full_path = ?",
                    [(p,) for p in stale],
                )
                _jprint(f"[INDEX] Removed {len(stale):,} stale index entries")
        except Exception:
            pass

    # ----------------------------------------------------------
    # Search
    # ----------------------------------------------------------

    def search(
        self,
        query: str,
        prefer_dirs: bool = False,
        limit: int = 20,
    ) -> list:
        """
        Search the index for files/folders matching query.
        Priority:
          1. Exact name match
          2. Starts-with match
          3. Substring match
          4. Fuzzy (any-word) match
        Returns a list of full_path strings, ranked.
        """
        if not query:
            return []
        q = query.strip().lower()
        conn = self._get_conn()
        try:
            results = []

            # 1. Exact name
            rows = conn.execute(
                "SELECT full_path, is_directory FROM file_index "
                "WHERE name_lower = ? LIMIT ?",
                (q, limit),
            ).fetchall()
            results.extend(rows)

            # 2. Starts-with
            if len(results) < limit:
                rows2 = conn.execute(
                    "SELECT full_path, is_directory FROM file_index "
                    "WHERE name_lower LIKE ? AND name_lower != ? LIMIT ?",
                    (q + "%", q, limit - len(results)),
                ).fetchall()
                seen = {r[0] for r in results}
                results.extend(r for r in rows2 if r[0] not in seen)

            # 3. Substring
            if len(results) < limit:
                rows3 = conn.execute(
                    "SELECT full_path, is_directory FROM file_index "
                    "WHERE name_lower LIKE ? AND name_lower NOT LIKE ? LIMIT ?",
                    ("%" + q + "%", q + "%", limit - len(results)),
                ).fetchall()
                seen = {r[0] for r in results}
                results.extend(r for r in rows3 if r[0] not in seen)

            # 4. Fuzzy: each word in query must appear in name
            if len(results) < limit:
                words = q.split()
                if len(words) > 1:
                    like_clauses = " AND ".join(
                        ["name_lower LIKE ?" for _ in words]
                    )
                    params: list[str | int] = ["%" + w + "%" for w in words]
                    params.append(limit - len(results))
                    rows4 = conn.execute(
                        f"SELECT full_path, is_directory FROM file_index "
                        f"WHERE {like_clauses} LIMIT ?",
                        params,
                    ).fetchall()
                    seen = {r[0] for r in results}
                    results.extend(r for r in rows4 if r[0] not in seen)

        finally:
            conn.close()

        # Older index databases can still contain generated trees. Filter those
        # rows until a background refresh removes them from the index.
        results = [row for row in results if is_safe_path(row[0], "search") and not _is_ignored_search_path(row[0])]

        # Re-rank: prefer directories when asked, then local over network
        home_str = str(Path.home()).lower()

        def rank_key(row):
            fp, is_dir = row
            fp_lower = fp.lower()
            is_local = fp_lower.startswith(home_str[:3])  # same drive letter
            exact = fp_lower.split("\\")[-1] == q
            return (
                0 if exact else 1,
                0 if (prefer_dirs and is_dir) else (0 if is_dir else 1),
                0 if is_local else 1,
            )

        results.sort(key=rank_key)
        return [row[0] for row in results]


# ============================================================
# MODULE-LEVEL INDEX SINGLETON
# Loads existing DB immediately; background thread updates it.
# ============================================================

_file_index: FileIndex | None = None

try:
    _file_index = FileIndex(_INDEX_DB_PATH)
except Exception as _idx_err:
    _jprint(f"[INDEX] WARNING - Could not start file index: {_idx_err}")
    _file_index = None


# ============================================================
# ACTIVE FOLDER CONTEXT
# Tracks the most-recently-opened DIRECTORY during this session.
# ============================================================

# Session-scoped active folder (None = no context set)
_ACTIVE_FOLDER: str | None = None

# Video / audio extensions that qualify for "play" commands
_VIDEO_EXT = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v",
    ".flv", ".ts", ".mpg", ".mpeg", ".3gp",
}
_AUDIO_EXT = {
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".wma", ".opus",
}
_MEDIA_EXT = _VIDEO_EXT | _AUDIO_EXT


def _get_active_explorer_path(foreground_only: bool = False) -> str | None:
    """
    Detect the directory currently open or focused in Windows Explorer.
    Uses Shell.Application COM interface safely with pythoncom.
    """
    try:
        import pythoncom
        import win32gui
        import win32com.client

        pythoncom.CoInitialize()
        try:
            fg = win32gui.GetForegroundWindow()
            shell = win32com.client.Dispatch("Shell.Application")
            if fg:
                for w in shell.Windows():
                    try:
                        if int(w.HWND) == fg:
                            p = str(w.Document.Folder.Self.Path)
                            if p and os.path.isdir(p) and not p.startswith("::{"):
                                return p
                    except Exception:
                        pass
            if not foreground_only:
                for w in shell.Windows():
                    try:
                        p = str(w.Document.Folder.Self.Path)
                        if p and os.path.isdir(p) and not p.startswith("::{"):
                            return p
                    except Exception:
                        pass
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
    except Exception:
        pass
    return None


def get_open_explorer_folder() -> str | None:
    """Return a real folder open in Explorer, without using stale session context."""
    path = _get_active_explorer_path(foreground_only=True) or _get_active_explorer_path()
    if path and is_safe_path(path, "search") and os.path.isdir(path):
        return path
    return None


def get_active_folder() -> str | None:
    """
    Return the current active folder path, or None.
    Dynamically prioritizes:
      1. Currently focused Explorer window
      2. The session's stored _ACTIVE_FOLDER
      3. Any open Explorer window
    """
    global _ACTIVE_FOLDER

    # 1. Check if foreground window is an Explorer window
    explorer_fg = _get_active_explorer_path(foreground_only=True)
    if explorer_fg and is_safe_path(explorer_fg, "search") and not _is_ignored_search_path(explorer_fg) and os.path.isdir(explorer_fg):
        if explorer_fg != _ACTIVE_FOLDER:
            _ACTIVE_FOLDER = explorer_fg
            session.active_folder = explorer_fg
            _jprint(f"[CONTEXT] Active folder synced from Explorer foreground -> {_ACTIVE_FOLDER}")
        return _ACTIVE_FOLDER

    # 2. Return stored active folder if valid
    if _ACTIVE_FOLDER and is_safe_path(_ACTIVE_FOLDER, "search") and os.path.isdir(_ACTIVE_FOLDER):
        if not _is_ignored_search_path(_ACTIVE_FOLDER):
            return _ACTIVE_FOLDER
        _jprint(f"[CONTEXT] Clearing generated dependency folder from active search scope -> {_ACTIVE_FOLDER}")
        _ACTIVE_FOLDER = None
        session.active_folder = None

    # 3. Fallback: check any open Explorer window
    explorer_any = _get_active_explorer_path(foreground_only=False)
    if explorer_any and is_safe_path(explorer_any, "search") and not _is_ignored_search_path(explorer_any) and os.path.isdir(explorer_any):
        _ACTIVE_FOLDER = explorer_any
        session.active_folder = explorer_any
        _jprint(f"[CONTEXT] Active folder synced from open Explorer -> {_ACTIVE_FOLDER}")
        return _ACTIVE_FOLDER

    return None


def set_active_folder(path_str: str) -> None:
    """
    Set the active folder to path_str.
    Called when a directory or a file within a directory is successfully opened.
    """
    global _ACTIVE_FOLDER
    if not is_safe_path(path_str, "search"):
        _jprint(UNSAFE_PATH_MESSAGE)
        return
    _ACTIVE_FOLDER = str(path_str)
    session.activate_context(f"folder:{os.path.normcase(os.path.normpath(_ACTIVE_FOLDER))}")
    session.active_folder = _ACTIVE_FOLDER
    _jprint(f"[CONTEXT] Active folder set -> {_ACTIVE_FOLDER}")


def clear_active_folder() -> None:
    """Clear the active folder (search everywhere after this)."""
    global _ACTIVE_FOLDER
    _ACTIVE_FOLDER = None
    session.active_folder = None
    _jprint("[CONTEXT] Active folder cleared.")


def _match_folder_score(query: str, folder_name: str) -> int:
    """
    Score how well a folder name matches a search query.
    Allows half of the folder name, unique words, bare numbers, abbreviations.
    Returns 0 if no match, higher score = better match.
    """
    q = query.strip().lower()
    fn = folder_name.strip().lower()
    q = re.sub(r"^(?:the|my)\s+", "", q)
    q = re.sub(r"\s+(?:folder|directory)$", "", q).strip()
    if not q or not fn:
        return 0

    # 1. Exact match
    if q == fn:
        return 1000

    # 2. Season abbreviation: 's1' -> 'season 1'
    s_abbr = re.fullmatch(r"s0*(\d+)", q)
    if s_abbr:
        num = s_abbr.group(1)
        if re.search(rf"\b(?:season|s)\s*0*{num}\b", fn):
            return 900

    # 3. Bare number for folders: '1' -> 'season 1', 'season 01'
    if q.isdigit():
        num = int(q)
        if re.search(rf"\b(?:season|s|part|pt|vol)\s*0*{num}\b", fn):
            return 850
        if re.search(rf"(?:^|\D)0*{num}(?:\D|$)", fn):
            return 700
        return 0

    # 4. Starts with
    if fn.startswith(q):
        return 800

    # 5. Standalone word boundary match (e.g. 'clover' in 'black clover')
    if re.search(rf"\b{re.escape(q)}\b", fn):
        return 750

    # 6. Substring match (half the name, e.g. 'season' in 'season 1', 'clover' in 'black clover')
    if len(q) >= 3 and q in fn:
        return 650

    # 7. Token matching
    tokens = [t for t in re.split(r"[\s_\-]+", q) if t]
    if tokens:
        matches = sum(1 for t in tokens if re.search(rf"\b{re.escape(t)}\b", fn) or t in fn)
        if matches == len(tokens):
            return 600 + len(tokens) * 10
        elif matches > 0 and len(tokens) > 1:
            return 200 * matches

    return 0


def _match_file_score(query: str, filename: str) -> int:
    """
    Score how well any filename matches a search query.
    Supports bare numbers, episode patterns, unique words, partial words, half names.
    Returns 0 if no match, higher score = better match.
    """
    stem, ext = os.path.splitext(filename)
    stem_lower = stem.lower()
    q_lower = query.strip().lower()

    q_clean = re.sub(r"^(?:the|that|a|my)\s+", "", q_lower).strip()
    if not q_clean:
        q_clean = q_lower

    # Generic video/movie words: if filename contains 'movie' or 'video', prioritize it!
    if q_clean in {"movie", "the movie", "video", "that video"}:
        if "movie" in stem_lower or "video" in stem_lower:
            return 850
        if ext.lower() in _VIDEO_EXT:
            return 100
        return 0

    if q_clean in {"song", "music", "audio"}:
        if ext.lower() in _AUDIO_EXT:
            return 100
        return 50 if q_clean in stem_lower else 0

    # 1. Exact stem match
    if stem_lower == q_clean:
        return 1000

    # 2. Number matching
    bare_num_match = re.fullmatch(r"0*(\d+)", q_clean)
    if bare_num_match:
        num = int(bare_num_match.group(1))
        # Ep prefix + number
        if re.search(rf"\b(?:ep|episode|e)\s*0*{num}\b", stem_lower):
            return 850
        # Standalone number bounded by non-digits
        if re.search(rf"(?:^|\D)0*{num}(?:\D|$)", stem_lower):
            return 800
        # S##E## pattern
        if re.search(rf"s\d+e0*{num}\b", stem_lower):
            return 750
        return 0

    # 3. Episode pattern in query: 'episode 5', 'ep 163', 'e163'
    ep_q = re.search(r"(?:ep|episode|e)\s*0*(\d+)", q_clean)
    if ep_q:
        num = int(ep_q.group(1))
        if re.search(rf"\b(?:ep|episode|e)\s*0*{num}\b", stem_lower):
            return 850
        if re.search(rf"(?:^|\D)0*{num}(?:\D|$)", stem_lower):
            return 750

    # 4. Starts with / word boundary / substring (at least 2 chars)
    if stem_lower.startswith(q_clean):
        return 800
    if re.search(rf"\b{re.escape(q_clean)}\b", stem_lower):
        return 750
    if len(q_clean) >= 3 and q_clean in stem_lower:
        return 650

    # 5. Token matching
    tokens = [t for t in re.split(r"[\s_\-\.\[\]\(\)]+", q_clean) if t]
    if tokens:
        matches = 0
        for t in tokens:
            if t.isdigit():
                num = int(t)
                if re.search(rf"(?:^|\D)0*{num}(?:\D|$)", stem_lower):
                    matches += 1
            elif t in stem_lower:
                matches += 1
        if matches == len(tokens):
            return 600 + len(tokens) * 10
        elif matches > 0 and len(tokens) > 1:
            return 150 * matches

    # 6. Single unique word substring (half name)
    if len(tokens) == 1 and not tokens[0].isdigit():
        if tokens[0] in stem_lower:
            return 600

    return 0


def _match_media_score(query: str, filename: str) -> int:
    """
    Score how well a media filename matches a search query.
    Only called for files with extensions in _MEDIA_EXT.
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _MEDIA_EXT:
        return 0
    return _match_file_score(query, filename)


def find_folder_in_hierarchy(query: str, active_folder: str | None = None) -> str | None:
    """
    Search for a folder matching query across the folder hierarchy:
      1. Immediate and nested subfolders of active_folder (recursive down to depth 4).
      2. Sibling subfolders under active_folder's parent.
      3. Parent folder itself.
      4. Grandparent's other child folders (e.g. other series in library).
    Allows half of the folder name, unique keywords, numbers, abbreviations.
    """
    if not active_folder or not is_safe_path(active_folder, "search") or _is_ignored_search_path(active_folder) or not os.path.isdir(active_folder):
        return None
    active = Path(active_folder)

    # 1. Search nested folders inside active_folder (recursive)
    nested_candidates = []
    try:
        for root, dirs, _ in os.walk(active):
            if not _prune_unsafe_walk_dirs(root, dirs):
                continue
            _prune_search_dirs(dirs)
            root_depth = len(Path(root).relative_to(active).parts)
            if root_depth >= 4:
                dirs.clear()
                continue
            for d in dirs:
                full = os.path.join(root, d)
                score = _match_folder_score(query, d)
                if score > 0:
                    depth = len(Path(full).relative_to(active).parts)
                    nested_candidates.append((score - depth * 5, full))
    except Exception:
        pass
    if nested_candidates:
        nested_candidates.sort(key=lambda x: (-x[0], x[1]))
        if nested_candidates[0][0] >= 600:
            return nested_candidates[0][1]

    # Check if active folder itself is the target
    a_score = _match_folder_score(query, active.name)
    if a_score >= 800:
        return str(active)

    # 2. Check sibling folders under parent (including active for ranking)
    parent = active.parent
    if parent and parent != active and parent.exists() and is_safe_path(str(parent), "search"):
        sibling_candidates = []
        try:
            for item in parent.iterdir():
                if item.is_dir():
                    score = _match_folder_score(query, item.name)
                    if score > 0:
                        sibling_candidates.append((score, str(item)))
        except Exception:
            pass
        if sibling_candidates:
            sibling_candidates.sort(key=lambda x: (-x[0], x[1]))
            if sibling_candidates[0][0] >= 600:
                return sibling_candidates[0][1]

        # 3. Check parent folder itself
        p_score = _match_folder_score(query, parent.name)
        if p_score >= 600:
            return str(parent)

    # 4. Check grandparent's other folders
    if parent and parent.parent and parent.parent != parent and parent.parent.exists() and is_safe_path(str(parent.parent), "search"):
        gp = parent.parent
        gp_candidates = []
        try:
            for item in gp.iterdir():
                if item.is_dir() and item != parent:
                    score = _match_folder_score(query, item.name)
                    if score > 0:
                        gp_candidates.append((score, str(item)))
        except Exception:
            pass
        if gp_candidates:
            gp_candidates.sort(key=lambda x: (-x[0], x[1]))
            if gp_candidates[0][0] >= 650:
                return gp_candidates[0][1]

    return None


def find_file_in_hierarchy(query: str, active_folder: str | None = None, media_only: bool = False) -> str | None:
    """
    Search for a file matching query:
      - If active_folder is Desktop: ONLY search direct files on the Desktop (never into subfolders).
      - If active_folder is a specific folder: search within that folder.
      - For regular files (media_only=False): strictly scoped to active_folder (never bleeding into parent/siblings/grandparents).
      - For media files (media_only=True): allows series hierarchy navigation across seasons.
    """
    if not active_folder or not is_safe_path(active_folder, "search") or _is_ignored_search_path(active_folder) or not os.path.isdir(active_folder):
        return None
    active = Path(active_folder)

    # 1. Desktop Barrier: Never descend into subfolders on Desktop
    if is_desktop_path(active):
        candidates = []
        try:
            for entry in os.scandir(active):
                if entry.is_file(follow_symlinks=False):
                    ext = os.path.splitext(entry.name)[1].lower()
                    if media_only and ext not in _MEDIA_EXT:
                        continue
                    score = _match_file_score(query, entry.name)
                    if score > 0:
                        candidates.append((score, entry.path))
        except Exception:
            pass
        if candidates:
            candidates.sort(key=lambda x: -x[0])
            return candidates[0][1]
        return None

    # 2. Specific folder search
    candidates = []
    try:
        for root, dirs, files in os.walk(active):
            if not _prune_unsafe_walk_dirs(root, dirs):
                continue
            _prune_search_dirs(dirs)
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if media_only and ext not in _MEDIA_EXT:
                    continue
                score = _match_file_score(query, f)
                if score > 0:
                    full = os.path.join(root, f)
                    depth = len(Path(full).relative_to(active).parts) - 1
                    media_bonus = 60 if ext in _MEDIA_EXT else 0
                    candidates.append((score + media_bonus - depth * 5, full))
    except Exception:
        pass

    if candidates:
        candidates.sort(key=lambda x: -x[0])
        return candidates[0][1]

    # For non-media files (e.g. table.py, code, text, documents), NEVER search sibling folders or grandparents
    if not media_only:
        return None

    # 3. Media-only series fallback: Search sibling folders under parent
    parent = active.parent
    if parent and parent != active and parent.exists() and is_safe_path(str(parent), "search") and not is_desktop_path(parent):
        sibling_candidates = []
        try:
            for item in parent.iterdir():
                if item.is_dir() and item != active and not _is_ignored_search_path(item):
                    for root, dirs, files in os.walk(item):
                        if not _prune_unsafe_walk_dirs(root, dirs):
                            continue
                        _prune_search_dirs(dirs)
                        for f in files:
                            ext = os.path.splitext(f)[1].lower()
                            if ext not in _MEDIA_EXT:
                                continue
                            score = _match_file_score(query, f)
                            if score > 0:
                                full = os.path.join(root, f)
                                sibling_candidates.append((score + 60, full))
        except Exception:
            pass
        if sibling_candidates:
            sibling_candidates.sort(key=lambda x: -x[0])
            return sibling_candidates[0][1]

    return None


def _search_media_in_folder(
    folder_path: str,
    query: str,
    limit: int = 50,
) -> list:
    """
    Fast recursive search inside folder_path for supported audio/video files
    matching query.
    Returns ranked list of full path strings.
    """
    if _is_ignored_search_path(folder_path):
        return []

    results = []
    stack = [folder_path]
    visited = set()

    while stack:
        current = stack.pop()
        if not is_safe_path(current, "search"):
            continue
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.casefold() in _SEARCH_SKIP_DIR_NAMES:
                                continue
                            if not is_safe_path(entry.path, "search"):
                                continue
                            try:
                                st = entry.stat(follow_symlinks=False)
                                ikey = (st.st_dev, st.st_ino)
                                if ikey in visited and ikey != (0, 0):
                                    continue
                                visited.add(ikey)
                            except Exception:
                                pass
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            ext = os.path.splitext(entry.name)[1].lower()
                            if ext in _MEDIA_EXT:
                                score = _match_media_score(query, entry.name)
                                if score > 0:
                                    results.append((score, entry.path))
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, FileNotFoundError, OSError):
            continue

    # Sort highest score first, then shorter filename
    results.sort(key=lambda item: (-item[0], len(os.path.basename(item[1]))))
    return [path for score, path in results[:limit]]


def _find_vlc_executable():
    """Find VLC without scanning the whole disk; return None when unavailable."""
    executable = shutil.which("vlc") or shutil.which("vlc.exe")
    if executable:
        return executable
    for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if root:
            candidate = os.path.join(root, "VideoLAN", "VLC", "vlc.exe")
            if os.path.isfile(candidate):
                return candidate
    return None


def _open_media_file(path_str: str, player=None) -> bool:
    """
    Open a media file using os.startfile() (Windows default player).
    Does NOT call Path.resolve() so network paths are safe.
    """
    try:
        if not is_safe_path(path_str, "open_readonly"):
            _jprint(UNSAFE_PATH_MESSAGE)
            return False
        if _is_desktop_non_app_file(path_str):
            _jprint(f"[CONTEXT] Desktop access is limited to apps and folders: {path_str}")
            return False
        if os.path.exists(path_str):
            _jprint(f"[CONTEXT] Playing -> {path_str}")
            if player and str(player).lower().strip() in {"vlc", "vlc media player", "videolan"}:
                vlc = _find_vlc_executable()
                if not vlc:
                    _jprint("[CONTEXT] VLC was requested but vlc.exe was not found.")
                    return False
                subprocess.Popen([vlc, path_str], shell=False)
            else:
                os.startfile(path_str)
            return True
        else:
            _jprint(f"[CONTEXT] Media file not found: {path_str}")
            return False
    except Exception as e:
        _jprint(f"[CONTEXT] Could not open media: {e}")
        return False

# ============================================================
# BASIC HELPERS
# ============================================================

def open_path(path):
    """
    Open a LOCAL path (safe to use resolve() here).
    For network/UNC paths use open_path_safe() instead.
    """
    try:
        path_obj = Path(str(path)).expanduser()
        # Preserve absolute drive and UNC paths; resolve only relative paths.
        path_str = str(path_obj)
        if not path_str.startswith("\\\\") and not re.match(r"^[A-Za-z]:[\\/]", path_str):
            try:
                path_obj = path_obj.resolve()
            except Exception:
                pass

        if not path_obj.exists():
            print(f"\u26a0\ufe0f Path does not exist: {path_obj}")
            return False

        if not is_safe_path(str(path_obj), "open_readonly", allow_windows_navigation=True):
            print(UNSAFE_PATH_MESSAGE)
            return False

        if path_obj.is_file() and _is_desktop_non_app_file(str(path_obj)):
            print("\u26a0\ufe0f On the Desktop, Jarvis can open apps and folders, not loose files.")
            return False

        os.startfile(str(path_obj))
        if path_obj.is_dir() and is_safe_path(str(path_obj), "search"):
            set_active_folder(str(path_obj))
        else:
            session.last_opened_file = str(path_obj)
        print(f"\U0001f4c2 Opening PC path \u2192 {path_obj}")
        return True

    except Exception as e:
        print(f"\u26a0\ufe0f Could not open path: {e}")
        return False


_BROWSER_COMMANDS = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "brave": "brave",
    "brave browser": "brave",
    "edge": "msedge",
    "microsoft edge": "msedge",
}
_BROWSER_PROCESS_NAMES = {
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "brave": "brave.exe",
    "brave browser": "brave.exe",
    "edge": "msedge.exe",
    "microsoft edge": "msedge.exe",
}

# Canonical home pages and common search routes used by destination-aware
# voice commands. Arbitrary domains are accepted too; these maps only provide
# better direct navigation/search for popular services.
WEBSITE_URLS = {
    "youtube": "https://www.youtube.com/",
    "google": "https://www.google.com/",
    "github": "https://github.com/",
    "chatgpt": "https://chatgpt.com/",
    "samsung": "https://www.samsung.com/",
    "gmail": "https://mail.google.com/",
    "reddit": "https://www.reddit.com/",
    "instagram": "https://www.instagram.com/",
    "facebook": "https://www.facebook.com/",
    "twitter": "https://x.com/",
    "x": "https://x.com/",
    "linkedin": "https://www.linkedin.com/",
    "amazon": "https://www.amazon.com/",
    "wikipedia": "https://www.wikipedia.org/",
    "netflix": "https://www.netflix.com/",
    "spotify": "https://open.spotify.com/",
    "discord": "https://discord.com/app",
    "whatsapp": "https://web.whatsapp.com/",
    "twitch": "https://www.twitch.tv/",
    "vimeo": "https://vimeo.com/",
    "tiktok": "https://www.tiktok.com/",
    "pinterest": "https://www.pinterest.com/",
    "onedrive": "https://onedrive.live.com/",
    "outlook": "https://outlook.live.com/",
    "google drive": "https://drive.google.com/",
    "google docs": "https://docs.google.com/",
    "google maps": "https://maps.google.com/",
    "slack": "https://slack.com/signin",
    "teams": "https://teams.microsoft.com/",
    "steam": "https://store.steampowered.com/",
    "stackoverflow": "https://stackoverflow.com/",
    "stackoverflow.com": "https://stackoverflow.com/",
    "apple": "https://www.apple.com/",
    "disney plus": "https://www.disneyplus.com/",
    "prime video": "https://www.primevideo.com/",
    "microsoft": "https://www.microsoft.com/",
    "bing": "https://www.bing.com/",
    "yahoo": "https://www.yahoo.com/",
    "quora": "https://www.quora.com/",
    "notion": "https://www.notion.so/",
    "canva": "https://www.canva.com/",
    "figma": "https://www.figma.com/",
    "roblox": "https://www.roblox.com/",
    "epic games": "https://store.epicgames.com/",
    "coursera": "https://www.coursera.org/",
    "udemy": "https://www.udemy.com/",
    "medium": "https://medium.com/",
    "telegram": "https://web.telegram.org/",
    "zoom": "https://zoom.us/",
    "icloud": "https://www.icloud.com/",
}

WEBSITE_SEARCH_URLS = {
    "youtube": "https://www.youtube.com/results?search_query={query}",
    "google": "https://www.google.com/search?q={query}",
    "github": "https://github.com/search?q={query}",
    "reddit": "https://www.reddit.com/search/?q={query}",
    "twitter": "https://x.com/search?q={query}",
    "x": "https://x.com/search?q={query}",
    "amazon": "https://www.amazon.com/s?k={query}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={query}",
    "linkedin": "https://www.linkedin.com/search/results/all/?keywords={query}",
    "facebook": "https://www.facebook.com/search/top/?q={query}",
    "netflix": "https://www.netflix.com/search?q={query}",
    "spotify": "https://open.spotify.com/search/{query}",
    "steam": "https://store.steampowered.com/search/?term={query}",
    "stackoverflow": "https://stackoverflow.com/search?q={query}",
    "stackoverflow.com": "https://stackoverflow.com/search?q={query}",
    "twitch": "https://www.twitch.tv/search?term={query}",
    "vimeo": "https://vimeo.com/search?q={query}",
    "tiktok": "https://www.tiktok.com/search?q={query}",
    "pinterest": "https://www.pinterest.com/search/pins/?q={query}",
    "bing": "https://www.bing.com/search?q={query}",
    "yahoo": "https://search.yahoo.com/search?p={query}",
    "quora": "https://www.quora.com/search?q={query}",
    "notion": "https://www.notion.so/search?query={query}",
    "canva": "https://www.canva.com/search?q={query}",
    "figma": "https://www.figma.com/search?q={query}",
    "medium": "https://medium.com/search?q={query}",
    "coursera": "https://www.coursera.org/search?query={query}",
    "udemy": "https://www.udemy.com/courses/search/?q={query}",
    "gmail": "https://mail.google.com/mail/u/0/#search/{query}",
    "google drive": "https://drive.google.com/drive/u/0/search?q={query}",
    "google docs": "https://docs.google.com/document/u/0/?q={query}",
    "google maps": "https://www.google.com/maps/search/{query}",
    "outlook": "https://outlook.live.com/mail/0/search?q={query}",
}


def _website_destination_url(destination):
    name = str(destination or "").strip().lower().removesuffix(" website").removesuffix(" site").strip()
    if name in WEBSITE_URLS:
        return WEBSITE_URLS[name], name
    raw = str(destination or "").strip()
    if re.match(r"^(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?$", raw, flags=re.IGNORECASE):
        url = raw if re.match(r"^https?://", raw, flags=re.IGNORECASE) else "https://" + raw
        host = (urllib.parse.urlparse(url).hostname or raw).lower().removeprefix("www.")
        return url, host
    return None, None


def search_website_destination(destination, query, browser=None, *, same_tab=None):
    """Search a website, reusing its current tab when it is already active."""
    query = str(query or "").strip()
    home_url, site_name = _website_destination_url(destination)
    if not home_url or not query:
        return None

    active = get_active_website_context()
    if same_tab is None:
        same_tab = bool(active and active[1] == site_name and (not browser or _canonical_browser_name(browser) == active[0]))
    if same_tab and active and not browser:
        browser = active[0]

    if site_name == "youtube":
        return search_youtube(query, browser=browser, same_tab=same_tab)

    encoded = urllib.parse.quote_plus(query)
    route = WEBSITE_SEARCH_URLS.get(site_name)
    if route:
        url = route.format(query=encoded)
        if not open_url(url, browser=browser, new_tab=not same_tab):
            return None
        session.last_opened_website = site_name
        if browser:
            session.set_current_app(browser)
        return f"Searching {site_name} for {query}."
    parsed_home = urllib.parse.urlparse(home_url)
    site_origin = f"{parsed_home.scheme or 'https'}://{parsed_home.netloc}"
    url = site_origin + "/search?q=" + encoded
    if not open_url(url, browser=browser, new_tab=not same_tab):
        return None
    session.last_opened_website = site_name
    if browser:
        session.set_current_app(browser)
    return f"Searching {site_name} for {query}."


def _browser_for_process(process_name: str) -> str | None:
    process_name = (process_name or "").lower()
    return {
        "chrome.exe": "chrome",
        "brave.exe": "brave",
        "msedge.exe": "edge",
    }.get(process_name)


def _canonical_browser_name(browser: str | None) -> str | None:
    name = (browser or "").strip().lower()
    return {
        "google chrome": "chrome",
        "brave browser": "brave",
        "microsoft edge": "edge",
    }.get(name, name or None)


def _website_from_window_title(title: str) -> str | None:
    page_title = re.sub(
        r"\s*[-|—]\s*(?:Google Chrome|Microsoft Edge|Brave(?: Browser)?)(?:\s*[-|—].*)?$",
        "",
        title or "",
        flags=re.IGNORECASE,
    ).strip().lower()
    if not page_title or page_title in {"new tab", "new tab page", "start page"}:
        return None
    if page_title == "x":
        return "x"

    for site in sorted(WEBSITE_URLS, key=len, reverse=True):
        if len(site) >= 3 and re.search(rf"(?<![a-z0-9]){re.escape(site)}(?![a-z0-9])", page_title):
            return site

    for site, home_url in WEBSITE_URLS.items():
        hostname = (urllib.parse.urlparse(home_url).hostname or "").lower().removeprefix("www.")
        if hostname and hostname in page_title:
            return site

    domain_match = re.search(r"\b([a-z0-9-]+\.(?:com|net|org|io|co|edu|gov|app|dev))\b", page_title)
    return domain_match.group(1) if domain_match else None


def get_active_website_context() -> tuple[str, str] | None:
    """Return (browser, website) only when a browser window is foreground."""
    try:
        from tools.window_tools import get_foreground_window, get_window_process_name, get_window_title

        hwnd = get_foreground_window()
        process_name = get_window_process_name(hwnd)
        browser = _browser_for_process(process_name)
        if not browser:
            return None

        title = get_window_title(hwnd)
        site = _website_from_window_title(title)
        if not site:
            page_title = re.sub(r"\s*[-|—]\s*(?:Google Chrome|Microsoft Edge|Brave(?: Browser)?)\s*$", "", title or "", flags=re.IGNORECASE).strip().lower()
            if page_title in {"new tab", "new tab page", "start page"}:
                return None
            saved_site = (session.last_opened_website or "").strip().lower()
            if saved_site and _website_destination_url(saved_site)[0]:
                site = saved_site
        return (browser, site) if site else None
    except Exception:
        return None


def _navigate_current_browser_tab(url: str, browser: str | None = None) -> bool:
    """Navigate an existing browser tab using its address bar, without a new tab."""
    try:
        from tools.window_tools import (
            find_any_window,
            force_foreground_window,
            get_foreground_window,
            get_window_process_name,
        )

        # Normalize broken schemes from keyboard shortcuts, drive-letter mistranslations, or missing protocols
        url_str = str(url).strip()
        if url_str.lower().startswith("file:///s:/") or url_str.lower().startswith("file:///s://"):
            clean_target = re.sub(r"^file:///[sS]:/*", "", url_str)
            url = "https://" + clean_target.lstrip("/")
        elif url_str.lower().startswith("s://"):
            url = "https://" + url_str[4:]
        elif url_str.lower().startswith("ttps://"):
            url = "https://" + url_str[7:]
        elif url_str.lower().startswith("ttp://"):
            url = "http://" + url_str[6:]
        elif not url_str.startswith(("http://", "https://", "file://", "chrome://")):
            url = f"https://{url_str}"

        hwnd = get_foreground_window()
        foreground_browser = _browser_for_process(get_window_process_name(hwnd))
        requested_browser = _canonical_browser_name(browser) or foreground_browser or ""
        if not requested_browser:
            return False
        if foreground_browser != requested_browser:
            hwnd = find_any_window(requested_browser)
        if not hwnd or not force_foreground_window(hwnd):
            return False
        if _browser_for_process(get_window_process_name(get_foreground_window())) != requested_browser:
            return False

        # Release any lingering modifier keys (Ctrl, Alt, Shift, Win) to avoid key chords
        for mod_vk in (0x11, 0x12, 0x10, 0x5B, 0x5C):
            ctypes.windll.user32.keybd_event(mod_vk, 0, 2, 0)

        # Ctrl + L to select address bar
        ctypes.windll.user32.keybd_event(0x11, 0, 0, 0)
        ctypes.windll.user32.keybd_event(0x4C, 0, 0, 0)
        ctypes.windll.user32.keybd_event(0x4C, 0, 2, 0)
        ctypes.windll.user32.keybd_event(0x11, 0, 2, 0)
        time.sleep(0.08)  # Pause so browser focuses address bar and releases Ctrl

        pasted = False
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            kernel32.GlobalAlloc.restype = ctypes.c_void_p
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
            kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
            user32.SetClipboardData.restype = ctypes.c_void_p
            user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
            if user32.OpenClipboard(0):
                try:
                    user32.EmptyClipboard()
                    data = str(url).encode("utf-16le") + b"\x00\x00"
                    h_mem = kernel32.GlobalAlloc(0x0042, len(data))
                    if h_mem:
                        p_mem = kernel32.GlobalLock(h_mem)
                        if p_mem:
                            ctypes.memmove(p_mem, data, len(data))
                            kernel32.GlobalUnlock(h_mem)
                            CF_UNICODETEXT = 13
                            if user32.SetClipboardData(CF_UNICODETEXT, h_mem):
                                pasted = True
                finally:
                    user32.CloseClipboard()
        except Exception as clip_err:
            _jprint(f"⚠️ Clipboard paste setup error: {clip_err}")

        if pasted:
            # Ctrl + V to paste full URL intact
            ctypes.windll.user32.keybd_event(0x11, 0, 0, 0)
            ctypes.windll.user32.keybd_event(0x56, 0, 0, 0)
            ctypes.windll.user32.keybd_event(0x56, 0, 2, 0)
            ctypes.windll.user32.keybd_event(0x11, 0, 2, 0)
            time.sleep(0.06)
        else:
            # SendInput fallback with Unicode encoding
            from ctypes import wintypes
            ULONG_PTR = ctypes.c_size_t

            class KEYBDINPUT(ctypes.Structure):
                _fields_ = [
                    ("wVk", wintypes.WORD),
                    ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR),
                ]

            class MOUSEINPUT(ctypes.Structure):
                _fields_ = [
                    ("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR),
                ]

            class HARDWAREINPUT(ctypes.Structure):
                _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

            class INPUTUNION(ctypes.Union):
                _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

            class INPUT(ctypes.Structure):
                _anonymous_ = ("value",)
                _fields_ = [("type", wintypes.DWORD), ("value", INPUTUNION)]

            INPUT_KEYBOARD = 1
            KEYEVENTF_KEYUP = 0x0002
            KEYEVENTF_UNICODE = 0x0004

            def key(vk=0, scan=0, flags=0):
                item = INPUT()
                item.type = INPUT_KEYBOARD
                item.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
                return item

            char_inputs = []
            for char in str(url):
                encoded = char.encode("utf-16-le", errors="surrogatepass")
                for offset in range(0, len(encoded), 2):
                    code_unit = int.from_bytes(encoded[offset:offset + 2], "little")
                    char_inputs.append(key(scan=code_unit, flags=KEYEVENTF_UNICODE))
                    char_inputs.append(key(scan=code_unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
            if char_inputs:
                ctypes.windll.user32.SendInput(len(char_inputs), (INPUT * len(char_inputs))(*char_inputs), ctypes.sizeof(INPUT))
                time.sleep(0.06)

        # Press Enter (0x0D) to submit navigation
        ctypes.windll.user32.keybd_event(0x0D, 0, 0, 0)
        ctypes.windll.user32.keybd_event(0x0D, 0, 2, 0)
        time.sleep(0.25)
        return _browser_for_process(get_window_process_name(get_foreground_window())) == requested_browser
    except Exception as e:
        _jprint(f"⚠️ Could not search in the current browser tab: {e}")
        return False


def open_url(url, browser=None, *, new_tab=False):
    """
    Open or navigate to a URL reusing the active tab.
    Never opens a new tab unless explicitly requested and no tabs exist.
    """
    try:
        from tools.browser_controller import navigate_to_url
        if navigate_to_url(url, new_tab=new_tab):
            _jprint(f"🌐 Navigated tab → {url}")
            return True
    except Exception as e:
        _jprint(f"⚠️ Browser controller navigation failed: {e}")

    try:
        if _navigate_current_browser_tab(url, browser=browser):
            _jprint(f"🌐 Navigated current tab via address bar → {url}")
            return True
    except Exception as e:
        _jprint(f"⚠️ Address bar navigation failed: {e}")

    try:
        import webbrowser
        _jprint(f"🌐 Opening (tab reuse fallback) → {url}")
        return bool(webbrowser.open(url, new=0))
    except Exception as e:
        _jprint(f"⚠️ Could not open URL: {e}")
        return False


def run_command(command):
    try:
        if isinstance(command, str) and command.strip().lower().startswith("start "):
            target = command.strip()[6:].strip()
            os.startfile(target)
            return True
        args = shlex.split(command, posix=False) if isinstance(command, str) else list(command)
        subprocess.Popen(
            args,
            shell=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True

    except Exception as e:
        _jprint(f"⚠️ Command failed: {e}")
        return False


def press_key(key):
    try:
        ctypes.windll.user32.keybd_event(
            key,
            0,
            0,
            0,
        )

        ctypes.windll.user32.keybd_event(
            key,
            0,
            2,
            0,
        )

        return True

    except Exception as e:
        print(f"⚠️ Key press failed: {e}")
        return False


# ============================================================
# COMMAND CLEANING
# ============================================================

def clean_command(command):
    if not command:
        return ""

    cmd = command.strip()

    # Remove the optional spoken wake-name prefix before local intent
    # matching. Transcripts such as "Hey Jarvis, open Chrome" must reach the
    # same local handler as "open Chrome" instead of falling through to AI.
    cmd = re.sub(
        r"^(?:(?:hey|okay|ok)\s+)?jarvis(?:\s+please)?\b[\s,:-]*",
        "",
        cmd,
        flags=re.IGNORECASE,
    )
    cmd = re.sub(r"^please\s+", "", cmd, flags=re.IGNORECASE)

    # 1. Remove commas directly following command verbs:
    # "open, WhatsApp" -> "open WhatsApp"
    # "open, Black Clover folder" -> "open Black Clover folder"
    # "play, episode 1" -> "play episode 1"
    # "switch to, VLC" -> "switch to VLC"
    cmd = re.sub(
        r"\b(open|launch|start|run|switch to|switch|go to|goto|visit|play|watch|listen to|close|show|bring|focus|maximize|maximise|restore|minimize|minimise|search|scroll|page)\s*,\s*",
        r"\1 ",
        cmd,
        flags=re.IGNORECASE,
    )
    # STT can repeat the command verb itself: "open, open Downloads folder".
    cmd = re.sub(
        r"^(open|launch|start|run|switch to|switch|go to|visit|play|watch|close|maximize|maximise|restore|minimize|minimise|scroll|page)\s+\1\s+",
        r"\1 ",
        cmd,
        flags=re.IGNORECASE,
    )
    # Whisper may repeat a short system command without a target (for
    # example, "maximize, maximize"). The earlier rule handles repetitions
    # before a target, but this exact two-word form otherwise becomes an app
    # name and is reported as "Maximize isn't open".
    cmd = re.sub(
        r"^(maximize|maximise|restore|minimize|minimise|close|switch|scroll)\s+\1$",
        r"\1",
        cmd,
        flags=re.IGNORECASE,
    )

    # Normalize "goto" -> "go to"
    cmd = re.sub(r"\bgoto\b", "go to", cmd, flags=re.IGNORECASE)

    # 2. Normalize "open the <X> folder" / "open <X>'s folder" -> "open <X> folder"
    cmd = re.sub(r"^(open|go to)\s+the\s+(.+?\s+folder)$", r"\1 \2", cmd, flags=re.IGNORECASE)
    cmd = re.sub(r"^(open|go to)\s+(.+?)'s\s+folder$", r"\1 \2 folder", cmd, flags=re.IGNORECASE)

    # 3. Remove punctuation commonly added by STT (. , ! ? ; :) from boundaries
    cmd = cmd.strip(".,!?;:\"' ")
    cmd = re.sub(r"\s+please$", "", cmd, flags=re.IGNORECASE)

    # 4. Normalize whitespace
    cmd = re.sub(r"\s+", " ", cmd).strip()

    # Speech recognition can repeat the complete app-opening phrase when a
    # speaker says it more than once (for example, "open YouTube open YouTube").
    # Treat two or three identical requests as one so they still reach the
    # normal app/site opener instead of making the repeated text part of the
    # requested app name.
    repeated_open = re.fullmatch(
        r"(?P<verb>open|launch|start|go to)\s+(?P<target>.+?)"
        r"(?:\s+(?P=verb)\s+(?P=target)){1,2}",
        cmd,
        flags=re.IGNORECASE,
    )
    if repeated_open:
        cmd = f"{repeated_open.group('verb')} {repeated_open.group('target')}"

    return cmd


# ============================================================
# PC FOLDER SEARCH
# ============================================================

def get_search_roots():
    roots = [KNOWN_FOLDERS[name] for name in ("desktop", "documents", "downloads", "pictures", "videos", "music")]

    # Remove duplicates
    unique = []

    for root in roots:

        try:
            root = root.resolve()
        except Exception:
            continue

        if root not in unique and root.exists():
            unique.append(root)

    return unique


def find_pc_folder(folder_name):
    """
    Find a folder by name.  Uses the persistent SQLite index
    for instant results.  Ranks exact folder name match first,
    then substring matches.  Falls back to direct paths and scan.
    """
    if not folder_name:
        return None

    raw_query = folder_name.strip().strip('"').strip("'")
    if not raw_query:
        return None

    # Clean natural command language from folder query
    query_clean = re.sub(r"^(?:the|my)\s+", "", raw_query, flags=re.IGNORECASE).strip()
    query_clean = re.sub(r"(?:'s|s)?\s+(?:folder|directory)$", "", query_clean, flags=re.IGNORECASE).strip()
    if not query_clean:
        query_clean = raw_query

    if query_clean.casefold() in {"windows", "windows folder", "the windows folder", "system windows folder"}:
        windows_path = Path(os.environ.get("WINDIR", r"C:\Windows")).resolve(strict=False)
        return windows_path if is_safe_path(str(windows_path), "open_readonly", allow_windows_navigation=True) and windows_path.is_dir() else None

    # Resolve Windows user folders before looking in the active folder or the
    # broad file index. For example, "videos" must mean the user's Videos
    # library, not a third-party package directory.
    known_user_folders = {
        "desktop": "desktop",
        "downloads": "downloads",
        "documents": "documents",
        "pictures": "pictures",
        "photos": "photos",
        "videos": "videos",
        "music": "music",
        "home": "home",
        "user folder": "user folder",
    }
    known_folder_name = known_user_folders.get(query_clean.lower())
    if known_folder_name is not None:
        known_path = KNOWN_FOLDERS[known_folder_name]
        return known_path if known_path.is_dir() else None

    # --------------------------------------------------------
    # 0a. Search active folder hierarchy (nested subfolders, siblings, parent)
    # --------------------------------------------------------
    active = get_active_folder()
    if active:
        hier_hit = find_folder_in_hierarchy(query_clean, active)
        if hier_hit and os.path.isdir(hier_hit):
            _jprint(f"[INDEX] Hierarchy folder match -> {hier_hit}")
            return hier_hit

    # --------------------------------------------------------
    # 0. Direct path (explicit path given by user)
    # --------------------------------------------------------

    for q in (raw_query, query_clean):
        possible_path = Path(q).expanduser()
        try:
            if possible_path.exists() and possible_path.is_dir():
                return possible_path
        except Exception:
            pass

        if re.match(r"^[A-Za-z]:[\\/]", q):
            p = Path(q)
            try:
                if p.exists() and p.is_dir():
                    return p
            except Exception:
                pass

        if q.startswith("\\\\"):
            try:
                if os.path.exists(q) and os.path.isdir(q):
                    return q
            except Exception:
                pass

        home_path = Path.home() / q
        try:
            if home_path.exists() and home_path.is_dir():
                return home_path
        except Exception:
            pass

    # --------------------------------------------------------
    # 1. Persistent index search (fast)
    # --------------------------------------------------------

    if _file_index is not None:
        hits = _file_index.search(
            query_clean,
            prefer_dirs=True,
            limit=25,
        )
        if hits:
            # Rank 1: Exact folder name match
            target_lower = query_clean.lower()
            for fp in hits:
                if os.path.isdir(fp) and os.path.basename(fp).lower() == target_lower:
                    _jprint(f"[INDEX] Exact folder match -> {fp}")
                    return fp

            # Rank 2: Folder name starts with query
            for fp in hits:
                if os.path.isdir(fp) and os.path.basename(fp).lower().startswith(target_lower):
                    _jprint(f"[INDEX] Prefix folder match -> {fp}")
                    return fp

            # Rank 3: Query is a word/substring in folder name
            for fp in hits:
                if os.path.isdir(fp) and target_lower in os.path.basename(fp).lower():
                    _jprint(f"[INDEX] Substring folder match -> {fp}")
                    return fp

            # Rank 4: Any directory hit from index
            for fp in hits:
                if os.path.isdir(fp):
                    _jprint(f"[INDEX] Folder hit -> {fp}")
                    return fp

    # --------------------------------------------------------
    # 2. Fallback: shallow iterdir scan of common folders
    #    (used when index is empty on very first run)
    # --------------------------------------------------------

    roots = get_search_roots()
    target = folder_name.lower()

    for root in roots:
        try:
            for item in root.iterdir():
                if not item.is_dir():
                    continue
                if item.name.lower() == target:
                    _jprint(f"[INDEX] Shallow folder match -> {item}")
                    return item
        except (PermissionError, OSError):
            continue

    return None


def find_subfolder(parent_path, child_name):
    """
    Search for a subfolder named *child_name* inside *parent_path*.

    Uses _match_folder_score to support bare numbers, half-names, abbreviations,
    and recursive subfolder search down to depth 3.

    Returns the first matching Path, or None.
    """
    if not parent_path or _is_ignored_search_path(parent_path) or not is_safe_path(str(parent_path), "search") or not child_name:
        return None

    parent = Path(str(parent_path))
    if not parent.is_dir():
        return None

    target = child_name.strip()
    target = re.sub(r"(?:'s|s)?\s+(?:folder|directory)$", "", target, flags=re.IGNORECASE).strip()
    if not target:
        return None

    # Search immediate children and nested subfolders
    candidates = []
    try:
        for root, dirs, _ in os.walk(parent):
            if not is_safe_path(root, "search"):
                dirs.clear()
                continue
            dirs[:] = [d for d in dirs if is_safe_path(os.path.join(root, d), "search")]
            _prune_search_dirs(dirs)
            for d in dirs:
                score = _match_folder_score(target, d)
                if score > 0:
                    full = os.path.join(root, d)
                    depth = len(Path(full).relative_to(parent).parts)
                    candidates.append((score - depth * 5, Path(full)))
    except Exception:
        pass

    if candidates:
        candidates.sort(key=lambda x: -x[0])
        return candidates[0][1]

    return None


# ============================================================
# OPEN PC FOLDER
# ============================================================

def handle_pc_folder_command(command):
    cmd = clean_command(command)

    # ----------------------------------------------------------
    # FOLDER HIERARCHY NAVIGATION: "go back", "parent folder", "up one folder"
    # ----------------------------------------------------------
    if cmd.lower() in {
        "go back", "parent folder", "go to parent folder", "open parent folder",
        "go up", "up one folder", "go to parent", "back to parent"
    }:
        active = get_active_folder()
        if active:
            parent = Path(active).parent
            if parent and parent != Path(active) and parent.exists() and parent.is_dir():
                parent_str = str(parent)
                if not open_path_safe(parent_str):
                    return f"I couldn't open the parent folder {parent.name}."
                set_active_folder(parent_str)
                from core.session_memory import session
                session.set_current_app(parent.name)
                return f"Opening parent folder {parent.name}."

    # ----------------------------------------------------------
    # NESTED FOLDER: "open X in/inside Y folder"
    # E.g. "open season 1 folder in black clover folder"
    #      "open season 1 in black clover"
    #      "open season 1 inside the black clover folder"
    # ----------------------------------------------------------
    nested_patterns = [
        # "open season 1 folder in black clover folder"
        r"^(?:open|go to|goto)\s+(?:the\s+|my\s+)?(.+?)\s+(?:folder\s+)?(?:in|inside|within|under)\s+(?:the\s+|my\s+)?(.+?)(?:\s+folder|\s+directory)?$",
    ]

    for pattern in nested_patterns:
        match = re.match(pattern, cmd, flags=re.IGNORECASE)
        if not match:
            continue

        child_raw = match.group(1).strip()
        parent_raw = match.group(2).strip()

        # Clean trailing "folder" / "directory" from both
        child_name = re.sub(r"(?:'s|s)?\s+(?:folder|directory)$", "", child_raw, flags=re.IGNORECASE).strip()
        parent_name = re.sub(r"(?:'s|s)?\s+(?:folder|directory)$", "", parent_raw, flags=re.IGNORECASE).strip()

        if not child_name or not parent_name:
            continue

        # Skip if parent looks like a website/app, not a folder
        web_names = {
            "youtube", "google", "gmail", "github", "chatgpt",
            "reddit", "instagram", "facebook", "twitter", "x", "samsung",
            "linkedin", "amazon", "wikipedia", "netflix",
            "chrome", "google chrome", "brave", "brave browser",
            "edge", "microsoft edge",
        }
        if parent_name.lower() in web_names:
            continue

        # 1. Find the parent folder on PC
        parent_folder = find_pc_folder(parent_name)
        if not parent_folder or not os.path.isdir(str(parent_folder)):
            if "folder" in cmd.lower() or "directory" in cmd.lower():
                return f"I couldn't find the {parent_name} folder on your PC."
            continue

        # 2. Search for the child subfolder inside the parent
        child_folder = find_subfolder(parent_folder, child_name)
        if child_folder and os.path.isdir(str(child_folder)):
            child_str = str(child_folder)
            opened = open_path_safe(child_str)
            if not opened:
                return f"I couldn't open the {os.path.basename(child_str)} folder."
            set_active_folder(child_str)
            from core.session_memory import session
            session.set_current_app(os.path.basename(child_str))
            return f"Opening {os.path.basename(child_str)} folder in {os.path.basename(str(parent_folder))}."

        return f"I couldn't find a '{child_name}' folder inside {os.path.basename(str(parent_folder))}."

    # ----------------------------------------------------------
    # STANDARD SINGLE FOLDER OPEN (existing logic)
    # ----------------------------------------------------------
    patterns = [
        r"^(?:open|go to|goto) (?:the |my )?(.+?)(?:'s|s)? folder$",
        r"^(?:open|go to|goto) folder (?:of |named )?(.+)$",
        # Generic folder open: "open Black Clover", "go to Black Clover", "open Downloads"
        r"^(?:open|go to|goto) (?:the |my )?(.+)$",
    ]

    for pattern in patterns:
        match = re.match(pattern, cmd, flags=re.IGNORECASE)
        if not match:
            continue

        raw_name = match.group(1).strip()
        folder_name = re.sub(r"(?:'s|s)?\s+(?:folder|directory)$", "", raw_name, flags=re.IGNORECASE).strip()

        # Don't treat a known site as a folder unless the user explicitly
        # requested a folder/directory with that name.
        if (
            folder_name.lower() in WEBSITE_URLS
            and not re.search(r"\b(?:folder|directory)\b", cmd, flags=re.IGNORECASE)
        ):
            return None

        # Check for folder on PC
        folder = find_pc_folder(folder_name)
        if folder and os.path.isdir(str(folder)):
            folder_str = str(folder)
            opened = open_path_safe(folder_str)
            if not opened:
                return f"I couldn't open the {os.path.basename(folder_str)} folder."
            set_active_folder(folder_str)
            from core.session_memory import session
            session.set_current_app(os.path.basename(folder_str))
            return f"Opening the {os.path.basename(folder_str)} folder."

        # Check if folder_name matches a file in the active folder hierarchy or index
        # (e.g. user said "open 163", "open asta", "open movie", "open wizard king", "open 1.ep 163")
        active = get_active_folder()
        is_file_target = bool(re.search(r"\.[a-zA-Z0-9]{1,8}$", folder_name)) or bool(re.match(r"^file\b", folder_name, re.I))
        file_hit = find_file_in_hierarchy(folder_name, active)
        if file_hit and os.path.isfile(file_hit):
            ext = os.path.splitext(file_hit)[1].lower()
            if ext in _MEDIA_EXT:
                if _open_media_file(file_hit):
                    from core.session_memory import session
                    session.last_played_media = file_hit
                    set_active_folder(os.path.dirname(file_hit))
                    return f"Playing {os.path.basename(file_hit)}."
            else:
                if open_path_safe(file_hit):
                    from core.session_memory import session
                    session.last_opened_file = file_hit
                    set_active_folder(os.path.dirname(file_hit))
                    return f"Opening {os.path.basename(file_hit)}."
        elif is_file_target:
            if is_desktop_path(active):
                return f"I couldn't find {folder_name} on the desktop. Open the folder first."
            if not active:
                return "Open the folder first; I only open files from an open folder."
            return f"I couldn't find {folder_name} in the current folder."

        # If user explicitly said "folder", inform them it was not found
        if "folder" in cmd.lower() or "directory" in cmd.lower():
            return f"I couldn't find the {folder_name} folder on your PC."

    return None


# ============================================================
# OPEN PC PATH
# ============================================================

def handle_pc_path_command(command):

    command = clean_command(command)

    patterns = [

        r"^open (.+[\\/].+)$",

        r"^open my (.+[\\/].+)$",

        r"^go to (.+[\\/].+)$",

    ]

    for pattern in patterns:

        match = re.match(
            pattern,
            command,
            flags=re.IGNORECASE,
        )

        if not match:
            continue

        path_text = match.group(1).strip()

        # Remove trailing "folder"
        path_text = re.sub(
            r"\s+folder$",
            "",
            path_text,
            flags=re.IGNORECASE,
        )

        path = Path(path_text).expanduser()

        if path.exists():

            path_str = str(path)
            if not open_path_safe(path_str):
                return f"I couldn't open {path.name}."
            if os.path.isdir(path_str):
                set_active_folder(path_str)
            return f"Opening {path.name}."

        # Try from home directory
        home_path = Path.home() / path_text

        if home_path.exists():

            hp_str = str(home_path)
            if not open_path_safe(hp_str):
                return f"I couldn't open {home_path.name}."
            if os.path.isdir(hp_str):
                set_active_folder(hp_str)
            return f"Opening {home_path.name}."

    return None


# ============================================================
# KNOWN WINDOWS FOLDERS
# ============================================================

def handle_folder_command(command):

    command = clean_command(command).lower()

    folders = {name: KNOWN_FOLDERS[name] for name in (
        "desktop", "downloads", "documents", "pictures", "photos", "videos", "music", "home", "user folder"
    )}

    for name, path in folders.items():

        commands = [

            f"open {name}",

            f"open my {name}",

            f"go to {name}",

            f"open {name} folder",

            f"open my {name} folder",

        ]

        if command in commands:

            if path.exists():

                open_path(path)
                set_active_folder(str(path))
                return f"Opening {name}."

    return None


# ============================================================
# APPS
# ============================================================

def handle_app_command(command):
    cmd = clean_command(command)
    cmd_lower = cmd.lower()
    if cmd_lower in {"refresh apps", "refresh app", "rescan apps", "rescan app", "reload apps", "reload app"}:
        from tools.app_tools import refresh_apps
        refresh_apps()
        return "App database refreshed."

    match = re.match(r"^(?:open|launch|start|run)\s+(.+)$", cmd, flags=re.IGNORECASE)
    if not match:
        return None

    raw_target = match.group(1).strip()
    target = raw_target.lower()

    # Don't capture explicit website, folder, or file commands
    if target.endswith((" website", " site", " folder", " directory")):
        return None

    # Don't capture known websites as app-launch attempts.
    if target.removesuffix(" website").removesuffix(" site").strip() in WEBSITE_URLS:
        return None

    # Don't capture files with common file extensions
    if re.search(r"\.(?:txt|pdf|docx?|xlsx?|pptx?|csv|py|js|html|css|json|xml|zip|rar|7z|mp3|wav|flac|mp4|mkv|avi|mov|jpg|jpeg|png)$", target):
        return None
    # These are context-sensitive file/folder requests, not applications.
    # Let the active-folder handler resolve them before app discovery.
    if target in {"file", "the file", "that file", "this file", "folder", "the folder", "that folder", "this folder"}:
        return None

    from tools.window_tools import open_app, APP_PROCESSES, APP_OPEN_COMMANDS, find_any_window
    from tools.app_tools import find_app

    target_clean = target.removesuffix(" app").removesuffix(" window").strip()

    # If it is a known app, open window, or installed Windows app:
    if (
        target_clean in APP_PROCESSES
        or target_clean in APP_OPEN_COMMANDS
        or find_any_window(target_clean)
        or find_app(target_clean)
    ):
        return open_app(target_clean)

    return None

   # ========================================================
    # 3. KNOWN WINDOWS FOLDERS
    # ========================================================

    result = handle_folder_command(
        command
    )

    if result:
        return result

    # ========================================================
    # 3.5 UNIVERSAL FILE / FOLDER / MEDIA HANDLER
    # ========================================================

    result = handle_file_system_command(
        command
    )

    if result:
        return result


# ============================================================
# TIME / DATE
# ============================================================

def handle_time_command(command):

    command = clean_command(command).lower()

    now = datetime.now()

    if command in [
        "what time is it",
        "what is the time",
        "what's the time",
        "tell me the time",
        "tell me what time it is",
        "time",
        "current time",
        "time now",
    ]:

        return f"It is {now.strftime('%I:%M %p')}."

    if command in [
        "what is today's date",
        "what is the date",
        "what's the date",
        "what is the date today",
        "what's today's date",
        "tell me the date",
        "today's date",
        "todays date",
        "date",
        "current date",
    ]:

        return (
            f"Today is "
            f"{now.strftime('%d %B %Y')}."
        )

    if command in [
        "what day is it",
        "what day is today",
        "what day is it today",
        "what's the day today",
        "today's day",
        "day",
    ]:

        return (
            f"Today is "
            f"{now.strftime('%A')}."
        )

    return None


# ============================================================
# YOUTUBE
# ============================================================

def _youtube_browser_target(browser=None):
    """Prefer an explicitly named browser, then the current Jarvis/browser context."""
    if browser and browser.strip().lower() in _BROWSER_COMMANDS:
        return browser.strip().lower()
    current = (session.current_app or session.last_opened_app or "").strip().lower()
    if current in _BROWSER_COMMANDS:
        return current
    try:
        from tools.window_tools import get_foreground_window, get_window_process_name
        process = get_window_process_name(get_foreground_window())
        return {
            "chrome.exe": "chrome",
            "brave.exe": "brave",
            "msedge.exe": "edge",
        }.get(process)
    except Exception:
        return None

def _fetch_youtube_video_urls(query: str, limit: int = 5) -> list[str]:
    """Find direct URLs for the first YouTube results for a query."""
    clean_query = query.strip()
    if not clean_query:
        return []

    # 1. Try yt-dlp via python module (highest precision matching)
    try:
        ytdlp_cmd = [sys.executable, "-m", "yt_dlp"]
        result = subprocess.run(
            [
                *ytdlp_cmd,
                "--flat-playlist",
                "--playlist-end",
                str(max(1, limit)),
                "--print",
                "https://www.youtube.com/watch?v=%(id)s",
                f"ytsearch{max(1, limit)}:{clean_query}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            urls = []
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith(("https://www.youtube.com/", "https://youtu.be/")):
                    if line not in urls:
                        urls.append(line)
            if urls:
                return urls
    except Exception as e:
        print(f"⚠️ yt-dlp query error: {e}")

    # 2. Fast HTTP fallback via httpx with desktop browser headers
    try:
        import httpx
        url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(clean_query)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = httpx.get(url, headers=headers, timeout=6.0, follow_redirects=True)
        video_ids = list(dict.fromkeys(re.findall(r"/watch\?v=([a-zA-Z0-9_-]{11})", resp.text)))
        if video_ids:
            return [f"https://www.youtube.com/watch?v={video_id}" for video_id in video_ids[:max(1, limit)]]
    except Exception as e:
        print(f"⚠️ YouTube web scrape error: {e}")

    return []


def _fetch_first_youtube_video_url(query: str) -> Optional[str]:
    urls = _fetch_youtube_video_urls(query, limit=1)
    if not urls:
        return None
    url = urls[0]
    return url + ("&autoplay=1" if "?" in url else "?autoplay=1")


def play_youtube(query, browser=None, *, new_tab=None):
    """
    Play YouTube media query:
    Always clicks the first real video result (skipping ads, Shorts, playlists, channels, etc.).
    If no query: resume playback in active tab.
    """
    query = (query or "").strip()
    from tools.browser_controller import play_or_resume_youtube
    _ok, msg = play_or_resume_youtube(query)
    session.set_current_app(browser or "chrome")
    session.last_opened_website = "youtube"
    if query:
        session.youtube_search_query = query
        session.last_played_media = query
    return msg


def play_youtube_action(query, browser=None, *, new_tab=None):
    """Execute YouTube playback/search returning explicit (success, message)."""
    msg = play_youtube(query, browser=browser, new_tab=new_tab)
    if not msg:
        return False, f"I couldn't open YouTube search for {query}."
    failed = any(err in msg.lower() for err in ("couldn't", "could not", "failed", "can't"))
    return not failed, msg


def search_youtube(query, browser=None, *, same_tab=None):
    """Show YouTube results without autoplaying; ordinal voice commands play them."""
    query = str(query or "").strip()
    if not query:
        return None
    browser = _youtube_browser_target(browser)
    active = get_active_website_context()
    if same_tab is None:
        same_tab = bool(active and active[1] == "youtube" and (not browser or _canonical_browser_name(browser) == active[0]))

    session.youtube_search_query = query
    session.youtube_search_results = []
    url = WEBSITE_SEARCH_URLS["youtube"].format(query=urllib.parse.quote_plus(query))
    if not open_url(url, browser=browser, new_tab=not same_tab):
        return f"I couldn't search YouTube for {query}."
    session.last_opened_website = "youtube"
    if browser:
        session.set_current_app(browser)
    return f"Searching YouTube for {query}."


def _play_current_youtube_video(browser=None):
    """Resume the current YouTube player, or open YouTube if no tab exists."""
    from tools.window_tools import find_any_window, force_foreground_window, get_window_process_name

    browser = _youtube_browser_target(browser)
    hwnd = find_any_window("youtube")
    expected_process = _BROWSER_PROCESS_NAMES.get(browser or "")
    if hwnd and expected_process and get_window_process_name(hwnd) != expected_process:
        hwnd = None
    if hwnd and force_foreground_window(hwnd):
        # YouTube's K shortcut toggles the focused player's play state.
        if not press_key(0x4B):
            return "I couldn't send the play command to YouTube."
        session.set_current_app("youtube")
        session.last_opened_website = "youtube"
        return "Playing the current video on YouTube."

    if not open_url("https://www.youtube.com/", browser=browser):
        return "I couldn't open YouTube."
    session.set_current_app(browser or "chrome")
    return "Opening YouTube. Tell me the video title if you want a specific video."


def _play_visible_browser_video() -> str | None:
    """Play a video visible in the active browser page, when one is present."""
    try:
        from tools.browser_controller import play_visible_video
        ok, message = play_visible_video()
        if ok:
            session.set_current_app("browser")
            session.last_played_media = "visible browser video"
            return message
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        print(f"[Browser] Visible video inspection failed: {error}")
    return None


def _open_visible_browser_item(item_kind: str = "link") -> str | None:
    """Open a visible browser link/file/folder when the browser is active."""
    try:
        from tools.browser_controller import open_visible_link
        ok, message = open_visible_link(item_kind)
        if ok:
            session.set_current_app("browser")
            session.last_opened_website = "browser"
            return message
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        print(f"[Browser] Visible {item_kind} inspection failed: {error}")
    return None


def handle_casual_phrase(command: str) -> str | None:
    """
    Handle casual conversational phrases locally before any AI router call:
    - The explicit "sleep" command -> "__GOODBYE_EXIT__" for clean shutdown
    - Thanks / okay / hello style phrases -> short local reply
    """
    clean = re.sub(r"[,:;!?]+", "", (command or "").strip().lower()).strip()
    if not clean:
        return None

    # Shutdown is intentionally narrow: conversational sign-offs never
    # terminate the assistant.
    if clean == "sleep":
        return "__GOODBYE_EXIT__"

    # 2. Thanks phrases
    thanks_patterns = [
        r"^(?:thank\s+you(?:\s+so\s+much|\s+very\s+much)?|thanks(?:\s+a\s+lot|\s+a\s+bunch)?|appreciate\s+it|many\s+thanks)$"
    ]
    if any(re.match(p, clean) for p in thanks_patterns):
        return "You're very welcome, sir."

    # 3. Okay / acknowledgement phrases
    okay_patterns = [
        r"^(?:ok|okay|got\s+it|sounds\s+good|alright|all\s+right|understood|cool|fine|perfect|great|nice)$"
    ]
    if any(re.match(p, clean) for p in okay_patterns):
        return "Standing by, sir."

    # 4. Hello / greeting phrases
    hello_patterns = [
        r"^(?:hello|hi|hey|good\s+morning|good\s+afternoon|good\s+evening)(?:\s+jarvis)?$",
        r"^(?:how\s+are\s+you(?:\s+doing)?|how's\s+it\s+going)(?:\s+today)?$",
    ]
    if any(re.match(p, clean) for p in hello_patterns):
        return "Hello, sir. How can I assist you today?"

    return None


def handle_remember_alias_command(command: str) -> str | None:
    """
    Handle user correction commands: 'remember that <misheard> means <canonical>'
    e.g. 'remember that cloud means Claude'
    """
    m = re.match(r"^remember\s+that\s+(.+?)\s+means\s+(.+)$", (command or "").strip(), re.IGNORECASE)
    if m:
        misheard = m.group(1).strip().strip("'\"")
        canonical = m.group(2).strip().strip("'\"")
        from tools.app_tools import add_alias
        if add_alias(misheard, canonical):
            try:
                from voice.stt_cleanup import reload_corrections
                reload_corrections()
            except Exception:
                pass
            return f"I'll remember that {misheard} means {canonical}."
        return "I couldn't save that alias."
    return None


def handle_youtube_command(command):

    command = clean_command(command)

    # Resolve spoken result positions against the most recent YouTube search or active YouTube tab.
    ordinal = re.match(
        r"^(?:play|watch|open|click)?(?:\s+(?:the|on))?\s*(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th)\s+(?:video|result|one)(?:\s+(?:in|on|from)\s+(?:youtube|you tube|yt))?$",
        command,
        re.IGNORECASE,
    )
    if ordinal:
        positions = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "fifth": 5, "5th": 5}
        position = positions[ordinal.group(1).lower()]
        browser = _youtube_browser_target()

        # "Play first video" prioritizes the first video currently visible on
        # screen, including a scrolled YouTube results page.
        visible_result = _play_visible_browser_video()
        if visible_result:
            return visible_result

        # 1. If CDP or active tab is on YouTube search page, try clicking directly on the page
        try:
            from tools.browser_controller import click_link_by_ordinal
            ok, msg = click_link_by_ordinal(position)
            if ok:
                session.set_current_app(browser or "chrome")
                session.last_opened_website = "youtube"
                return f"Playing YouTube result {position}."
        except Exception:
            pass

        # Extract query from active YouTube window if not already set in session
        if not session.youtube_search_query:
            try:
                from tools.window_tools import get_foreground_window, get_window_title
                hwnd = get_foreground_window()
                title = get_window_title(hwnd)
                if "youtube" in title.lower():
                    clean_t = re.sub(r"^\(\d+\)\s*", "", title)
                    m_title = re.match(r"^(.+?)\s+-\s+YouTube", clean_t, re.IGNORECASE)
                    if m_title:
                        session.youtube_search_query = m_title.group(1).strip()
            except Exception:
                pass

        # 2. Check cached or freshly fetched search results
        results = list(session.youtube_search_results)
        if len(results) < position and session.youtube_search_query:
            results = _fetch_youtube_video_urls(session.youtube_search_query, limit=max(8, position))
            session.youtube_search_results = results
        if len(results) >= position:
            active = get_active_website_context()
            same_tab = bool(active and active[1] == "youtube" and (not browser or _canonical_browser_name(browser) == active[0]))
            url = results[position - 1]
            url += ("&autoplay=1" if "?" in url else "?autoplay=1")
            # CDP click already failed above, so use a fresh tab for the
            # browser fallback instead of depending on stale active-tab state.
            if open_url(url, browser=browser, new_tab=True):
                session.set_current_app(browser or "browser")
                session.last_opened_website = "youtube"
                session.last_played_media = f"{session.youtube_search_query} result {position}"
                return f"Playing YouTube result {position}."
            return f"I found YouTube result {position}, but couldn't open it."

        # 3. Direct fetch fallback if we have a query
        if session.youtube_search_query:
            direct_url = _fetch_first_youtube_video_url(session.youtube_search_query)
            if direct_url and open_url(direct_url, browser=browser, new_tab=True):
                session.set_current_app(browser or "chrome")
                session.last_opened_website = "youtube"
                return f"Playing YouTube result {position}."

        # 4. If YouTube tab is active, play or resume
        from tools.browser_controller import play_or_resume_youtube
        ok, msg = play_or_resume_youtube("")
        if ok:
            return f"Playing YouTube result {position}."

        return "Search for a YouTube video first, then tell me which result to play."

    # "Open Chrome in YouTube and play ..." is one intent: route the selected
    # video URL through that browser rather than the system default browser.
    browser_pattern = (
        r"^(?:open|launch|start)\s+(chrome|google chrome|brave(?: browser)?|"
        r"edge|microsoft edge)\s+(?:in|to)\s+(?:youtube|you tube|yt)(?:\s+app)?"
        r"(?:\s+(?:and\s+)?(?:play|watch)\s+(.+))?$"
    )
    browser_request = re.match(browser_pattern, command, flags=re.IGNORECASE)
    if browser_request:
        browser = browser_request.group(1).lower().strip()
        query = (browser_request.group(2) or "").strip()
        if query and query.lower() not in {"video", "the video", "current video", "the current video"}:
            return play_youtube(query, browser=browser)
        if not open_url("https://www.youtube.com/", browser=browser):
            return f"I couldn't open YouTube in {browser.title()}."
        session.set_current_app(browser)
        session.last_opened_website = "youtube"
        if query:
            return _play_current_youtube_video(browser=browser)
        return f"Opening YouTube in {browser.title()}."

    # Accept YouTube's common spoken abbreviation and optional explicit browser.
    browser = None
    browser_suffix = re.search(
        r"\s+(?:in|using|with)\s+(chrome|google chrome|brave(?: browser)?|edge|microsoft edge)$",
        command,
        flags=re.IGNORECASE,
    )
    if browser_suffix:
        browser = browser_suffix.group(1).lower().strip()
        command = command[:browser_suffix.start()].strip()
    else:
        browser = _youtube_browser_target(browser)

    cmd_lower = command.strip().lower()
    if cmd_lower in {"bad apple", "play bad apple"}:
        return play_youtube("bad apple", browser=browser)
    if cmd_lower in {"search bad apple", "find bad apple", "search for bad apple"}:
        return search_youtube("bad apple", browser=browser or "chrome")

    if cmd_lower in {"play", "resume", "resume video", "resume music", "resume playback"}:
        active = get_active_website_context()
        if (active and active[1] == "youtube") or session.last_opened_website == "youtube":
            return play_youtube("", browser=browser)
        return None

    patterns = [
        r"^(?:play|watch) (.+?) (?:on|in|from) (?:youtube|you tube|yt)(?: app)?$",
        r"^youtube play (.+)$",
    ]

    for pattern in patterns:
        match = re.match(
            pattern,
            command,
            flags=re.IGNORECASE,
        )
        if match:
            query = match.group(1).strip()
            if query.lower() in {"video", "the video", "current video", "the current video"}:
                return _play_current_youtube_video(browser=browser)
            if query.lower() in {"music", "song", "video", "playback", "the music", "the song", "it", "that", "this"}:
                return play_youtube("", browser=browser)
            return play_youtube(query, browser=browser)

    search_patterns = [
        r"^(?:search|find)\s+(?:for\s+)?(?:video\s+(?:on|in)\s+(?:youtube|you tube|yt)\s+|video\s+)?(.+?)\s+(?:on|in)\s+(?:youtube|you tube|yt)(?:\s+app)?$",
        r"^(?:search|find)\s+(?:video\s+)?(?:on|in)\s+(?:youtube|you tube|yt)\s+(?:for\s+)?(.+)$",
        r"^(?:search|find)\s+(?:youtube|you tube|yt)\s+(?:for\s+)?(?:video\s+)?(.+)$",
        r"^(?:youtube|you tube|yt)\s+(?:search|find)\s+(?:for\s+)?(?:video\s+)?(.+)$",
    ]

    for pattern in search_patterns:
        match = re.match(
            pattern,
            command,
            flags=re.IGNORECASE,
        )
        if match:
            query = match.group(1).strip()
            return search_youtube(query, browser=browser or "chrome")

    # A common voice form omits "search" and just says a title followed by
    # "in YouTube" (for example, "KGF trailer in YouTube").
    title_search = re.match(
        r"^(.+?)\s+(?:on|in)\s+(?:youtube|you tube|yt)(?:\s+app)?$",
        command,
        flags=re.IGNORECASE,
    )
    if title_search:
        return search_youtube(title_search.group(1).strip(), browser=browser or "chrome")

    if command.lower() in [
        "open youtube",
        "open youtube app",
        "start youtube",
        "start youtube app",
        "go to youtube",
        "go to youtube app",
        "open youtube website",
    ]:
        browser = _youtube_browser_target(browser)
        if not open_url("https://www.youtube.com/", browser=browser):
            return "I couldn't open YouTube."
        session.last_opened_website = "youtube"
        if browser:
            session.set_current_app(browser)
        return "Opening YouTube."

    return None


def handle_browser_control_command(command):
    """Run direct browser tab, link, scroll, and history controls."""
    cmd = clean_command(command).lower()

    browser_expr = r"(?:chrome|google chrome|brave(?: browser)?|edge|microsoft edge|firefox)"
    browser_suffix = re.search(rf"\s+(?:in|on)\s+({browser_expr})$", cmd)
    browser_name = browser_suffix.group(1) if browser_suffix else None
    tab_command = cmd[:browser_suffix.start()].strip() if browser_suffix else cmd

    previous_tab_phrases = {
        "previous tab", "switch previous tab", "switch to previous tab",
        "go to previous tab", "previous browser tab", "switch to the previous tab",
        "switch to previous browser tab", "previous tab please",
    }
    next_tab_phrases = {
        "next tab", "switch next tab", "switch to next tab", "go to next tab",
        "next browser tab", "switch to the next tab", "switch to next browser tab",
    }
    new_tab_phrases = {
        "new tab", "open new tab", "open a new tab", "add a new tab",
        "add new tab", "create a new tab", "create new tab",
    }

    tab_action = (
        "previous" if tab_command in previous_tab_phrases
        else "next" if tab_command in next_tab_phrases
        else "new" if tab_command in new_tab_phrases
        else None
    )
    if tab_action:
        try:
            from tools.window_tools import (
                find_any_window,
                force_foreground_window,
                get_foreground_window,
                get_window_process_name,
            )

            browser_processes = {"chrome.exe", "msedge.exe", "brave.exe", "firefox.exe"}
            if browser_name:
                hwnd = find_any_window(browser_name)
            else:
                hwnd = get_foreground_window()
                process_name = get_window_process_name(hwnd).lower() if hwnd else ""
                if process_name not in browser_processes:
                    hwnd = None
                if not hwnd:
                    from core.session_memory import session
                    browser_aliases = {
                        "chrome": "chrome", "google chrome": "chrome",
                        "brave": "brave", "brave browser": "brave",
                        "edge": "edge", "microsoft edge": "edge",
                        "firefox": "firefox",
                    }
                    for recent_app in (session.current_app, session.last_opened_app):
                        alias = browser_aliases.get(str(recent_app or "").casefold())
                        if alias:
                            hwnd = find_any_window(alias)
                            if hwnd:
                                break

            # Window lookup must resolve to a real, already-running browser
            # process before sending tab keystrokes. This handler never starts
            # a browser process.
            process_name = get_window_process_name(hwnd).lower() if hwnd else ""
            if process_name not in browser_processes:
                hwnd = None

            if not hwnd:
                if browser_name:
                    return f"I couldn't find an open {browser_name.title()} window; I won't open a new browser."
                return "I couldn't find an open browser window; I won't open a new browser."
            if not force_foreground_window(hwnd):
                return "I couldn't bring the browser to the front."

            user32 = ctypes.windll.user32
            key_up = 0x0002
            keys = {
                "previous": (0x11, 0x10, 0x09),  # Ctrl+Shift+Tab
                "next": (0x11, 0x09),           # Ctrl+Tab
                "new": (0x11, 0x54),            # Ctrl+T
            }[tab_action]
            for key in keys:
                user32.keybd_event(key, 0, 0, 0)
            time.sleep(0.04)
            for key in reversed(keys):
                user32.keybd_event(key, 0, key_up, 0)

            target = browser_name.title() if browser_name else "the browser"
            messages = {
                "previous": f"Switched to the previous tab in {target}.",
                "next": f"Switched to the next tab in {target}.",
                "new": f"Opened a new tab in {target}.",
            }
            return messages[tab_action]
        except (AttributeError, OSError, RuntimeError) as error:
            print(f"[BrowserControl] Could not execute tab command: {error}")
            return "I couldn't control the browser tabs."

    # 1. Check browser link clicking ("open first link", "click 2nd result") and scrolling
    try:
        from tools.browser_controller import handle_browser_voice_command
        browser_res = handle_browser_voice_command(cmd)
        if browser_res is not None:
            return browser_res
    except Exception as e:
        print(f"[BrowserControl] browser_controller error: {e}")

    # 2. History back
    browser_expr = r"(?:chrome|google chrome|brave(?: browser)?|edge|microsoft edge)"
    back = re.match(rf"^(?:go\s+)?back(?:\s+(?:in|on)\s+({browser_expr}))?$", cmd)
    if not back:
        return None

    try:
        from tools.window_tools import find_any_window, force_foreground_window, get_foreground_window
        browser_name = back.group(1)
        hwnd = find_any_window(browser_name) if browser_name else get_foreground_window()
        if not hwnd:
            return f"I couldn't find {browser_name.title()} open." if browser_name else "I couldn't find an active window."
        if not force_foreground_window(hwnd):
            return f"I couldn't bring {browser_name.title()} to the front." if browser_name else "I couldn't bring the active window to the front."
        user32 = ctypes.windll.user32
        key_down, key_up = 0, 0x0002
        # Alt+Left is the browser's native previous-page shortcut.
        user32.keybd_event(0x12, 0, key_down, 0)
        user32.keybd_event(0x25, 0, key_down, 0)
        time.sleep(0.06)
        user32.keybd_event(0x25, 0, key_up, 0)
        user32.keybd_event(0x12, 0, key_up, 0)
        return f"Going back in {browser_name.title()}." if browser_name else "Going back."
    except (AttributeError, OSError, RuntimeError) as error:
        print(f"[BrowserControl] Could not execute command: {error}")
        return "I couldn't control the active browser window."


# ============================================================
# GOOGLE & SITE SEARCH
# ============================================================

def google_search(query):
    """
    Search using the site currently on:
    - YouTube if active tab is YouTube
    - Google if on Google or default
    Never passes a URL as a search query.
    Navigates within the existing tab.
    """
    from tools.browser_controller import build_site_search_url, clean_search_query, navigate_to_url

    clean_q = clean_search_query(query)
    if not clean_q:
        clean_q = str(query).strip()

    if not clean_q:
        return None

    final_url, clean_q, site = build_site_search_url(clean_q)

    # Validate that final URL starts with http:// or https://
    if not final_url.startswith(("http://", "https://")):
        final_url = f"https://{final_url.lstrip('/')}"

    print(f"🌐 Site search ({site}) → {clean_q} ({final_url})")

    # Navigate in the existing tab (same-site rule)
    navigate_to_url(final_url)

    if site == "youtube":
        session.youtube_search_query = clean_q
        session.last_opened_website = "youtube"
        return f"Searching YouTube for {clean_q}."

    session.last_opened_website = "google"
    return f"Searching Google for {clean_q}."


def handle_search_command(command):
    command = clean_command(command)

    # ========================================================
    # OPEN BROWSER + SEARCH
    # ========================================================

    match = re.match(
        r"^(?:open|go to)\s+(chrome|google chrome|brave|brave browser|edge|microsoft edge)\s+"
        r"(?:and\s+)?(?:search|google|look up)\s+(.+)$",
        command,
        flags=re.IGNORECASE,
    )

    if match:
        browser = match.group(1).lower().strip()
        raw_query = match.group(2).strip()
        from tools.browser_controller import clean_search_query
        query = clean_search_query(raw_query) or raw_query
        if not query:
            return None
        return google_search(query)

    # ========================================================
    # SITE-AWARE SEARCH (e.g. "search bad apple")
    # ========================================================

    patterns = [
        r"^search (.+)$",
        r"^google (.+)$",
        r"^search google for (.+)$",
        r"^look up (.+)$",
    ]

    for pattern in patterns:
        match = re.match(
            pattern,
            command,
            flags=re.IGNORECASE,
        )
        if match:
            raw_query = match.group(1).strip()
            if raw_query:
                return google_search(raw_query)

    return None

# ============================================================
# FIRST WEB RESULT
# ============================================================

def get_first_search_result(query):

    query = query.strip()

    if not query:
        return None

    search_url = (
        "https://html.duckduckgo.com/html/?q="
        + urllib.parse.quote_plus(query)
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/153.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:

        request = urllib.request.Request(
            search_url,
            headers=headers,
        )

        with urllib.request.urlopen(
            request,
            timeout=10,
        ) as response:

            html = response.read().decode(
                "utf-8",
                errors="ignore",
            )

        matches = re.findall(
            r'class=["\']result__a["\'][^>]*href=["\']([^"\']+)',
            html,
            flags=re.IGNORECASE,
        )

        for url in matches:

            url = urllib.parse.unquote(url)

            url = url.replace(
                "&amp;",
                "&",
            )

            if "uddg=" in url:

                parsed = urllib.parse.urlparse(
                    url
                )

                params = (
                    urllib.parse.parse_qs(
                        parsed.query
                    )
                )

                if "uddg" in params:

                    url = params["uddg"][0]

            if url.startswith(
                (
                    "http://",
                    "https://",
                )
            ):

                hostname = (
                    urllib.parse.urlparse(
                        url
                    ).hostname
                    or ""
                ).lower()

                if hostname.endswith(
                    "duckduckgo.com"
                ):
                    continue

                _jprint(
                    f"🎯 First web result → {url}"
                )

                return url

    except Exception as e:

        _jprint(
            f"⚠️ Web resolver failed: {e}"
        )

    return None


def open_first_search_result(query, browser=None):

    query = query.strip()

    if not query:
        return None

    _jprint(
        f"🔎 Web search → {query}"
    )

    url = get_first_search_result(
        query
    )

    if url:

        open_url(url, browser=browser)

        return (
            f"Opening the first result "
            f"for {query}."
        )

    return google_search(query)


# ============================================================
# OPEN WEBSITE
# ============================================================

def handle_open_website(command, browser=None):

    command = clean_command(command)

    # This is a browser-search request, not a website name.
    if re.match(
        r"^(?:open|go to)\s+(?:chrome|google chrome|brave|brave browser|edge|microsoft edge)\s+(?:and\s+)?(?:search|google|look up)\s+.+$",
        command,
        flags=re.IGNORECASE,
    ):
        return None

    # ChatGPT navigation is handled by handle_window_command
    # (which checks for existing windows first).  Skip here so
    # we don't bypass the window-first logic.
    from tools.window_tools import _is_chatgpt_navigation
    if _is_chatgpt_navigation(command.lower()):
        return None

    direct_name = clean_command(command).lower()
    if direct_name.startswith("open "):
        candidate = direct_name[5:].strip()
        direct_url, canonical_site = _website_destination_url(candidate)
        if direct_url:
            if not open_url(direct_url, browser=browser):
                return f"I couldn't open {candidate}."
            session.last_opened_website = canonical_site
            active = get_active_website_context()
            active_browser = browser or (active[0] if active else None)
            if active_browser:
                session.set_current_app(active_browser)
            return f"Opening {candidate}."

    patterns = [

        r"^open (.+?) official website$",

        r"^open (.+?) official site$",

        r"^open (.+?) web site$",

        r"^open (.+?) website$",

        r"^open (.+?) site$",

        r"^go to (.+)$",

        r"^visit (.+)$",

    ]

    for pattern in patterns:

        match = re.match(
            pattern,
            command,
            flags=re.IGNORECASE,
        )

        if match:

            query = match.group(1).strip()

            # Folders and local paths are NOT websites! Never do web searches for them.
            if re.search(r"\b(?:folder|directory)\b", query, flags=re.IGNORECASE) or "\\" in query or "/" in query:
                return None

            if query:
                normalized = query.lower().removesuffix(" website").removesuffix(" site").strip()
                direct_url, canonical_site = _website_destination_url(normalized)
                if direct_url:
                    if not open_url(direct_url, browser=browser):
                        return f"I couldn't open {query}."
                    session.last_opened_website = canonical_site
                    active = get_active_website_context()
                    active_browser = browser or (active[0] if active else None)
                    if active_browser:
                        session.set_current_app(active_browser)
                    return f"Opening {query}."
                # Unknown sites are deliberately resolved on the web, never on disk.
                session.last_search = query
                result = open_first_search_result(query, browser=browser)
                session.last_opened_website = query
                return result

    return None


# ============================================================
# VOLUME
# ============================================================

VK_VOLUME_UP = 0xAF
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_MUTE = 0xAD


def handle_volume_command(command):

    command = clean_command(command).lower()

    if command in [
        "volume up",
        "increase volume",
        "raise volume",
        "turn volume up",
        "turn the volume up",
        "make it louder",
        "louder",
    ]:

        press_key(VK_VOLUME_UP)

        return "Volume increased."

    if command in [
        "volume down",
        "decrease volume",
        "lower volume",
        "turn volume down",
        "turn the volume down",
        "make it quieter",
        "quieter",
    ]:

        press_key(VK_VOLUME_DOWN)

        return "Volume decreased."

    if command in [
        "mute",
        "mute volume",
        "turn volume off",
    ]:

        press_key(VK_VOLUME_MUTE)

        return "Volume muted."

    if command in [
        "unmute",
        "turn volume on",
    ]:

        press_key(VK_VOLUME_MUTE)

        return "Volume toggled."

    return None


# ============================================================
# MEDIA
# ============================================================
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_MEDIA_STOP = 0xB2
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1


def handle_media_command(command):

    command = clean_command(command).lower()
    command = re.sub(
        r"^(play|pause|resume|stop) the (music|song|video|playback)$",
        r"\1 \2",
        command,
    )

    if command in [
        "stop music",
        "stop song",
        "stop video",
        "stop playing",
        "stop playback",
        "stop media",
    ]:
        if not press_key(VK_MEDIA_STOP):
            return "I couldn't send the media stop command."
        return "Media playback stopped."

    if command in [
        "play",
        "pause",
        "pause music",
        "pause video",
        "pause song",
        "pause playback",
        "play music",
        "play song",
        "play video",
        "playback",
        "resume",
        "resume music",
        "resume video",
        "resume song",
        "resume playback",
        "toggle playback",
        "play pause",
        "toggle play",
    ]:

        if not press_key(VK_MEDIA_PLAY_PAUSE):
            return "I couldn't send the media play/pause command."

        return (
            "Media playback toggled."
        )

    if command in [
        "next",
        "next song",
        "next track",
        "skip",
    ]:

        if not press_key(VK_MEDIA_NEXT_TRACK):
            return "I couldn't skip to the next track."

        return (
            "Skipping to the next track."
        )

    if command in [
        "previous",
        "previous song",
        "previous track",
    ]:

        if not press_key(VK_MEDIA_PREV_TRACK):
            return "I couldn't go to the previous track."

        return (
            "Going to the previous track."
        )

    return None


# ============================================================
# SCREENSHOT
# ============================================================

def handle_screenshot(command):

    command = clean_command(command).lower()

    if command in [
        "take screenshot",
        "take a screenshot",
        "screenshot",
        "capture screen",
        "capture screenshot",
        "screen capture",
        "capture my screen",
        "take a picture of the screen",
    ]:

        try:

            # Windows + Shift + S

            ctypes.windll.user32.keybd_event(
                0x5B,
                0,
                0,
                0,
            )

            ctypes.windll.user32.keybd_event(
                0x10,
                0,
                0,
                0,
            )

            ctypes.windll.user32.keybd_event(
                0x53,
                0,
                0,
                0,
            )

            ctypes.windll.user32.keybd_event(
                0x53,
                0,
                2,
                0,
            )

            ctypes.windll.user32.keybd_event(
                0x10,
                0,
                2,
                0,
            )

            ctypes.windll.user32.keybd_event(
                0x5B,
                0,
                2,
                0,
            )

            return (
                "Screenshot tool opened."
            )

        except Exception as e:

            print(
                f"⚠️ Screenshot failed: {e}"
            )

            return (
                "I couldn't take the screenshot."
            )

    return None


# ============================================================
# LOCK
# ============================================================

def handle_lock_command(command):

    command = clean_command(command).lower()

    if command in [
        "lock computer",
        "lock pc",
        "lock my computer",
        "lock my pc",
        "lock the screen",
        "lock system",
    ]:

        try:

            ctypes.windll.user32.LockWorkStation()

            return (
                "Locking the computer."
            )

        except Exception as e:

            print(
                f"⚠️ Lock failed: {e}"
            )

            return (
                "I couldn't lock the computer."
            )

    return None


# ============================================================
# RECYCLE BIN
# ============================================================

def handle_recycle_bin(command):

    command = clean_command(command).lower()

    if command in [
        "open recycle bin",
        "open recyclebin",
        "show recycle bin",
    ]:

        try:

            subprocess.Popen(
                [
                    "explorer.exe",
                    "shell:RecycleBinFolder",
                ]
            )

            return (
                "Opening Recycle Bin."
            )

        except Exception as e:

            print(
                f"⚠️ Recycle Bin error: {e}"
            )

            return (
                "I couldn't open Recycle Bin."
            )

    return None


# ============================================================
# TASK MANAGER
# ============================================================

def handle_task_manager(command):

    command = clean_command(command).lower()

    if command in [
        "open task manager",
        "start task manager",
        "show task manager",
        "open taskmgr",
        "task manager",
    ]:

        try:

            subprocess.Popen(
                "taskmgr.exe",
                shell=False,
            )

            return (
                "Opening Task Manager."
            )

        except Exception as e:

            print(
                f"⚠️ Task Manager error: {e}"
            )

            return (
                "I couldn't open Task Manager."
            )

    return None


# ============================================================
# SETTINGS
# ============================================================

def handle_settings_command(command):

    command = clean_command(command).lower()

    settings = {

        "open settings":
            "start ms-settings:",

        "start settings":
            "start ms-settings:",

        "open windows settings":
            "start ms-settings:",

        "open network settings":
            "start ms-settings:network",

        "open wifi settings":
            "start ms-settings:network-wifi",

        "open bluetooth settings":
            "start ms-settings:bluetooth",

        "open display settings":
            "start ms-settings:display",

        "open sound settings":
            "start ms-settings:sound",

        "open apps settings":
            "start ms-settings:appsfeatures",

        "open storage settings":
            "start ms-settings:storagesense",
    }

    if command in settings:

        run_command(
            settings[command]
        )

        return (
            "Opening Windows Settings."
        )

    return None


# ============================================================
# FILE SEARCH
# ============================================================

def handle_file_search(command):
    """
    Find a file by name.  Uses the persistent SQLite index for
    fast results.  Falls back to a limited rglob scan when the
    index is unavailable.
    """
    command = clean_command(command)

    patterns = [
        r"^find file (.+)$",
        r"^find the file (.+)$",
        r"^search file (.+)$",
        r"^search for file (.+)$",
    ]

    query = None

    for pattern in patterns:
        match = re.match(
            pattern,
            command,
            flags=re.IGNORECASE,
        )
        if match:
            query = match.group(1).strip()
            break

    if not query:
        return None

    location_match = re.search(r"\s+(?:in|inside|within|under|from)\s+(?:my\s+)?(desktop|downloads|documents|pictures|videos|music)$", query, re.I)
    location_root = None
    if location_match:
        location_name = location_match.group(1).casefold()
        location_root = KNOWN_FOLDERS[location_name]
        query = query[:location_match.start()].strip()
        if not is_safe_path(str(location_root), "search"):
            return UNSAFE_PATH_MESSAGE
    if not query:
        return "Tell me the file name to search for."

    _jprint(f"[INDEX] Searching files -> {query}")

    # Scope file search to active folder or desktop barrier if no explicit location given
    if location_root is None:
        active = get_open_explorer_folder() or get_active_folder()
        if is_desktop_path(active) or is_desktop_active() or not active:
            # When on Desktop or no folder open: never search inside subfolders on Desktop
            desk_path = KNOWN_FOLDERS.get("desktop")
            if desk_path and desk_path.is_dir():
                desk_hit = find_file_in_hierarchy(query, str(desk_path))
                if desk_hit and os.path.isfile(desk_hit):
                    if not open_path_safe(desk_hit):
                        return f"I found {os.path.basename(desk_hit)}, but couldn't open it."
                    return f"I found and opened {os.path.basename(desk_hit)}."
            return f"I couldn't find {query} on the desktop. Open the folder first to search inside it."
        elif active and os.path.isdir(active):
            # Scoped to currently open folder
            file_hit = find_file_in_hierarchy(query, active)
            if file_hit and os.path.isfile(file_hit):
                if not open_path_safe(file_hit):
                    return f"I found {os.path.basename(file_hit)}, but couldn't open it."
                return f"I found and opened {os.path.basename(file_hit)}."
            return f"I couldn't find {query} in the current folder."

    for root in search_roots:
        if not root.exists() or not is_safe_path(str(root), "search"):
            continue
        try:
            for current, dirs, files in os.walk(root):
                if not is_safe_path(current, "search"):
                    dirs.clear()
                    continue
                dirs[:] = [d for d in dirs if is_safe_path(os.path.join(current, d), "search")]
                if is_desktop_path(current) or is_desktop_path(root):
                    dirs.clear()  # Desktop barrier: Never search into subfolders on Desktop
                _prune_search_dirs(dirs)
                for name in [*dirs, *files]:
                    path = Path(current) / name
                    if query.lower() in name.lower():
                        found.append(path)
                        if len(found) >= 10:
                            break
                if len(found) >= 10:
                    break
        except Exception:
            continue
        if len(found) >= 10:
            break

    if not found:
        return (
            f"I couldn't find a file matching {query}."
        )

    _jprint("[INDEX] Files found (fallback scan):")
    for path in found:
        print(f"   {path}")

    open_path(found[0])
    return f"I found and opened {found[0].name}."


# ============================================================
# PLAY COMMAND  (searches ACTIVE_FOLDER first)
# ============================================================

def handle_play_command(command, player=None):
    """
    Handle "play X", "watch X", "listen to X" commands strictly for media.

    Behavior:
    1. If ACTIVE_FOLDER exists:
       Search ONLY ACTIVE_FOLDER + its subfolders for supported audio/video files.
       - If a media match is found: play it and return "Playing <filename>." (ACTIVE_FOLDER remains unchanged).
       - If NO media match is found: return "I couldn't find a video or audio file matching {query} in the current folder."
    2. If NO ACTIVE_FOLDER exists:
       Search global persistent index for supported audio/video files.
       - If a media match is found: play it and return "Playing <filename>."
       - If NO media match is found: return "I couldn't find a video or audio file matching {query}."

    PLAY commands NEVER select non-media files (.txt, .pdf, .doc, .jpg, folders, etc.).
    """
    command_clean = clean_command(command)
    cmd_lower = command_clean.lower()
    cmd_lower = re.sub(
        r"^(play|pause|resume|stop) the (music|song|video|playback)$",
        r"\1 \2",
        cmd_lower,
    )

    # Match "play X on youtube" / "youtube play X"
    play_patterns = [
        r"^play (.+?) on youtube$",
        r"^play (.+?) in youtube$",
        r"^play (.+?) from youtube$",
        r"^youtube play (.+)$",
    ]
    for pp in play_patterns:
        if re.match(pp, command_clean, flags=re.IGNORECASE):
            return None  # let handle_youtube_command handle it

    # Also skip keyboard media-key commands
    # so common transport phrases never get searched as media filenames.
    if cmd_lower in {
        "play", "play music", "play song", "play video", "pause",
        "pause music", "pause video", "pause song", "pause playback",
        "resume", "resume music", "resume video", "resume song",
        "resume playback", "play pause", "toggle play", "toggle playback",
        "stop music", "stop video", "stop song", "stop playing",
        "stop playback", "stop media",
    }:
        return None  # let handle_media_command handle it

    match = re.match(
        r"^(?:play|watch|listen to|listen)\s+(.+)$",
        command_clean,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    query = match.group(1).strip()
    # Resolve lightweight conversational references without changing ACTIVE_FOLDER.
    query = re.sub(r"\s+(?:from|in)\s+(?:this|that|the same) folder$", "", query, flags=re.IGNORECASE)
    ordinal = {"first": 1, "second": 2, "third": 3, "fourth": 4}
    ordinal_match = re.fullmatch(r"(?:the )?(first|second|third|fourth)(?: episode| video)?", query, flags=re.IGNORECASE)
    ord_index = None
    if ordinal_match:
        if ordinal_match.group(1).lower() == "first":
            visible_result = _play_visible_browser_video()
            if visible_result:
                return visible_result
        if "video" in ordinal_match.group(0).lower():
            active = get_active_folder()
            if not active or is_desktop_path(active) or session.last_opened_website == "youtube":
                return handle_youtube_command("play " + ordinal_match.group(0))
        ord_name = ordinal_match.group(1).lower()
        ord_num = str(ordinal[ord_name])
        ord_index = ordinal[ord_name] - 1
        query = f"episode {ord_num}"
    elif query.lower() in {"it", "that", "that episode", "same one"} and session.last_played_media:
        query = os.path.splitext(os.path.basename(session.last_played_media))[0]
    if not query:
        return None

    if query.casefold() in {
        "this video", "the video", "current video", "this", "that", "it",
    }:
        visible_result = _play_visible_browser_video()
        if visible_result:
            return visible_result

    # "Play this/the/current video" means a local video visible to the user,
    # with Desktop taking precedence. It must not inspect the active browser
    # or redirect to YouTube.
    if query.casefold() in {"this video", "the video", "current video"}:
        desktop = KNOWN_FOLDERS.get("desktop")
        search_roots = [desktop] if desktop and os.path.isdir(desktop) else []
        videos = KNOWN_FOLDERS.get("videos")
        if videos and os.path.isdir(videos) and videos not in search_roots:
            search_roots.append(videos)
        for root in search_roots:
            hits = _search_media_in_folder(root, "")
            if hits:
                best = hits[0]
                if _open_media_file(best, player=player):
                    session.last_played_media = best
                    set_active_folder(os.path.dirname(best))
                    return f"Playing {os.path.basename(best)}."
        return "I couldn't find a video on the Desktop or Videos folder."

    if query.strip().lower() in {"bad apple", "the bad apple", "bad apple song", "bad apple video"}:
        return play_youtube("bad apple", browser=player)

    active = get_active_folder()

    # --------------------------------------------------------
    # 1. Search inside ACTIVE_FOLDER
    # --------------------------------------------------------
    if active and not _is_ignored_search_path(active) and os.path.isdir(active):
        _jprint(f"[CONTEXT] Searching for media '{query}' in active folder: {active}")
        hits = _search_media_in_folder(active, query)
        if not hits and ord_index is not None:
            # Fallback for ordinals: scan all media in active folder, sort naturally, take ord_index
            all_media = []
            try:
                for root, dirs, files in os.walk(active):
                    if not _prune_unsafe_walk_dirs(root, dirs):
                        continue
                    _prune_search_dirs(dirs)
                    for f in files:
                        if os.path.splitext(f)[1].lower() in _MEDIA_EXT:
                            all_media.append(os.path.join(root, f))
                all_media.sort()
                if len(all_media) > ord_index:
                    hits = [all_media[ord_index]]
            except Exception:
                pass
        if hits:
            best = hits[0]
            _jprint(f"[CONTEXT] Found media in active folder -> {best}")
            if not _open_media_file(best, player=player):
                if player and str(player).lower().strip() == "vlc":
                    return "VLC is not installed or couldn't open that media file."
                return f"I couldn't open {os.path.basename(best)}."
            session.last_played_media = best
            set_active_folder(os.path.dirname(best))
            return f"Playing {os.path.basename(best)}."
        else:
            _jprint(f"[CONTEXT] No media matching '{query}' found in active folder: {active}. Searching related hierarchy...")
            hier_hit = find_file_in_hierarchy(query, active, media_only=True)
            if hier_hit and os.path.isfile(hier_hit):
                _jprint(f"[CONTEXT] Found media in related hierarchy folder -> {hier_hit}")
                if not _open_media_file(hier_hit, player=player):
                    if player and str(player).lower().strip() == "vlc":
                        return "VLC is not installed or couldn't open that media file."
                    return f"I couldn't open {os.path.basename(hier_hit)}."
                session.last_played_media = hier_hit
                set_active_folder(os.path.dirname(hier_hit))
                return f"Playing {os.path.basename(hier_hit)}."

    # --------------------------------------------------------
    # 2. Global index search (when NO ACTIVE_FOLDER set or not in hierarchy)
    # --------------------------------------------------------
    _jprint(f"[CONTEXT] Searching global index for media matching '{query}'...")
    if _file_index is not None:
        hits = _file_index.search(query, prefer_dirs=False, limit=50)
        media_hits = []
        for fp in hits:
            if os.path.isfile(fp):
                ext = os.path.splitext(fp)[1].lower()
                if ext in _MEDIA_EXT:
                    score = _match_media_score(query, os.path.basename(fp))
                    if score > 0:
                        media_hits.append((score, fp))
        if media_hits:
            media_hits.sort(key=lambda item: (-item[0], len(os.path.basename(item[1]))))
            best = media_hits[0][1]
            _jprint(f"[CONTEXT] Global index media match -> {best}")
            if not _open_media_file(best, player=player):
                if player and str(player).lower().strip() == "vlc":
                    return "VLC is not installed or couldn't open that media file."
                return f"I couldn't open {os.path.basename(best)}."
            session.last_played_media = best
            set_active_folder(os.path.dirname(best))
            return f"Playing {os.path.basename(best)}."

    # Direct disk check if query is an explicit media file path
    if os.path.isfile(query):
        ext = os.path.splitext(query)[1].lower()
        if ext in _MEDIA_EXT:
            if not _open_media_file(query, player=player):
                if player and str(player).lower().strip() == "vlc":
                    return "VLC is not installed or couldn't open that media file."
                return f"I couldn't open {os.path.basename(query)}."
            session.last_played_media = query
            set_active_folder(os.path.dirname(query))
            return f"Playing {os.path.basename(query)}."

    if any(k in command_clean.lower() for k in ("youtube", "you tube", "yt")):
        return play_youtube(query, browser=player)

    if active and os.path.isdir(active):
        return f"I couldn't find a video or audio file matching {query} in the current folder."
    return f"I couldn't find a video or audio file matching {query}."


# ============================================================
# ACTIVE FOLDER MANAGEMENT COMMANDS
# ============================================================

def handle_active_folder_command(command):
    """
    Commands for inspecting / clearing the active folder context.

      "active folder"          -> report current active folder
      "what folder"            -> same
      "clear folder context"   -> set ACTIVE_FOLDER = None
      "forget this folder"     -> set ACTIVE_FOLDER = None
      "reset folder"           -> set ACTIVE_FOLDER = None
      "search everywhere"      -> clear context + acknowledge
    """
    cmd = clean_command(command).lower()

    # --- Report current ---
    if cmd in [
        "active folder",
        "what folder",
        "current folder",
        "show active folder",
        "which folder",
        "what is the active folder",
    ]:
        active = get_active_folder()
        if active:
            return f"The active folder is {active}."
        return "No active folder is set. I will search everywhere."

    # --- Clear / reset ---
    if cmd in [
        "clear folder context",
        "forget this folder",
        "forget folder",
        "reset folder",
        "reset folder context",
        "no folder",
        "clear context",
    ]:
        clear_active_folder()
        return "Folder context cleared. I will now search everywhere."

    # --- Search everywhere (one-shot bypass) ---
    if cmd in [
        "search everywhere",
        "look everywhere",
        "search all drives",
    ]:
        clear_active_folder()
        return (
            "Folder context cleared. "
            "Future searches will cover all indexed locations."
        )

    # --- "search everywhere for X" (search global index for X) ---
    m = re.match(
        r"^search everywhere (?:for )?(.+)$",
        cmd,
        flags=re.IGNORECASE,
    )
    if m:
        query = m.group(1).strip()
        if query and _file_index is not None:
            clear_active_folder()
            hits = _file_index.search(query, prefer_dirs=False, limit=10)
            if hits:
                best = hits[0]
                _jprint(f"[CONTEXT] Global search result -> {best}")
                if not open_path_safe(best):
                    return f"I found {os.path.basename(best)}, but couldn't open it."
                return f"Found {os.path.basename(best)} and opened it."
            return f"Nothing found for '{query}' in the index."

    return None


# ============================================================
# NETWORK
# ============================================================

def handle_network_command(command):

    command = clean_command(command).lower()

    if command in [
        "network info",
        "show network info",
        "network information",
        "show my ip",
        "what is my ip",
        "what is my ip address",
        "show my ip address",
    ]:

        try:

            result = subprocess.run(
                [
                    "ipconfig"
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )

            addresses = re.findall(
                r"\bIPv4[^:\r\n]*:\s*([0-9]{1,3}(?:\.[0-9]{1,3}){3})",
                result.stdout,
                flags=re.IGNORECASE,
            )
            active_addresses = [
                address for address in addresses
                if not address.startswith("169.254.")
            ]
            if active_addresses:
                return f"Your active IPv4 address is {active_addresses[0]}."
            return "I couldn't find an active IPv4 address."

        except Exception as e:

            print(
                f"⚠️ Network info failed: {e}"
            )

            return (
                "I couldn't get network information."
            )

    return None


# ============================================================
# CALCULATOR
# ============================================================

def handle_calculator(command):

    command = clean_command(command)

    match = re.match(
        r"^(?:calculate|what is|compute)\s+(.+)$",
        command,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    raw_expr = match.group(1).strip().lower().rstrip("?")

    # Normalize spoken words to math operators
    replacements = [
        (r"\bmultiplied\s+by\b", "*"),
        (r"\bdivided\s+by\b", "/"),
        (r"\bplus\b", "+"),
        (r"\bminus\b", "-"),
        (r"\btimes\b", "*"),
        (r"\bx\b", "*"),
    ]

    expression = raw_expr
    for pat, rep in replacements:
        expression = re.sub(pat, rep, expression)

    allowed = set(
        "0123456789+-*/().% "
    )

    if not expression or not any(c.isdigit() for c in expression) or not any(op in expression for op in "+-*/%"):
        return None

    if not all(
        char in allowed
        for char in expression
    ):
        return None

    try:

        result = _safe_calculate(expression)

        # Format integer results cleanly
        if isinstance(result, float) and result.is_integer():
            result = int(result)

        return (
            f"The answer is {result}."
        )

    except Exception:

        return None



# ============================================================
# HELP
# ============================================================

def handle_help_command(command):

    command = clean_command(command).lower()

    if command in [
        "help",
        "what can you do",
        "what can i ask you",
        "what commands can i say",
        "what commands do you know",
        "commands",
        "show commands",
        "list commands",
    ]:

        return (
            "I can open and switch apps and websites, control windows and media, "
            "search for files and folders, adjust volume, take screenshots, "
            "calculate, tell the time, and answer questions."
        )

    return None


# ============================================================
# INDEX STATUS COMMAND
# ============================================================

def handle_index_status_command(command):

    command = clean_command(command).lower()

    if command not in [
        "index status",
        "file index status",
        "show index status",
        "index info",
    ]:
        return None

    if _file_index is None:
        return "The file index is not available."

    stats = _file_index.get_stats()
    net = ", ".join(
        d.rstrip("\\") for d in stats["network_drives"]
    ) or "none detected"
    updated = stats["last_updated"] or "not yet"
    indexing = " (currently updating)" if stats["indexing"] else ""

    _jprint(
        f"[INDEX] Status:"
        f"\n   Total items : {stats['total']:,}"
        f"\n   Files       : {stats['files']:,}"
        f"\n   Folders     : {stats['directories']:,}"
        f"\n   Network drives: {net}"
        f"\n   Last updated: {updated}{indexing}"
    )

    return (
        f"The file index has {stats['total']:,} items. "
        f"Network drives: {net}. "
        f"Last updated: {updated}."
    )


# ============================================================
# MAIN LOCAL COMMAND ROUTER
# ============================================================

def _handle_command(command):

    if not command:
        return None

    # 0. CASUAL CONVERSATIONAL PHRASES & ALIAS CORRECTIONS (LOCAL FIRST)
    casual = handle_casual_phrase(command)
    if casual is not None:
        return casual

    remember = handle_remember_alias_command(command)
    if remember is not None:
        return remember

    # 1. NORMALIZE COMMAND
    command = clean_command(command)

    _jprint(
        f"🧠 Local command check: "
        f"{command.lower()}"
    )

    # 2. SESSION CONTEXT RESOLUTION
    from core.session_memory import session
    lower = command.lower()
    if lower in {"open that folder", "open the folder", "open current folder"} and session.active_folder:
        if not open_path_safe(session.active_folder):
            return "I couldn't open the current folder."
        return f"Opening the {os.path.basename(session.active_folder)} folder."
    if lower in {"open that file", "open the file", "open that", "open file"}:
        active = get_open_explorer_folder() or get_active_folder()
        if not active or is_desktop_path(active):
            return "Open a folder first; I only open files from an open folder."
        if session.last_opened_file and os.path.dirname(session.last_opened_file) == active and os.path.isfile(session.last_opened_file):
            if open_path_safe(session.last_opened_file):
                return f"Opening {os.path.basename(session.last_opened_file)}."
        try:
            for entry in os.scandir(active):
                if entry.is_file(follow_symlinks=False) and not entry.name.startswith("."):
                    if open_path_safe(entry.path):
                        session.last_opened_file = entry.path
                        return f"Opening {entry.name}."
        except Exception:
            pass
        return f"No files found in the {os.path.basename(active)} folder."

    # Route YouTube intents before looking through the active folder. A local
    # dependency may contain a folder named "youtube", but that must never
    # override the user's site or video command.
    result = handle_youtube_command(command)
    if result is not None:
        return result

    # 4. MULTI-STEP COMMAND PARSER
    from core.command_engine import execute_command_chain
    chain_result = execute_command_chain(command)
    if chain_result is not None:
        return chain_result

    from core.command_engine import execute_scoped_action
    scoped_result = execute_scoped_action(command)
    if scoped_result is not None:
        return scoped_result

    # 4. WINDOW / APP CONTROL (maximize, restore, minimize, switch, close, desktop)
    result = handle_window_command(command)
    if result is not None:
        return result

    # 5. WHATSAPP CHAT ACTIONS (open contact chat)
    result = handle_whatsapp_command(command)
    if result is not None:
        return result

    # 5b. ACTIVE FOLDER DIRECT MATCH (when user is in a folder)
    active = get_open_explorer_folder() or get_active_folder()
    if active:
        m_hier = re.match(
            r"^(?:open|go to|goto|play|watch)\s+(?:the\s+|my\s+)?(?:file\s+)?(.+?)(?:\s+folder|\s+file)?$",
            command,
            re.IGNORECASE,
        )
        if m_hier:
            target_raw = m_hier.group(1).strip()
            target_lower = target_raw.lower()
            known_sys_apps = {
                "chrome", "google chrome", "notepad", "calculator", "calc", "paint",
                "edge", "microsoft edge", "brave", "brave browser", "spotify",
                "discord", "steam", "vlc", "settings", "task manager", "whatsapp",
            }
            known_sys_apps.update(WEBSITE_URLS)
            if target_lower not in known_sys_apps:
                is_file_query = bool(re.search(r"\.[a-zA-Z0-9]{1,8}$", target_raw)) or bool(re.match(r"^file\b", target_raw, re.I))

                # If on Desktop and asking for a file
                if is_desktop_path(active) and is_file_query:
                    file_hit = find_file_in_hierarchy(target_raw, active)
                    if file_hit and os.path.isfile(file_hit):
                        if open_path_safe(file_hit):
                            from core.session_memory import session
                            session.last_opened_file = file_hit
                            return f"Opening {os.path.basename(file_hit)}."
                    return f"I couldn't find {target_raw} on the desktop. Open the folder first."

                # 1. Check folder match in hierarchy
                if not is_file_query:
                    f_hit = find_folder_in_hierarchy(target_raw, active)
                    if f_hit and os.path.isdir(f_hit):
                        if open_path_safe(f_hit):
                            set_active_folder(f_hit)
                            from core.session_memory import session
                            session.set_current_app(os.path.basename(f_hit))
                            return f"Opening the {os.path.basename(f_hit)} folder."

                # 2. Check file match in active folder
                file_hit = find_file_in_hierarchy(target_raw, active)
                if file_hit and os.path.isfile(file_hit):
                    ext = os.path.splitext(file_hit)[1].lower()
                    if ext in _MEDIA_EXT:
                        if _open_media_file(file_hit):
                            from core.session_memory import session
                            session.last_played_media = file_hit
                            set_active_folder(os.path.dirname(file_hit))
                            return f"Playing {os.path.basename(file_hit)}."
                    else:
                        if open_path_safe(file_hit):
                            from core.session_memory import session
                            session.last_opened_file = file_hit
                            set_active_folder(os.path.dirname(file_hit))
                            return f"Opening {os.path.basename(file_hit)}."
                elif is_file_query:
                    if is_desktop_path(active):
                        return f"I couldn't find {target_raw} on the desktop. Open the folder first."
                    return f"I couldn't find {target_raw} in the current folder."

    # 5c. BROWSER CONTROL COMMANDS (link clicking, scrolling, back)
    result = handle_browser_control_command(command)
    if result is not None:
        return result

    # 5d. BROWSER PROFILE COMMANDS (open chrome profile X)
    from tools.browser_profiles import handle_browser_profile_command
    result = handle_browser_profile_command(command)
    if result is not None:
        return result

    # 6. EXPLICIT APP LAUNCH / OPEN (open WhatsApp, open Chrome, open Notepad, etc.)
    result = handle_app_command(command)
    if result is not None:
        return result

    # 7. FOLDER / FILE COMMANDS
    result = handle_pc_path_command(command)
    if result is not None:
        return result

    result = handle_pc_folder_command(command)
    if result is not None:
        return result

    result = handle_folder_command(command)
    if result is not None:
        return result

    result = handle_file_search(command)
    if result is not None:
        return result

    # 8. MEDIA INTENT (searches ACTIVE_FOLDER first, then global media index)
    result = handle_active_folder_command(command)
    if result is not None:
        return result

    result = handle_play_command(command)
    if result is not None:
        return result

    result = handle_media_command(command)
    if result is not None:
        return result

    # 9. EXPLICIT WEBSITE / BROWSER NAVIGATION
    result = handle_open_website(command)
    if result is not None:
        return result

    result = handle_youtube_command(command)
    if result is not None:
        return result

    # 10. SYSTEM UTILITY COMMANDS
    result = handle_task_manager(command)
    if result is not None:
        return result

    result = handle_settings_command(command)
    if result is not None:
        return result

    result = handle_calculator(command)
    if result is not None:
        return result

    result = handle_network_command(command)
    if result is not None:
        return result

    result = handle_index_status_command(command)
    if result is not None:
        return result

    result = handle_help_command(command)
    if result is not None:
        return result

    result = handle_volume_command(command)
    if result is not None:
        return result

    result = handle_time_command(command)
    if result is not None:
        return result

    result = handle_screenshot(command)
    if result is not None:
        return result

    result = handle_lock_command(command)
    if result is not None:
        return result

    result = handle_recycle_bin(command)
    if result is not None:
        return result

    result = handle_search_command(command)
    if result is not None:
        return result

    result = handle_file_system_command(command)
    if result is not None:
        return result

    # 11. GENERIC OPEN FALLBACK
    # Don't guess a web search for an unknown application name. Apps only use
    # the explicit web-client allowlist in window_tools; sites can still be
    # opened with an explicit "website" request or a typed domain.
    match = re.match(
        r"^open (.+)$",
        command,
        flags=re.IGNORECASE,
    )

    if match:
        query = match.group(1).strip()
        if query:
            if re.fullmatch(
                r"(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s]*)?",
                query,
                flags=re.IGNORECASE,
            ):
                url = query if query.lower().startswith(("http://", "https://")) else f"https://{query}"
                if open_url(url):
                    return f"Opening {query}."
            from tools.app_tools import GENERIC_APP_WORDS
            if query.lower().strip() in GENERIC_APP_WORDS:
                return f"What did you mean by '{query}'?"
            if re.search(r"[\\/]", query) or re.search(r"\.[a-z0-9]{1,8}$", query, re.IGNORECASE):
                return f"I couldn't find {query} on this PC."
            google_search(query)
            return f"I couldn't find {query} as an app, so I'm searching the web."

    return None


def _remember_local_command(command: str, result: str) -> None:
    """Update the shared session after any successful local action."""
    text = clean_command(command).lower()
    if text in {"reset memory", "clear memory", "new conversation"}:
        session.clear()
        clear_active_folder()
        return
    if session.active_folder:
        session.active_folder = get_active_folder()

    folder_match = re.match(r"^(?:open|go to) (.+?)(?: folder)?$", command, flags=re.IGNORECASE)
    website_targets = {
        "samsung", "youtube", "google", "github", "chatgpt", "gmail", "reddit",
        "instagram", "facebook", "twitter", "x", "linkedin", "amazon",
        "wikipedia", "netflix",
    }
    is_known_website_request = (
        "website" in text
        or "official site" in text
        or "official website" in text
        or (text.startswith(("go to ", "visit ")) and folder_match and folder_match.group(1).strip().lower() in website_targets)
    )

    # Only set active folder if the user explicitly commanded a folder, or the result specifically opened a folder
    is_folder_command = (
        "folder" in text
        or "directory" in text
        or "folder" in result.lower()
        or result.lower().startswith("opening the ")
    )

    if folder_match and is_folder_command and result.lower().startswith("opening") and not is_known_website_request:
        folder = find_pc_folder(folder_match.group(1).strip())
        if folder and os.path.isdir(str(folder)):
            set_active_folder(str(folder))

    if result.lower().startswith("playing "):
        session.last_played_media = result[8:].rstrip(".")

    if text.startswith(("search ", "google ", "look up ")):
        session.last_search = re.sub(r"^(?:search(?: google for)?|google|look up)\s+", "", command, flags=re.IGNORECASE)

    if text.startswith(("open ", "launch ", "start ")) and not any(word in text for word in ("website", "site", "folder", "directory")):
        app = re.sub(r"^(?:open|launch|start)\s+", "", command, flags=re.IGNORECASE).strip()
        if app.lower() in {"notepad", "calculator", "calc", "paint", "chrome", "edge", "brave", "spotify", "discord", "steam", "vlc", "settings", "task manager"}:
            session.last_opened_app = app



def handle_command(command):
    """Public local router with one place for session-memory updates."""
    # 0. Local casual phrases & user corrections (immediate local response, no AI call)
    casual = handle_casual_phrase(command)
    if casual is not None:
        return casual

    remember = handle_remember_alias_command(command)
    if remember is not None:
        return remember

    cleaned = clean_command(command)
    from core.command_engine import normalize_voice_text
    normalized = normalize_voice_text(cleaned)
    lower = normalized.lower()
    # Speech often adds the active destination as a prefix:
    # "on YouTube, open YouTube" should remain one local browser command.
    normalized = re.sub(
        r"^(?:on|in)\s+(?:youtube|you\s+tube|yt)\s*[-,:]?\s*",
        "",
        normalized,
        flags=re.IGNORECASE,
    ).strip()
    lower = normalized.lower()
    # Whisper can hear the Indian-accented phrase "play first video" as
    # "Fresh Studio". Correct that only while YouTube is the active destination
    # and there is a pending search, so a real app request remains untouched.
    if (
        lower in {"fresh studio", "fresh video", "press studio"}
        and session.last_opened_website == "youtube"
        and session.youtube_search_query
    ):
        print(f"[STT] Context correction: {normalized} -> play first video")
        cleaned = normalized = lower = "play first video"
    gesture_tracking_commands = {
        "turn on gestures": True,
        "turn on gesture control": True,
        "turn gestures on": True,
        "turn gesture tracking on": True,
        "enable gestures": True,
        "enable gesture control": True,
        "enable gesture tracking": True,
        "start gesture control": True,
        "start gestures": True,
        "turn off gestures": False,
        "turn off gesture control": False,
        "turn gestures off": False,
        "turn gesture tracking off": False,
        "disable gestures": False,
        "disable gesture control": False,
        "disable gesture tracking": False,
        "stop gesture control": False,
        "stop gestures": False,
    }
    if lower in gesture_tracking_commands:
        enabled = gesture_tracking_commands[lower]
        try:
            from core.gesture_engine import set_camera_tracking_enabled
            applied = set_camera_tracking_enabled(enabled)
            if applied == enabled:
                return "Gesture tracking enabled." if enabled else "Gesture tracking disabled."
            return "I couldn't change gesture tracking."
        except Exception as error:
            print(f"[GestureEngine] Voice toggle failed: {error}")
            return "I couldn't change gesture tracking."
    if lower in {
        "gesture preview",
        "preview gesture",
        "show gesture preview",
        "open gesture preview",
        "start gesture preview",
        "gesture control",
    }:
        event_bus.publish("gesture_preview_requested")
        return "Opening gesture preview. It shows recognized gestures and their meanings; it does not control your PC."
    if lower in {"reset memory", "clear memory", "new conversation"}:
        session.clear()
        clear_active_folder()
        return "Session memory cleared."
    from core.command_engine import handle_pending_confirmation
    confirmation_result = handle_pending_confirmation(lower)
    if confirmation_result is not None:
        return confirmation_result

    if lower in {
        "stop",
        "stop it",
        "stop that",
        "stop now",
        "stop talking",
        "stop speaking",
        "shut up",
        "be quiet",
        "silence",
        "quiet",
        "hush",
        "cancel",
        "abort",
        "hold on",
        "stop jarvis",
        "jarvis stop",
    }:
        from core.state import state_machine
        if state_machine.mode:
            state_machine.set_mode(None)
        return "__STOP_TTS__"

    # Explicit app or browser searches must be executed before the general
    # browser/YouTube handlers, or those handlers can redirect them to Chrome.
    from core.command_engine import execute_scoped_action, parse_action
    scoped_search = parse_action(normalized)
    if scoped_search and scoped_search.verb == "search" and (
        scoped_search.args.get("destination") or scoped_search.args.get("browser")
    ):
        result = execute_scoped_action(normalized)
        if result is not None:
            _remember_local_command(normalized, result)
            return result

    # Browser controls and website commands have exact local actions and must
    # not be handed to the AI router, which can otherwise only narrate them.
    visible_item = re.fullmatch(
        r"(?:open|click|go to)\s+(?:(?:the|this|that)\s+)?"
        r"(link|file|folder|directory|document)",
        lower,
    )
    if visible_item:
        requested_kind = visible_item.group(1)
        if requested_kind in {"document"}:
            requested_kind = "file"
        if requested_kind in {"directory"}:
            requested_kind = "folder"
        visible_result = _open_visible_browser_item(requested_kind)
        if visible_result:
            _remember_local_command(normalized, visible_result)
            return visible_result

    if lower in {"open this", "open that", "open it", "open the visible item"}:
        visible_result = _open_visible_browser_item("link")
        if visible_result:
            _remember_local_command(normalized, visible_result)
            return visible_result

    browser_result = handle_browser_control_command(normalized)
    direct_result = browser_result or handle_youtube_command(normalized)
    if direct_result is not None:
        event_bus.publish("heard", text=command, normalized=normalized)
        failed = any(word in direct_result.lower() for word in ("couldn't", "could not", "can't", "failed"))
        event_bus.publish("step_result", success=not failed, message=direct_result)
        if failed:
            event_bus.publish("error", message=direct_result)
        _remember_local_command(normalized, direct_result)
        return direct_result

    # Check browser profiles before general app launch
    from tools.browser_profiles import handle_browser_profile_command
    profile_result = handle_browser_profile_command(normalized)
    if profile_result is not None:
        event_bus.publish("heard", text=command, normalized=normalized)
        event_bus.publish("step_result", success=True, message=profile_result)
        _remember_local_command(normalized, profile_result)
        return profile_result

    # Check installed applications before file/folder or AI/web fallback.
    context_only_request = normalized.lower() in {
        "open file", "open the file", "open that file", "open this file",
        "open folder", "open the folder", "open that folder", "open this folder",
    }
    app_result = None if context_only_request else handle_app_command(normalized)
    if app_result is not None:
        event_bus.publish("heard", text=command, normalized=normalized)
        event_bus.publish("step_result", success=True, message=app_result)
        _remember_local_command(normalized, app_result)
        return app_result

    # Resolve the generic "open file" intent before workspace routing. The
    # workspace handler treats "file" as a literal search term and can hide
    # the active Explorer folder context.
    if normalized.lower() in {"open file", "open the file", "open that file", "open this file"}:
        active = get_open_explorer_folder() or get_active_folder()
        if not active or is_desktop_path(active):
            result = "Open a folder first; I only open files from an open folder."
        else:
            result = None
            if session.last_opened_file and os.path.dirname(session.last_opened_file) == active and os.path.isfile(session.last_opened_file):
                if open_path_safe(session.last_opened_file):
                    result = f"Opening {os.path.basename(session.last_opened_file)}."
            if result is None:
                try:
                    for entry in os.scandir(active):
                        if entry.is_file(follow_symlinks=False) and not entry.name.startswith("."):
                            if open_path_safe(entry.path):
                                session.last_opened_file = entry.path
                                result = f"Opening {entry.name}."
                                break
                except OSError as error:
                    result = f"Could not inspect the open folder: {error}"
            if result is None:
                result = f"No files found in the {os.path.basename(active)} folder."
        event_bus.publish("heard", text=command, normalized=normalized)
        failed = result.lower().startswith(("open a folder", "no files", "could not"))
        event_bus.publish("step_result", success=not failed, message=result)
        if failed:
            event_bus.publish("error", message=result)
        _remember_local_command(normalized, result)
        return result

    # File and folder requests are handled before global active-folder/index search.
    workspace_result = _workspace_command(normalized)
    if workspace_result is not None:
        event_bus.publish("heard", text=command)
        failed = any(word in workspace_result.lower() for word in ("couldn't", "can't", "set a workspace first", "confirm", "only open files inside"))
        event_bus.publish("step_result", success=not failed, message=workspace_result)
        if failed:
            event_bus.publish("error", message=workspace_result)
        return workspace_result

    from core.command_engine import parse_action, parse_command_chain, action_to_dsl
    parsed = parse_command_chain(normalized)
    if not parsed:
        action = parse_action(normalized)
        parsed = [action] if action else []
    if parsed:
        event_bus.publish("heard", text=command)
        event_bus.publish("parsed", actions=[action_to_dsl(action) for action in parsed], raw=command)
    if len(parsed) == 1 and parsed[0].verb == "mode":
        from core.command_engine import execute_dsl
        _success, result = execute_dsl(action_to_dsl(parsed[0]), {"current_app": None}, parsed[0])
        return result

    if re.match(r"^(?:open|launch|start)\s+", normalized, re.I) and not context_only_request:
        from core.command_engine import _destination_kind, parse_action, action_to_dsl, execute_dsl
        action = parse_action(normalized)
        if action and action.verb == "open":
            kind, canonical = _destination_kind(action.target)
            if kind in {"app", "browser"}:
                allowed = get_settings().get("app_allowlist", [])
                launch_name = canonical.lower()
                if launch_name not in allowed and (action.target or "").lower() not in allowed:
                    return f"I can't launch {action.target}; it isn't in app_allowlist."
                _success, result = execute_dsl(f"launch:{launch_name}", {"current_app": None}, action)
                _remember_local_command(normalized, result)
                return result

    # Let the YouTube parser consume explicit YouTube searches first. The
    # generic active-site route otherwise treats words like "in yt for" as
    # part of the user's search text instead of as voice-command syntax.
    if re.match(
        r"^(?:search|find)\b.*\b(?:youtube|you\s+tube|yt)\b|^(?:youtube|you\s+tube|yt)\s+(?:search|find)\b",
        normalized,
        flags=re.IGNORECASE,
    ):
        result = handle_youtube_command(normalized)
        if result is not None:
            _remember_local_command(normalized, result)
            return result

    active_website = get_active_website_context()
    explicit_search = re.match(r"^(?:search(?:\s+for)?|look\s+up|google)\s+(.+)$", normalized, flags=re.IGNORECASE)
    if active_website and explicit_search:
        browser, website = active_website
        query = explicit_search.group(1).strip()
        result = search_website_destination(website, query, browser=browser, same_tab=True)
        if result:
            session.last_search = query
            return result

    result = _handle_command(normalized)
    if result is not None:
        _remember_local_command(normalized, result)
    return result


# ============================================================
# DIRECT TEST MODE
# ============================================================

if __name__ == "__main__":

    print("=" * 55)
    print("          JARVIS SYSTEM TOOLS TEST")
    print("=" * 55)

    print()
    print("Examples:")
    print("  open anime folder")
    print("  open GTA")
    print("  open Documents folder")
    print("  open Downloads")
    print("  play Naruto")
    print("  play episode 5")
    print("  active folder")
    print("  clear folder context")
    print("  search everywhere for Naruto")
    print("  find file report.pdf")
    print("  index status")
    print("  open Nothing website")
    print("  play Thriller in YouTube")
    print("  search Python")
    print()
    print("Note: The background indexer starts automatically.")
    print("      Allow a few seconds for the first index build.")
    print()

    # Give the background thread a moment to report network drives
    time.sleep(0.5)

    while True:

        try:

            command = input(
                "\nCommand: "
            ).strip()

            if command.lower() == "sleep":

                break

            result = handle_command(
                command
            )

            print(
                f"\nResult: {result}"
            )

        except KeyboardInterrupt:

            print(
                "\nExiting."
            )

            break

        except Exception as e:

            print(
                f"\n\u274c Error: {e}"
            )
