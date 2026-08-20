"""LM Studio translation client (spec section 8).

One in-flight request at a time, enforced with a lock. No batching, no
backpressure machinery: chunks arrive every few seconds and a translation
completes in ~130ms, so the queue provably cannot back up (spec section 6).

Streaming is on so the audience sees text appear while the speaker is still
talking. The stream yields the *cumulative cleaned line* rather than raw
deltas, so the display can simply replace the current line each time and never
has to reason about partial escape sequences or a leaked prefix.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import AsyncIterator

import aiohttp

from .postprocess import capitalize_and_punctuate, postprocess, strip_scaffolding
from .prompt import build_pt_en_prompt, build_system_prompt

log = logging.getLogger("livetranslate.translator")


class LMStudioUnavailable(RuntimeError):
    """Raised with a human-readable message when the server or model is missing."""


class Translator:
    def __init__(self, cfg):
        self.base_url = cfg.get("lmstudio.base_url", "http://localhost:1234/v1").rstrip("/")
        self.model = cfg.get("lmstudio.model", "hunyuan-mt2-1.8b-mlx")
        self.temperature = float(cfg.get("lmstudio.temperature", 0.0))
        self.top_p = float(cfg.get("lmstudio.top_p", 1.0))
        self.seed = cfg.get("lmstudio.seed", 7)
        self.max_tokens = int(cfg.get("lmstudio.max_tokens", 200))
        self.timeout_s = float(cfg.get("lmstudio.timeout_s", 20.0))
        self.context_lines = int(cfg.get("lmstudio.context_lines", 2))

        # Built once: may carry a session vocabulary from the config.
        # Two directions, each with its own prompt and its own context. Keeping
        # the contexts apart matters: the speaker's Portuguese output must not
        # become "prior turns" for translating someone else's Portuguese back
        # into English, or the model starts answering the wrong conversation.
        self.system_prompt = build_system_prompt(cfg)
        self.pt_en_prompt = build_pt_en_prompt(cfg)

        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()          # one in-flight request, per spec
        # (english, portuguese) pairs, replayed as real chat turns.
        self._contexts: dict[str, deque] = {
            "en2pt": deque(maxlen=max(0, self.context_lines)),
            "pt2en": deque(maxlen=max(0, self.context_lines)),
        }

    # ---------------- lifecycle ----------------

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_s)
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def preflight(self) -> None:
        """Verify the server is reachable and the model id exists.

        Raises LMStudioUnavailable with a message meant for a human standing in
        a classroom, not a stack trace.
        """
        await self.start()
        assert self._session is not None
        url = f"{self.base_url}/models"
        try:
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    raise LMStudioUnavailable(
                        f"LM Studio answered HTTP {resp.status} at {url}.\n"
                        "Open LM Studio and make sure the local server is running."
                    )
                data = await resp.json()
        except aiohttp.ClientError as exc:
            raise LMStudioUnavailable(
                f"Cannot reach LM Studio at {self.base_url}.\n"
                "Open LM Studio, go to the Developer/Server tab, and start the "
                f"local server on port {self.base_url.split(':')[-1].split('/')[0]}.\n"
                f"({exc})"
            ) from exc
        except asyncio.TimeoutError as exc:
            raise LMStudioUnavailable(
                f"LM Studio did not respond within {self.timeout_s}s at {self.base_url}."
            ) from exc

        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        if self.model in ids:
            return

        # The exact id differs between platforms: the Mac runs an MLX build,
        # Windows a GGUF one, and LM Studio names them differently. Rather than
        # fail on a naming mismatch, accept an unambiguous near-match.
        wanted = self.model.lower()
        matches = [i for i in ids if i.lower() == wanted]
        if not matches:
            stem = wanted.split("/")[-1].replace("-mlx", "").replace("-gguf", "")
            key = stem.split("-")[0] or stem            # e.g. "hunyuan"
            matches = [i for i in ids if key and key in i.lower()]

        if len(matches) == 1:
            log.warning(
                "model %r not found; using the one close match %r instead. "
                "Set lmstudio.model to that id to silence this.",
                self.model, matches[0],
            )
            self.model = matches[0]
            return

        available = "\n  ".join(ids) or "(none loaded)"
        hint = (f"\nSeveral models could match: {', '.join(matches)}.\n"
                "Set lmstudio.model to exactly one of them."
                if matches else
                "\nDownload it in LM Studio, or change lmstudio.model in your config.")
        raise LMStudioUnavailable(
            f"Model '{self.model}' is not available in LM Studio.\n"
            f"Models currently offered:\n  {available}{hint}"
        )

    async def warmup(self) -> float:
        """Force LM Studio to actually load the model, before anyone speaks.

        preflight() only lists models, which with JIT loading does not load
        anything -- so without this the model stays unloaded until the first
        phrase, the first translation pays the whole load time, and a silent
        pipeline is indistinguishable from a broken one because nothing ever
        appears in LM Studio. Returns seconds taken.
        """
        t0 = time.perf_counter()
        out = await self.translate("this is a microphone test")
        elapsed = time.perf_counter() - t0
        self.reset_context()          # never let the warmup leak into context
        log.info("LM Studio warmup: %.2fs, model returned %r", elapsed, out)
        if not out:
            raise LMStudioUnavailable(
                f"LM Studio accepted the request but returned nothing for model "
                f"'{self.model}'. Check that the model loads correctly in LM Studio."
            )
        return elapsed

    # ---------------- prompting ----------------

    @staticmethod
    def _labels(direction: str) -> tuple[str, str]:
        return ("English", "Portuguese") if direction == "en2pt" else ("Portuguese", "English")

    def _build_messages(self, text: str, direction: str = "en2pt") -> list[dict]:
        """System prompt, prior turns, then the line to translate.

        Context is replayed as genuine user/assistant turns rather than pasted
        into the user message. Told in prose to "not repeat" the previous
        lines, this model repeats them anyway -- it prepended the last
        translation to the next one, so the display showed the same sentence
        twice and the line grew until the type shrank. As real turns it has
        nothing to copy: the earlier translations are already its own replies.
        """
        prompt = self.system_prompt if direction == "en2pt" else self.pt_en_prompt
        src, dst = self._labels(direction)
        messages = [{"role": "system", "content": prompt}]
        for prior_src, prior_dst in self._contexts[direction]:
            messages.append({"role": "user", "content": f"{src}: {prior_src}\n{dst}:"})
            messages.append({"role": "assistant", "content": prior_dst})
        messages.append({"role": "user", "content": f"{src}: {text}\n{dst}:"})
        return messages

    def _payload(self, text: str, stream: bool, direction: str = "en2pt") -> dict:
        return {
            "model": self.model,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "seed": self.seed,
            "max_tokens": self.max_tokens,
            "stream": stream,
            "messages": self._build_messages(text, direction),
        }

    def remember(self, source: str, translated: str, direction: str = "en2pt") -> None:
        if translated and self.context_lines > 0:
            self._contexts[direction].append((source, translated))

    def reset_context(self) -> None:
        for ctx in self._contexts.values():
            ctx.clear()

    # ---------------- translation ----------------

    async def translate_stream(
        self, text: str, direction: str = "en2pt"
    ) -> AsyncIterator[tuple[str, bool]]:
        """Yield (cumulative_line, is_final). Holds the single-flight lock."""
        await self.start()
        assert self._session is not None

        target = "pt" if direction == "en2pt" else "en"
        async with self._lock:
            raw = ""
            try:
                async with self._session.post(
                    f"{self.base_url}/chat/completions",
                    json=self._payload(text, stream=True, direction=direction),
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.error("translation HTTP %s: %s", resp.status, body[:300])
                        yield ("", True)
                        return

                    async for line_bytes in resp.content:
                        line = line_bytes.decode("utf-8", "ignore").strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = obj.get("choices") or [{}]
                        delta = (choices[0].get("delta") or {}).get("content") or ""
                        if not delta:
                            continue
                        raw += delta
                        partial = strip_scaffolding(raw)
                        if partial:
                            # Capitalize as we go; the terminal period waits
                            # for completion so it does not flicker in and out.
                            yield (partial[0].upper() + partial[1:], False)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.error("translation failed for %r: %s", text, exc)
                final_err = postprocess(raw, text, target=target) if raw else ""
                yield (final_err, True)
                return

            final = postprocess(raw, text, target=target)
            if final:
                final = self._strip_echoed_context(final, direction)
                self.remember(text, final, direction)
            yield (final, True)

    def _strip_echoed_context(self, line: str, direction: str = "en2pt") -> str:
        """Safety net: drop a previous translation the model prepended anyway."""
        for _prior_src, prior_pt in self._contexts[direction]:
            if prior_pt and line.startswith(prior_pt):
                trimmed = line[len(prior_pt):].strip()
                if trimmed:
                    log.warning("model echoed prior line; trimmed %r", prior_pt)
                    return trimmed
        return line

    async def translate(self, text: str, direction: str = "en2pt") -> str:
        """Non-streaming convenience wrapper, used by tests."""
        final = ""
        async for line, is_final in self.translate_stream(text, direction):
            if is_final:
                final = line
        return final
