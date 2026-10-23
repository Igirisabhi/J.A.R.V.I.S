"""
Window management module for Jarvis HUD.

Supports both:
1. Free Window Mode (Default): Resizable to any free size, minimizable, maximizable,
   movable across screens with native border-resize hit testing, zero flickering.
2. Pinned Desktop Mode (Optional): Can be toggled to pin the HUD to the desktop background.
"""

from __future__ import annotations

import sys
import ctypes
from typing import Any

PIN_TO_DESKTOP_BOTTOM = True

# Win32 Constants
GWL_STYLE = -16
GWL_EXSTYLE = -20
GWLP_WNDPROC = -4

WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_THICKFRAME = 0x00040000

WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000

HWND_BOTTOM = 1
HWND_NOTOPMOST = -2
HWND_TOPMOST = -1
HWND_TOP = 0

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080

WM_SIZE = 0x0005
SIZE_RESTORED = 0
SIZE_MINIMIZED = 1
SIZE_MAXIMIZED = 2

WM_ACTIVATE = 0x0006
WM_SHOWWINDOW = 0x0018
WM_WINDOWPOSCHANGING = 0x0046
WM_WINDOWPOSCHANGED = 0x0047
WM_MOUSEACTIVATE = 0x0021
MA_NOACTIVATE = 3
WM_SYSCOMMAND = 0x0112
SC_MINIMIZE = 0xF020

SW_RESTORE = 9
SW_SHOWNOACTIVATE = 4

WM_NCHITTEST = 0x0084
HTTRANSPARENT = -1
HTCLIENT = 1
HTCAPTION = 2
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17

_is_pinned = False
_settings_modal_active = False
_click_pass_through = True
_is_transparent = False
_pass_through_timer = None
_pinned_hwnd_p = None
_user32 = None
_old_wndproc = None
_wndproc_callback = None


if sys.platform == "win32":
    class WINDOWPOS(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("hwndInsertAfter", ctypes.c_void_p),
            ("x", ctypes.c_int),
            ("y", ctypes.c_int),
            ("cx", ctypes.c_int),
            ("cy", ctypes.c_int),
            ("flags", ctypes.c_uint),
        ]

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class POINT(ctypes.Structure):
        _fields_ = [
            ("x", ctypes.c_long),
            ("y", ctypes.c_long),
        ]

    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t,
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_size_t,
        ctypes.c_ssize_t,
    )
else:
    class WINDOWPOS(ctypes.Structure):
        pass

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class POINT(ctypes.Structure):
        _fields_ = [
            ("x", ctypes.c_long),
            ("y", ctypes.c_long),
        ]


def is_window_pinned() -> bool:
    return _is_pinned


def is_click_pass_through() -> bool:
    """Return whether desktop icon click pass-through is currently enabled."""
    return _click_pass_through


def set_click_pass_through(enabled: bool) -> None:
    """Enable or disable desktop icon click pass-through."""
    global _click_pass_through
    _click_pass_through = bool(enabled)
    if not _click_pass_through and _pinned_hwnd_p and _user32:
        _set_window_transparent(_pinned_hwnd_p, False, _user32)


def is_point_interactive(screen_x: int, screen_y: int, hwnd_p: Any, user32: Any) -> bool:
    """Check whether a screen coordinate falls within an interactive region of the HUD."""
    if _settings_modal_active:
        return True

    if not hwnd_p or not user32:
        return True

    rect = RECT()
    try:
        get_rect = getattr(user32, "GetWindowRect", None)
        if not get_rect or not get_rect(hwnd_p, ctypes.byref(rect)):
            return True
    except Exception:
        return True

    # If cursor is completely outside window bounds, do not capture clicks
    if screen_x < rect.left or screen_x >= rect.right or screen_y < rect.top or screen_y >= rect.bottom:
        return False

    rel_x = screen_x - rect.left
    rel_y = screen_y - rect.top
    win_w = rect.right - rect.left
    win_h = rect.bottom - rect.top

    # 1. Top bar: window controls, pin toggle, drag header
    if rel_y <= 60:
        return True

    # 2. Central Arc Reactor Core & Voice visualizer
    cx = win_w // 2
    cy = win_h // 2
    radius = min(win_w, win_h) * 0.22
    if (rel_x - cx) ** 2 + (rel_y - cy) ** 2 <= radius * radius:
        return True

    # 3. Controls Module / Settings Gear Icon (bottom-left HUD card)
    # The card sits at left 24px, width 310px, bottom 24px, height ~200px
    if 15 <= rel_x <= 360 and win_h - 280 <= rel_y <= win_h - 15:
        return True

    return False


