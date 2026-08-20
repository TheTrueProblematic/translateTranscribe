"""Minimum reading time and the catch-up queue.

A line must stay on screen long enough to actually be read. When speech
outruns reading, later lines queue rather than flashing past, and the backlog
is reported so the speaker can see how far behind the display is.
"""
import asyncio
import time

import pytest

from livetranslate.chunker import Word
from livetranslate.pipeline import Pipeline

SHORT = "Estou pronto."
MEDIUM = "O IMU está com defeito no lado esquerdo."
LONG = "Não toque nesse conector, ele ainda está energizado e pode causar um choque."


class _Queue:
    """Stands in for the chunk queue so backlog pressure can be set exactly."""

    def __init__(self, size):
        self._size = size

    def qsize(self):
        return self._size


class RecordingServer:
    def __init__(self):
        self.shown: list[tuple[float, int, str]] = []
        self.backlog: list[int] = []

    async def send_line(self, seq, text, final, direction="en2pt"):
        if final:
            self.shown.append((time.perf_counter(), seq, text))

    async def send_status(self, **kw):
        pass

    async def send_english(self, text, partial="", note=""):
        pass

    async def send_level(self, rms):
        pass

    async def send_backlog(self, pending):
        self.backlog.append(pending)


class InstantTranslator:
    """Fast enough that any gap between lines is the reading hold, not work."""

    async def start(self):
        pass

    async def close(self):
        pass

    async def translate_stream(self, text, direction="en2pt"):
        await asyncio.sleep(0.02)
        yield (f"Linha {text[:24]}.", True)


@pytest.fixture
def pipe(cfg):
    return Pipeline(cfg, RecordingServer(), translator=InstantTranslator())


# ---------------- how long a line holds ----------------

def test_dwell_grows_with_line_length(pipe):
    pipe._queue = _Queue(0)
    assert pipe._dwell_ms(SHORT) < pipe._dwell_ms(MEDIUM) < pipe._dwell_ms(LONG)


def test_dwell_is_floored_for_very_short_lines(pipe, cfg):
    pipe._queue = _Queue(0)
    assert pipe._dwell_ms("Sim.") == cfg.get("display.min_dwell_ms")


def test_dwell_is_capped_so_nothing_lingers(pipe, cfg):
    pipe._queue = _Queue(0)
    huge = "palavra " * 200
    assert pipe._dwell_ms(huge) == cfg.get("display.max_dwell_ms")


def test_dwell_is_not_excessive_for_a_typical_line(pipe):
    """A normal sentence should read comfortably without dragging."""
    pipe._queue = _Queue(0)
    assert 1.5 <= pipe._dwell_ms(MEDIUM) / 1000 <= 4.0


# ---------------- catching up ----------------

def test_backlog_shortens_the_hold(pipe):
    pipe._queue = _Queue(0)
    relaxed = pipe._dwell_ms(LONG)
    pipe._queue = _Queue(6)
    assert pipe._dwell_ms(LONG) < relaxed


def test_catchup_never_makes_a_long_line_unreadable(pipe, cfg):
    """Compression must not reduce a long sentence to a glance."""
    pipe._queue = _Queue(0)
    relaxed = pipe._dwell_ms(LONG)
    pipe._queue = _Queue(50)                      # far past full pressure
    squeezed = pipe._dwell_ms(LONG)
    assert squeezed >= relaxed * cfg.get("display.catchup_floor") - 1
    assert squeezed > cfg.get("display.min_dwell_ms"), (
        "a long line was compressed to the same hold as a three-word one"
    )


def test_compression_is_bounded_below(pipe, cfg):
    pipe._queue = _Queue(1000)
    assert pipe._dwell_ms(SHORT) >= cfg.get("display.min_dwell_ms")


# ---------------- end to end through the pipeline ----------------

async def _speak(pipe, sentences):
    t = 0.0
    for s in sentences:
        for token in s.split():
            await pipe.feed_word(Word(token, t, t + 0.15, confidence=0.99))
            t += 0.18
        await pipe.flush()
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_fast_speech_queues_instead_of_flashing_past(cfg):
    """The reported problem: talking quickly replaced lines before they could
    be read. Every line must now hold the screen for its reading time."""
    server = RecordingServer()
    pipe = Pipeline(cfg, server, translator=InstantTranslator())
    await pipe.start()
    try:
        await _speak(pipe, [
            "this is the first sentence spoken very quickly indeed",
            "here is a second sentence following immediately after it",
            "and a third one with no pause at all between them",
            "then a fourth sentence still without any pause whatsoever",
        ])
        await asyncio.wait_for(pipe._queue.join(), timeout=60)
    finally:
        await pipe.stop()

    assert len(server.shown) == 4, "lines were lost instead of queued"
    gaps = [b[0] - a[0] for a, b in zip(server.shown, server.shown[1:])]
    floor = cfg.get("display.min_dwell_ms") / 1000.0
    assert min(gaps) >= floor * 0.9, (
        f"a line was replaced after only {min(gaps):.2f}s, below the "
        f"{floor:.2f}s minimum reading time"
    )
    assert [seq for _, seq, _ in server.shown] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_backlog_is_reported_and_returns_to_zero(cfg):
    server = RecordingServer()
    pipe = Pipeline(cfg, server, translator=InstantTranslator())
    await pipe.start()
    try:
        await _speak(pipe, [
            "one sentence spoken quickly right now for the test",
            "two more words following straight after with no pause",
            "three more words following straight after with no pause",
        ])
        await asyncio.wait_for(pipe._queue.join(), timeout=60)
        await asyncio.sleep(0.1)
    finally:
        await pipe.stop()

    assert server.backlog, "backlog was never reported"
    assert max(server.backlog) >= 1, "queue depth never rose while speaking fast"
    assert server.backlog[-1] == 0, "backlog did not clear once caught up"


@pytest.mark.asyncio
async def test_a_lone_line_is_not_delayed(cfg):
    """With nothing queued, text still streams straight to the screen."""
    server = RecordingServer()
    pipe = Pipeline(cfg, server, translator=InstantTranslator())
    await pipe.start()
    started = time.perf_counter()
    try:
        await _speak(pipe, ["a single sentence with nothing before it at all"])
        await asyncio.wait_for(pipe._queue.join(), timeout=30)
    finally:
        await pipe.stop()
    assert server.shown, "nothing was displayed"
    assert server.shown[0][0] - started < 1.0, "an unqueued line was held back"
