"""TRMNL devices paired with this server, their telemetry, and the refresh schedule."""

from __future__ import annotations

import asyncio
import re
import secrets
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .models import now_utc
from .store import atomic_write_bytes

DeviceStatus = Literal["pending", "approved", "denied"]
MAX_LOGS = 50
MIN_REFRESH_SECONDS = 60
MAX_REFRESH_SECONDS = 86400
# OG LiPo: ~4.2 V full, ~3.0 V empty. Only a rough guide.
BATTERY_FULL_V = 4.2
BATTERY_EMPTY_V = 3.0
LOW_BATTERY_REARM_MARGIN_V = 0.2


def normalize_mac(mac: str) -> str:
    return mac.strip().upper().replace("-", ":")


def _parse_hhmm(value: str) -> time:
    if not re.fullmatch(r"\d{1,2}:\d{2}", value.strip()):
        raise ValueError(f"time must be HH:MM (24h), got {value!r}")
    hours, minutes = map(int, value.strip().split(":"))
    return time(hours, minutes)  # raises on out-of-range values


class Device(BaseModel):
    mac: str
    friendly_id: str
    api_key: str
    status: DeviceStatus = "pending"
    created_at: datetime = Field(default_factory=now_utc)
    last_seen: datetime | None = None
    fw_version: str | None = None
    model: str | None = None
    battery_voltage: float | None = None
    battery_charging: bool | None = None
    usb_connected: bool | None = None
    rssi: int | None = None
    last_filename: str | None = None
    last_refresh_seconds: int | None = None
    low_battery_alerted: bool = False
    logs: list[dict] = Field(default_factory=list)

    def battery_percent(self) -> int | None:
        if self.battery_voltage is None:
            return None
        frac = (self.battery_voltage - BATTERY_EMPTY_V) / (BATTERY_FULL_V - BATTERY_EMPTY_V)
        return round(max(0.0, min(1.0, frac)) * 100)

    def check_low_battery(self, threshold_v: float) -> bool:
        """Update the alert latch; return True exactly when a new low-battery alert should be sent."""
        v = self.battery_voltage
        if v is None or self.usb_connected or self.battery_charging:
            return False
        if v < threshold_v and not self.low_battery_alerted:
            self.low_battery_alerted = True
            return True
        if v >= threshold_v + LOW_BATTERY_REARM_MARGIN_V:
            self.low_battery_alerted = False
        return False


class DeviceSettings(BaseModel):
    refresh_minutes: int = Field(15, ge=5, le=1440)
    night_enabled: bool = False
    night_start: str = "23:00"
    night_end: str = "06:00"

    @field_validator("night_start", "night_end")
    @classmethod
    def _valid_time(cls, value: str) -> str:
        return _parse_hhmm(value).strftime("%H:%M")

    def in_night(self, now: datetime) -> bool:
        if not self.night_enabled:
            return False
        start, end, t = _parse_hhmm(self.night_start), _parse_hhmm(self.night_end), now.time()
        if start == end:
            return False
        return start <= t < end if start < end else (t >= start or t < end)

    def refresh_seconds(self, now: datetime) -> int:
        """Seconds the device should sleep, given local `now`. At night: sleep until the window ends."""
        if self.in_night(now):
            end = _parse_hhmm(self.night_end)
            wake = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
            if wake <= now:
                wake += timedelta(days=1)
            seconds = int((wake - now).total_seconds()) + 60
        else:
            seconds = self.refresh_minutes * 60
        return max(MIN_REFRESH_SECONDS, min(MAX_REFRESH_SECONDS, seconds))

    def describe(self) -> str:
        text = f"Refresh every {self.refresh_minutes} min"
        if self.night_enabled:
            text += f"; night mode {self.night_start}–{self.night_end} (one wake at {self.night_end})"
        else:
            text += "; night mode off"
        return text


class DeviceState(BaseModel):
    devices: list[Device] = Field(default_factory=list)
    settings: DeviceSettings = Field(default_factory=DeviceSettings)


