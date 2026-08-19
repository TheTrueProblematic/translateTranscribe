"""Offline harness for spec section 12 tests 4, 5 and 6.

Drives the real pipeline -- real ASR, real gate, real normalizer, real LM
Studio -- from recorded audio instead of a live microphone, so the integration
tests exercise production code paths rather than mocks.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
import wave
from pathlib import Path

import numpy as np

from livetranslate.pipeline import Pipeline

AUDIO_DIR = Path(__file__).resolve().parent / "audio"
SAMPLE_RATE = 16000

def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1, f"{path} is not mono"
        assert w.getframerate() == SAMPLE_RATE, f"{path} is not {SAMPLE_RATE} Hz"
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(SAMPLE_RATE * seconds), dtype=np.float32)


def rss_kb(pid: int | None = None) -> int:
    """Current resident set size in KB, without adding a psutil dependency."""
    pid = pid or os.getpid()
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    return int(out) if out else 0


class CapturingServer:
    """Stands in for DisplayServer, recording exactly what would be shown."""

    def __init__(self):
        self.lines: list[tuple[int, str, bool]] = []
        self.statuses: list[dict] = []
        self.levels: list[float] = []
        self.english: list[tuple[str, str, str]] = []

    async def send_line(self, seq, text, final):
        self.lines.append((seq, text, final))

    async def send_status(self, **kw):
        self.statuses.append(kw)

    async def send_english(self, text, partial="", note=""):
        self.english.append((text, partial, note))

    async def send_level(self, rms):
        self.levels.append(rms)

    async def clear(self):
        self.lines.clear()

    @property
    def final_lines(self) -> list[str]:
        return [t for _, t, final in self.lines if final and t]

    @property
    def displayed_text(self) -> str:
        return " ".join(self.final_lines)


class OfflineRun:
    """One pipeline run fed from an audio array."""

    def __init__(self, cfg, translator=None):
        self.cfg = cfg
        self.server = CapturingServer()
        self.pipeline = Pipeline(cfg, self.server, translator=translator)
        self.asr = None

    async def __aenter__(self):
        from livetranslate.asr import ParakeetASR

        loop = asyncio.get_running_loop()
        await self.pipeline.start()

        def on_word(w):
            asyncio.ensure_future(self.pipeline.feed_word(w))

        def on_tick(t, speech):
            asyncio.ensure_future(self.pipeline.tick(t, speech))

        def on_level(r):
            asyncio.ensure_future(self.server.send_level(r))

        def on_epoch(e):
            self.pipeline.audio_epoch = e

        # The model is loaded by the ASR's own worker thread: MLX streams are
        # thread-local, so a model cannot be shared across runs.
        self.asr = ParakeetASR(self.cfg, loop, on_word, on_tick, on_level, None, on_epoch)
        self.asr.start(use_microphone=False)
        await asyncio.get_running_loop().run_in_executor(None, self.asr.wait_ready)
        return self

    async def __aexit__(self, *exc):
        if self.asr:
            self.asr.stop()
        await self.pipeline.stop()

    async def feed(self, audio: np.ndarray, realtime: bool = True) -> None:
        """Push audio in mic-sized blocks.

        realtime=True paces at 1x so wall-clock latency measurements mean what
        they say; the audio clock and the wall clock stay aligned.
        """
        block_ms = int(self.cfg.get("asr.block_ms", 160))
        block = int(SAMPLE_RATE * block_ms / 1000)
        start = time.perf_counter()
        for i in range(0, len(audio), block):
            chunk = audio[i:i + block]
            if len(chunk) < block:
                chunk = np.pad(chunk, (0, block - len(chunk)))
            self.asr.push_audio(chunk.astype(np.float32))
            if realtime:
                target = start + ((i // block) + 1) * (block_ms / 1000.0)
                delay = target - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
            else:
                await asyncio.sleep(0)

    async def settle(self, timeout: float = 60.0) -> None:
        """Let trailing audio decode, flush the last word, drain translations."""
        await asyncio.sleep(1.0)
        self.asr.flush_pending_word()
        await asyncio.sleep(0.3)
        await self.pipeline.flush()
        try:
            await asyncio.wait_for(self.pipeline._queue.join(), timeout=timeout)
        except asyncio.TimeoutError:
            pass


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = (len(ordered) - 1) * (pct / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)
