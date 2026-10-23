"""
Gesture Engine for Jarvis HUD & Assistant.
Monitors optical sensor feed, identifies hand gestures via MediaPipe,
and executes mapped directives according to gesture_settings.json.
"""

from __future__ import annotations

import json
import sqlite3
import os
import time
import math
import re
import threading
from pathlib import Path
from typing import List, Optional
from core.config_store import save_json_atomic

ROOT = Path(__file__).resolve().parent.parent
GESTURE_CONFIG_PATH = ROOT / "config" / "gesture_settings.json"
MODEL_PATH = ROOT / "models" / "gesture_recognizer.task"
_PENDING_GESTURE: tuple[str, str] | None = None
_CONFIRMATION_ACTIONS = (
    "lock",
    "close",
    "exit",
    "stop",
    "defense",
    "matrix",
    "automation",
    "shutdown",
)
_DIRECT_GESTURE_ACTIONS = {
    "exit jarvis",
    "quit jarvis",
    "shutdown jarvis",
    "close jarvis",
}

DEFAULT_GESTURE_CONFIG = {
    "camera_tracking_enabled": True,
    "camera_device": "integrated",
    "camera_inverted": True,
    "confidence_threshold": 0.80,
    "camera_width": 320,
    "camera_height": 240,
    "camera_fps": 20,
    "recognition_fps": 12,
    "max_hands": 2,
    "custom_gestures": [],
    "app_gesture_mappings": [],
    "default_mappings": {
        "Swipe Left": "Switch Desktop / Previous Tab",
        "Swipe Right": "Next Tab / Forward Navigation",
        "Two-Finger Pinch": "Minimize Windows",
        "Thumbs Up": "Volume Up",
        "Thumbs Down": "Volume Down",
        "Fist": "Pause / Resume Media",
        "Pointing": "Show Desktop",
        "Peace Sign": "Take Screenshot",
        "Iron Man Repulsor Aim": "Toggle Defense Matrix",
        "Open Palm": "Mute / Stop J.A.R.V.I.S.",
        "Two Hands": "Exit Jarvis",
    },
    "hand_sign_mappings": {
        "1:ILoveYou": "Open Calculator",
        "2:Open_Palm+Open_Palm": "Exit Jarvis",
    },
}


