import pytest

from coffee_bot.sandbox import Sandbox, SandboxError, run_command


@pytest.fixture
def sandbox(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "data").mkdir()
    return Sandbox(tmp_path, read_roots=(tmp_path / "templates", tmp_path / "data"), write_roots=(tmp_path / "templates",))


def test_allows_inside(sandbox, tmp_path):
    assert sandbox.writable("templates/menu.css") == (tmp_path / "templates" / "menu.css").resolve()
    assert sandbox.readable("data/menu.json") == (tmp_path / "data" / "menu.json").resolve()


@pytest.mark.parametrize("path", ["../etc/passwd", "templates/../../x", "/etc/passwd", "~/.ssh/id_rsa", "src/x.py"])
def test_rejects_escapes(sandbox, path):
    with pytest.raises(SandboxError):
        sandbox.readable(path)


def test_data_is_read_only(sandbox):
    with pytest.raises(SandboxError):
        sandbox.writable("data/menu.json")


def test_symlink_escape(sandbox, tmp_path):
    (tmp_path / "templates" / "link").symlink_to("/etc")
    with pytest.raises(SandboxError):
        sandbox.readable("templates/link/passwd")


async def test_run_command(tmp_path):
    out = await run_command("echo hi && pwd", cwd=tmp_path)
    assert "[exit code 0]" in out and "hi" in out


async def test_run_command_timeout(tmp_path):
    out = await run_command("sleep 5", cwd=tmp_path, timeout=0.5)
    assert "timed out" in out
