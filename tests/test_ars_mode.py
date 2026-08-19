"""ARS training mode (config.ars.toml) and the config layering behind it.

Two things must hold: the ARS vocabulary must actually fix the mishearings, and
loading it must not change anything about the normal mode.
"""
import pytest

from livetranslate.config import Config
from livetranslate.normalizer import Normalizer
from livetranslate.prompt import SYSTEM_PROMPT, build_system_prompt


@pytest.fixture(scope="module")
def base_cfg():
    return Config.load("config.toml")


@pytest.fixture(scope="module")
def ars_cfg():
    return Config.load("config.ars.toml")


@pytest.fixture(scope="module")
def ars(ars_cfg):
    return Normalizer(ars_cfg)


@pytest.fixture(scope="module")
def base(base_cfg):
    return Normalizer(base_cfg)


# ---------------- layering ----------------

@pytest.mark.parametrize("key", [
    "gate.min_confidence", "gate.min_english_score", "chunker.silence_ms",
    "chunker.max_words", "asr.model", "lmstudio.model", "server.port",
    "asr.stability_lag_ms", "transcript.enabled",
])
def test_ars_inherits_every_shared_setting(base_cfg, ars_cfg, key):
    """Tuning config.toml must tune both modes, never leave ARS behind."""
    assert ars_cfg.get(key) == base_cfg.get(key)


def test_ars_keeps_the_base_vocabulary(ars_cfg, base_cfg):
    """Merging is per key: adding acronyms must not drop the originals."""
    for section in ("normalizer.acronyms", "normalizer.compounds"):
        for key, value in base_cfg.section(section).items():
            assert ars_cfg.section(section)[key] == value


def test_ars_adds_to_the_base_vocabulary(ars_cfg, base_cfg):
    for section in ("normalizer.acronyms", "normalizer.compounds"):
        assert len(ars_cfg.section(section)) > len(base_cfg.section(section))


def test_extends_pointing_at_a_missing_file_is_reported(tmp_path):
    bad = tmp_path / "broken.toml"
    bad.write_text('extends = "nope.toml"\n')
    with pytest.raises(FileNotFoundError) as exc:
        Config.load(bad)
    assert "nope.toml" in str(exc.value)


# ---------------- the vocabulary itself ----------------

@pytest.mark.parametrize("heard,expected", [
    ("the hair craft is on the ramp", "the aircraft is on the ramp"),   # the reported case
    ("check the air craft", "check the aircraft"),
    ("the emu is reporting a fault", "the IMU is reporting a fault"),
    ("the jimbal is not stabilizing", "the gimbal is not stabilizing"),
    ("the jumble mount is loose", "the gimbal mount is loose"),
    ("shot over systems built it", "SHOTOVER Systems built it"),
    ("adam is the flight computer", "ATOM is the flight computer"),
    ("the atom talks to the gimbal", "the ATOM talks to the gimbal"),
    ("open pilot display", "open PilotDisplay"),
    ("send it through earth scape", "send it through Earthscape"),
    ("the m two beats the fleer", "the M2 beats the FLIR"),
    ("the a r s software", "the ARS software"),
    ("turn on the a r overlay", "turn on the AR overlay"),
    ("the f l i r is theirs", "the FLIR is theirs"),
])
def test_ars_vocabulary_is_corrected(ars, heard, expected):
    assert ars.normalize(heard) == expected


def test_longer_acronym_wins_over_shorter(ars):
    """"a r s" must not be consumed as "a r" + a stray s."""
    assert ars.normalize("the a r s shows a r data") == "the ARS shows AR data"


def test_keyboard_shortcut_letters_are_preserved(ars):
    """The speaker dictates shortcuts as single letters; they must survive."""
    assert ars.normalize("press i then press v") == "press i then press v"
    assert ars.normalize("hit e to export") == "hit e to export"


def test_corrections_keep_surrounding_punctuation(ars):
    assert ars.normalize("check the emu, then the jimbal.") == \
        "check the IMU, then the gimbal."


# ---------------- the normal mode is untouched ----------------

@pytest.mark.parametrize("text", [
    "the hair craft is on the ramp",
    "the emu is reporting a fault",
    "adam is the flight computer",
    "the jumble mount is loose",
])
def test_normal_mode_does_not_apply_ars_vocabulary(base, text):
    """These rewrites would be wrong outside an ARS session."""
    assert base.normalize(text) == text


def test_normal_mode_prompt_is_unchanged(base_cfg):
    assert build_system_prompt(base_cfg) == SYSTEM_PROMPT


def test_ars_prompt_extends_without_losing_the_examples(ars_cfg):
    prompt = build_system_prompt(ars_cfg)
    assert prompt != SYSTEM_PROMPT
    for term in ("SHOTOVER", "ATOM", "PilotDisplay", "Earthscape", "IMU", "M2"):
        assert term in prompt
    # The worked examples carry masculine agreement and must stay last.
    assert prompt.rstrip().endswith("Não toque nesse conector, ele ainda está energizado.")
    assert "Examples:" in prompt
    assert prompt.index("SHOTOVER") < prompt.index("Examples:")


def test_base_normalizer_has_no_corrections(base):
    """Whole-word rewriting is opt-in; the base config must not do it."""
    assert base.corrections == {}
