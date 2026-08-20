"""Speaker / language gate (spec section 5).

Tiered, because no single tier is reliable on its own:

  Tier 1  English-only ASR. Free, always on, lives in the ASR choice itself.
  Tier 2  Confidence + coherence. Mean ASR confidence and an English-ness
          score, both thresholds in config.toml so they can be tuned in the room.
  Tier 3  Speaker embedding similarity. Optional, off by default -- see README
          and the report for the measured reason.
  Tier 4  Manual hold. Mandatory, always available, and independent of 1-3.

Rejected text NEVER reaches the display. It goes to the debug log so the
thresholds can be tuned afterward.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .langid import detect_language, english_score

log = logging.getLogger("livetranslate.gate")


@dataclass
class GateDecision:
    accepted: bool
    reason: str = "ok"
    english: float = 0.0
    confidence: float = 0.0
    speaker: float | None = None
    language: str = "en"        # "en" = the speaker, "pt" = someone in the room
    portuguese: float = 0.0

    def __bool__(self) -> bool:
        return self.accepted


class Gate:
    """Tier 2-4 gating. Construct once; call evaluate() per chunk.

    `normalizer` is optional but strongly recommended: language scoring runs on
    the normalized text so that spelled-out acronyms ("i m u") have already
    become "IMU". Without it, every acronym looks like three one-letter
    fragments and legitimate technical English gets rejected.
    """

    def __init__(self, cfg: Any, normalizer=None, speaker_verifier=None):
        # Two-way mode can afford a looser confidence floor: the language
        # router decides what is Portuguese, so confidence only has to catch
        # noise. With [dual] off, confidence is the ONLY thing standing between
        # the room and the screen, so it stays strict.
        self.dual_enabled = bool(cfg.get("dual.enabled", False))
        self.min_confidence = float(
            cfg.get("dual.min_confidence", 0.70) if self.dual_enabled
            else cfg.get("gate.min_confidence", 0.85)
        )
        self.min_english = float(cfg.get("gate.min_english_score", 0.45))
        self.min_words = int(cfg.get("gate.min_words", 2))
        self.log_rejections = bool(cfg.get("gate.log_rejections", True))

        # Dual-language routing. Off: only English passes, everything else is
        # dropped (the original behaviour). On: Portuguese is recognised as
        # Portuguese and routed back into English instead of being thrown away.
        self.dual = self.dual_enabled
        self.pt_min = float(cfg.get("dual.portuguese_min", 0.30))
        self.lang_margin = float(cfg.get("dual.margin", 0.10))
        # How sure the text has to be before it overrules the recogniser's own
        # language label. Both conditions must hold.
        self.override_min = float(cfg.get("dual.text_override_min", 0.55))
        self.override_max = float(cfg.get("dual.text_override_max", 0.20))

        self.speaker_enabled = bool(cfg.get("gate.speaker.enabled", False))
        self.speaker_threshold = float(cfg.get("gate.speaker.threshold", 0.72))
        self.speaker_verifier = speaker_verifier

        self.normalizer = normalizer
        self.paused = bool(cfg.get("hotkey.start_paused", False))

        self.stats = {"accepted": 0, "paused": 0, "short": 0, "confidence": 0,
                      "language": 0, "speaker": 0, "accepted_pt": 0}

    # ---------------- tier 4: manual hold ----------------

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def toggle(self) -> bool:
        self.paused = not self.paused
        return self.paused

    # ---------------- evaluation ----------------

    def evaluate(self, text: str, confidence: float = 1.0,
                 audio=None, word_count: int | None = None,
                 asr_language: str | None = None) -> GateDecision:
        # Tier 4 first: when held, nothing gets through, whatever the scores say.
        if self.paused:
            self.stats["paused"] += 1
            return self._reject(text, "paused", 0.0, confidence)

        stripped = (text or "").strip()
        if not stripped:
            return self._reject(text, "empty", 0.0, confidence)

        n_words = word_count if word_count is not None else len(stripped.split())
        if n_words < self.min_words:
            self.stats["short"] += 1
            return self._reject(text, "too_short", 0.0, confidence)

        if confidence < self.min_confidence:
            self.stats["confidence"] += 1
            return self._reject(text, "low_confidence", 0.0, confidence)

        scored = stripped
        if self.normalizer is not None:
            try:
                scored = self.normalizer.normalize(stripped) or stripped
            except Exception:            # never let scoring break the pipeline
                scored = stripped
        if self.dual:
            lang, eng, pt = detect_language(
                scored, english_min=self.min_english,
                portuguese_min=self.pt_min, margin=self.lang_margin,
            )
            # Some backends identify the language themselves (faster-whisper
            # does; parakeet-mlx cannot). That label comes from the audio, so
            # it is usually better evidence than scoring the spelling -- but it
            # is not infallible: on a heavily accented Portuguese clip Whisper
            # reported "en" with probability 0.95 while the text scored
            # en=0.00 pt=0.68. So the label wins by default, and only loses to
            # a decisive disagreement from the text.
            if asr_language in ("en", "pt"):
                if lang is not None and lang != asr_language:
                    winner = pt if lang == "pt" else eng
                    loser = eng if lang == "pt" else pt
                    decisive = (winner >= self.override_min
                                and loser <= self.override_max)
                    log.info(
                        "language disagreement: recogniser=%s text=%s "
                        "(en=%.2f pt=%.2f) -> using %s",
                        asr_language, lang, eng, pt,
                        lang if decisive else asr_language,
                    )
                    if not decisive:
                        lang = asr_language
                else:
                    lang = asr_language
            if lang is None:
                self.stats["language"] += 1
                d = self._reject(text, "no_language", eng, confidence)
                d.portuguese = pt
                return d
        else:
            lang, pt = "en", 0.0
            eng = english_score(scored)
            if eng < self.min_english:
                self.stats["language"] += 1
                return self._reject(text, "not_english", eng, confidence)

        sim = None
        if self.speaker_enabled and self.speaker_verifier is not None and audio is not None:
            try:
                sim = float(self.speaker_verifier.similarity(audio))
            except Exception as exc:
                log.debug("speaker verifier failed, allowing through: %s", exc)
                sim = None
            if sim is not None and sim < self.speaker_threshold:
                self.stats["speaker"] += 1
                return self._reject(text, "wrong_speaker", eng, confidence, sim)

        self.stats["accepted"] += 1
        if lang == "pt":
            self.stats["accepted_pt"] += 1
        return GateDecision(True, "ok", eng, confidence, sim,
                            language=lang, portuguese=pt)

    def _reject(self, text, reason, eng, conf, sim=None) -> GateDecision:
        if self.log_rejections:
            log.info(
                "REJECTED [%s] english=%.3f conf=%.3f speaker=%s :: %r",
                reason, eng, conf, f"{sim:.3f}" if sim is not None else "n/a", text,
            )
        return GateDecision(False, reason, eng, conf, sim)
