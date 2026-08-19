"""Spec section 12, test 2: chunker driven by synthetic timed word streams.

Verifies all three emit triggers fire, that cuts land on word boundaries, and
that a 60 second run-on utterance with no silence still produces regular chunks.
"""
import pytest

from livetranslate.chunker import Chunk, Chunker, Word


def stream(n, start=0.0, word_dur=0.25, gap=0.05, prefix="w"):
    """n words back to back with no silence long enough to trigger a cut."""
    words, t = [], start
    for i in range(n):
        words.append(Word(f"{prefix}{i}", t, t + word_dur))
        t += word_dur + gap
    return words


# ---------------- trigger 1: silence ----------------

def test_silence_trigger_fires_at_threshold(cfg):
    c = Chunker(cfg)
    for w in stream(3):
        c.add_word(w)
    last_end = 0.25 + 2 * 0.30
    assert c.tick(last_end + 0.399) == []          # just under 400ms
    out = c.tick(last_end + 0.401)                 # just over
    assert len(out) == 1 and out[0].reason == "silence"
    assert out[0].text == "w0 w1 w2"


def test_speech_activity_suppresses_the_silence_cut(cfg):
    c = Chunker(cfg)
    for w in stream(3):
        c.add_word(w)
    # 1.15s after the last word: far past the 400ms silence threshold, but
    # still under max_elapsed, so only the silence trigger is in play.
    assert c.tick(2.0, speech_active=True) == []   # VAD says still speaking


def test_silence_does_not_emit_an_empty_chunk(cfg):
    c = Chunker(cfg)
    assert c.tick(10.0) == []


# ---------------- trigger 2: word count ----------------

def test_word_count_trigger(cfg):
    c = Chunker(cfg)
    emitted = []
    for w in stream(14):
        emitted += c.add_word(w)
    assert len(emitted) == 1
    assert emitted[0].reason == "max_words"
    assert emitted[0].word_count == cfg.get("chunker.max_words")


def test_word_count_trigger_is_configurable():
    c = Chunker(silence_ms=400, max_words=5, max_elapsed_ms=3500)
    emitted = []
    for w in stream(5):
        emitted += c.add_word(w)
    assert len(emitted) == 1 and emitted[0].word_count == 5


# ---------------- trigger 3: elapsed ----------------

def test_elapsed_trigger(cfg):
    c = Chunker(cfg)
    for w in stream(4, word_dur=0.3, gap=0.05):
        c.add_word(w)
    # Speech still active, well under 14 words: only elapsed can fire.
    assert c.tick(3.4, speech_active=True) == []
    out = c.tick(3.6, speech_active=True)
    assert len(out) == 1 and out[0].reason == "max_elapsed"


# ---------------- word boundaries ----------------

def test_cuts_land_on_word_boundaries(cfg):
    """No chunk may split a word: text must rejoin exactly, and chunk
    start/end must coincide with real word timestamps."""
    words = stream(40)
    c = Chunker(cfg)
    chunks = []
    for w in words:
        chunks += c.add_word(w)
        chunks += c.tick(w.end, speech_active=True)
    chunks += c.flush()

    rejoined = [w.text for ch in chunks for w in ch.words]
    assert rejoined == [w.text for w in words]          # nothing lost or split
    for ch in chunks:
        assert ch.start == ch.words[0].start
        assert ch.end == ch.words[-1].end
        assert ch.text == " ".join(w.text for w in ch.words)


def test_no_word_appears_in_two_chunks(cfg):
    words = stream(50)
    c = Chunker(cfg)
    chunks = []
    for w in words:
        chunks += c.add_word(w)
    chunks += c.flush()
    seen = [w.text for ch in chunks for w in ch.words]
    assert len(seen) == len(set(seen)) == 50


# ---------------- the run-on case the spec calls out ----------------

