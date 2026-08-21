# AGENTS.md

Operating guide for any AI agent working on LiveTranslate. Read this fully
before changing anything. It is the single source of truth for context that is
not obvious from the code, and it is a **living document** — see the policy at
the end, which is mandatory.

---

## 1. What this is

A local live-captioning system, on macOS and Windows 11. Max, a presenter at SHOTOVER Systems, speaks
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

macOS:

```bash
./LiveTranslate.command                     # normal mode
./ARSLiveTranslate.command                  # ARS training mode (adds vocabulary)
.venv/bin/python -m livetranslate --diagnose   # self-test each stage
```

Windows 11:

```bat
LiveTranslate.bat            always-on-top subtitle overlay (the main way)
ARSLiveTranslate.bat         the same, with the ARS vocabulary
LiveTranslateBrowser.bat     full-screen browser display, as on the Mac
Diagnose.bat                 self-test each stage
```

LM Studio elsewhere on the network: `LiveTranslate.bat --lmstudio 192.168.1.50`
(bare host, host:port, or a full URL).

Requires LM Studio running on `http://localhost:1234` with
`hunyuan-mt2-1.8b-mlx` available. First run creates `.venv` and installs
dependencies; later runs skip that.

macOS Microphone permission is required. Without it, opening the input stream
**never returns** rather than failing — a watchdog reports this after 6s.

### Tests

