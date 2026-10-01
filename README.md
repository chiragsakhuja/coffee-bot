# coffee-bot

A Telegram bot that keeps a coffee menu on a [TRMNL](https://trmnl.com) OG e-ink display up to date. Send it photos of coffee bags and notes about brew recipes. Claude pulls out the details, updates the menu, and renders an 800×480 grayscale image. The bot also *is* the TRMNL's server: it implements the BYOS device protocol (`/api/setup`, `/api/display`, `/api/log`), so the display pulls the menu directly from it.

```
 Telegram ──► bot.py ──► agent.py (Claude, tool runner) ──► tools.py
 (you)          ▲                                              │
                │ previews, questions, approve buttons,        ▼
                │ pairing requests, battery alerts      store.py (data/menu.json)
                │                                              │
                │                     render.py: Jinja → Chromium → 2-bit / 1-bit images (ImageCache)
                │                                              │
                └──────── server.py (aiohttp, BYOS) ◄──────────┘
                            ▲   /api/setup  /api/display  /api/log  /images/<hash>.png
                            │
                       TRMNL OG (polls every refresh_rate seconds)
```

## What's on the display

Each coffee (up to 4) shows its **roaster and name**, **roast date and days since roast**, **tasting notes**, **container** (black / white / green / blue, as a labeled swatch), and a flexible list of **brew methods**. Each brew method has a status (trying / dialing in / dialed), the usual parameters (dose, yield/water, ratio, grind, temp, time), free-form extras (bloom, pours, …), and a short note.

## Talking to the bot

- Send a bag photo and say which container it's in. The bot asks about anything it can't read.
- Send a recipe: *"V60 on the Onyx: 15g/250g, 22 clicks, 3:00, a bit sour"*
- Say it's dialed: *"espresso on the Sey is dialed"*
- Ask for layout changes: *"make the tasting notes bigger"*. The bot edits `templates/` and sends a preview.
- Ask for server tasks: *"how much disk is free?"*. Every shell command needs your approval via a button.
- Ask about the display: *"how's the battery?"*, *"refresh every 30 minutes"*, *"night mode 11pm to 6am"*.
- `/menu` shows the current image, `/undo` reverts the last menu change, `/new` starts a fresh conversation, `/device` shows the TRMNL's status, `/forget <id>` un-pairs a device.

## The TRMNL server

Point the device's **API server** (in its Wi-Fi setup page) at `http://<server-ip>:9157`. The first time it connects, the bot asks you in Telegram to approve it. After that, it gets the menu as a 2-bit grayscale PNG (or 1-bit for older firmware). The filename is a content hash, so the device only redraws when the menu changed. See [docs/SETUP.md §8–10](docs/SETUP.md#8-connect-the-trmnl) for setup, curl recipes, and `scripts/fake_device.py`, which simulates a device for testing.

## Quick start (development)

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/playwright install chromium
cp .env.example .env   # fill in tokens, see docs/SETUP.md
.venv/bin/python -m coffee_bot
```

Iterate on the template without Telegram:

```bash
.venv/bin/python scripts/render_sample.py   # renders 0-4 coffee variants into output/samples/
.venv/bin/pytest
.venv/bin/python scripts/fake_device.py     # simulate a TRMNL against the running bot
```

Full server setup (BotFather, user IDs, systemd, TRMNL output formats): **[docs/SETUP.md](docs/SETUP.md)**.

## Layout

| Path | What |
|---|---|
| `src/coffee_bot/bot.py` | Telegram handlers, allowlist, album batching, approval buttons, device pairing, nightly re-render; starts the HTTP server |
| `src/coffee_bot/server.py` | TRMNL BYOS HTTP endpoints (aiohttp) |
| `src/coffee_bot/devices.py` | Paired devices, telemetry, refresh/night schedule (`data/devices.json`) |
| `src/coffee_bot/agent.py` | Claude conversation per chat (system prompt, tool runner, history) |
| `src/coffee_bot/tools.py` | Tools Claude can call: menu edits, preview, sandboxed file I/O, approved shell |
| `src/coffee_bot/models.py` | Menu / Coffee / BrewMethod schema |
| `src/coffee_bot/store.py` | Atomic JSON storage + undo history |
| `src/coffee_bot/render.py` | HTML → PNG (Playwright) → 2-bit / 1-bit TRMNL images, image cache |
| `src/coffee_bot/sandbox.py` | Path allowlist and command runner |
| `templates/` | `menu.html.j2` + `menu.css`, the display design (the bot can edit these) |
| `scripts/fake_device.py` | Simulated TRMNL for testing the endpoints |
| `deploy/coffee-bot.service` | systemd unit |