class DeviceRegistry:
    """In-memory device state persisted to DATA_DIR/devices.json. Mutate under `lock`, then `save()`."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "devices.json"
        self.lock = asyncio.Lock()
        self.state = DeviceState.model_validate_json(self.path.read_text()) if self.path.exists() else DeviceState()

    @property
    def settings(self) -> DeviceSettings:
        return self.state.settings

    @property
    def devices(self) -> list[Device]:
        return self.state.devices

    def save(self) -> None:
        atomic_write_bytes(self.path, self.state.model_dump_json(indent=2).encode())

    def by_mac(self, mac: str) -> Device | None:
        mac = normalize_mac(mac)
        return next((d for d in self.devices if d.mac == mac), None)

    def by_token(self, token: str) -> Device | None:
        if not token:
            return None
        return next((d for d in self.devices if secrets.compare_digest(d.api_key, token)), None)

    def by_friendly_id(self, friendly_id: str) -> Device | None:
        return next((d for d in self.devices if d.friendly_id == friendly_id), None)

    def add_pending(self, mac: str, fw_version: str | None = None, model: str | None = None) -> Device:
        taken = {d.friendly_id for d in self.devices}
        friendly_id = secrets.token_hex(3).upper()
        while friendly_id in taken:
            friendly_id = secrets.token_hex(3).upper()
        device = Device(
            mac=normalize_mac(mac),
            friendly_id=friendly_id,
            api_key=secrets.token_urlsafe(24),
            fw_version=fw_version,
            model=model,
        )
        self.devices.append(device)
        return device

    def set_status(self, friendly_id: str, status: DeviceStatus) -> Device | None:
        device = self.by_friendly_id(friendly_id)
        if device is not None:
            device.status = status
        return device

    def remove(self, friendly_id: str) -> bool:
        device = self.by_friendly_id(friendly_id)
        if device is None:
            return False
        self.devices.remove(device)
        return True


def _ago(then: datetime | None, now: datetime) -> str:
    if then is None:
        return "never"
    seconds = int((now - then).total_seconds())
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 90 * 60:
        return f"{seconds // 60} min ago"
    if seconds < 36 * 3600:
        return f"{seconds / 3600:.1f} h ago"
    return f"{seconds / 86400:.1f} days ago"


def _rssi_label(rssi: int) -> str:
    if rssi >= -60:
        return "excellent"
    if rssi >= -70:
        return "good"
    if rssi >= -80:
        return "fair"
    return "weak"


def format_status(registry: DeviceRegistry, tz, log_lines: int = 5) -> str:
    """Plain-text summary of all devices and the schedule (used by /device and the Claude tool)."""
    now = now_utc()
    lines = [registry.settings.describe() + "."]
    if not registry.devices:
        lines.append("No TRMNL devices have contacted the server yet.")
        return "\n".join(lines)
    for d in registry.devices:
        lines.append("")
        lines.append(f"TRMNL {d.friendly_id} ({d.mac}) — {d.status}")
        lines.append(f"  Last seen: {_ago(d.last_seen, now)}")
        if d.battery_voltage is not None:
            power = " (charging)" if d.battery_charging else " (USB)" if d.usb_connected else ""
            lines.append(f"  Battery: {d.battery_voltage:.2f} V ≈ {d.battery_percent()}%{power}")
        if d.rssi is not None:
            lines.append(f"  Wi-Fi: {d.rssi} dBm ({_rssi_label(d.rssi)})")
        if d.fw_version or d.model:
            lines.append(f"  Firmware: {d.fw_version or '?'}  Model: {d.model or '?'}")
        if d.last_filename:
            lines.append(f"  Showing: {d.last_filename}")
        if d.last_seen and d.last_refresh_seconds:
            wake = (d.last_seen + timedelta(seconds=d.last_refresh_seconds)).astimezone(tz)
            lines.append(f"  Next wake: ~{wake.strftime('%a %H:%M')}")
        for entry in d.logs[-log_lines:]:
            lines.append(f"  log: {entry.get('message', '')}"[:200])
    return "\n".join(lines)
