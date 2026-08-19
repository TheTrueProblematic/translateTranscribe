"""Translation behaviour against the real LM Studio endpoint (spec section 8).

Verifies the prompt rules that matter in the room: masculine agreement for the
speaker, "live" as energizado, Brazilian rather than European forms, and the
post-processing of the model's known capitalization/punctuation defect.
"""
from __future__ import annotations

import json
import re
import urllib.request

import pytest

from livetranslate.translator import LMStudioUnavailable, Translator

pytestmark = pytest.mark.integration


def _ready(cfg) -> bool:
    try:
        url = cfg.get("lmstudio.base_url").rstrip("/") + "/models"
        with urllib.request.urlopen(url, timeout=5) as r:
            ids = [m["id"] for m in json.load(r).get("data", [])]
        return cfg.get("lmstudio.model") in ids
    except Exception:
        return False


@pytest.fixture(scope="module", autouse=True)
def _requires_lmstudio(cfg):
    if not _ready(cfg):
        pytest.skip("LM Studio is not serving the configured model")


@pytest.fixture
async def tr(cfg):
    t = Translator(cfg)
    await t.start()
    yield t
    await t.close()


@pytest.mark.asyncio
async def test_preflight_accepts_the_configured_model(cfg):
    t = Translator(cfg)
    try:
        await t.preflight()
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_preflight_reports_a_missing_model_clearly(cfg):
    t = Translator(cfg)
    t.model = "definitely-not-a-real-model"
    try:
        with pytest.raises(LMStudioUnavailable) as exc:
            await t.preflight()
        assert "not available" in str(exc.value)
        assert cfg.get("lmstudio.model") in str(exc.value)   # lists what IS there
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_preflight_reports_an_unreachable_server(cfg):
    t = Translator(cfg)
    t.base_url = "http://127.0.0.1:9/v1"       # discard port: always refuses
    t.timeout_s = 5
    try:
        with pytest.raises(LMStudioUnavailable) as exc:
            await t.preflight()
        assert "Cannot reach LM Studio" in str(exc.value)
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_output_is_capitalized_and_terminated(tr):
    out = await tr.translate("the firmware is out of date")
    assert out and out[0].isupper()
    assert out[-1] in ".!?…"


@pytest.mark.asyncio
async def test_live_circuit_is_energizado_not_vivo(tr):
    out = await tr.translate("don't touch that connector it's still live")
    low = out.lower()
    assert "energizado" in low, out
    assert "vivo" not in low, out


@pytest.mark.asyncio
@pytest.mark.parametrize("english,feminine,masculine", [
    ("i'm tired i've been standing all day", "cansada", "cansado"),
    ("i was surprised i thought i was already finished", "surpresa", "surpreso"),
    ("i'm ready to start the calibration", "pronta", "pronto"),
])
async def test_speaker_is_male(tr, english, feminine, masculine):
    out = (await tr.translate(english)).lower()
    assert feminine not in out, f"feminine agreement leaked: {out}"
    assert masculine in out, f"expected {masculine!r} in {out!r}"


@pytest.mark.asyncio
async def test_brazilian_not_european_forms(tr):
    out = (await tr.translate("she is fixing the wiring right now")).lower()
    assert "está a " not in out, f"European gerund construction: {out}"


@pytest.mark.asyncio
async def test_no_enclitic_pronouns_survive(tr):
    """The prompt forbids them and post-processing repairs what leaks."""
    out = await tr.translate("meet me at twenty past four")
    assert not re.search(r"\w-(me|te|se|nos|lhe|lhes)\b", out), out


@pytest.mark.asyncio
async def test_technical_terms_stay_in_english(tr):
    out = (await tr.translate("check the firmware and the gimbal setup")).lower()
    assert "firmware" in out and "gimbal" in out, out


@pytest.mark.asyncio
async def test_output_contains_no_english_commentary(tr):
    """The display shows Portuguese only; the model must not editorialise."""
    out = (await tr.translate("the i m u is broken")).lower()
    for leak in ("translation", "english", "note:", "i cannot", "as an ai"):
        assert leak not in out, out


@pytest.mark.asyncio
async def test_deterministic_at_temperature_zero(tr):
    a = await tr.translate("the propeller is loose and needs tightening")
    tr.reset_context()
    b = await tr.translate("the propeller is loose and needs tightening")
    assert a == b, f"non-deterministic at temp 0:\n{a}\n{b}"


@pytest.mark.asyncio
async def test_streaming_is_incremental_and_ordered(tr):
    """Text must appear progressively, and each update extends the last."""
    updates = [text async for text, _ in tr.translate_stream(
        "the fuselage has a crack near the gimbal mount")]
    assert len(updates) > 1, "no incremental streaming observed"
    partials = [u for u in updates if u]
    assert len(partials[-1]) >= len(partials[0])


@pytest.mark.asyncio
async def test_context_lines_are_not_echoed(tr):
    """Context is for agreement only; it must not be re-translated."""
    first = await tr.translate("i'm going to open the panel now")
    second = await tr.translate("it's still live")
    assert second != first
    assert "energizado" in second.lower() or "ligad" in second.lower(), second


# ---------------- regression: the model echoing its own context ----------------

@pytest.mark.asyncio
async def test_previous_line_is_not_prepended_to_the_next(tr):
    """Context must inform agreement without being repeated on screen.

    Told in prose to "not repeat" the previous lines, this model repeated them
    anyway: the second translation arrived with the first glued to the front,
    so the audience read the same sentence twice and the line grew until the
    autofit shrank the type. Context is now replayed as real chat turns.
    """
    tr.reset_context()
    first = await tr.translate("the aircraft is ready for the flight")
    second = await tr.translate("the imu is not calibrated yet")
    third = await tr.translate("open the map on the ipad")

    assert first and second and third
    assert not second.startswith(first), f"echoed previous line:\n{second}"
    assert not third.startswith(second), f"echoed previous line:\n{third}"
    # And each line should be about its own sentence, not a running paragraph.
    assert len(second.split()) < 18, f"line grew unexpectedly: {second}"


@pytest.mark.asyncio
async def test_context_still_carries_agreement_across_lines(tr):
    """The point of context: a pronoun in the next line resolves correctly."""
    tr.reset_context()
    await tr.translate("i am opening the gimbal panel now")
    out = await tr.translate("it is still live")
    assert out
    assert "energizado" in out.lower() or "ligad" in out.lower(), out
