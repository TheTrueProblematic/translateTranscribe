# LiveTranslate

Live Brazilian Portuguese captions of spoken English, for an audience to read
while I present. Everything runs on the laptop: local speech recognition, local
translation model, local display. Nothing leaves the machine once the models are
in place.

When someone in the room answers in Portuguese, that gets translated back into
English and shown in blue, so I can read the reply without losing the thread.

---

Runs on macOS and Windows 11.

## Requirements

**macOS**

- Apple Silicon, current release
- Python 3.10 or newer (arm64 — the Intel build cannot run MLX)
- LM Studio serving `hunyuan-mt2-1.8b-mlx`

**Windows 11**

- Python 3.10 or newer from python.org (tick "Add python.exe to PATH")
- LM Studio serving `Hunyuan-MT-7B`. There is no GGUF build of the Mac's
  `hunyuan-mt2-1.8b-mlx` — MLX is Apple-only — so Windows uses the 7B model.
  In LM Studio, search for **Hunyuan-MT-7B** and take the **Q4_K_M**
  quantisation, about 4.6 GB.
- A CUDA GPU is optional and makes recognition several times faster

LM Studio can run on a different machine:

```
LiveTranslate.bat --lmstudio 192.168.1.50
```

A bare host, `host:port`, or a full URL all work. On macOS pass the same flag,
or set `lmstudio.base_url` in the config.

Microphone permission is required. Without it macOS never returns from opening
the input stream rather than failing, so the app would look healthy while
hearing nothing. It detects this after six seconds and says so, and the status
dot turns red. Grant it under System Settings → Privacy & Security →
Microphone.

---

## Running it

### macOS

Double-click `LiveTranslate.command`, or from a terminal:

```bash
./LiveTranslate.command
```

### Windows

Double-click `LiveTranslate.bat`. This is the main way it runs on Windows: an
always-on-top strip of subtitles that floats over whatever is on screen, so it
works over a presentation, a video call, or the software being demonstrated.

| | |
|---|---|
| `LiveTranslate.bat` | Subtitle overlay |
| `ARSLiveTranslate.bat` | The same, with the ARS vocabulary |
| `LiveTranslateBrowser.bat` | Full-screen browser display, as on the Mac |
| `Diagnose.bat` | Self-test, run this first when something is wrong |

System-wide hotkeys, which work whichever application has focus:

| Key | What it does |
|---|---|
| `Ctrl+Alt+S` | Show and hide the subtitles |
| `Ctrl+Alt+T` | Move them between the bottom and the top of the screen |
| `Ctrl+Alt+P` | Pause and resume recognition |

White text on a partly transparent black band, never more than two lines. A
long sentence shrinks to fit before anything is dropped. Room speech translated
back into English appears in blue, as it does on the full-screen display.

First run creates a virtualenv and installs dependencies, which takes a few
minutes, and downloads the speech model. Later runs go straight to starting. Startup checks that LM Studio is
reachable and loads the translation model before anything else, so the first
sentence does not pay for it.

### ARS training mode

```bash
./ARSLiveTranslate.command
```

Same application with the ARS session vocabulary loaded: Max, SHOTOVER Systems,
ARS, ATOM, M2, FLIR, PilotDisplay, Earthscape, IMU, gimbal, aircraft, AR.

It corrects the terms the recogniser reliably mangles — "hair craft" becomes
aircraft, "emu" becomes IMU, "jimbal" and "jumble" become gimbal, "adam"
becomes ATOM, "shot over" becomes SHOTOVER — and tells the translation model to
keep every product name in English. Single letters stay single letters, so
dictating keyboard shortcuts still works.

Those corrections are deterministic string rules rather than a request to the
model, so they cannot fail intermittently. The prompt glossary is a second
layer for mishearings no rule anticipated.

---

## Using it in the room

| Key | What it does |
|---|---|
| `Space` or `P` | Hold and resume. Holding raises a full-width PAUSADO banner |
| `E` | Hide or show the English monitor line |
| `+` and `-` | Type size up and down, remembered between sessions |
| `0` | Reset type size |
| `F` | Fullscreen |

All shortcuts work on the display window. There is no global hotkey to set up.

**Status dot:** green listening, amber held, blue translating, red
disconnected. The bar beside it is the microphone level.

**English monitor.** The small line along the bottom shows what the recogniser
is hearing, with the not-yet-committed tail in italic. When a phrase is dropped
it says so inline, with the reason and the scores:

```
dropped (low_confidence, conf 0.74, en 0.61)
```

That is there so "is it deaf, or is it ignoring me?" is answerable at a glance.
It stays 13px on a projector, because it is read from the laptop rather than
from the room.

**Backlog bar.** The thin strip along the very bottom edge grows as lines queue
up waiting for their reading time. Blue is fine, amber means it is lagging, red
means ease off and let it catch up.

