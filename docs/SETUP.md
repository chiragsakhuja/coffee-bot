# Setup guide

This guide takes you from nothing to a running bot on a Linux server with systemd. It covers:

1. [Creating the Telegram bot](#1-create-the-telegram-bot)
2. [Finding your Telegram user ID](#2-find-your-telegram-user-id)
3. [Getting an Anthropic API key](#3-get-an-anthropic-api-key)
4. [Installing on the server](#4-install-on-the-server)
5. [Configuring `.env`](#5-configure-env)
6. [First run and smoke test](#6-first-run-and-smoke-test)
7. [Running as a systemd service](#7-run-as-a-systemd-service)
8. [Connecting the HTTP server / TRMNL](#8-connect-your-http-server--trmnl)
9. [Troubleshooting](#9-troubleshooting)
10. [Security notes](#10-security-notes)

---

## 1. Create the Telegram bot

Telegram bots are created by talking to Telegram's own bot, **@BotFather**.

1. In Telegram (phone or desktop), search for **@BotFather**. It has a blue verified check mark. Open the chat and tap **Start**.
2. Send `/newbot`.
3. BotFather asks for a **name**. This is the display name shown in chats and can be anything, e.g. `Coffee Menu`.
4. BotFather asks for a **username**. It must be unique and end in `bot`, e.g. `sakhuja_coffee_bot`. If it's taken, try another.
5. BotFather replies with a message that contains your **HTTP API token**. It looks like this:

   ```
   7123456789:AAH-some-long-random-string_abcdefg
   ```

   Copy it. This token is the bot's password: anyone who has it can control the bot. Don't commit it to git or share it. If it leaks, send `/revoke` to BotFather and pick your bot to get a new one.

### Optional polish

Still in the BotFather chat:

- **Description** (shown before someone presses Start): `/setdescription`, pick your bot, then send e.g. `Keeps the coffee menu on my e-ink display up to date.`
- **Profile photo:** `/setuserpic`, pick your bot, then send an image.
- **Command menu:** the bot registers its commands (`/menu`, `/new`, `/undo`, `/help`) automatically on startup, so you don't need `/setcommands`.

### Privacy mode

By default, bots in *group chats* only see commands and replies. This bot is meant for a **private 1:1 chat**, where it sees every message, so you don't need to change anything. If you want to use it in a group, send `/setprivacy` to BotFather and choose **Disable**, then add the bot to the group. The allowlist below still applies per user.

## 2. Find your Telegram user ID

The bot ignores everyone except the numeric user IDs you allow. You need your ID, which is a number, not your @username.

**Option A (easiest):** message **@userinfobot** in Telegram. It replies with your `Id`, e.g. `123456789`.

**Option B (no third-party bot):**
1. Open a chat with *your* new bot and send it any message, e.g. `hi`.
2. On any computer, run (replace `<TOKEN>`):
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool
   ```
3. Look for `"from": { "id": 123456789, ... }`. That number is your user ID.

To let several people use the bot, collect everyone's ID and separate them with commas.

## 3. Get an Anthropic API key

1. Sign in at <https://console.anthropic.com/>.
2. Add billing under **Settings → Billing**. The bot uses Claude Opus 5.5; a typical message costs a few cents, a bit more with photos.
3. Go to **Settings → API Keys → Create Key**, name it `coffee-bot`, and copy the key (`sk-ant-...`). It's shown only once.
4. Optional: set a monthly spend limit under **Settings → Limits**.

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
| `TELEGRAM_BOT_TOKEN` | `7123456789:AAH...` | From step 1 |
| `ALLOWED_USER_IDS` | `123456789,987654321` | From step 2, comma-separated |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | From step 3 |
| `OUTPUT_DIR` | `/var/lib/coffee-bot/output` | Rendered images. Your HTTP server reads from here |
| `DATA_DIR` | `/var/lib/coffee-bot/data` | `menu.json` + undo history |
| `MODEL` | `claude-opus-5-5` | Claude model |
| `TIMEZONE` | `America/Los_Angeles` | IANA name. Used for "days since roast" and the nightly re-render |

With the systemd unit below, use the `/var/lib/coffee-bot/...` paths. The unit's sandbox only allows writes there and in `templates/`.

## 6. First run and smoke test

Run it once in the foreground to confirm everything works:

```bash
cd /opt/coffee-bot
sudo mkdir -p /var/lib/coffee-bot && sudo chown coffeebot:coffeebot /var/lib/coffee-bot
sudo -u coffeebot env PLAYWRIGHT_BROWSERS_PATH=/opt/coffee-bot/.playwright HOME=/var/lib/coffee-bot \
    .venv/bin/python -m coffee_bot
```

You should see `initial render done` in the log, and `/var/lib/coffee-bot/output/menu.png` should exist. Then in Telegram:

1. Open your bot and send `/start`. You should get the help text.
2. Send `/menu`. You should get the (empty) menu image.
3. Send a photo of a coffee bag with the caption `this one's in the blue bin`. The bot reads the bag, asks about anything it can't read (e.g. the roast date), then sends a preview.
4. Send a recipe: `espresso on that: 18g in, 38g out, 28s, grind 2.4 — dialed`.
5. Try a command: `how much disk space is free?`. You should get a **Run / Don't run** button.

Press `Ctrl+C` to stop.

You can also render without Telegram, which is handy when editing the template:

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

If you installed somewhere other than `/opt/coffee-bot`, edit the paths in the unit file.

### Updating

```bash
cd /opt/coffee-bot
sudo -u coffeebot git pull
sudo -u coffeebot .venv/bin/pip install -e .
sudo systemctl restart coffee-bot
```

The bot may have edited `templates/` from Telegram. Commit those changes (or `git stash`) before pulling so they don't conflict. Backups of every template edit live in `DATA_DIR/history/templates/`.

## 8. Connect your HTTP server / TRMNL

The bot writes three files to `OUTPUT_DIR` after every change and again just after midnight every day, so the date and roast age stay current:

| File | Format | Use when |
|---|---|---|
| `menu.png` | 800×480, **2-bit grayscale** PNG (4 shades) | TRMNL OG firmware **1.6.0+** (recommended) |
| `menu-1bit.png` | 800×480, 1-bit PNG | Older firmware / pure black & white |
| `menu-1bit.bmp` | 800×480, 1-bit BMP3 | Firmware that only accepts BMP |

Each file is written atomically (temp file + rename), so your server never serves a half-written image. Point your BYOS server's image URL at one of them and make sure the response includes a `Content-Length` header; the TRMNL firmware expects it. The files are world-readable (`0644`), so an HTTP server running as a different user can read them if it can traverse `/var/lib/coffee-bot/output`.

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| Bot doesn't reply at all | Check `journalctl -u coffee-bot`. Look for `ignoring message from unauthorized user <id>` and add that ID to `ALLOWED_USER_IDS`. |
| `Conflict: terminated by other getUpdates request` | Two copies of the bot are running with the same token (e.g. a foreground test + the service). Stop one. |
| `Executable doesn't exist at .../chrome-headless-shell` | Chromium isn't installed where the service looks. Re-run the `playwright install` step with the same `PLAYWRIGHT_BROWSERS_PATH` as the unit file. |
| Chromium crashes with missing `.so` libraries | Run `sudo .venv/bin/playwright install-deps chromium`. |
| Text looks like a different font than expected | Install `fonts-inter` (or edit `font-family` in `templates/menu.css`). |
| `Claude API returned an error (401)` | Bad `ANTHROPIC_API_KEY`. |
| `(429)` or `(529)` errors | Rate-limited or overloaded. Wait a minute and retry. |
| Display shows an old image | Check the HTTP server path matches `OUTPUT_DIR`. Send `/menu` to force a re-render. |
| A template edit broke rendering | Ask the bot to fix it, or restore from `DATA_DIR/history/templates/` (or `git checkout templates/`). |
| Menu change you didn't want | Send `/undo` (it can be repeated). |

## 10. Security notes

- **Only allowlisted users** can talk to the bot. Messages from anyone else are logged and ignored. Approval buttons also check the allowlist.
- **File edits are sandboxed.** Claude can *write* only in `templates/` and *read* only in `templates/` and `DATA_DIR`. The menu itself is changed only through validated tools.
- **Shell commands need your approval.** Every command is shown to you with a Run / Don't run button and times out after 5 minutes without an answer. Approved commands run in the project directory with a 60 s timeout. Under systemd they also run inside the unit's sandbox: read-only filesystem except `/var/lib/coffee-bot` and `templates/`, no access to `/home`, and no privilege escalation. Read each command before approving it. Text in a photo could try to trick the model into proposing a harmful command, and the approval step is your safeguard.
- Keep `.env` at `chmod 600`. Rotate the Telegram token (`/revoke` in BotFather) or the Anthropic key if either leaks.
