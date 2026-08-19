"""Deterministic pre-normalizer (spec section 7).

Runs on raw ASR text before translation. These are string problems: solving
them here is faster and far more reliable than asking the translation model to
cope. Every rule is driven from config.toml and covered by a unit test.

Pipeline order matters:
  disfluencies -> repeats -> acronyms -> compounds -> truncations -> clock times

Compounds run before truncations because "ver" repair keys off the *joined*
word ("firmware"), which only exists after "firm ware" has been rejoined.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# Hours may arrive as words or as digits ("twenty past 4").
_HOUR_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
# Minute quantities that appear in spoken clock phrasing.
_MINUTE_WORDS = {
    "five": 5, "ten": 10, "quarter": 15, "twenty": 20, "half": 30,
    "twentyfive": 25, "twenty-five": 25,
}
_OCLOCK = {"o'clock", "oclock", "o’clock"}

# Adjacent duplicates that are legitimate English rather than a stutter.
_LEGITIMATE_DOUBLES = {"had", "that", "very", "no", "yes"}

_TOKEN_RE = re.compile(r"[^\s]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _strip_edge_punct(tok: str) -> str:
    return tok.strip(".,!?;:").lower()


class Normalizer:
    """Applies the section 7 rules. Construct once, call normalize() per chunk."""

    def __init__(self, cfg: Any):
        n = cfg.section("normalizer")
        self.enable_disfluencies = n.get("strip_disfluencies", True)
        self.enable_repeats = n.get("collapse_repeats", True)
        self.enable_clock = n.get("convert_clock_times", True)

        self.disfluencies = {
            w.lower() for w in cfg.get("normalizer.disfluencies.words", []) or []
        }
        # Longest patterns first so "twenty five" style multi-token rules win
        # over any shorter prefix that also matches.
        self.acronyms = self._compile_seq(cfg.section("normalizer.acronyms"))
        self.compounds = self._compile_seq(cfg.section("normalizer.compounds"))

        self.truncations: dict[str, tuple[str, set[str]]] = {}
        for short, spec in (cfg.section("normalizer.truncations") or {}).items():
            if isinstance(spec, dict):
                self.truncations[short.lower()] = (
                    spec.get("full", short),
                    {n.lower() for n in spec.get("neighbours", [])},
                )

    @staticmethod
    def _compile_seq(mapping: dict[str, str]) -> list[tuple[tuple[str, ...], str]]:
        out = [
            (tuple(k.lower().split()), v) for k, v in (mapping or {}).items()
        ]
        out.sort(key=lambda kv: len(kv[0]), reverse=True)
        return out

    # ---------------- individual rules ----------------

    def strip_disfluencies(self, tokens: list[str]) -> list[str]:
        return [t for t in tokens if _strip_edge_punct(t) not in self.disfluencies]

    def collapse_repeats(self, tokens: list[str]) -> list[str]:
        out: list[str] = []
        for tok in tokens:
            key = _strip_edge_punct(tok)
            if (
                out
                and key
                and key == _strip_edge_punct(out[-1])
                and key not in _LEGITIMATE_DOUBLES
            ):
                continue  # immediate stutter: "the the power" -> "the power"
            out.append(tok)
        return out

    def _apply_sequences(
        self, tokens: list[str], patterns: Iterable[tuple[tuple[str, ...], str]]
    ) -> list[str]:
        patterns = list(patterns)
        out: list[str] = []
        i = 0
        while i < len(tokens):
            matched = False
            for seq, replacement in patterns:
                n = len(seq)
                if i + n <= len(tokens):
                    window = tuple(_strip_edge_punct(t) for t in tokens[i : i + n])
                    if window == seq:
                        out.append(replacement)
                        i += n
                        matched = True
                        break
            if not matched:
                out.append(tokens[i])
                i += 1
        return out

    def join_acronyms(self, tokens: list[str]) -> list[str]:
        return self._apply_sequences(tokens, self.acronyms)

    def join_compounds(self, tokens: list[str]) -> list[str]:
        return self._apply_sequences(tokens, self.compounds)

    def repair_truncations(self, tokens: list[str]) -> list[str]:
        out = list(tokens)
        for i, tok in enumerate(out):
            key = _strip_edge_punct(tok)
            if key not in self.truncations:
                continue
            full, neighbours = self.truncations[key]
            prev_t = _strip_edge_punct(out[i - 1]) if i > 0 else ""
            next_t = _strip_edge_punct(out[i + 1]) if i + 1 < len(out) else ""
            if prev_t in neighbours or next_t in neighbours:
                out[i] = full
        return out

    # ---------------- clock times ----------------

    def _parse_minutes(self, tokens: list[str], i: int) -> tuple[int, int] | None:
        """Return (minutes, tokens_consumed) starting at i, or None."""
        if i + 1 < len(tokens):
            two = f"{_strip_edge_punct(tokens[i])}{_strip_edge_punct(tokens[i+1])}"
            if two in _MINUTE_WORDS:  # "twenty five" -> twentyfive
                return _MINUTE_WORDS[two], 2
        one = _strip_edge_punct(tokens[i])
        if one in _MINUTE_WORDS:
            return _MINUTE_WORDS[one], 1
        if one.isdigit() and 1 <= int(one) <= 59:
            return int(one), 1
        return None

    @staticmethod
    def _parse_hour(tokens: list[str], i: int) -> int | None:
        if i >= len(tokens):
            return None
        tok = _strip_edge_punct(tokens[i])
        if tok in _HOUR_WORDS:
            return _HOUR_WORDS[tok]
        if tok.isdigit() and 1 <= int(tok) <= 12:
            return int(tok)
        return None

    def convert_clock_times(self, tokens: list[str]) -> list[str]:
        out: list[str] = []
        i = 0
        while i < len(tokens):
            # "<minutes> past|to <hour>"
            parsed = self._parse_minutes(tokens, i)
            if parsed:
                minutes, consumed = parsed
                j = i + consumed
                if j < len(tokens):
                    rel = _strip_edge_punct(tokens[j])
                    if rel in ("past", "to"):
                        hour = self._parse_hour(tokens, j + 1)
                        if hour is not None:
                            if rel == "past":
                                out.append(f"{hour}:{minutes:02d}")
                            else:
                                h = hour - 1 or 12          # "quarter to one" -> 12:45
                                out.append(f"{h}:{60 - minutes:02d}")
                            i = j + 2
                            continue
            # "<hour> o'clock"
            hour = self._parse_hour(tokens, i)
            if hour is not None and i + 1 < len(tokens):
                if _strip_edge_punct(tokens[i + 1]) in _OCLOCK:
                    out.append(f"{hour}:00")
                    i += 2
                    continue
                # ASR sometimes splits it: "four o clock"
                if (
                    _strip_edge_punct(tokens[i + 1]) == "o"
                    and i + 2 < len(tokens)
                    and _strip_edge_punct(tokens[i + 2]) == "clock"
                ):
                    out.append(f"{hour}:00")
                    i += 3
                    continue
            out.append(tokens[i])
            i += 1
        return out

    # ---------------- entry point ----------------

    def normalize(self, text: str) -> str:
        if not text or not text.strip():
            return ""
        tokens = _tokenize(text)
        if self.enable_disfluencies:
            tokens = self.strip_disfluencies(tokens)
        if self.enable_repeats:
            tokens = self.collapse_repeats(tokens)
        tokens = self.join_acronyms(tokens)
        tokens = self.join_compounds(tokens)
        tokens = self.repair_truncations(tokens)
        if self.enable_clock:
            tokens = self.convert_clock_times(tokens)
        return " ".join(tokens).strip()