def _set_window_transparent(hwnd_p: Any, transparent: bool, user32: Any) -> None:
    """Dynamically toggle WS_EX_TRANSPARENT without changing Z-order or moving window."""
    global _is_transparent
    if transparent == _is_transparent:
        return
    if not hwnd_p or not user32:
        _is_transparent = transparent
        return
    try:
        get_style = getattr(user32, "GetWindowLongPtrW", getattr(user32, "GetWindowLongW", None))
        set_style = getattr(user32, "SetWindowLongPtrW", getattr(user32, "SetWindowLongW", None))
        if not get_style or not set_style:
            return
        cur_ex = get_style(hwnd_p, GWL_EXSTYLE)
        if transparent:
            new_ex = cur_ex | WS_EX_TRANSPARENT
        else:
            new_ex = cur_ex & ~WS_EX_TRANSPARENT
        if new_ex != cur_ex:
            set_style(hwnd_p, GWL_EXSTYLE, new_ex)
            set_pos = getattr(user32, "SetWindowPos", None)
            if set_pos:
                set_pos(
                    hwnd_p,
                    None,
                    0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
                )
        _is_transparent = transparent
    except Exception:
        pass


def _tick_pass_through() -> None:
    """Timer callback to monitor mouse position and update click-through transparent state."""
    if not _is_pinned or not _click_pass_through:
        if _is_transparent and _pinned_hwnd_p and _user32:
            _set_window_transparent(_pinned_hwnd_p, False, _user32)
        return

    if _settings_modal_active:
        if _is_transparent and _pinned_hwnd_p and _user32:
            _set_window_transparent(_pinned_hwnd_p, False, _user32)
        return

    if not _pinned_hwnd_p or not _user32:
        return

    try:
        pt = POINT()
        get_cursor = getattr(_user32, "GetCursorPos", None)
        if get_cursor and get_cursor(ctypes.byref(pt)):
            interactive = is_point_interactive(pt.x, pt.y, _pinned_hwnd_p, _user32)
            _set_window_transparent(_pinned_hwnd_p, not interactive, _user32)
    except Exception:
        pass


def _ensure_pass_through_timer() -> None:
    global _pass_through_timer
    if _pass_through_timer is None:
        try:
            from PySide6.QtCore import QTimer
            _pass_through_timer = QTimer()
            _pass_through_timer.setInterval(30)
            _pass_through_timer.timeout.connect(_tick_pass_through)
            _pass_through_timer.start()
        except Exception:
            pass


def set_settings_active(active: bool) -> None:
    global _settings_modal_active
    active = bool(active)
    _settings_modal_active = active
    if not _is_pinned or not _pinned_hwnd_p or not _user32:
        return

    # Pinned HUDs use WS_EX_NOACTIVATE so they never steal keyboard focus.
    # Temporarily remove it while settings are open so text fields can type.
    try:
        get_style = getattr(_user32, "GetWindowLongPtrW", getattr(_user32, "GetWindowLongW", None))
        set_style = getattr(_user32, "SetWindowLongPtrW", getattr(_user32, "SetWindowLongW", None))
        if not get_style or not set_style:
            return
        current_ex = get_style(_pinned_hwnd_p, GWL_EXSTYLE)
        new_ex = current_ex & ~WS_EX_NOACTIVATE if active else current_ex | WS_EX_NOACTIVATE
        if new_ex != current_ex:
            set_style(_pinned_hwnd_p, GWL_EXSTYLE, new_ex)
            set_pos = getattr(_user32, "SetWindowPos", None)
            if set_pos:
                set_pos(
                    _pinned_hwnd_p,
                    None,
                    0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
                )
    except Exception:
        pass


def _resolve_hwnd(window: Any) -> int | None:
    hwnd = None
    if hasattr(window, "winId"):
        try:
            hwnd = int(window.winId())
        except Exception:
            hwnd = None
    elif isinstance(window, int):
        hwnd = window
    return hwnd


