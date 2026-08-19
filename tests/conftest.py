import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from livetranslate.config import Config


@pytest.fixture(scope="session")
def cfg():
    """The real config, with per-run transcript files turned off.

    Tests build many pipelines; without this each one would drop a pair of
    files into logs/transcripts/ and bury the real sessions.
    """
    config = Config.load()
    config._data.setdefault("transcript", {})["enabled"] = False
    return config
