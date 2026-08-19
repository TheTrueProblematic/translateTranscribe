"""LiveTranslate entry point (spec section 10).

Starting must: verify LM Studio is reachable and the model id is present,
start the ASR, start the websocket server, open the display, and print nothing
else unless something is wrong.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livetranslate.config import Config
from livetranslate.logging_setup import setup_logging
from livetranslate.pipeline import Pipeline
from livetranslate.server import DisplayServer
from livetranslate.translator import LMStudioUnavailable, Translator

log = logging.getLogger("livetranslate.main")


def die(message: str) -> None:
    print(f"\nLiveTranslate cannot start.\n\n{message}\n", file=sys.stderr)
    sys.exit(1)


async def run(args) -> None:
    cfg = Config.load(args.config)
    log_path = setup_logging(cfg)
    log.info("=== LiveTranslate starting ===")

    log.info("config: %s", cfg.path)
    log.info("lmstudio=%s model=%s", cfg.get("lmstudio.base_url"), cfg.get("lmstudio.model"))
    log.info("asr=%s decode_interval=%sms stability=%sms",
             cfg.get("asr.model"), cfg.get("asr.decode_interval_ms"),
             cfg.get("asr.stability_lag_ms"))
    log.info("gate: min_confidence=%s min_english=%s min_words=%s start_paused=%s",
             cfg.get("gate.min_confidence"), cfg.get("gate.min_english_score"),
             cfg.get("gate.min_words"), cfg.get("hotkey.start_paused"))

    translator = Translator(cfg)
    try:
        await translator.preflight()
        # Actually load the model now. Listing models does not load anything
        # under JIT, so without this the model only appears in LM Studio when
        # the first phrase arrives -- and if nothing ever arrives, it looks
        # like the translator is broken when the real fault is upstream.
        await translator.warmup()
    except LMStudioUnavailable as exc:
        await translator.close()
        die(str(exc))

    server = DisplayServer(cfg)
    pipeline = Pipeline(cfg, server, translator=translator)

    async def on_command(payload: dict) -> None:
        if payload.get("type") == "toggle_pause":
            await pipeline.toggle_pause()

    server.on_command = on_command

    try:
        url = await server.start()
    except RuntimeError as exc:
        await translator.close()
        die(str(exc))

    await pipeline.start()

    loop = asyncio.get_running_loop()
    asr = None
    if not args.no_asr:
        from livetranslate.asr import ParakeetASR

        def on_word(word):
            asyncio.ensure_future(pipeline.feed_word(word))

        def on_tick(now_s, speech_active):
            asyncio.ensure_future(pipeline.tick(now_s, speech_active))

        def on_level(rms):
            asyncio.ensure_future(server.send_level(rms))

        def on_partial(text):
            asyncio.ensure_future(pipeline.set_partial(text))

        def on_epoch(epoch):
            pipeline.audio_epoch = epoch

        def on_state(ok):
            pipeline.set_listening(ok)
            asyncio.ensure_future(pipeline.publish_status())

        asr = ParakeetASR(cfg, loop, on_word, on_tick, on_level, on_state,
                          on_epoch, on_partial)
        asr.start()
        try:
            # The worker thread owns the model (MLX streams are thread-local).
            # First run downloads the weights, which is slow.
            await asyncio.to_thread(asr.wait_ready)
        except Exception as exc:
            asr.stop()
            await pipeline.stop()
            await server.stop()
            die(
                f"Could not load the ASR model '{cfg.get('asr.model')}'.\n{exc}\n"
                "The first run downloads it; check the network and try again."
            )

    heartbeat = asyncio.create_task(_heartbeat(pipeline, asr, server))

    if cfg.get("server.open_browser", True) and not args.no_browser:
        _open_display(url)

    await pipeline.publish_status()
    log.info("ready at %s (log: %s)", url, log_path)

    stop = asyncio.Event()
    try:
        for sig in ("SIGINT", "SIGTERM"):
            import signal
            loop.add_signal_handler(getattr(signal, sig), stop.set)
    except (NotImplementedError, RuntimeError):
        pass

    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    finally:
        heartbeat.cancel()
        if asr is not None:
            asr.stop()
        await pipeline.stop()
        await server.stop()
        log.info("=== LiveTranslate stopped ===")


async def _heartbeat(pipeline, asr, server) -> None:
    """Periodic one-line summary of the whole pipeline.

    Deliberately unconditional. A session that produces nothing is the hardest
    thing to debug after the fact, and this makes the failing stage obvious
    from the log alone: no audio, no words, or words that are all being gated.
    """
    interval = 15.0
    prev = None
    while True:
        await asyncio.sleep(interval)
        a = dict(asr.stats) if asr is not None else {}
        p = dict(pipeline.stats)
        g = dict(pipeline.gate.stats)
        avg_ms = (a.get("decode_ms_total", 0.0) / a["decodes"]) if a.get("decodes") else 0.0
        log.info(
            "HEARTBEAT audio=%.0fs blocks=%d speech=%d peak_rms=%.4f | "
            "decodes=%d avg=%.0fms words=%d | chunks=%d accepted=%d rejected=%d "
            "translated=%d errors=%d | gate=%s | paused=%s listening=%s clients=%d",
            (a.get("blocks", 0) * (asr.mic_block_ms / 1000.0)) if asr else 0.0,
            a.get("blocks", 0), a.get("speech_blocks", 0), a.get("peak_rms", 0.0),
            a.get("decodes", 0), avg_ms, a.get("words", 0),
            p["chunks"], p["accepted"], p["rejected"], p["translated"],
            p["translation_errors"], g, pipeline.gate.paused,
            pipeline._listening, server.client_count,
        )
        # Call out the two silent-failure modes explicitly.
        if asr is not None and a.get("blocks", 0) == (prev or {}).get("blocks", -1):
            log.warning("no audio blocks since the last heartbeat -- microphone stalled?")
        if p["chunks"] and p["accepted"] == 0:
            log.warning(
                "every chunk so far has been rejected (%s). If reason=paused, press "
                "SPACE on the display to resume; if low_confidence, lower "
                "gate.min_confidence in config.toml.", g,
            )
        prev = a


def _open_display(url: str) -> None:
    """Open the display, preferring a Chrome app window in fullscreen."""
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if Path(chrome).exists():
        try:
            subprocess.Popen(
                [chrome, f"--app={url}", "--start-fullscreen",
                 "--user-data-dir=" + str(Path.home() / ".livetranslate-chrome")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return
        except Exception:
            pass
    webbrowser.open(url)


def main() -> None:
    ap = argparse.ArgumentParser(prog="livetranslate")
    ap.add_argument("--config", default=None, help="path to config.toml")
    ap.add_argument("--no-browser", action="store_true", help="do not open the display")
    ap.add_argument("--no-asr", action="store_true", help="display/server only, no microphone")
    ap.add_argument("--diagnose", action="store_true",
                    help="run a self-test of mic, ASR and LM Studio, then exit")
    args = ap.parse_args()
    try:
        if args.diagnose:
            from livetranslate.diagnose import run_diagnostics
            sys.exit(asyncio.run(run_diagnostics(args.config)))
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
