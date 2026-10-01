import io
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from coffee_bot.devices import DeviceRegistry
from coffee_bot.render import ImageCache, cached_image
from coffee_bot.server import DeviceServer

MAC = "AA:BB:CC:DD:EE:FF"


def png(mode):
    buf = io.BytesIO()
    Image.new(mode, (800, 480), 255).save(buf, "PNG")
    return buf.getvalue()


class FakeNotifier:
    def __init__(self):
        self.pairings, self.messages = [], []

    async def notify_pairing(self, device):
        self.pairings.append(device.friendly_id)

    async def notify(self, text):
        self.messages.append(text)


class FakeMessages:
    def __init__(self, cache):
        self.cache = cache

    async def render_message(self, title, body):
        return self.cache.put(cached_image("msg", "1b.png", f"{title}|{body}".encode(), "image/png"))


@pytest.fixture
def cache():
    c = ImageCache()
    c.publish({
        "2bit": cached_image("menu", "2b.png", png("L") + b"2", "image/png"),
        "1bit": cached_image("menu", "1b.png", png("1"), "image/png"),
        "bmp": cached_image("menu", "1b.bmp", b"BM" + b"\0" * 48060, "image/bmp"),
    })
    return c


@pytest.fixture
async def ctx(tmp_path, cache):
    registry = DeviceRegistry(tmp_path)
    notifier = FakeNotifier()
    server = DeviceServer(registry, cache, FakeMessages(cache), notifier, ZoneInfo("UTC"), low_battery_volts=3.5)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    yield client, server, registry, notifier
    await client.close()


async def drain(server):
    for task in list(server._background):
        await task


async def setup(client, mac=MAC):
    return await client.get("/api/setup", headers={"ID": mac, "FW-Version": "1.8.6", "Model": "og"})


def display_headers(key, mac=MAC, fw="1.8.6", battery="4.0", **extra):
    return {"ID": mac, "Access-Token": key, "FW-Version": fw, "Battery-Voltage": battery, "RSSI": "-61",
            "Refresh-Rate": "900", "Width": "800", "Height": "480", **extra}


async def test_pairing_flow(ctx, cache):
    client, server, registry, notifier = ctx

    resp = await setup(client)
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == 200 and body["api_key"] and len(body["friendly_id"]) == 6
    assert body["image_url"].startswith("http://") and "/images/msg-" in body["image_url"]
    await drain(server)
    assert notifier.pairings == [body["friendly_id"]]

    # Repeated setup reuses the device and doesn't notify again
    again = await (await setup(client)).json()
    await drain(server)
    assert again["api_key"] == body["api_key"] and len(notifier.pairings) == 1

    # Pending -> 202
    pending = await (await client.get("/api/display", headers=display_headers(body["api_key"]))).json()
    assert pending["status"] == 202

    # Approve -> menu
    registry.set_status(body["friendly_id"], "approved")
    resp = await client.get("/api/display", headers=display_headers(body["api_key"]))
    data = await resp.json()
    assert data["status"] == 0
    assert data["filename"] == cache.latest["2bit"].filename and len(data["filename"]) <= 31
    assert data["image_url"].endswith("/images/" + data["filename"])
    assert isinstance(data["refresh_rate"], int) and data["refresh_rate"] == 900
    assert data["reset_firmware"] is False and data["update_firmware"] is False

    device = registry.by_mac(MAC)
    assert device.battery_voltage == 4.0 and device.rssi == -61 and device.last_seen is not None
    assert DeviceRegistry(registry.path.parent).by_mac(MAC).rssi == -61  # persisted


async def test_unknown_token_and_denied(ctx):
    client, server, registry, notifier = ctx
    data = await (await client.get("/api/display", headers=display_headers("bogus"))).json()
    assert data["status"] == 500

    body = await (await setup(client)).json()
    registry.set_status(body["friendly_id"], "denied")
    resp = await setup(client)
    assert resp.status == 404 and (await resp.json())["api_key"] is None
    data = await (await client.get("/api/display", headers=display_headers(body["api_key"]))).json()
    assert data["status"] == 500


