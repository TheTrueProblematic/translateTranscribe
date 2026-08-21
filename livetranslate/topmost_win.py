"""Keeping the overlay above a full-screen application, on Windows.

Tk's ``wm attributes -topmost`` asserts WS_EX_TOPMOST once, when the window is
created. That is enough to float over ordinary windowed applications, and it is
*not* enough over one running full screen: Windows raises a foreground
full-screen window above the topmost band, and applications that present full
screen frequently assert topmost themselves -- in which case whichever window
was raised last wins. A window that asserts its position only at startup
therefore disappears the moment the presented application takes the front.

Three things are done about that here.

1. The window is pushed back to the top of the topmost band on a timer, driven
   from the overlay's own Tk tick. Re-raising is the part that actually fixes
   the full-screen case; asserting once does not.
2. It is given WS_EX_NOACTIVATE and WS_EX_TRANSPARENT. NOACTIVATE means the
   overlay can never take keyboard focus, so raising it cannot pull a
   full-screen application out of full screen -- an application that loses
   focus usually drops out of exclusive mode or minimises itself, which would
   be a worse failure than subtitles behind a window. TRANSPARENT means mouse
   clicks pass through to whatever is underneath, so a strip across the bottom
   of the screen does not swallow a click meant for the application below it.
3. Tk destroys and recreates a toplevel's wrapper window when some attributes
   change, which silently drops styles applied to the old handle. The handle is
   re-read on every tick and restyled whenever it has changed.

What this cannot do: an application in *exclusive* full screen -- a Direct3D
swap chain flipped straight to the display -- is not composited by the desktop
window manager at all, and nothing drawn by any other process can appear over
it. No window style, Z-order call or timer changes that; the only fixes are
outside this process. ``exclusive_fullscreen()`` reports that state so it is
logged as itself instead of looking like a bug in the overlay, and the remedy
is to run the presented application in borderless or windowed full screen.

Every function is a no-op that reports it did nothing on other platforms, so
callers need no platform checks.
"""
from __future__ import annotations

import logging
import sys

log = logging.getLogger("livetranslate.overlay")

IS_WINDOWS = sys.platform == "win32"

GWL_EXSTYLE = -20

# Deliberately not WS_EX_TOPMOST: the documented way into the topmost band is
# SetWindowPos(HWND_TOPMOST), and setting the bit through SetWindowLong leaves
# the window's real Z-order band unchanged.
WS_EX_TRANSPARENT = 0x00000020     # clicks fall through to the window below
WS_EX_TOOLWINDOW = 0x00000080      # keep it out of Alt-Tab and the taskbar
WS_EX_NOACTIVATE = 0x08000000      # never take focus, however it is clicked

HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
_RAISE_FLAGS = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE

GA_ROOT = 2

# SHQueryUserNotificationState: what the shell believes is in front.
QUNS_NOT_PRESENT = 1
QUNS_BUSY = 2
QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4
QUNS_ACCEPTS_NOTIFICATIONS = 5
QUNS_QUIET_TIME = 6
QUNS_APP = 7

_STATE_NAMES = {
    QUNS_NOT_PRESENT: "screen locked or logged out",
    QUNS_BUSY: "a full-screen application is running",
    QUNS_RUNNING_D3D_FULL_SCREEN:
        "a full-screen exclusive Direct3D application is running",
    QUNS_PRESENTATION_MODE: "presentation mode",
    QUNS_ACCEPTS_NOTIFICATIONS: "ordinary desktop",
    QUNS_QUIET_TIME: "quiet hours",
    QUNS_APP: "a full-screen Store app is running",
}

_user32 = None
_shell32 = None


def describe_state(state: int | None) -> str:
    """A readable name for an SHQueryUserNotificationState value."""
    if state is None:
        return "unknown"
    return _STATE_NAMES.get(int(state), f"state {int(state)}")


def _api():
    """user32 with the prototypes used here declared, or None off Windows."""
    global _user32
    if not IS_WINDOWS:
        return None
    if _user32 is None:
        import ctypes
        from ctypes import wintypes

        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        u.GetAncestor.restype = wintypes.HWND
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.UINT]
        u.SetWindowPos.restype = wintypes.BOOL

        # 64-bit Windows has the ...Ptr forms; 32-bit has only the LONG ones.
        getter = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW
        setter = getattr(u, "SetWindowLongPtrW", None) or u.SetWindowLongW
        getter.argtypes = [wintypes.HWND, ctypes.c_int]
        getter.restype = ctypes.c_ssize_t
        setter.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        setter.restype = ctypes.c_ssize_t

        u._get_long = getter
        u._set_long = setter
        u._topmost = wintypes.HWND(HWND_TOPMOST)
        _user32 = u
    return _user32


def window_handle(root) -> int | None:
    """The top-level window handle behind a Tk root, or None.

    ``winfo_id()`` is the inner Tk window, not the wrapper the window manager
    orders, so it is walked up to its root ancestor. ``wm frame`` reports the
    wrapper directly but only as a string whose base differs between builds,
    which is a worse thing to depend on.
    """
    api = _api()
    if api is None or root is None:
        return None
    try:
        inner = int(root.winfo_id())
    except Exception:
        log.debug("no window id yet", exc_info=True)
        return None
    if not inner:
        return None
    try:
        return int(api.GetAncestor(inner, GA_ROOT) or inner)
    except Exception:
        log.debug("GetAncestor failed", exc_info=True)
        return inner


def harden(hwnd: int | None, click_through: bool = True) -> bool:
    """Add the extended styles an overlay needs. True if they are now set."""
    api = _api()
    if api is None or not hwnd:
        return False
    wanted = WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    if click_through:
        wanted |= WS_EX_TRANSPARENT
    try:
        current = api._get_long(hwnd, GWL_EXSTYLE)
        if current & wanted != wanted:
            api._set_long(hwnd, GWL_EXSTYLE, current | wanted)
            log.info("overlay window styles applied (click-through=%s)",
                     click_through)
        return True
    except Exception:
        log.debug("could not set window styles", exc_info=True)
        return False


def raise_to_top(hwnd: int | None) -> bool:
    """Move the window to the front of the topmost band, without focusing it."""
    api = _api()
    if api is None or not hwnd:
        return False
    try:
        return bool(api.SetWindowPos(hwnd, api._topmost, 0, 0, 0, 0,
                                     _RAISE_FLAGS))
    except Exception:
        log.debug("SetWindowPos failed", exc_info=True)
        return False


def notification_state() -> int | None:
    """What the shell believes is in front, or None if it cannot be asked.

    Call this from the UI thread: the shell expects a thread with a message
    pump.
    """
    global _shell32
    if not IS_WINDOWS:
        return None
    try:
        import ctypes

        if _shell32 is None:
            _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        state = ctypes.c_int(0)
        if _shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return None
        return int(state.value)
    except Exception:
        log.debug("SHQueryUserNotificationState unavailable", exc_info=True)
        return None


def exclusive_fullscreen() -> bool:
    """True when a full-screen exclusive Direct3D application is in front.

    That is the one case the overlay cannot win: such an application bypasses
    the desktop compositor entirely, so no other process can draw over it.
    """
    return notification_state() == QUNS_RUNNING_D3D_FULL_SCREEN
