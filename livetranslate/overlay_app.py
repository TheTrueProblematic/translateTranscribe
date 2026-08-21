"""Runs the pipeline behind the always-on-top subtitle overlay.

The awkward part is that tkinter insists on owning the main thread and is not
thread-safe, while the pipeline is asyncio. So:

    main thread          tkinter mainloop, drains a queue.Queue
    background thread    asyncio loop: server, ASR, translation

The server pushes every display payload into that queue through a local sink,
which is the same data the browser display receives. Nothing touches a widget
from the asyncio thread.

Startup failures are shown *in the overlay* as well as logged. On a machine
being tested remotely, a window saying "LM Studio is not reachable" is worth
far more than an empty screen and a log file nobody has opened yet.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading

log = logging.getLogger("livetranslate.overlay")


class OverlayApp:
    def __init__(self, cfg, args):
        self.cfg = cfg
        self.args = args
        self.inbox: "queue.Queue[dict]" = queue.Queue()
        self.overlay = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.pipeline = None
        self.server = None
        self.asr = None
        self.hotkeys = None
        self._engine_thread: threading.Thread | None = None
        self._stop_event: asyncio.Event | None = None
        self._startup_error: str | None = None
        self._started = threading.Event()

    # ---------------- engine, on the background thread ----------------

    def _run_engine(self) -> None:
        try:
            asyncio.run(self._engine())
        except Exception as exc:
            log.exception("engine thread died")
            self._startup_error = self._startup_error or str(exc)
            self._started.set()
            self._show_error(str(exc))

    async def _engine(self) -> None:
        from livetranslate.pipeline import Pipeline
        from livetranslate.server import DisplayServer
        from livetranslate.translator import LMStudioUnavailable, Translator

        self.loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()

        translator = Translator(self.cfg)
        try:
            await translator.preflight()
            await translator.warmup()
        except LMStudioUnavailable as exc:
            await translator.close()
            self._fail(str(exc))
            return

        self.server = DisplayServer(self.cfg)
        self.pipeline = Pipeline(self.cfg, self.server, translator=translator)

        async def on_command(payload: dict) -> None:
            if payload.get("type") == "toggle_pause":
                await self.pipeline.toggle_pause()

        self.server.on_command = on_command
        # Feed the overlay. queue.Queue is thread-safe, so this is the whole
        # bridge between the asyncio thread and the UI thread.
        self.server.add_sink(self.inbox.put)

        try:
            url = await self.server.start()
            log.info("display also available in a browser at %s", url)
        except RuntimeError as exc:
            await translator.close()
            self._fail(str(exc))
            return

        await self.pipeline.start()

        if not self.args.no_asr:
            from livetranslate.asr_backend import create_asr

            def on_word(word):
                asyncio.ensure_future(self.pipeline.feed_word(word))

            def on_tick(now_s, speech):
                asyncio.ensure_future(self.pipeline.tick(now_s, speech))

            def on_level(rms):
                asyncio.ensure_future(self.server.send_level(rms))

            def on_partial(text):
                asyncio.ensure_future(self.pipeline.set_partial(text))

            def on_epoch(epoch):
                self.pipeline.audio_epoch = epoch

            def on_state(ok):
                self.pipeline.set_listening(ok)
                asyncio.ensure_future(self.pipeline.publish_status())

            try:
                self.asr = create_asr(self.cfg, self.loop, on_word, on_tick,
                                      on_level, on_state, on_epoch, on_partial)
            except RuntimeError as exc:
                self._fail(str(exc))
                return

            self.asr.start()
            try:
                await asyncio.to_thread(self.asr.wait_ready)
            except Exception as exc:
                self.asr.stop()
                self._fail(f"Speech recognition failed to start.\n{exc}")
                return

        self._register_hotkeys()
        await self.pipeline.publish_status()
        self._started.set()
        log.info("overlay engine ready")

        try:
            await self._stop_event.wait()
        finally:
            if self.hotkeys:
                self.hotkeys.stop()
            if self.asr is not None:
                self.asr.stop()
            await self.pipeline.stop()
            await self.server.stop()
            log.info("overlay engine stopped")

    def _register_hotkeys(self) -> None:
        from livetranslate.hotkeys_win import GlobalHotkeys

        self.hotkeys = GlobalHotkeys(self.loop)
        toggle = self.cfg.get("overlay.hotkey_toggle", "ctrl+alt+s")
        move = self.cfg.get("overlay.hotkey_position", "ctrl+alt+t")
        pause = self.cfg.get("overlay.hotkey_pause", "ctrl+alt+p")
        quit_key = self.cfg.get("overlay.hotkey_quit", "ctrl+alt+q")

        # These run on the asyncio loop, so hop back to the UI thread for
        # anything that touches a widget.
        self.hotkeys.add(toggle, lambda: self._on_ui(lambda: self.overlay.toggle_visible()))
        self.hotkeys.add(move, lambda: self._on_ui(lambda: self.overlay.toggle_position()))
        self.hotkeys.add(pause, self._toggle_pause)
        # The overlay cannot be clicked or focused, so its own Escape binding is
        # unreachable on Windows; this is the way out that does not mean going
        # to find the console window.
        self.hotkeys.add(quit_key, lambda: self._on_ui(lambda: self.overlay.close()))
        self.hotkeys.start()

        if self.hotkeys.registered:
            log.info("global hotkeys: %s", ", ".join(self.hotkeys.registered))
        for binding, reason in self.hotkeys.failed:
            log.warning("hotkey %s unavailable: %s", binding, reason)

    def _toggle_pause(self):
        if self.pipeline is not None:
            return self.pipeline.toggle_pause()
        return None

    def _on_ui(self, fn) -> None:
        """Schedule work on the tkinter thread."""
        if self.overlay is not None and not self.overlay._closing:
            try:
                self.overlay.root.after(0, fn)
            except Exception:
                log.exception("could not schedule UI work")

    def _fail(self, message: str) -> None:
        log.error("startup failed: %s", message)
        self._startup_error = message
        self._started.set()
        self._show_error(message)

    def _show_error(self, message: str) -> None:
        first = message.strip().splitlines()[0] if message.strip() else "Startup failed"
        self.inbox.put({"type": "line", "text": f"LiveTranslate: {first}",
                        "direction": "en2pt", "final": True})

    # ---------------- UI, on the main thread ----------------

    def run(self) -> int:
        from livetranslate.overlay import SubtitleOverlay

        def on_command(payload):
            if self.loop is not None and self.pipeline is not None:
                self.loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(self.pipeline.toggle_pause())
                )

        self.overlay = SubtitleOverlay(self.cfg, self.inbox, on_command=on_command)
        self.inbox.put({"type": "line", "text": "LiveTranslate starting...",
                        "direction": "en2pt", "final": True})

        self._engine_thread = threading.Thread(
            target=self._run_engine, name="engine", daemon=True
        )
        self._engine_thread.start()

        try:
            self.overlay.run()
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
        return 1 if self._startup_error else 0

    def shutdown(self) -> None:
        if self.loop is not None and self._stop_event is not None:
            try:
                self.loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                pass
        if self._engine_thread is not None:
            self._engine_thread.join(timeout=10)
            self._engine_thread = None
