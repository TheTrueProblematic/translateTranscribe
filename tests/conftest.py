import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from livetranslate.config import Config


@pytest.fixture(scope="session")
def cfg():
    return Config.load()
