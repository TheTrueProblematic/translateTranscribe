"""Rolling debug log (spec sections 5, 8, 9).

Everything the audience must never see -- the English transcript, gated
rejections, feminine-agreement leaks, latency samples -- goes here.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path


def setup_logging(cfg) -> Path:
    log_dir = Path(cfg.get("logging.dir", "logs"))
    if not log_dir.is_absolute():
        log_dir = Path(__file__).resolve().parent.parent / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "livetranslate.log"

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(cfg.get("logging.level", "INFO")).upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=int(cfg.get("logging.max_bytes", 5_000_000)),
        backupCount=int(cfg.get("logging.backups", 3)),
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-26s %(message)s"
    ))
    root.addHandler(handler)

    # The console stays quiet unless something is actually wrong: the launcher
    # must "print nothing else unless something is wrong" (spec section 10).
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.addHandler(console)

    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    return log_path
