"""Always-on-top subtitle overlay.

The main way LiveTranslate runs on Windows: a borderless, always-on-top strip
of white text on a partially transparent black band, floating over whatever
application is being presented. No browser, no window chrome, nothing to
alt-tab to.

Built on tkinter, which ships with Python, so the overlay adds no dependency
and cannot fail to install.

Threading: tkinter must own the main thread, and it is not thread-safe. The
pipeline therefore runs its asyncio loop on a background thread and hands
finished lines over through a queue.Queue; the UI drains that queue from a
`root.after` tick. Nothing touches a widget from another thread.

Never more than two lines are shown. Text is wrapped to the window width, and
if it still will not fit the font steps down through a small range; only if it
still overflows is the beginning dropped. That order matters -- shrinking is
less disruptive to read than losing words.
"""
from __future__ import annotations

import logging
import queue
import sys
import tkinter as tk
import tkinter.font as tkfont

log = logging.getLogger("livetranslate.overlay")

# Direction -> colour. Portuguese spoken in the room, translated back into
# English, is blue so it cannot be mistaken for the presenter's own words.
COLOUR_DEFAULT = "#FFFFFF"
COLOUR_FROM_PT = "#6FB4FF"


class SubtitleOverlay:
    def __init__(self, cfg, inbox: "queue.Queue[dict]", on_command=None):
        self.inbox = inbox
        self.on_command = on_command          # called with a dict, e.g. toggle_pause

        self.font_family = cfg.get("overlay.font", "Segoe UI")
        self.font_size = int(cfg.get("overlay.font_size", 34))
        self.min_font_size = int(cfg.get("overlay.min_font_size", 22))
        self.opacity = float(cfg.get("overlay.opacity", 0.85))
        self.width_fraction = float(cfg.get("overlay.width_fraction", 0.82))
        self.margin_fraction = float(cfg.get("overlay.margin_fraction", 0.06))
        self.pad_x = int(cfg.get("overlay.pad_x", 28))
        self.pad_y = int(cfg.get("overlay.pad_y", 18))
        self.position = str(cfg.get("overlay.position", "bottom")).lower()
        self.background = cfg.get("overlay.background", "#000000")
        self.foreground = cfg.get("overlay.foreground", COLOUR_DEFAULT)
        self.max_lines = 2                    # deliberately not configurable

        self.visible = True
        self._text = ""
        self._direction = "en2pt"
        self._closing = False

        self.root = tk.Tk()
        self.root.title("LiveTranslate")
        self.root.overrideredirect(True)       # no title bar, no border
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", self.opacity)
        except tk.TclError:
            log.warning("window transparency unavailable on this platform")
        self.root.configure(bg=self.background)

        self.frame = tk.Frame(self.root, bg=self.background)
        self.frame.pack(fill="both", expand=True)

        self._font = tkfont.Font(family=self.font_family, size=self.font_size,
                                 weight="bold")
        if self.font_family not in tkfont.families():
            fallback = "Helvetica" if sys.platform == "darwin" else "Arial"
            log.warning("font %r not found; using %s", self.font_family, fallback)
            self._font.configure(family=fallback)

        self.label = tk.Label(
            self.frame, text="", font=self._font, fg=self.foreground,
            bg=self.background, justify="left", anchor="w",
        )
        self.label.pack(fill="both", expand=True, padx=self.pad_x, pady=self.pad_y)

        self._bind_local_keys()
        self._layout()
        self.root.after(50, self._drain)

    # ---------------- geometry ----------------

    @property
    def _screen(self) -> tuple[int, int]:
        return self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _window_width(self) -> int:
        sw, _ = self._screen
        return max(400, int(sw * self.width_fraction))

    def _line_height(self) -> int:
        return self._font.metrics("linespace")

    def _window_height(self) -> int:
        return self._line_height() * self.max_lines + self.pad_y * 2 + 6

    def _layout(self) -> None:
        """Size and place the strip for the current position and font."""
        sw, sh = self._screen
        w, h = self._window_width(), self._window_height()
        x = (sw - w) // 2
        margin = int(sh * self.margin_fraction)
        y = (sh - h - margin) if self.position == "bottom" else margin
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.label.configure(wraplength=w - self.pad_x * 2)

    def toggle_position(self) -> str:
        self.position = "top" if self.position == "bottom" else "bottom"
        log.info("overlay moved to %s", self.position)
        self._layout()
        self._render()
        return self.position

    def toggle_visible(self) -> bool:
        self.visible = not self.visible
        if self.visible:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
        else:
            self.root.withdraw()
        log.info("overlay %s", "shown" if self.visible else "hidden")
        return self.visible

    # ---------------- text fitting ----------------

    def _wrap(self, text: str, width: int, font: tkfont.Font) -> list[str]:
        """Greedy word wrap using real font metrics."""
        lines: list[str] = []
        current = ""
        for word in text.split():
            trial = f"{current} {word}".strip()
            if current and font.measure(trial) > width:
                lines.append(current)
                current = word
            else:
                current = trial
        if current:
            lines.append(current)
        return lines

    def _fit(self, text: str) -> tuple[str, int]:
        """Return text trimmed to at most two lines, and the size to show it at.

        Tries the configured size first, then steps down to min_font_size, and
        only drops leading words if it still does not fit.
        """
        if not text:
            return "", self.font_size
        width = self._window_width() - self.pad_x * 2
        probe = tkfont.Font(family=self._font.cget("family"), weight="bold",
                            size=self.font_size)

        for size in range(self.font_size, self.min_font_size - 1, -2):
            probe.configure(size=size)
            lines = self._wrap(text, width, probe)
            if len(lines) <= self.max_lines:
                return "\n".join(lines), size

        # Still too long even at the smallest size: keep the end of the
        # sentence, which is the part still being spoken.
        probe.configure(size=self.min_font_size)
        words = text.split()
        while words:
            words.pop(0)
            candidate = "… " + " ".join(words)
            lines = self._wrap(candidate, width, probe)
            if len(lines) <= self.max_lines:
                return "\n".join(lines), self.min_font_size
        return text[:80], self.min_font_size

    def _render(self) -> None:
        fitted, size = self._fit(self._text)
        if size != self._font.cget("size"):
            self._font.configure(size=size)
            self._layout()
        colour = COLOUR_FROM_PT if self._direction == "pt2en" else self.foreground
        self.label.configure(text=fitted, fg=colour)

    # ---------------- messages from the pipeline ----------------

    def set_line(self, text: str, direction: str = "en2pt") -> None:
        self._text = text or ""
        self._direction = direction or "en2pt"
        self._render()

    def _handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "line":
            self.set_line(msg.get("text", ""), msg.get("direction", "en2pt"))
        elif kind == "clear":
            self.set_line("")
        elif kind == "status":
            # Paused is shown by dimming rather than by a banner: this window
            # sits over someone else's application and must not shout.
            paused = bool(msg.get("paused"))
            self.label.configure(fg="#8A8A8A" if paused else (
                COLOUR_FROM_PT if self._direction == "pt2en" else self.foreground))
        elif kind == "shutdown":
            self.close()

    def _drain(self) -> None:
        """Pump the queue on the UI thread. Never blocks."""
        if self._closing:
            return
        try:
            for _ in range(64):                # bounded: keep the UI responsive
                try:
                    msg = self.inbox.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle(msg)
                except Exception:
                    log.exception("overlay failed to handle %r", msg.get("type"))
        finally:
            if not self._closing:
                self.root.after(50, self._drain)

    # ---------------- keys ----------------

    def _bind_local_keys(self) -> None:
        """Keys that work when the overlay itself has focus.

        The global hotkeys registered with Windows are the ones that matter in
        use, since this window never takes focus; these are a fallback and make
        the overlay testable on any platform.
        """
        self.root.bind("<Escape>", lambda e: self.close())
        self.root.bind("<space>", lambda e: self._command({"type": "toggle_pause"}))
        self.root.bind("t", lambda e: self.toggle_position())
        self.root.bind("h", lambda e: self.toggle_visible())

    def _command(self, payload: dict) -> None:
        if self.on_command:
            try:
                self.on_command(payload)
            except Exception:
                log.exception("overlay command failed")

    # ---------------- lifecycle ----------------

    def run(self) -> None:
        log.info("overlay running at %s, %dpx %s", self.position,
                 self.font_size, self._font.cget("family"))
        self.root.mainloop()

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        log.info("overlay closing")
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass
