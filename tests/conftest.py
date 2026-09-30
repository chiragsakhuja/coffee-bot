from __future__ import annotations

import json
from pathlib import Path

import pytest

from coffee_bot.models import Menu

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_menu() -> Menu:
    return Menu.model_validate(json.loads((FIXTURES / "sample_menu.json").read_text()))
