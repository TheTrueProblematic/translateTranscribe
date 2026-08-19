"""Gate and language-ID coverage (spec section 5), independent of the ASR.

test_e2e.py::test_portuguese_audio_never_reaches_the_display is the audio-level
version of test 5. This file covers the decision logic directly so the gate
stays honest even when the ASR stack is unavailable.
"""
import pytest

from livetranslate.gate import Gate
from livetranslate.langid import english_score
from livetranslate.normalizer import Normalizer

# Confidence the speaker's own English measures at through the built-in mic:
# 0.921-0.988 in a real session. Portuguese measures 0.765-0.927, so the two
# overlap and gate.min_confidence sits at 0.80 -- below anything the speaker
# actually said, because a blank screen is the worse failure.
SPEAKER_CONF = 0.99
BELOW_GATE_CONF = 0.50

# Real English the speaker will actually say, including stopword-free technical
# phrases -- the case that naive language ID gets wrong.
ENGLISH = [
    "the imu is reporting a fault on the left side",
    "i need you to check the firmware version",
    "don't touch that connector it's still live",
    "gimbal calibration failed",
    "imu fault detected",
    "check battery voltage",
    "so we're going to look at the fuselage now",
    "the propeller is loose",
    "we should replace the landing gear bolts",
    "i'm ready to start the calibration",
]

# Portuguese, and Portuguese as an English-only ASR actually mangles it:
# phonetic fragments rather than real Portuguese words.
NOT_ENGLISH = [
    "não toque nesse conector ele ainda está energizado",
    "eu vou fazer o setup do drone agora",
    "professor eu tenho uma pergunta sobre o sistema",
    "o bree gah doo pro fess or",
    "vo say pod ay me ah zhoo dar",
    "kwan doo voh say kee zher",
    "eh oo kay roo sah ber",
    "noon kah vee oo ee soo",
    "a zhen chi pod ay fah zer oo tes chi",
]


@pytest.fixture(scope="module")
def gate(cfg):
    return Gate(cfg, normalizer=Normalizer(cfg))


@pytest.mark.parametrize("text", ENGLISH)
def test_english_scores_above_threshold(cfg, text):
    assert english_score(text) >= cfg.get("gate.min_english_score")


@pytest.mark.parametrize("text", NOT_ENGLISH)
def test_non_english_scores_below_threshold(cfg, text):
    assert english_score(text) < cfg.get("gate.min_english_score")


def test_separation_margin_is_wide(cfg):
    """The two populations must not merely straddle the threshold."""
    worst_en = min(english_score(t) for t in ENGLISH)
    best_other = max(english_score(t) for t in NOT_ENGLISH)
    assert worst_en > best_other, (
        f"populations overlap: worst English {worst_en:.3f} "
        f"<= best non-English {best_other:.3f}"
    )


@pytest.mark.parametrize("text", ENGLISH)
def test_gate_accepts_confident_english(gate, text):
    assert gate.evaluate(text, confidence=SPEAKER_CONF).accepted


@pytest.mark.parametrize("text", NOT_ENGLISH)
def test_gate_rejects_non_english(gate, text):
    # High confidence on purpose: this asserts the *language* check is what
    # rejects it, not merely that it scored low on confidence.
    d = gate.evaluate(text, confidence=SPEAKER_CONF)
    assert not d.accepted and d.reason == "not_english"


def test_gate_rejects_low_confidence(gate, cfg):
    """Confidence is the filter that actually catches Portuguese in the room."""
    d = gate.evaluate("the imu is reporting a fault", confidence=BELOW_GATE_CONF)
    assert not d.accepted and d.reason == "low_confidence"


def test_gate_rejects_single_word_fragments(gate):
    assert not gate.evaluate("ok", confidence=SPEAKER_CONF).accepted


def test_acronyms_survive_the_gate(gate):
    """Spelled-out acronyms must not be mistaken for phonetic fragments."""
    assert gate.evaluate("the i m u and the l e d are on", confidence=SPEAKER_CONF).accepted


