"""TRMNL BYOS ("bring your own server") HTTP endpoints, served by aiohttp in the bot's event loop.

Device protocol (TRMNL firmware):
  GET  /api/setup    ID: <MAC>                        -> {"status": 200, api_key, friendly_id, image_url, ...}
  GET  /api/display  ID, Access-Token, telemetry hdrs -> {"status": 0|202|500, image_url, filename, refresh_rate, ...}
  POST /api/log      Access-Token, {"logs": [...]}     -> 204
Plus GET /images/{filename} (the image_url target), /healthz and /preview.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Awaitable, Callable, Literal, Protocol
from zoneinfo import ZoneInfo

from aiohttp import web

from .devices import MAX_LOGS, Device, DeviceRegistry, normalize_mac
from .models import now_utc
from .render import MAX_DEVICE_IMAGE_BYTES, CachedImage, ImageCache

log = logging.getLogger(__name__)

ImageFormat = Literal["auto", "2bit", "1bit", "bmp"]
PENDING_REFRESH_SECONDS = 30
TWO_BIT_MIN_FIRMWARE = (1, 6, 0)

PAIRING_TITLE = "Almost there"
PAIRING_BODY = "This display is waiting for approval. Check Telegram and tap Approve."
UNAVAILABLE_TITLE = "Menu unavailable"
UNAVAILABLE_BODY = "The coffee menu couldn't be rendered. Check the coffee-bot logs."


class DeviceNotifier(Protocol):
    """Implemented by the Telegram bot."""

    async def notify_pairing(self, device: Device) -> None: ...
    async def notify(self, text: str) -> None: ...


class MessageRenderer(Protocol):
    async def render_message(self, title: str, body: str) -> CachedImage: ...


def _parse_version(value: str | None) -> tuple[int, ...] | None:
    if not value:
        return None
    parts = re.findall(r"\d+", value)
    return tuple(int(p) for p in parts[:3]) if parts else None


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _int(value: str | None) -> int | None:
    f = _float(value)
    return int(f) if f is not None else None


def _bool(value: str | None) -> bool | None:
    if value in (None, ""):
        return None
    return value.strip().lower() in ("1", "true", "yes")


def _header(request: web.Request, name: str) -> str | None:
    # Firmware sends "Access-Token"; some tools send "ACCESS_TOKEN". Headers are case-insensitive.
    return request.headers.get(name) or request.headers.get(name.replace("-", "_"))


class DeviceServer:
    def __init__(
        self,
        registry: DeviceRegistry,
        images: ImageCache,
        messages: MessageRenderer,
        notifier: DeviceNotifier,
        tz: ZoneInfo,
        *,
        public_base_url: str | None = None,
        image_format: ImageFormat = "auto",
        low_battery_volts: float = 3.5,
    ):
        self.registry = registry
        self.images = images
        self.messages = messages
        self.notifier = notifier
        self.tz = tz
        self.public_base_url = public_base_url.rstrip("/") if public_base_url else None
        self.image_format = image_format
        self.low_battery_volts = low_battery_volts
        self._runner: web.AppRunner | None = None
        self._background: set[asyncio.Task] = set()

        self.app = web.Application()
        self.app.add_routes([
            web.get("/api/setup", self.setup),
            web.get("/api/display", self.display),
            web.post("/api/log", self.log),
            web.get("/images/{filename}", self.image),
            web.get("/preview", self.preview),
            web.get("/healthz", self.healthz),
        ])

    # ---- lifecycle ----

    async def start(self, host: str, port: int) -> None:
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, host, port).start()
        log.info("TRMNL server listening on http://%s:%d", host, port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    def _spawn(self, make: Callable[[], Awaitable[None]], what: str) -> None:
        """Run a notification in the background so the device's HTTP request isn't held up by Telegram."""

        async def runner() -> None:
            try:
                await make()
            except Exception:
                log.exception("failed to %s", what)

        task = asyncio.create_task(runner())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ---- helpers ----

    def _base_url(self, request: web.Request) -> str:
        return self.public_base_url or f"{request.scheme}://{request.host}"

    def _image_url(self, request: web.Request, image: CachedImage) -> str:
        return f"{self._base_url(request)}/images/{image.filename}"

    def choose_image(self, fw_version: str | None) -> CachedImage | None:
        latest = self.images.latest
        if not latest:
            return None
        variant = self.image_format
        if variant == "auto":
            version = _parse_version(fw_version)
            variant = "2bit" if version is not None and version >= TWO_BIT_MIN_FIRMWARE else "1bit"
        image = latest.get(variant) or latest.get("1bit")
        if image and len(image.data) > MAX_DEVICE_IMAGE_BYTES and variant != "1bit":
            log.warning("%s image is %d bytes (> %d); serving 1-bit", variant, len(image.data), MAX_DEVICE_IMAGE_BYTES)
            image = latest.get("1bit")
        return image

    async def _menu_or_notice(self, fw_version: str | None) -> CachedImage:
        return self.choose_image(fw_version) or await self.messages.render_message(UNAVAILABLE_TITLE, UNAVAILABLE_BODY)

    # ---- device endpoints ----

    async def setup(self, request: web.Request) -> web.Response:
        mac_header = _header(request, "ID")
        fw, model = _header(request, "FW-Version"), _header(request, "Model")
        not_registered = {"status": 404, "api_key": None, "friendly_id": None, "image_url": None, "filename": None}
        if not mac_header:
            return web.json_response(not_registered, status=404)

        async with self.registry.lock:
            device = self.registry.by_mac(mac_header)
            is_new = device is None
            if is_new:
                device = self.registry.add_pending(mac_header, fw, model)
                self.registry.save()
        log.info("setup from %s (%s): %s", device.mac, device.friendly_id, "new" if is_new else device.status)

        if device.status == "denied":
            return web.json_response(not_registered, status=404)
        if is_new:
            self._spawn(lambda: self.notifier.notify_pairing(device), "send pairing request")

        if device.status == "approved":
            image, message = await self._menu_or_notice(fw), "Coffee menu connected"
        else:
            image, message = await self.messages.render_message(PAIRING_TITLE, PAIRING_BODY), "Waiting for approval"
        return web.json_response({
            "status": 200,
            "api_key": device.api_key,
            "friendly_id": device.friendly_id,
            "image_url": self._image_url(request, image),
            "filename": image.filename,
            "message": message,
        })

    async def display(self, request: web.Request) -> web.Response:
        token = _header(request, "Access-Token") or ""
        mac_header = _header(request, "ID")
        device = self.registry.by_token(token)
        if device is None or (mac_header and normalize_mac(mac_header) != device.mac) or device.status == "denied":
            # status 500 makes the firmware wipe its credentials and run /api/setup again.
            log.warning("display from unknown/denied device (ID=%s)", mac_header)
            return web.json_response({"status": 500, "error": "Device not found"})

        if device.status == "pending":
            return web.json_response({"status": 202, "image_url": None, "filename": None,
                                      "refresh_rate": PENDING_REFRESH_SECONDS})

        fw = _header(request, "FW-Version")
        image = await self._menu_or_notice(fw or device.fw_version)
        refresh = self.registry.settings.refresh_seconds(datetime.now(self.tz))

        async with self.registry.lock:
            device.last_seen = now_utc()
            device.fw_version = fw or device.fw_version
            device.model = _header(request, "Model") or device.model
            device.battery_voltage = _float(_header(request, "Battery-Voltage"))
            device.battery_charging = _bool(_header(request, "Battery-Charging"))
            device.usb_connected = _bool(_header(request, "USB-Connected"))
            device.rssi = _int(_header(request, "RSSI"))
            device.last_filename = image.filename
            device.last_refresh_seconds = refresh
            alert = device.check_low_battery(self.low_battery_volts)
            self.registry.save()

        if alert:
            text = (f"🔋 TRMNL {device.friendly_id} battery is low: {device.battery_voltage:.2f} V "
                    f"(≈{device.battery_percent()}%). Time to charge it.")
            self._spawn(lambda: self.notifier.notify(text), "send low-battery alert")

        log.info("display %s: %s, sleep %ss, battery %s V", device.friendly_id, image.filename, refresh,
                 device.battery_voltage)
        return web.json_response({
            "status": 0,
            "image_url": self._image_url(request, image),
            "image_url_timeout": 0,
            "filename": image.filename,
            "refresh_rate": refresh,
            "reset_firmware": False,
            "update_firmware": False,
            "firmware_url": None,
            "special_function": "none",
        })

    async def log(self, request: web.Request) -> web.Response:
        device = self.registry.by_token(_header(request, "Access-Token") or "")
        if device is None:
            return web.json_response({"error": "unknown device"}, status=401)
        try:
            payload = await request.json()
            entries = payload.get("logs") or []
        except Exception:
            return web.json_response({"error": "expected JSON body {\"logs\": [...]}"}, status=400)
        if not isinstance(entries, list):
            entries = [entries]

        keep = ("created_at", "level", "message", "source_path", "source_line", "wake_reason",
                "battery_voltage", "wifi_signal", "firmware_version", "refresh_rate", "retry")
        async with self.registry.lock:
            for entry in entries:
                if isinstance(entry, dict):
                    device.logs.append({k: entry[k] for k in keep if k in entry})
                    log.info("device %s log: %s", device.friendly_id, entry.get("message"))
            del device.logs[:-MAX_LOGS]
            self.registry.save()
        return web.Response(status=204)

    # ---- images & utilities ----

    async def image(self, request: web.Request) -> web.Response:
        image = self.images.get(request.match_info["filename"])
        if image is None:
            raise web.HTTPNotFound(text="unknown image")
        # Exact Content-Type matters: the firmware picks the PNG decoder from it. aiohttp sets Content-Length.
        return web.Response(body=image.data, content_type=image.content_type,
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})

    async def preview(self, request: web.Request) -> web.Response:
        image = self.images.latest.get("2bit")
        if image is None:
            raise web.HTTPServiceUnavailable(text="nothing rendered yet")
        return web.Response(body=image.data, content_type=image.content_type, headers={"Cache-Control": "no-store"})

    async def healthz(self, request: web.Request) -> web.Response:
        latest = self.images.latest.get("2bit")
        return web.json_response({
            "ok": True,
            "menu_image": latest.filename if latest else None,
            "devices": {d.friendly_id: d.status for d in self.registry.devices},
        })
