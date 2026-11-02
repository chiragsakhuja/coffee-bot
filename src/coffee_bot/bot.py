"""Slack front-end (Socket Mode): auth filter, message/photo intake, command approvals, replies."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import logging
import re
import secrets
import signal
from dataclasses import dataclass
from datetime import datetime, timedelta

import aiohttp
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from .agent import CoffeeAgent
from .devices import Device, format_status
from .render import PNG_2BIT
from .server import DeviceServer
from .tools import Services

log = logging.getLogger(__name__)

register_heif_opener()  # iPhone photos can arrive as HEIC

SLASH_COMMAND = "/coffee"
APPROVAL_TIMEOUT_SECONDS = 300
SLACK_TEXT_LIMIT = 3900  # Slack truncates long messages; keep chunks well under its 4000-char guidance
MAX_DOWNLOAD_BYTES = 30_000_000
MAX_IMAGE_BYTES = 4_500_000  # Anthropic's per-image limit is 5 MB (base64-decoded)
MAX_IMAGE_SIDE = 2000
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
WORKING_REACTION = "hourglass_flowing_sand"
# Message subtypes that carry something the user wrote. Others (edits, joins, bot posts…) are ignored.
USER_SUBTYPES = {None, "file_share", "thread_broadcast"}

HELP_TEXT = (
    "Send me a photo of a coffee bag (tell me which container it's in: black, white, green or blue) "
    "and I'll add it to the menu. Send brew recipes or dial-in notes, e.g. \"V60 on the Onyx: 15g in, "
    "250g out, 22 clicks, 3:00, a bit sour\", and I'll update the brew methods. You can also ask me "
    "to change the layout of the display.\n\n"
    f"`{SLASH_COMMAND} menu` shows the current image\n"
    f"`{SLASH_COMMAND} new` starts a fresh conversation\n"
    f"`{SLASH_COMMAND} undo` reverts the last menu change\n"
    f"`{SLASH_COMMAND} device` shows the TRMNL's battery, Wi-Fi and last check-in\n"
    f"`{SLASH_COMMAND} forget <id>` un-pairs a device"
)


def escape(text: str) -> str:
    """Escape the three characters Slack treats as control sequences in message text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def unescape(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _section(mrkdwn: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": mrkdwn}}


def _button(label: str, action_id: str, value: str, style: str) -> dict:
    return {
        "type": "button", "text": {"type": "plain_text", "text": label, "emoji": True},
        "action_id": action_id, "value": value, "style": style,
    }


def _decision_blocks(mrkdwn: str, prefix: str, value: str, yes: str, no: str) -> list[dict]:
    return [
        _section(mrkdwn),
        {"type": "actions", "elements": [
            _button(yes, f"{prefix}_yes", value, "primary"),
            _button(no, f"{prefix}_no", value, "danger"),
        ]},
    ]


def _pairing_text(device: Device) -> str:
    return (
        f"A TRMNL wants to connect.\n\n*Friendly ID:* `{device.friendly_id}`\n*MAC:* {escape(device.mac)}\n"
        f"*Model:* {escape(device.model or '?')}   *Firmware:* {escape(device.fw_version or '?')}\n\n"
        "The device screen should show the same friendly ID. Approve it?"
    )


def _pairing_blocks(device: Device) -> list[dict]:
    return _decision_blocks(_pairing_text(device), "pair", device.friendly_id, "✅ Approve", "❌ Deny")


def _resolved_blocks(body: dict, outcome: str) -> tuple[str, list[dict]]:
    """Replace a decision message's buttons with the outcome, keeping its original text."""
    blocks = body.get("message", {}).get("blocks") or []
    original = blocks[0]["text"]["text"] if blocks and blocks[0].get("type") == "section" else ""
    text = f"{original}\n\n{outcome}".strip()
    return text, [_section(text)]


def _prepare_image(data: bytes, media_type: str) -> tuple[bytes, str]:
    """Rotate per EXIF, downscale, and re-encode images the API can't take as-is."""
    with Image.open(io.BytesIO(data)) as im:
        upright = im.getexif().get(0x0112, 1) == 1  # EXIF orientation: phone photos are often rotated
        if (
            upright and len(data) <= MAX_IMAGE_BYTES and media_type in SUPPORTED_IMAGE_TYPES
            and max(im.size) <= MAX_IMAGE_SIDE
        ):
            return data, media_type
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return buf.getvalue(), "image/jpeg"


def strip_mention(text: str, bot_user_id: str | None) -> str:
    if bot_user_id:
        text = re.sub(rf"<@{re.escape(bot_user_id)}(\|[^>]*)?>", "", text)
    return text.strip()


@dataclass
class PendingApproval:
    future: asyncio.Future[bool]
    channel: str


class SlackChat:
    """Implements tools.ChatIO for one Slack conversation (optionally one thread in it)."""

    def __init__(self, bot: "CoffeeBot", channel: str, thread_ts: str | None = None):
        self.bot = bot
        self.channel = channel
        self.thread_ts = thread_ts

    async def say(self, text: str, blocks: list[dict] | None = None) -> dict:
        return await self.bot.client.chat_postMessage(
            channel=self.channel, thread_ts=self.thread_ts, text=text, blocks=blocks, unfurl_links=False
        )

    async def send_photo(self, png: bytes, caption: str | None = None) -> None:
        await self.bot.client.files_upload_v2(
            channel=self.channel, thread_ts=self.thread_ts, file=png, filename="menu.png",
            title="Coffee menu", alt_txt="Coffee menu display preview",
            initial_comment=escape(caption) if caption else None,
        )

    async def request_approval(self, command: str, reason: str) -> bool:
        token = secrets.token_hex(8)
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.bot.pending_approvals[token] = PendingApproval(future, self.channel)
        # Long commands can't fit in one section block (3000 chars); the full command is in the tool call anyway.
        shown = command if len(command) <= 2500 else command[:2500] + "\n… (truncated)"
        text = (
            f"Claude wants to run a command on the server:\n```{escape(shown)}```\n*Why:* {escape(reason)}"
        )
        posted = await self.say(
            "Claude wants to run a command on the server.",
            _decision_blocks(text, "approve", token, "✅ Run", "❌ Don't run"),
        )
        try:
            return await asyncio.wait_for(future, APPROVAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            with contextlib.suppress(SlackApiError):
                timed_out = f"{text}\n\n⌛ No answer in 5 minutes, so the command was not run."
                await self.bot.client.chat_update(
                    channel=posted["channel"], ts=posted["ts"], text=timed_out, blocks=[_section(timed_out)]
                )
            return False
        finally:
            self.bot.pending_approvals.pop(token, None)


class CoffeeBot:
    def __init__(
        self,
        bot_token: str,
        app_token: str,
        allowed_user_ids: frozenset[str],
        agent: CoffeeAgent,
        services: Services,
        *,
        http_host: str = "0.0.0.0",
        http_port: int = 9157,
        **server_options,
    ):
        self.agent = agent
        self.svc = services
        self.allowed = allowed_user_ids
        self.bot_token, self.app_token = bot_token, app_token
        self.http_host, self.http_port = http_host, http_port
        self.server = DeviceServer(
            services.devices, services.renderer.cache, services.renderer, self, services.tz, **server_options
        )
        self.pending_approvals: dict[str, PendingApproval] = {}
        self.bot_user_id: str | None = None
        self._dm_channels: dict[str, str] = {}
        self._nightly_task: asyncio.Task | None = None

        self.app = AsyncApp(token=bot_token)
        self.client: AsyncWebClient = self.app.client
        self.app.event("message")(self.on_message)
        self.app.event("app_mention")(self.on_mention)
        self.app.command(SLASH_COMMAND)(self.on_command)
        self.app.action(re.compile(r"^approve_(yes|no)$"))(self.on_approval)
        self.app.action(re.compile(r"^pair_(yes|no)$"))(self.on_pairing)

    def run(self) -> None:
        asyncio.run(self._main())

    # ---- lifecycle ----

    async def _main(self) -> None:
        main_task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, main_task.cancel)

        auth = await self.client.auth_test()
        self.bot_user_id = auth["user_id"]
        log.info("connected to Slack workspace %s as @%s", auth.get("team"), auth.get("user"))
        await self.svc.renderer.start()
        handler = AsyncSocketModeHandler(self.app, self.app_token)
        try:
            try:
                await self.svc.render()
                log.info("initial render done")
            except Exception:
                log.exception("initial render failed")
            await self.server.start(self.http_host, self.http_port)
            self._nightly_task = asyncio.create_task(self._nightly_rerender())
            await handler.start_async()  # runs until cancelled
        except asyncio.CancelledError:
            log.info("shutting down")
        finally:
            if self._nightly_task:
                self._nightly_task.cancel()
            with contextlib.suppress(Exception):
                await handler.close_async()
            await self.server.stop()
            await self.svc.renderer.close()

    async def _nightly_rerender(self) -> None:
        """Re-render just after local midnight so the date and 'days since roast' stay current."""
        while True:
            now = datetime.now(self.svc.tz)
            next_run = (now + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
            await asyncio.sleep((next_run - now).total_seconds())
            try:
                await self.svc.render()
                log.info("nightly re-render done")
            except Exception:
                log.exception("nightly re-render failed")

    # ---- DeviceNotifier (called by the HTTP server) ----

    async def _dm(self, user_id: str) -> SlackChat:
        channel = self._dm_channels.get(user_id)
        if channel is None:
            resp = await self.client.conversations_open(users=user_id)
            channel = self._dm_channels[user_id] = resp["channel"]["id"]
        return SlackChat(self, channel)

    async def notify_pairing(self, device: Device) -> None:
        for user_id in self.allowed:
            dm = await self._dm(user_id)
            await dm.say(f"A TRMNL ({device.friendly_id}) wants to connect.", _pairing_blocks(device))

    async def notify(self, text: str) -> None:
        for user_id in self.allowed:
            dm = await self._dm(user_id)
            await dm.say(escape(text))

    # ---- slash command ----

    async def on_command(self, ack, command: dict, respond) -> None:
        await ack()
        if command.get("user_id") not in self.allowed:
            log.warning("ignoring %s from unauthorized user %s", SLASH_COMMAND, command.get("user_id"))
            await respond("Sorry, you're not on this bot's allowlist.")
            return
        args = command.get("text", "").split()
        sub = args[0].lower() if args else "help"
        chat = SlackChat(self, command["channel_id"])
        try:
            await self._dispatch(sub, args[1:], chat)
        except SlackApiError as exc:
            # Typically channel_not_found / not_in_channel: the bot can't post where the command was typed.
            log.warning("%s %s failed: %s", SLASH_COMMAND, sub, exc.response.get("error"))
            await respond(
                f"I can't post in this conversation ({exc.response.get('error')}). "
                f"Use `{SLASH_COMMAND}` in a DM with me, or invite me to this channel."
            )

    async def _dispatch(self, sub: str, args: list[str], chat: SlackChat) -> None:
        if sub == "menu":
            await self.cmd_menu(chat)
        elif sub == "new":
            self.agent.reset(chat.channel)
            await chat.say("Started a fresh conversation. The menu is unchanged.")
        elif sub == "undo":
            await self.cmd_undo(chat)
        elif sub in ("device", "devices"):
            await self.cmd_device(chat)
        elif sub == "forget":
            await self.cmd_forget(chat, args)
        else:
            await chat.say(HELP_TEXT)

    async def cmd_menu(self, chat: SlackChat) -> None:
        try:
            result = await self.svc.render()
        except Exception as exc:
            await chat.say(escape(f"Render failed: {exc}"))
            return
        await chat.send_photo(result.preview_png, f"Published to {PNG_2BIT}")

    async def cmd_undo(self, chat: SlackChat) -> None:
        async with self.svc.menu_lock:
            menu = self.svc.store.undo()
        if menu is None:
            await chat.say("Nothing to undo.")
            return
        # Claude's history still describes the undone change; start fresh to avoid confusion.
        self.agent.reset(chat.channel)
        result = await self.svc.render(menu)
        await chat.send_photo(result.preview_png, "Reverted the last menu change.")

    async def cmd_device(self, chat: SlackChat) -> None:
        await chat.say(f"```{escape(format_status(self.svc.devices, self.svc.tz))}```")
        for device in self.svc.devices.devices:
            if device.status == "pending":
                await chat.say(f"A TRMNL ({device.friendly_id}) wants to connect.", _pairing_blocks(device))

    async def cmd_forget(self, chat: SlackChat, args: list[str]) -> None:
        """forget <friendly_id>: remove a device so it has to pair again (e.g. after a denial)."""
        if not args:
            await chat.say(f"Usage: `{SLASH_COMMAND} forget <friendly ID>`  (see `{SLASH_COMMAND} device`)")
            return
        friendly_id = args[0].upper()
        async with self.svc.devices.lock:
            removed = self.svc.devices.remove(friendly_id)
            if removed:
                self.svc.devices.save()
        await chat.say(escape(
            f"Forgot {friendly_id}. If it's still running, it will ask to pair again."
            if removed else f"No device with friendly ID {friendly_id}."
        ))

    # ---- buttons ----

    async def on_pairing(self, ack, body: dict, respond) -> None:
        await ack()
        if body["user"]["id"] not in self.allowed:
            await respond(text="Not allowed.", response_type="ephemeral", replace_original=False)
            return
        action = body["actions"][0]
        friendly_id, approved = action["value"], action["action_id"] == "pair_yes"
        async with self.svc.devices.lock:
            device = self.svc.devices.by_friendly_id(friendly_id)
            if device is None or device.status != "pending":
                outcome = f"Already handled ({device.status if device else 'gone'})."
            else:
                device.status = "approved" if approved else "denied"
                self.svc.devices.save()
                outcome = (
                    "✅ Approved. The menu appears on the display at its next check-in (within a minute)."
                    if approved else
                    f"❌ Denied. Use `{SLASH_COMMAND} forget {friendly_id}` if you want to let it pair again later."
                )
        await self._resolve(body, outcome)

    async def on_approval(self, ack, body: dict, respond) -> None:
        await ack()
        if body["user"]["id"] not in self.allowed:
            await respond(text="Not allowed.", response_type="ephemeral", replace_original=False)
            return
        action = body["actions"][0]
        pending = self.pending_approvals.get(action["value"])
        if pending is None or pending.future.done():
            await self._resolve(body, "This request has expired.")
            return
        approved = action["action_id"] == "approve_yes"
        pending.future.set_result(approved)
        await self._resolve(body, "✅ Approved" if approved else "❌ Declined")

    async def _resolve(self, body: dict, outcome: str) -> None:
        text, blocks = _resolved_blocks(body, outcome)
        await self.client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text=text, blocks=blocks)

    # ---- messages ----

    async def on_message(self, event: dict) -> None:
        """Direct messages to the bot. (Channel messages arrive as app_mention instead.)"""
        if event.get("channel_type") != "im" or event.get("subtype") not in USER_SUBTYPES or event.get("bot_id"):
            return
        await self._handle_user_message(event, thread_ts=event.get("thread_ts"))

    async def on_mention(self, event: dict) -> None:
        """@coffee-bot in a channel. Replies go in a thread so the channel stays tidy."""
        if event.get("channel", "").startswith("D"):
            return  # DMs are handled by on_message
        await self._handle_user_message(event, thread_ts=event.get("thread_ts") or event["ts"])

    async def _handle_user_message(self, event: dict, thread_ts: str | None) -> None:
        user = event.get("user")
        if user not in self.allowed:
            log.warning("ignoring message from unauthorized user %s", user)
            return
        chat = SlackChat(self, event["channel"], thread_ts)
        try:
            content = await self._to_content(event)
        except Exception as exc:
            log.exception("failed to read message")
            await chat.say(escape(f"Sorry, I couldn't read that: {exc}"))
            return
        if content:
            await self._run_turn(chat, event["ts"], content)

    async def _to_content(self, event: dict) -> list[dict]:
        content: list[dict] = []
        for f in event.get("files") or []:
            if f.get("file_access") == "check_file_info":
                f = (await self.client.files_info(file=f["id"]))["file"]
            mimetype = f.get("mimetype") or ""
            if not mimetype.startswith("image/"):
                continue
            data = await self._download(f)
            data, media_type = _prepare_image(data, mimetype)
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode()},
            })
        text = unescape(strip_mention(event.get("text") or "", self.bot_user_id))
        if text:
            content.append({"type": "text", "text": text})
        return content

    async def _download(self, f: dict) -> bytes:
        if (f.get("size") or 0) > MAX_DOWNLOAD_BYTES:
            raise ValueError(f"{f.get('name', 'file')} is too large ({f['size'] // 1_000_000} MB)")
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            raise ValueError(f"no download URL for {f.get('name', 'file')}")
        headers = {"Authorization": f"Bearer {self.bot_token}"}
        async with aiohttp.ClientSession() as session, session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            if not resp.content_type.startswith("image/") and resp.content_type != "application/octet-stream":
                # Slack answers with an HTML login page when the token lacks files:read.
                raise ValueError("Slack didn't return the image (does the app have the files:read scope?)")
            return await resp.read()

    async def _run_turn(self, chat: SlackChat, message_ts: str, content: list[dict]) -> None:
        # Slack has no typing indicator for bots; a reaction on the user's message shows we're working.
        with contextlib.suppress(SlackApiError):
            await self.client.reactions_add(channel=chat.channel, timestamp=message_ts, name=WORKING_REACTION)
        try:
            reply = await self.agent.handle(chat.channel, content, chat)
        except Exception:
            log.exception("turn failed")
            reply = "Sorry, something went wrong on my end. Check the server logs."
        finally:
            with contextlib.suppress(SlackApiError):
                await self.client.reactions_remove(channel=chat.channel, timestamp=message_ts, name=WORKING_REACTION)
        for i in range(0, len(reply), SLACK_TEXT_LIMIT):
            await chat.say(escape(reply[i : i + SLACK_TEXT_LIMIT]))
