# AGENTS.md

Operating guide for any AI agent working on LiveTranslate. Read this fully
before changing anything. It is the single source of truth for context that is
not obvious from the code, and it is a **living document** — see the policy at
the end, which is mandatory.

---

## 1. What this is

A local live-captioning system. Max, a presenter at SHOTOVER Systems, speaks
English to a room of Brazilian Portuguese speakers. The audience reads a
Portuguese translation off a projector or his laptop screen. When someone in
the room answers in Portuguese, that is translated back into English for him,
shown in blue.

Everything runs on the machine. No cloud APIs, no keys, no network calls at
runtime.

### Who it is for, and what that implies

This is not a general product. It is one person's tool for a specific room, and
several design decisions only make sense in that light:

- **The audience is several metres away.** Type is enormous. Only two lines are
  ever on screen. If a change makes text smaller or denser, it is wrong.
- **A blank screen is the worst failure.** Worse than an imperfect
  translation. Bias every threshold toward showing something.
- **Wrong text is nearly as bad as none.** If the display shows the room a
  confident mistranslation of a side conversation, it is worse than useless.
  This tension is the source of most of the tuning in the project.
- **Fan noise is a functional problem.** The MacBook's own fans degrade its
  microphone, which degrades recognition, which degrades everything. Power
  efficiency is a real constraint, not a nicety.
- **Sessions are 30 to 90 minutes, live, in front of people.** There is no
  chance to restart or debug. Failures must be visible and self-explanatory.

---

## 2. Running it

```bash
./LiveTranslate.command                     # normal mode
./ARSLiveTranslate.command                  # ARS training mode (adds vocabulary)
.venv/bin/python -m livetranslate --diagnose   # self-test each stage
```

Requires LM Studio running on `http://localhost:1234` with
`hunyuan-mt2-1.8b-mlx` available. First run creates `.venv` and installs
dependencies; later runs skip that.

macOS Microphone permission is required. Without it, opening the input stream
**never returns** rather than failing — a watchdog reports this after 6s.

### Tests

```bash
.venv/bin/python -m pytest -q -m "not slow"              # 267 tests, ~2 min
.venv/bin/python -m pytest -q -m "not slow and not integration"   # no LM Studio needed
.venv/bin/python -m pytest tests/test_soak.py -m slow -s # 15 minute soak
```

Integration tests need LM Studio running and the ASR weights present; they skip
cleanly otherwise.

---

## 3. Architecture

```
built-in mic
  -> ASR              rolling re-decode, parakeet-mlx           asr.py
  -> chunker          silence / word-count / elapsed            chunker.py
  -> normalizer       acronyms, compounds, clock times          normalizer.py
  -> gate             confidence + language routing             gate.py, langid.py
  -> translator       LM Studio, both directions                translator.py
  -> ordered buffer   strict sequence order                     output_buffer.py
  -> reading-time gate minimum dwell, catch-up queue            pipeline.py
  -> websocket        static page + live updates                server.py
  -> browser          the display surface                       static/
```

`pipeline.py` orchestrates all of it. `__main__.py` wires startup, preflight
and the heartbeat.

### Module map

| file | responsibility |
|---|---|
| `asr.py` | Mic capture, rolling decode, word commitment. The most subtle file. |
| `chunker.py` | Groups words into translatable chunks on three triggers. |
| `normalizer.py` | Deterministic text repair before translation. |
| `langid.py` | English and Portuguese scoring; routes each line. |
| `gate.py` | Decides accept/reject and which direction to translate. |
| `translator.py` | LM Studio client, both directions, streaming, context. |
| `postprocess.py` | Capitalization, punctuation, known model defects. |
| `output_buffer.py` | Guarantees display order by sequence number. |
| `pipeline.py` | Orchestration, reading-time scheduling, backlog. |
| `server.py` | aiohttp static + websocket, loopback only. |
| `transcript.py` | Per-run session records in both languages. |
| `diagnose.py` | `--diagnose` self-test. |
| `config.py` | TOML loading with `extends` layering. |
| `prompt.py` | System prompts for both directions; injects session vocabulary. |
| `logging_setup.py` | Rolling debug log; console stays quiet unless something is wrong. |

---

## 4. Landmines

Things that have already cost significant time. Do not rediscover them.

### 4.1 MLX streams are thread-local

Arrays created on one thread cannot be evaluated on another —
`RuntimeError: There is no Stream(cpu, 1) in current thread`. This includes the
model weights and the cached mel filterbank.

**The ASR model must be loaded inside the decode worker thread.** Never load it
on the main thread and hand it over. `ParakeetASR.wait_ready()` exists so
callers can await the load and surface failures.

### 4.2 parakeet-mlx `StreamingParakeet` is unusable

Version 0.5.2's streaming decoder produces output that depends chaotically on
where speech falls relative to its internal block grid. Same utterance, only
the leading silence changed:

| leading silence | words finalized |
|---|---|
| 0.0 s | 87 |
| 0.2 s | 7 |
| 1.6 s | 0 |

