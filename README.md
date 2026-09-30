# coffee-bot

A Telegram bot that keeps a coffee menu on a [TRMNL](https://trmnl.com) OG e-ink display up to date. Send it photos of coffee bags and notes about brew recipes. Claude pulls out the details, updates the menu, and renders an 800×480 grayscale image for your TRMNL's HTTP server to serve.

```
 Telegram ──► bot.py ──► agent.py (Claude, tool runner) ──► tools.py
 (you)          ▲                                              │
                │ previews, questions, approve/deny buttons    ▼
                └──────────────────────────────── store.py (data/menu.json)
                                                               │
                                     render.py: Jinja → Chromium → 2-bit PNG
                                                               │
                                              OUTPUT_DIR/menu.png ◄── your HTTP server ◄── TRMNL
```

## What's on the display

Each coffee (up to 4) shows its **roaster and name**, **roast date and days since roast**, **tasting notes**, **container** (black / white / green / blue, as a labeled swatch), and a flexible list of **brew methods**. Each brew method has a status (trying / dialing in / dialed), the usual parameters (dose, yield/water, ratio, grind, temp, time), free-form extras (bloom, pours, …), and a short note.

## Talking to the bot

- Send a bag photo and say which container it's in. The bot asks about anything it can't read.
- Send a recipe: *"V60 on the Onyx: 15g/250g, 22 clicks, 3:00, a bit sour"*
- Say it's dialed: *"espresso on the Sey is dialed"*
- Ask for layout changes: *"make the tasting notes bigger"*. The bot edits `templates/` and sends a preview.
- Ask for server tasks: *"how much disk is free?"*. Every shell command needs your approval via a button.
- `/menu` shows the current image, `/undo` reverts the last menu change, `/new` starts a fresh conversation.

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
```

Full server setup (BotFather, user IDs, systemd, TRMNL output formats): **[docs/SETUP.md](docs/SETUP.md)**.

## Layout

| Path | What |
|---|---|
| `src/coffee_bot/bot.py` | Telegram handlers, allowlist, album batching, approval buttons, nightly re-render |
| `src/coffee_bot/agent.py` | Claude conversation per chat (system prompt, tool runner, history) |
| `src/coffee_bot/tools.py` | Tools Claude can call: menu edits, preview, sandboxed file I/O, approved shell |
| `src/coffee_bot/models.py` | Menu / Coffee / BrewMethod schema |
| `src/coffee_bot/store.py` | Atomic JSON storage + undo history |
| `src/coffee_bot/render.py` | HTML → PNG (Playwright) → 2-bit / 1-bit TRMNL images |
| `src/coffee_bot/sandbox.py` | Path allowlist and command runner |
| `templates/` | `menu.html.j2` + `menu.css`, the display design (the bot can edit these) |
| `deploy/coffee-bot.service` | systemd unit |
