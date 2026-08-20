"""Chooses the speech recognition backend.

There are two, because there has to be: parakeet-mlx runs only on Apple
Silicon, so Windows and everything else needs a different recogniser entirely.
Both expose the same interface, so nothing downstream changes.

  parakeet-mlx     Apple Silicon only. Faster, lower power, and the one the
                   macOS timings in docs/REPORT.md were measured against.
  faster-whisper   Windows, Intel Macs, Linux. Runs on CPU or CUDA, and
                   identifies the spoken language itself, which the dual
                   language router uses in preference to scoring the text.

`asr.backend = "auto"` picks parakeet on Apple Silicon and faster-whisper
elsewhere, so one config works on both platforms.
"""
from __future__ import annotations

import logging
import platform
import sys

log = logging.getLogger("livetranslate.asr")


def is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def resolve_backend(name: str = "auto") -> str:
    """Return "parakeet-mlx" or "faster-whisper" for a configured value."""
    name = (name or "auto").strip().lower()
    if name in ("parakeet", "parakeet-mlx", "mlx"):
        return "parakeet-mlx"
    if name in ("whisper", "faster-whisper", "fasterwhisper"):
        return "faster-whisper"
    if name != "auto":
        log.warning("unknown asr.backend %r; falling back to auto", name)
    return "parakeet-mlx" if is_apple_silicon() else "faster-whisper"


def describe_platform() -> str:
    return f"{platform.system()} {platform.release()} ({platform.machine()})"


def create_asr(cfg, loop, on_word, on_tick, on_level=None, on_state=None,
               on_epoch=None, on_partial=None):
    """Build the recogniser for this machine.

    Import failures are turned into a message that names the platform and says
    what to install, rather than a bare ImportError from deep inside a thread.
    """
    backend = resolve_backend(cfg.get("asr.backend", "auto"))
    log.info("ASR backend: %s on %s", backend, describe_platform())

    if backend == "parakeet-mlx":
        if not is_apple_silicon():
            raise RuntimeError(
                "asr.backend is parakeet-mlx, which needs Apple Silicon "
                f"(this machine is {describe_platform()}).\n"
                'Set asr.backend = "faster-whisper" in config.toml.'
            )
        try:
            from .asr import ParakeetASR
        except ImportError as exc:
            raise RuntimeError(
                f"The MLX recogniser could not be imported: {exc}\n"
                "Install it with:  pip install parakeet-mlx"
            ) from exc
        return ParakeetASR(cfg, loop, on_word, on_tick, on_level, on_state,
                           on_epoch, on_partial)

    try:
        from .asr_whisper import FasterWhisperASR
    except ImportError as exc:
        raise RuntimeError(
            f"The Whisper recogniser could not be imported: {exc}\n"
            "Install it with:  pip install faster-whisper"
        ) from exc
    return FasterWhisperASR(cfg, loop, on_word, on_tick, on_level, on_state,
                            on_epoch, on_partial)
