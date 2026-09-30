import asyncio
from zoneinfo import ZoneInfo

import pytest

from coffee_bot.config import TEMPLATES_DIR
from coffee_bot.sandbox import Sandbox
from coffee_bot.store import MenuStore
from coffee_bot.tools import Services, build_tools


class FakeRenderer:
    def __init__(self):
        self.calls = 0

    async def render(self, menu, today):
        self.calls += 1
        return type("R", (), {"preview_png": b"png", "paths": []})()


class FakeChat:
    def __init__(self, approve=True):
        self.approve = approve
        self.photos = []
        self.approvals = []

    async def send_photo(self, png, caption=None):
        self.photos.append(caption)

    async def request_approval(self, command, reason):
        self.approvals.append(command)
        return self.approve


@pytest.fixture
def env(tmp_path):
    (tmp_path / "templates").mkdir()
    svc = Services(
        store=MenuStore(tmp_path / "data"),
        renderer=FakeRenderer(),
        sandbox=Sandbox(tmp_path, (tmp_path / "templates", tmp_path / "data"), (tmp_path / "templates",)),
        tz=ZoneInfo("UTC"),
    )
    chat = FakeChat()
    tools = {t.name: t for t in build_tools(svc, chat)}
    return svc, chat, tools


async def call(tools, tool_name, **kwargs):
    return await tools[tool_name].call(kwargs)


async def test_add_update_brew_flow(env):
    svc, chat, tools = env
    out = await call(tools, "add_coffee", roaster="Onyx", name="Geisha", roast_date="2026-09-20",
                     container="blue", tasting_notes=["jasmine"])
    assert "onyx-geisha" in out
    await call(tools, "upsert_brew_method", coffee_id="onyx-geisha", method="V60", dose_g=15, water_g=250,
               params={"bloom": "45g"})
    await call(tools, "upsert_brew_method", coffee_id="onyx-geisha", method="v60", status="dialed", grind="22")
    menu = svc.store.load()
    brew = menu.coffees[0].brew_methods
    assert len(brew) == 1
    assert (brew[0].dose_g, brew[0].grind, brew[0].status, brew[0].params) == (15, "22", "dialed", {"bloom": "45g"})
    assert svc.renderer.calls == 3


async def test_add_rejects_bad_date_and_full_menu(env):
    svc, chat, tools = env
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        await call(tools, "add_coffee", roaster="A", name="B", roast_date="last week", container="black", tasting_notes=[])
    for i in range(4):
        await call(tools, "add_coffee", roaster="R", name=f"C{i}", roast_date="2026-09-01", container="black", tasting_notes=[])
    with pytest.raises(ValueError, match="menu is full"):
        await call(tools, "add_coffee", roaster="R", name="C5", roast_date="2026-09-01", container="black", tasting_notes=[])


async def test_write_file_sandboxed(env):
    svc, chat, tools = env
    await call(tools, "write_file", path="templates/x.css", content="body{}")
    assert (svc.sandbox.project_root / "templates" / "x.css").read_text() == "body{}"
    with pytest.raises(Exception, match="outside"):
        await call(tools, "write_file", path="src/evil.py", content="")


async def test_shell_requires_approval(env):
    svc, chat, tools = env
    chat.approve = False
    out = await call(tools, "run_shell_command", command="touch should-not-exist", reason="test")
    assert "declined" in out
    assert not (svc.sandbox.project_root / "should-not-exist").exists()
    chat.approve = True
    out = await call(tools, "run_shell_command", command="echo ok", reason="test")
    assert "ok" in out and chat.approvals == ["touch should-not-exist", "echo ok"]
