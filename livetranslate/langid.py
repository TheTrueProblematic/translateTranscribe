"""Cheap English-vs-not language check for the tier-2 gate (spec section 5).

The ASR is English-only, so Portuguese speech does not come back as Portuguese
text -- it comes back as *incoherent English*. The useful question is therefore
"does this read like real English?", not "does this contain Portuguese words?".

Signals, in order of how much work they do:

  1. Token length. Phonetic garbage from mis-decoded Portuguese is dominated by
     2-3 character fragments ("vo say pod ay me ah"); real speech, especially
     the technical vocabulary this app targets, is not.
  2. Fragment density: short tokens that are not genuine common English words.
  3. English function-word density (a bonus, not a requirement -- plenty of
     legitimate technical speech is stopword-free: "gimbal calibration failed").
  4. Explicit Portuguese markers and diacritics (a penalty).

A dictionary lookup was tried and deliberately rejected: /usr/share/dict/words
is an unabridged 1934 wordlist that happily contains "bree", "kwan", "kee" and
"fess", so garbled Portuguese scored 0.5-0.75 coverage -- indistinguishable
from real English. It was pure noise.

No model, no dependency, no file I/O, ~10us per call.
"""
from __future__ import annotations

import re

_WORD_RE = re.compile(r"[a-zA-ZÀ-ſ'0-9:]+")

# Unambiguous English function words. Deliberately excludes tokens that are
# also common Portuguese words (a, as, no, me, e, o, do, se, la, um).
_EN_STOP = {
    "the", "and", "is", "are", "to", "of", "in", "it", "that", "this", "you",
    "we", "i", "have", "has", "was", "were", "for", "with", "on", "at", "be",
    "not", "but", "they", "he", "she", "does", "can", "will", "would", "there",
    "what", "when", "all", "get", "got", "going", "just", "so", "if", "from",
    "about", "out", "up", "one", "know", "like", "right", "now", "here",
    "need", "want", "make", "take", "look", "see", "think", "because", "very",
    "more", "than", "then", "them", "my", "your", "our", "how", "who", "why",
    "into", "over", "back", "down", "off", "any", "these", "those", "let",
    "put", "should", "could", "must", "may", "each", "other", "which", "while",
    "after", "before", "again", "still", "only", "also", "well", "much",
    "many", "don't", "it's", "i'm", "we're", "that's", "doesn't", "isn't",
}

# Genuine short English words, so "is the fan on" is not read as fragments.
# Includes the technical short forms this deployment actually uses.
_SHORT_OK = _EN_STOP | {
    "an", "as", "at", "by", "do", "go", "he", "if", "in", "is", "it", "me",
    "my", "no", "of", "on", "or", "so", "to", "up", "us", "we", "a", "i",
    "all", "any", "are", "bad", "big", "bit", "box", "but", "can", "car",
    "cut", "day", "did", "end", "fan", "far", "few", "fit", "fix", "fly",
    "for", "gas", "get", "got", "had", "has", "her", "him", "his", "hit",
    "hot", "how", "its", "job", "key", "kit", "led", "let", "lot", "low",
    "man", "map", "max", "may", "min", "new", "not", "now", "nut", "off",
    "oil", "old", "one", "our", "out", "own", "pin", "put", "ram", "red",
    "rig", "run", "saw", "say", "sea", "see", "set", "she", "sit", "six",
    "sky", "tab", "tap", "ten", "tie", "tip", "ton", "too", "top", "try",
    "two", "use", "van", "war", "was", "way", "wet", "who", "why", "win",
    "yes", "yet", "you", "amp", "bat", "cam", "cpu", "gpu", "ssd", "psi",
    "rpm", "imu", "led", "usb", "gps", "esc", "pid", "fpv", "aoa", "rf",
    "ip", "ac", "dc", "cg", "hz", "kg", "cm", "mm", "add", "air", "arm",
    "bar", "bus", "cap", "dry", "due", "fed", "fit", "gap", "hip", "ice",
    "ink", "jet", "lab", "lay", "leg", "lid", "log", "mid", "net", "odd",
    "pad", "pay", "per", "pot", "raw", "ray", "rod", "row", "sun", "tag",
    "tie", "tin", "tow", "wax", "web", "yaw", "arc", "bolt",
}

# Portuguese morphology that survives an English-only ASR's mangling. The
# decoder spells Portuguese phonetically, so exact words often do not appear --
# but the shapes do: "-ção" comes back as "-ceo"/"-seo"/"-sao", and the "nh"
# and "lh" digraphs are vanishingly rare in English.
_PT_SUFFIXES = (
    "ção", "ções", "cao", "coes", "ceo", "seo", "sao", "çao",
    "mente", "inho", "inha", "agem", "eiro", "eira", "ando", "endo", "indo",
    "aria", "acao", "idade", "amos", "aram", "ado", "ada", "ados", "adas",
)
_PT_DIGRAPHS = ("nh", "lh", "ç")
# English words that would otherwise trip the suffix test.
_SUFFIX_EXEMPT = {
    "commando", "command", "tornado", "avocado", "desperado", "bravado",
    "aficionado", "armada", "granada", "canada", "nevada", "salada", "salad",
    "government", "moment", "comment", "cargo", "banjo", "loaded", "unloaded",
    "threaded", "graded", "faded", "traded", "landed", "handed", "banded",
    "sanded", "branded", "grounded", "founded", "rounded", "sounded", "guided",
}


