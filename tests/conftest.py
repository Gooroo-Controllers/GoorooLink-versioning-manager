import json
from pathlib import Path

import pytest


@pytest.fixture
def valid_registry():
    fixtures_dir = Path(__file__).parent / "fixtures"
    with open(fixtures_dir / "valid_registry.json", encoding="utf-8") as f:
        return json.load(f)
