"""System-wide hotkeys on Windows, via RegisterHotKey.

The overlay never takes keyboard focus -- that is the point of it -- so its own
key bindings only work if you click it first. These hotkeys work regardless of
which application is in front.

Implemented with ctypes against user32 rather than a third-party package: no
extra dependency, no administrator rights, and no keyboard hook that antivirus
software might object to.

Windows requires that RegisterHotKey and the message loop that receives
WM_HOTKEY live on the *same* thread, so both run on a dedicated thread here.
Callbacks are handed back to the asyncio loop with call_soon_threadsafe; they
never run on the message-pump thread.

On any other platform every method is a no-op that reports it did nothing, so
callers need no platform checks.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading

log = logging.getLogger("livetranslate.hotkeys")

IS_WINDOWS = sys.platform == "win32"

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000            # ignore auto-repeat while a key is held

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

_MODIFIERS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN, "super": MOD_WIN, "cmd": MOD_WIN,
}

# Virtual-key codes for the non-character keys worth binding.
_NAMED_KEYS = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "tab": 0x09, "backspace": 0x08, "up": 0x26, "down": 0x28, "left": 0x25,
    "right": 0x27, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "insert": 0x2D, "delete": 0x2E, "pause": 0x13,
    **{f"f{i}": 0x70 + i - 1 for i in range(1, 25)},
}


class HotkeyError(ValueError):
    """A binding string that cannot be parsed."""


def parse_binding(binding: str) -> tuple[int, int]:
    """Turn "ctrl+alt+s" into (modifier flags, virtual key code)."""
    parts = [p.strip().lower() for p in str(binding).split("+") if p.strip()]
    if not parts:
        raise HotkeyError(f"empty hotkey binding: {binding!r}")

    mods = 0
    key = None
    for part in parts:
        if part in _MODIFIERS:
            mods |= _MODIFIERS[part]
        elif key is None:
            key = part
        else:
            raise HotkeyError(f"more than one non-modifier key in {binding!r}")

    if key is None:
        raise HotkeyError(f"no key in {binding!r}, only modifiers")
    if key in _NAMED_KEYS:
        vk = _NAMED_KEYS[key]
    elif len(key) == 1 and (key.isalpha() or key.isdigit()):
        vk = ord(key.upper())
    else:
        raise HotkeyError(f"unrecognised key {key!r} in {binding!r}")

    if not mods:
        raise HotkeyError(
            f"{binding!r} has no modifier. A bare key would be captured "
            "system-wide and swallowed from every other application."
        )
    return mods | MOD_NOREPEAT, vk


class GlobalHotkeys:
    """Registers hotkeys and dispatches them onto the asyncio loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self._bindings: list[tuple[str, int, int, object]] = []
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self.active = False
        self.registered: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def add(self, binding: str, callback) -> bool:
        """Queue a hotkey. Returns False if the binding cannot be parsed."""
        try:
            mods, vk = parse_binding(binding)
        except HotkeyError as exc:
            log.warning("ignoring hotkey %r: %s", binding, exc)
            self.failed.append((binding, str(exc)))
            return False
        self._bindings.append((binding, mods, vk, callback))
        return True

    def start(self) -> bool:
        """Start the message-pump thread. False if unavailable or nothing bound."""
        if not IS_WINDOWS:
            log.info("global hotkeys are Windows-only; skipping on %s", sys.platform)
            return False
        if not self._bindings:
            return False
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)
        return self.active

    def _run(self) -> None:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                          wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                       wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = ctypes.c_int

        self._thread_id = kernel32.GetCurrentThreadId()
        handlers: dict[int, object] = {}

        for index, (binding, mods, vk, callback) in enumerate(self._bindings, start=1):
            if user32.RegisterHotKey(None, index, mods, vk):
                handlers[index] = callback
                self.registered.append(binding)
                log.info("hotkey registered: %s", binding)
            else:
                err = ctypes.get_last_error()
                reason = ("already taken by another application"
                          if err == 1409 else f"error {err}")
                log.warning("could not register hotkey %s: %s", binding, reason)
                self.failed.append((binding, reason))

        if not handlers:
            log.warning("no global hotkeys could be registered")
            self._ready.set()
            return

        self.active = True
        self._ready.set()

        msg = wintypes.MSG()
        try:
            while True:
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):            # WM_QUIT, or an error
                    break
                if msg.message == WM_HOTKEY:
                    callback = handlers.get(int(msg.wParam))
                    if callback is not None:
                        self._dispatch(callback)
        finally:
            for index in handlers:
                user32.UnregisterHotKey(None, index)
            self.active = False
            log.info("hotkey listener stopped")

    def _dispatch(self, callback) -> None:
        """Run the callback on the asyncio loop, never on the pump thread."""
        def call():
            try:
                result = callback()
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception:
                log.exception("hotkey callback failed")

        try:
            self.loop.call_soon_threadsafe(call)
        except RuntimeError:
            log.debug("event loop closed; hotkey ignored")

    def stop(self) -> None:
        if not (IS_WINDOWS and self._thread and self._thread_id):
            return
        try:
            import ctypes
            ctypes.WinDLL("user32", use_last_error=True).PostThreadMessageW(
                self._thread_id, WM_QUIT, 0, 0
            )
        except Exception:
            log.debug("could not post WM_QUIT to the hotkey thread", exc_info=True)
        self._thread.join(timeout=2)
        self._thread = None
        self.active = False
