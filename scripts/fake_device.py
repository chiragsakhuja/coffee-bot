"""Pretend to be a TRMNL OG and exercise the server the way the firmware does.

    python scripts/fake_device.py                                  # http://localhost:9157
    python scripts/fake_device.py --base-url http://192.168.1.50:9157
    python scripts/fake_device.py --battery 3.3                    # trigger the low-battery alert
    python scripts/fake_device.py --fw 1.5.0                       # older firmware -> 1-bit PNG
    python scripts/fake_device.py --reset                          # forget the saved key, pair again

Flow: /api/setup (key saved to a state file) -> /api/display (waits while the server says 202,
i.e. until you tap Approve in Telegram) -> downloads image_url and checks it against the OG's
limits -> POSTs a sample /api/log entry.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

MAX_IMAGE_BYTES = 90_000


def request(method: str, url: str, headers: dict[str, str], body: dict | None = None) -> tuple[int, dict, bytes]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def fail(msg: str) -> None:
    print(f"  ✗ {msg}")
    sys.exit(1)


def check_image(url: str, headers: dict[str, str]) -> None:
    status, resp_headers, data = request("GET", url, headers)
    if status != 200:
        fail(f"image download returned HTTP {status}")
    ctype = resp_headers.get("Content-Type", "")
    length = resp_headers.get("Content-Length")
    print(f"  image: {len(data)} bytes, Content-Type={ctype!r}, Content-Length={length}")
    if len(data) > MAX_IMAGE_BYTES:
        fail(f"image is larger than the OG's {MAX_IMAGE_BYTES}-byte limit")
    if data[:2] == b"BM":
        if len(data) != 48062:
            fail(f"BMP must be exactly 48062 bytes, got {len(data)}")
    elif ctype != "image/png":
        fail("PNG must be served with exactly Content-Type: image/png (the firmware relies on it)")
    img = Image.open(io.BytesIO(data))
    img.load()
    bits = data[24] if img.format == "PNG" else 1  # PNG: IHDR bit-depth byte
    print(f"  decoded: {img.format} {img.size[0]}x{img.size[1]} mode={img.mode} bit-depth={bits}")
    if img.size != (800, 480):
        fail("image must be 800x480")
    ok("image is valid for a TRMNL OG")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://localhost:9157")
    ap.add_argument("--mac", default="AA:BB:CC:00:11:22")
    ap.add_argument("--fw", default="1.8.6", help="firmware version to report")
    ap.add_argument("--battery", default="4.05", help="battery voltage to report")
    ap.add_argument("--rssi", default="-58")
    ap.add_argument("--wait", type=int, default=300, help="seconds to wait for approval")
    ap.add_argument("--reset", action="store_true", help="discard the saved api_key and pair again")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    state_file = Path(tempfile.gettempdir()) / f"trmnl-fake-{args.mac.replace(':', '')}.json"
    state = {} if args.reset or not state_file.exists() else json.loads(state_file.read_text())

    if not state.get("api_key"):
        print(f"→ GET {base}/api/setup  (ID: {args.mac})")
        status, _, raw = request("GET", f"{base}/api/setup", {"ID": args.mac, "FW-Version": args.fw, "Model": "og"})
        body = json.loads(raw or b"{}")
        print(f"  HTTP {status}: {json.dumps(body)}")
        if status != 200 or body.get("status") != 200:
            fail("setup refused (device denied? use /forget <id> in Telegram, then --reset)")
        state = {"api_key": body["api_key"], "friendly_id": body["friendly_id"]}
        state_file.write_text(json.dumps(state))
        ok(f"paired as friendly ID {state['friendly_id']} (key saved to {state_file})")
        if body.get("image_url"):
            check_image(body["image_url"], {})
    else:
        print(f"Using saved key for {state['friendly_id']} ({state_file}); pass --reset to pair again")

    headers = {
        "ID": args.mac, "Access-Token": state["api_key"], "Refresh-Rate": "900", "Battery-Voltage": args.battery,
        "FW-Version": args.fw, "RSSI": args.rssi, "Model": "og", "Width": "800", "Height": "480",
        "USB-Connected": "false", "Battery-Charging": "false",
    }
    deadline = time.monotonic() + args.wait
    while True:
        print(f"→ GET {base}/api/display")
        status, _, raw = request("GET", f"{base}/api/display", headers)
        body = json.loads(raw or b"{}")
        print(f"  HTTP {status}: {json.dumps(body)}")
        if body.get("status") == 202:
            if time.monotonic() > deadline:
                fail("still waiting for approval")
            print(f"  … waiting for approval: tap Approve for {state['friendly_id']} in Telegram")
            time.sleep(5)
            continue
        if body.get("status") == 500:
            state_file.unlink(missing_ok=True)
            fail("server doesn't know this key (status 500); a real device would reset and re-run setup. Rerun to pair again.")
        if body.get("status") != 0:
            fail(f"unexpected status {body.get('status')}")
        break

    if not isinstance(body.get("refresh_rate"), int):
        fail("refresh_rate should be an integer")
    if len(body.get("filename") or "") > 31:
        fail("filename longer than 31 chars gets mangled by the firmware")
    ok(f"display OK: filename={body['filename']}, sleep {body['refresh_rate']}s")
    check_image(body["image_url"], {"ID": args.mac, "Access-Token": state["api_key"]})

    print(f"→ POST {base}/api/log")
    status, _, _ = request("POST", f"{base}/api/log", {"ID": args.mac, "Access-Token": state["api_key"]}, {
        "logs": [{"created_at": int(time.time()), "id": 1, "level": "info", "message": "fake_device.py test log",
                  "source_path": "fake_device.py", "source_line": 1, "battery_voltage": float(args.battery),
                  "wifi_signal": int(args.rssi), "firmware_version": args.fw, "wake_reason": "timer"}]
    })
    if status not in (200, 204):
        fail(f"log returned HTTP {status}")
    ok("log accepted")
    print("\nAll checks passed. Try /device in Telegram.")


if __name__ == "__main__":
    main()
