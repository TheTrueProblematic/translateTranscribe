"""Config loading. Single source of truth is config.toml at the project root."""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"


class Config:
    """Dotted-path access over the parsed TOML, with defaults.

    cfg.get("chunker.max_words", 14) keeps call sites readable and means a
    partially-filled config.toml still boots instead of raising KeyError.
    """

    def __init__(self, data: dict[str, Any], path: Path | None = None):
        self._data = data
        self.path = path

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        p = Path(path) if path else DEFAULT_CONFIG_PATH
        if not p.exists():
            raise FileNotFoundError(
                f"config.toml not found at {p}. LiveTranslate needs it to start."
            )
        with open(p, "rb") as fh:
            return cls(tomllib.load(fh), p)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        val = self.get(name, {})
        return val if isinstance(val, dict) else {}

    def __contains__(self, dotted: str) -> bool:
        sentinel = object()
        return self.get(dotted, sentinel) is not sentinel
