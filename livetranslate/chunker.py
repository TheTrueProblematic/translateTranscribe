"""Chunker (spec section 6).

Segmentation must not depend on the speaker pausing. A chunk closes on
whichever fires first:
  * silence_ms of voice-activity silence
  * max_words accumulated
  * max_elapsed_ms since the last emit

Cuts always land on word boundaries because the buffer only ever holds whole
ASR words. The chunker is driven by the *audio* clock (word timestamps and
tick(now_s)) rather than wall time, which keeps it deterministic under test.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Word:
    text: str
    start: float                 # seconds on the audio timeline
    end: float
    confidence: float = 1.0
    # Language reported by the recogniser itself, when the backend can say.
    # faster-whisper detects it per decode; parakeet-mlx cannot, and leaves
    # this None so the gate falls back to scoring the text.
    language: str | None = None


@dataclass
class Chunk:
    seq: int
    text: str
    words: list[Word] = field(default_factory=list)
    start: float = 0.0
    end: float = 0.0
    reason: str = ""             # silence | max_words | max_elapsed | flush

    @property
    def mean_confidence(self) -> float:
        if not self.words:
            return 0.0
        return sum(w.confidence for w in self.words) / len(self.words)

    @property
    def word_count(self) -> int:
        return len(self.words)

    @property
    def language(self) -> str | None:
        """Language the recogniser reported, if it reports one at all.

        Majority vote across the chunk's words; ties and absent labels give
        None, which tells the gate to score the text instead.
        """
        labels = [w.language for w in self.words if w.language]
        if not labels:
            return None
        top = max(set(labels), key=labels.count)
        return top if labels.count(top) > len(labels) / 2 else None


class Chunker:
    def __init__(self, cfg: Any = None, *, silence_ms=400, max_words=14, max_elapsed_ms=3500):
        if cfg is not None:
            silence_ms = cfg.get("chunker.silence_ms", silence_ms)
            max_words = cfg.get("chunker.max_words", max_words)
            max_elapsed_ms = cfg.get("chunker.max_elapsed_ms", max_elapsed_ms)
        self.silence_ms = float(silence_ms)
        self.max_words = int(max_words)
        self.max_elapsed_ms = float(max_elapsed_ms)

        self._buffer: list[Word] = []
        self._seq = 0
        self._last_emit_s: float | None = None   # audio-clock time of last cut

    # ---------------- inputs ----------------

    def add_word(self, word: Word) -> list[Chunk]:
        """Feed one finalized ASR word. Returns chunks closed by this word."""
        if self._last_emit_s is None:
            self._last_emit_s = word.start
        self._buffer.append(word)
        if len(self._buffer) >= self.max_words:
            return [self._emit("max_words")]
        return []

    def tick(self, now_s: float, speech_active: bool = False) -> list[Chunk]:
        """Advance the audio clock. Fires the two time-based triggers."""
        if not self._buffer:
            # Keep the elapsed window anchored to *now* while idle. Without
            # this, a pause longer than max_elapsed leaves a stale anchor and
            # the first word spoken afterwards is instantly emitted as a lone
            # one-word chunk -- which the gate then drops as too short, losing
            # the speaker's first word after every pause.
            self._last_emit_s = now_s
            return []

        last_end = self._buffer[-1].end
        if not speech_active and (now_s - last_end) * 1000.0 >= self.silence_ms:
            return [self._emit("silence")]

        anchor = self._last_emit_s if self._last_emit_s is not None else self._buffer[0].start
        if (now_s - anchor) * 1000.0 >= self.max_elapsed_ms:
            return [self._emit("max_elapsed")]
        return []

    def flush(self) -> list[Chunk]:
        """Close whatever is buffered, e.g. on pause or shutdown."""
        return [self._emit("flush")] if self._buffer else []

    # ---------------- internals ----------------

    def _emit(self, reason: str) -> Chunk:
        words = self._buffer
        self._buffer = []
        self._seq += 1
        chunk = Chunk(
            seq=self._seq,
            text=" ".join(w.text for w in words),
            words=words,
            start=words[0].start,
            end=words[-1].end,
            reason=reason,
        )
        self._last_emit_s = chunk.end
        return chunk

    @property
    def pending_words(self) -> int:
        return len(self._buffer)