def _ensure_wndproc_hooked(window: Any, hwnd_p: ctypes.c_void_p, user32: Any) -> bool:
    global _wndproc_callback, _old_wndproc
    if getattr(window, "_jarvis_wndproc_hooked", False) and _wndproc_callback is not None:
        return True

    get_style = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    set_style = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    get_style.restype = ctypes.c_ssize_t
    get_style.argtypes = [ctypes.c_void_p, ctypes.c_int]
    set_style.restype = ctypes.c_ssize_t
    set_style.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]

    call_wndproc = user32.CallWindowProcW
    call_wndproc.restype = ctypes.c_ssize_t
    call_wndproc.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_size_t,
        ctypes.c_ssize_t,
    ]

    old_wndproc_val = get_style(hwnd_p, GWLP_WNDPROC)
    _old_wndproc = ctypes.c_void_p(old_wndproc_val)

    def wndproc(h, msg, wparam, lparam):
        if _is_pinned:
            # 1. Strictly block SC_MINIMIZE from menus, shortcuts (Win+M, Alt+Space+N, Aero shake)
            if msg == WM_SYSCOMMAND and (wparam & 0xFFF0) == SC_MINIMIZE:
                return 0

            # 2. Strictly block hiding or iconic minimizing on WM_WINDOWPOSCHANGING (e.g. Win+D Show Desktop)
            if msg == WM_WINDOWPOSCHANGING and lparam:
                try:
                    pos = ctypes.cast(lparam, ctypes.POINTER(WINDOWPOS)).contents
                    # Never allow hiding the pinned background window
                    pos.flags &= ~SWP_HIDEWINDOW
                    pos.flags |= SWP_SHOWWINDOW
                    # Strip attempts to reposition offscreen to iconic coordinates (-32000, -32000)
                    if pos.x <= -10000 or pos.y <= -10000:
                        pos.flags |= (SWP_NOMOVE | SWP_NOSIZE)
                    # Keep pinned strictly at HWND_BOTTOM
                    pos.hwndInsertAfter = ctypes.c_void_p(HWND_BOTTOM).value
                except Exception:
                    pass

            # 3. Strictly block window hiding
            if msg == WM_SHOWWINDOW and wparam == 0:
                return 0

            # 4. If WM_SIZE with SIZE_MINIMIZED somehow arrives, immediately restore to desktop bottom
            if msg == WM_SIZE and wparam == SIZE_MINIMIZED:
                user32.ShowWindow(h, SW_RESTORE)
                user32.SetWindowPos(
                    h,
                    ctypes.c_void_p(HWND_BOTTOM),
                    0, 0,
                    user32.GetSystemMetrics(0),
                    user32.GetSystemMetrics(1),
                    SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW,
                )
                return 0

            # 5. If window becomes iconic in WM_WINDOWPOSCHANGED, restore it immediately
            if msg == WM_WINDOWPOSCHANGED:
                try:
                    is_iconic = getattr(user32, "IsIconic", None)
                    if is_iconic and is_iconic(h):
                        user32.ShowWindow(h, SW_RESTORE)
                        user32.SetWindowPos(
                            h,
                            ctypes.c_void_p(HWND_BOTTOM),
                            0, 0,
                            user32.GetSystemMetrics(0),
                            user32.GetSystemMetrics(1),
                            SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW,
                        )
                except Exception:
                    pass

        if msg == WM_NCHITTEST:
            if _is_pinned:
                if _settings_modal_active or not _click_pass_through:
                    return HTCLIENT
                try:
                    x = lparam & 0xFFFF
                    if x >= 0x8000: x -= 0x10000
                    y = (lparam >> 16) & 0xFFFF
                    if y >= 0x8000: y -= 0x10000

                    if is_point_interactive(x, y, hwnd_p, user32):
                        _set_window_transparent(hwnd_p, False, user32)
                        return HTCLIENT
                    else:
                        _set_window_transparent(hwnd_p, True, user32)
                        return HTTRANSPARENT
                except Exception:
                    pass
            else:
                # Free window mode: check resize borders if not maximized
                is_max = getattr(window, "isMaximized", lambda: False)() or getattr(window, "isFullScreen", lambda: False)()
                if not is_max:
                    rect = RECT()
                    user32.GetWindowRect(hwnd_p, ctypes.byref(rect))
                    x = lparam & 0xFFFF
                    if x >= 0x8000: x -= 0x10000
                    y = (lparam >> 16) & 0xFFFF
                    if y >= 0x8000: y -= 0x10000

                    b = 8  # 8px resize edge
                    on_left = x < rect.left + b
                    on_right = x >= rect.right - b
                    on_top = y < rect.top + b
                    on_bottom = y >= rect.bottom - b

                    if on_top and on_left: return HTTOPLEFT
                    if on_top and on_right: return HTTOPRIGHT
                    if on_bottom and on_left: return HTBOTTOMLEFT
                    if on_bottom and on_right: return HTBOTTOMRIGHT
                    if on_left: return HTLEFT
                    if on_right: return HTRIGHT
                    if on_top: return HTTOP
                    if on_bottom: return HTBOTTOM

                return HTCLIENT

        return call_wndproc(_old_wndproc, h, msg, wparam, lparam)

    _wndproc_callback = WNDPROC(wndproc)
    try:
        setattr(window, "_jarvis_wndproc_hooked", True)
        setattr(window, "_free_wndproc_ref", _wndproc_callback)
    except Exception:
        pass

    set_style(hwnd_p, GWLP_WNDPROC, ctypes.cast(_wndproc_callback, ctypes.c_void_p).value)
    return True


