"""Qt WebEngine bridge for the optional event-bus driven Jarvis HUD."""

from __future__ import annotations

import os
import ctypes
import json
import re
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot, QUrl, QTimer, Property, Qt, QFileSystemWatcher, QEvent
from PySide6.QtGui import QAction, QCursor
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QStyle, QSystemTrayIcon, QWidget
from PySide6.QtWebEngineWidgets import QWebEngineView


ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT.parent / "config"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
UI_SETTINGS_PATH = CONFIG_DIR / "ui_settings.json"
CORRECTIONS_PATH = CONFIG_DIR / "command_corrections.json"
GESTURE_PATH = CONFIG_DIR / "gesture_settings.json"
DATABASE_PATH = CONFIG_DIR / "jarvis.db"
UI_DEFAULTS = {
    "accent_color": "#0066ff",
    "motion_enabled": True,
    "rotation_enabled": True,
    "orbit_radius_vmin": 44,
    "rotation_speed_degrees_per_second": 12,
    "motion_fps": 6,
    "particle_count": 0,
    "telemetry_interval_ms": 2500,
    "scanlines_enabled": True,
    "blur_px": 24,
    "dim_opacity": 0.65,
    "click_pass_through": True,
    "ui_scale": 1.0,
}


def _validate_object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object.")


def _load_ui_settings(base=None):
    settings = dict(UI_DEFAULTS if base is None else base)
    try:
        with UI_SETTINGS_PATH.open("r", encoding="utf-8") as source:
            supplied = json.load(source)
        if isinstance(supplied, dict):
            settings.update({key: value for key, value in supplied.items() if key in UI_DEFAULTS})
    except (OSError, json.JSONDecodeError):
        if base is not None:
            print("[UI] UI settings are invalid or unavailable; keeping the last applied values.")
            return settings
    for key, fallback, low, high in (
        ("particle_count", 0, 0, 200),
        ("motion_fps", 6, 1, 30),
        ("telemetry_interval_ms", 2500, 500, 10000),
    ):
        try:
            settings[key] = max(low, min(high, int(settings[key])))
        except (TypeError, ValueError, OverflowError):
            settings[key] = fallback
    settings["accent_color"] = settings["accent_color"] if isinstance(settings["accent_color"], str) else UI_DEFAULTS["accent_color"]
    settings["motion_enabled"] = settings["motion_enabled"] is True
    settings["rotation_enabled"] = settings["rotation_enabled"] is True
    settings["scanlines_enabled"] = settings.get("scanlines_enabled", True) is True
    settings["click_pass_through"] = settings.get("click_pass_through", True) is True
    try:
        settings["blur_px"] = max(0, min(100, int(settings.get("blur_px", 24))))
    except (TypeError, ValueError):
        settings["blur_px"] = 24
    try:
        settings["dim_opacity"] = max(0.0, min(1.0, float(settings.get("dim_opacity", 0.65))))
    except (TypeError, ValueError):
        settings["dim_opacity"] = 0.65
    try:
        settings["ui_scale"] = max(0.5, min(2.5, float(settings.get("ui_scale", 1.0))))
    except (TypeError, ValueError):
        settings["ui_scale"] = 1.0
    return settings


from ui.window_pin import pin_to_desktop_bottom, set_settings_active, set_click_pass_through
from core.config_store import load_json, save_json_atomic
from core.settings import save_settings



class _FILETIME(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32), ("memory_load", ctypes.c_uint32),
        ("total_phys", ctypes.c_uint64), ("avail_phys", ctypes.c_uint64),
        ("total_page_file", ctypes.c_uint64), ("avail_page_file", ctypes.c_uint64),
        ("total_virtual", ctypes.c_uint64), ("avail_virtual", ctypes.c_uint64),
        ("avail_extended_virtual", ctypes.c_uint64),
    ]


class SystemMetrics:
    """Read CPU and memory percentages from the Windows system APIs."""

    def __init__(self):
        self._last_cpu = None

    def read(self):
        metrics = {}
        try:
            idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
            if ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
            ):
                current = tuple(self._filetime_value(item) for item in (idle, kernel, user))
                if self._last_cpu is not None:
                    previous = self._last_cpu
                    total = (current[1] - previous[1]) + (current[2] - previous[2])
                    idle_delta = current[0] - previous[0]
                    metrics["cpu"] = round(max(0.0, min(100.0, 100.0 * (total - idle_delta) / total)), 1) if total else 0
                self._last_cpu = current
        except (AttributeError, OSError, ZeroDivisionError):
            pass

        try:
            memory = _MEMORYSTATUSEX()
            memory.length = ctypes.sizeof(memory)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
                metrics["ram"] = int(memory.memory_load)
        except (AttributeError, OSError):
            pass
        return metrics

    @staticmethod
    def _filetime_value(value):
        return (value.high << 32) | value.low


