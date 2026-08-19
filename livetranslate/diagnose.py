"""Self-test: `python -m livetranslate --diagnose`.

Checks each stage in the order it can fail and prints a verdict per stage, so a
silent session is diagnosed in one command instead of by guesswork. Every check
is designed to fail loudly and specifically rather than hang.
"""
from __future__ import annotations

import asyncio
import sys
import time

from .config import Config

OK, BAD, WARN = "  OK  ", " FAIL ", " WARN "


def _line(status: str, name: str, detail: str = "") -> None:
    print(f"[{status}] {name}" + (f"\n         {detail}" if detail else ""), flush=True)


async def run_diagnostics(config_path: str | None = None) -> int:
    print("\nLiveTranslate diagnostics\n" + "-" * 58, flush=True)
    failures = 0

    # ---- config ----
    try:
        cfg = Config.load(config_path)
        _line(OK, "config.toml", str(cfg.path))
    except Exception as exc:
        _line(BAD, "config.toml", str(exc))
        return 1

    # ---- python / arch ----
    import platform
    arch = platform.machine()
    if arch != "arm64":
        _line(BAD, "architecture", f"{arch}: MLX needs arm64 Python")
        failures += 1
    else:
        _line(OK, "architecture", f"{arch}, Python {sys.version.split()[0]}")

    # ---- imports ----
    for mod, hint in (("mlx.core", "pip install mlx"),
                      ("parakeet_mlx", "pip install parakeet-mlx"),
                      ("sounddevice", "pip install sounddevice"),
                      ("aiohttp", "pip install aiohttp")):
        try:
            __import__(mod)
            _line(OK, f"import {mod}")
        except Exception as exc:
            _line(BAD, f"import {mod}", f"{exc}  ({hint})")
            failures += 1

    # ---- microphone ----
    mic_audio = None
    try:
        import numpy as np
        import sounddevice as sd

        dev = sd.query_devices(kind="input")
        _line(OK, "input device", f"{dev['name']} ({dev['max_input_channels']} ch)")

        # Opening the stream is done in a thread with a timeout: without
        # Microphone permission macOS never returns from it rather than failing.
        captured: list = []
        opened = asyncio.Event()
        loop = asyncio.get_running_loop()

        def record():
            def cb(indata, frames, t, status):
                captured.append(indata[:, 0].copy())
            with sd.InputStream(samplerate=int(cfg.get("asr.sample_rate", 16000)),
                                blocksize=int(cfg.get("asr.sample_rate", 16000) * 0.16),
                                channels=1, dtype="float32", callback=cb):
                loop.call_soon_threadsafe(opened.set)
                time.sleep(3.0)

        task = asyncio.get_running_loop().run_in_executor(None, record)
        try:
            await asyncio.wait_for(opened.wait(), timeout=8.0)
            await asyncio.wait_for(asyncio.shield(task), timeout=12.0)
            mic_audio = np.concatenate(captured) if captured else None
            if mic_audio is None or not len(mic_audio):
                _line(BAD, "microphone capture", "stream opened but delivered no audio")
                failures += 1
            else:
                rms = float(np.sqrt(np.mean(mic_audio ** 2)))
                peak = float(np.max(np.abs(mic_audio)))
                thr = float(cfg.get("vad.energy_threshold", 0.012))
                detail = f"3.0s captured, rms={rms:.4f} peak={peak:.4f} (VAD threshold {thr})"
                if rms < 0.0005:
                    _line(WARN, "microphone capture",
                          detail + "\n         Essentially silent. Speak during the test, "
                                   "check the input device and that it is not muted.")
                else:
                    _line(OK, "microphone capture", detail)
        except asyncio.TimeoutError:
            _line(BAD, "microphone capture",
                  "Opening the microphone did not return. macOS is waiting on "
                  "Microphone permission.\n         Grant it in System Settings > "
                  "Privacy & Security > Microphone, then run this again.")
            failures += 1
    except Exception as exc:
        _line(BAD, "microphone", f"{type(exc).__name__}: {exc}")
        failures += 1

    # ---- ASR ----
    try:
        import numpy as np
        from .asr import ParakeetASR

        probe = ParakeetASR(cfg, asyncio.get_running_loop(), lambda w: None, lambda t, s: None)
        source = probe._resolve_model_source()
        t0 = time.perf_counter()
        from parakeet_mlx import from_pretrained
        model = from_pretrained(source)
        _line(OK, "ASR model", f"{source} loaded in {time.perf_counter() - t0:.1f}s")

        if mic_audio is not None and len(mic_audio) > 16000:
            import mlx.core as mx
            from parakeet_mlx.audio import get_logmel
            t0 = time.perf_counter()
            res = model.generate(get_logmel(mx.array(mic_audio), model.preprocessor_config))[0]
            cost = (time.perf_counter() - t0) * 1000
            text = res.text.strip()
            confs = [float(t.confidence) for t in res.tokens]
            if text:
                mean_conf = sum(confs) / len(confs)
                gate_min = float(cfg.get("gate.min_confidence", 0.96))
                detail = (f"{cost:.0f}ms -> {text!r}\n         mean token confidence "
                          f"{mean_conf:.3f} (gate needs {gate_min})")
                if mean_conf < gate_min:
                    _line(WARN, "ASR on your voice",
                          detail + "\n         This would be REJECTED. Lower "
                                   "gate.min_confidence in config.toml.")
                else:
                    _line(OK, "ASR on your voice", detail)
            else:
                _line(WARN, "ASR on your voice",
                      f"{cost:.0f}ms but produced no words -- was anyone speaking?")
    except Exception as exc:
        _line(BAD, "ASR", f"{type(exc).__name__}: {exc}")
        failures += 1

    # ---- LM Studio ----
    try:
        from .translator import LMStudioUnavailable, Translator

        tr = Translator(cfg)
        try:
            await tr.preflight()
            _line(OK, "LM Studio reachable", f"{tr.base_url}, model {tr.model} listed")
            t0 = time.perf_counter()
            out = await tr.translate("do not touch that connector it is still live")
            elapsed = (time.perf_counter() - t0) * 1000
            if out:
                _line(OK, "translation", f"{elapsed:.0f}ms -> {out!r}")
            else:
                _line(BAD, "translation", "model returned an empty string")
                failures += 1
        except LMStudioUnavailable as exc:
            _line(BAD, "LM Studio", str(exc))
            failures += 1
        finally:
            await tr.close()
    except Exception as exc:
        _line(BAD, "LM Studio", f"{type(exc).__name__}: {exc}")
        failures += 1

    # ---- gate configuration ----
    paused = bool(cfg.get("hotkey.start_paused", False))
    if paused:
        _line(WARN, "gate", "hotkey.start_paused is TRUE: nothing will be displayed "
                            "until you resume.")
    else:
        _line(OK, "gate", f"starts listening; min_confidence="
                          f"{cfg.get('gate.min_confidence')}, "
                          f"min_english_score={cfg.get('gate.min_english_score')}")

    print("-" * 58)
    print(("All checks passed." if failures == 0
           else f"{failures} check(s) failed -- see above."), flush=True)
    return 1 if failures else 0
