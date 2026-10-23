import os
import re
import difflib
from pathlib import Path


# ============================================================
# BASIC PATHS
# ============================================================

HOME = Path.home()
JARVIS_DIR = HOME / "Documents" / "Jarvis"


def _user_folder(registry_value: str, fallback: str) -> Path:
    """Resolve a Windows known folder, including OneDrive redirections."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            value, _value_type = winreg.QueryValueEx(key, registry_value)
        if isinstance(value, str) and value.strip():
            return Path(os.path.expandvars(os.path.expanduser(value.strip())))
    except (ImportError, OSError, TypeError, ValueError):
        pass
    return HOME / fallback if fallback else HOME


# ============================================================
# KNOWN WINDOWS FOLDERS
# ============================================================

KNOWN_FOLDERS = {
    "desktop": _user_folder("Desktop", "Desktop"),
    "downloads": _user_folder("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"),
    "documents": _user_folder("Personal", "Documents"),
    "pictures": _user_folder("My Pictures", "Pictures"),
    "photos": _user_folder("My Pictures", "Pictures"),
    "videos": _user_folder("My Video", "Videos"),
    "music": _user_folder("My Music", "Music"),
    "home": HOME,
    "user": HOME,
    "user folder": HOME,
    "onedrive": HOME / "OneDrive",
    "jarvis": JARVIS_DIR,
    "jarvis folder": JARVIS_DIR,
    "jarvis project": JARVIS_DIR,
}


# ============================================================
# FILE TYPES
# ============================================================

FILE_TYPES = {
    "python": {
        ".py",
        ".pyw",
    },

    "pdf": {
        ".pdf",
    },

    "word": {
        ".doc",
        ".docx",
    },

    "excel": {
        ".xls",
        ".xlsx",
        ".csv",
    },

    "powerpoint": {
        ".ppt",
        ".pptx",
    },

    "text": {
        ".txt",
        ".md",
        ".rtf",
    },

    "image": {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".bmp",
        ".tiff",
        ".ico",
    },

    "video": {
        ".mp4",
        ".mkv",
        ".avi",
        ".mov",
        ".wmv",
        ".webm",
        ".m4v",
        ".mpeg",
        ".mpg",
        ".3gp",
        ".ts",
    },

    "audio": {
        ".mp3",
        ".wav",
        ".flac",
        ".m4a",
        ".aac",
        ".ogg",
        ".opus",
        ".wma",
    },
}


MEDIA_EXTENSIONS = (
    FILE_TYPES["audio"]
    | FILE_TYPES["video"]
)


# ============================================================
# DIRECTORIES TO SKIP
# ============================================================

SKIP_DIRS = {
    "$recycle.bin",
    "system volume information",
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "appdata",
    "node_modules",
    ".git",
    ".venv",
    ".ttsenv",
    "__pycache__",
}


# ============================================================
# QUERY CLEANING
# ============================================================

def _clean_query(query: str) -> str:

    if not query:
        return ""

    query = str(query).strip()

    query = query.strip('"').strip("'")

    query = re.sub(
        r"\s+",
        " ",
        query,
    )

    return query.strip()


# ============================================================
# NORMALIZE NAME
# ============================================================

def _normalize_name(text: str) -> str:

    text = text.lower()

    text = text.replace(".", " ")

    text = re.sub(
        r"[_\-]+",
        " ",
        text,
    )

    text = re.sub(
        r"[^a-z0-9\s]",
        " ",
        text,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


# ============================================================
# FILE TYPE DETECTION
# ============================================================

def _detect_file_type(query: str):

    text = query.lower()

    aliases = {
        "python": "python",
        "py": "python",
        "script": "python",

        "pdf": "pdf",

        "word": "word",
        "doc": "word",
        "docx": "word",

        "excel": "excel",
        "xlsx": "excel",
        "spreadsheet": "excel",

        "powerpoint": "powerpoint",
        "ppt": "powerpoint",
        "pptx": "powerpoint",
        "presentation": "powerpoint",

        "text": "text",
        "txt": "text",

        "image": "image",
        "picture": "image",
        "photo": "image",

        "video": "video",
        "movie": "video",

        "audio": "audio",
        "music": "audio",
        "song": "audio",
    }

    for word, file_type in aliases.items():

        if re.search(
            rf"\b{re.escape(word)}\b",
            text,
        ):
            return file_type

    return None


# ============================================================
# LOCATION DETECTION
# ============================================================

def _detect_location(query: str):

    text = query.lower()

    locations = [
        "downloads",
        "documents",
        "desktop",
        "videos",
        "music",
        "pictures",
        "photos",
        "onedrive",
        "jarvis",
    ]

    for location in locations:

        if re.search(
            rf"\b{re.escape(location)}\b",
            text,
        ):
            return location

    return None


# ============================================================
# SEARCH QUERY EXTRACTION
# ============================================================

def _extract_search_query(query: str):

    text = _clean_query(query)

    if not text:
        return ""

    # --------------------------------------------------------
    # Remove command prefixes
    # --------------------------------------------------------

    text = re.sub(
        r"^(?:open|launch|start)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"^(?:go to|navigate to|access)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"^(?:find|locate|show|get)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # --------------------------------------------------------
    # Remove file/folder wording
    # --------------------------------------------------------

    remove_phrases = [
        r"\bmy\b",
        r"\bthe\b",
        r"\bfile\b",
        r"\bfiles\b",
        r"\bfolder\b",
        r"\bfolders\b",
        r"\bdirectory\b",
        r"\bdocument\b",
        r"\bdocuments\b",
        r"\bcalled\b",
        r"\bnamed\b",
        r"\babout\b",
        r"\brelated to\b",
    ]

    for pattern in remove_phrases:

        text = re.sub(
            pattern,
            " ",
            text,
            flags=re.IGNORECASE,
        )

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # DO NOT remove "python", "video", "pdf", etc.
    #
    # Those words are useful signals for file type.
    # --------------------------------------------------------

    # --------------------------------------------------------
    # Remove location phrases
    # --------------------------------------------------------

    text = re.sub(
        r"\bfrom\s+(downloads|documents|desktop|videos|music|pictures|photos|onedrive|jarvis)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"\bin\s+(downloads|documents|desktop|videos|music|pictures|photos|onedrive|jarvis)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"\bon\s+(downloads|documents|desktop|videos|music|pictures|photos|onedrive|jarvis)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )

    # --------------------------------------------------------
    # Clean
    # --------------------------------------------------------

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


# ============================================================
# QUERY TOKENS
# ============================================================

def _query_tokens(query: str):

    normalized = _normalize_name(query)

    return [
        token
        for token in normalized.split()
        if token
    ]


# ============================================================
# SEARCH ROOTS
# ============================================================

def _roots():
    roots = []
    allowed = {"desktop", "documents", "downloads", "pictures", "videos", "music", "jarvis"}
    for alias, path in KNOWN_FOLDERS.items():
        if alias in allowed and path.exists() and path not in roots and _is_safe_file_path(path, "search"):
            roots.append(path)
    if JARVIS_DIR.exists() and JARVIS_DIR not in roots and _is_safe_file_path(JARVIS_DIR, "search"):
        roots.append(JARVIS_DIR)
    return roots


def _is_safe_file_path(path, operation="read", *, allow_windows_navigation=False):
    """Use the central system path policy without creating an import cycle."""
    try:
        from tools.system_tools import is_safe_path
        return is_safe_path(str(path), operation, allow_windows_navigation=allow_windows_navigation)
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        return False


# ============================================================
# LOCATION ROOT
# ============================================================

def _location_root(location):

    if not location:
        return None

    location = location.lower()

    path = KNOWN_FOLDERS.get(
        location
    )

    if path and path.exists() and _is_safe_file_path(path, "search"):
        return path

    return None


# ============================================================
# OPEN PATH
# ============================================================

def _open_path(path: Path):

    try:

        # Keep mapped drives and UNC paths intact for Windows shell opening.
        path = Path(path).expanduser()
        path_text = str(path)
        if not path_text.startswith("\\\\") and not re.match(r"^[A-Za-z]:[\\/]", path_text):
            path = path.resolve()

        if not path.exists():
            return f"I couldn't find {path.name}."

        if not _is_safe_file_path(path, "open_readonly", allow_windows_navigation=True):
            from tools.system_tools import UNSAFE_PATH_MESSAGE
            return UNSAFE_PATH_MESSAGE

        desktop = HOME / "Desktop"
        try:
            from tools.system_tools import is_desktop_path
            is_loose_desktop = is_desktop_path(path.parent) or (path.parent.resolve() == desktop.resolve())
        except Exception:
            is_loose_desktop = (path.parent.resolve() == desktop.resolve())

        if path.is_file() and is_loose_desktop and path.suffix.lower() not in {".lnk", ".url", ".exe", ".appref-ms"}:
            return "On the Desktop I can open apps and folders, not loose files."

        os.startfile(str(path))

        return f"Opening {path.name}."

    except Exception as e:

        print(
            f"⚠️ Could not open path: {e}"
        )

        try:
            name = path.name
        except Exception:
            name = "that"

        return f"I couldn't open {name}."


# ============================================================
# SAFE DIRECTORY ITERATION
# ============================================================

def _iter_children(root: Path):

    if not _is_safe_file_path(root, "search"):
        return

    try:
        for child in root.iterdir():
            if _is_safe_file_path(child, "search"):
                yield child

    except (
        PermissionError,
        OSError,
    ):

        return


# ============================================================
# FUZZY SCORE
# ============================================================

def _fuzzy_score(
    query: str,
    name: str,
):

    query_clean = _normalize_name(query)
    name_clean = _normalize_name(name)

    if not query_clean or not name_clean:
        return 0

    score = 0

    # Exact
    if name_clean == query_clean:
        score += 1000

    # Contains
    if query_clean in name_clean:
        score += 500

    # Starts with
    if name_clean.startswith(query_clean):
        score += 250

    # Token matching
    query_tokens = _query_tokens(query)
    name_tokens = _query_tokens(name)

    for token in query_tokens:

        if token in name_tokens:

            score += 180

        elif any(
            token in name_token
            for name_token in name_tokens
        ):

            score += 120

    # Overall similarity
    similarity = difflib.SequenceMatcher(
        None,
        query_clean,
        name_clean,
    ).ratio()

    score += int(
        similarity * 200
    )

    # Individual similarity
    for q_token in query_tokens:

        best_ratio = 0.0

        for n_token in name_tokens:

            ratio = difflib.SequenceMatcher(
                None,
                q_token,
                n_token,
            ).ratio()

            best_ratio = max(
                best_ratio,
                ratio,
            )

        if best_ratio >= 0.75:

            score += int(
                best_ratio * 100
            )

    return score


# ============================================================
# BEST FUZZY MATCH
# ============================================================

def _best_fuzzy_match(
    query,
    candidates,
    minimum_score=180,
):

    best_path = None
    best_score = 0

    for path in candidates:

        try:

            score = _fuzzy_score(
                query,
                path.name,
            )

            if score > best_score:

                best_score = score
                best_path = path

        except Exception:
            continue

    if not best_path:
        return None

    if best_score < minimum_score:
        return None

    print(
        f"🧠 Fuzzy match → {best_path} "
        f"(score: {best_score})"
    )

    return best_path


# ============================================================
# FIND FILE
# ============================================================

def find_file(query: str):

    original_query = _clean_query(
        query
    )

    if not original_query:
        return None

    file_type = _detect_file_type(
        original_query
    )

    location = _detect_location(
        original_query
    )

    search_query = _extract_search_query(
        original_query
    )

    # --------------------------------------------------------
    # If natural-language query has no specific filename,
    # use the original query as fallback.
    # --------------------------------------------------------

    if not search_query:

        search_query = original_query

    # --------------------------------------------------------
    # For type-only searches such as:
    #
    # "open my python file"
    # "find a pdf"
    #
    # allow empty filename matching.
    # --------------------------------------------------------

    search_query_clean = _normalize_name(
        search_query
    )

    type_only = (
        file_type is not None
        and search_query_clean
        in {
            "python",
            "py",
            "script",
            "pdf",
            "word",
            "doc",
            "docx",
            "excel",
            "xlsx",
            "spreadsheet",
            "powerpoint",
            "ppt",
            "pptx",
            "presentation",
            "text",
            "txt",
            "image",
            "picture",
            "photo",
            "video",
            "movie",
            "audio",
            "music",
            "song",
        }
    )

    # --------------------------------------------------------
    # Direct path
    # --------------------------------------------------------

    try:

        candidate = Path(
            original_query
        ).expanduser()

        if candidate.is_file() and _is_safe_file_path(candidate, "read"):

            print(
                f"📄 Direct file match → {candidate}"
            )

            return candidate

    except Exception:
        pass

    # --------------------------------------------------------
    # Home relative
    # --------------------------------------------------------

    try:

        candidate = (
            HOME / original_query
        )

        if candidate.is_file() and _is_safe_file_path(candidate, "read"):

            print(
                f"📄 Home file match → {candidate}"
            )

            return candidate

    except Exception:
        pass

    # --------------------------------------------------------
    # Location-specific search
    # --------------------------------------------------------

    location_root = _location_root(
        location
    )

    if location_root and _is_safe_file_path(location_root, "search"):

        search_roots = [
            location_root
        ]

    else:

        search_roots = _roots()

    # ========================================================
    # EXACT / PARTIAL / FUZZY FILE SEARCH
    # ========================================================

    candidates = []

    for root in search_roots:

        try:

            for current, dirs, files in os.walk(root):

                if not _is_safe_file_path(current, "search"):
                    dirs.clear()
                    continue
                dirs[:] = [d for d in dirs if _is_safe_file_path(Path(current) / d, "search")]

                try:
                    from tools.system_tools import is_desktop_path
                    if is_desktop_path(current) or is_desktop_path(root):
                        dirs.clear()  # Desktop barrier: Never search into subfolders on Desktop
                except Exception:
                    pass

                dirs[:] = [
                    d
                    for d in dirs
                    if (
                        d.lower()
                        not in SKIP_DIRS
                        and not d.startswith(".")
                    )
                ]

                for filename in files:

                    path = (
                        Path(current)
                        / filename
                    )

                    # File type filter
                    if file_type:

                        if (
                            path.suffix.lower()
                            not in FILE_TYPES[
                                file_type
                            ]
                        ):
                            continue

                    filename_lower = (
                        filename.lower()
                    )

                    # ------------------------------------------------
                    # Exact filename
                    # ------------------------------------------------

                    if (
                        not type_only
                        and filename_lower
                        == search_query.lower()
                    ):

                        print(
                            f"📄 Exact file match → {path}"
                        )

                        return path

                    # ------------------------------------------------
                    # Partial filename
                    # ------------------------------------------------

                    if (
                        not type_only
                        and search_query.lower()
                        in filename_lower
                    ):

                        candidates.append(
                            path
                        )

                    # ------------------------------------------------
                    # Type-only
                    # ------------------------------------------------

                    elif type_only:

                        candidates.append(
                            path
                        )

                    # ------------------------------------------------
                    # General fuzzy candidates
                    # ------------------------------------------------

                    elif path not in candidates:

                        candidates.append(
                            path
                        )

                    if len(candidates) >= 50000:
                        break

                if len(candidates) >= 50000:
                    break

        except (
            PermissionError,
            OSError,
        ):

            continue

        # --------------------------------------------------------
        # Strong partial match already found
        # --------------------------------------------------------

        if candidates:

            partial_candidates = [
                path
                for path in candidates
                if (
                    search_query.lower()
                    in path.name.lower()
                )
            ]

            if partial_candidates:

                # Prefer shorter filename/path
                best = min(
                    partial_candidates,
                    key=lambda p: (
                        len(p.name),
                        len(str(p)),
                    ),
                )

                print(
                    f"📄 Partial file match → {best}"
                )

                return best

    # ========================================================
    # TYPE-ONLY SEARCH
    # ========================================================

    if type_only and candidates:

        # Prefer files in common user folders
        # and shorter paths.

        candidates.sort(
            key=lambda p: (
                len(str(p)),
                len(p.name),
            )
        )

        result = candidates[0]

        print(
            f"📄 File type match → {result}"
        )

        return result

    # ========================================================
    # FUZZY SEARCH
    # ========================================================

    result = _best_fuzzy_match(
        search_query,
        candidates,
        minimum_score=190,
    )

    if result:
        return result

    return None


# ============================================================
# FIND FOLDER
# ============================================================

def find_folder(query: str):

    original_query = _clean_query(
        query
    )

    if not original_query:
        return None

    search_query = _extract_search_query(
        original_query
    )

    if not search_query:
        search_query = original_query

    target = search_query.lower()

    # Known aliases
    for alias, path in KNOWN_FOLDERS.items():

        if (
            target == alias
            or original_query.lower() == alias
        ):

            if path.exists() and _is_safe_file_path(path, "search"):
                return path

    # Direct path
    try:

        candidate = Path(
            original_query
        ).expanduser()

        if candidate.is_dir() and _is_safe_file_path(candidate, "search"):
            return candidate

    except Exception:
        pass

    # Home-relative
    try:

        candidate = (
            HOME / original_query
        )

        if candidate.is_dir() and _is_safe_file_path(candidate, "search"):
            return candidate

    except Exception:
        pass

    # Exact folder
    for root in _roots():

        try:

            for path in _iter_children(root):

                if not path.is_dir():
                    continue

                if (
                    path.name.lower()
                    == target
                ):

                    print(
                        f"📁 Exact folder match → {path}"
                    )

                    return path

        except (
            PermissionError,
            OSError,
        ):

            continue

    # Recursive search
    candidates = []

    for root in _roots():

        try:

            for current, dirs, _files in os.walk(root):

                if not _is_safe_file_path(current, "search"):
                    dirs.clear()
                    continue
                dirs[:] = [d for d in dirs if _is_safe_file_path(Path(current) / d, "search")]

                dirs[:] = [
                    d
                    for d in dirs
                    if (
                        d.lower()
                        not in SKIP_DIRS
                        and not d.startswith(".")
                    )
                ]

                for directory in dirs:

                    path = (
                        Path(current)
                        / directory
                    )

                    # Exact
                    if (
                        directory.lower()
                        == target
                    ):

                        print(
                            f"📁 Folder found → {path}"
                        )

                        return path

                    candidates.append(
                        path
                    )

                    if len(candidates) >= 50000:
                        break

                if len(candidates) >= 50000:
                    break

        except (
            PermissionError,
            OSError,
        ):

            continue

    # Partial
    partial = [
        path
        for path in candidates
        if target in path.name.lower()
    ]

    if partial:

        result = min(
            partial,
            key=lambda p: (
                len(p.name),
                len(str(p)),
            ),
        )

        print(
            f"📁 Partial folder match → {result}"
        )

        return result

    # Fuzzy
    return _best_fuzzy_match(
        search_query,
        candidates,
        minimum_score=200,
    )


# ============================================================
# SMART FIND
# ============================================================

def smart_find(query: str):

    query = _clean_query(query)

    if not query:
        return None

    # File first
    file_result = find_file(
        query
    )

    if file_result:
        return file_result

    # Folder second
    folder_result = find_folder(
        query
    )

    if folder_result:
        return folder_result

    return None


# ============================================================
# MEDIA CHECK
# ============================================================

def is_media_file(path: Path):

    return (
        path.suffix.lower()
        in MEDIA_EXTENSIONS
    )


# ============================================================
# UNIVERSAL FILE / FOLDER COMMAND HANDLER
# ============================================================

def handle_file_system_command(
    command: str
):

    text = _clean_query(
        command
    )

    if not text:
        return None

    # ========================================================
    # MEDIA
    # ========================================================

    media_match = re.match(
        r"^(?:play|watch|listen to|listen)\s+(.+)$",
        text,
        re.IGNORECASE,
    )

    if media_match:

        query = _clean_query(
            media_match.group(1)
        )

        if not query:
            return None

        path = find_file(query)

        if path and path.is_file() and is_media_file(path):
            return _open_path(path)

        return (
            f"I couldn't find a video or audio file matching "
            f"{query} on this PC."
        )

    # ========================================================
    # OPEN / GO TO / NAVIGATE
    # ========================================================

    path_match = re.match(
        r"^(?:open|go to|navigate to|access)\s+(.+)$",
        text,
        re.IGNORECASE,
    )

    if not path_match:
        return None

    query = _clean_query(
        path_match.group(1)
    )

    if not query:
        return None

    if re.fullmatch(r"(?:the\s+)?windows(?:\s+folder)?", query, re.I):
        windows_path = Path(os.environ.get("WINDIR", r"C:\Windows"))
        return _open_path(windows_path)
    if re.search(r"\bc\s*drive\b", query, re.I):
        from tools.system_tools import UNSAFE_PATH_MESSAGE
        return UNSAFE_PATH_MESSAGE
    explicit_path = Path(query).expanduser()
    if (explicit_path.is_absolute() or re.match(r"^[A-Za-z]:[\\/]", query)) and not _is_safe_file_path(
        explicit_path, "open_readonly", allow_windows_navigation=True
    ):
        from tools.system_tools import UNSAFE_PATH_MESSAGE
        return UNSAFE_PATH_MESSAGE

    # ========================================================
    # DIRECT PATH
    # ========================================================

    try:

        candidate = Path(
            query
        ).expanduser()

        if candidate.exists():

            return _open_path(
                candidate
            )

    except Exception:
        pass

    # ========================================================
    # FILE FIRST
    # ========================================================

    file_path = find_file(
        query
    )

    if file_path:

        return _open_path(
            file_path
        )

    # ========================================================
    # IMPORTANT:
    #
    # If the request explicitly asks for a file type,
    # DO NOT fall back to an unrelated folder.
    #
    # Example:
    #
    # "open video in Downloads"
    #
    # should NOT open:
    #
    # Downloads\video\
    #
    # if there is no video file.
    # ========================================================

    requested_type = _detect_file_type(
        query
    )

    if requested_type:

        return (
            f"I couldn't find a "
            f"{requested_type} file "
            f"matching that request."
        )

    # ========================================================
    # FOLDER SECOND
    # ========================================================

    folder = find_folder(
        query
    )

    if folder:

        return _open_path(
            folder
        )

    return None


# ============================================================
# TEST
# ============================================================

if __name__ == "__main__":

    print("=" * 65)
    print(
        "       JARVIS SMART FILE SYSTEM TEST"
    )
    print("=" * 65)

    tests = [
        "main.py",
        "main",
        "system tools",
        "my python file",
        "pdf",
        "Downloads",
        "video in Downloads",
        "Jarvis",
        "Jarvis folder",
    ]

    for test in tests:

        print()
        print(
            f"Testing: {test}"
        )

        result = smart_find(
            test
        )

        if result:

            print(
                f"FOUND → {result}"
            )

        else:

            print(
                "NOT FOUND"
            )
