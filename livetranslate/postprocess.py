"""Post-processing of raw model output (spec section 8, "known defects").

The model mirrors the input's capitalization and punctuation. ASR output is
lowercase and unpunctuated, so raw translations arrive lowercase and often with
no terminal period. That is fixed here, in code, not by asking the prompt again.

Also detects two prompt-rule violations and logs them so their real-world rate
can be measured:
  * feminine first-person agreement (the spec calls this out explicitly)
  * enclitic pronouns ("Encontro-me"), which the prompt forbids but which this
    model still emits; observed during benchmarking on this hardware.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("livetranslate.postprocess")

_TERMINAL = ".!?…\"')]"

# Leaked scaffolding the model occasionally prepends.
_PREFIX_RE = re.compile(
    r"^\s*(?:portuguese|português|portugues|english|inglês|ingles|"
    r"translation|tradução)\s*[:\-]\s*",
    re.IGNORECASE,
)

# First-person markers followed (within a couple of tokens) by a feminine
# adjective/participle. Deliberately a fixed list: a generic "-ada" rule fires
# on plenty of correct third-person feminine agreement ("a porta está fechada").
_FEM_WORDS = (
    "cansada|exausta|surpresa|pronta|frustrada|preocupada|animada|confusa|"
    "satisfeita|feita|obrigada|segura|certa|acostumada|chateada|irritada|"
    "perdida|sentada|parada|nervosa|ocupada|autorizada|responsável"
)
_FIRST_PERSON = (
    r"(?:estou|fiquei|sou|estava|fui|tenho|tinha|acabei|serei|estarei|"
    r"me sinto|me senti|já fiquei|ainda estou)"
)
_FEMININE_RE = re.compile(
    rf"\b{_FIRST_PERSON}\b(?:\s+\w+){{0,2}}\s+\b(?:{_FEM_WORDS})\b", re.IGNORECASE
)

# Enclisis: clitic hyphenated onto a finite verb. Restricted to the unambiguous
# clitic set. Verbs ending in a circumflex vowel are infinitive contractions
# ("fazê-lo") and are left alone -- moving those produces broken Portuguese.
_ENCLISIS_RE = re.compile(
    r"\b([A-Za-zÀ-ÿ]*[^âêîôûÂÊÎÔÛ])-(me|te|se|nos|lhe|lhes)\b", re.IGNORECASE
)


# Accusative enclisis ("deixei-a"). Detected and logged but deliberately NOT
# rewritten: moving o/a/os/as safely needs to know the preceding token is a
# verb, and guessing wrong mangles hyphenated nouns ("guarda-roupa"). Limited
# to unambiguous past-tense verb endings to keep the log signal clean.
_ENCLISIS_ACC_RE = re.compile(
    r"\b[A-Za-zÀ-ÿ]*(?:ei|ou|amos|aram|iu)-(?:o|a|os|as|lo|la|los|las)\b",
    re.IGNORECASE,
)


def strip_scaffolding(text: str) -> str:
    out = (text or "").strip()
    out = _PREFIX_RE.sub("", out)
    # Unwrap a fully-quoted line, but never strip a legitimate inner quote.
    if len(out) >= 2 and out[0] in "\"'“”" and out[-1] in "\"'“”":
        out = out[1:-1].strip()
    return out


def fix_enclisis(text: str) -> tuple[str, bool]:
    """Move enclitic pronouns before the verb, as spoken in Brazil."""
    found = False

    def _swap(m: re.Match) -> str:
        nonlocal found
        found = True
        verb, clitic = m.group(1), m.group(2)
        # Preserve sentence-initial capitalization on the clitic instead.
        if verb[:1].isupper():
            return f"{clitic.capitalize()} {verb.lower()}"
        return f"{clitic.lower()} {verb}"

    return _ENCLISIS_RE.sub(_swap, text), found


def capitalize_and_punctuate(text: str) -> str:
    out = text.strip()
    if not out:
        return out
    if not out[0].isupper():
        out = out[0].upper() + out[1:]
    if out[-1] not in _TERMINAL:
        out += "."
    return out


def postprocess(text: str, source_text: str = "", fix_enclitics: bool = True,
                target: str = "pt") -> str:
    """Full cleanup of one translated line. Safe on empty input.

    target="en" skips the Portuguese-specific repairs (enclisis, feminine
    agreement); capitalization and terminal punctuation still apply, because
    the model mirrors the unpunctuated recogniser input in either direction.
    """
    out = strip_scaffolding(text)
    if not out:
        return ""
    if target != "pt":
        return capitalize_and_punctuate(out)

    if _FEMININE_RE.search(out):
        log.warning(
            "FEMININE_AGREEMENT_LEAK translation=%r source=%r", out, source_text
        )

    if _ENCLISIS_ACC_RE.search(out):
        log.warning(
            "ENCLISIS_ACCUSATIVE_UNFIXED translation=%r source=%r", out, source_text
        )

    if fix_enclitics:
        out, had = fix_enclisis(out)
        if had:
            log.warning("ENCLISIS_FIXED translation=%r source=%r", out, source_text)

    return capitalize_and_punctuate(out)