class EventBridge(QObject):
    event_received = Signal(str, object)
    launcher_execution = Signal(str)
    gesture_preview_requested = Signal()
    toast_requested = Signal(str)
    changed = Signal()

    def __init__(self, debug=False):
        super().__init__()
        self._state = "SLEEPING"
        self._heard = ""
        self._normalized = ""
        self._actions = []
        self._detail = "Jarvis is standing by"
        self._error = ""
        self._level = 0.0
        self._workspace = ""
        self._steps = []
        self._recent_files = []
        self._panel_visible = False
        self._visible = True
        self._debug = bool(debug)
        from core.events import event_bus
        self._bus = event_bus
        self._callbacks = []
        from core.settings import get_settings
        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.setInterval(get_settings().get("wheel_dismiss_ms", 5000))
        self._dismiss_timer.timeout.connect(self._dismiss_steps)
        self._error_timer = QTimer(self)
        self._error_timer.setSingleShot(True)
        self._error_timer.setInterval(550)
        self._error_timer.timeout.connect(self._clear_error_state)
        events = ("state_changed", "heard", "parsed", "step_started", "step_result", "error", "audio_level", "workspace_changed", "confirmation_requested", "gesture_preview_requested", "gesture_detected", "gesture_saved")
        for name in events:
            callback = lambda payload, event=name: self._receive(event, payload)
            event_bus.subscribe(name, callback)
            self._callbacks.append((name, callback))
        self.event_received.connect(self._apply_event)

    def _receive(self, name, payload):
        self.event_received.emit(name, payload)

    @Slot(str, object)
    def _apply_event(self, name, payload):
        if name == "state_changed":
            self._state = str(payload.get("state", self._state)).upper()
            if payload.get("detail"):
                self._detail = str(payload["detail"])
        elif name == "heard":
            self._error = ""
            self._heard = str(payload.get("text", ""))
            self._normalized = str(payload.get("normalized", self._normalized))
            self._detail = f"Heard: {self._heard}"
        elif name == "parsed":
            self._actions = list(payload.get("actions", []))
            self._steps = [{"action": item, "state": "pending", "message": ""} for item in self._actions]
            self._dismiss_timer.stop()
        elif name == "step_started":
            index = int(payload.get("index", 0))
            if 0 <= index < len(self._steps):
                self._steps[index]["state"] = "running"
                self._detail = f"Running: {self._steps[index]['action']}"
        elif name == "step_result":
            index = int(payload.get("index", -1))
            if 0 <= index < len(self._steps):
                self._steps[index]["state"] = "skipped" if payload.get("skipped") else "done" if payload.get("success") else "failed"
                self._steps[index]["message"] = str(payload.get("message", ""))
                if payload.get("success") and self._steps[index]["action"].startswith(("open:", "create:")):
                    self._recent_files.insert(0, self._steps[index]["action"].split(":", 1)[1])
                    self._recent_files = self._recent_files[:5]
            self._detail = str(payload.get("message", self._detail))
            if self._steps and all(step["state"] in {"done", "failed", "skipped"} for step in self._steps):
                self._dismiss_timer.start()
        elif name == "error":
            self._error = str(payload.get("message", payload))
            self._detail = self._error
            self._state = "ERROR"
            self._error_timer.start()
        elif name == "audio_level":
            self._level = max(0.0, min(1.0, float(payload.get("level", 0))))
        elif name == "workspace_changed":
            self._workspace = str(payload.get("path", ""))
            self._detail = f"Workspace: {self._workspace}"
        elif name == "gesture_detected":
            gesture = str(payload.get("gesture", "Gesture"))
            action = str(payload.get("action", ""))
            status = "Executed" if payload.get("executed") else "Not executed"
            self._detail = f"{gesture}: {action} ({status})"
            self._state = "EXECUTING" if payload.get("executed") else "ERROR"
            self._dismiss_timer.start()
            if payload.get("executed"):
                self.toast_requested.emit(f"✋ Gesture: {gesture} -> {action}")
        elif name == "confirmation_requested":
            action = str(payload.get("action", "sensitive action"))
            self._detail = f"Confirmation required before {action}."
            self._state = "CONFIRMING"
        elif name == "gesture_saved":
            self._detail = str(payload.get("message", "Hand gesture saved."))
            self._state = "IDLE"
        elif name == "gesture_preview_requested":
            self.gesture_preview_requested.emit()

        if name in {"step_started", "step_result", "error"}:
            if name == "step_started":
                label = str(payload.get("action", "Jarvis command"))
                event_state = "doing"
                detail = f"Running {label}"
            elif name == "error":
                label = "Jarvis reported an error"
                event_state = "error"
                detail = str(payload.get("message", "An error occurred."))
            else:
                label = str(payload.get("action", "Command execution"))
                event_state = "done" if payload.get("success") else "error"
                detail = str(payload.get("message", "Command finished."))
            self.launcher_execution.emit(json.dumps({
                "time": time.strftime("%H:%M:%S"),
                "label": label,
                "detail": detail,
                "state": event_state,
            }, ensure_ascii=True))
        self.changed.emit()

    def _dismiss_steps(self):
        self._steps = []
        self._actions = []
        self.changed.emit()

    def _clear_error_state(self):
        if self._state == "ERROR":
            self._state = "IDLE"
            self.changed.emit()

    state = Property(str, lambda self: self._state, notify=changed)
    heard = Property(str, lambda self: self._heard, notify=changed)
    normalized = Property(str, lambda self: self._normalized, notify=changed)
    actions = Property(list, lambda self: self._actions, notify=changed)
    detail = Property(str, lambda self: self._detail, notify=changed)
    error = Property(str, lambda self: self._error, notify=changed)
    level = Property(float, lambda self: self._level, notify=changed)
    workspace = Property(str, lambda self: self._workspace, notify=changed)
    steps = Property(list, lambda self: self._steps, notify=changed)
    recentFiles = Property(list, lambda self: self._recent_files, notify=changed)
    panel_visible = Property(bool, lambda self: self._panel_visible, notify=changed)
    visible = Property(bool, lambda self: self._visible, notify=changed)
    debug = Property(bool, lambda self: self._debug, notify=changed)

    @Slot()
    def toggle_panel(self):
        self._panel_visible = not self._panel_visible
        self.changed.emit()

    @Slot()
    def toggle_visible(self):
        self._visible = not self._visible
        self.changed.emit()

    @Slot()
    def toggle_debug(self):
        self._debug = not self._debug
        self.changed.emit()

    @Slot(bool)
    def set_debug(self, enabled):
        self._debug = bool(enabled)
        self.changed.emit()


