"""Spec section 12, test 3: artificially delayed translation responses must
never scramble the display. Rendering is strictly in sequence order.
"""
import asyncio
import random

import pytest

from livetranslate.chunker import Word
from livetranslate.output_buffer import OrderedOutputBuffer
from livetranslate.pipeline import Pipeline


# ---------------- the buffer itself ----------------

def test_reversed_arrival_renders_in_order():
    buf = OrderedOutputBuffer()
    out = []
    for seq in (5, 4, 3, 2):
        out += buf.submit(seq, f"linha {seq}", True)
    assert out == []                       # nothing may show while 1 is missing
    out += buf.submit(1, "linha 1", True)
    assert [r.seq for r in out] == [1, 2, 3, 4, 5]


def test_partials_of_a_later_sequence_are_held():
    buf = OrderedOutputBuffer()
    assert buf.submit(2, "par", False) == []
    assert buf.submit(2, "parcial", True) == []
    out = buf.submit(1, "primeira", True)
    assert [(r.seq, r.text) for r in out] == [
        (1, "primeira"), (2, "par"), (2, "parcial")
    ]


def test_streaming_head_stays_open_for_live_updates():
    buf = OrderedOutputBuffer()
    buf.submit(1, "a", True)
    out = buf.submit(2, "par", False)
    assert [(r.seq, r.text, r.final) for r in out] == [(2, "par", False)]
    out = buf.submit(2, "parcial completa", True)
    assert out[-1].final and out[-1].seq == 2


def test_skipped_sequence_does_not_block_later_lines():
    buf = OrderedOutputBuffer()
    buf.submit(3, "terceira", True)
    buf.submit(2, "segunda", True)
    out = buf.skip(1)                      # chunk 1 was gated out
    assert [r.seq for r in out] == [2, 3]


def test_stale_update_after_close_is_dropped():
    buf = OrderedOutputBuffer()
    buf.submit(1, "primeira", True)
    assert buf.submit(1, "chegou tarde", True) == []


@pytest.mark.parametrize("seed", range(12))
def test_randomized_arrival_order_always_renders_sorted(seed):
    rng = random.Random(seed)
    n = 25
    order = list(range(1, n + 1))
    rng.shuffle(order)
    buf = OrderedOutputBuffer()
    rendered = []
    for seq in order:
        rendered += buf.submit(seq, f"linha {seq}", True)
    assert [r.seq for r in rendered] == list(range(1, n + 1))


# ---------------- through the real pipeline ----------------

class FakeServer:
    def __init__(self):
        self.lines = []

    async def send_line(self, seq, text, final):
        self.lines.append((seq, text, final))

    async def send_status(self, **kw):
        pass

    async def send_english(self, text, partial="", note=""):
        pass

    async def send_level(self, rms):
        pass


class DelayedTranslator:
    """Later chunks finish sooner, which is exactly the inversion that would
    scramble the display if ordering were not enforced."""

    def __init__(self, delays):
        self.delays = delays
        self.calls = 0

    async def start(self):
        pass

    async def close(self):
        pass

    async def translate_stream(self, text):
        idx = self.calls
        self.calls += 1
        await asyncio.sleep(self.delays[idx % len(self.delays)])
        yield (f"PT[{text}]", False)
        await asyncio.sleep(0.005)
        yield (f"PT[{text}].", True)


@pytest.mark.asyncio
async def test_pipeline_renders_in_sequence_order_under_inverted_delays(cfg):
    server = FakeServer()
    # Descending delays: chunk 1 is slowest, chunk 4 fastest.
    pipe = Pipeline(cfg, server, translator=DelayedTranslator([0.20, 0.15, 0.10, 0.01]))
    await pipe.start()
    try:
        sentences = [
            "the imu is reporting a fault on the left side today",
            "check the firmware version on the usb port right now",
            "do not touch that connector it is still live please",
            "the fuselage has a crack near the gimbal mount here",
        ]
        t = 0.0
        for s in sentences:
            for token in s.split():
                await pipe.feed_word(Word(token, t, t + 0.2, confidence=0.99))
                t += 0.25
            await pipe.flush()
        await asyncio.wait_for(pipe._queue.join(), timeout=15)
    finally:
        await pipe.stop()

    seqs = [seq for seq, _, _ in server.lines]
    assert seqs == sorted(seqs), f"display order was scrambled: {seqs}"
    finals = [seq for seq, _, final in server.lines if final]
    assert finals == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_gated_chunk_does_not_stall_the_following_lines(cfg):
    """A rejected chunk must not leave later translations stuck behind it."""
    server = FakeServer()
    pipe = Pipeline(cfg, server, translator=DelayedTranslator([0.01]))
    await pipe.start()
    try:
        t = 0.0
        for s in ["kwan doo voh say kee zher noon kah vee oo",      # rejected
                  "the imu is reporting a fault on the left side"]:  # accepted
            for token in s.split():
                await pipe.feed_word(Word(token, t, t + 0.2, confidence=0.99))
                t += 0.25
            await pipe.flush()
        await asyncio.wait_for(pipe._queue.join(), timeout=15)
    finally:
        await pipe.stop()

    finals = [(seq, text) for seq, text, final in server.lines if final]
    assert len(finals) == 1, f"gated text reached the display: {finals}"
    assert finals[0][0] == 2