# ---------------- tier 4: the mandatory manual hold ----------------

def test_manual_hold_blocks_everything(gate):
    gate.resume()
    assert gate.evaluate("the imu is reporting a fault", confidence=SPEAKER_CONF).accepted
    gate.pause()
    d = gate.evaluate("the imu is reporting a fault", confidence=SPEAKER_CONF)
    assert not d.accepted and d.reason == "paused"
    gate.resume()
    assert gate.evaluate("the imu is reporting a fault", confidence=SPEAKER_CONF).accepted


def test_toggle_reports_state(gate):
    gate.resume()
    assert gate.toggle() is True
    assert gate.toggle() is False


def test_hold_overrides_every_other_tier(gate):
    """Tier 4 must win even over input that would otherwise pass cleanly."""
    gate.pause()
    try:
        for text in ENGLISH:
            assert not gate.evaluate(text, confidence=1.0).accepted
    finally:
        gate.resume()


# ---------------- regression: the session that displayed nothing ----------------

# Verbatim from logs/livetranslate.log of a real session. Every one of these was
# recognised correctly and then thrown away, because the gate had been paused by
# a stray SPACE keypress on the display window. They must all pass now.
REAL_SESSION_SPEECH = [
    ("Okay. Oh, now that bar's moving at least. very", 0.969),
    ("is it working though?", 0.988),
    ("guess anything about this actually working.", 0.986),
    ("I don't think", 0.983),
    ("so, considering no models", 0.980),
    ("loaded. Yeah, a car.", 0.921),
]


@pytest.mark.parametrize("text,conf", REAL_SESSION_SPEECH)
def test_real_session_speech_is_accepted(gate, text, conf):
    """These are the speaker's actual words at their actual confidences.

    Two separate faults dropped them: the gate was paused, and min_confidence
    was tuned on synthesized speech at 0.96 -- above the 0.921 and 0.945 this
    session really produced.
    """
    gate.resume()
    d = gate.evaluate(text, confidence=conf)
    assert d.accepted, f"real speech rejected as {d.reason} (en {d.english:.2f})"


HIGHEST_PORTUGUESE_CONFIDENCE = 0.823   # highest that language ID cannot catch


def test_confidence_threshold_sits_above_english_looking_portuguese(cfg):
    """The other side of the window: Portuguese that decodes into plausible
    English is only stopped by confidence."""
    assert cfg.get("gate.min_confidence") > HIGHEST_PORTUGUESE_CONFIDENCE, (
        f"min_confidence {cfg.get('gate.min_confidence')} would let "
        f"English-looking Portuguese through"
    )


def test_confidence_threshold_sits_below_real_speech(cfg):
    """Guards against re-tuning the gate above what the microphone delivers."""
    lowest_real = min(conf for _, conf in REAL_SESSION_SPEECH)
    assert cfg.get("gate.min_confidence") < lowest_real, (
        f"min_confidence {cfg.get('gate.min_confidence')} is above the quietest "
        f"real utterance ({lowest_real}); the speaker's own words would vanish"
    )


def test_mangled_portuguese_is_still_rejected(gate):
    """The other half of the trade-off: loosening confidence must not let the
    room's Portuguese through. These are real ASR outputs on pt_BR audio."""
    gate.resume()
    for text, conf in [
        ("Professor Eu Tenho Yuma Pragunta", 0.927),
        ("Sabaro Sistema de Navigaseo, Cuando Voqui Ve caladraceo do", 0.862),
        ("do jimbal, a gente pog phaser o test depois du intervalo, tudo", 0.893),
        ("I need this connector, henda esta energizado,", 0.812),
        # These two come back looking like plain English, so the language check
        # cannot catch them -- confidence is the only thing that does.
        ("in physiology, but support.", 0.814),
        ("Naughtoquines connector, henda esta energizado, and you verify the "
         "firmware. The problem is", 0.823),
    ]:
        d = gate.evaluate(text, confidence=conf)
        assert not d.accepted, f"Portuguese reached the display: {text!r}"