def _gesture_key(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _clean_gesture_name(val: str) -> str:
    """Normalize gesture/pose name by stripping emoji prefixes and punctuation."""
    s = str(val or "").strip()
    s = re.sub(r"^[^\w\d]+", "", s).strip()
    return " ".join(s.casefold().split())


def normalize_action_str(action: str) -> str:
    """Normalize action string for comparison: case-insensitive, normalized whitespace and slashes."""
    if not action:
        return ""
    text = " ".join(str(action).casefold().split())
    return text.replace("/", "\\")


def normalize_gesture_name(name: str) -> str:
    """Normalize gesture name for comparison: case-insensitive, trimmed whitespace."""
    return " ".join(str(name or "").casefold().split())


def check_duplicate_gesture(
    name: str,
    action: str,
    config: dict,
    exclude_name: str | None = None,
) -> tuple[str | None, str | None]:
    """Check if a gesture name or action already exists across custom gestures,
    hand sign mappings, or default mappings.
    Returns (name_error, action_error).
    """
    name_norm = normalize_gesture_name(name)
    action_norm = normalize_action_str(action)
    exclude_norm = normalize_gesture_name(exclude_name) if exclude_name else None

    name_error = None
    action_error = None

    if name_norm:
        for item in config.get("custom_gestures", []):
            if not isinstance(item, dict):
                continue
            item_name = str(item.get("name", "")).strip()
            item_name_norm = normalize_gesture_name(item_name)
            if exclude_norm and item_name_norm == exclude_norm:
                continue
            if item_name_norm == name_norm:
                name_error = f"Gesture '{name}' already exists in custom gestures."
                break

        if not name_error:
            for sign in (config.get("hand_sign_mappings") or {}):
                sign_norm = normalize_gesture_name(sign)
                if exclude_norm and sign_norm == exclude_norm:
                    continue
                if sign_norm == name_norm:
                    name_error = f"Gesture '{name}' already exists in hand sign mappings."
                    break

        if not name_error:
            for def_name in (config.get("default_mappings") or {}):
                def_norm = normalize_gesture_name(def_name)
                if exclude_norm and def_norm == exclude_norm:
                    continue
                if def_norm == name_norm:
                    name_error = f"Gesture '{name}' conflicts with built-in gesture '{def_name}'."
                    break

    if action_norm:
        for item in config.get("custom_gestures", []):
            if not isinstance(item, dict):
                continue
            item_name = str(item.get("name", "")).strip()
            item_name_norm = normalize_gesture_name(item_name)
            if exclude_norm and item_name_norm == exclude_norm:
                continue
            item_action = str(item.get("action", "")).strip()
            if item_action and normalize_action_str(item_action) == action_norm:
                action_error = f"Action '{action}' is already assigned to custom gesture '{item_name}'."
                break

        if not action_error:
            for sign, mapped_action in (config.get("hand_sign_mappings") or {}).items():
                if exclude_norm and normalize_gesture_name(sign) == exclude_norm:
                    continue
                if name_norm and normalize_gesture_name(sign) == name_norm:
                    continue
                if mapped_action and normalize_action_str(mapped_action) == action_norm:
                    action_error = f"Action '{action}' is already assigned to hand sign '{sign}'."
                    break

        if not action_error:
            for def_name, def_action in (config.get("default_mappings") or {}).items():
                if exclude_norm and normalize_gesture_name(def_name) == exclude_norm:
                    continue
                if def_action and normalize_action_str(def_action) == action_norm:
                    action_error = f"Action '{action}' is already assigned to built-in gesture '{def_name}'."
                    break

    return name_error, action_error



def get_camera_runtime_config(config: dict) -> dict:
    """Return bounded camera settings shared by the engine and preview."""
    try:
        width = max(160, min(1280, int(config.get("camera_width", 320))))
    except (TypeError, ValueError):
        width = 320
    try:
        height = max(120, min(720, int(config.get("camera_height", 240))))
    except (TypeError, ValueError):
        height = 240
    try:
        camera_fps = max(5, min(60, int(config.get("camera_fps", 20))))
    except (TypeError, ValueError):
        camera_fps = 20
    try:
        recognition_fps = max(5, min(camera_fps, int(config.get("recognition_fps", 12))))
    except (TypeError, ValueError):
        recognition_fps = min(camera_fps, 12)
    configured_hands = config.get("max_hands", 2)
    try:
        max_hands = max(1, min(2, int(configured_hands)))
    except (TypeError, ValueError):
        max_hands = 2
    has_two_hand_mapping = any(
        str(key).strip().startswith("2:")
        for key in (config.get("hand_sign_mappings") or {})
    )
    has_two_hand_app_mapping = any(
        isinstance(item, dict)
        and (
            str(item.get("gesture", "")).strip().casefold().startswith("hand sign 2:")
            or isinstance(item.get("landmark_template"), list)
            and len(item["landmark_template"]) == 126
        )
        for item in config.get("app_gesture_mappings", [])
    )
    has_two_hand_custom = any(
        isinstance(item, dict)
        and isinstance(item.get("landmark_template"), list)
        and len(item["landmark_template"]) == 126
        for item in config.get("custom_gestures", [])
    )
    return {
        "width": width,
        "height": height,
        "camera_fps": camera_fps,
        "recognition_fps": recognition_fps,
        "max_hands": 2 if has_two_hand_mapping or has_two_hand_app_mapping or has_two_hand_custom else min(1, max_hands),
    }


def _hand_sign_key(result) -> str:
    """Build a stable multi-hand key, preferring left-to-right ordering."""
    hands = []
    for index, categories in enumerate(result.gestures or []):
        if not categories:
            continue
        label = categories[0].category_name or ""
        landmarks = result.hand_landmarks[index] if index < len(result.hand_landmarks) else []
        x_position = landmarks[0].x if landmarks else float(index)
        handedness = ""
        if index < len(result.handedness) and result.handedness[index]:
            handedness = str(result.handedness[index][0].category_name or "").casefold()
        side_order = 0 if handedness == "left" else 1 if handedness == "right" else 2
        hands.append((side_order, x_position, label))
    hands.sort(key=lambda item: (item[0], item[1]))
    return f"{len(hands)}:{'+'.join(item[2] for item in hands)}"


def load_gesture_config() -> dict:
    config = {}
    if GESTURE_CONFIG_PATH.exists():
        try:
            with open(GESTURE_CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            print(f"[GestureEngine] Error loading gesture_settings.json: {e}")
    if not isinstance(config, dict):
        config = {}
    if not config:
        config = json.loads(json.dumps(DEFAULT_GESTURE_CONFIG))
    else:
        defaults = json.loads(json.dumps(DEFAULT_GESTURE_CONFIG))
        defaults.update(config)
        config = defaults
    # Older settings treated an open palm push as a separate pose. Keep its
    # action only when Open Palm has no saved action, then retire the alias.
    default_mappings = config.get("default_mappings")
    if isinstance(default_mappings, dict) and "Palm Push" in default_mappings:
        legacy_action = default_mappings.pop("Palm Push")
        default_mappings.setdefault("Open Palm", legacy_action)
    if not isinstance(config.get("app_gesture_mappings"), list):
        config["app_gesture_mappings"] = []
    try:
        from core.database import load_gesture_data
        hand_signs, custom = load_gesture_data()
        config["hand_sign_mappings"] = hand_signs
        config["custom_gestures"] = custom
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(f"[GestureEngine] Could not load SQLite gesture data: {error}")
    return config


def save_gesture_config(config: dict) -> dict:
    """Validate and atomically persist gesture configuration."""
    if not isinstance(config, dict):
        raise ValueError("Gesture configuration must be an object.")
    default_mappings = config.get("default_mappings")
    if isinstance(default_mappings, dict) and "Palm Push" in default_mappings:
        legacy_action = default_mappings.pop("Palm Push")
        default_mappings.setdefault("Open Palm", legacy_action)
    confidence = config.get("confidence_threshold", 0.8)
    if not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0.0 < confidence <= 1.0:
        raise ValueError("confidence_threshold must be a finite number between 0 and 1.")
    if not isinstance(config.get("default_mappings", {}), dict):
        raise ValueError("default_mappings must be an object.")
    hand_signs = config.get("hand_sign_mappings", {})
    if not isinstance(hand_signs, dict):
        raise ValueError("hand_sign_mappings must be an object.")
    for sign, action in hand_signs.items():
        if not str(sign).strip() or not str(action).strip():
            raise ValueError("Hand sign mappings require a sign and action.")
    custom = config.get("custom_gestures", [])
    if not isinstance(custom, list):
        raise ValueError("custom_gestures must be a list.")
    app_mappings = config.get("app_gesture_mappings", [])
    if not isinstance(app_mappings, list):
        raise ValueError("app_gesture_mappings must be a list.")
    seen_app_gestures = set()
    normalized_app_mappings = []
    for item in app_mappings:
        if not isinstance(item, dict):
            raise ValueError("Each app gesture mapping must be an object.")
        app = str(item.get("app", "")).strip()
        gesture = str(item.get("gesture", "")).strip()
        action = str(item.get("action", "")).strip()
        if not app or not gesture or not action:
            raise ValueError("App gesture mappings require an app, gesture, and action.")
        key = (_gesture_key(app), _gesture_key(gesture))
        if key in seen_app_gestures:
            raise ValueError(f"Duplicate app gesture mapping: {app} / {gesture}.")
        seen_app_gestures.add(key)
        normalized_item = {"app": app, "gesture": gesture, "action": action}
        template = item.get("landmark_template")
        if template is not None:
            if (
                not isinstance(template, list)
                or len(template) not in {63, 126}
                or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in template)
            ):
                raise ValueError("An app-scoped gesture template must contain 63 or 126 finite numeric values.")
            normalized_item["landmark_template"] = template
        normalized_app_mappings.append(normalized_item)
    config["app_gesture_mappings"] = normalized_app_mappings
    names = set()
    actions = set()
    for item in custom:
        if not isinstance(item, dict):
            raise ValueError("Each custom gesture must be an object.")
        name = str(item.get("name", "")).strip()
        if not name:
            raise ValueError("Each custom gesture must have a name.")
        key = _gesture_key(name)
        if key in names:
            raise ValueError(f"Duplicate custom gesture name: {name}")
        names.add(key)
        action = str(item.get("action", "")).strip()
        if not action:
            raise ValueError("Each custom gesture must have an action.")
        action_key = normalize_action_str(action)
        if action_key in actions:
            raise ValueError(f"Duplicate custom gesture action: {action}")
        actions.add(action_key)
        template = item.get("landmark_template")
        if template is not None and (
            not isinstance(template, list)
            or len(template) not in {63, 126}
            or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in template)
        ):
            raise ValueError("A custom landmark template must contain 63 or 126 finite numeric values.")
    from core.database import save_gesture_data
    save_gesture_data(config.get("hand_sign_mappings", {}), config.get("custom_gestures", []))
    json_config = dict(config)
    json_config.pop("hand_sign_mappings", None)
    json_config.pop("custom_gestures", None)
    save_json_atomic(GESTURE_CONFIG_PATH, json_config)
    return config


