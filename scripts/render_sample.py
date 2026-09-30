"""Render a menu JSON to PNGs without Telegram, for iterating on the template.

Usage:
    python scripts/render_sample.py                         # fixture, 1-4 coffee variants -> output/samples/
    python scripts/render_sample.py data/menu.json          # render a specific menu file
    python scripts/render_sample.py --today 2026-10-01 ...  # pretend it's another day
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coffee_bot.config import TEMPLATES_DIR  # noqa: E402
from coffee_bot.models import Menu  # noqa: E402
from coffee_bot.render import Renderer  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "sample_menu.json"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("menu", nargs="?", type=Path, help="menu JSON (default: fixture, rendered with 0-4 coffees)")
    ap.add_argument("--today", type=date.fromisoformat, default=date.today())
    ap.add_argument("--out", type=Path, default=ROOT / "output" / "samples")
    args = ap.parse_args()

    base = Menu.model_validate_json((args.menu or FIXTURE).read_text())
    variants = {"menu": base} if args.menu else {
        f"{n}-coffees": base.model_copy(update={"coffees": base.coffees[:n]}) for n in range(0, len(base.coffees) + 1)
    }

    for name, menu in variants.items():
        renderer = Renderer(TEMPLATES_DIR, args.out / name)
        try:
            result = await renderer.render(menu, args.today)
        finally:
            await renderer.close()
        (args.out / f"{name}-preview.png").write_bytes(result.preview_png)
        print(f"{name}: {args.out / f'{name}-preview.png'}")


if __name__ == "__main__":
    asyncio.run(main())
