# Setup guide

This guide takes you from nothing to a running bot on a Linux server with systemd, with your TRMNL OG pulling the menu straight from the bot's built-in server. It covers:

1. [Creating the Slack app](#1-create-the-slack-app)
2. [Finding your Slack member ID](#2-find-your-slack-member-id)
3. [Getting an OpenRouter API key](#3-get-an-openrouter-api-key)
4. [Installing on the server](#4-install-on-the-server)
5. [Configuring `.env`](#5-configure-env)
6. [First run and smoke test](#6-first-run-and-smoke-test)
7. [Running as a systemd service](#7-run-as-a-systemd-service)
8. [Connecting the TRMNL](#8-connect-the-trmnl)
9. [Testing the endpoints](#9-test-the-endpoints)
10. [Refresh schedule and device status](#10-refresh-schedule-and-device-status)
11. [Troubleshooting](#11-troubleshooting)
12. [Security notes](#12-security-notes)

---

## 1. Create the Slack app

The bot connects to Slack with **Socket Mode**: it opens an outbound websocket to Slack, so Slack never needs to reach your server. No public URL, port forwarding, or TLS certificate is needed, which suits a server on your home LAN.

The repo includes an app manifest that sets up all scopes, events, the `/coffee` command and Socket Mode in one go.

1. Go to <https://api.slack.com/apps> and click **Create New App → From an app manifest**.
2. Pick the workspace you want the bot in.
3. Choose **YAML**, delete the example, and paste the contents of [`deploy/slack-app-manifest.yml`](../deploy/slack-app-manifest.yml). Click **Next**, then **Create**.
4. **App-level token** (lets the bot open the Socket Mode connection): on the app's **Basic Information** page, scroll to **App-Level Tokens → Generate Token and Scopes**. Name it `socket`, add the scope `connections:write`, and click **Generate**. Copy the token (`xapp-…`). This is `SLACK_APP_TOKEN`.
5. **Install the app:** open **Install App** (or **OAuth & Permissions**) and click **Install to *your workspace***, then **Allow**. Copy the **Bot User OAuth Token** (`xoxb-…`). This is `SLACK_BOT_TOKEN`.

   > **Company workspaces** often require an admin to approve new apps. If you see **Request to Install** instead of **Install**, submit the request and wait for approval. You can also create the app in a personal workspace you own.

Both tokens are passwords: anyone who has them can act as the bot. Don't commit them to git. If one leaks, regenerate it (**Basic Information → App-Level Tokens** for `xapp-`, **OAuth & Permissions → Revoke / Reinstall** for `xoxb-`).

### Optional polish

On **Basic Information → Display Information** you can set the app icon, name and description. If you change scopes or events later, Slack asks you to **reinstall** the app; the bot token usually stays the same.

### Where you can talk to it

- **Direct messages (recommended):** find **Coffee Bot** under **Apps** in Slack's sidebar (or search for it) and open its **Messages** tab. Everything you send there goes to the bot.
- **Channels:** invite it with `/invite @coffee-bot`, then @mention it: `@coffee-bot espresso on the Sey is dialed`. It replies in a thread. In channels it only sees messages that mention it.
- **`/coffee` command:** works in the bot's DM and in channels the bot is in. See [Talking to the bot](../README.md#talking-to-the-bot).

## 2. Find your Slack member ID

The bot ignores everyone except the Slack member IDs you allow. A member ID looks like `U012AB3CDE` (on Enterprise Grid it may start with `W`). It's not your display name or @handle.

1. In Slack, click your profile picture → **Profile**.
2. Click the **⋮** (more) button → **Copy member ID**.

To let several people use the bot, collect everyone's ID (same steps on their profile) and separate them with commas.

## 3. Get an OpenRouter API key

1. Sign in at <https://openrouter.ai/>.
2. Add credits under **Credits**. By default the bot uses `anthropic/claude-opus-5-5`; a typical message costs a few cents, a bit more with photos. Any OpenRouter model that supports tool calling and image input works (set `MODEL`).
3. Go to **Keys → Create Key**, name it `coffee-bot`, and copy the key (`sk-or-v1-...`). It's shown only once.
4. Optional: set a credit limit on the key when you create it.

## 4. Install on the server

These steps assume Debian, Ubuntu, or Raspberry Pi OS (64-bit) and a user with `sudo`. Adjust the package names for other distros.

```bash
# System packages: Python, git, and a nice font for the display
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git fonts-inter

# A dedicated, unprivileged user for the service
sudo useradd --system --home /var/lib/coffee-bot --shell /usr/sbin/nologin coffeebot

# Get the code. Clone your repo, or copy the project directory over with rsync/scp.
sudo git clone <your-repo-url> /opt/coffee-bot
#   or: rsync -a --exclude .venv --exclude output --exclude data ./coffee-bot/ server:/tmp/coffee-bot && sudo mv /tmp/coffee-bot /opt/
sudo chown -R coffeebot:coffeebot /opt/coffee-bot

# Python environment + dependencies
cd /opt/coffee-bot
sudo -u coffeebot python3 -m venv .venv
sudo -u coffeebot .venv/bin/pip install --upgrade pip
sudo -u coffeebot .venv/bin/pip install -e .

# Headless Chromium used to render the HTML template.
# --with-deps installs the system libraries Chromium needs (runs apt, so needs sudo).
sudo PLAYWRIGHT_BROWSERS_PATH=/opt/coffee-bot/.playwright .venv/bin/playwright install --with-deps chromium
sudo chown -R coffeebot:coffeebot /opt/coffee-bot/.playwright
```

> `python3 --version` must be 3.11 or newer. Debian 12 / Ubuntu 24.04 / Raspberry Pi OS Bookworm all qualify.

> **Raspberry Pi:** Playwright's Chromium works on 64-bit (arm64) Pi OS. It won't work on 32-bit. A Pi 4 or 5 renders a frame in about a second.

## 5. Configure `.env`

```bash
cd /opt/coffee-bot
sudo -u coffeebot cp .env.example .env
sudo chmod 600 .env
sudo -u coffeebot nano .env
```

| Variable | Example | Notes |
|---|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-...` | Bot User OAuth Token, from step 1 |
| `SLACK_APP_TOKEN` | `xapp-...` | App-level token with `connections:write`, from step 1 |
| `ALLOWED_USER_IDS` | `U012AB3CDE,U045FG6HIJ` | Slack member IDs from step 2, comma-separated |
| `OPENROUTER_API_KEY` | `sk-or-v1-...` | From step 3 |
| `OUTPUT_DIR` | `/var/lib/coffee-bot/output` | Copies of the rendered images (handy for debugging; the device is served from memory) |
| `DATA_DIR` | `/var/lib/coffee-bot/data` | `menu.json`, `devices.json`, undo history |
| `MODEL` | `anthropic/claude-opus-5-5` | OpenRouter model slug; must support tool calling and image input |
| `TIMEZONE` | `America/Los_Angeles` | IANA name. Used for "days since roast", the nightly re-render, and night mode |
| `HTTP_HOST` | `0.0.0.0` | Address the TRMNL server listens on (`0.0.0.0` = all interfaces) |
| `HTTP_PORT` | `9157` | Port the TRMNL server listens on |
| `PUBLIC_BASE_URL` | *(empty)* | Base URL put into image links. Leave empty: it's taken from the address the device used. Set it (e.g. `http://192.168.1.50:9157`) only if the device reaches the bot through a proxy or different hostname |
| `IMAGE_FORMAT` | `auto` | `auto` = 2-bit PNG for firmware ≥ 1.6, otherwise 1-bit PNG. Or force `2bit`, `1bit`, `bmp` |
| `LOW_BATTERY_VOLTS` | `3.5` | Slack DM alert when the device's battery drops below this |

With the systemd unit below, use the `/var/lib/coffee-bot/...` paths. The unit's sandbox only allows writes there and in `templates/`.

## 6. First run and smoke test

Run it once in the foreground to confirm everything works:

```bash
cd /opt/coffee-bot
sudo mkdir -p /var/lib/coffee-bot && sudo chown coffeebot:coffeebot /var/lib/coffee-bot
sudo -u coffeebot env PLAYWRIGHT_BROWSERS_PATH=/opt/coffee-bot/.playwright HOME=/var/lib/coffee-bot \
    .venv/bin/python -m coffee_bot
```

You should see `connected to Slack workspace …`, `initial render done`, `TRMNL server listening on http://0.0.0.0:9157` and `⚡️ Bolt app is running!` in the log. From another terminal, `curl http://localhost:9157/healthz` should return `{"ok": true, ...}`. Then in Slack, open the bot's DM (**Apps → Coffee Bot → Messages**):

1. Send `/coffee help`. You should get the help text.
2. Send `/coffee menu`. You should get the (empty) menu image.
3. Send a photo of a coffee bag with the caption `this one's in the blue bin`. The bot reads the bag, asks about anything it can't read (e.g. the roast date), then sends a preview.
4. Send a recipe: `espresso on that: 18g in, 38g out, 28s, grind 2.4 — dialed`.
5. Try a command: `how much disk space is free?`. You should get **Run / Don't run** buttons.

Press `Ctrl+C` to stop.

You can also render without Slack, which is handy when editing the template:

```bash
.venv/bin/python scripts/render_sample.py                    # fixture with 0-4 coffees → output/samples/
.venv/bin/python scripts/render_sample.py /var/lib/coffee-bot/data/menu.json
```

## 7. Run as a systemd service

```bash
sudo cp /opt/coffee-bot/deploy/coffee-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now coffee-bot

systemctl status coffee-bot          # is it running?
journalctl -u coffee-bot -f          # follow logs
sudo systemctl restart coffee-bot    # after changing .env or pulling new code
```

If you installed somewhere other than `/opt/coffee-bot`, edit the paths in the unit file. The TRMNL server runs inside the same service. Port 9157 is unprivileged, so the unit needs no extra permissions.

### Updating

```bash
cd /opt/coffee-bot
sudo -u coffeebot git pull
sudo -u coffeebot .venv/bin/pip install -e .
sudo systemctl restart coffee-bot
```

The bot may have edited `templates/` from Slack. Commit those changes (or `git stash`) before pulling so they don't conflict. Backups of every template edit live in `DATA_DIR/history/templates/`.

## 8. Connect the TRMNL

The bot has a built-in HTTP server that speaks TRMNL's **BYOS** ("bring your own server") protocol. The device talks to it directly; no separate web server is needed.

### How the device and the server interact

```
TRMNL wakes up ──► GET /api/setup      (only on first contact: gets an API key)
                └► GET /api/display    (every wake: "what should I show, and when do I wake next?")
                     ◄── {"image_url": ".../images/menu-<hash>-2b.png", "filename": ..., "refresh_rate": 900}
                └► GET /images/menu-<hash>-2b.png   (only when the filename changed)
                └► POST /api/log       (only when the device has something to report)
TRMNL sleeps for refresh_rate seconds
```

The image `filename` contains a hash of the image, so the device only downloads and redraws when the menu actually changed. Menu edits show up at the device's **next wake**, not instantly.

### 8.1 Find the server's LAN address and open the port

```bash
hostname -I          # e.g. 192.168.1.50 — use the address on your home network
```

If you use `ufw`, allow the port from your home network only:

```bash
sudo ufw allow from 192.168.1.0/24 to any port 9157 proto tcp
```

Check it from another machine on the LAN (laptop/phone browser): `http://192.168.1.50:9157/healthz` should show `{"ok": true, ...}` and `http://192.168.1.50:9157/preview` shows the current menu image.

> Give the server a **fixed IP** (a DHCP reservation in your router). The device stores the URL; if the server's IP changes, the device can't find it.

### 8.2 Point the device at the server

The server address is set in the TRMNL's Wi-Fi setup page (captive portal):

1. **Put the device into Wi-Fi setup mode.** A brand-new device starts in setup mode by itself. For a device that is already set up, use the button on the back to reset its Wi-Fi settings (see TRMNL's help center for "reset Wi-Fi" on your firmware version). The screen then shows setup instructions.
2. On your phone, join the Wi-Fi network named **TRMNL**. A setup page opens. If it doesn't, open any `http://` website in your phone's browser to trigger it.
3. Choose your home Wi-Fi network and enter its password.
4. Find the **API server** / custom server field (on some firmware versions it's behind an "advanced" or "custom server" option) and enter:

   ```
   http://192.168.1.50:9157
   ```

   Use your server's address, with `http://` and **no trailing slash**. The device adds `/api/...` itself.
5. Save. The device joins Wi-Fi and contacts the server.

### 8.3 Approve it in Slack

On first contact the bot sends you a DM:

> A TRMNL wants to connect. Friendly ID: **3FA9C1** … Approve it?  **[✅ Approve] [❌ Deny]**

The device screen shows its own friendly ID while it waits. Check that the IDs match, then click **Approve**. While waiting, the device checks in every few seconds, so the menu appears within about a minute.

- Missed the message? Send `/coffee device`. It shows any pending devices again with Approve/Deny buttons.
- Clicked **Deny** by mistake? Send `/coffee forget <friendly ID>`. The device will ask again next time it checks in.
- **Moving a device from trmnl.com (or another server):** the device still has its old API key, which this server doesn't recognise. The server replies with `status: 500`, which tells the firmware to drop its key and pair again, so you get the Slack approval message after one or two wake-ups.

### 8.4 Which image the device gets

| `IMAGE_FORMAT` | Device firmware | Served file |
|---|---|---|
| `auto` (default) | ≥ 1.6.0 | 800×480 **2-bit grayscale PNG** (4 shades) |
| `auto` | < 1.6.0 or unknown | 800×480 1-bit PNG |
| `2bit` / `1bit` / `bmp` | any | that format (BMP is the 48062-byte 1-bit BMP3) |

Images are served with an exact `Content-Type` (`image/png` / `image/bmp`) and `Content-Length`. The firmware relies on both. The OG accepts images up to 90 KB; the menu is typically 5–10 KB. The same three files are also written to `OUTPUT_DIR` (`menu.png`, `menu-1bit.png`, `menu-1bit.bmp`) for debugging.

## 9. Test the endpoints

### 9.1 With the fake device script (recommended)

`scripts/fake_device.py` behaves like the firmware: it pairs, waits for your approval, fetches the display JSON, downloads the image, and checks it against the OG's limits (800×480, ≤ 90 KB, correct `Content-Type`, bit depth). Then it posts a log entry.

```bash
cd /opt/coffee-bot
.venv/bin/python scripts/fake_device.py --base-url http://localhost:9157
```

Expected output (shortened):

```
→ GET http://localhost:9157/api/setup  (ID: AA:BB:CC:00:11:22)
  HTTP 200: {"status": 200, "api_key": "…", "friendly_id": "E8BD02", "image_url": "…/images/msg-…-1b.png", …}
  ✓ paired as friendly ID E8BD02
→ GET http://localhost:9157/api/display
  HTTP 200: {"status": 202, …}
  … waiting for approval: click Approve for E8BD02 in Slack        ← click Approve now
→ GET http://localhost:9157/api/display
  HTTP 200: {"status": 0, "image_url": "…/images/menu-9bb2e6b342-2b.png", "filename": "menu-9bb2e6b342-2b.png", "refresh_rate": 900, …}
  ✓ display OK
  decoded: PNG 800x480 mode=L bit-depth=2
  ✓ image is valid for a TRMNL OG
→ POST http://localhost:9157/api/log
  ✓ log accepted
All checks passed. Try /coffee device in Slack.
```

Useful variations:

```bash
fake_device.py --battery 3.3        # low battery → you get a 🔋 alert in Slack (once)
fake_device.py --fw 1.5.0           # old firmware → 1-bit PNG
fake_device.py --reset              # forget the saved key and pair from scratch
fake_device.py --mac AA:BB:CC:00:00:02   # a second "device"
```

The fake device appears in `/coffee device` like a real one. Remove it afterwards with `/coffee forget <friendly ID>`.

### 9.2 By hand with curl

```bash
BASE=http://localhost:9157

# Health + current image
curl -s $BASE/healthz
curl -s $BASE/preview -o preview.png && file preview.png
#   preview.png: PNG image data, 800 x 480, 2-bit grayscale, non-interlaced

# 1) Setup: what a new device sends first
curl -s $BASE/api/setup -H 'ID: AA:BB:CC:DD:EE:01' -H 'FW-Version: 1.8.6' -H 'Model: og'
#   {"status": 200, "api_key": "Xy…", "friendly_id": "3FA9C1", "image_url": "…", "filename": "msg-…", …}
#   → the bot DMs you in Slack to approve 3FA9C1

KEY=<api_key from above>

# 2) Display: what the device sends on every wake
curl -s $BASE/api/display \
  -H 'ID: AA:BB:CC:DD:EE:01' -H "Access-Token: $KEY" -H 'FW-Version: 1.8.6' \
  -H 'Battery-Voltage: 4.02' -H 'RSSI: -61' -H 'Refresh-Rate: 900' -H 'Width: 800' -H 'Height: 480'
#   before approval: {"status": 202, …}
#   after approval:  {"status": 0, "image_url": "http://localhost:9157/images/menu-…-2b.png",
#                     "filename": "menu-…-2b.png", "refresh_rate": 900, "reset_firmware": false, …}

# 3) Image: check headers and format
curl -sI <image_url>                      # Content-Type: image/png, Content-Length: …
curl -s <image_url> -o menu.png && file menu.png

# 4) Log
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BASE/api/log \
  -H "Access-Token: $KEY" -H 'Content-Type: application/json' \
  -d '{"logs":[{"message":"hello from curl","level":"info"}]}'
#   204
```

Responses you can check for:

| Request | Situation | Response |
|---|---|---|
| `/api/setup` | new MAC | HTTP 200, `status: 200`, new key; Slack approval request |
| `/api/setup` | denied MAC | HTTP 404, `status: 404`, nulls |
| `/api/display` | pending approval | `status: 202` (device shows its friendly ID, retries quickly) |
| `/api/display` | approved | `status: 0`, `image_url`, `filename`, `refresh_rate` |
| `/api/display` | unknown key / denied / MAC mismatch | `status: 500` (device drops its key and re-pairs) |
| `/api/log` | valid key | HTTP 204 |
| `/images/<name>` | unknown name | HTTP 404 |

### 9.3 Automated tests

```bash
.venv/bin/pytest            # includes tests/test_server.py, which covers all of the above
```

## 10. Refresh schedule and device status

**Device status:** send `/coffee device` (or ask *"how's the display battery?"*). You get each device's last check-in, battery (volts and an approximate %), Wi-Fi strength, firmware, the image it's showing, its next expected wake, and its recent log messages.

**Refresh interval:** ask Claude in plain language:

- *"Refresh the display every 30 minutes"*
- *"Turn on night mode from 10:30pm to 6am"*
- *"Turn off night mode"*

The default is every 15 minutes, with night mode off. A shorter interval drains the battery faster. During the night window the device gets one long sleep that ends at the window's end, so it updates once in the morning. Changes apply at the device's next wake-up. The settings are stored in `DATA_DIR/devices.json`.

**Low-battery alert:** when a device reports a battery voltage below `LOW_BATTERY_VOLTS` (default 3.5 V, roughly 25%) and isn't on USB, every allowed Slack user gets one DM. It re-arms after the battery is charged (≥ 0.2 V above the threshold).

## 11. Troubleshooting

| Symptom | Fix |
|---|---|
| Bot doesn't reply at all | Check `journalctl -u coffee-bot`. Look for `ignoring message from unauthorized user <id>` and add that member ID to `ALLOWED_USER_IDS`. |
| Bot replies twice, or only sometimes | Two copies are running with the same tokens (e.g. a foreground test + the service); Slack spreads events across both connections. Stop one. |
| `invalid_auth` / `not_authed` at startup | Wrong `SLACK_BOT_TOKEN`, or the app was uninstalled. Reinstall and copy the `xoxb-` token again. |
| Socket Mode connection fails at startup | Wrong `SLACK_APP_TOKEN`, or it lacks the `connections:write` scope. Generate a new app-level token. |
| `/coffee` says "dispatch_failed" or nothing happens | The bot isn't running, or Socket Mode is off for the app (**Settings → Socket Mode**). |
| "Sending messages to this app has been turned off" | **App Home → Show Tabs**: enable the Messages tab and tick *Allow users to send Slash commands and messages from the messages tab*. (The manifest sets this.) |
| Photos are ignored or "couldn't read that" | The app lacks `files:read`. Add it under **OAuth & Permissions** and reinstall. |
| `/coffee` in a channel says it can't post there | Invite the bot (`/invite @coffee-bot`) or use the command in its DM. |
| `Executable doesn't exist at .../chrome-headless-shell` | Chromium isn't installed where the service looks. Re-run the `playwright install` step with the same `PLAYWRIGHT_BROWSERS_PATH` as the unit file. |
| Chromium crashes with missing `.so` libraries | Run `sudo .venv/bin/playwright install-deps chromium`. |
| Text looks like a different font than expected | Install `fonts-inter` (or edit `font-family` in `templates/menu.css`). |
| `model API returned an error (401)` | Bad `OPENROUTER_API_KEY`. |
| `(429)` or `(529)` errors | Rate-limited or overloaded. Wait a minute and retry. |
| Display shows an old image | Menu changes appear at the next wake. Check `/coffee device` for "Next wake". `/coffee menu` forces a re-render. |
| Device shows "MAC not registered" or similar | The device was denied. `/coffee forget <friendly ID>`, then wait for it to ask again. |
| Device stuck showing its friendly ID | It's waiting for approval. Send `/coffee device` and click Approve. |
| Device says it can't reach the API / server | Wrong URL in the captive portal (needs `http://`, IP, port, no trailing slash), firewall blocking 9157, or the server's IP changed. Test `http://<ip>:9157/healthz` from a phone on the same Wi-Fi. |
| `/coffee device` says "last seen: never" after approval | The device hasn't woken since. Press the button on the back to wake it, or wait one refresh interval. |
| Log shows `display from unknown/denied device` | Normal right after moving a device to this server; it re-pairs automatically. |
| `OSError: [Errno 98] address already in use` | Something else uses port 9157. Change `HTTP_PORT` (and the device's API server URL). |
| A template edit broke rendering | Ask the bot to fix it, or restore from `DATA_DIR/history/templates/` (or `git checkout templates/`). |
| Menu change you didn't want | Send `/coffee undo` (it can be repeated). |

## 12. Security notes

- **Only allowlisted users** can talk to the bot. Messages from anyone else are logged and ignored. Approval buttons and `/coffee` also check the allowlist. If you add the bot to a channel, everyone in it can see its replies and previews.
- **Devices must be approved** in Slack before they get the menu. Each one gets its own random API key, and display/log requests are checked against it (and the MAC). The HTTP server is plain HTTP and meant for your **home LAN only**. Don't port-forward 9157 to the internet. Images are served without auth (their names are content hashes); they contain only your coffee menu.
- **File edits are sandboxed.** Claude can *write* only in `templates/` and *read* only in `templates/` and `DATA_DIR`. The menu itself is changed only through validated tools.
- **Shell commands need your approval.** Every command is shown to you with a Run / Don't run button and times out after 5 minutes without an answer. Approved commands run in the project directory with a 60 s timeout. Under systemd they also run inside the unit's sandbox: read-only filesystem except `/var/lib/coffee-bot` and `templates/`, no access to `/home`, and no privilege escalation. Read each command before approving it. Text in a photo could try to trick the model into proposing a harmful command, and the approval step is your safeguard.
- Keep `.env` at `chmod 600`. Rotate the Slack tokens (see step 1) or the OpenRouter key if any of them leaks. On a company workspace, remember that Slack messages and files (including bag photos and approved shell commands) are retained under your workspace's policies.
