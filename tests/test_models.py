import pytest
from pydantic import ValidationError

from coffee_bot.models import Coffee, Menu


def coffee(**overrides):
    data = dict(id="x", roaster="R", name="N", roast_date="2026-09-01", container="black")
    data.update(overrides)
    return data


def test_roast_date_required():
    data = coffee()
    del data["roast_date"]
    with pytest.raises(ValidationError, match="roast_date"):
        Coffee.model_validate(data)


def test_container_enum():
    with pytest.raises(ValidationError, match="container"):
        Coffee.model_validate(coffee(container="red"))


def test_max_four_coffees():
    with pytest.raises(ValidationError, match="at most 4"):
        Menu.model_validate({"coffees": [coffee(id=f"c{i}") for i in range(5)]})


def test_unique_ids():
    with pytest.raises(ValidationError, match="unique"):
        Menu.model_validate({"coffees": [coffee(), coffee()]})


def test_new_id_dedupes(sample_menu):
    menu = Menu(coffees=[Coffee.model_validate(coffee(id="onyx-geisha"))])
    assert menu.new_id("Onyx", "Geisha") == "onyx-geisha-2"


def test_find_method_case_insensitive(sample_menu):
    assert sample_menu.coffees[0].find_method("v60").method == "V60"