```bash
.venv/bin/python -m pytest -q -m "not slow"              # 335 tests, ~2 min
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
| `asr.py` | macOS only: mic capture, rolling decode, word commitment. The most subtle file. |
| `asr_whisper.py` | Windows and everything else: faster-whisper, utterance decoding. |
| `asr_backend.py` | Chooses between the two. `auto` follows the platform. |
| `overlay.py` | The always-on-top subtitle window (tkinter). |
| `overlay_app.py` | Runs the pipeline behind the overlay: asyncio thread plus tkinter main thread. |
| `hotkeys_win.py` | System-wide hotkeys on Windows, via ctypes and RegisterHotKey. |
| `topmost_win.py` | Windows only: keeps the overlay above a full-screen application. |
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

### 4.6 Whisper pads everything to thirty seconds

faster-whisper decodes a 1.5 second clip and an 8 second clip in about the same
time (measured: 1490ms versus 1681ms), because Whisper pads every input to
thirty seconds internally.

This makes the macOS rolling re-decode strategy **impossible** on the Windows
backend: re-decoding a short window several times a second costs more than the
interval it runs on, and the backlog grows without bound. The first attempt did
exactly this and never kept up.

`asr_whisper.py` therefore decodes **once per utterance**, cut on silence by
the VAD, and never decodes silence at all. Words are final when they arrive, so
there is no local agreement step and no stability lag. If you are tempted to
unify the two backends onto one strategy, this is why they differ.

### 4.7 Batch files need CRLF, and cmd.exe expands blocks early

`.bat` files are stored with CRLF (`.gitattributes` enforces `*.bat text
eol=crlf`). LF-only batch files misparse `goto` labels on some Windows builds.

`scripts/bootstrap.bat` is written with `goto` rather than parenthesised
`if` blocks on purpose: cmd.exe expands `%VAR%` for an entire block when it
parses it, so a variable set by a subroutine inside a block reads as empty on
the next line of that same block. The first version had exactly that bug.

### 4.8 One Tk root per process

Creating and destroying several `tk.Tk()` roots in one process segfaults Tk on
macOS and is discouraged everywhere. `tests/test_overlay.py` uses a single
module-scoped root and resets its state between tests. Do not convert that
fixture to function scope.

### 4.9 Always-on-top is not one flag, on Windows

Observed on the target machine: the overlay floated correctly over windowed
applications and sat *behind* an application running full screen (ARS). The
cause is not the overlay's styles. Windows raises a foreground full-screen
window above the topmost band, and full-screen applications commonly assert
topmost themselves, in which case whichever window was raised last wins. Tk's
`wm attributes -topmost` asserts the position exactly once, at creation, so it
always loses that race.

`topmost_win.py` re-raises the window with `SetWindowPos(HWND_TOPMOST)` on a
timer (`overlay.topmost_interval_ms`, default 250). **Re-raising is the fix.
Asserting once, however it is asserted, is not.** Three details in that file
are easy to get wrong and fail silently:

- **Never set `WS_EX_TOPMOST` through `SetWindowLong`.** It sets the bit
  without moving the window between Z-order bands, so the window reports
  itself as topmost and still renders behind. `SetWindowPos` is the only way
  in. A test asserts the constant does not appear in `harden()`.
- **Tk can destroy and recreate a toplevel's wrapper HWND** when an attribute
  changes, which silently drops styles applied to the old handle. The handle
  is re-read every tick and restyled whenever it differs.
- **`winfo_id()` is not the window the window manager orders.** It is the
  inner Tk window; the wrapper is its root ancestor, so it is walked up with
  `GetAncestor(GA_ROOT)`. `wm frame` reports the wrapper directly but only as
  a string whose base differs between builds.

The one case none of this wins: an application in **exclusive** full screen (a
Direct3D swap chain flipped straight to the display) is not composited by the
desktop window manager, so no other process can draw over it at all. That is a
platform fact, not a bug to be fixed here. `exclusive_fullscreen()` detects it
through `SHQueryUserNotificationState` and both the log and `--diagnose` name
it, so it is not rediscovered as a mystery. The remedy is outside this
process: run the presented application in borderless or windowed full screen.

Measured on the target machine after the fix, on the live window: topmost band
entered, `WS_EX_NOACTIVATE`, `WS_EX_TRANSPARENT`, `WS_EX_TOOLWINDOW` and
`WS_EX_LAYERED` all set, tick firing on interval, and the strip visible in a
screenshot over a full-screen topmost window that had taken the foreground.

The interval was chosen from measurement, not taste. A full-screen topmost
window was made to claim the front, and the real Z-order sampled 20 times a
second for 6 seconds; the figure is the share of that time the strip was
genuinely in front:

| interval | app claims the front once | app claims it 4x a second |
|---|---|---|
| 1000 ms | 100% | 14% |
| 250 ms | 100% | 63% |
| 100 ms | - | 78% |

Claiming once is what an application does when it goes full screen or is
switched to, and any interval fixes that. **No interval wins outright against
an application that re-claims the front continuously.** That is a race, and
tuning the number is not going to end it — do not read a stray report of the
strip flickering behind something as a bug to be fixed by a smaller interval.

### 4.10 Config layering

`config.ars.toml` declares `extends = "config.toml"` and overrides only the
session vocabulary. Tables merge key by key. Do not fork the config into two
copies — the whole point is that tuning the base tunes both modes, and a test
asserts the shared settings stay identical.

---

## 5. Decisions and why

Where a decision was reversed, both sides are recorded. Reversals are the
easiest thing for a new agent to undo by accident.

### Two ASR backends, and why they behave differently

MLX runs only on Apple Silicon, so Windows cannot use parakeet-mlx at all. The
second backend is faster-whisper (CTranslate2), which has Windows wheels and
runs on CPU or CUDA.

They are not interchangeable in behaviour:

| | parakeet-mlx (macOS) | faster-whisper (Windows) |
|---|---|---|
| strategy | rolling re-decode, local agreement | one decode per utterance |
| text appears | while the phrase is still being said | after the speaker pauses |
| reports language | no | yes, and the router prefers it |
| decode cost | ~35-60 ms | ~440 ms (base), ~1530 ms (small) |
| ready latency | ~1.3 s | ~3 s on CPU |

`asr.backend = "auto"` picks by platform, so one config works on both.

### Whisper identifies the language, but not infallibly

Whisper reports the spoken language, which is better evidence than scoring the
spelling of a transcript, so the gate prefers it. But on a heavily accented
Portuguese clip it reported "en" with probability 0.95 while the text scored
en=0.00 pt=0.68.

So the label wins by default and loses only to a decisive disagreement from the
text (`dual.text_override_min` / `text_override_max`). Every disagreement is
logged. Neither signal alone was good enough.

### Windows uses a different translation model, of necessity

There is no GGUF build of Hunyuan-MT2 1.8B: it exists only in MLX, which is
Apple-only. Windows uses `Hunyuan-MT-7B` (GGUF, Q4_K_M) instead. The prompt and
its worked examples were tuned against MT2; they carry over, but if Windows
output drifts in style that is the first thing to suspect.

`Translator.preflight()` accepts a single unambiguous near-match on the model
id, because LM Studio names the MLX and GGUF builds differently and a naming
mismatch is a bad reason to fail to start.

### The overlay cannot be focused or clicked (Windows only)

`topmost_win.harden()` sets `WS_EX_NOACTIVATE` and, unless
`overlay.click_through` is turned off, `WS_EX_TRANSPARENT`. Both exist because
of what the overlay sits over.

NOACTIVATE means the window can never take keyboard focus. That is a
requirement, not a nicety: an application in full screen that loses focus
typically drops out of full screen or minimises itself, so an overlay that
could be activated would sabotage the very thing it is captioning — worse than
no overlay at all. It is also why re-raising can be done every second without
disturbing anything: `SWP_NOACTIVATE` changes Z-order and nothing else.

TRANSPARENT means mouse clicks pass through to whatever is underneath. A strip
across the bottom of the screen that swallowed clicks meant for the application
below it would be a hazard during a live session.

The cost is that the overlay's own key bindings (`space`, `t`, `h`) are
unreachable on Windows, since they need a click to focus the window. They were
already documented as a fallback; the global hotkeys are the real mechanism and
they work regardless of focus, which is the whole point of `hotkeys_win.py`.
`overlay.click_through = false` restores clickability for anyone who wants it.

### Owning the presented window was considered and rejected (Windows only)

A timer that re-raises the overlay cannot beat an application that re-claims
the front continuously (landmine 4.9 has the numbers). Win32 offers a way that
would: an *owned* window is always above its owner, permanently and without
polling, so setting the overlay's owner to the full-screen application's window
would win the Z-order outright.

Rejected. It couples this process's window to another process's window, and
every failure mode lands during a live session: an owned window is hidden when
its owner minimises, so the subtitles would vanish rather than fall behind;
cross-process ownership attaches input queues, so an application that hangs can
take the overlay's UI thread with it; and the owner has to be re-assigned every
time the presenter switches application. Polling is coupled to nothing,
self-heals within one interval, and fails visibly rather than silently.

The alternative that actually removes the race is not available to us either:
raising the window into a higher Z-order band with `SetWindowBand` requires a
UIAccess manifest and installation under Program Files.

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

### Language routing uses whatever evidence exists

On macOS it is text-based, because parakeet-mlx cannot report a language. On
Windows the recogniser's own label is preferred, overruled only by a decisive
text disagreement. Both paths end at `detect_language()` in `langid.py`.

### Text-based routing

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

Lines hold the screen for `lead_in + characters / reading_cps`, clamped. The
speeds were raised after real use: 13.5 cps with a 4.5 s cap held lines longer
than anyone needed and pushed everything behind them further out of step with
the speaker.

| | reading_cps | min | max |
|---|---|---|---|
| full-screen display | 18.0 | 1.0 s | 3.2 s |
| Windows overlay | 22.0 | 0.8 s | 2.4 s |

The overlay is faster because its type is far smaller, so two lines hold about
a third more text, and because its recogniser is slower per utterance: the hold
is where that time has to be given back.

Under backlog the hold compresses toward a floor **proportional to
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

Windows, measured on this Mac's CPU as a stand-in (a Windows laptop will
differ, and CUDA changes it entirely):

| | value |
|---|---|
| Whisper decode, per utterance | 440 ms (base), 1533 ms (small) |
| Whisper WER, English fixture | 0.045 (base), 0.023 (small) |
| Ready latency, full pipeline | ~3 s median on CPU |
| Real-time factor, full pipeline | keeps up: 33 s wall for 31 s of audio |

Measured on the target Windows 11 machine (2880x1800 at 200%, Python 3.14.5,
whisper base on CPU):

| | value |
|---|---|
| Whisper 'base' load | 1.0 s, int8 on CPU |
| Overlay in front of a full-screen window that claimed the front once | 100% of samples |
| Overlay in front of one re-claiming it 4x a second | 63% at a 250 ms interval, 14% at 1000 ms |

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
7. **Changes behaviour on one platform only** — say which. A reader on the
   other platform must not act on it. Every claim here that is true of only
   one of macOS or Windows says so explicitly.

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

- **Windows is now running on Windows, but not yet through a live session.**
  The pipeline, the overlay and the `.bat` launchers have all been exercised on
  a Windows 11 machine. Always-on-top was wrong there and is fixed: see
  landmine 4.9. Verified on the real window manager — the strip holds over
  windowed applications and over a full-screen window. Still unverified on the
  target platform: `RegisterHotKey` under a real presentation, and behaviour
  over a genuinely *exclusive* full-screen Direct3D application, which cannot
  work and is instead detected and reported.
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

1. Run `--diagnose` (or `Diagnose.bat`). It tests each stage in the order it
   can fail, and reports which backend it chose.
2. Read `docs/REPORT.md` for the full history, including measurements and
   everything that was tried and rejected.
3. Read `livetranslate/asr.py`'s module docstring before touching recognition.
4. Check `logs/livetranslate.log` — it is DEBUG level and has a HEARTBEAT line
   every 15 s summarising the whole pipeline.
