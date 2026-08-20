"""Visual-verification harness for spec section 12, test 7.

Serves the real display page and pushes representative Portuguese lines over
the real websocket, so screenshots exercise the production render path rather
than a mock. Text is genuine output from the translation model.

    python scripts/preview.py [--hold SECONDS]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livetranslate.config import Config
from livetranslate.server import DisplayServer

# Real model output. The last line is a worst case: a full 14-word chunk.
# (text, direction). pt2en lines are someone in the room, translated back.
LINES = [
    ("Verifique a versão do firmware na porta USB antes de continuar.", "en2pt"),
    ("Professor, I have a question about the navigation system. When will you "
     "show the gimbal calibration?", "pt2en"),
]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold", type=float, default=600.0)
    ap.add_argument("--paused", action="store_true", help="render the paused state")
    ap.add_argument("--backlog", type=int, default=0, help="render a queue depth")
    args = ap.parse_args()

    cfg = Config.load()
    server = DisplayServer(cfg)
    url = await server.start()
    print(f"preview at {url}", flush=True)

    # Wait for the browser to attach before sending anything.
    for _ in range(600):
        if server.client_count:
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.3)

    await server.send_status(paused=args.paused, translating=False, listening=True)
    await server.send_level(0.42)
    await server.send_backlog(args.backlog)
    await server.send_english(
        "do not touch that connector it is still live the fuselage has a crack",
        "near the gimbal mount",
        "" if not args.paused else "",
    )

    for seq, (line, direction) in enumerate(LINES, start=1):
        # Stream it in the same way the translator does: cumulative text.
        for i in range(1, len(line) + 1):
            await server.send_line(seq, line[:i], i == len(line), direction)
            await asyncio.sleep(0.004)
        await asyncio.sleep(0.25)

    print("READY", flush=True)
    await asyncio.sleep(args.hold)
    await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
