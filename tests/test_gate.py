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


def _cfg_with(cfg, **overrides):
    """A shallow copy of the real config with a few keys overridden."""
    import copy
    from livetranslate.config import Config
    data = copy.deepcopy(cfg._data)
    for dotted, value in overrides.items():
        node = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return Config(data, cfg.path)


@pytest.fixture(scope="module")
def gate(cfg):
    """English-only routing: Portuguese is rejected outright.

    This is what runs when [dual] is disabled, and it is still the behaviour
    the confidence and coherence thresholds are tuned against.
    """
    return Gate(_cfg_with(cfg, **{"dual.enabled": False}), normalizer=Normalizer(cfg))


@pytest.fixture(scope="module")
def dual_gate(cfg):
    """Two-way routing: Portuguese is recognised and sent back as English."""
    return Gate(_cfg_with(cfg, **{"dual.enabled": True}), normalizer=Normalizer(cfg))


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
    """Single-language mode only. There, Portuguese that decodes into plausible
    English is stopped by confidence and nothing else, so the floor must sit
    above it. (Two-way mode routes such lines by language instead, and uses the
    looser dual.min_confidence.)"""
    assert cfg.get("gate.min_confidence") > HIGHEST_PORTUGUESE_CONFIDENCE, (
        f"gate.min_confidence {cfg.get('gate.min_confidence')} would let "
        f"English-looking Portuguese through when [dual] is disabled"
    )


def test_dual_mode_uses_the_looser_floor(cfg):
    dual = Gate(_cfg_with(cfg, **{"dual.enabled": True}), normalizer=Normalizer(cfg))
    single = Gate(_cfg_with(cfg, **{"dual.enabled": False}), normalizer=Normalizer(cfg))
    assert dual.min_confidence == cfg.get("dual.min_confidence")
    assert single.min_confidence == cfg.get("gate.min_confidence")
    assert dual.min_confidence < single.min_confidence


def test_dual_mode_keeps_quieter_real_speech(cfg):
    """The 447 dropped lines: ordinary English at 0.75-0.85 must now survive."""
    dual = Gate(_cfg_with(cfg, **{"dual.enabled": True}), normalizer=Normalizer(cfg))
    for text, conf in [("different things, you can use the same thing.", 0.766),
                       ("that information is important that they", 0.848),
                       ("it is not a good idea you can not do that", 0.782)]:
        d = dual.evaluate(text, confidence=conf)
        assert d.accepted, f"still dropped at {conf}: {d.reason}"


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



# ---------------- dual-language routing ----------------

# Real multilingual-recogniser output: Portuguese comes back AS Portuguese.
ROOM_PORTUGUESE = [
    "Não o toque nesse conector, ele ainda está energizado.",
    "Eu vou verificar a versão do firmware agora mesmo.",
    "O problema está no lado esquerdo da fuselagem, perto do suporte.",
    "Professor, eu tenho uma pergunta sobre o sistema de navegação.",
    "Quando você vai mostrar a calibração do gimbal?",
    "A gente pode fazer o teste depois do intervalo, tudo bem?",
]


@pytest.mark.parametrize("text", ROOM_PORTUGUESE)
def test_dual_mode_routes_portuguese_back_to_english(dual_gate, text):
    d = dual_gate.evaluate(text, confidence=0.95)
    assert d.accepted, f"room Portuguese was dropped: {d.reason}"
    assert d.language == "pt", f"routed as {d.language}, scores en={d.english:.2f} pt={d.portuguese:.2f}"


@pytest.mark.parametrize("text", ENGLISH)
def test_dual_mode_still_routes_the_speaker_as_english(dual_gate, text):
    d = dual_gate.evaluate(text, confidence=SPEAKER_CONF)
    assert d.accepted and d.language == "en", (
        f"the speaker's own English was routed as {d.language}"
    )


@pytest.mark.parametrize("text", REAL_SESSION_SPEECH)
def test_dual_mode_does_not_misroute_real_session_speech(dual_gate, text):
    """Every line here is Max talking; none may turn up in blue."""
    body, conf = text
    d = dual_gate.evaluate(body, confidence=conf)
    assert d.accepted and d.language == "en", f"misrouted as {d.language}: {body!r}"


def test_dual_mode_drops_what_is_neither_language(dual_gate):
    """Noise must not be forced into one language or the other."""
    d = dual_gate.evaluate("kwan doo voh say kee zher", confidence=0.95)
    assert not d.accepted and d.reason == "no_language"


def test_english_only_mode_still_rejects_portuguese(gate):
    """With [dual] off, the original contract holds."""
    for text in ROOM_PORTUGUESE:
        d = gate.evaluate(text, confidence=0.95)
        assert not d.accepted, f"Portuguese reached the display: {text!r}"
