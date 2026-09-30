"""Filesystem and shell guardrails for the tools Claude can call."""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT_CHARS = 4000
MAX_FILE_BYTES = 200_000


class SandboxError(Exception):
    pass


@dataclass(frozen=True)
class Sandbox:
    project_root: Path
    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]

    def _resolve(self, rel: str, roots: tuple[Path, ...]) -> Path:
        candidate = Path(rel).expanduser()
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        resolved = candidate.resolve()
        for root in roots:
            root = root.resolve()
            if resolved == root or resolved.is_relative_to(root):
                return resolved
        allowed = ", ".join(self.display(r) for r in roots)
        raise SandboxError(f"path {rel!r} is outside the allowed directories ({allowed})")

    def readable(self, rel: str) -> Path:
        return self._resolve(rel, self.read_roots)

    def writable(self, rel: str) -> Path:
        return self._resolve(rel, self.write_roots)

    def display(self, path: Path) -> str:
        path = path.resolve()
        root = self.project_root.resolve()
        return str(path.relative_to(root)) if path.is_relative_to(root) else str(path)


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} chars truncated] ...\n{text[-half:]}"


async def run_command(command: str, cwd: Path, timeout: float = 60) -> str:
    """Run a shell command in its own process group; kill the whole group on timeout."""
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
        return f"[timed out after {timeout:g}s; process killed]"
    text = out.decode(errors="replace")
    return f"[exit code {proc.returncode}]\n{truncate(text)}"
