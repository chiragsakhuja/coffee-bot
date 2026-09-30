from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .models import Menu, now_utc

HISTORY_LIMIT = 100


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write via a temp file in the same directory + os.replace, so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class MenuStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "menu.json"
        self.history_dir = data_dir / "history"

    def load(self) -> Menu:
        if not self.path.exists():
            return Menu()
        return Menu.model_validate_json(self.path.read_text())

    def save(self, menu: Menu) -> None:
        """Snapshot the current file into history, then atomically write the new menu."""
        menu = Menu.model_validate(menu.model_dump())  # re-run validators
        menu.updated_at = now_utc()
        if self.path.exists():
            self.history_dir.mkdir(parents=True, exist_ok=True)
            stamp = now_utc().strftime("%Y%m%dT%H%M%S%f")
            (self.history_dir / f"menu-{stamp}.json").write_bytes(self.path.read_bytes())
            self._prune()
        atomic_write_bytes(self.path, menu.model_dump_json(indent=2).encode())

    def undo(self) -> Menu | None:
        """Restore the most recent snapshot. Returns the restored menu, or None if there's no history."""
        snapshots = self._snapshots()
        if not snapshots:
            return None
        latest = snapshots[-1]
        menu = Menu.model_validate_json(latest.read_text())
        atomic_write_bytes(self.path, latest.read_bytes())
        latest.unlink()
        return menu

    def _snapshots(self) -> list[Path]:
        if not self.history_dir.exists():
            return []
        return sorted(self.history_dir.glob("menu-*.json"))

    def _prune(self) -> None:
        for old in self._snapshots()[:-HISTORY_LIMIT]:
            old.unlink(missing_ok=True)
