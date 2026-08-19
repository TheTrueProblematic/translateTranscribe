"""Spec section 12, test 1: a unit test for every normalizer rule."""
import pytest

from livetranslate.normalizer import Normalizer


@pytest.fixture(scope="module")
def norm(cfg):
    return Normalizer(cfg)


# ---------------- spelled-out acronyms (section 7) ----------------

@pytest.mark.parametrize("spoken,expected", [
    ("i m u", "IMU"), ("l e d", "LED"), ("u s b", "USB"), ("g p s", "GPS"),
    ("r f", "RF"), ("i p", "IP"), ("a c", "AC"), ("d c", "DC"),
    ("e s c", "ESC"), ("p i d", "PID"), ("f p v", "FPV"), ("r p m", "RPM"),
])
def test_every_acronym_in_config(norm, spoken, expected):
    assert norm.normalize(spoken) == expected


def test_acronym_inside_sentence(norm):
    assert norm.normalize("the i m u is reporting a fault") == "the IMU is reporting a fault"


def test_multiple_acronyms_in_one_sentence(norm):
    assert norm.normalize("l e d on the u s b port") == "LED on the USB port"


def test_acronym_letters_not_joined_when_not_a_known_acronym(norm):
    # "x y z" is not in the config, so it must be left exactly as spoken.
    assert norm.normalize("x y z reading") == "x y z reading"


# ---------------- split compound technical words ----------------

@pytest.mark.parametrize("spoken,expected", [
    ("firm ware", "firmware"), ("fire wall", "firewall"), ("hard ware", "hardware"),
    ("soft ware", "software"), ("fuse large", "fuselage"), ("air frame", "airframe"),
    ("way point", "waypoint"), ("auto pilot", "autopilot"), ("gim bal", "gimbal"),
])
def test_every_compound_in_config(norm, spoken, expected):
    assert norm.normalize(spoken) == expected


def test_compound_inside_sentence(norm):
    assert norm.normalize("the fuse large has a crack") == "the fuselage has a crack"


# ---------------- clock times (explicitly not optional) ----------------

@pytest.mark.parametrize("spoken,expected", [
    ("twenty past four", "4:20"),      # the three cases named in the spec
    ("quarter to six", "5:45"),
    ("half past nine", "9:30"),
    ("quarter past four", "4:15"),
    ("ten past three", "3:10"),
    ("five to eight", "7:55"),
    ("twenty five past three", "3:25"),
    ("twenty five to ten", "9:35"),
    ("half past twelve", "12:30"),
    ("quarter to one", "12:45"),       # hour must wrap, not go to 0:45
    ("ten to one", "12:50"),
    ("four o'clock", "4:00"),
    ("seven oclock", "7:00"),
])
def test_clock_times(norm, spoken, expected):
    assert norm.normalize(spoken) == expected


def test_clock_time_inside_sentence(norm):
    assert norm.normalize("meet me at twenty past four in the hangar") == \
        "meet me at 4:20 in the hangar"


def test_non_time_numbers_untouched(norm):
    assert norm.normalize("we need four bolts") == "we need four bolts"


# ---------------- disfluency stripping ----------------

@pytest.mark.parametrize("spoken,expected", [
    ("um the power is on", "the power is on"),
    ("uh check the wiring", "check the wiring"),
    ("er i think so", "i think so"),
    ("the um power is on", "the power is on"),
])
def test_standalone_disfluencies_removed(norm, spoken, expected):
    assert norm.normalize(spoken) == expected


@pytest.mark.parametrize("spoken,expected", [
    ("i i got", "i got"),                    # both cases named in the spec
    ("the the power", "the power"),
    ("we we need to check", "we need to check"),
])
def test_immediate_repetitions_collapsed(norm, spoken, expected):
    assert norm.normalize(spoken) == expected


def test_legitimate_doubles_preserved(norm):
    # Collapsing these would corrupt correct English.
    assert norm.normalize("i had had enough") == "i had had enough"


def test_disfluency_substring_not_removed(norm):
    # "umbilical" starts with "um" but is not a disfluency.
    assert norm.normalize("the umbilical is connected") == "the umbilical is connected"


# ---------------- truncation repair ----------------

def test_ver_after_firmware(norm):
    assert norm.normalize("check the firm ware ver") == "check the firmware version"


def test_ver_before_firmware(norm):
    assert norm.normalize("ver firmware is old") == "version firmware is old"


def test_ver_after_software(norm):
    assert norm.normalize("the software ver is wrong") == "the software version is wrong"


def test_ver_alone_is_not_expanded(norm):
    # No firmware/software neighbour, so there is nothing to repair.
    assert norm.normalize("ver is unclear") == "ver is unclear"


# ---------------- combinations and edge cases ----------------

def test_combined_rules_in_one_utterance(norm):
    got = norm.normalize("um the the i m u and the firm ware ver at half past nine")
    assert got == "the IMU and the firmware version at 9:30"


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_empty_input_is_safe(norm, text):
    assert norm.normalize(text) == ""


def test_plain_sentence_is_unchanged(norm):
    s = "the connector is still live"
    assert norm.normalize(s) == s
