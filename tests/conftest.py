import sys
import tempfile
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
    # Disabled AND pointed at a throwaway directory. Belt and braces: a test
    # run must never write into logs/transcripts/, and nothing in the test
    # suite may ever have a reason to clear it -- real sessions live there.
    config._data.setdefault("transcript", {})["enabled"] = False
    config._data["transcript"]["dir"] = tempfile.mkdtemp(prefix="lt-test-transcripts-")
    return config