async def test_mac_mismatch_rejected(ctx):
    client, server, registry, notifier = ctx
    body = await (await setup(client)).json()
    registry.set_status(body["friendly_id"], "approved")
    data = await (await client.get("/api/display", headers=display_headers(body["api_key"], mac="11:22:33:44:55:66"))).json()
    assert data["status"] == 500


@pytest.mark.parametrize("fw, image_format, variant", [
    ("1.8.6", "auto", "2bit"), ("1.5.9", "auto", "1bit"), (None, "auto", "1bit"),
    ("1.8.6", "bmp", "bmp"), ("1.8.6", "1bit", "1bit"),
])
async def test_variant_choice(ctx, cache, fw, image_format, variant):
    client, server, registry, notifier = ctx
    server.image_format = image_format
    assert server.choose_image(fw).filename == cache.latest[variant].filename


async def test_oversized_falls_back_to_1bit(ctx, cache):
    client, server, registry, notifier = ctx
    cache.publish({**cache.latest, "2bit": cached_image("menu", "2b.png", b"x" * 95_000, "image/png")})
    assert server.choose_image("1.8.6").filename == cache.latest["1bit"].filename


async def test_image_endpoint(ctx, cache):
    client, server, registry, notifier = ctx
    img = cache.latest["2bit"]
    resp = await client.get(f"/images/{img.filename}")
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "image/png"
    assert int(resp.headers["Content-Length"]) == len(img.data)
    assert await resp.read() == img.data
    bmp = await client.get(f"/images/{cache.latest['bmp'].filename}")
    assert bmp.headers["Content-Type"] == "image/bmp"
    assert (await client.get("/images/nope.png")).status == 404


async def test_public_base_url(ctx):
    client, server, registry, notifier = ctx
    server.public_base_url = "http://coffee.lan:9157"
    body = await (await setup(client)).json()
    assert body["image_url"].startswith("http://coffee.lan:9157/images/")


async def test_log_endpoint(ctx):
    client, server, registry, notifier = ctx
    body = await (await setup(client)).json()
    resp = await client.post("/api/log", headers={"Access-Token": body["api_key"]},
                             json={"logs": [{"message": "hello", "level": "info", "junk": "x"}]})
    assert resp.status == 204
    assert registry.by_mac(MAC).logs == [{"message": "hello", "level": "info"}]
    assert (await client.post("/api/log", headers={"Access-Token": "bad"}, json={"logs": []})).status == 401


async def test_low_battery_alert_once(ctx):
    client, server, registry, notifier = ctx
    body = await (await setup(client)).json()
    registry.set_status(body["friendly_id"], "approved")
    for _ in range(2):
        await client.get("/api/display", headers=display_headers(body["api_key"], battery="3.31"))
    await drain(server)
    assert len(notifier.messages) == 1 and "3.31 V" in notifier.messages[0]


async def test_night_mode_refresh(ctx):
    client, server, registry, notifier = ctx
    body = await (await setup(client)).json()
    registry.set_status(body["friendly_id"], "approved")
    now = datetime.now(ZoneInfo("UTC"))
    registry.settings.night_enabled = True
    registry.settings.night_start = (now - timedelta(hours=1)).strftime("%H:%M")
    registry.settings.night_end = (now + timedelta(hours=3)).strftime("%H:%M")
    data = await (await client.get("/api/display", headers=display_headers(body["api_key"]))).json()
    assert 3 * 3600 - 120 <= data["refresh_rate"] <= 3 * 3600 + 60


async def test_healthz_and_preview(ctx, cache):
    client, server, registry, notifier = ctx
    health = await (await client.get("/healthz")).json()
    assert health["ok"] and health["menu_image"] == cache.latest["2bit"].filename
    resp = await client.get("/preview")
    assert resp.headers["Content-Type"] == "image/png"
