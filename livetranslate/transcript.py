"""Per-run transcript files, for evaluating a session afterwards.

Two files per run, written side by side:

  transcript-<timestamp>.txt    readable: English above its Portuguese
  transcript-<timestamp>.jsonl  one JSON object per line, for scoring

Both are appended and flushed line by line, so a crash or a hard quit still
leaves everything said up to that moment on disk.

Rejected chunks are recorded too (marked accepted=false with the reason and
scores). Without them a transcript would silently omit exactly the material you
need when tuning the gate.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("livetranslate.transcript")


class TranscriptWriter:
    def __init__(self, cfg):
        self.enabled = bool(cfg.get("transcript.enabled", True))
        self.include_rejected = bool(cfg.get("transcript.include_rejected", True))

        directory = Path(cfg.get("transcript.dir", "logs/transcripts"))
        if not directory.is_absolute():
            directory = Path(__file__).resolve().parent.parent / directory
        self.dir = directory

        self.started = datetime.now()
        stamp = self.started.strftime("%Y-%m-%d_%H-%M-%S")
        self.txt_path = self.dir / f"transcript-{stamp}.txt"
        self.jsonl_path = self.dir / f"transcript-{stamp}.jsonl"

        self._txt = None
        self._jsonl = None
        self.lines = 0

    def open(self) -> None:
        if not self.enabled:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._txt = open(self.txt_path, "a", encoding="utf-8")
            self._jsonl = open(self.jsonl_path, "a", encoding="utf-8")
            header = (
                f"# LiveTranslate transcript\n"
                f"# started {self.started.isoformat(timespec='seconds')}\n"
                f"# EN = recognised English, PT = displayed Portuguese\n\n"
            )
            self._txt.write(header)
            self._txt.flush()
            log.info("transcript: %s", self.txt_path)
        except OSError as exc:
            log.warning("could not open transcript files (%s); continuing", exc)
            self._txt = self._jsonl = None

    def write(
        self,
        *,
        seq: int,
        english: str,
        portuguese: str = "",
        accepted: bool = True,
        reason: str = "ok",
        confidence: float = 0.0,
        english_score: float = 0.0,
        latency_ms: float | None = None,
        audio_start: float | None = None,
        audio_end: float | None = None,
        chunk_reason: str = "",
        raw_english: str = "",
    ) -> None:
        if not self.enabled or self._txt is None:
            return
        if not accepted and not self.include_rejected:
            return

        stamp = datetime.now().strftime("%H:%M:%S")
        record: dict[str, Any] = {
            "seq": seq,
            "time": datetime.now().isoformat(timespec="milliseconds"),
            "accepted": accepted,
            "reason": reason,
            "english": english,
            "portuguese": portuguese,
            "confidence": round(confidence, 4),
            "english_score": round(english_score, 4),
            "chunk_reason": chunk_reason,
        }
        if raw_english and raw_english != english:
            record["english_raw"] = raw_english
        if latency_ms is not None:
            record["latency_ms"] = round(latency_ms, 1)
        if audio_start is not None:
            record["audio_start"] = round(audio_start, 2)
            record["audio_end"] = round(audio_end or 0.0, 2)

        try:
            if accepted:
                self._txt.write(
                    f"[{stamp}] #{seq}\n  EN  {english}\n  PT  {portuguese}\n\n"
                )
            else:
                self._txt.write(
                    f"[{stamp}] #{seq}  (dropped: {reason}, "
                    f"conf {confidence:.2f}, en {english_score:.2f})\n"
                    f"  EN  {english}\n\n"
                )
            self._txt.flush()
            self._jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._jsonl.flush()
            self.lines += 1
        except OSError as exc:
            log.warning("transcript write failed (%s); disabling", exc)
            self.close()
            self.enabled = False

    def close(self) -> None:
        for fh in (self._txt, self._jsonl):
            if fh is not None:
                try:
                    if fh is self._txt:
                        ended = datetime.now()
                        mins = (ended - self.started).total_seconds() / 60.0
                        fh.write(
                            f"# ended {ended.isoformat(timespec='seconds')} "
                            f"after {mins:.1f} min, {self.lines} entries\n"
                        )
                    fh.close()
                except OSError:
                    pass
        self._txt = self._jsonl = None
