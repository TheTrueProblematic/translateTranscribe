"""Speech recognition for Windows and anything that is not Apple Silicon.

MLX -- and therefore parakeet-mlx -- runs only on Apple Silicon, so the macOS
recogniser cannot be used here at all. This backend uses faster-whisper
(CTranslate2), which ships Windows wheels and runs on CPU or CUDA.

It presents exactly the same interface as ParakeetASR so the rest of the
pipeline does not know or care which one is running: same constructor
callbacks, same start/stop, same push_audio for offline use, same stats.

Two things it does that the MLX backend cannot:

  * It reports the language it heard. Whisper identifies language from the
    audio, which is far better evidence than scoring the spelling of the
    transcript, and it feeds straight into the dual-language router.
  * It runs anywhere, at the cost of being slower per decode.

Decoding strategy: one decode per utterance, cut on silence.

This is deliberately NOT the rolling re-decode the macOS backend uses. Whisper
pads every input to thirty seconds internally, so a decode costs the same
whether it is given 1.5 seconds of audio or 8 -- measured here, 1490ms versus
1681ms. Re-decoding a short window several times a second is therefore
impossible: the decode alone exceeds the interval, and the backlog grows
without bound.

Instead, audio accumulates until the voice-activity detector reports a pause,
and that utterance is decoded once. Silence is never decoded at all. The cost
is that nothing appears until the speaker pauses; the benefit is that it
actually keeps up, on a CPU, without a GPU.

Because each utterance is decoded once and completely, there is no local
agreement step and no stability lag: words are final when they arrive.
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


class FasterWhisperASR:
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
        self.model_size = cfg.get("whisper.model", "small")
        self.device = cfg.get("whisper.device", "auto")
        self.compute_type = cfg.get("whisper.compute_type", "auto")
        self.beam_size = int(cfg.get("whisper.beam_size", 1))
        self.model_dir = cfg.get("whisper.download_root", "") or None
        self.languages = [
            s.lower() for s in (cfg.get("whisper.languages", ["en", "pt"]) or [])
        ]

        self.sample_rate = int(cfg.get("asr.sample_rate", 16000))
        self.mic_block_ms = int(cfg.get("asr.mic_block_ms", 160))
        # Utterance segmentation. A pause of this long closes the utterance
        # and triggers a decode.
        self.utterance_silence_ms = float(cfg.get("whisper.utterance_silence_ms", 450))
        # Backstop for someone who does not pause: decode anyway at this
        # length, so words keep flowing during a run-on sentence.
        self.max_utterance_s = float(cfg.get("whisper.max_utterance_s", 6.0))
        # Shorter than this is a cough or a door, not speech worth decoding.
        self.min_utterance_s = float(cfg.get("whisper.min_utterance_s", 0.4))
        self.device_hint = cfg.get("asr.device", "") or None
        self.mic_open_timeout_s = float(cfg.get("asr.mic_open_timeout_s", 6.0))

        self.vad_threshold = float(cfg.get("vad.energy_threshold", 0.008))
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
        self._finish = threading.Event()
        self._worker: threading.Thread | None = None
        self._supervisor: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None

        self._model = None
        self._ready = threading.Event()
        self._load_error: BaseException | None = None

        self._samples_fed = 0
        self._last_level_post = 0.0
        # The utterance being accumulated, and where it starts on the audio
        # timeline.
        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_start = 0.0
        self._speech_seen = False       # has this utterance contained speech?
        self._silence_run = 0.0         # seconds of silence since the last speech
        self._decoded_to = 0.0          # audio time everything is decoded up to
        self._vad: collections.deque = collections.deque(maxlen=2048)

        self.stats = {"blocks": 0, "decodes": 0, "words": 0,
                      "decode_ms_total": 0.0, "peak_rms": 0.0, "speech_blocks": 0}
        self.running = False
        self.device_ok = False
        self.audio_epoch: float | None = None

    # ---------------- model ----------------

    def _resolve_device(self) -> tuple[str, str]:
        """Pick device and compute type, preferring CUDA when it is usable."""
        device, compute = self.device, self.compute_type
        if device == "auto":
            device = "cpu"
            try:
                import ctranslate2
                if ctranslate2.get_cuda_device_count() > 0:
                    device = "cuda"
            except Exception as exc:
                log.debug("CUDA probe failed, using CPU: %s", exc)
        if compute == "auto":
            compute = "float16" if device == "cuda" else "int8"
        return device, compute

    def load_model(self) -> None:
        from faster_whisper import WhisperModel

        device, compute = self._resolve_device()
        root = self.model_dir
        if root:
            root_path = Path(root)
            if not root_path.is_absolute():
                root_path = Path(__file__).resolve().parent.parent / root_path
            root_path.mkdir(parents=True, exist_ok=True)
            root = str(root_path)

        t0 = time.perf_counter()
        log.info("loading whisper '%s' on %s (%s)...", self.model_size, device, compute)
        self._model = WhisperModel(
            self.model_size, device=device, compute_type=compute, download_root=root
        )
        log.info(
            "whisper '%s' loaded on %s (%s) in %.1fs",
            self.model_size, device, compute, time.perf_counter() - t0,
        )

    # ---------------- lifecycle ----------------

    def start(self, use_microphone: bool = True) -> None:
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

    def wait_ready(self, timeout: float = 900.0) -> None:
        """Block until the model is loaded, re-raising failures.

        The timeout is generous: the first run downloads the weights.
        """
        if not self._ready.wait(timeout):
            raise RuntimeError(
                f"Whisper model '{self.model_size}' did not load within {timeout:.0f}s."
            )
        if self._load_error is not None:
            raise RuntimeError(f"Could not load the Whisper model: {self._load_error}")

    def stop(self) -> None:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=10)
        self._supervisor = self._worker = self._watchdog = None
        self.running = False

    def push_audio(self, block) -> None:
        self._audio_q.put(block)

    def flush_pending_word(self) -> None:
        self._finish.set()
        for _ in range(200):
            if not self._finish.is_set():
                return
            time.sleep(0.05)

    # ---------------- microphone ----------------

    def _resolve_input_device(self):
        if not self.device_hint:
            return None
        import sounddevice as sd

        for idx, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0 and self.device_hint.lower() in dev["name"].lower():
                return idx
        log.warning("input device %r not found; using system default", self.device_hint)
        return None

    def _run_supervisor(self) -> None:
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
                    samplerate=self.sample_rate, blocksize=blocksize, channels=1,
                    dtype="float32", device=self._resolve_input_device(),
                    callback=callback,
                ):
                    self._set_device_ok(True)
                    log.info("microphone open at %d Hz", self.sample_rate)
                    backoff = 0.5
                    while not self._stop.is_set():
                        time.sleep(0.2)
            except Exception as exc:
                self._set_device_ok(False)
                if self._stop.is_set():
                    break
                log.warning("microphone unavailable (%s); retrying in %.1fs", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
        self._set_device_ok(False)

    def _run_watchdog(self) -> None:
        if self._stop.wait(self.mic_open_timeout_s):
            return
        if self.device_ok:
            return
        log.warning(
            "No audio from the microphone after %.0fs. Check that a recording "
            "device is connected and enabled, and that microphone access is "
            "allowed under Settings > Privacy & security > Microphone. The "
            "display shows a red dot until audio arrives.",
            self.mic_open_timeout_s,
        )

    def _set_device_ok(self, ok: bool) -> None:
        if ok != self.device_ok:
            self.device_ok = ok
            if self.on_state:
                self.loop.call_soon_threadsafe(self.on_state, ok)

    # ---------------- worker ----------------

    def _run_worker(self) -> None:
        try:
            self.load_model()
        except BaseException as exc:
            self._load_error = exc
            self._ready.set()
            log.exception("Whisper model failed to load")
            return
        self._ready.set()

        block_seconds = self.mic_block_ms / 1000.0

        while not self._stop.is_set():
            try:
                block = self._audio_q.get(timeout=0.2)
            except queue.Empty:
                if self._finish.is_set():
                    self._close_utterance(force=True)
                    self._finish.clear()
                self._emit_tick()
                continue

            if self.audio_epoch is None:
                self.audio_epoch = time.perf_counter()
                if self.on_epoch:
                    self.loop.call_soon_threadsafe(self.on_epoch, self.audio_epoch)

            is_speech = self._observe_level(block)
            self._buf = np.concatenate([self._buf, block.astype(np.float32)])
            self._samples_fed += len(block)

            if is_speech:
                self._speech_seen = True
                self._silence_run = 0.0
            else:
                self._silence_run += block_seconds
                if not self._speech_seen:
                    # Leading silence: drop it rather than decode it, and keep
                    # the utterance start moving with the live edge.
                    keep = int(self.sample_rate * 0.25)
                    if len(self._buf) > keep:
                        trimmed = len(self._buf) - keep
                        self._buf = self._buf[trimmed:]
                        self._buf_start += trimmed / float(self.sample_rate)
                    self._decoded_to = self._buf_start

            silence_closed = (
                self._speech_seen
                and self._silence_run * 1000.0 >= self.utterance_silence_ms
            )
            too_long = self._buffer_seconds >= self.max_utterance_s

            if silence_closed or too_long:
                self._close_utterance(reason="silence" if silence_closed else "max_length")

            if self._finish.is_set():
                self._close_utterance(force=True)
                self._finish.clear()

            self._emit_tick()

    @property
    def _buffer_seconds(self) -> float:
        return len(self._buf) / float(self.sample_rate)

    def _close_utterance(self, reason: str = "flush", force: bool = False) -> None:
        """Decode the accumulated utterance, emit its words, and reset."""
        duration = self._buffer_seconds
        has_speech = self._speech_seen
        start = self._buf_start
        audio = self._buf                    # capture before the reset below

        # Reset before decoding so audio arriving during the decode belongs to
        # the next utterance rather than being lost or decoded twice.
        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_start = start + duration
        self._speech_seen = False
        self._silence_run = 0.0

        if not has_speech and not force:
            self._decoded_to = self._buf_start
            return
        if duration < self.min_utterance_s:
            self._decoded_to = self._buf_start
            return

        try:
            self._decode_utterance(audio, start, reason)
        except Exception:
            log.exception("Whisper decode failed; continuing")
        finally:
            self._decoded_to = self._buf_start

    def _decode_utterance(self, audio, start: float, reason: str) -> None:
        t0 = time.perf_counter()
        segments, info = self._model.transcribe(
            audio,
            beam_size=self.beam_size,
            word_timestamps=True,
            condition_on_previous_text=False,   # stops runaway repetition loops
            vad_filter=False,                   # our own VAD already segmented this
            language=self.languages[0] if len(self.languages) == 1 else None,
        )
        language = (info.language or "").lower() if info is not None else None
        lang_prob = float(getattr(info, "language_probability", 0.0) or 0.0)
        if self.languages and language not in self.languages:
            log.debug("whisper reported language %r (p=%.2f), not routed",
                      language, lang_prob)
            language = None

        words = self._words_from(segments, language, start)
        decode_ms = (time.perf_counter() - t0) * 1000.0
        self.stats["decodes"] += 1
        self.stats["decode_ms_total"] += decode_ms
        log.debug(
            "utterance #%d (%s) %.2fs audio, cost=%.0fms, words=%d, lang=%s p=%.2f",
            self.stats["decodes"], reason, len(audio) / self.sample_rate,
            decode_ms, len(words), language, lang_prob,
        )

        for text, w_start, w_end, conf, lang in words:
            self.stats["words"] += 1
            log.debug("word %r [%.2f-%.2f] conf=%.3f lang=%s",
                      text, w_start, w_end, conf, lang)
            self.loop.call_soon_threadsafe(
                self.on_word,
                Word(text=text, start=w_start, end=w_end,
                     confidence=max(0.0, min(1.0, conf)), language=lang),
            )

        if self.on_partial is not None:
            # No streaming from Whisper, so the monitor shows the utterance
            # just decoded rather than a live tail.
            self.loop.call_soon_threadsafe(
                self.on_partial, " ".join(t for t, *_ in words)
            )

    def _words_from(self, segments, language, offset: float):
        """Flatten Whisper segments into absolute-timed words.

        Segment times are relative to the utterance, so its start time on the
        audio timeline is added. Whisper reports a per-word probability; where it is
        missing the segment's average log-probability is converted instead.
        """
        out = []
        for seg in segments:
            seg_conf = _logprob_to_confidence(getattr(seg, "avg_logprob", None))
            seg_words = getattr(seg, "words", None)
            if seg_words:
                for w in seg_words:
                    text = (w.word or "").strip()
                    if not text:
                        continue
                    prob = getattr(w, "probability", None)
                    conf = float(prob) if prob is not None else seg_conf
                    out.append((text, offset + float(w.start),
                                offset + float(w.end), conf, language))
            else:
                text = (seg.text or "").strip()
                if text:
                    out.append((text, offset + float(seg.start),
                                offset + float(seg.end), seg_conf, language))
        return out

    # ---------------- clocks and VAD ----------------

    @property
    def _leading_edge(self) -> float:
        return self._samples_fed / float(self.sample_rate)

    @property
    def frontier(self) -> float:
        """Audio time everything has been decoded up to.

        Unlike the MLX backend there is no stability lag: an utterance is
        decoded once and its words are final, so the frontier simply follows
        the last completed decode.
        """
        return self._decoded_to

    def _observe_level(self, block: np.ndarray) -> bool:
        rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64)) + 1e-12))
        self.stats["blocks"] += 1
        self.stats["peak_rms"] = max(self.stats["peak_rms"], rms)
        is_speech = rms >= self.vad_threshold
        if is_speech:
            self.stats["speech_blocks"] += 1
        self._vad.append((self._leading_edge, is_speech))
        if self.on_level:
            wall = time.perf_counter()
            if wall - self._last_level_post >= 0.1:
                self._last_level_post = wall
                self.loop.call_soon_threadsafe(self.on_level, min(1.0, rms * 12.0))
        return is_speech

    def _speech_near(self, t: float) -> bool:
        window = self.vad_hangover_ms / 1000.0
        return any(speech for (ts, speech) in self._vad if t - window <= ts <= t)

    def _emit_tick(self) -> None:
        now = self.frontier
        self.loop.call_soon_threadsafe(self.on_tick, now, self._speech_near(now))


def _logprob_to_confidence(avg_logprob) -> float:
    """Whisper reports an average log-probability per segment; the pipeline
    works in 0..1 confidence. exp() is the natural conversion and lands in a
    range comparable to the MLX backend's token confidences."""
    if avg_logprob is None:
        return 0.5
    try:
        return max(0.0, min(1.0, float(np.exp(avg_logprob))))
    except Exception:
        return 0.5