def run_ui(debug=False, demo=False, backend_start=None):
    chromium_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    if "--enable-transparent-visuals" not in chromium_flags:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
            f"{chromium_flags} --enable-transparent-visuals"
        ).strip()
    app = QApplication.instance() or QApplication(sys.argv)
    QApplication.setQuitOnLastWindowClosed(False)
    ui_settings = _load_ui_settings()
    bridge = EventBridge(debug=debug)
    gesture_preview_windows: list[QWidget] = []

    def save_captured_gesture(result):
        request = result.get("request", {})
        name = str(request.get("name", "")).strip()
        action = str(request.get("action", "")).strip()
        editing_name = str(request.get("editing_name", "")).strip() or None
        template = result.get("landmark_template")
        if not name or not action or not isinstance(template, list) or len(template) not in {63, 126}:
            print("[GestureEngine] Camera capture was incomplete; gesture was not saved.")
            return
        if request.get("app_scoped"):
            app = str(request.get("app", "")).strip()
            if not app:
                view.page().runJavaScript("showToast('Enter an app or process name for this custom gesture.', true);")
                return
            try:
                from core.gesture_engine import load_gesture_config, save_gesture_config, GestureEngine
                config = load_gesture_config()
                mappings = config.setdefault("app_gesture_mappings", [])
                if not isinstance(mappings, list):
                    mappings = config["app_gesture_mappings"] = []
                editing_index = request.get("editing_index")
                try:
                    editing_index = int(editing_index) if editing_index is not None else None
                except (TypeError, ValueError):
                    editing_index = None
                if editing_index is not None and 0 <= editing_index < len(mappings):
                    mappings[editing_index] = {
                        "app": app,
                        "gesture": name,
                        "action": action,
                        "landmark_template": template,
                    }
                else:
                    duplicate = any(
                        isinstance(item, dict)
                        and str(item.get("app", "")).strip().casefold() == app.casefold()
                        and str(item.get("gesture", "")).strip().casefold() == name.casefold()
                        for item in mappings
                    )
                    if duplicate:
                        view.page().runJavaScript("showToast('That app already has a rule for this gesture.', true);")
                        return
                    mappings.append({
                        "app": app,
                        "gesture": name,
                        "action": action,
                        "landmark_template": template,
                    })
                save_gesture_config(config)
                GestureEngine.get_instance().reload_settings()
                sync_all_config_to_ui()
                bridge._bus.publish(
                    "gesture_saved",
                    name=name,
                    action=action,
                    message=f"Saved app-scoped gesture '{name}' for {app}.",
                )
                for win in list(gesture_preview_windows):
                    try:
                        win.close()
                    except Exception:
                        pass
                view.page().runJavaScript(
                    f"showToast({json.dumps(f'✓ Saved custom gesture {name} for {app}.')});"
                    "if(window.cancelAppGestureMappingEdit) window.cancelAppGestureMappingEdit();"
                )
                print(f"[GestureEngine] App-scoped custom gesture saved: {app} / {name} -> {action}")
            except Exception as exc:
                view.page().runJavaScript(f"showToast({json.dumps(f'Could not save app-scoped gesture: {exc}')}, true);")
                print(f"[GestureEngine] Could not save app-scoped gesture: {exc}")
            return
        try:
            from core.gesture_engine import load_gesture_config, check_duplicate_gesture
            config = load_gesture_config()
            name_err, act_err = check_duplicate_gesture(name, action, config, exclude_name=editing_name)
            if name_err or act_err:
                err_msg = name_err or act_err
                print(f"[GestureEngine] Cannot save camera gesture: {err_msg}")
                view.page().runJavaScript(f"showToast({json.dumps(err_msg)}, true);")
                bridge._bus.publish("error", message=f"Cannot save hand gesture: {err_msg}")
                return
            customs = config.setdefault("custom_gestures", [])
            if not isinstance(customs, list):
                customs = config["custom_gestures"] = []
            target_to_remove = (editing_name or name).casefold()
            customs[:] = [
                item for item in customs
                if not isinstance(item, dict) or str(item.get("name", "")).casefold() != target_to_remove
            ]
            customs.append({
                "name": name,
                "pose": "Camera-trained hand pose",
                "action": action,
                "icon": "✋",
                "landmark_template": template,
            })
            hand_signs = config.setdefault("hand_sign_mappings", {})
            if not isinstance(hand_signs, dict):
                hand_signs = config["hand_sign_mappings"] = {}
            if editing_name and editing_name in hand_signs and editing_name != name:
                hand_signs.pop(editing_name, None)
            hand_signs[name] = action

            from core.gesture_engine import GestureEngine, save_gesture_config
            save_gesture_config(config)
            engine = GestureEngine.get_instance()
            engine.reload_settings()
            sync_all_config_to_ui()
            bridge._bus.publish(
                "gesture_saved",
                name=name,
                action=action,
                message=f"Saved hand gesture '{name}'.",
            )
            for win in list(gesture_preview_windows):
                try:
                    win.close()
                except Exception:
                    pass
            view.page().runJavaScript(
                f"showToast({json.dumps(f'✓ Successfully registered neural gesture \"{name}\".')});"
                "if(document.getElementById('customGestureName')) document.getElementById('customGestureName').value = '';"
                "if(document.getElementById('customGestureAction')) document.getElementById('customGestureAction').value = '';"
                "if(document.getElementById('customGestureActionPreset')) document.getElementById('customGestureActionPreset').selectedIndex = 0;"
                "if(document.getElementById('handSignKey')) document.getElementById('handSignKey').value = '';"
                "if(document.getElementById('handSignAction')) document.getElementById('handSignAction').value = '';"
                "if(window.cancelCustomGestureEdit) window.cancelCustomGestureEdit();"
            )
            print(f"[GestureEngine] Camera gesture saved: {name} -> {action}")
        except Exception as exc:
            bridge._bus.publish(
                "error",
                message=f"Could not save hand gesture '{name}': {exc}",
            )
            print(f"[GestureEngine] Could not save camera gesture: {exc}")

    def show_gesture_preview(capture_request=None, target_gesture=None):
        if gesture_preview_windows:
            window = gesture_preview_windows[-1]
            if capture_request:
                window.begin_capture(capture_request)
            if target_gesture:
                window.target_gesture = target_gesture
                guide_title, guide_pose = window._guide_for(target_gesture)
                window.guide.setText(
                    f"<b>HOW TO MAKE IT</b><br>{guide_title}<br>"
                    f"<span style='color:#54d6ff'>{guide_pose}</span>"
                )
                window.notice.setText(f"PREVIEW ONLY — gestures shown here will not execute actions — show: {target_gesture}.")
            window.showNormal()
            window.raise_()
            window.activateWindow()
            return
        from core.gesture_engine import GestureEngine
        from ui.gesture_preview import GesturePreviewWindow

        engine = GestureEngine.get_instance()
        resume_engine = engine.is_active()
        if resume_engine:
            engine.stop()

        try:
            window = GesturePreviewWindow(
                capture_request=capture_request,
                target_gesture=target_gesture,
            )
        except Exception:
            if resume_engine:
                engine.start()
            raise
        gesture_preview_windows.append(window)
        window.gesture_captured.connect(save_captured_gesture)

        def preview_closed(_obj=None):
            if window in gesture_preview_windows:
                gesture_preview_windows.remove(window)
            if resume_engine:
                engine.start()

        window.destroyed.connect(preview_closed)
        if hasattr(window, "closed"):
            window.closed.connect(preview_closed)
        window.show()

    bridge.gesture_preview_requested.connect(show_gesture_preview)
    view = QWebEngineView()
    view.setWindowTitle("J.A.R.V.I.S. Overlay")
    view.setWindowFlags(
        Qt.WindowType.Window
        | Qt.WindowType.FramelessWindowHint
    )
    view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    view.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
    view.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
    view.setStyleSheet("background: transparent")
    view.page().setBackgroundColor(Qt.GlobalColor.transparent)
    view.setMinimumSize(880, 560)
    bridge.toast_requested.connect(
        lambda message: view.page().runJavaScript(
            f"window.showToast && window.showToast({json.dumps(message)});"
        )
    )

    def publish_launcher_execution(serialized_event):
        try:
            event_data = json.loads(serialized_event)
            event_json = json.dumps(event_data, ensure_ascii=True, separators=(",", ":"))
            view.page().runJavaScript(
                f"window.dispatchEvent(new CustomEvent('jarvis:execution', {{detail:{event_json}}}));"
            )
        except (TypeError, ValueError):
            pass

    bridge.launcher_execution.connect(publish_launcher_execution)
    screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
    if screen:
        view.winId()  # Create the native window before selecting its display.
        view.windowHandle().setScreen(screen)
    launcher_page = ROOT.parent / "jarvis-app-ui-design" / "out" / "index.html"
    if launcher_page.is_file():
        print(f"[UI] Loading Jarvis launcher app: {launcher_page}")
        set_settings_active(True)
        view.load(QUrl.fromLocalFile(str(launcher_page)))
    else:
        print("[UI] Launcher export is not built; loading the standard Jarvis HUD.")
        view.load(QUrl.fromLocalFile(str(ROOT / "overlay.html")))

    def get_all_config_data():
        data = {
            "settings": {},
            "ui_settings": dict(ui_settings),
            "corrections": {},
            "voice_commands": {},
            "gestures": {},
            "api_key_status": {}
        }
        try:
            data["settings"] = load_json(SETTINGS_PATH, {})
        except Exception as e:
            print(f"[UI] Failed to load settings.json: {e}")

        try:
            raw = load_json(CORRECTIONS_PATH, {})
            data["corrections"] = raw.get("corrections", raw) if isinstance(raw, dict) else {}
            from core.database import load_voice_commands
            data["voice_commands"] = load_voice_commands()
        except Exception as e:
            print(f"[UI] Failed to load command_corrections.json: {e}")

        try:
            from core.gesture_engine import load_gesture_config
            data["gestures"] = load_gesture_config()
        except Exception as e:
            print(f"[UI] Failed to load gesture_settings.json: {e}")

        try:
            from core.api_keys import key_status
            data["api_key_status"] = key_status()
        except Exception:
            data["api_key_status"] = {"gemini": False, "groq": False}

        try:
            import sounddevice as sd
            devs = sd.query_devices()
            names = []
            for d in devs:
                if d.get("max_input_channels", 0) > 0:
                    name = str(d.get("name", "")).strip()
                    if name and name not in names:
                        names.append(name)
            data["audio_devices"] = names
        except Exception:
            data["audio_devices"] = []

        return data

    def sync_all_config_to_ui():
        config_data = get_all_config_data()
        payload = json.dumps(config_data, ensure_ascii=True, separators=(",", ":"))
        view.page().runJavaScript(f"window.initJarvisConfigData && window.initJarvisConfigData({payload});")

    def get_recent_log_snapshot():
        log_path = ROOT / "runtime" / "jarvis.log"
        try:
            raw = log_path.read_bytes()[-160_000:].decode("utf-8", errors="replace")
            lines = raw.splitlines()[-180:]
            text = "\n".join(lines)
        except OSError:
            text = "Jarvis has not written a runtime log yet."

        secrets = {value for value in (os.getenv("GEMINI_API_KEY"), os.getenv("GROQ_API_KEY")) if value}
        try:
            from dotenv import dotenv_values
            env_values = dotenv_values(str(ROOT / ".env"))
            secrets.update(value for name, value in env_values.items()
                           if name in {"GEMINI_API_KEY", "GROQ_API_KEY"} and value)
        except Exception:
            pass
        for secret in sorted(secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        text = re.sub(
            r"(?i)\b(GEMINI_API_KEY|GROQ_API_KEY)\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
            text,
        )
        return {"text": text[-32_000:], "updated_at": time.strftime("%H:%M:%S")}

    def handle_backend_action(action: str, payload: dict):
        def _deep_update(base: dict, overlay: dict):
            for k, v in overlay.items():
                if isinstance(v, dict) and isinstance(base.get(k), dict):
                    _deep_update(base[k], v)
                else:
                    base[k] = v

        if action == "get_api_key_status":
            try:
                from core.api_keys import key_status
                status = key_status()
            except Exception:
                status = {"gemini": False, "groq": False}
            view.page().runJavaScript(
                f"window.updateApiKeyStatus && window.updateApiKeyStatus({json.dumps(status)});"
            )

        elif action == "get_api_storage_info":
            try:
                from core.api_keys import read_storage_note, storage_info
                info = storage_info()
                note = read_storage_note()
            except Exception:
                info = {"providers": {"gemini": 0, "groq": 0}, "used_bytes": 0, "max_bytes": 5 * 1024 * 1024}
                note = ""
            view.page().runJavaScript(
                f"window.updateApiStorage && window.updateApiStorage({json.dumps(info)});"
                f"window.loadApiStorageNote && window.loadApiStorageNote({json.dumps({'text': note})});"
            )

        elif action == "save_api_keys":
            try:
                from core.api_keys import update_api_keys
                updates = {
                    provider: str(payload.get(provider, "") or "").strip()
                    for provider in ("gemini", "groq")
                }
                if any(len(value.encode("utf-8")) > 5 * 1024 * 1024 for value in updates.values()):
                    raise ValueError("API key is too large for the 5 MB storage allocation.")
                clear = [provider for provider in payload.get("clear", [])
                         if provider in {"gemini", "groq"}]
                if not any(updates.values()) and not clear:
                    raise ValueError("Enter a key or choose a key to remove.")
                status = update_api_keys(updates, clear)
                from core.api_keys import storage_info
                storage = storage_info()
                view.page().runJavaScript(
                    f"window.updateApiKeyStatus && window.updateApiKeyStatus({json.dumps(status)});"
                    f"window.updateApiStorage && window.updateApiStorage({json.dumps(storage)});"
                    "document.getElementById('geminiApiKeyInput').value='';"
                    "document.getElementById('groqApiKeyInput').value='';"
                    "showToast('API key settings saved and applied.');"
                )
            except Exception as error:
                message = "API storage is limited to 5 MB." if "5 MB" in str(error) else "Could not save API key settings."
                view.page().runJavaScript(f"showToast({json.dumps(message)}, true);")

        elif action == "save_api_storage_note":
            try:
                from core.api_keys import save_storage_note
                info = save_storage_note(str(payload.get("text", "") or ""))
                note = str(payload.get("text", "") or "")
                view.page().runJavaScript(
                    f"window.updateApiStorage && window.updateApiStorage({json.dumps(info)});"
                    f"window.loadApiStorageNote && window.loadApiStorageNote({json.dumps({'text': note})});"
                    "showToast('Storage note saved.');"
                )
            except Exception as error:
                message = "API storage is limited to 5 MB." if "5 MB" in str(error) else "Could not save the storage note."
                view.page().runJavaScript(f"showToast({json.dumps(message)}, true);")

        elif action == "get_recent_logs":
            snapshot = get_recent_log_snapshot()
            payload_json = json.dumps(snapshot, ensure_ascii=True, separators=(",", ":"))
            view.page().runJavaScript(
                f"window.updateLogUpdater && window.updateLogUpdater({payload_json});"
            )

        elif action == "save_all":
            errors = []
            if "settings" in payload and isinstance(payload["settings"], dict):
                try:
                    current = load_json(SETTINGS_PATH, {})
                    _deep_update(current, payload["settings"])
                    save_settings(current)
                except Exception as e:
                    print(f"[UI] Error saving settings.json: {e}")
                    errors.append(f"settings: {e}")

            if "ui_settings" in payload and isinstance(payload["ui_settings"], dict):
                try:
                    current_ui = dict(ui_settings)
                    current_ui.update(payload["ui_settings"])
                    save_json_atomic(UI_SETTINGS_PATH, current_ui, validator=lambda value: _validate_object(value, "UI settings"))
                    reload_ui_settings()
                except Exception as e:
                    print(f"[UI] Error saving ui_settings.json: {e}")
                    errors.append(f"UI settings: {e}")

            if "gestures" in payload and isinstance(payload["gestures"], dict):
                try:
                    from core.gesture_engine import load_gesture_config
                    current_gestures = load_gesture_config()
                    _deep_update(current_gestures, payload["gestures"])
                    from core.gesture_engine import save_gesture_config
                    save_gesture_config(current_gestures)
                    try:
                        from core.gesture_engine import GestureEngine
                        GestureEngine.get_instance().reload_settings()
                    except Exception as e:
                        print(f"[UI] Error reloading GestureEngine: {e}")
                except Exception as e:
                    print(f"[UI] Error saving gesture_settings.json: {e}")
                    errors.append(f"gestures: {e}")

            sync_all_config_to_ui()
            if errors:
                view.page().runJavaScript(
                    f"showToast({json.dumps('Configuration save failed: ' + '; '.join(errors))}, true);"
                )
            else:
                view.page().runJavaScript("showToast('Configuration saved and applied.');")

        elif action == "add_custom_gesture":
            name = str(payload.get("name", "")).strip()
            pose = str(payload.get("pose", "")).strip()
            gesture_action = str(payload.get("gesture_action", "")).strip()
            editing_name = str(payload.get("editing_name", "")).strip() or None
            if not name or not gesture_action:
                view.page().runJavaScript("showToast('Gesture name and action are required.', true);")
            else:
                try:
                    from core.gesture_engine import load_gesture_config, check_duplicate_gesture
                    config = load_gesture_config()
                    name_err, act_err = check_duplicate_gesture(name, gesture_action, config, exclude_name=editing_name)
                    if name_err:
                        view.page().runJavaScript(f"showToast({json.dumps(name_err)}, true);")
                        return
                    if act_err:
                        view.page().runJavaScript(f"showToast({json.dumps(act_err)}, true);")
                        return
                    customs = config.setdefault("custom_gestures", [])
                    if not isinstance(customs, list):
                        raise ValueError("custom_gestures must be a list")
                    icon = pose.split(" ")[0] if pose else "✋"
                    existing = next((item for item in customs if isinstance(item, dict) and str(item.get("name", "")).casefold() == (editing_name or name).casefold()), None)
                    if existing:
                        existing["name"] = name
                        existing["pose"] = pose
                        existing["action"] = gesture_action
                        existing["icon"] = icon
                    else:
                        customs.append({"name": name, "pose": pose, "action": gesture_action, "icon": icon})
                    hand_signs = config.setdefault("hand_sign_mappings", {})
                    if not isinstance(hand_signs, dict):
                        hand_signs = config["hand_sign_mappings"] = {}
                    if editing_name and editing_name in hand_signs and editing_name != name:
                        hand_signs.pop(editing_name, None)
                    hand_signs[name] = gesture_action

                    from core.gesture_engine import GestureEngine, save_gesture_config
                    save_gesture_config(config)
                    GestureEngine.get_instance().reload_settings()
                    sync_all_config_to_ui()
                    view.page().runJavaScript("showToast('Custom gesture saved successfully.');")
                except Exception as error:
                    view.page().runJavaScript(f"showToast({json.dumps(f'Could not create gesture: {error}')}, true);")

        elif action == "delete_custom_gesture":
            name = str(payload.get("name", "")).strip()
            try:
                from core.gesture_engine import load_gesture_config
                config = load_gesture_config()
                customs = config.get("custom_gestures", [])
                if not isinstance(customs, list):
                    raise ValueError("custom_gestures must be a list")
                filtered = [item for item in customs if not isinstance(item, dict) or str(item.get("name", "")).casefold() != name.casefold()]
                if len(filtered) == len(customs):
                    raise ValueError(f"Gesture '{name}' was not found.")
                config["custom_gestures"] = filtered
                hand_signs = config.get("hand_sign_mappings", {})
                if isinstance(hand_signs, dict):
                    sign_key_to_del = next((k for k in hand_signs if k.casefold() == name.casefold()), None)
                    if sign_key_to_del:
                        del hand_signs[sign_key_to_del]
                from core.gesture_engine import GestureEngine, save_gesture_config
                save_gesture_config(config)
                GestureEngine.get_instance().reload_settings()
                sync_all_config_to_ui()
            except Exception as error:
                view.page().runJavaScript(f"showToast({json.dumps(f'Could not delete gesture: {error}')}, true);")

        elif action == "delete_gesture_mapping":
            gesture_name = str(payload.get("gesture", "")).strip()
            mapping_type = str(payload.get("mapping_type", "default")).strip()
            try:
                from core.gesture_engine import load_gesture_config
                config = load_gesture_config()
                key = "hand_sign_mappings" if mapping_type == "hand_sign" else "default_mappings"
                mappings = config.get(key, {})
                if not isinstance(mappings, dict):
                    raise ValueError(f"Mapping '{gesture_name}' was not found.")
                matched_key = next(
                    (key for key in mappings if str(key).casefold() == gesture_name.casefold()),
                    None,
                )
                if matched_key is None:
                    raise ValueError(f"Mapping '{gesture_name}' was not found.")
                del mappings[matched_key]
                config[key] = mappings
                if mapping_type == "hand_sign":
                    customs = config.get("custom_gestures", [])
                    if isinstance(customs, list):
                        config["custom_gestures"] = [
                            c for c in customs
                            if not isinstance(c, dict) or str(c.get("name", "")).casefold() != gesture_name.casefold()
                        ]
                from core.gesture_engine import GestureEngine, save_gesture_config
                save_gesture_config(config)
                GestureEngine.get_instance().reload_settings()
                sync_all_config_to_ui()
            except Exception as error:
                view.page().runJavaScript(f"showToast({json.dumps(f'Could not delete mapping: {error}')}, true);")

        elif action == "add_hand_sign_mapping":
            sign = str(payload.get("sign", "")).strip()
            gesture_action = str(payload.get("gesture_action", "")).strip()
            if not sign or not gesture_action:
                view.page().runJavaScript("showToast('Gesture name/key and action are required.', true);")
            else:
                try:
                    from core.gesture_engine import load_gesture_config, check_duplicate_gesture
                    config = load_gesture_config()
                    name_err, act_err = check_duplicate_gesture(sign, gesture_action, config, exclude_name=sign)
                    if act_err:
                        view.page().runJavaScript(f"showToast({json.dumps(act_err)}, true);")
                        return
                    mappings = config.setdefault("hand_sign_mappings", {})
                    if not isinstance(mappings, dict):
                        raise ValueError("hand_sign_mappings must be an object")
                    existing_key = next(
                        (key for key in mappings if str(key).casefold() == sign.casefold()),
                        None,
                    )
                    mappings[existing_key or sign] = gesture_action
                    from core.gesture_engine import GestureEngine, save_gesture_config
                    save_gesture_config(config)
                    GestureEngine.get_instance().reload_settings()
                    sync_all_config_to_ui()
                    view.page().runJavaScript("showToast('MAP.03 mapping saved and added to the list.');")
                except Exception as error:
                    view.page().runJavaScript(f"showToast({json.dumps(f'Could not add MAP.03 mapping: {error}')}, true);")

        elif action == "add_correction":
            phrase = str(payload.get("phrase", "")).strip().lower()
            target = str(payload.get("command", "")).strip()
            if phrase and target and CORRECTIONS_PATH.exists():
                try:
                    with open(CORRECTIONS_PATH, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if "corrections" not in data:
                        data["corrections"] = {}
                    data["corrections"][phrase] = target
                    save_json_atomic(CORRECTIONS_PATH, data, validator=lambda value: _validate_object(value, "corrections"))
                    sync_all_config_to_ui()
                except Exception as e:
                    print(f"[UI] Error adding correction: {e}")

        elif action == "add_voice_command":
            phrase = " ".join(str(payload.get("phrase", "")).split()).casefold()
            command = str(payload.get("command", "")).strip()
            if not phrase or not command:
                view.page().runJavaScript("showToast('Voice phrase and Jarvis command are required.', true);")
            elif phrase.startswith("_"):
                view.page().runJavaScript("showToast('Voice phrases cannot start with _.', true);")
            else:
                try:
                    from core.database import save_voice_command
                    save_voice_command(phrase, command)
                    from voice.stt_cleanup import reload_corrections
                    reload_corrections()
                    sync_all_config_to_ui()
                    view.page().runJavaScript("showToast('Custom voice command saved and active.');")
                except Exception as error:
                    view.page().runJavaScript(f"showToast({json.dumps(f'Could not save voice command: {error}')}, true);")

        elif action == "delete_voice_command":
            phrase = " ".join(str(payload.get("phrase", "")).split()).casefold()
            try:
                from core.database import delete_voice_command
                if not delete_voice_command(phrase):
                    raise ValueError(f"Voice command '{phrase}' was not found.")
                from voice.stt_cleanup import reload_corrections
                reload_corrections()
                sync_all_config_to_ui()
            except Exception as error:
                view.page().runJavaScript(f"showToast({json.dumps(f'Could not delete voice command: {error}')}, true);")

        elif action == "delete_correction":
            phrase = str(payload.get("phrase", "")).strip().lower()
            if phrase and CORRECTIONS_PATH.exists():
                try:
                    with open(CORRECTIONS_PATH, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if "corrections" in data and phrase in data["corrections"]:
                        del data["corrections"][phrase]
                        save_json_atomic(CORRECTIONS_PATH, data, validator=lambda value: _validate_object(value, "corrections"))
                    sync_all_config_to_ui()
                except Exception as e:
                    print(f"[UI] Error deleting correction: {e}")

        elif action == "purge_correction":
            keyword = str(payload.get("keyword", "")).strip().lower()
            if keyword and CORRECTIONS_PATH.exists():
                try:
                    with open(CORRECTIONS_PATH, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if "corrections" in data:
                        to_del = [k for k in data["corrections"] if keyword in k.lower()]
                        for k in to_del:
                            del data["corrections"][k]
                        save_json_atomic(CORRECTIONS_PATH, data, validator=lambda value: _validate_object(value, "corrections"))
                    sync_all_config_to_ui()
                except Exception as e:
                    print(f"[UI] Error purging correction: {e}")

        elif action == "test_tts":
            text = payload.get("text", "Vocal synthesis calibration verified, sir. Audio output at optimal levels.")
            voice_profile = payload.get("voice_profile")
            groq_voice = payload.get("groq_voice")
            gemini_voice = payload.get("gemini_voice")
            local_voice = payload.get("local_voice")
            speech_rate = payload.get("speech_rate")
            volume = payload.get("volume")
            bridge._bus.publish("speak", text=text)

            def _play_test_voice():
                try:
                    from voice.tts import speak
                    speak(
                        text,
                        voice_profile=voice_profile,
                        groq_voice=groq_voice,
                        gemini_voice=gemini_voice,
                        local_voice=local_voice,
                        speech_rate=speech_rate,
                        volume=volume,
                    )
                except Exception as ex:
                    print(f"[UI] Error during test TTS playback: {ex}")

            threading.Thread(target=_play_test_voice, daemon=True).start()

        elif action == "confirm_gesture":
            from core.gesture_engine import confirm_pending_gesture
            approved = bool(payload.get("approved", False))
            executed = confirm_pending_gesture(approved)
            bridge._bus.publish(
                "gesture_detected",
                gesture="Confirmed gesture",
                action="Approved" if approved else "Rejected",
                executed=executed,
            )

        elif action == "test_microphone":
            try:
                from core.audio_devices import list_input_devices
                devices = list_input_devices()
                message = (
                    f"Microphone ready: {devices[0]['name']} at "
                    f"{devices[0]['default_samplerate']:.0f} Hz."
                    if devices else
                    "No microphone detected. Check Windows privacy and default input settings."
                )
                view.page().runJavaScript(f"showToast({json.dumps(message)});")
            except (ImportError, OSError, ValueError) as error:
                view.page().runJavaScript(f"showToast({json.dumps(f'Microphone test failed: {error}')} );")

        elif action == "test_camera":
            try:
                import cv2
                camera = cv2.VideoCapture(0, cv2.CAP_DSHOW)
                opened = camera.isOpened()
                started = __import__("time").monotonic()
                frames = 0
                deadline = started + 1.0
                width = height = 0
                while opened and __import__("time").monotonic() < deadline:
                    ok, frame = camera.read()
                    if ok:
                        frames += 1
                        height, width = frame.shape[:2]
                camera.release()
                elapsed = max(0.001, __import__("time").monotonic() - started)
                message = (
                    f"Camera ready: {width}x{height} at {frames / elapsed:.1f} FPS."
                    if frames else
                    "Camera did not return frames. Check permissions or whether another app is using it."
                )
                view.page().runJavaScript(f"showToast({json.dumps(message)});")
            except (ImportError, OSError, RuntimeError, ValueError) as error:
                view.page().runJavaScript(f"showToast({json.dumps(f'Camera test failed: {error}')} );")

        elif action == "restore_defaults":
            try:
                from core.settings import DEFAULT_SETTINGS
                from core.gesture_engine import DEFAULT_GESTURE_CONFIG, save_gesture_config
                save_settings(DEFAULT_SETTINGS)
                if UI_SETTINGS_PATH.exists():
                    save_json_atomic(UI_SETTINGS_PATH, UI_DEFAULTS, validator=lambda value: _validate_object(value, "UI settings"))
                    reload_ui_settings()
                save_gesture_config(DEFAULT_GESTURE_CONFIG)
                from core.gesture_engine import GestureEngine
                GestureEngine.get_instance().reload_settings()
            except Exception as error:
                print(f"[UI] Error restoring defaults: {error}")
                view.page().runJavaScript(f"showToast({json.dumps(f'Restore defaults failed: {error}')}, true);")
                return
            sync_all_config_to_ui()
            view.page().runJavaScript("showToast('All configuration restored to defaults.');")

        elif action == "window_minimize":
            from ui.window_pin import is_window_pinned
            if is_window_pinned():
                print("[UI] Window is pinned to desktop background: minimization blocked strictly.")
                view.page().runJavaScript("window.showToast && window.showToast('HUD is pinned to desktop background and cannot be minimized.');")
            else:
                view.showMinimized()

        elif action == "window_toggle_maximize":
            from ui.window_pin import is_window_pinned
            if is_window_pinned():
                print("[UI] Window is pinned to desktop background: maximize toggle blocked.")
            elif view.isMaximized() or view.isFullScreen():
                view.showNormal()
            else:
                view.showMaximized()

        elif action == "window_close":
            from ui.window_pin import is_window_pinned
            if is_window_pinned():
                print("[UI] Window is pinned to desktop background: close/minimize blocked strictly.")
                view.page().runJavaScript("window.showToast && window.showToast('HUD is pinned to desktop background.');")
            else:
                view.showMinimized()

        elif action == "window_start_drag":
            if hasattr(view, "windowHandle") and view.windowHandle():
                view.windowHandle().startSystemMove()

        elif action == "toggle_pin":
            from ui.window_pin import toggle_window_pin
            pinned = toggle_window_pin(view)
            view.page().runJavaScript(f"window.updatePinState && window.updatePinState({json.dumps(pinned)});")

        elif action == "launch_gesture_preview":
            target_gesture = str(payload.get("gesture", "")).strip() or None
            show_gesture_preview(target_gesture=target_gesture)

        elif action == "capture_app_scoped_gesture":
            app = str(payload.get("app", "")).strip()
            name = str(payload.get("name", "")).strip()
            gesture_action = str(payload.get("action", "")).strip()
            if not app or not name or not gesture_action:
                view.page().runJavaScript("showToast('App, custom gesture name, and action are required.', true);")
            else:
                editing_index = payload.get("editing_index")
                show_gesture_preview({
                    "app": app,
                    "name": name,
                    "action": gesture_action,
                    "app_scoped": True,
                    "editing_index": editing_index,
                })

        elif action == "capture_custom_gesture":
            name = str(payload.get("name", "")).strip()
            gesture_action = str(payload.get("action", "")).strip()
            editing_name = str(payload.get("editing_name", "")).strip() or None
            if not name or not gesture_action:
                view.page().runJavaScript("showToast('Gesture name and action are required to capture.', true);")
            else:
                from core.gesture_engine import load_gesture_config, check_duplicate_gesture
                config = load_gesture_config()
                name_err, act_err = check_duplicate_gesture(name, gesture_action, config, exclude_name=editing_name)
                if name_err:
                    view.page().runJavaScript(f"showToast({json.dumps(name_err)}, true);")
                elif act_err:
                    view.page().runJavaScript(f"showToast({json.dumps(act_err)}, true);")
                else:
                    show_gesture_preview({"name": name, "action": gesture_action, "editing_name": editing_name})

        elif action == "test_gesture":
            act = payload.get("action", "")
            if act:
                try:
                    from core.gesture_engine import execute_gesture_action
                    execute_gesture_action(act)
                    from core.event_logger import emit_event
                    emit_event("TOOL", f"TEST DIRECTIVE: {act}")
                except Exception as e:
                    print(f"[UI] Error executing test gesture: {e}")

    def handle_title_change(title: str):
        if title.startswith("JARVIS_APP_CMD:"):
            raw = title[len("JARVIS_APP_CMD:"):]
            try:
                import urllib.parse
                data = json.loads(urllib.parse.unquote(raw))
                action = data.get("action")
                if action == "app_minimize":
                    from ui.window_pin import is_window_pinned, toggle_window_pin
                    set_settings_active(False)
                    if is_window_pinned():
                        toggle_window_pin(view)
                    view.showMinimized()
                elif action == "app_open_settings":
                    set_settings_active(True)
                    view.page().runJavaScript("window.jarvisOpenSettings && window.jarvisOpenSettings();")
                    view.activateWindow()
                    view.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
                elif action == "app_exit":
                    set_settings_active(False)
                    app.quit()
            except Exception as error:
                print(f"[UI] Error handling app action: {error}")
        elif not title.startswith("JARVIS_CMD:"):
            from ui.window_pin import set_settings_active
            settings_open = "[SETTINGS_OPEN]" in title
            set_settings_active(settings_open)
            if settings_open:
                view.activateWindow()
                view.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        else:
            raw = title[len("JARVIS_CMD:"):]
            try:
                import urllib.parse
                data = json.loads(urllib.parse.unquote(raw))
                action = data.get("action")
                payload = data.get("payload", {})
                if not isinstance(action, str) or not isinstance(payload, dict):
                    raise ValueError("HUD command must contain a string action and object payload.")
                handle_backend_action(action, payload)
            except Exception as e:
                print(f"[UI] Error handling action from HUD: {e}")

    view.titleChanged.connect(handle_title_change)
    view.loadFinished.connect(
        lambda ok: print("[UI] Jarvis interface loaded." if ok else "[UI] Jarvis interface failed to load.")
    )

    from ui.window_pin import setup_free_window, is_window_pinned, pin_to_desktop_bottom

    # Render the HUD across the complete active display before placing the
    # native window in the desktop wallpaper layer.
    screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
    if screen:
        view.winId()
        view.windowHandle().setScreen(screen)
        geom = screen.geometry()
        view.setGeometry(geom)

    setup_free_window(view)
    view.show()
    # Apply z-order after Qt creates the native WebEngine surface. This keeps
    # the HUD behind normal windows while preserving its full-screen geometry.
    pin_to_desktop_bottom(view)

    class StrictPinEventFilter(QObject):
        def __init__(self, target_view):
            super().__init__(target_view)
            self._view = target_view

        def eventFilter(self, watched, event):
            if is_window_pinned():
                event_type = event.type()
                if event_type == QEvent.Type.WindowStateChange:
                    if self._view.isMinimized() or (self._view.windowState() & Qt.WindowState.WindowMinimized):
                        self._view.setWindowState(self._view.windowState() & ~Qt.WindowState.WindowMinimized)
                        pin_to_desktop_bottom(self._view)
                        return True
                elif event_type == QEvent.Type.Hide:
                    self._view.show()
                    return True
            return super().eventFilter(watched, event)

    pin_filter = StrictPinEventFilter(view)
    view.installEventFilter(pin_filter)
    if view.windowHandle():
        view.windowHandle().installEventFilter(pin_filter)

    orig_show_minimized = view.showMinimized
    def safe_show_minimized():
        if is_window_pinned():
            print("[UI] Window is pinned strictly to desktop background: minimization blocked.")
            return
        orig_show_minimized()
    view.showMinimized = safe_show_minimized

    def handle_screen_change():
        if is_window_pinned():
            current_screen = view.windowHandle().screen() if view.windowHandle() else QApplication.primaryScreen()
            if current_screen:
                geom = current_screen.geometry()
                target = (0, 0, geom.width(), geom.height())
                if (
                    view.x(), view.y(), view.width(), view.height()
                ) != target:
                    view.setGeometry(*target)
                pin_to_desktop_bottom(view)

    app.screenAdded.connect(lambda _: handle_screen_change())
    app.screenRemoved.connect(lambda _: handle_screen_change())
    if QApplication.primaryScreen():
        QApplication.primaryScreen().geometryChanged.connect(lambda _: handle_screen_change())

    metrics = SystemMetrics()

    def push_telemetry():
        from core.event_logger import get_live_state

        data = get_live_state()
        data["state"] = bridge.state
        data["overlay_pid"] = os.getpid()
        try:
            from core.gesture_engine import GestureEngine
            gesture_engine = GestureEngine.get_instance()
            data["gesture_active"] = gesture_engine.is_active()
            data["gesture_tracking_enabled"] = gesture_engine.is_enabled()
        except Exception:
            data["gesture_active"] = False
            data["gesture_tracking_enabled"] = False
        data.update(metrics.read())
        payload = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
        view.page().runJavaScript(f"window.updateHUDTelemetry && window.updateHUDTelemetry({payload});")

    def apply_ui_settings(ok):
        if ok:
            set_click_pass_through(ui_settings.get("click_pass_through", True))
            payload = json.dumps({"ui": ui_settings}, ensure_ascii=True, separators=(",", ":"))
            view.page().runJavaScript(f"window.applyJarvisSettings && window.applyJarvisSettings({payload});")
            sync_all_config_to_ui()
            push_telemetry()
            if is_window_pinned():
                view.page().runJavaScript("window.updatePinState && window.updatePinState(true);")

    view.loadFinished.connect(apply_ui_settings)

    # The voice listener and recorder publish audio_level events themselves.
    # Do not open a second microphone stream here: many Windows drivers cannot
    # share the default input device, and a competing meter stream can stall
    # speech capture or make the microphone appear unavailable.
    # Smooth live ballistics timer for voice dB meter (25 FPS)
    audio_meter_timer = QTimer(app)
    audio_meter_timer.setInterval(40)
    audio_meter_state = {"disp": 0.0, "last_sent": 0.0}

    def push_live_audio():
        lvl = bridge.level
        disp = audio_meter_state["disp"]
        if lvl > disp:
            disp = lvl
        else:
            disp = disp * 0.82
        audio_meter_state["disp"] = disp
        bridge._level = 0.0

        last_sent = audio_meter_state["last_sent"]
        if disp > 0.003 or last_sent > 0.003:
            audio_meter_state["last_sent"] = disp
            val = round(disp, 3)
            view.page().runJavaScript(f"window.updateAudioLevel && window.updateAudioLevel({val});")

    audio_meter_timer.timeout.connect(push_live_audio)
    audio_meter_timer.start()

    # Settings Watcher: monitor specific files only (avoids folder reload loops/flickering)
    settings_watcher = QFileSystemWatcher(app)
    for p in (UI_SETTINGS_PATH, SETTINGS_PATH, CORRECTIONS_PATH, GESTURE_PATH, DATABASE_PATH):
        if p.exists():
            settings_watcher.addPath(str(p))

    def reload_ui_settings():
        updated = _load_ui_settings(ui_settings)
        changed = updated != ui_settings
        ui_settings.update(updated)
        set_click_pass_through(ui_settings.get("click_pass_through", True))
        telemetry_timer.setInterval(ui_settings["telemetry_interval_ms"])
        payload = json.dumps({"ui": ui_settings}, ensure_ascii=True, separators=(",", ":"))
        view.page().runJavaScript(f"window.applyJarvisSettings && window.applyJarvisSettings({payload});")
        if changed:
            print("[UI] UI settings changes applied.")

    settings_reload_timer = QTimer(app)
    settings_reload_timer.setSingleShot(True)
    settings_reload_timer.setInterval(300)
    settings_reload_timer.timeout.connect(reload_ui_settings)
    settings_watcher.fileChanged.connect(lambda _path: settings_reload_timer.start())

    telemetry_timer = QTimer(app)
    telemetry_timer.setInterval(ui_settings["telemetry_interval_ms"])
    telemetry_timer.timeout.connect(push_telemetry)
    telemetry_timer.start()

    tray_icon = QApplication.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
    tray = QSystemTrayIcon(tray_icon, app)
    tray.setToolTip("Jarvis HUD")
    menu = QMenu()
    config_action = QAction("Open Config", menu)
    config_action.triggered.connect(lambda: os.startfile(str(ROOT.parent / "config" / "settings.json")))
    menu.addAction(config_action)
    ui_config_action = QAction("Open UI Settings", menu)
    ui_config_action.triggered.connect(lambda: os.startfile(str(UI_SETTINGS_PATH)))
    menu.addAction(ui_config_action)
    workspace_action = QAction("Set Workspace", menu)
    def choose_workspace():
        folder = QFileDialog.getExistingDirectory(None, "Choose Jarvis workspace")
        if folder:
            from core.settings import save_settings
            from core.events import event_bus
            save_settings({"workspace_dir": folder})
            event_bus.publish("workspace_changed", path=folder)
    workspace_action.triggered.connect(choose_workspace)
    menu.addAction(workspace_action)
    reconnect_action = QAction("Reconnect Mic", menu)
    reconnect_action.triggered.connect(lambda: bridge._bus.publish("reconnect_mic"))
    menu.addAction(reconnect_action)
    menu.addSeparator()
    quit_action = QAction("Quit", menu)
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)
    tray.setContextMenu(menu)
    tray.show()
    try:
        from core.gesture_engine import GestureEngine
        GestureEngine.get_instance().start()
        app.aboutToQuit.connect(lambda: GestureEngine.get_instance().stop())
    except Exception as exc:
        print(f"[UI] GestureEngine start notice: {exc}")

    if demo:
        from ui.demo import start_demo
        start_demo(bridge)
    if backend_start is not None:
        QTimer.singleShot(0, backend_start)
    return app.exec()
