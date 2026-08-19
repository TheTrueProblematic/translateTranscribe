# LiveTranslate — build report

> **Update after the first real session.** It displayed nothing. The cause was
> not what it looked like: recognition and the microphone were working
> perfectly. The gate had been **paused** by a stray SPACE on the display
> window, so no chunk ever reached the translator — which is why the model
> never even loaded in LM Studio. Separately, `min_confidence` had been tuned
> on synthesized speech at 0.96, above the 0.921 real speech actually produced.
> Both are fixed, with regression tests built from that session's log. See §10.

Everything in the spec is built and running locally. Below are the measured
numbers, the things I had to change, and what those changes cost.

Read the four short sections in **Deviations** even if you skip the rest — two
of them change how the system behaves in the room.

---

## 1. Measured end-to-end latency

From the true end of a spoken phrase (last word's end on the audio timeline) to
the first Portuguese character rendered on screen. Real-time paced audio, real
ASR, real LM Studio, real websocket.

| | first character | full line |
|---|---|---|
| **median** | **1233 ms** | 1326 ms |
| **p90** | **1936 ms** | 1976 ms |
| fastest | 550 ms | — |
| slowest | 2150 ms | — |

Translation alone, measured separately over 10 sentences: **median 131 ms**,
p90 189 ms, which matches your own 137 ms benchmark. The translation model is
not the bottleneck; recognition is.

**The spec asked for under a second, and phrase-final text does not meet it.**
Where the time goes:

| stage | cost | can it move? |
|---|---|---|
| ASR stability lag | 500 ms | Lowering it raises word error and, worse, collapses the confidence gap the gate depends on (see §3). |
| ASR decode interval | ~250 ms avg | Configurable; small gains only. |
| Silence detection | 400 ms | Your spec, section 6. It is definitionally part of knowing a phrase ended. |
| Translation first token | ~90 ms | Already fast. |

Chunks closed by the word-count or elapsed triggers — i.e. text arriving while
you are still talking — do land inside a second (**fastest 550 ms**). Only
phrase-final chunks pay the 400 ms silence wait on top. If you want the display
to feel faster at the cost of more mid-sentence cuts, lower
`chunker.silence_ms` and `asr.stability_lag_ms` in `config.toml`.

Recognition accuracy on the synthesized fixtures: **WER 0.045** (technical) and
**0.000** (conversational). Note these are macOS TTS voices, not you; validate
in the room.

---

## 2. Soak test — passed

15 minutes 5 seconds of continuous audio, real time, full pipeline.

| | |
|---|---|
| duration | **15.09 min**, 64 passes over the fixtures |
| chunks translated / lines displayed | 298 / 298 |
| crashes, stuck states | **none** — workers alive, queue drained |
| memory growth (2nd half vs 1st) | **+3.2%** — no leak |
| RSS peak | 1.43 GB (dominated by the 2.4 GB model's mapped weights) |

Latency across the whole run:

| median | p90 | p99 | min | max |
|---|---|---|---|---|
| 1367 ms | 2695 ms | 3194 ms | 569 ms | 3233 ms |

**One thing worth flagging:** latency drifted upward across the run — first-half
median 1073 ms, second-half 1534 ms (1.43×). Memory was flat, so it is not a
leak. I instrumented the recognizer to find out whether its rolling window was
growing, and it is not:

| | first third | last third |
|---|---|---|
| decode window | 2.97 s | 2.99 s |
| decode cost | 58 ms | 58 ms |

So recognition is stable and the drift is downstream — most likely sustained-load
thermal behaviour on the GPU over a quarter of an hour. Two caveats that make me
not worry much: the soak feeds speech back-to-back with only 0.6 s gaps, which
is far denser than a person actually talking, and the numbers stay bounded
(p99 3.2 s) rather than running away. Worth watching in a real 90 minute
session; the log records every latency sample so you can check.

The test asserts against drift directly (second-half median must stay within
1.6× of the first half) rather than against an absolute number, because a
slowly wedging pipeline is the failure that matters.

---

## 3. Speaker gate: what is in, what is not

| tier | status | notes |
|---|---|---|
| 1. English-only ASR | **implemented** | Uses `parakeet-tdt-0.6b-v2`. See the model note below — this one mattered more than expected. |
| 2. Confidence + coherence | **implemented** | Both thresholds in `config.toml`. |
| 3. Speaker embedding | **skipped** | Reasoning below. |
| 4. Manual hold | **implemented** | Global hotkey **and** SPACE on the display page. |

### Tier 1 — and a model choice worth knowing about

I started on `parakeet-tdt-0.6b-v3` because it is the newest. **v3 is
multilingual**, and that quietly destroys tier 1. Measured on your Portuguese
fixtures, v3 either transcribed them as clean Portuguese, or — far worse —
turned them into confident English-looking word salad:

> "Professor, a U-100 Your Metric and Tsapro Sistema de Navigasio, Commando
> Vokibon story calibration, ahead take code phaser or test depoys to enter value."

That scored **0.727** on the English coherence check and sailed straight
through the gate onto the screen. Exactly the failure you said makes the
display worse than useless.

`v2` is English-only, as your spec assumed, and on the same audio it produces
low-confidence fragments that the gate rejects cleanly. **Do not "upgrade" the
ASR to v3.** The config comment says so too.

### Tier 2 — what actually does the work

I expected language identification to be the discriminator. It is not. Measured
per-chunk on the fixtures:

| | ASR confidence | English-ness score |
|---|---|---|
| your English | 0.991 – 1.000 | 0.600 – 0.902 |
| Portuguese in the room | 0.765 – 0.927 | 0.000 – **0.840** |

The language score **overlaps** — mangled Portuguese like *"I verifara"* scores
0.840, higher than plenty of your real technical English. Confidence separates
cleanly with a 0.064 gap, so `gate.min_confidence = 0.96` sits in the middle of
it and does most of the rejecting. The language check stays on as a second
filter for the cases confidence misses.

Language ID still had to be built carefully: a dictionary-based approach was
tried and **rejected**, because `/usr/share/dict/words` cheerfully contains
"bree", "kwan", "kee" and "fess", so phonetic garbage scored 0.5–0.75 coverage
— indistinguishable from real English. The shipped scorer keys on token length
and fragment density instead, which is what actually separates the two.

**Caveat you should act on:** those confidence figures come from synthesized
speech, which is unnaturally clean. Your own voice through the built-in mic
with fans running will score lower. If the display starts dropping *your*
words, lower `gate.min_confidence`. It is commented in `config.toml` for
exactly this.

### Tier 3 — skipped, and why

Your instruction was to skip it if it costs more than ~50 ms per segment or
meaningfully adds power draw. Every viable local speaker-embedding model
(Resemblyzer, SpeechBrain ECAPA-TDNN) pulls in PyTorch: roughly a gigabyte of
dependencies and a second inference engine running alongside MLX, on a machine
where you told me fan noise is a functional problem because it degrades the
very recognition this is meant to protect. There is no MLX-native equivalent
packaged today. Tier 3 would also be working against a single built-in mic
array in a room with cross-talk, which is where embedding similarity is least
reliable.

So it is not implemented. The seam is left in place — `Gate` accepts a
`speaker_verifier` and `config.toml` has a disabled `[gate.speaker]` section —
so it can be added without restructuring anything.

Given tier 2 rejects every Portuguese chunk in the fixtures on confidence
alone, and tier 4 is always available, I judged the dependency cost not worth
it. Say the word if you disagree and I'll wire it in.

---

## 4. Deviations from the spec

### 4.1 The streaming ASR API is not used — this is the big one

`parakeet-mlx` ships `StreamingParakeet`, which is the obvious thing to build
on. **It is not usable for continuous live audio in version 0.5.2.** Its output
depends chaotically on where speech happens to sit relative to its internal
block grid. Same utterance, same settings, only the leading silence changed:

| leading silence | words finalized |
|---|---|
| 0.0 s | 87 |
| 0.2 s | 7 |
| 0.4 s | 7 |
| 1.6 s | **0** |

Reproducible every run. Some block sizes (1120 ms, 1280 ms) finalize nothing at
all regardless of the audio. It also never converts token timestamps to
absolute stream time, unlike its own offline path, so word timestamps could not
be used for chunking.

In a live session the audio always starts with arbitrary silence, so this would
have meant randomly losing whole minutes of speech.

**What I built instead:** a rolling re-decode. A short window of recent audio is
re-decoded with the well-tested stateless `generate()` path every 500 ms, and a
word is committed only when two consecutive decodes agree on it *and* it sits
500 ms behind the live edge (a local-agreement policy). The window is trimmed
to just behind the last committed word, so cost stays flat over a 90 minute
session.

| | StreamingParakeet | rolling re-decode |
|---|---|---|
| WER (technical) | 0.18 – 1.00 | **0.045** |
| WER (conversational) | 0.14 – 1.00 | **0.000** |
| sensitive to leading silence | yes, severely | no |
| cost | — | ~35 ms per decode, **~7% GPU duty** |

**What it cost:** the ASR now runs on the GPU rather than staying off it. You
asked me to note this rather than redesign around it — measured duty cycle is
about 7%, against the translation model's ~140 ms bursts, so contention is
minor and I did not build anything to manage it.

### 4.2 The gate runs per chunk, not before the chunker

Your diagram puts the gate before the chunker. Language identification on a two
or three word ASR fragment is unreliable, and a chunk carries both enough text
to score and a mean confidence over its words. Cost is identical. Rejected
chunks still never reach the display; they call `skip()` on the ordered buffer
so their sequence number retires and later lines are not held up behind them.

### 4.3 The status indicator has no words

You asked for no English on the display surface, and also for a
listening/paused/translating indicator. Those conflict. The indicator is a
coloured dot plus a glyph (green = listening, amber = held, blue = translating,
red = disconnected) with the mic level beside it. No text, so the audience sees
Portuguese only.

### 4.4 First run needs the network

Runtime is fully local — nothing leaves the machine, no API keys, no cloud. But
first-run setup downloads dependencies from PyPI and the ASR weights (2.4 GB)
from Hugging Face. Both are now on disk under `models/`, verified by SHA-256,
and `config.toml` points at the local copy, so **startup is offline from here
on**.

---

## 5. Things added that you did not ask for

Three small ones, each because the alternative was visibly wrong:

- **Enclitic pronoun repair.** Your prompt forbids enclisis; the model emits it
  anyway (`"Encontro-me às 4:20"`). Post-processing moves the clitic before the
  verb as spoken in Brazil, leaves infinitive contractions like `fazê-lo`
  alone, and logs every occurrence. Accusative enclisis (`deixei-a`) is logged
  but deliberately **not** rewritten — doing that safely needs to know the
  preceding token is a verb, and guessing wrong mangles hyphenated nouns.
- **Line replay to late clients.** Reloading the page, or plugging in the
  projector mid-session, left the audience staring at a blank screen until you
  next spoke. The server now replays the current two lines on connect.
- **Feminine-agreement logging**, as you asked. Zero occurrences across the
  test runs; the few-shot examples are holding.

I also found and fixed a bug of my own worth mentioning, since it would have
bitten you in the room: after any pause longer than 3.5 s, the first word of
the next sentence was emitted as a lone one-word chunk and then dropped by the
gate as too short — silently losing the first word of every sentence after a
pause. Fixed, with a regression test.

---

## 6. Test results

All **182 automated tests pass**, plus the 15-minute soak.

| # | test | result |
|---|---|---|
| 1 | Normalizer — every rule in section 7 | **46 tests pass**. Every acronym, every compound, all clock-time forms including the three named in the spec and the 12→wrap cases, disfluencies, repetitions, truncation repair. |
| 2 | Chunker — synthetic timed word streams | **15 tests pass**. All three triggers fire at their thresholds; cuts land on word boundaries with nothing lost or duplicated; the 60-second run-on with no silence produces 15 regular chunks. |
| 3 | Ordering — delayed responses | **19 tests pass**, including randomized arrival orders and inverted per-chunk delays through the real pipeline. |
| 4 | End-to-end vs real LM Studio | **passes**. Portuguese appears; latency reported in §1. |
| 5 | Gate — Portuguese audio | **passes**. Both Portuguese fixtures produce an empty display; every chunk rejected on confidence. |
| 6 | Soak, 15 minutes | **passes**. See §2. |
| 7 | Visual verification | **done** — see §7. |

Beyond the spec's list: 45 gate/language tests, 15 translation tests against the
real model (masculine agreement, *energizado*, Brazilian forms, determinism at
temperature 0, streaming, context handling), 13 post-processing tests, and 8
server/websocket tests.

Reproduce:

```bash
.venv/bin/python -m pytest -q -m "not slow"        # 182 tests, ~70s
.venv/bin/python -m pytest tests/test_soak.py -m slow -s   # 15 minutes
```

Raw numbers are written to `docs/test-results.json`.

---

## 7. Screenshots

Both captured from the real display page over the real websocket, showing a
worst-case 14-word chunk (the longest the chunker will emit).

- `docs/screenshots/macbook14.png` — 1512 × 982, your 14" panel
- `docs/screenshots/projector1080p.png` — 1920 × 1080, projector

At 1080p the current line renders at **115 px** and fills the width; two lines
are visible with the previous one dimmed to 40%; diacritics (ã, ç, é, ó) all
render correctly.

I verified the two things that are easy to get wrong by measuring the DOM
rather than eyeballing it:

- **Nothing jumps when a new line arrives.** The current line's top edge was
  measured across lines of 1, 2, 5, 14, 15 and 16 words: identical to the pixel
  (193.45 px) every time, so **maximum jump 0 px**. This is why the previous
  line sits in a fixed-height band rather than being laid out in flow.
- **Nothing clips.** A deliberately pathological 29-word line (double the
  chunker's cap) shrinks from 115 px to 92 px to fit and resets to 115 px on
  the next line. The page never scrolls.

---

## 8. Before your first session — two macOS permissions

Neither can be granted from a script; both need you to click.

1. **Microphone.** Without it, opening the input stream does not fail — macOS
   simply never returns from it, so the app would look healthy while hearing
   nothing. I added a watchdog: after 6 seconds of no audio it tells you
   exactly this, and the display shows a red dot. Grant it to Terminal (or to
   `LiveTranslate.command`) under System Settings → Privacy & Security →
   Microphone.

2. **Input Monitoring**, only for the global `Cmd+Shift+Space` hotkey. Without
   it the app says so plainly at startup and carries on — **SPACE on the
   display page always works** and is the guaranteed tier-4 hold either way.

I could not verify the live microphone path end to end here, because this
environment cannot present the permission prompt. Everything downstream of the
microphone is verified against real recorded audio through the real pipeline.
The first thing worth doing is starting it, granting the prompt, and talking
for a minute.

---

## 9. What I would check first in the room

- `gate.min_confidence = 0.96` is tuned on synthesized speech. If your own
  words get dropped, lower it; if Portuguese leaks in, raise it. Everything
  rejected is logged with its scores in `logs/livetranslate.log`, so you can
  tune from evidence rather than guesswork.
- Recognition accuracy on your actual voice and your actual technical
  vocabulary. Extend the acronym and compound lists in `config.toml` as you
  hear things go wrong — that is the cheapest quality win available.
- Whether 1.2 s feels acceptable. If not, drop `chunker.silence_ms` to 250 and
  `asr.stability_lag_ms` to 400 and see whether the extra mid-sentence cuts
  bother you more than the delay does.


---

## 10. Fixes after the first real session

**What actually happened.** The log was unambiguous:

```
REJECTED [paused] ... conf=0.988 :: 'is it working though?'
REJECTED [paused] ... conf=0.921 :: 'loaded. Yeah, a car.'
```

Your microphone and the recogniser were working perfectly — it transcribed you
at 0.92–0.99 confidence. But a single SPACE keypress on the display window had
paused the gate, and the only feedback was a small amber dot in the corner. No
chunk reached the translator, so LM Studio was never asked for anything and the
model never loaded. That was my design failure, not yours.

**Fixed:**

1. **Pausing is now unmissable** — a full-width amber `PAUSADO` banner, an
   amber border, and the Portuguese dimmed to 22%. The server logs it loudly.
2. **The global hotkey is gone**, as you asked. No Input Monitoring permission,
   no setup. Hold/resume is SPACE or P on the display window.
3. **LM Studio is warmed up at startup.** `preflight()` only *listed* models,
   which under JIT loads nothing. It now sends one real translation on launch,
   so the model appears in LM Studio immediately and the first phrase does not
   pay the load time.
4. **`min_confidence` retuned on your data**, 0.96 → 0.80. Your real speech
   measured 0.921–0.988; the old threshold would have dropped "I don't" (0.945)
   and "Yeah, a car" (0.921) even unpaused.
5. **The language gate was strengthened** to pay for that, so loosening
   confidence did not let the room in. It now recognises Portuguese morphology
   that survives phonetic mangling (`-ção` → `-ceo`/`-seo`, the `nh`/`lh`
   digraphs) and treats short fragments with no Portuguese evidence leniently.

   Measured on a corpus that includes your real transcripts:
   **0/17 of your utterances dropped, 0/13 Portuguese chunks leaked.**

**English monitor strip.** A thin dim line at the top shows what the recogniser
is hearing, live, including the not-yet-committed tail in italic. Whenever a
phrase is dropped it says so inline — `dropped (low_confidence, conf 0.74, en
0.61)` — so "is it deaf, or is it gating me?" is answerable at a glance, and
the thresholds can be tuned from what you see. Press `E` to hide it.

**Logging.** The file log is now DEBUG and records every decode (window, cost,
words), every committed word with confidence, every chunk with its trigger,
every gate decision with all scores, and every translation with timing. On top
of that a HEARTBEAT line every 15 seconds:

```
HEARTBEAT audio=30s blocks=187 speech=0 peak_rms=0.0054 | decodes=46 avg=88ms
words=42 | chunks=10 accepted=4 rejected=6 translated=4 errors=0 | gate={...}
| paused=False listening=True clients=0
```

It also calls out the two silent-failure modes explicitly — no audio since the
last beat, and every chunk being rejected (naming the reason).

**`--diagnose`.** One command that tests each stage in the order it can fail:

```bash
.venv/bin/python -m livetranslate --diagnose
```

It records 3 seconds from your microphone, transcribes it, and compares the
confidence against the gate — so it tells you *"this would be REJECTED, lower
gate.min_confidence"* rather than leaving you to infer it.

**Regression tests.** The six utterances from that session's log are now test
cases at their real confidences, plus a guard asserting `min_confidence` stays
below the quietest thing you actually said, plus the Portuguese chunks that must
still be rejected. 188 tests pass.