def test_sixty_second_runon_with_no_silence_still_chunks_regularly(cfg):
    """The speaker does not reliably pause. 60s of continuous speech must
    still produce regular chunks via the word-count/elapsed triggers."""
    word_dur, gap = 0.25, 0.05
    per_word = word_dur + gap
    n = int(60.0 / per_word)                      # 200 words over 60 seconds
    words = stream(n, word_dur=word_dur, gap=gap)

    c = Chunker(cfg)
    chunks = []
    for w in words:
        chunks += c.add_word(w)
        # speech_active=True throughout: there is never any silence.
        chunks += c.tick(w.end, speech_active=True)
    chunks += c.flush()

    assert len(chunks) >= 12, f"only {len(chunks)} chunks in 60s"
    assert all(ch.reason in ("max_words", "max_elapsed", "flush") for ch in chunks)
    assert not any(ch.reason == "silence" for ch in chunks)

    # Every chunk must respect the configured ceilings.
    max_words = cfg.get("chunker.max_words")
    max_elapsed = cfg.get("chunker.max_elapsed_ms") / 1000.0
    for ch in chunks:
        assert ch.word_count <= max_words
        assert (ch.end - ch.start) <= max_elapsed + per_word

    # And they must arrive regularly, not in one late burst.
    gaps = [b.end - a.end for a, b in zip(chunks, chunks[1:])]
    assert max(gaps) <= max_elapsed + per_word, f"largest gap {max(gaps):.2f}s"

    assert [w.text for ch in chunks for w in ch.words] == [w.text for w in words]


# ---------------- sequencing and confidence ----------------

def test_sequence_numbers_are_contiguous_from_one(cfg):
    c = Chunker(cfg)
    chunks = []
    for w in stream(45):
        chunks += c.add_word(w)
    chunks += c.flush()
    assert [ch.seq for ch in chunks] == list(range(1, len(chunks) + 1))


def test_mean_confidence_is_averaged_over_words(cfg):
    c = Chunker(cfg)
    for i, conf in enumerate([0.2, 0.4, 0.6]):
        c.add_word(Word(f"w{i}", i * 0.3, i * 0.3 + 0.2, confidence=conf))
    ch = c.flush()[0]
    assert ch.mean_confidence == pytest.approx(0.4)


def test_flush_on_empty_buffer_emits_nothing(cfg):
    assert Chunker(cfg).flush() == []


# ---------------- regression: pauses between utterances ----------------

def test_first_word_after_a_long_pause_is_not_emitted_alone(cfg):
    """The speaker pauses between topics, then starts a new sentence.

    Regression: while the buffer was empty the elapsed anchor went stale, so
    the first word spoken after a pause longer than max_elapsed was emitted
    immediately as a one-word chunk. The gate then dropped it as too short,
    silently losing the first word of every sentence after a pause.
    """
    c = Chunker(cfg)
    for w in stream(14):                       # emit a first chunk
        c.add_word(w)
    for t in (5.0, 10.0, 20.0, 30.0):          # 30 seconds of silence
        assert c.tick(t) == []

    assert c.add_word(Word("hello", 30.0, 30.2)) == []
    assert c.tick(30.25, speech_active=True) == [], \
        "first word after the pause was emitted on its own"

    for i, word in enumerate(["there", "everyone", "let", "us", "begin"]):
        c.add_word(Word(word, 30.3 + i * 0.3, 30.5 + i * 0.3))
    out = c.tick(33.9, speech_active=True)
    assert len(out) == 1
    assert out[0].text.startswith("hello there"), out[0].text
    assert out[0].word_count >= 6


def test_idle_ticks_do_not_emit_anything(cfg):
    c = Chunker(cfg)
    for t in range(0, 60, 1):
        assert c.tick(float(t)) == []


def test_pause_then_resume_still_respects_word_limit(cfg):
    c = Chunker(cfg)
    for t in (5.0, 30.0, 60.0):
        c.tick(t)
    emitted = []
    for w in stream(14, start=60.0):
        emitted += c.add_word(w)
    assert len(emitted) == 1
    assert emitted[0].word_count == cfg.get("chunker.max_words")
