# LiveTranslate

Live Brazilian Portuguese captions of spoken English, for an audience to read
while you talk. Everything runs on this machine: local ASR, local translation
model, local display. No cloud APIs, no API keys, no network calls off the box
once the models are in place.

## Running it

Double-click **`LiveTranslate.command`**, or from a terminal:

```bash
./LiveTranslate.command
```

### ARS training mode

Double-click **`ARSLiveTranslate.command`** instead. Same app, plus the session
vocabulary: Max, SHOTOVER Systems, ARS, ATOM, M2, FLIR, PilotDisplay,
Earthscape, IMU, gimbal, aircraft, AR.

It fixes the terms the recogniser reliably mangles — "hair craft" → aircraft,
"emu" → IMU, "jimbal"/"jumble" → gimbal, "adam" → ATOM, "shot over" → SHOTOVER
— and tells the translation model to keep every product name in English. Single
letters stay single letters, so dictating keyboard shortcuts ("press I", "press
V") survives.

The corrections are deterministic string rules, not a request to the model, so
they cannot fail intermittently. The prompt glossary is the second layer, for
mishearings no rule anticipated.

`config.ars.toml` holds only the vocabulary and `extends = "config.toml"` for
everything else, so tuning the base config tunes both modes. Add a term by
editing `[normalizer.corrections]` (a whole word heard wrong) or
`[normalizer.compounds]` (a word split in two).

First run creates `.venv` and installs dependencies. Later runs skip straight
to starting. Startup verifies LM Studio is reachable and that the configured
model is loaded, and fails with a plain-language message if not.

LM Studio must be running with its local server on `http://localhost:1234` and
the model `hunyuan-mt2-1.8b-mlx` available (JIT loading is fine).

### One macOS permission

**Microphone.** Without it macOS never returns from opening the input stream,
so the app would look fine while hearing nothing. It detects this after 6
seconds and says so; the display shows a red dot. Grant it under System
Settings → Privacy & Security → Microphone.

### If something looks wrong

```bash
.venv/bin/python -m livetranslate --diagnose
```

Checks architecture, imports, the microphone (records 3 seconds and reports the
level), the ASR model (transcribes what it just recorded and compares its
confidence against the gate), LM Studio, and the gate settings — then tells you
which stage failed and what to change.

`logs/livetranslate.log` records every decode, word, chunk, gate decision and
translation, plus a HEARTBEAT line every 15 seconds summarising the whole
pipeline. If nothing is appearing on screen, that line says which stage is
silent.

## Using it in the room

It starts listening immediately. There is no global hotkey to set up.

| Key (on the display window) | What it does |
|---|---|
| `Space` or `P` | Hold / resume. Holding raises a full-width **PAUSADO** banner |
| `E` | Hide / show the English monitor strip |
| `+` / `-` | Type size up / down, remembered |
| `0` | Reset type size |
| `F` | Toggle fullscreen |

Each line stays on screen long enough to be read — roughly 1.3s for a few
words up to 4.5s for a full sentence. If you outrun that, later lines **queue**
rather than flashing past, and the thin bar along the very bottom grows to show
how far behind the display is: blue is fine, amber means it is lagging, red
means pause and let it catch up. It compresses the hold as the queue builds so
it recovers on its own, but never so far that a long sentence becomes
unreadable.

The small English line along the **bottom** is the speaker's monitor — what the
recogniser is hearing, live, with the not-yet-committed tail in italic and a
note whenever a phrase was dropped and why (`dropped (low_confidence, conf
0.74, en 0.61)`). It is 13px and stays 13px on a projector, because it is read
from the laptop, not from the room. Press `E` to hide it.

The status dot: **green** listening, **amber** held, **blue** translating,
**red** disconnected. The bar beside it is the mic level. There is deliberately
no English text on the display — the audience sees Portuguese only.

**When someone else speaks Portuguese, hit the hold key.** The automatic gate
catches most of it, but the hold is the guarantee.

## Configuration

Everything tunable is in `config.toml`, commented. The values most worth
touching mid-session are under `[gate]`:

- `min_confidence` — raise it to be stricter about noisy input
- `min_english_score` — raise it if Portuguese starts leaking onto the screen

Chunking thresholds, the model id, fonts, the hotkey binding, and the
vocabulary and normalizer rules all live there too. The acronym, compound-word
and truncation lists are meant to be extended.

## Transcripts

Every run writes both languages to `logs/transcripts/`:

- `transcript-<timestamp>.txt` — readable, English above its Portuguese
- `transcript-<timestamp>.jsonl` — one record per line with timings,
  confidences, English-ness scores and latency, for scoring a session later

Dropped phrases are included too, marked with the reason and scores — that is
the material you need to tune the gate. Both files are flushed line by line, so
quitting hard loses nothing.

```
[06:19:03] #4
  EN  I am ready to start the calibration now.
  PT  Estou pronto para iniciar a calibração agora.

[06:19:13] #5  (dropped: low_confidence, conf 0.82, en 0.00)
  EN  Naughtoquines connector, henda esta energizado, and you verify the firmware.
```

## What gets logged

`logs/livetranslate.log` (rolling, 5 MB × 4) holds the English transcript,
every gated rejection with its scores, latency samples, and any
feminine-agreement or enclitic leak from the translation model. None of this
ever appears on the display.

## Tests

```bash
.venv/bin/python -m pytest -q                          # unit tests, no models needed
.venv/bin/python -m pytest -m integration -s            # needs LM Studio + ASR
.venv/bin/python -m pytest tests/test_soak.py -m slow -s  # 15 minute soak
```

Screenshots for visual verification:

```bash
.venv/bin/python scripts/preview.py
```

## Layout

```
livetranslate/
  __main__.py      startup, preflight, wiring
  asr.py           streaming Parakeet ASR (English-only v2) from the built-in mic
  chunker.py       silence / word-count / elapsed segmentation
  normalizer.py    acronyms, compounds, clock times, disfluencies
  langid.py        English-vs-not scoring for the gate
  gate.py          tiers 2-4 of the speaker/language gate
  translator.py    LM Studio client, streaming, one in flight
  postprocess.py   capitalization, punctuation, known model defects
  output_buffer.py strict sequence-ordered rendering
  pipeline.py      orchestration
  server.py        static page + websocket
  static/          the display surface
```