**Reading time.** Each line holds the screen long enough to be read — roughly
1.3 seconds for a few words, up to 4.5 for a full sentence. Talking faster than
that does not drop anything; later lines queue instead. The hold shortens as
the queue builds so it recovers on its own, but never far enough to make a long
sentence unreadable.

---

## Two-way mode

The recogniser is multilingual, so the room is handled properly rather than
guessed at.

- I speak English, Portuguese appears in white.
- Someone answers in Portuguese, English appears in blue.
- Anything that is clearly neither language is dropped rather than guessed at.

Set `dual.enabled = false` in `config.toml` for English only, with everything
else discarded. If you do that, switch `asr.model` back to
`parakeet-tdt-0.6b-v2` — in single-language mode, a recogniser that cannot
speak Portuguese is an advantage.

---

## Configuration

Everything tunable lives in `config.toml`, commented. The values worth touching
between sessions:

| Setting | Effect |
|---|---|
| `dual.min_confidence` | Lower it if my own words go missing; raise it if noise reaches the screen |
| `gate.min_english_score` | How English something must read to be treated as mine |
| `chunker.silence_ms` | Shorter means snappier updates and more mid-sentence cuts |
| `display.reading_cps` | Reading speed the hold time is calculated from |
| `display.min_dwell_ms` | Shortest a line may stay on screen |

The configs inherit rather than duplicate, so tuning the base tunes everything:

```
config.toml              shared settings, and the macOS build
config.ars.toml          ARS vocabulary only
config.windows.toml      Windows: recogniser, overlay, reading speed
config.windows.ars.toml  both of the above
```
 To teach it a new term,
add to `[normalizer.corrections]` for a whole word heard wrong, or
`[normalizer.compounds]` for one split in two.

---

## Transcripts

Every run writes both languages to `logs/transcripts/`:

- `transcript-<timestamp>.txt` — readable, English above its Portuguese
- `transcript-<timestamp>.jsonl` — timings, confidences, language scores and
  latency, for reviewing a session afterwards

Dropped phrases are recorded too, with the reason and scores, since that is the
material worth tuning against.

```
[06:19:03] #4
  EN  I am ready to start the calibration now.
  PT  Estou pronto para iniciar a calibração agora.

[06:19:13] #5  (dropped: low_confidence, conf 0.82, en 0.00)
  EN  Naughtoquines connector, henda esta energizado, and you verify the firmware.
```

---

## When something is wrong

```bash
.venv/bin/python -m livetranslate --diagnose
```

Checks each stage in the order it can fail: architecture, imports, the
microphone (records three seconds and reports the level), the ASR model
(transcribes what it just recorded and compares the confidence against the
gate), LM Studio, and the gate settings. It reports which stage failed and what
to change.

`logs/livetranslate.log` records every decode, word, chunk, gate decision and
translation, plus a summary line every fifteen seconds:

```
HEARTBEAT audio=30s blocks=187 speech=186 peak_rms=0.184 | decodes=46 avg=65ms
words=70 | chunks=6 accepted=6 rejected=0 translated=5 errors=0 | paused=False
```

If nothing is appearing on screen, that line says which stage is silent.

---

## Tests

```bash
.venv/bin/python -m pytest -q -m "not slow"                      # 335 tests
.venv/bin/python -m pytest -q -m "not slow and not integration"  # no LM Studio needed
.venv/bin/python -m pytest tests/test_soak.py -m slow -s         # 15 minute soak
```

Integration tests need LM Studio and the recognition weights, and skip cleanly
without them.

---

## Layout

```
livetranslate/
  __main__.py      startup, preflight, heartbeat
  asr.py           microphone capture and speech recognition
  chunker.py       groups words into translatable chunks
  normalizer.py    acronyms, compound words, clock times, disfluencies
  langid.py        English and Portuguese scoring, language routing
  gate.py          what reaches the screen, and in which direction
  translator.py    LM Studio client, both directions
  postprocess.py   capitalisation, punctuation, known model defects
  prompt.py        system prompts for both translation directions
  output_buffer.py keeps the display in sequence order
  pipeline.py      orchestration, reading time, backlog
  server.py        static page and websocket
  transcript.py    per-session records
  diagnose.py      the self-test
  logging_setup.py rolling debug log
  config.py        settings loading, including config inheritance
  overlay.py       the always-on-top subtitle window (Windows)
  overlay_app.py   runs the pipeline behind the overlay
  asr_whisper.py   speech recognition on Windows (faster-whisper)
  asr_backend.py   picks the recogniser for the platform
  hotkeys_win.py   system-wide hotkeys on Windows
  static/          the browser display surface

config.toml        all settings
config.ars.toml    ARS vocabulary, inherits the above
docs/REPORT.md     build history, measurements, what was tried and rejected
AGENTS.md          context for anyone (or anything) picking this up
```
