"""Config loading. Single source of truth is config.toml at the project root."""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Overlay wins; nested tables merge key by key rather than replacing.

    So a mode can add three acronyms without restating the whole list.
    """
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


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
                f"{p.name} not found at {p}. LiveTranslate needs it to start."
            )
        with open(p, "rb") as fh:
            data = tomllib.load(fh)

        # A config may declare `extends = "config.toml"` and override only what
        # differs. That keeps the shared settings -- thresholds, model ids,
        # timings -- in one file, so tuning the base does not silently leave a
        # second mode behind on stale values.
        base_name = data.pop("extends", None)
        if base_name:
            base_path = Path(base_name)
            if not base_path.is_absolute():
                base_path = p.parent / base_path
            if not base_path.exists():
                raise FileNotFoundError(
                    f"{p.name} extends {base_name}, which does not exist at {base_path}."
                )
            base = cls.load(base_path)
            data = _deep_merge(base._data, data)

        return cls(data, p)

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
