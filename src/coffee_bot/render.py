"""Menu → HTML (Jinja) → screenshot (Playwright/Chromium) → TRMNL-ready grayscale images."""

from __future__ import annotations

import asyncio
import io
import struct
import zlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from PIL import Image
from playwright.async_api import Browser, Playwright, async_playwright

from .models import Menu
from .store import atomic_write_bytes

# TRMNL OG: 7.5" panel, 800x480 landscape. 2-bit mode (firmware >= 1.6) shows 4 grays.
WIDTH, HEIGHT = 800, 480
GRAY_LEVELS = (0, 85, 170, 255)

PNG_2BIT = "menu.png"
PNG_1BIT = "menu-1bit.png"
BMP_1BIT = "menu-1bit.bmp"


@dataclass
class RenderResult:
    preview_png: bytes  # 8-bit grayscale PNG of the 4-level image, for sending to Telegram
    paths: list[Path]


def quantize_4gray(img: Image.Image) -> Image.Image:
    """Snap to the 4 panel grays (nearest level, no dithering, so text stays crisp)."""
    return img.convert("L").point(lambda v: GRAY_LEVELS[min(3, (v + 42) // 85)])


def to_1bit(img: Image.Image) -> Image.Image:
    return img.convert("L").point(lambda v: 255 if v >= 128 else 0).convert("1", dither=Image.Dither.NONE)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def encode_png_gray2(img: Image.Image) -> bytes:
    """Encode a 4-level L image as a true 2-bit grayscale PNG (color type 0, bit depth 2).

    Pillow can only write 2-bit PNGs in palette mode, so we pack pixels with Pillow's P;2
    packer and write the (standard, fully valid) PNG container ourselves.
    """
    w, h = img.size
    indices = img.point(lambda v: min(3, (v + 42) // 85))
    packed = Image.frombytes("P", (w, h), indices.tobytes()).tobytes("raw", "P;2")
    stride = (w + 3) // 4
    raw = b"".join(b"\x00" + packed[y * stride : (y + 1) * stride] for y in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 2, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


def _png_bytes(img: Image.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def _fmt_date(d: date) -> str:
    return f"{d.strftime('%b')} {d.day}"


def _fmt_num(v: float | None) -> str:
    if v is None:
        return ""
    return f"{v:g}"


class Renderer:
    """Owns one headless Chromium for the process lifetime; renders are serialized."""

    def __init__(self, templates_dir: Path, output_dir: Path):
        self.templates_dir = templates_dir
        self.output_dir = output_dir
        self._env = Environment(
            loader=FileSystemLoader(templates_dir),
            autoescape=select_autoescape(["html", "j2"]),
            undefined=StrictUndefined,
            auto_reload=True,  # pick up template edits made by the bot
        )
        self._env.filters["short_date"] = _fmt_date
        self._env.filters["num"] = _fmt_num
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._browser is None:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch()

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    def render_html(self, menu: Menu, today: date) -> str:
        template = self._env.get_template("menu.html.j2")
        return template.render(menu=menu, today=today, width=WIDTH, height=HEIGHT)

    async def screenshot(self, html: str) -> Image.Image:
        await self.start()
        assert self._browser is not None
        page = await self._browser.new_page(viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1)
        try:
            await page.set_content(html, wait_until="load")
            await page.evaluate("document.fonts.ready")
            png = await page.screenshot(type="png", full_page=False)
        finally:
            await page.close()
        return Image.open(io.BytesIO(png))

    async def render(self, menu: Menu, today: date) -> RenderResult:
        """Render the menu and atomically publish all output images."""
        async with self._lock:
            html = self.render_html(menu, today)
            shot = await self.screenshot(html)
            gray = quantize_4gray(shot)
            mono = to_1bit(gray)

            outputs = {
                self.output_dir / PNG_2BIT: encode_png_gray2(gray),
                self.output_dir / PNG_1BIT: _png_bytes(mono),
                self.output_dir / BMP_1BIT: _png_bytes(mono, "BMP"),
            }
            for path, data in outputs.items():
                atomic_write_bytes(path, data)
            return RenderResult(preview_png=_png_bytes(gray), paths=list(outputs))
