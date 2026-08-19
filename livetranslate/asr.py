"""Streaming English ASR from the built-in mic (spec section 4).

Parakeet TDT via parakeet-mlx. The model is deliberately the **English-only**
v2 rather than the multilingual v3: English-only status is tier 1 of the
speaker gate (spec section 5). Measured here, v3 transcribes Portuguese as
clean Portuguese or as confident English-looking word salad that sails through
the coherence gate.

Why this does not use parakeet-mlx's StreamingParakeet
------------------------------------------------------
It is not usable for continuous live audio in 0.5.2. Its finalized output
depends chaotically on where speech sits relative to the block grid: feeding
the same utterance behind 0.0s / 0.2s / 1.6s of leading silence produced 87,
7 and *zero* finalized tokens respectively, reproducibly. Some block sizes
(1120ms, 1280ms) finalize nothing at all whatever the audio. It also never
makes token timestamps absolute, unlike the offline path.

Instead this runs the well-tested stateless path (`generate()`) over a short
rolling window, and commits words with a local-agreement policy: a word is
emitted only once two consecutive decodes agree on it AND it sits at least
`stability_lag_ms` behind the live edge. Measured on the same audio this gives
0.00-0.05 WER (versus 0.18-1.00), is insensitive to leading silence, and costs
~35ms per decode -- about 7% GPU duty, so it barely contends with the
translation model.

The window is trimmed to just behind the last committed word, so its cost stays
flat no matter how long the session runs.

Threading model:
  * sounddevice's callback runs on a realtime audio thread. It does nothing but
    hand the block to a queue -- MLX inference there would drop audio.
  * a worker thread drains the queue, runs the rolling decode, and emits words.
  * words, ticks and levels are posted back to the asyncio loop with
    call_soon_threadsafe.

MLX streams are thread-local. Arrays created on one thread cannot be evaluated
on another ("There is no Stream(cpu, 1) in current thread"), and that includes
the model weights and the cached mel filterbank. The model is therefore loaded
*inside* the decode worker thread and never handed across threads.

Recovery: the input stream is supervised. If the mic is taken by another app,
the machine sleeps and wakes, or the default device changes, the stream is torn
down and reopened with backoff without restarting the process.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from .chunker import Word

log = logging.getLogger("livetranslate.asr")

# Parakeet tokens carry a leading space at the start of each new word.
_WORD_START = " "


class ParakeetASR:
    def __init__(
        self,
        cfg,
        loop: asyncio.AbstractEventLoop,
        on_word: Callable[[Word], None],
        on_tick: Callable[[float, bool], None],
        on_level: Callable[[float], None] | None = None,
        on_state: Callable[[bool], None] | None = None,
        on_epoch: Callable[[float], None] | None = None,
        on_partial: Callable[[str], None] | None = None,
    ):
        self.model_id = cfg.get("asr.model", "mlx-community/parakeet-tdt-0.6b-v2")
        self.model_path = cfg.get("asr.model_path", "") or ""
        self.sample_rate = int(cfg.get("asr.sample_rate", 16000))
        self.mic_block_ms = int(cfg.get("asr.mic_block_ms", 160))
        self.decode_interval_ms = int(cfg.get("asr.decode_interval_ms", 500))
        self.stability_lag_ms = int(cfg.get("asr.stability_lag_ms", 700))
        self.context_seconds = float(cfg.get("asr.context_seconds", 2.0))
        self.max_window_seconds = float(cfg.get("asr.max_window_seconds", 24.0))
        self.device_hint = cfg.get("asr.device", "") or None
        self.mic_open_timeout_s = float(cfg.get("asr.mic_open_timeout_s", 6.0))

        self.vad_threshold = float(cfg.get("vad.energy_threshold", 0.012))
        self.vad_hangover_ms = float(cfg.get("vad.hangover_ms", 200))

        self.loop = loop
        self.on_word = on_word
        self.on_tick = on_tick
        self.on_level = on_level
        self.on_state = on_state
        self.on_epoch = on_epoch
        self.on_partial = on_partial

        self._audio_q: queue.Queue = queue.Queue(maxsize=512)
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._supervisor: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None

        # Loaded by the worker thread; see the module docstring. Never assign
        # a model created on another thread.
        self._model = None
        self._ready = threading.Event()
        self._load_error: BaseException | None = None
        self._samples_fed = 0            # samples handed to the ASR
        self._last_level_post = 0.0
        # Rolling decode window and the local-agreement commit state.
        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_start = 0.0            # absolute audio time of _buf[0]
        self._committed = 0.0            # absolute time up to which words are emitted
        self._prev_words: list[tuple[str, float, float, float]] = []
        self._finish = threading.Event()
        # (audio_time_end, is_speech) so VAD can be queried *at the frontier*
        # rather than at the microphone's leading edge.
        self._vad: collections.deque = collections.deque(maxlen=2048)

        # Counters for the heartbeat log, so a silent pipeline can be diagnosed
        # from the log alone rather than by guessing.
        self.stats = {"blocks": 0, "decodes": 0, "words": 0,
                      "decode_ms_total": 0.0, "peak_rms": 0.0, "speech_blocks": 0}
        self.running = False
        self.device_ok = False
        self.audio_epoch: float | None = None

    # ---------------- model ----------------

    def _resolve_model_source(self) -> str:
        """Prefer local weights when present; fall back to the Hugging Face id."""
        if self.model_path:
            path = Path(self.model_path)
            if not path.is_absolute():
                path = Path(__file__).resolve().parent.parent / path
            if (path / "config.json").exists() and (path / "model.safetensors").exists():
                return str(path)
        return self.model_id

    def load_model(self) -> None:
        from parakeet_mlx import from_pretrained

        source = self._resolve_model_source()
        t0 = time.perf_counter()
        self._model = from_pretrained(source)
        log.info("ASR model %s loaded in %.1fs", source, time.perf_counter() - t0)

    # ---------------- lifecycle ----------------

    def start(self, use_microphone: bool = True) -> None:
        """Start decoding. With use_microphone=False the decode worker runs but
        no input stream is opened, and audio must be supplied via push_audio().
        That is how the offline tests drive the real pipeline from recordings."""
        self._stop.clear()
        self._ready.clear()
        self._load_error = None
        self._worker = threading.Thread(target=self._run_worker, name="asr-worker", daemon=True)
        self._worker.start()
        if use_microphone:
            self._supervisor = threading.Thread(
                target=self._run_supervisor, name="asr-mic", daemon=True
            )
            self._supervisor.start()
            self._watchdog = threading.Thread(
                target=self._run_watchdog, name="asr-mic-watchdog", daemon=True
            )
            self._watchdog.start()
        else:
            self._set_device_ok(True)
        self.running = True

    def wait_ready(self, timeout: float = 600.0) -> None:
        """Block until the worker has loaded the model, re-raising failures."""
        if not self._ready.wait(timeout):
            raise RuntimeError(
                f"ASR model did not load within {timeout:.0f}s "
                f"({self._resolve_model_source()})."
            )
        if self._load_error is not None:
            raise RuntimeError(f"Could not load the ASR model: {self._load_error}")

    def stop(self) -> None:
        self._stop.set()
        # The supervisor is deliberately NOT joined: opening a CoreAudio input
        # stream can block indefinitely (see _run_watchdog), and a shutdown
        # must not inherit that hang. It is a daemon thread.
        for t in (self._worker,):
            if t is not None:
                t.join(timeout=5)
        self._supervisor = self._worker = self._watchdog = None
        self.running = False

    def _run_watchdog(self) -> None:
        """Report a microphone that never opens.

        On macOS, opening an input stream from a process that has not been
        granted Microphone permission does not raise -- CoreAudio simply never
        returns, because there is no way to show the permission prompt. Without
        this the app would sit there looking healthy while hearing nothing.
        """
        if self._stop.wait(self.mic_open_timeout_s):
            return
        if self.device_ok:
            return
        log.warning(
            "No audio from the microphone after %.0fs. macOS is probably "
            "waiting on Microphone permission, which cannot be prompted for "
            "here. Grant it to your terminal (or to LiveTranslate.command) in "
            "System Settings > Privacy & Security > Microphone, then start "
            "LiveTranslate again. The display shows a red dot until audio "
            "arrives.",
            self.mic_open_timeout_s,
        )

    def push_audio(self, block) -> None:
        """Feed one block of float32 mono samples (offline sources)."""
        self._audio_q.put(block)

    def flush_pending_word(self) -> None:
        """Commit everything still pending (end of an offline file).

        Signals the worker rather than decoding here: the worker thread owns
        the model, and MLX arrays cannot cross threads.
        """
        self._finish.set()
        for _ in range(100):
            if not self._finish.is_set():
                return
            time.sleep(0.05)

    # ---------------- mic supervision ----------------

    def _resolve_device(self):
        if not self.device_hint:
            return None
        import sounddevice as sd

        for idx, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0 and self.device_hint.lower() in dev["name"].lower():
                return idx
        log.warning("input device %r not found; using system default", self.device_hint)
        return None

    def _run_supervisor(self) -> None:
        """Keeps an input stream open, reopening it after any failure."""
        import sounddevice as sd

        backoff = 0.5
        while not self._stop.is_set():
            try:
                blocksize = int(self.sample_rate * self.mic_block_ms / 1000)

                def callback(indata, frames, time_info, status):
                    if status:
                        log.debug("audio status: %s", status)
                    mono = indata[:, 0].copy() if indata.ndim > 1 else indata.copy()
                    try:
                        self._audio_q.put_nowait(mono)
                    except queue.Full:
                        log.warning("audio queue full; dropping a block")

                with sd.InputStream(
                    samplerate=self.sample_rate,
                    blocksize=blocksize,
                    channels=1,
                    dtype="float32",
                    device=self._resolve_device(),
                    callback=callback,
                ):
                    self._set_device_ok(True)
                    log.info("microphone open at %d Hz", self.sample_rate)
                    backoff = 0.5
                    while not self._stop.is_set():
                        time.sleep(0.2)
            except Exception as exc:
                # Device stolen by another app, sleep/wake, or device change.
                self._set_device_ok(False)
                if self._stop.is_set():
                    break
                log.warning("microphone unavailable (%s); retrying in %.1fs", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

        self._set_device_ok(False)

    def _set_device_ok(self, ok: bool) -> None:
        if ok != self.device_ok:
            self.device_ok = ok
            if self.on_state:
                self.loop.call_soon_threadsafe(self.on_state, ok)

    # ---------------- transcription worker ----------------

    def _run_worker(self) -> None:
        import mlx.core as mx
        from parakeet_mlx.audio import get_logmel

        try:
            self.load_model()
        except BaseException as exc:          # surfaced by wait_ready()
            self._load_error = exc
            self._ready.set()
            log.exception("ASR model failed to load")
            return
        self._ready.set()

        interval_samples = int(self.sample_rate * self.decode_interval_ms / 1000)
        since_decode = 0

        while not self._stop.is_set():
            try:
                block = self._audio_q.get(timeout=0.2)
            except queue.Empty:
                if self._finish.is_set():
                    self._decode(mx, get_logmel, final=True)
                    self._finish.clear()
                # Still advance the clock so the silence and elapsed triggers
                # fire even if the mic has momentarily gone away.
                self._emit_tick()
                continue

            if self.audio_epoch is None:
                self.audio_epoch = time.perf_counter()
                if self.on_epoch:
                    self.loop.call_soon_threadsafe(self.on_epoch, self.audio_epoch)

            self._observe_level(block)
            self._buf = np.concatenate([self._buf, block.astype(np.float32)])
            self._samples_fed += len(block)
            since_decode += len(block)

            if since_decode < interval_samples:
                continue
            since_decode = 0
            try:
                self._decode(mx, get_logmel, final=self._finish.is_set())
            except Exception:
                log.exception("ASR decode failed; continuing")
            self._finish.clear()
            self._emit_tick()

    def _decode(self, mx, get_logmel, final: bool = False) -> None:
        """One rolling decode, committing whatever is now stable."""
        if len(self._buf) < int(self.sample_rate * 0.4):
            return
        t0 = time.perf_counter()
        result = self._model.generate(
            get_logmel(mx.array(self._buf), self._model.preprocessor_config)
        )[0]
        words = self._words_from(result, self._buf_start)
        decode_ms = (time.perf_counter() - t0) * 1000.0
        self.stats["decodes"] += 1
        self.stats["decode_ms_total"] += decode_ms
        log.debug(
            "decode #%d window=%.2fs cost=%.0fms words=%d edge=%.2f committed=%.2f",
            self.stats["decodes"], len(self._buf) / self.sample_rate, decode_ms,
            len(words), self.frontier, self._committed,
        )

        # Local agreement: a word is trustworthy once two consecutive decodes
        # produce it identically. This is what stops half-decoded words from
        # reaching the screen and then changing.
        cand = [w for w in words if w[1] >= self._committed - 0.05]
        prev = [w for w in self._prev_words if w[1] >= self._committed - 0.05]
        agreed = 0
        while (
            agreed < len(cand) and agreed < len(prev)
            and cand[agreed][0] == prev[agreed][0]
            and abs(cand[agreed][1] - prev[agreed][1]) < 0.35
        ):
            agreed += 1
        self._prev_words = words
        if final:
            agreed = len(cand)          # end of input: nothing more is coming

        edge = float("inf") if final else self.frontier
        emitted = 0
        for text, start, end, conf in cand[:agreed]:
            if end > edge:
                break
            self._committed = end
            emitted += 1
            self.stats["words"] += 1
            log.debug("word %r [%.2f-%.2f] conf=%.3f", text, start, end, conf)
            self.loop.call_soon_threadsafe(
                self.on_word,
                Word(text=text, start=start, end=end,
                     confidence=max(0.0, min(1.0, conf))),
            )

        # Everything in the window that is not yet committed: the live tail the
        # speaker is saying right now. Surfaced so the English strip on the
        # display proves recognition is alive even before anything is emitted.
        if self.on_partial is not None:
            tail = " ".join(t for t, s_, e_, _c in words if e_ > self._committed)
            self.loop.call_soon_threadsafe(self.on_partial, tail)

        # Trim the window to just behind the last committed word. The second
        # term bounds it even if nothing ever commits (constant noise), so
        # decode cost and memory stay flat over a 90 minute session.
        keep = max(self._committed - self.context_seconds,
                   self._leading_edge - self.max_window_seconds)
        drop = int((keep - self._buf_start) * self.sample_rate)
        if drop > 0:
            self._buf = self._buf[drop:]
            self._buf_start += drop / float(self.sample_rate)

    @staticmethod
    def _words_from(result, offset: float):
        """Group subword tokens into whole words with absolute timestamps.

        A token beginning with a space starts a new word, so the chunker only
        ever sees word boundaries.
        """
        groups: list[list] = []
        current: list = []
        for tok in result.tokens:
            if tok.text.startswith(_WORD_START) and current:
                groups.append(current)
                current = []
            current.append(tok)
        if current:
            groups.append(current)

        out = []
        for g in groups:
            text = "".join(t.text for t in g).strip()
            if not text:
                continue
            # Geometric mean of token confidences, matching parakeet's own
            # aggregation for sentences.
            confs = [max(1e-10, float(t.confidence)) for t in g]
            conf = float(np.exp(np.mean(np.log(confs))))
            out.append((text, offset + float(g[0].start), offset + float(g[-1].end), conf))
        return out

    # ---------------- clocks and VAD ----------------

    @property
    def _leading_edge(self) -> float:
        """Audio time of the most recent sample handed to the ASR."""
        return self._samples_fed / float(self.sample_rate)

    @property
    def frontier(self) -> float:
        """Audio time behind which words are considered stable."""
        return max(0.0, self._leading_edge - self.stability_lag_ms / 1000.0)

    def _observe_level(self, block: np.ndarray) -> None:
        rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64)) + 1e-12))
        self.stats["blocks"] += 1
        self.stats["peak_rms"] = max(self.stats["peak_rms"], rms)
        is_speech = rms >= self.vad_threshold
        if is_speech:
            self.stats["speech_blocks"] += 1
        self._vad.append((self._leading_edge, is_speech))
        if self.on_level:
            wall = time.perf_counter()
            if wall - self._last_level_post >= 0.1:      # ~10 Hz is plenty
                self._last_level_post = wall
                # Raw speech RMS is small; map it to a useful visual range.
                self.loop.call_soon_threadsafe(self.on_level, min(1.0, rms * 12.0))

    def _speech_near(self, t: float) -> bool:
        """Was there speech within the hangover window ending at time t?

        Queried at the frontier, not at the microphone, so the chunker's notion
        of silence matches the transcript it is actually cutting.
        """
        window = self.vad_hangover_ms / 1000.0
        return any(speech for (ts, speech) in self._vad if t - window <= ts <= t)

    def _emit_tick(self) -> None:
        now = self.frontier
        self.loop.call_soon_threadsafe(self.on_tick, now, self._speech_near(now))