Some block sizes (1120 ms, 1280 ms) finalize nothing at all. It also never
converts token timestamps to absolute stream time.

**We do not use it.** `asr.py` re-decodes a short rolling window with the
stateless `generate()` path every 500 ms and commits a word only when two
consecutive decodes agree on it and it sits 500 ms behind the live edge
(local agreement). This measured 0.00–0.05 WER against 0.18–1.00, is
insensitive to leading silence, and costs about 7% GPU duty.

If you are tempted to "simplify" this back to the streaming API, don't.

### 4.3 The display page caches aggressively

Chrome runs the display in a persistent profile (`--app` with a fixed
`--user-data-dir`). Static assets are served `Cache-Control: no-store` for this
reason. A feature that appears to be missing from the display is very often a
stale asset. Remove the no-store middleware and this recurs.

### 4.4 `logs/transcripts/` holds real session data

An earlier agent ran `rm -rf logs/transcripts` in a test script and destroyed
two real sessions (3183 entries) permanently. `rm` does not use the Trash.

**Never delete or clear that directory.** Tests point transcripts at a
temporary directory and disable them (`tests/conftest.py`); keep it that way.

### 4.5 The translation model echoes its context

Given previous lines pasted into the user message with an instruction not to
repeat them, hunyuan-mt2 repeats them anyway — prepending the last translation
to the next line. Context is therefore replayed as genuine user/assistant chat
turns, with a `_strip_echoed_context` safety net. Do not "simplify" context
back into the prompt text.

The two directions keep **separate** contexts. Sharing them makes the model
answer the wrong conversation.

### 4.6 Config layering

`config.ars.toml` declares `extends = "config.toml"` and overrides only the
session vocabulary. Tables merge key by key. Do not fork the config into two
copies — the whole point is that tuning the base tunes both modes, and a test
asserts the shared settings stay identical.

---

## 5. Decisions and why

Where a decision was reversed, both sides are recorded. Reversals are the
easiest thing for a new agent to undo by accident.

### ASR model: multilingual v3 (reversed once)

Originally `parakeet-tdt-0.6b-v2`, English-only, chosen *because* it fails on
Portuguese — that failure was the first line of defence against the room's
speech reaching the screen.

That was correct while the goal was rejecting Portuguese. It became wrong when
the goal changed to understanding it. An English-only model cannot report "that
was Portuguese"; it can only return English it is unsure about, which is why
447 lines in a real session were dropped as low-confidence when most were
ordinary speech.

Now `parakeet-tdt-0.6b-v3`, multilingual. Measured: English accuracy unchanged
within noise, decoding faster, and Portuguese scores 0.000 on the English check
instead of v2's ambiguous 0.301.

**If `dual.enabled` is set to false, switch back to v2.** In single-language
mode, v2's inability to speak Portuguese is a feature.

### Language routing is text-based

v3's vocabulary contains `<|en|>` and `<|pt|>` tokens, but parakeet-mlx's greedy
decode never emits them. Routing uses `english_score()` and `portuguese_score()`
in `langid.py`. Validated on 400 real English lines from live sessions: zero
misrouted.

### A dictionary was tried for language ID and rejected

`/usr/share/dict/words` contains "bree", "kwan", "kee", "fess". Phonetic
garbage scored 0.5–0.75 coverage, indistinguishable from real English. The
scorer keys on token length, fragment density and Portuguese morphology
instead. Do not reintroduce a dictionary check.

### Gate placement

The gate runs per chunk, not per ASR segment. Language identification on a
two-word fragment is unreliable; a chunk carries enough text to score and a
mean confidence over its words. Rejected chunks call `OrderedOutputBuffer.skip()`
so their sequence number retires and later lines are not held behind them.

### Confidence thresholds are mode-dependent

- `gate.min_confidence = 0.85` — single-language mode, where confidence is the
  only thing separating the room from the screen. Measured: the speaker
  0.921–0.998, English-looking Portuguese up to 0.823.
- `dual.min_confidence = 0.70` — two-way mode, where the language router does
  that job and confidence only has to catch noise.

Tuning these is expected. `tests/test_gate.py` contains guards that fail if the
threshold moves outside the measured window in either direction.

### Reading time

Lines hold the screen for `lead_in + characters / 13.5 cps`, clamped to
1.3–4.5 s. Under backlog the hold compresses toward a floor **proportional to
the line's own reading time** (`catchup_floor = 0.55`), never a flat minimum —
a flat floor squeezes a long sentence into the same glance a three-word one
gets, which is the bug this was built to fix.

---

## 6. Measured numbers

Re-measure before trusting these if the pipeline has changed.

| | value |
|---|---|
| Translation, EN→PT | 131 ms median |
| Translation, PT→EN | 125 ms median |
| Time to ready (end of phrase → translation available) | 1276 ms median, 2041 ms p90 |
| Time on screen (includes reading-time hold) | 1723 ms median |
| ASR decode cost | ~35–60 ms per 500 ms, ~7% GPU duty |
| ASR WER, synthesized fixtures | 0.045 technical, 0.000 conversational |
| Soak, 15 min continuous | no crash, RSS +3.2%, latency drift 1.43x |

