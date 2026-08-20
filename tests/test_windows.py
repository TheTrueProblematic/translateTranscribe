"""The Windows build: backend selection, configs, overlay and hotkeys.

These run on any platform. The parts that genuinely cannot (registering a
system-wide hotkey, opening a window on a headless machine) are covered by
testing the logic around them: parsing, layout arithmetic, text fitting.
"""
import platform
import sys

import pytest

from livetranslate.asr_backend import is_apple_silicon, resolve_backend
from livetranslate.config import Config
from livetranslate.hotkeys_win import (MOD_ALT, MOD_CONTROL, MOD_NOREPEAT,
                                       MOD_SHIFT, MOD_WIN, GlobalHotkeys,
                                       HotkeyError, parse_binding)
from livetranslate.normalizer import Normalizer


# ---------------- backend selection ----------------

@pytest.mark.parametrize("value,expected", [
    ("parakeet-mlx", "parakeet-mlx"), ("parakeet", "parakeet-mlx"),
    ("mlx", "parakeet-mlx"), ("faster-whisper", "faster-whisper"),
    ("whisper", "faster-whisper"), ("fasterwhisper", "faster-whisper"),
])
def test_backend_names_resolve(value, expected):
    assert resolve_backend(value) == expected


def test_auto_follows_the_platform():
    expected = "parakeet-mlx" if is_apple_silicon() else "faster-whisper"
    assert resolve_backend("auto") == expected


def test_unknown_backend_falls_back_rather_than_crashing():
    assert resolve_backend("nonsense") in ("parakeet-mlx", "faster-whisper")
    assert resolve_backend("") in ("parakeet-mlx", "faster-whisper")


def test_parakeet_is_refused_off_apple_silicon():
    """Better a clear message than an ImportError from inside a thread."""
    if is_apple_silicon():
        pytest.skip("this machine is Apple Silicon")
    from livetranslate.asr_backend import create_asr
    cfg = Config.load("config.windows.toml")
    cfg._data["asr"]["backend"] = "parakeet-mlx"
    with pytest.raises(RuntimeError, match="Apple Silicon"):
        create_asr(cfg, None, lambda w: None, lambda t, s: None)


# ---------------- the Windows configs ----------------

@pytest.fixture(scope="module")
def win():
    return Config.load("config.windows.toml")


@pytest.fixture(scope="module")
def win_ars():
    return Config.load("config.windows.ars.toml")


def test_windows_config_selects_whisper(win):
    assert resolve_backend(win.get("asr.backend")) == "faster-whisper"


@pytest.mark.parametrize("key", [
    "chunker.silence_ms", "chunker.max_words", "chunker.max_elapsed_ms",
    "gate.min_english_score", "gate.min_words", "dual.enabled",
    "dual.portuguese_min", "transcript.enabled", "lmstudio.context_lines",
    "vad.energy_threshold",
])
def test_windows_inherits_shared_settings(win, key):
    """Tuning config.toml must tune Windows too, for everything that is not
    genuinely platform-specific."""
    assert win.get(key) == Config.load("config.toml").get(key)


def test_windows_overrides_only_what_it_should(win):
    """The deliberate divergences, each for a reason recorded in the config.

    Asserted explicitly so that adding a fourth override is a conscious act
    rather than something that drifts in unnoticed.
    """
    base = Config.load("config.toml")
    # A different translation model: there is no GGUF of the Mac's MLX build.
    assert win.get("lmstudio.model") != base.get("lmstudio.model")
    # A different recogniser: MLX is Apple Silicon only.
    assert win.get("asr.backend") != base.get("asr.backend", "auto")
    # Faster reading: the overlay's small type fits more per screen, and the
    # recogniser here is slower per utterance, so the hold has to give.
    assert win.get("display.reading_cps") > base.get("display.reading_cps")
    assert win.get("display.max_dwell_ms") < base.get("display.max_dwell_ms")
    assert win.get("display.min_dwell_ms") < base.get("display.min_dwell_ms")


def test_reading_speeds_are_sane(win):
    """Fast, but not past what anyone can actually read."""
    base = Config.load("config.toml")
    for cfg in (base, win):
        assert 12.0 <= cfg.get("display.reading_cps") <= 25.0
        assert cfg.get("display.min_dwell_ms") >= 700
        assert cfg.get("display.max_dwell_ms") > cfg.get("display.min_dwell_ms")


