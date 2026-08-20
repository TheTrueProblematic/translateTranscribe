"""The always-on-top subtitle overlay.

tkinter is cross-platform, so the real widget is exercised here rather than
mocked. Skipped where there is no display (headless CI, or a Python built
without Tk).
"""
import queue

import pytest

tk = pytest.importorskip("tkinter", reason="tkinter not available")

from livetranslate.config import Config
from livetranslate.overlay import COLOUR_FROM_PT, SubtitleOverlay


@pytest.fixture(scope="module")
def cfg_win():
    return Config.load("config.windows.toml")


# One Tk root for the whole module, reset between tests. Creating and
# destroying several roots in one process segfaults Tk on macOS, and is
# discouraged everywhere else, so this is the correct shape regardless.
@pytest.fixture(scope="module")
def _root(cfg_win):
    try:
        ov = SubtitleOverlay(cfg_win, queue.Queue())
    except tk.TclError as exc:            # no display attached
        pytest.skip(f"no display available: {exc}")
    yield ov
    ov.close()


@pytest.fixture
def overlay(_root, cfg_win):
    _root.position = cfg_win.get("overlay.position", "bottom")
    _root.visible = True
    _root._font.configure(size=cfg_win.get("overlay.font_size"))
    _root._layout()
    _root.set_line("")
    while not _root.inbox.empty():
        _root.inbox.get_nowait()
    return _root


SHORT = "Estou pronto."
MEDIUM = "Não toque nesse conector, ele ainda está energizado."
LONG = ("Não toque nesse conector, ele ainda está energizado e pode causar um "
        "choque elétrico muito sério se você encostar nele agora mesmo.")
ABSURD = " ".join(["palavra"] * 80)


# ---------------- the two-line rule ----------------

@pytest.mark.parametrize("text", [SHORT, MEDIUM, LONG, ABSURD])
def test_never_more_than_two_lines(overlay, text):
    """The whole point of the overlay: it must not creep down the screen."""
    overlay.set_line(text)
    shown = overlay.label.cget("text")
    assert shown.count("\n") + 1 <= 2, f"{shown.count(chr(10)) + 1} lines for {text[:40]!r}"


def test_long_text_shrinks_before_it_is_cut(overlay, cfg_win):
    """Shrinking is less disruptive to read than losing words."""
    overlay.set_line(SHORT)
    assert overlay._font.cget("size") == cfg_win.get("overlay.font_size")
    overlay.set_line(LONG)
    assert overlay._font.cget("size") <= cfg_win.get("overlay.font_size")
    assert overlay._font.cget("size") >= cfg_win.get("overlay.min_font_size")


def test_absurd_text_is_truncated_from_the_front(overlay):
    """Only after shrinking fails, and keeping the end of the sentence."""
    overlay.set_line(ABSURD)
    shown = overlay.label.cget("text")
    assert shown.startswith("…")
    assert shown.count("\n") + 1 <= 2


def test_font_returns_to_full_size_for_the_next_short_line(overlay, cfg_win):
    overlay.set_line(LONG)
    overlay.set_line(SHORT)
    assert overlay._font.cget("size") == cfg_win.get("overlay.font_size")


def test_empty_line_is_safe(overlay):
    overlay.set_line("")
    assert overlay.label.cget("text") == ""


# ---------------- colour by direction ----------------

def test_room_speech_is_a_different_colour(overlay, cfg_win):
    overlay.set_line(MEDIUM, "en2pt")
    assert overlay.label.cget("fg") == cfg_win.get("overlay.foreground")
    overlay.set_line("Professor, I have a question.", "pt2en")
    assert overlay.label.cget("fg") == COLOUR_FROM_PT


# ---------------- position ----------------

def test_position_toggles_between_top_and_bottom(overlay):
    start = overlay.position
    assert overlay.toggle_position() != start
    assert overlay.toggle_position() == start


def _y_of(overlay) -> int:
    """Y offset from the geometry string: WxH+X+Y.

    Read rather than measured, because querying a borderless window's real
    position needs the window manager to have mapped it, which is unreliable
    under a test runner.
    """
    return int(overlay.root.geometry().rsplit("+", 1)[1])


def test_moving_to_the_top_actually_moves_the_window(overlay):
    overlay.position = "bottom"
    overlay._layout()
    bottom_y = _y_of(overlay)
    overlay.toggle_position()
    top_y = _y_of(overlay)
    assert top_y < bottom_y, f"top ({top_y}) should be above bottom ({bottom_y})"


def test_the_strip_fits_on_screen(overlay):
    sw, sh = overlay._screen
    geom = overlay.root.geometry()
    x = int(geom.split("+")[1])
    assert 0 <= x
    assert x + overlay._window_width() <= sw
    assert overlay._window_height() < sh / 2, "the strip should not dominate the screen"


def test_height_holds_exactly_two_lines_plus_padding(overlay, cfg_win):
    expected = overlay._line_height() * 2 + cfg_win.get("overlay.pad_y") * 2 + 6
    assert overlay._window_height() == expected


# ---------------- visibility ----------------

def test_visibility_toggles(overlay):
    assert overlay.visible is True
    assert overlay.toggle_visible() is False
    assert overlay.toggle_visible() is True


# ---------------- messages from the pipeline ----------------

def test_queue_messages_are_applied(overlay):
    overlay.inbox.put({"type": "line", "text": MEDIUM, "direction": "en2pt"})
    overlay._drain()
    assert "conector" in overlay.label.cget("text")


def test_clear_empties_the_strip(overlay):
    overlay.set_line(MEDIUM)
    overlay.inbox.put({"type": "clear"})
    overlay._drain()
    assert overlay.label.cget("text") == ""


def test_paused_status_dims_the_text(overlay, cfg_win):
    overlay.set_line(MEDIUM, "en2pt")
    overlay.inbox.put({"type": "status", "paused": True})
    overlay._drain()
    assert overlay.label.cget("fg") != cfg_win.get("overlay.foreground")
    overlay.inbox.put({"type": "status", "paused": False})
    overlay._drain()
    assert overlay.label.cget("fg") == cfg_win.get("overlay.foreground")


def test_an_unknown_message_does_not_break_the_overlay(overlay):
    overlay.set_line(MEDIUM)
    overlay.inbox.put({"type": "level", "rms": 0.5})
    overlay.inbox.put({"type": "nonsense"})
    overlay.inbox.put({})
    overlay._drain()
    assert "conector" in overlay.label.cget("text")


def test_draining_an_empty_queue_is_safe(overlay):
    overlay._drain()


def test_position_setting_is_honoured(cfg_win, overlay):
    """A config asking for the top must start at the top."""
    overlay.position = "top"
    overlay._layout()
    top_y = _y_of(overlay)
    overlay.position = "bottom"
    overlay._layout()
    assert top_y < _y_of(overlay)
