"""Pipeline orchestration (spec section 3).

  words -> chunker -> normalizer -> gate -> translator -> ordered buffer -> display

Note on gate placement: the spec diagram puts the gate before the chunker. It
runs at chunk level here instead. Language identification on a 2-3 word ASR
fragment is unreliable, and a chunk carries both enough text to score and a
mean confidence over its words. Cost is identical; accuracy is materially
better. Rejected chunks call OrderedOutputBuffer.skip() so the sequence stays
contiguous and later lines are never held waiting on a dropped one.

No batching, no backpressure (spec section 6): one in-flight translation, a
plain FIFO, and sequence numbers assigned at chunk time.
"""
from __future__ import annotations

import asyncio
import logging
import time

from collections import deque

from .chunker import Chunk, Chunker, Word
from .gate import Gate
from .normalizer import Normalizer
from .output_buffer import OrderedOutputBuffer
from .transcript import TranscriptWriter
from .translator import Translator

log = logging.getLogger("livetranslate.pipeline")
timing_log = logging.getLogger("livetranslate.timing")


class Pipeline:
    def __init__(self, cfg, server, translator: Translator | None = None):
        self.cfg = cfg
        self.server = server
        self.chunker = Chunker(cfg)
        self.normalizer = Normalizer(cfg)
        self.gate = Gate(cfg, normalizer=self.normalizer)
        self.translator = translator or Translator(cfg)
        self.buffer = OrderedOutputBuffer()
        self.transcript = TranscriptWriter(cfg)

        self._queue: asyncio.Queue[Chunk] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._translating = False
        self._listening = False

        # Rolling English transcript for the monitor strip on the display.
        # This is for the speaker, not the audience: it is the fastest way to
        # tell "the mic is dead" apart from "the gate is dropping everything".
        self._english: deque[str] = deque(maxlen=18)
        self._partial = ""

        # Minimum reading time. A line holds the screen until it has been up
        # long enough to read; anything said in the meantime waits its turn
        # instead of flashing past. See _dwell_ms().
        self.reading_cps = float(cfg.get("display.reading_cps", 13.5))
        self.dwell_lead_in_ms = float(cfg.get("display.dwell_lead_in_ms", 250))
        self.min_dwell_ms = float(cfg.get("display.min_dwell_ms", 1300))
        self.max_dwell_ms = float(cfg.get("display.max_dwell_ms", 4500))
        self.catchup_backlog = max(1, int(cfg.get("display.catchup_backlog", 6)))
        self.catchup_floor = float(cfg.get("display.catchup_floor", 0.55))
        self._display_free_at = 0.0     # loop clock; 0 = free immediately
        self._awaiting_display = False
        self.stats = {"chunks": 0, "accepted": 0, "rejected": 0,
                      "translated": 0, "translation_errors": 0}

        # Latency samples: end-of-phrase -> first character on screen.
        # audio_epoch is the wall-clock time corresponding to audio t=0. When
        # it is known, latency is measured from the real end of the phrase
        # (chunk.end on the audio timeline), which includes ASR decode lag and
        # the silence-detection wait. Without it we can only measure from the
        # moment the chunk was emitted, which understates the true figure.
        self.audio_epoch: float | None = None
        # Two different questions, kept apart now that lines are held for
        # reading time:
        #   ready_latencies  end-of-phrase -> translation ready to show. This
        #                    is how responsive the pipeline is.
        #   first_char_latencies  end-of-phrase -> actually on screen. Larger
        #                    whenever the speaker outruns reading, by design.
        self.ready_latencies: list[float] = []
        self.first_char_latencies: list[float] = []
        self.total_latencies: list[float] = []
        self._chunk_wall: dict[int, float] = {}
        self._chunk_end: dict[int, float] = {}

    # ---------------- lifecycle ----------------

    async def start(self) -> None:
        self.transcript.open()
        await self.translator.start()
        if self._worker is None:
            self._worker = asyncio.create_task(self._run_worker())
        await self.publish_status()

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        await self.translator.close()
        self.transcript.close()

    # ---------------- inputs from ASR ----------------

    def set_listening(self, value: bool) -> None:
        self._listening = value

    async def feed_word(self, word: Word) -> None:
        self._english.append(word.text)
        await self._publish_english()
        for chunk in self.chunker.add_word(word):
            await self._enqueue(chunk)

    async def set_partial(self, text: str) -> None:
        """Live, not-yet-committed speech, shown greyed on the monitor strip."""
        if text == self._partial:
            return
        self._partial = text
        await self._publish_english()

    async def _publish_english(self, note: str = "") -> None:
        await self.server.send_english(
            " ".join(self._english), self._partial, note
        )

    async def tick(self, now_s: float, speech_active: bool = False) -> None:
        for chunk in self.chunker.tick(now_s, speech_active):
            await self._enqueue(chunk)

    async def flush(self) -> None:
        for chunk in self.chunker.flush():
            await self._enqueue(chunk)

    async def _enqueue(self, chunk: Chunk) -> None:
        # Wall time at end-of-phrase, for the latency measurement in section 12.
        self._chunk_wall[chunk.seq] = time.perf_counter()
        self._chunk_end[chunk.seq] = chunk.end
        await self._queue.put(chunk)
        await self._publish_backlog()

    # ---------------- reading time ----------------

    @property
    def backlog(self) -> int:
        """Lines said but not yet shown."""
        return self._queue.qsize() + (1 if self._awaiting_display else 0)

    def _dwell_ms(self, text: str) -> float:
        """How long this line should hold the screen.

        Proportional to length, floored so even three words get a beat, and
        capped so nothing lingers. Compressed toward the floor as the backlog
        grows, so the display catches up on its own rather than falling further
        behind the longer the speaker talks.
        """
        raw = self.dwell_lead_in_ms + (len(text) / self.reading_cps) * 1000.0
        dwell = max(self.min_dwell_ms, min(self.max_dwell_ms, raw))

        # Under backlog, hurry -- but not past the point of legibility. The
        # floor scales with the length of the line, so catching up never
        # reduces a long sentence to the same 1.3s a three-word one gets.
        floor = max(self.min_dwell_ms, dwell * self.catchup_floor)
        pressure = min(1.0, self._queue.qsize() / self.catchup_backlog)
        return dwell + (floor - dwell) * pressure

    async def _publish_backlog(self) -> None:
        await self.server.send_backlog(self.backlog)

    async def _wait_for_display_slot(self) -> None:
        loop = asyncio.get_running_loop()
        remaining = self._display_free_at - loop.time()
        if remaining <= 0:
            return
        self._awaiting_display = True
        await self._publish_backlog()
        try:
            # Re-check rather than sleeping blind: the slot may already have
            # passed while the translation was running.
            while True:
                remaining = self._display_free_at - loop.time()
                if remaining <= 0:
                    return
                await asyncio.sleep(min(remaining, 0.25))
        finally:
            self._awaiting_display = False

    def _hold_display(self, text: str) -> None:
        loop = asyncio.get_event_loop()
        self._display_free_at = loop.time() + self._dwell_ms(text) / 1000.0

    # ---------------- tier 4 manual hold ----------------

    async def toggle_pause(self) -> bool:
        paused = self.gate.toggle()
        if paused:
            # Drop whatever was mid-sentence so the held-over words do not
            # reappear as a stale line when listening resumes.
            for chunk in self.chunker.flush():
                self.buffer.skip(chunk.seq)
                log.info("HELD [paused] discarded chunk %d: %r", chunk.seq, chunk.text)
        await self.publish_status()
        return paused

    async def publish_status(self) -> None:
        await self.server.send_status(
            paused=self.gate.paused,
            translating=self._translating,
            listening=self._listening,
        )

    # ---------------- worker ----------------

    async def _run_worker(self) -> None:
        while True:
            chunk = await self._queue.get()
            try:
                await self._process(chunk)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("chunk %d failed; skipping to keep order", chunk.seq)
                for r in self.buffer.skip(chunk.seq):
                    await self.server.send_line(r.seq, r.text, r.final)
            finally:
                self._chunk_wall.pop(chunk.seq, None)
                self._chunk_end.pop(chunk.seq, None)
                self._queue.task_done()

    def _phrase_end_wall(self, chunk: Chunk) -> float:
        if self.audio_epoch is not None:
            return self.audio_epoch + chunk.end
        return self._chunk_wall.get(chunk.seq, time.perf_counter())

    async def _process(self, chunk: Chunk) -> None:
        self.stats["chunks"] += 1
        normalized = self.normalizer.normalize(chunk.text)
        if normalized != chunk.text:
            log.debug("normalized [%d] %r -> %r", chunk.seq, chunk.text, normalized)

        decision = self.gate.evaluate(
            normalized, confidence=chunk.mean_confidence, word_count=chunk.word_count
        )
        if not decision.accepted:
            self.stats["rejected"] += 1
            # The Portuguese display never shows this, but the speaker's
            # monitor strip does: silently dropping speech with no feedback is
            # exactly how a paused gate went unnoticed for a whole session.
            await self._publish_english(
                note=f"dropped ({decision.reason}, conf {decision.confidence:.2f},"
                     f" en {decision.english:.2f})"
            )
            self.transcript.write(
                seq=chunk.seq, english=normalized, raw_english=chunk.text,
                accepted=False, reason=decision.reason,
                confidence=decision.confidence, english_score=decision.english,
                audio_start=chunk.start, audio_end=chunk.end,
                chunk_reason=chunk.reason,
            )
            for r in self.buffer.skip(chunk.seq):
                await self.server.send_line(r.seq, r.text, r.final)
            return
        self.stats["accepted"] += 1

        log.info(
            "EN  [%d] (%s, %d words, conf %.3f, en %.3f) %r",
            chunk.seq, chunk.reason, chunk.word_count,
            decision.confidence, decision.english, normalized,
        )

        self._translating = True
        await self.publish_status()
        t0 = self._phrase_end_wall(chunk)
        loop = asyncio.get_running_loop()
        behind = (self._display_free_at - loop.time()) > 0

        try:
            if behind:
                # The line on screen has not been up long enough to read yet.
                # Translate now anyway -- that work is exactly how the display
                # catches up -- but hold the result until the slot frees, then
                # show it whole. Streaming it would be pointless: by the time
                # it is allowed on screen it is already complete.
                final = ""
                async for text, is_final in self.translator.translate_stream(normalized):
                    if is_final:
                        final = text
                if not final:
                    await self._fail_chunk(chunk, normalized)
                    return
                ready = (time.perf_counter() - t0) * 1000.0
                self.ready_latencies.append(ready)
                timing_log.info("ready_ms=%.1f seq=%d (queued)", ready, chunk.seq)
                await self._wait_for_display_slot()
                dt = (time.perf_counter() - t0) * 1000.0
                self.first_char_latencies.append(dt)
                timing_log.info("first_char_ms=%.1f seq=%d (held)", dt, chunk.seq)
                for r in self.buffer.submit(chunk.seq, final, True):
                    await self.server.send_line(r.seq, r.text, r.final)
                await self._finish_line(chunk, normalized, final, decision, t0)
                return

            # Nothing waiting: stream it in as it generates, as usual.
            first_char_seen = False
            final = ""
            async for text, is_final in self.translator.translate_stream(normalized):
                if not text and is_final:
                    await self._fail_chunk(chunk, normalized)
                    return
                if text and not first_char_seen:
                    first_char_seen = True
                    dt = (time.perf_counter() - t0) * 1000.0
                    self.first_char_latencies.append(dt)
                    self.ready_latencies.append(dt)   # shown as soon as ready
                    timing_log.info("first_char_ms=%.1f seq=%d", dt, chunk.seq)
                for r in self.buffer.submit(chunk.seq, text, is_final):
                    await self.server.send_line(r.seq, r.text, r.final)
                if is_final:
                    final = text
            if final:
                await self._finish_line(chunk, normalized, final, decision, t0)
        finally:
            self._translating = False
            await self.publish_status()
            await self._publish_backlog()

    async def _fail_chunk(self, chunk: Chunk, normalized: str) -> None:
        self.stats["translation_errors"] += 1
        for r in self.buffer.skip(chunk.seq):
            await self.server.send_line(r.seq, r.text, r.final)
        log.warning(
            "empty translation for chunk %d (%r) -- is the model still loaded "
            "in LM Studio?", chunk.seq, normalized
        )
        await self._publish_english(note="translation returned nothing")

    async def _finish_line(self, chunk: Chunk, normalized: str, text: str,
                           decision, t0: float) -> None:
        """Record a displayed line and reserve its reading time."""
        total = (time.perf_counter() - t0) * 1000.0
        self.total_latencies.append(total)
        self.stats["translated"] += 1
        timing_log.info("total_ms=%.1f seq=%d", total, chunk.seq)
        dwell = self._dwell_ms(text)
        log.info("PT  [%d] (%.0fms, hold %.1fs, backlog %d) %r",
                 chunk.seq, total, dwell / 1000.0, self.backlog, text)
        self._hold_display(text)
        self.transcript.write(
            seq=chunk.seq, english=normalized, raw_english=chunk.text,
            portuguese=text, accepted=True,
            confidence=decision.confidence,
            english_score=decision.english, latency_ms=total,
            audio_start=chunk.start, audio_end=chunk.end,
            chunk_reason=chunk.reason,
        )