def test_windows_overlay_settings_are_present(win):
    assert win.get("overlay.font_size") > 0
    assert 0.0 < win.get("overlay.opacity") <= 1.0
    assert win.get("overlay.position") in ("top", "bottom")
    for key in ("overlay.hotkey_toggle", "overlay.hotkey_position",
                "overlay.hotkey_pause"):
        parse_binding(win.get(key))          # must not raise


def test_windows_hotkeys_are_distinct(win):
    bindings = [win.get(f"overlay.hotkey_{n}") for n in ("toggle", "position", "pause")]
    assert len(set(bindings)) == len(bindings), f"duplicate hotkeys: {bindings}"


def test_ars_on_windows_keeps_windows_settings(win, win_ars):
    """Parent order matters: config.ars.toml also extends config.toml, so
    listing it last would drag the macOS timings back in."""
    for key in ("asr.backend", "whisper.model", "overlay.font_size",
                "whisper.utterance_silence_ms"):
        assert win_ars.get(key) == win.get(key), f"{key} lost the Windows value"


def test_ars_on_windows_keeps_the_vocabulary(win_ars):
    norm = Normalizer(win_ars)
    assert norm.normalize("the hair craft and the emu") == "the aircraft and the IMU"
    assert "SHOTOVER" in (win_ars.get("prompt.extra_rules") or "")


# ---------------- LM Studio on another machine ----------------

@pytest.mark.parametrize("value,expected", [
    ("192.168.1.50", "http://192.168.1.50:1234/v1"),
    ("192.168.1.50:4321", "http://192.168.1.50:4321/v1"),
    ("desktop.local", "http://desktop.local:1234/v1"),
    ("http://10.0.0.7:1234/v1", "http://10.0.0.7:1234/v1"),
    ("https://box:8080/api/v1", "https://box:8080/api/v1"),
    ("192.168.1.50/", "http://192.168.1.50:1234/v1"),
])
def test_lmstudio_override_accepts_the_obvious_spellings(value, expected):
    from livetranslate.__main__ import apply_lmstudio_override
    cfg = Config.load("config.toml")
    apply_lmstudio_override(cfg, value)
    assert cfg.get("lmstudio.base_url") == expected


def test_lmstudio_override_left_alone_when_absent():
    from livetranslate.__main__ import apply_lmstudio_override
    cfg = Config.load("config.toml")
    before = cfg.get("lmstudio.base_url")
    apply_lmstudio_override(cfg, None)
    apply_lmstudio_override(cfg, "")
    assert cfg.get("lmstudio.base_url") == before


# ---------------- hotkey parsing ----------------

@pytest.mark.parametrize("binding,mods,vk", [
    ("ctrl+alt+s", MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x53),
    ("ctrl+shift+f8", MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, 0x77),
    ("alt+space", MOD_ALT | MOD_NOREPEAT, 0x20),
    ("win+alt+1", MOD_WIN | MOD_ALT | MOD_NOREPEAT, 0x31),
    ("CTRL+ALT+T", MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x54),
])
def test_hotkey_parsing(binding, mods, vk):
    assert parse_binding(binding) == (mods, vk)


@pytest.mark.parametrize("binding", [
    "s", "", "ctrl+", "ctrl+alt", "ctrl+alt+notakey", "ctrl+a+b", "+++",
])
def test_bad_hotkeys_are_rejected(binding):
    with pytest.raises(HotkeyError):
        parse_binding(binding)


def test_a_bare_key_is_refused():
    """Registering one system-wide would swallow it from every application."""
    with pytest.raises(HotkeyError, match="modifier"):
        parse_binding("f9")


def test_hotkeys_are_a_no_op_off_windows():
    hk = GlobalHotkeys(loop=None)
    assert hk.add("ctrl+alt+s", lambda: None) is True
    if sys.platform != "win32":
        assert hk.start() is False
        hk.stop()


def test_unparseable_hotkey_is_recorded_not_raised():
    hk = GlobalHotkeys(loop=None)
    assert hk.add("nonsense", lambda: None) is False
    assert hk.failed and hk.failed[0][0] == "nonsense"