# Unambiguous Brazilian Portuguese markers.
_PT_MARK = {
    "não", "nao", "está", "esta", "estão", "sim", "você", "voce", "com",
    "para", "por", "que", "como", "quando", "onde", "isso", "isto", "esse",
    "essa", "aqui", "ali", "ele", "ela", "eles", "elas", "nós", "também",
    "tambem", "já", "ainda", "então", "entao", "porque", "vai", "vou",
    "fazer", "muito", "mais", "mas", "tudo", "todo", "pode", "tem",
    "obrigado", "obrigada", "dia", "eu", "meu", "minha", "seu", "sua",
    "das", "dos", "uma", "pra", "aos", "ser", "foi", "são", "sao", "coisa",
    "nesse", "toque", "conector", "energizado", "agora", "bem",
    # Function words and stems that are unambiguous against English.
    "de", "da", "na", "nas", "em", "por", "sem", "sobre", "gente", "depois",
    "intervalo", "sistema", "cuando", "quanto", "quem", "seu", "sua", "pelo",
    "pela", "nosso", "nossa", "todos", "todas", "esta", "estou", "tenho",
    "pergunta", "pregunta", "professor", "calibracao", "navegacao",
}

_PT_CHARS = set("ãõçáéíóúâêôàÃÕÇÁÉÍÓÚÂÊÔÀ")

# Weights. Tuned against real Parakeet output on synthesized pt_BR speech.
_W_LENGTH, _W_FRAGMENT, _W_STOPWORD, _W_PT_PENALTY = 0.40, 0.25, 0.35, 1.6


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _WORD_RE.findall(text or "")]


def english_score(text: str) -> float:
    """0.0 = certainly not English, 1.0 = confidently English."""
    tokens = [t for t in tokenize(text) if t]
    n = len(tokens)
    if n == 0:
        return 0.0

    avg_len = sum(len(t) for t in tokens) / n
    # Real speech averages >5 chars/token; phonetic garbage sits near 3.
    len_signal = max(0.0, min(1.0, (avg_len - 3.0) / 2.5))

    fragments = sum(1 for t in tokens if len(t) <= 3 and t not in _SHORT_OK)
    frag_signal = 1.0 - (fragments / n)

    stop_hits = sum(1 for t in tokens if t in _EN_STOP)
    stop_signal = min(1.0, (stop_hits / n) / 0.30)

    pt_hits = sum(1 for t in tokens if t in _PT_MARK)
    pt_hits += sum(1 for t in tokens if any(ch in _PT_CHARS for ch in t))
    # Morphological evidence, for phonetically-mangled Portuguese where the
    # exact words never appear but their shapes do.
    pt_hits += sum(
        1 for t in tokens
        if len(t) >= 5 and t not in _SUFFIX_EXEMPT and t.endswith(_PT_SUFFIXES)
    )
    pt_hits += sum(
        1 for t in tokens
        if len(t) >= 4 and any(d in t for d in _PT_DIGRAPHS)
    )
    pt_signal = pt_hits / n

    score = (
        _W_LENGTH * len_signal
        + _W_FRAGMENT * frag_signal
        + _W_STOPWORD * stop_signal
        - _W_PT_PENALTY * pt_signal
    )

    # Short fragments carry little evidence either way, and the length signal
    # punishes them unfairly -- "loaded. Yeah, a car." is real speech but scores
    # like noise. When a short fragment shows NO Portuguese evidence at all,
    # lean toward accepting it and let the confidence gate be the filter.
    # Dropping the speaker's own words is the worse failure.
    if n <= 5 and pt_signal == 0.0:
        lenient = 0.30 + 0.275 * frag_signal + 0.175 * stop_signal
        score = max(score, lenient)

    return max(0.0, min(1.0, score))


def portuguese_score(text: str) -> float:
    """0.0 = certainly not Portuguese, 1.0 = confidently Portuguese.

    The mirror of english_score, used to ROUTE rather than to reject. With the
    multilingual recogniser, Portuguese comes back as real Portuguese -- proper
    words and diacritics -- so this is decisive rather than a guess.
    """
    tokens = [t for t in tokenize(text) if t]
    n = len(tokens)
    if n == 0:
        return 0.0

    mark_hits = sum(1 for t in tokens if t in _PT_MARK)
    diacritics = sum(1 for t in tokens if any(ch in _PT_CHARS for ch in t))
    morph = sum(
        1 for t in tokens
        if len(t) >= 5 and t not in _SUFFIX_EXEMPT and t.endswith(_PT_SUFFIXES)
    )
    digraphs = sum(
        1 for t in tokens if len(t) >= 4 and any(d in t for d in _PT_DIGRAPHS)
    )
    stop_hits = sum(1 for t in tokens if t in _EN_STOP)

    # Function words are the strongest cue; a diacritic is near-proof.
    signal = (
        min(1.0, (mark_hits / n) / 0.30) * 0.55
        + min(1.0, (diacritics / n) / 0.15) * 0.30
        + min(1.0, ((morph + digraphs) / n) / 0.20) * 0.15
    )
    # English function words argue against it.
    signal -= 1.2 * (stop_hits / n)
    return max(0.0, min(1.0, signal))


def detect_language(
    text: str,
    *,
    english_min: float = 0.45,
    portuguese_min: float = 0.30,
    margin: float = 0.10,
) -> tuple[str | None, float, float]:
    """Route a line by language: returns ("en" | "pt" | None, en_score, pt_score).

    None means neither language is clearly present -- noise, or a fragment too
    short to judge. Those are dropped rather than guessed at, because showing
    the audience a confidently mistranslated fragment is worse than showing
    nothing.
    """
    en = english_score(text)
    pt = portuguese_score(text)
    if pt >= portuguese_min and pt >= en + margin:
        return "pt", en, pt
    if en >= english_min and en >= pt:
        return "en", en, pt
    return None, en, pt


def looks_english(text: str, threshold: float) -> bool:
    return english_score(text) >= threshold