def set_camera_tracking_enabled(enabled: bool) -> bool:
    """Persist and apply the camera gesture tracking switch immediately."""
    config = load_gesture_config()
    config["camera_tracking_enabled"] = bool(enabled)
    save_gesture_config(config)
    engine = GestureEngine.get_instance()
    engine.reload_settings()
    return bool(engine._config.get("camera_tracking_enabled", False))


def execute_gesture_action(action: str) -> bool:
    """Execute one or more gesture actions separated by ``+``."""
    actions = [part.strip() for part in str(action or "").split("+") if part.strip()]
    if not actions:
        return False
    # Jarvis exit terminates this process, so run it after any other requested
    # actions (such as launching an application) even when it appears first.
    exit_actions = {"exit jarvis", "quit jarvis", "shutdown jarvis", "close jarvis"}
    requested_exits = [part for part in actions if part.casefold() in exit_actions]
    other_actions = [part for part in actions if part.casefold() not in exit_actions]
    results = [_execute_single_gesture_action(part) for part in other_actions]
    if requested_exits and not all(results):
        print("[GestureEngine] Jarvis exit skipped because another gesture action failed.")
        return False
    results.extend(_execute_single_gesture_action(part) for part in requested_exits)
    return all(results)


def _execute_single_gesture_action(action: str) -> bool:
    action_text = str(action or "").strip()
    action_clean = action_text.lower()
    if not action_clean:
        return False

    import ctypes
    user32 = ctypes.windll.user32

    def send_key(vk, extended=False):
        flags = 1 if extended else 0
        user32.keybd_event(vk, 0, flags, 0)
        time.sleep(0.04)
        user32.keybd_event(vk, 0, flags | 2, 0)

    def send_combo(mod_vk, vk):
        user32.keybd_event(mod_vk, 0, 0, 0)
        time.sleep(0.03)
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.04)
        user32.keybd_event(vk, 0, 2, 0)
        user32.keybd_event(mod_vk, 0, 2, 0)

    def send_win_combo(vk):
        VK_LWIN = 0x5B
        send_combo(VK_LWIN, vk)

    VK_VOLUME_MUTE = 0xAD
    VK_VOLUME_DOWN = 0xAE
    VK_VOLUME_UP = 0xAF
    VK_TAB = 0x09
    VK_MEDIA_PLAY_PAUSE = 0xB3
    VK_UP = 0x26

    def launch_executable(target_path: Path) -> str:
        """Launch a configured executable with its own folder as the working directory."""
        import subprocess

        shell_error = None
        try:
            shell_execute = ctypes.WinDLL("shell32", use_last_error=True).ShellExecuteW
            shell_execute.argtypes = [
                ctypes.c_void_p,
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_int,
            ]
            shell_execute.restype = ctypes.c_void_p
            result = shell_execute(
                None,
                "open",
                str(target_path),
                None,
                str(target_path.parent),
                1,
            )
            if result is not None and int(result) > 32:
                return "Windows ShellExecute"
            shell_error = OSError(
                f"ShellExecuteW returned error code {int(result or 0)}"
            )
        except Exception as error:
            shell_error = error

        try:
            subprocess.Popen(
                [str(target_path)],
                cwd=str(target_path.parent),
                close_fds=True,
                shell=False,
                creationflags=(
                    getattr(subprocess, "DETACHED_PROCESS", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                ),
            )
            return "detached process"
        except OSError as process_error:
            raise OSError(
                f"ShellExecute failed ({shell_error}); process launch failed ({process_error})"
            ) from process_error

    if action_clean in {"exit jarvis", "quit jarvis", "shutdown jarvis", "close jarvis"}:
        try:
            from core.events import event_bus
            event_bus.publish("jarvis_exit_requested", source="gesture")
            return True
        except ImportError as error:
            print(f"[GestureEngine] Could not request Jarvis exit: {error}")
            return False

    file_match = re.match(
        r"^(?:open|launch|start)\s+file\s*:\s*(.+)$",
        action_text,
        flags=re.IGNORECASE,
    )
    if file_match:
        target_path = Path(os.path.expandvars(os.path.expanduser(file_match.group(1).strip())))
        if not target_path.exists():
            print(f"[GestureEngine] Configured gesture target does not exist: {target_path}")
            return False
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            print("[GestureEngine] File and executable gesture targets require Windows.")
            return False
        try:
            startfile(str(target_path))
            return True
        except OSError as error:
            print(f"[GestureEngine] Could not open configured target {target_path}: {error}")
            return False

    app_path_match = re.match(
        r"^(?:open|launch|start)\s+app\s*:\s*(.+)$",
        action_text,
        flags=re.IGNORECASE,
    )
    if app_path_match:
        target_str = app_path_match.group(1).strip()
        target_path = Path(os.path.expandvars(os.path.expanduser(target_str)))
        if target_path.exists():
            if target_path.suffix.casefold() == ".exe":
                try:
                    method = launch_executable(target_path)
                    print(
                        f"[GestureEngine] Launched gesture application: {target_path} "
                        f"(working directory: {target_path.parent}; {method})"
                    )
                    return True
                except Exception as error:
                    print(f"[GestureEngine] Could not launch application {target_path}: {error}")
                    return False
            startfile = getattr(os, "startfile", None)
            if startfile:
                try:
                    startfile(str(target_path))
                    return True
                except OSError as error:
                    print(f"[GestureEngine] Could not open configured target {target_path}: {error}")
                    return False

    # Direct file or application path (e.g. C:\Games\osu.exe or "C:\Docs\file.pdf")
    raw_path_candidate = action_text.strip().strip('"').strip("'")
    if (len(raw_path_candidate) >= 3 and raw_path_candidate[1:3] in {":\\", ":/"}) or raw_path_candidate.startswith(("\\\\", "//")):
        candidate_path = Path(os.path.expandvars(os.path.expanduser(raw_path_candidate)))
        if candidate_path.exists():
            if candidate_path.suffix.casefold() == ".exe":
                try:
                    method = launch_executable(candidate_path)
                    print(
                        f"[GestureEngine] Launched gesture application: {candidate_path} "
                        f"(working directory: {candidate_path.parent}; {method})"
                    )
                    return True
                except Exception as error:
                    print(f"[GestureEngine] Could not launch application {candidate_path}: {error}")
                    return False
            startfile = getattr(os, "startfile", None)
            if startfile:
                try:
                    startfile(str(candidate_path))
                    return True
                except OSError as error:
                    print(f"[GestureEngine] Could not open target path {candidate_path}: {error}")
                    return False

    # Embedded file or application path (e.g. "Exit Jarvis C:\Games\osu.exe")
    embedded_path_match = re.search(r'([A-Za-z]:\\[^"\'\r\n\t]+?\.[a-zA-Z0-9_!]+)', action_text)
    if embedded_path_match:
        found_path_str = embedded_path_match.group(1).strip()
        candidate = Path(os.path.expandvars(os.path.expanduser(found_path_str)))
        if candidate.exists():
            other_text = action_text.replace(found_path_str, "").strip()
            if other_text and other_text != action_text:
                _execute_single_gesture_action(other_text)
            if candidate.suffix.casefold() == ".exe":
                try:
                    method = launch_executable(candidate)
                    print(
                        f"[GestureEngine] Launched gesture application: {candidate} "
                        f"(working directory: {candidate.parent}; {method})"
                    )
                    return True
                except Exception as error:
                    print(f"[GestureEngine] Could not launch application {candidate}: {error}")
                    return False
            startfile = getattr(os, "startfile", None)
            if startfile:
                try:
                    startfile(str(candidate))
                    return True
                except OSError as error:
                    print(f"[GestureEngine] Could not open embedded target {candidate}: {error}")
                    return False

    app_match = re.match(
        r"^(?:open|launch|start)\s+(?:app\s*:\s*|app\s+)?(.+)$",
        action_text,
        flags=re.IGNORECASE,
    )
    if app_match:
        from tools.app_tools import load_aliases
        from tools.window_tools import open_app
        target = app_match.group(1).strip().strip('"').strip("'")
        target_path = Path(os.path.expandvars(os.path.expanduser(target)))
        if target_path.exists():
            startfile = getattr(os, "startfile", None)
            if startfile is None:
                print("[GestureEngine] File and executable gesture targets require Windows.")
                return False
            try:
                startfile(str(target_path))
                return True
            except OSError as error:
                print(f"[GestureEngine] Could not open configured target {target_path}: {error}")
                return False
        # Gesture actions often store executable names such as
        # "open chrome.exe". Treat a bare executable name as its app alias so
        # it can pass the same allowlist check as "open chrome". Explicit paths
        # have already been handled above and keep their path semantics.
        app_target = target
        if not any(separator in app_target for separator in ("\\", "/")) and app_target.casefold().endswith(".exe"):
            app_target = app_target[:-4].strip()
        aliases = load_aliases()
        canonical = str(aliases.get(app_target, aliases.get(app_target.casefold(), app_target))).casefold()
        # Gesture actions are explicitly configured by the user. Resolve them
        # through Jarvis's installed-app catalog so custom app names are not
        # limited to the built-in voice-command allowlist.
        try:
            result = open_app(canonical)
        except Exception as error:
            print(f"[GestureEngine] Could not open configured app {app_target}: {error}")
            return False
        return bool(result and "couldn't" not in result.casefold() and "failed" not in result.casefold())

    if "previous tab" in action_clean or "switch desktop" in action_clean:
        user32.keybd_event(0x11, 0, 0, 0)  # Ctrl
        user32.keybd_event(0x10, 0, 0, 0)  # Shift
        user32.keybd_event(VK_TAB, 0, 0, 0)
        time.sleep(0.04)
        user32.keybd_event(VK_TAB, 0, 2, 0)
        user32.keybd_event(0x10, 0, 2, 0)
        user32.keybd_event(0x11, 0, 2, 0)
        return True

    elif "next tab" in action_clean or "forward" in action_clean:
        send_combo(0x11, VK_TAB)  # Ctrl+Tab
        return True

    elif action_clean in {"scroll up", "scroll down"}:
        # Chrome-scoped wheel actions. Windows defines positive delta as up.
        user32.mouse_event(0x0800, 0, 0, 120 if action_clean == "scroll up" else -120, 0)
        return True

    elif action_clean in {"mouse pointer moving", "move mouse pointer"}:
        # Continuous pointer tracking is handled from the camera loop while
        # this Chrome-scoped rule is active.
        return True

    elif "volume up" in action_clean:
        for _ in range(3):
            send_key(VK_VOLUME_UP)
            time.sleep(0.02)
        return True

    elif "volume down" in action_clean:
        for _ in range(3):
            send_key(VK_VOLUME_DOWN)
            time.sleep(0.02)
        return True

    elif "mute" in action_clean or "stop" in action_clean:
        try:
            from core.events import event_bus
            event_bus.publish("stop_speaking")
        except Exception:
            pass
        send_key(VK_VOLUME_MUTE)
        return True

    elif "minimize" in action_clean:
        send_win_combo(0x44)  # Win+D
        return True

    elif "maximize" in action_clean:
        send_win_combo(VK_UP)
        return True

    elif "show desktop" in action_clean:
        send_win_combo(0x44)  # Win+D
        return True

    elif "play" in action_clean or "pause" in action_clean or "resume" in action_clean:
        send_key(VK_MEDIA_PLAY_PAUSE)
        return True

    elif "screenshot" in action_clean or "screen capture" in action_clean:
        user32.keybd_event(0x5B, 0, 0, 0)
        user32.keybd_event(0x10, 0, 0, 0)
        user32.keybd_event(0x53, 0, 0, 0)
        time.sleep(0.04)
        user32.keybd_event(0x53, 0, 2, 0)
        user32.keybd_event(0x10, 0, 2, 0)
        user32.keybd_event(0x5B, 0, 2, 0)
        return True

    elif "dismiss" in action_clean or "close" in action_clean:
        send_combo(0x12, 0x73)  # Alt+F4
        return True

    elif "terminal" in action_clean:
        import subprocess
        try:
            subprocess.Popen(["wt.exe"])
        except Exception:
            subprocess.Popen(["cmd.exe", "/c", "start"])
        return True

    elif "snapshot" in action_clean or "hologram" in action_clean:
        user32.keybd_event(0x5B, 0, 0, 0)  # Win
        user32.keybd_event(0x10, 0, 0, 0)  # Shift
        user32.keybd_event(0x53, 0, 0, 0)  # S
        time.sleep(0.04)
        user32.keybd_event(0x53, 0, 2, 0)
        user32.keybd_event(0x10, 0, 2, 0)
        user32.keybd_event(0x5B, 0, 2, 0)
        return True

    elif "lockdown" in action_clean or "screen lock" in action_clean:
        user32.LockWorkStation()
        return True

    elif "arc shield" in action_clean or "defense matrix" in action_clean:
        try:
            from core.events import event_bus
            event_bus.publish("defense_matrix_triggered", active=True)
        except Exception:
            pass
        return True

    try:
        from core.command_engine import execute_command_chain
        res = execute_command_chain(action_text)
        if res is not None:
            return True
    except Exception as e:
        pass

    # Direct app name launch fallback (e.g. "osu", "spotify", "notepad")
    try:
        from tools.window_tools import open_app
        app_res = open_app(action_text)
        if app_res and "couldn't" not in app_res.casefold() and "failed" not in app_res.casefold():
            return True
    except Exception:
        pass

    return False


def confirm_pending_gesture(approved: bool) -> bool:
    """Execute a pending sensitive gesture only after explicit approval."""
    global _PENDING_GESTURE
    pending = _PENDING_GESTURE
    _PENDING_GESTURE = None
    if not approved or pending is None:
        return False
    return execute_gesture_action(pending[1])


def normalize_landmarks(landmarks) -> list[float]:
    """Return translation- and scale-normalized 3D hand landmarks for matching."""
    if not landmarks or len(landmarks) < 21:
        return []
    wrist = landmarks[0]
    origin = (float(wrist.x), float(wrist.y), float(wrist.z))
    points = [
        (float(point.x) - origin[0], float(point.y) - origin[1], float(point.z) - origin[2])
        for point in landmarks[:21]
    ]
    scale = math.sqrt(sum(value * value for value in points[9]))
    if scale < 1e-6:
        return []
    return [value / scale for point in points for value in point]


def normalize_landmark_hands(hand_landmarks, handedness=None) -> list[float]:
    """Normalize one or two hands in a stable left-to-right order."""
    if not hand_landmarks:
        return []
    ordered = []
    for index, landmarks in enumerate(hand_landmarks[:2]):
        wrist_x = landmarks[0].x if landmarks else float(index)
        hand_side = ""
        if handedness and index < len(handedness) and handedness[index]:
            hand_side = str(handedness[index][0].category_name or "").casefold()
        side_order = 0 if hand_side == "left" else 1 if hand_side == "right" else 2
        ordered.append((side_order, wrist_x, landmarks))
    ordered.sort(key=lambda entry: (entry[0], entry[1]))
    normalized = [normalize_landmarks(landmarks) for _, _, landmarks in ordered]
    if any(len(hand) != 63 for hand in normalized):
        return []
    return [value for hand in normalized for value in hand]


def match_custom_landmarks(landmarks, custom_gestures, threshold=0.16, min_confidence=0.70) -> str | None:
    """Match only complete 21-point hand landmark samples to custom poses."""
    sample = normalize_landmarks(landmarks)
    return match_custom_sample(sample, custom_gestures, threshold, min_confidence=min_confidence)


def match_custom_sample(sample, custom_gestures, threshold=0.16, min_confidence=0.70) -> str | None:
    name, _, _ = match_custom_sample_details(sample, custom_gestures, threshold, min_confidence=min_confidence)
    return name


def match_custom_sample_details(
    sample,
    custom_gestures,
    threshold=0.16,
    min_confidence=0.70,
) -> tuple[str | None, float, float]:
    """Match a normalized one- or two-hand sample to a custom pose."""
    if len(sample) not in {63, 126}:
        return None, 0.0, 0.0
    ranked = []
    for item in custom_gestures or []:
        template = item.get("landmark_template") if isinstance(item, dict) else None
        if not isinstance(template, list) or len(template) != len(sample):
            continue
        try:
            error = math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(sample, template)) / len(sample))
        except (TypeError, ValueError):
            continue
        ranked.append((error, str(item.get("name", "")).strip()))
    ranked.sort()
    if not ranked:
        return None, 0.0, 0.0
    closest_error, closest_name = ranked[0]
    confidence = max(0.0, min(1.0, 1.0 - closest_error / max(threshold, 1e-6)))

    if len(ranked) > 1:
        second_error = ranked[1][0]
        ambiguity = max(0.0, min(1.0, 1.0 - max(0.0, second_error - closest_error) / max(threshold, 1e-6)))
    else:
        ambiguity = 0.0

    matched_name = (
        closest_name
        if closest_name and closest_error <= threshold and confidence >= min_confidence
        else None
    )
    if matched_name is None:
        ambiguity = 0.0

    return (
        matched_name,
        confidence,
        ambiguity,
    )


