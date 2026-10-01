from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from coffee_bot.devices import DeviceRegistry, DeviceSettings, format_status

TZ = ZoneInfo("America/Los_Angeles")


def at(hh, mm):
    return datetime(2026, 9, 30, hh, mm, tzinfo=TZ)


def test_daytime_refresh():
    s = DeviceSettings(refresh_minutes=20, night_enabled=True)
    assert s.refresh_seconds(at(12, 0)) == 20 * 60


@pytest.mark.parametrize("now, expected_minutes", [
    (at(23, 30), 6 * 60 + 30 + 1),  # 23:30 -> 06:00 next day (+1 min margin)
    (at(2, 0), 4 * 60 + 1),         # 02:00 -> 06:00
    (at(5, 59), 1 + 1),
])
def test_night_window_spanning_midnight(now, expected_minutes):
    s = DeviceSettings(night_enabled=True, night_start="23:00", night_end="06:00")
    assert s.refresh_seconds(now) == expected_minutes * 60


def test_night_window_same_day():
    s = DeviceSettings(night_enabled=True, night_start="13:00", night_end="15:00")
    assert s.in_night(at(14, 0)) and not s.in_night(at(15, 0)) and not s.in_night(at(12, 59))


def test_night_disabled():
    assert DeviceSettings(refresh_minutes=15).refresh_seconds(at(2, 0)) == 900


def test_validation():
    with pytest.raises(ValidationError):
        DeviceSettings(refresh_minutes=2)
    with pytest.raises(ValidationError):
        DeviceSettings(night_start="7pm")
    assert DeviceSettings(night_start="7:05").night_start == "07:05"


def test_registry_roundtrip(tmp_path):
    reg = DeviceRegistry(tmp_path)
    d = reg.add_pending("ab-cd-ef-01-02-03", "1.8.6", "og")
    assert d.mac == "AB:CD:EF:01:02:03" and len(d.friendly_id) == 6 and len(d.api_key) >= 30
    reg.set_status(d.friendly_id, "approved")
    reg.save()

    again = DeviceRegistry(tmp_path)
    assert again.by_mac("ab:cd:ef:01:02:03").status == "approved"
    assert again.by_token(d.api_key).friendly_id == d.friendly_id
    assert again.by_token("") is None and again.by_token("nope") is None
    assert again.remove(d.friendly_id) and again.devices == []


def test_low_battery_latch(tmp_path):
    d = DeviceRegistry(tmp_path).add_pending("AA:AA:AA:AA:AA:AA")
    d.battery_voltage = 3.4
    assert d.check_low_battery(3.5) is True
    assert d.check_low_battery(3.5) is False   # only once
    d.battery_voltage = 3.6
    assert d.check_low_battery(3.5) is False   # still latched (needs +0.2 V)
    d.battery_voltage = 3.8
    d.check_low_battery(3.5)
    d.battery_voltage = 3.3
    assert d.check_low_battery(3.5) is True    # re-armed
    d.low_battery_alerted, d.usb_connected = False, True
    assert d.check_low_battery(3.5) is False   # no alerts while on USB


def test_format_status(tmp_path):
    reg = DeviceRegistry(tmp_path)
    d = reg.add_pending("AA:AA:AA:AA:AA:AA")
    d.battery_voltage, d.rssi = 3.9, -65
    d.logs.append({"message": "wifi reconnect"})
    text = format_status(reg, TZ)
    assert "3.90 V" in text and "good" in text and "wifi reconnect" in text and "pending" in text