The sub-1-second target from the original brief is **not met** for phrase-final
text and cannot be, given a spec-mandated 400 ms silence detection plus the ASR
stability lag. Mid-utterance text does land inside a second. This is documented
rather than hidden; do not quietly change the assertions to make it look met.

---

## 7. Testing standards

- Tests assert **behaviour**, not implementation. If a test fails because a
  contract genuinely changed, update the test and say so plainly in the report;
  never loosen a threshold silently to get green.
- Regression tests are built from **real captured data** where possible.
  `tests/test_gate.py` contains verbatim utterances from a live session at their
  measured confidences. Keep that pattern.
- Integration tests must skip cleanly when LM Studio or the weights are absent
  (`pytest.importorskip` at module import, not in a fixture — a collection
  error is not a skip).
- Never let a test write to `logs/transcripts/`.
- Beware asserting on a single fastest/slowest sample. `test_e2e.py` asserts
  latency on the median and p90; the `min` bound is deliberately loose, because
  it degrades when the rest of the suite competes for the GPU and was flaky at
  a tight threshold. Aggregate statistics are the meaningful ones.

---

## 8. Living document policy

**This policy is mandatory. Treat it as part of the definition of done.**

`AGENTS.md` is the handover document. A fresh agent session with no memory of
this conversation must be able to read it and work competently. That only holds
if it is updated as the code changes.

### You must update this document in the same change that:

1. **Adds, removes or renames a module** — update the module map (section 3).
2. **Changes a default in `config.toml`** that is quoted here — update the value
   and the reasoning. Values in this document must never contradict the config.
3. **Reverses or supersedes a decision in section 5** — do not delete the old
   reasoning. Record what changed, what the new evidence was, and why the
   earlier decision was right at the time. Reversals undone by accident are the
   single most expensive failure mode in this project.
4. **Uncovers a new landmine** — a library bug, a platform behaviour, a
   non-obvious constraint that cost more than about twenty minutes to diagnose.
   Add it to section 4 with the evidence that identified it.
5. **Changes a measured number in section 6** — re-measure and update. A stale
   number is worse than no number, because it will be trusted.
6. **Changes how the project is run or tested** — update section 2 and the
   README together.

### Rules for writing entries

- **Record evidence, not conclusions.** "v3 scores 0.000 where v2 scored 0.301"
  is useful. "v3 is better" is not.
- **Keep the why.** Anyone can read the code to see what it does. This document
  exists for what the code cannot say.
- **Delete what has become false.** This is a living document, not a changelog.
  If a landmine no longer exists because the upstream bug was fixed, remove it
  and note the version that fixed it. Superseded decisions are the exception:
  those stay, marked as superseded.
- **No speculation.** If something is untested, say it is untested and say what
  would test it.

### At the end of any substantial task, verify:

- [ ] Section 3 module map matches the files that exist.
- [ ] Every config value quoted here matches `config.toml`.
- [ ] Section 6 numbers were measured against the current code.
- [ ] Any decision reversed is recorded with both sides.
- [ ] Section 9 reflects the true current state and open questions.
- [ ] The README still describes the software as it now behaves.

If you changed code and touched nothing in this document, be suspicious. That
is common and legitimate for a narrow bug fix, and rare for anything else.

---

## 9. Current state

Working and verified in live use: the full pipeline in both modes, two-way
language routing, reading-time queueing, transcripts, diagnostics.

### Known limitations

- **The Portuguese direction has never been tested with a real speaker.** All
  Portuguese validation used macOS `say` voices. Routing was clean on 400 real
  English lines, so the speaker's own words are safe, but how the system
  handles a Brazilian speaker at conversational distance through the built-in
  microphone array is genuinely unmeasured. This is the most important open
  question in the project.
- Very short Portuguese fragments ("sim", "tá") lack enough evidence to route
  and are dropped rather than translated.
- A sentence genuinely mixing both languages goes whichever way the scores
  fall. There is no per-word routing.
- Side conversations in Portuguese are translated too. `dual.enabled = false`
  restores the old behaviour; the hold key stops everything.
- Tier 3 of the speaker gate (voice-embedding identification) was never built.
  Every viable local model pulls in PyTorch, which conflicts with the power
  constraint. The seam is left in place: `Gate` accepts a `speaker_verifier`
  and `config.toml` has a disabled `[gate.speaker]` section.
- Latency drifted 1.43x across a 15-minute soak. Memory was flat and the ASR
  window and decode cost were both flat, so it is not recognition; most likely
  sustained-load thermal behaviour. Unquantified over a full 90-minute session.

### If you are picking this up cold

1. Run `--diagnose`. It tests each stage in the order it can fail.
2. Read `docs/REPORT.md` for the full history, including measurements and
   everything that was tried and rejected.
3. Read `livetranslate/asr.py`'s module docstring before touching recognition.
4. Check `logs/livetranslate.log` — it is DEBUG level and has a HEARTBEAT line
   every 15 s summarising the whole pipeline.
