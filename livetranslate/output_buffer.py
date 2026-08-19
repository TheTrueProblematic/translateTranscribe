"""Ordered output buffer (spec section 6).

There is only ever one in-flight translation, so out-of-order responses should
be impossible. This exists anyway, because the spec requires that an
out-of-order response can never scramble the display: sequence numbers are
assigned at chunk time and rendering is strictly ordered here.

Streaming complicates it slightly. A sequence is "open" while its partial
updates arrive and "closed" once its final update lands. Updates for a later
sequence are held until every earlier sequence has closed.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rendered:
    seq: int
    text: str
    final: bool


class OrderedOutputBuffer:
    def __init__(self, first_seq: int = 1):
        self._next = first_seq
        self._pending: dict[int, list[tuple[str, bool]]] = {}

    @property
    def expecting(self) -> int:
        return self._next

    def submit(self, seq: int, text: str, final: bool) -> list[Rendered]:
        """Feed one update. Returns the updates that may now be displayed."""
        if seq < self._next:
            return []                       # stale: its slot has already closed

        if seq > self._next:
            self._pending.setdefault(seq, []).append((text, final))
            return []

        out = [Rendered(seq, text, final)]
        if final:
            self._next += 1
            out.extend(self._drain())
        return out

    def _drain(self) -> list[Rendered]:
        out: list[Rendered] = []
        while True:
            items = self._pending.pop(self._next, None)
            if not items:
                return out
            closed = False
            for text, final in items:
                out.append(Rendered(self._next, text, final))
                if final:
                    closed = True
                    break
            if not closed:
                # Sequence is still streaming; keep the slot open for live
                # updates rather than advancing past it.
                return out
            self._next += 1

    def skip(self, seq: int) -> list[Rendered]:
        """Abandon a sequence (translation failed / produced nothing)."""
        if seq != self._next:
            self._pending.pop(seq, None)
            return []
        self._next += 1
        return self._drain()

    def reset(self, first_seq: int = 1) -> None:
        self._next = first_seq
        self._pending.clear()