def setup_free_window(window: Any) -> bool:
    """
    Configure the window for Free Mode:
    - Minimizable, maximizable, and resizable to any free size.
    - Native 8-directional border resize hit testing on WM_NCHITTEST.
    - Eliminates DWM bottom-reassertion flicker completely.
    """
    if sys.platform != "win32":
        return False

    hwnd = _resolve_hwnd(window)
    if not hwnd:
        return False

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except Exception as exc:
        print(f"[UI] Cannot load user32: {exc}")
        return False

    hwnd_p = ctypes.c_void_p(hwnd)
    global _pinned_hwnd_p, _user32, _is_pinned
    _pinned_hwnd_p = hwnd_p
    _user32 = user32
    _is_pinned = False
    _set_window_transparent(hwnd_p, False, user32)

    get_style = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    set_style = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    get_style.restype = ctypes.c_ssize_t
    get_style.argtypes = [ctypes.c_void_p, ctypes.c_int]
    set_style.restype = ctypes.c_ssize_t
    set_style.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]

    _ensure_wndproc_hooked(window, hwnd_p, user32)

    # Ensure window style includes minimize, maximize, and thick resize frame
    current_style = get_style(hwnd_p, GWL_STYLE)
    new_style = current_style | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_THICKFRAME
    set_style(hwnd_p, GWL_STYLE, new_style)

    # Clean ex_style: allow normal focus and interaction
    current_ex_style = get_style(hwnd_p, GWL_EXSTYLE)
    new_ex_style = (current_ex_style & ~WS_EX_TOPMOST & ~WS_EX_NOACTIVATE & ~WS_EX_TOOLWINDOW)
    set_style(hwnd_p, GWL_EXSTYLE, new_ex_style)

    user32.SetWindowPos(
        hwnd_p,
        ctypes.c_void_p(HWND_NOTOPMOST),
        0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_FRAMECHANGED | SWP_SHOWWINDOW,
    )

    print("[UI] Free window mode configured: native resize, minimize, and maximize enabled.")
    return True


def pin_to_desktop_bottom(window: Any, enabled: bool = True) -> bool:
    """
    Pin window strictly to desktop background layer:
    - Sets HWND_BOTTOM, WS_EX_TOOLWINDOW, and WS_EX_NOACTIVATE.
    - Removes WS_MINIMIZEBOX, WS_MAXIMIZEBOX, and WS_THICKFRAME to disallow minimization.
    - Native window procedure intercepts WM_WINDOWPOSCHANGING, WM_SYSCOMMAND, WM_SHOWWINDOW,
      and WM_SIZE to strictly prevent minimization or hiding (e.g. Win+D Show Desktop).
    """
    if not enabled or sys.platform != "win32":
        return False

    hwnd = _resolve_hwnd(window)
    if not hwnd:
        return False

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except Exception as exc:
        print(f"[UI] Cannot load user32: {exc}")
        return False

    hwnd_p = ctypes.c_void_p(hwnd)
    global _pinned_hwnd_p, _user32, _is_pinned
    _pinned_hwnd_p = hwnd_p
    _user32 = user32
    _is_pinned = True

    get_style = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    set_style = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    get_style.restype = ctypes.c_ssize_t
    get_style.argtypes = [ctypes.c_void_p, ctypes.c_int]
    set_style.restype = ctypes.c_ssize_t
    set_style.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]

    _ensure_wndproc_hooked(window, hwnd_p, user32)

    # Strip minimize/maximize boxes and thick resizing frame from style
    current_style = get_style(hwnd_p, GWL_STYLE)
    new_style = (current_style & ~WS_MINIMIZEBOX & ~WS_MAXIMIZEBOX & ~WS_THICKFRAME)
    set_style(hwnd_p, GWL_STYLE, new_style)

    # Apply tool window and no-activate styles; remove topmost
    current_ex_style = get_style(hwnd_p, GWL_EXSTYLE)
    new_ex_style = (
        (current_ex_style & ~WS_EX_TOPMOST)
        | WS_EX_TOOLWINDOW
        | WS_EX_NOACTIVATE
    )
    set_style(hwnd_p, GWL_EXSTYLE, new_ex_style)

    user32.SetWindowPos(
        hwnd_p,
        ctypes.c_void_p(HWND_BOTTOM),
        0, 0,
        user32.GetSystemMetrics(0),
        user32.GetSystemMetrics(1),
        SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW,
    )
    _ensure_pass_through_timer()
    print("[UI] Window pinned strictly to desktop background (never minimize mode active).")
    return True


def unpin_from_desktop(window: Any) -> bool:
    """Restore window to free floating mode."""
    return setup_free_window(window)


def toggle_window_pin(window: Any) -> bool:
    """Toggle between Free Window Mode and Pinned Desktop Mode."""
    global _is_pinned
    if _is_pinned:
        unpin_from_desktop(window)
        return False
    else:
        pin_to_desktop_bottom(window)
        return True
