from datetime import date

import pytest
from PIL import Image

from coffee_bot.config import TEMPLATES_DIR
from coffee_bot.render import BMP_1BIT, HEIGHT, PNG_1BIT, PNG_2BIT, WIDTH, Renderer, encode_png_gray2

TODAY = date(2026, 9, 30)


def test_encode_png_gray2_roundtrip():
    img = Image.new("L", (6, 3))
    img.putdata([0, 85, 170, 255, 255, 0] * 3)
    reopened = Image.open(__import__("io").BytesIO(encode_png_gray2(img)))
    reopened.load()
    assert reopened.size == (6, 3)
    assert list(reopened.convert("L").get_flattened_data()) == [0, 85, 170, 255, 255, 0] * 3


def test_html_includes_roast_age(sample_menu):
    html = Renderer(TEMPLATES_DIR, TEMPLATES_DIR).render_html(sample_menu, TODAY)
    assert "Roasted Sep 12 · 18d" in html
    assert "Southern Weather" in html


@pytest.fixture
async def renderer(tmp_path):
    r = Renderer(TEMPLATES_DIR, tmp_path)
    try:
        await r.start()
    except Exception as exc:  # browser not installed
        pytest.skip(f"Chromium unavailable: {exc}")
    yield r
    await r.close()


@pytest.mark.parametrize("count", [0, 1, 2, 3, 4])
async def test_render_outputs(renderer, tmp_path, sample_menu, count):
    menu = sample_menu.model_copy(update={"coffees": sample_menu.coffees[:count]})
    await renderer.render(menu, TODAY)

    two = Image.open(tmp_path / PNG_2BIT)
    assert two.size == (WIDTH, HEIGHT)
    assert two.mode == "L"
    assert set(two.get_flattened_data()) <= {0, 85, 170, 255}

    one = Image.open(tmp_path / PNG_1BIT)
    assert one.size == (WIDTH, HEIGHT) and one.mode == "1"
    bmp = Image.open(tmp_path / BMP_1BIT)
    assert bmp.size == (WIDTH, HEIGHT) and bmp.mode == "1"
