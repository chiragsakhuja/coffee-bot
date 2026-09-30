from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

MAX_COFFEES = 4

Container = Literal["black", "white", "green", "blue"]
BrewStatus = Literal["dialing_in", "dialed", "trying"]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "coffee"


class BrewMethod(BaseModel):
    """One way of brewing a coffee. Common fields are typed; anything else goes in `params`."""

    method: str
    status: BrewStatus = "trying"
    dose_g: float | None = None
    yield_g: float | None = None
    water_g: float | None = None
    ratio: str | None = None
    grind: str | None = None
    temp_c: float | None = None
    time: str | None = None
    params: dict[str, str] = Field(default_factory=dict)
    notes: str | None = None
    updated_at: datetime = Field(default_factory=now_utc)


class Coffee(BaseModel):
    id: str
    roaster: str
    name: str
    roast_date: date
    container: Container
    tasting_notes: list[str] = Field(default_factory=list)
    origin: str | None = None
    process: str | None = None
    roast_level: str | None = None
    notes: str | None = None
    brew_methods: list[BrewMethod] = Field(default_factory=list)
    added_at: datetime = Field(default_factory=now_utc)

    def find_method(self, method: str) -> BrewMethod | None:
        key = method.strip().lower()
        return next((m for m in self.brew_methods if m.method.strip().lower() == key), None)


class Menu(BaseModel):
    title: str = "Coffee Menu"
    coffees: list[Coffee] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=now_utc)

    @field_validator("coffees")
    @classmethod
    def _limit(cls, coffees: list[Coffee]) -> list[Coffee]:
        if len(coffees) > MAX_COFFEES:
            raise ValueError(f"the menu holds at most {MAX_COFFEES} coffees")
        ids = [c.id for c in coffees]
        if len(ids) != len(set(ids)):
            raise ValueError("coffee ids must be unique")
        return coffees

    def get(self, coffee_id: str) -> Coffee:
        for c in self.coffees:
            if c.id == coffee_id:
                return c
        known = ", ".join(c.id for c in self.coffees) or "none"
        raise KeyError(f"no coffee with id {coffee_id!r} (known ids: {known})")

    def new_id(self, roaster: str, name: str) -> str:
        base = slugify(f"{roaster} {name}")
        taken = {c.id for c in self.coffees}
        candidate, n = base, 2
        while candidate in taken:
            candidate, n = f"{base}-{n}", n + 1
        return candidate