class GestureEngine:
    _instance: Optional["GestureEngine"] = None

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._config = load_gesture_config()
        self._last_trigger_time = 0.0
        self._debounce_interval = 1.4  # seconds between activations
        self._last_gesture_times: dict[str, float] = {}
        self._pending_gesture: str | None = None
        self._pending_frames = 0
        self._stable_frames_required = 3
        self._history: List[dict] = []  # hand position history for swipe/push detection
        self._lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "GestureEngine":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def is_active(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def is_enabled(self) -> bool:
        return bool(self._config.get("camera_tracking_enabled", True))

    def reload_settings(self):
        with self._lock:
            self._config = load_gesture_config()
            tracking_enabled = bool(self._config.get("camera_tracking_enabled", True))

        if tracking_enabled and not self.is_active():
            self.start()
        elif not tracking_enabled and self.is_active():
            self.stop()

    def start(self):
        if self.is_active():
            return
        if not self._config.get("camera_tracking_enabled", True):
            print("[GestureEngine] Camera tracking is disabled in gesture_settings.json.")
            return

        self._running = True
        self._thread = threading.Thread(target=self._worker_loop, daemon=True, name="JarvisGestureEngine")
        self._thread.start()
        print("[GestureEngine] Optical tracking service starting.")

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None
        print("[GestureEngine] Optical tracking service stopped.")

    def _worker_loop(self):
        camera = None
        recognizer = None

        try:
            import cv2
            import mediapipe as mp

            if not MODEL_PATH.is_file():
                print(f"[GestureEngine] Gesture model not found at {MODEL_PATH}")
                self._running = False
                return

            # Prefer the configured camera, then scan common Windows camera
            # indexes so an unavailable/default index does not disable tracking.
            configured_device = self._config.get("camera_device", 0)

            def camera_indices_for(device):
                preferred = 0
                if isinstance(device, int):
                    preferred = max(0, device)
                elif str(device).strip().isdigit():
                    preferred = int(str(device).strip())
                elif str(device).strip().casefold() == "external":
                    preferred = 1
                return list(dict.fromkeys([preferred, 0, 1, 2, 3, 4]))

            camera_indices = camera_indices_for(configured_device)
            opened_camera_index = None

            def open_camera(skip_index=None):
                nonlocal opened_camera_index
                for index in camera_indices:
                    if index == skip_index:
                        continue
                    candidate = cv2.VideoCapture(index, cv2.CAP_DSHOW)
                    if candidate.isOpened():
                        opened_camera_index = index
                        return candidate
                    candidate.release()
                    candidate = cv2.VideoCapture(index)
                    if candidate.isOpened():
                        opened_camera_index = index
                        return candidate
                    candidate.release()
                return cv2.VideoCapture()

            camera = open_camera()
            if not camera.isOpened():
                print(f"[GestureEngine] No optical sensor available; checked camera indexes {camera_indices}.")
                self._running = False
                return
            frame_width = int(camera.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            frame_height = int(camera.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            print(
                f"[GestureEngine] Camera ready: index {opened_camera_index} "
                f"({frame_width}x{frame_height}); show your hand to begin."
            )

            runtime_config = get_camera_runtime_config(self._config)
            camera.set(cv2.CAP_PROP_FRAME_WIDTH, runtime_config["width"])
            camera.set(cv2.CAP_PROP_FRAME_HEIGHT, runtime_config["height"])
            camera.set(cv2.CAP_PROP_FPS, runtime_config["camera_fps"])
            camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            options = mp.tasks.vision.GestureRecognizerOptions(
                base_options=mp.tasks.BaseOptions(model_asset_path=str(MODEL_PATH)),
                running_mode=mp.tasks.vision.RunningMode.VIDEO,
                num_hands=runtime_config["max_hands"],
            )
            recognizer = mp.tasks.vision.GestureRecognizer.create_from_options(options)
            recognizer_max_hands = runtime_config["max_hands"]

            failed_reads = 0
            last_timestamp_ms = 0
            next_recognition = 0.0
            frame_number = 0
            recognition_interval = 1.0 / max(1, runtime_config.get("recognition_fps", 12))
            last_recognition_error_log = 0.0
            hand_was_detected = False

            while self._running:
                latest_device = self._config.get("camera_device", 0)
                if latest_device != configured_device:
                    camera.release()
                    configured_device = latest_device
                    camera_indices = camera_indices_for(configured_device)
                    opened_camera_index = None
                    camera = open_camera()
                    if not camera.isOpened():
                        print(f"[GestureEngine] Selected camera unavailable; checked indexes {camera_indices}.")
                        time.sleep(0.25)
                        continue
                    camera.set(cv2.CAP_PROP_FRAME_WIDTH, runtime_config["width"])
                    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, runtime_config["height"])
                    camera.set(cv2.CAP_PROP_FPS, runtime_config["camera_fps"])
                    camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    print(f"[GestureEngine] Camera selection applied: index {opened_camera_index}.")

                latest_runtime_config = get_camera_runtime_config(self._config)
                if latest_runtime_config["max_hands"] != recognizer_max_hands:
                    recognizer.close()
                    options = mp.tasks.vision.GestureRecognizerOptions(
                        base_options=mp.tasks.BaseOptions(model_asset_path=str(MODEL_PATH)),
                        running_mode=mp.tasks.vision.RunningMode.VIDEO,
                        num_hands=latest_runtime_config["max_hands"],
                    )
                    recognizer = mp.tasks.vision.GestureRecognizer.create_from_options(options)
                    recognizer_max_hands = latest_runtime_config["max_hands"]
                    print(f"[GestureEngine] Recognizer updated for {recognizer_max_hands} hand(s).")

                now = time.monotonic()
                if now < next_recognition:
                    time.sleep(max(0.005, next_recognition - now))
                    continue
                next_recognition = now + recognition_interval

                ok, frame = camera.read()
                if not ok or frame is None:
                    failed_reads += 1
                    if failed_reads >= 15:
                        print("[GestureEngine] Camera frames stopped; rescanning camera indexes.")
                        camera.release()
                        time.sleep(0.25)
                        camera = open_camera(skip_index=opened_camera_index)
                        if camera.isOpened():
                            camera.set(cv2.CAP_PROP_FRAME_WIDTH, runtime_config["width"])
                            camera.set(cv2.CAP_PROP_FRAME_HEIGHT, runtime_config["height"])
                            camera.set(cv2.CAP_PROP_FPS, runtime_config["camera_fps"])
                            camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                            print(f"[GestureEngine] Camera fallback selected: index {opened_camera_index}.")
                        failed_reads = 0
                    time.sleep(0.04)
                    continue
                failed_reads = 0
                frame_number += 1

                if self._config.get("camera_inverted", True):
                    frame = cv2.flip(frame, 1)
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

                ts = int(now * 1000)
                if ts <= last_timestamp_ms:
                    ts = last_timestamp_ms + 1
                last_timestamp_ms = ts

                try:
                    result = recognizer.recognize_for_video(mp_img, ts)
                except Exception as error:
                    if now - last_recognition_error_log >= 5.0:
                        print(f"[GestureEngine] Frame recognition failed: {error}")
                        last_recognition_error_log = now
                    continue
                if result.hand_landmarks and len(result.hand_landmarks) > 0:
                    if not hand_was_detected:
                        print(f"[GestureEngine] Hand landmarks detected: {len(result.hand_landmarks)} hand(s).")
                    hand_was_detected = True
                    landmarks = result.hand_landmarks[0]
                    wrist = landmarks[0]
                    index_tip = landmarks[8]
                    thumb_tip = landmarks[4]

                    # Pinch distance
                    pinch_dist = math.hypot(index_tip.x - thumb_tip.x, index_tip.y - thumb_tip.y)

                    self._history.append({
                        "t": now,
                        "x": wrist.x,
                        "y": wrist.y,
                        "pinch": pinch_dist
                    })

                    # Maintain recent history (last 450ms)
                    self._history = [h for h in self._history if now - h["t"] <= 0.45]

                    detected_gesture = None

                    # A mapping key such as ``2:Open_Palm+Open_Palm`` lets
                    # users assign an action to a specific number of hands
                    # without changing the recognizer model.
                    sign_key = _hand_sign_key(result)
                    hand_sign_action = self._config.get("hand_sign_mappings", {}).get(sign_key)
                    app_scoped_hand_sign = any(
                        isinstance(item, dict)
                        and _gesture_key(item.get("gesture", "")) == _gesture_key(f"Hand Sign {sign_key}")
                        for item in self._config.get("app_gesture_mappings", [])
                    )
                    if hand_sign_action or app_scoped_hand_sign:
                        detected_gesture = f"Hand Sign {sign_key}"

                    conf_threshold = float(self._config.get("confidence_threshold", 0.75))

                    # Check MediaPipe static categories
                    category_name = ""
                    score = 0.0
                    if result.gestures and result.gestures[0]:
                        top_gesture = result.gestures[0][0]
                        category_name = top_gesture.category_name or ""
                        score = float(top_gesture.score or 0.0)

                    standard_gesture = None
                    if score >= conf_threshold:
                        if category_name == "Victory":
                            standard_gesture = "Peace Sign"
                        elif category_name == "ILoveYou":
                            standard_gesture = "Iron Man Repulsor Aim"
                        elif category_name == "Closed_Fist":
                            standard_gesture = "Fist"
                        elif category_name == "Pointing_Up":
                            standard_gesture = "Pointing"
                        elif category_name == "Thumb_Up":
                            standard_gesture = "Thumbs Up"
                        elif category_name == "Thumb_Down":
                            standard_gesture = "Thumbs Down"
                        elif category_name == "Open_Palm":
                            standard_gesture = "Open Palm"

                    custom_sample = normalize_landmark_hands(
                        result.hand_landmarks,
                        getattr(result, "handedness", None),
                    )
                    app_custom_name = None
                    # Capture foreground identity once per frame so the app
                    # scope used to match a template is the same scope used
                    # when dispatching it (the overlay can briefly affect
                    # foreground-window queries).
                    app_identity = self._foreground_app_identity()
                    app_custom_gestures = self._active_app_scoped_custom_gestures(app_identity)
                    app_standard_gesture = (
                        standard_gesture
                        if standard_gesture
                        and self._app_scoped_gesture_action(standard_gesture, app_identity)
                        else None
                    )
                    if app_custom_gestures:
                        app_custom_name, app_custom_conf, _ = match_custom_sample_details(
                            custom_sample,
                            app_custom_gestures,
                            threshold=0.28,
                            min_confidence=0.35,
                        )
                        if app_custom_conf < 0.35:
                            app_custom_name = None
                    custom_name, custom_conf, custom_ambiguity = match_custom_sample_details(
                        custom_sample,
                        self._config.get("custom_gestures", []),
                        min_confidence=0.35,
                    )

                    if app_custom_name and not hand_sign_action:
                        # App-scoped templates are intentional overrides for
                        # gestures used in that app. MediaPipe often labels the
                        # same pose as a generic static gesture (for example
                        # Pointing_Up), which must not mask the Chrome mapping.
                        detected_gesture = app_custom_name
                    elif app_standard_gesture:
                        # A standard pose can have a different action inside
                        # an app while keeping its global action elsewhere.
                        detected_gesture = app_standard_gesture
                    elif not detected_gesture:
                        if custom_name and custom_conf >= 0.35:
                            detected_gesture = custom_name
                        elif standard_gesture:
                            # Check if a custom gesture registered this standard pose
                            custom_pose_match = next((
                                cg.get("name") for cg in self._config.get("custom_gestures", [])
                                if _clean_gesture_name(cg.get("pose", "")) in {_clean_gesture_name(standard_gesture), _gesture_key(standard_gesture)}
                            ), None)
                            detected_gesture = custom_pose_match or standard_gesture

                    # Kinematic Swipes & Dynamic Gestures
                    if not detected_gesture and len(self._history) >= 3:
                        dx = self._history[-1]["x"] - self._history[0]["x"]

                        # In mirrored camera: dx < -0.15 is hand moving West (Swipe Left)
                        if dx < -0.16:
                            detected_gesture = "Swipe Left"
                        elif dx > 0.16:
                            detected_gesture = "Swipe Right"
                        elif pinch_dist < 0.045:
                            detected_gesture = "Two-Finger Pinch"

                    if detected_gesture:
                        if detected_gesture == self._pending_gesture:
                            self._pending_frames += 1
                        else:
                            self._pending_gesture = detected_gesture
                            self._pending_frames = 1
                        is_dynamic = detected_gesture in {"Swipe Left", "Swipe Right"}
                        cooldown = float(
                            self._config.get("gesture_cooldowns", {}).get(
                                detected_gesture, self._debounce_interval
                            )
                        )
                        last_time = self._last_gesture_times.get(_gesture_key(detected_gesture), 0.0)
                        stable = is_dynamic or self._pending_frames >= self._stable_frames_required
                        app_action = self._app_scoped_gesture_action(detected_gesture, app_identity)
                        pointer_tracking = bool(app_action and _gesture_key(app_action) == _gesture_key("Mouse Pointer Moving"))
                        if pointer_tracking:
                            self._move_cursor(wrist.x, wrist.y)
                        if stable and not pointer_tracking and now - last_time >= max(0.2, cooldown):
                            if self._dispatch_gesture(detected_gesture, app_identity):
                                self._last_trigger_time = now
                                self._last_gesture_times[_gesture_key(detected_gesture)] = now
                                self._history.clear()
                                self._pending_gesture = None
                                self._pending_frames = 0
                else:
                    hand_was_detected = False
                    self._pending_gesture = None
                    self._pending_frames = 0

                time.sleep(0.045)

        except Exception as e:
            print(f"[GestureEngine] Error in tracking loop: {e}")
        finally:
            if recognizer is not None:
                try:
                    recognizer.close()
                except Exception:
                    pass
            if camera is not None:
                try:
                    camera.release()
                except Exception:
                    pass
            self._running = False
            print("[GestureEngine] Optical tracking loop exited.")

    def _match_custom_landmarks_details(self, landmarks, min_confidence=None) -> tuple[str | None, float, float]:
        if min_confidence is None:
            min_confidence = 0.35
        sample = normalize_landmarks(landmarks)
        return match_custom_sample_details(
            sample,
            self._config.get("custom_gestures", []),
            threshold=0.28,
            min_confidence=min_confidence,
        )

    def _match_custom_landmarks(self, landmarks, min_confidence=None) -> str | None:
        name, _, _ = self._match_custom_landmarks_details(landmarks, min_confidence=min_confidence)
        return name

    def _foreground_app_identity(self) -> tuple[str, str] | None:
        try:
            from tools.window_tools import get_foreground_window, get_window_process_name, get_window_title
            hwnd = get_foreground_window()
            if not hwnd:
                return None
            process = os.path.basename(get_window_process_name(hwnd) or "").casefold()
            process_stem = process.removesuffix(".exe")
            title = get_window_title(hwnd).casefold()
        except Exception:
            return None
        return process_stem, title

    @staticmethod
    def _app_scope_matches(app: str, identity: tuple[str, str] | None) -> bool:
        if not identity:
            return False
        process_stem, _title = identity
        app = os.path.basename(str(app or "").strip()).casefold()
        app_stem = app.removesuffix(".exe")
        if app_stem in {"google chrome", "chrome"}:
            app_stem = "chrome"
        return app_stem == "chrome" and process_stem == "chrome"

    @staticmethod
    def _move_cursor(x: float, y: float) -> None:
        """Move the system pointer to a normalized camera landmark."""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            width = max(1, int(user32.GetSystemMetrics(0)))
            height = max(1, int(user32.GetSystemMetrics(1)))
            screen_x = round(max(0.0, min(1.0, float(x))) * (width - 1))
            screen_y = round(max(0.0, min(1.0, float(y))) * (height - 1))
            user32.SetCursorPos(screen_x, screen_y)
        except (AttributeError, OSError, TypeError, ValueError):
            return

    def _active_app_scoped_custom_gestures(self, identity=None) -> list[dict]:
        if identity is None:
            identity = self._foreground_app_identity()
        return [
            {"name": str(item.get("gesture", "")), "landmark_template": item["landmark_template"]}
            for item in self._config.get("app_gesture_mappings", [])
            if isinstance(item, dict)
            and str(item.get("gesture", "")).strip()
            and isinstance(item.get("landmark_template"), list)
            and self._app_scope_matches(item.get("app", ""), identity)
        ]

    def _app_scoped_gesture_action(self, gesture_name: str, identity=None) -> str | None:
        """Find a mapping for this gesture when its configured app is focused."""
        mappings = self._config.get("app_gesture_mappings", [])
        if identity is None:
            identity = self._foreground_app_identity()
        if not identity:
            return None
        gesture_key = _gesture_key(gesture_name)
        for item in mappings:
            if not isinstance(item, dict):
                continue
            if self._app_scope_matches(item.get("app", ""), identity) and _gesture_key(item.get("gesture", "")) == gesture_key:
                return str(item.get("action", "")).strip() or None
        return None

    def _dispatch_gesture(self, gesture_name: str, app_identity=None):
        gesture_key = _gesture_key(gesture_name)
        clean_key = _clean_gesture_name(gesture_name)
        action = self._app_scoped_gesture_action(gesture_name, app_identity)
        customs = self._config.get("custom_gestures", [])
        for cg in (() if action else customs):
            aliases = {
                _gesture_key(cg.get("name", "")),
                _clean_gesture_name(cg.get("name", "")),
                _gesture_key(cg.get("pose", "")),
                _clean_gesture_name(cg.get("pose", "")),
            }
            pose_clean = _clean_gesture_name(cg.get("pose", ""))
            if " hold" in pose_clean:
                aliases.add(pose_clean.replace(" hold", ""))
            if "3-finger swipe" in pose_clean or "3 finger swipe" in pose_clean:
                aliases.add("swipe left")
                aliases.add("swipe right")
            if gesture_key in aliases or clean_key in aliases:
                action = cg.get("action")
                break

        # Also check hand_sign_mappings
        if not action:
            hand_signs = self._config.get("hand_sign_mappings", {})
            for h_name, h_act in hand_signs.items():
                if _gesture_key(h_name) in {gesture_key, clean_key} or _clean_gesture_name(h_name) in {gesture_key, clean_key}:
                    action = h_act
                    break

        # Default mappings use exact normalized names with alias fallbacks
        if not action:
            defaults = self._config.get("default_mappings", {})
            for d_name, d_act in defaults.items():
                if _gesture_key(d_name) == gesture_key:
                    action = d_act
                    break

            if not action:
                gesture_aliases = {
                    "peace sign": ["victory", "peace sign", "peace_sign"],
                    "victory": ["victory", "peace sign", "peace_sign"],
                    "iron man repulsor aim": ["iron man repulsor aim", "i love you", "iloveyou"],
                    "i love you": ["iron man repulsor aim", "i love you", "iloveyou"],
                    "fist": ["fist", "closed fist", "closed_fist"],
                    "closed fist": ["fist", "closed fist", "closed_fist"],
                    "pointing": ["pointing", "pointing up", "pointing_up"],
                    "pointing up": ["pointing", "pointing up", "pointing_up"],
                    "thumbs up": ["thumbs up", "thumb up", "thumb_up"],
                    "thumbs down": ["thumbs down", "thumb down", "thumb_down"],
                    "open palm": ["open palm", "open_palm"],
                }
                equivs = gesture_aliases.get(gesture_key, [])
                for d_name, d_act in defaults.items():
                    if _gesture_key(d_name) in equivs:
                        action = d_act
                        break

        if not action and gesture_name.startswith("Hand Sign "):
            sign_key = gesture_name.removeprefix("Hand Sign ")
            action = self._config.get("hand_sign_mappings", {}).get(sign_key)

        if action:
            print(f"[GestureEngine] GESTURE DETECTED: {gesture_name} -> Action: {action}")

            global _PENDING_GESTURE
            action_text = str(action)
            if gesture_name.startswith("Hand Sign "):
                action_text = str(self._config.get("hand_sign_mappings", {}).get(
                    gesture_name.removeprefix("Hand Sign "), action_text
                ))
            action_parts = [part.strip() for part in action_text.split("+") if part.strip()]
            needs_confirmation = any(
                part.casefold() not in _DIRECT_GESTURE_ACTIONS
                and not re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", part.strip().strip("\"'"))
                and any(
                    re.search(rf"\b{re.escape(token)}\b", part.casefold())
                    for token in _CONFIRMATION_ACTIONS
                )
                for part in action_parts
            )
            if needs_confirmation:
                print(
                    f"[GestureEngine] Gesture action requires confirmation: "
                    f"{gesture_name} -> {action_text}"
                )
                _PENDING_GESTURE = (gesture_name, action_text)
                from core.events import event_bus
                event_bus.publish(
                    "confirmation_requested",
                    source="gesture",
                    gesture=gesture_name,
                    action=action_text,
                )
                return False

            # Run the action without allowing one launcher/API error to stop
            # the camera worker. The detection line above is emitted before
            # execution, so log its outcome separately for useful diagnostics.
            try:
                executed = bool(execute_gesture_action(action_text))
            except Exception as error:
                print(f"[GestureEngine] Gesture action failed: {action_text}: {error}")
                executed = False
            print(
                f"[GestureEngine] GESTURE ACTION {'EXECUTED' if executed else 'FAILED'}: "
                f"{gesture_name} -> {action_text}"
            )

            # Notify event bus
            try:
                from core.events import event_bus
                event_bus.publish(
                    "gesture_detected",
                    gesture=gesture_name,
                    action=action,
                    executed=executed,
                )
            except Exception:
                pass

            # Log to live HUD event stream
            try:
                from core.event_logger import emit_event
                emit_event("TOOL", f"GESTURE: {gesture_name} -> {action}")
            except Exception:
                pass
            return executed
        return False
