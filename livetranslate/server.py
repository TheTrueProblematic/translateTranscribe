"""Display server: static page + websocket, on one aiohttp app (spec section 3).

Binds loopback only. Nothing about this process talks to the network beyond
localhost, and the display page loads no external resources.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import WSMsgType, web

log = logging.getLogger("livetranslate.server")

STATIC_DIR = Path(__file__).resolve().parent / "static"


class DisplayServer:
    def __init__(self, cfg, on_command: Callable[[dict], Awaitable[None]] | None = None):
        self.settings = {
            "type": "settings",
            "show_english_monitor": bool(cfg.get("display.show_english_monitor", True)),
            "backlog_bar_full": int(cfg.get("display.backlog_bar_full", 8)),
        }
        self.host = cfg.get("server.host", "127.0.0.1")
        self.port = int(cfg.get("server.port", 8420))
        self.on_command = on_command

        self._clients: set[web.WebSocketResponse] = set()
        self._runner: web.AppRunner | None = None
        self._last_status: dict[str, Any] = {}
        # Last two rendered lines, replayed to any client that connects late.
        # Without this, reloading the page or plugging in the projector
        # mid-session leaves the audience staring at a blank screen until the
        # speaker happens to say something new.
        self._recent_lines: list[dict[str, Any]] = []
        self._last_english: dict[str, Any] | None = None
        self._last_backlog = -1

        # Chrome runs the display in a persistent profile, so without this a
        # stale display.js/display.css survives across restarts and the page
        # silently keeps running an old build. That is exactly how an added
        # feature can appear to be missing. Assets are tiny and local; never
        # cache them.
        @web.middleware
        async def no_store(request, handler):
            response = await handler(request)
            response.headers["Cache-Control"] = "no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            return response

        self.app = web.Application(middlewares=[no_store])
        self.app.add_routes([
            web.get("/", self._index),
            web.get("/ws", self._ws),
            web.get("/healthz", self._healthz),
            web.static("/", STATIC_DIR),
        ])

    # ---------------- routes ----------------

    async def _index(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    async def _healthz(self, _request: web.Request) -> web.StreamResponse:
        return web.json_response({"ok": True, "clients": len(self._clients)})

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self._clients.add(ws)
        await self._send(ws, self.settings)
        for line in self._recent_lines:
            await self._send(ws, line)
        if self._last_english:
            await self._send(ws, self._last_english)
        if self._last_status:
            await self._send(ws, self._last_status)
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if self.on_command:
                    await self.on_command(payload)
        finally:
            self._clients.discard(ws)
        return ws

    # ---------------- broadcast ----------------

    @staticmethod
    async def _send(ws: web.WebSocketResponse, payload: dict) -> None:
        try:
            await ws.send_str(json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass

    def _remember_line(self, payload: dict) -> None:
        # Collapse repeated updates for the same sequence so streaming a line
        # character by character does not grow this list without bound.
        if self._recent_lines and self._recent_lines[-1]["seq"] == payload["seq"]:
            self._recent_lines[-1] = payload
        else:
            self._recent_lines.append(payload)
        del self._recent_lines[:-2]

    async def broadcast(self, payload: dict) -> None:
        kind = payload.get("type")
        if kind == "status":
            self._last_status = payload
        elif kind == "line":
            self._remember_line(payload)
        elif kind == "english":
            self._last_english = payload
        elif kind == "clear":
            self._recent_lines.clear()
            self._last_english = None
        if not self._clients:
            return
        await asyncio.gather(
            *(self._send(ws, payload) for ws in list(self._clients)),
            return_exceptions=True,
        )

    async def send_line(self, seq: int, text: str, final: bool,
                        direction: str = "en2pt") -> None:
        await self.broadcast({"type": "line", "seq": seq, "text": text,
                              "final": final, "direction": direction})

    async def send_status(self, *, paused: bool, translating: bool, listening: bool) -> None:
        await self.broadcast({
            "type": "status", "paused": paused,
            "translating": translating, "listening": listening,
        })

    async def send_english(self, text: str, partial: str = "", note: str = "") -> None:
        """The speaker's monitor strip. Never part of the audience's reading."""
        await self.broadcast({"type": "english", "text": text,
                              "partial": partial, "note": note})

    async def send_backlog(self, pending: int) -> None:
        """How many lines have been said but not yet shown."""
        if pending == self._last_backlog:
            return
        self._last_backlog = pending
        await self.broadcast({"type": "backlog", "pending": pending})

    async def send_level(self, rms: float) -> None:
        await self.broadcast({"type": "level", "rms": rms})

    async def clear(self) -> None:
        await self.broadcast({"type": "clear"})

    # ---------------- lifecycle ----------------

    async def start(self) -> str:
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        try:
            await site.start()
        except OSError as exc:
            raise RuntimeError(
                f"Cannot bind {self.host}:{self.port} ({exc}).\n"
                "Another LiveTranslate may already be running, or change "
                "server.port in config.toml."
            ) from exc
        return self.url

    async def stop(self) -> None:
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    @property
    def client_count(self) -> int:
        return len(self._clients)
