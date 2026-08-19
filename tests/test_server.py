"""Display server and websocket behaviour (spec sections 9 and 10)."""
from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest

from livetranslate.server import DisplayServer


class _Cfg:
    """Minimal config stub so the server can bind an ephemeral port."""

    def __init__(self, port):
        self._d = {"server.host": "127.0.0.1", "server.port": port}

    def get(self, k, default=None):
        return self._d.get(k, default)

    def section(self, name):
        return {}


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def server():
    srv = DisplayServer(_Cfg(_free_port()))
    await srv.start()
    yield srv
    await srv.stop()


@pytest.mark.asyncio
async def test_serves_the_display_page_and_assets(server):
    async with aiohttp.ClientSession() as s:
        async with s.get(server.url) as r:
            assert r.status == 200
            body = await r.text()
        assert "display.css" in body and "display.js" in body
        # No external resources: the page must work with no network.
        assert "http://" not in body.replace(server.url, "")
        for asset in ("display.css", "display.js"):
            async with s.get(server.url + asset) as r:
                assert r.status == 200


@pytest.mark.asyncio
async def test_binds_loopback_only(server):
    assert server.host == "127.0.0.1"


@pytest.mark.asyncio
async def test_websocket_receives_lines_in_order(server):
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(server.url + "ws") as ws:
            await asyncio.sleep(0.1)
            for seq, text in ((1, "Primeira linha."), (2, "Segunda linha.")):
                await server.send_line(seq, text, True)
            got = []
            # A settings frame is sent on connect; collect until both lines land.
            while len([m for m in got if m.get("type") == "line"]) < 2:
                msg = await asyncio.wait_for(ws.receive(), timeout=5)
                got.append(json.loads(msg.data))
    lines = [m for m in got if m["type"] == "line"]
    assert [m["seq"] for m in lines] == [1, 2]
    assert lines[1]["text"] == "Segunda linha."


@pytest.mark.asyncio
async def test_late_client_gets_the_current_lines_replayed(server):
    """Reloading the page, or plugging in the projector mid-session, must not
    leave the audience looking at a blank screen."""
    await server.send_line(1, "Linha anterior.", True)
    await server.send_line(2, "Linha atual.", True)
    await server.send_status(paused=False, translating=False, listening=True)

    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(server.url + "ws") as ws:
            received = []
            while not any(m.get("type") == "status" for m in received):
                msg = await asyncio.wait_for(ws.receive(), timeout=5)
                received.append(json.loads(msg.data))
    lines = [m for m in received if m["type"] == "line"]
    assert [m["text"] for m in lines] == ["Linha anterior.", "Linha atual."]
    assert any(m["type"] == "status" for m in received)


@pytest.mark.asyncio
async def test_replay_buffer_stays_bounded_while_streaming(server):
    """Streaming a line character by character must not grow the replay list."""
    for i in range(1, 200):
        await server.send_line(1, "a" * i, False)
    await server.send_line(1, "final", True)
    assert len(server._recent_lines) <= 2


@pytest.mark.asyncio
async def test_client_commands_reach_the_handler(server):
    got = asyncio.Event()

    async def on_command(payload):
        if payload.get("type") == "toggle_pause":
            got.set()

    server.on_command = on_command
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(server.url + "ws") as ws:
            await ws.send_str(json.dumps({"type": "toggle_pause"}))
            await asyncio.wait_for(got.wait(), timeout=5)


@pytest.mark.asyncio
async def test_broadcast_survives_a_dropped_client(server):
    async with aiohttp.ClientSession() as s:
        ws = await s.ws_connect(server.url + "ws")
        await asyncio.sleep(0.1)
        await ws.close()
        await asyncio.sleep(0.1)
        await server.send_line(1, "ainda funciona", True)   # must not raise


@pytest.mark.asyncio
async def test_port_conflict_is_reported_clearly(server):
    clash = DisplayServer(_Cfg(server.port))
    with pytest.raises(RuntimeError) as exc:
        await clash.start()
    assert "already be running" in str(exc.value)
    await clash.stop()
