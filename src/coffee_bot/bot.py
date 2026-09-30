"""Telegram front-end: auth filter, message/photo intake, command approvals, replies."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from PIL import Image
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .agent import CoffeeAgent
from .render import PNG_2BIT
from .tools import Services

log = logging.getLogger(__name__)

APPROVAL_TIMEOUT_SECONDS = 300
MEDIA_GROUP_WAIT_SECONDS = 1.5
TELEGRAM_TEXT_LIMIT = 4000
MAX_IMAGE_BYTES = 4_500_000  # Anthropic's per-image limit is 5 MB (base64-decoded)
MAX_IMAGE_SIDE = 2000
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

BOT_COMMANDS = [
    ("menu", "Show the current display image"),
    ("new", "Start a fresh conversation"),
    ("undo", "Revert the last menu change"),
    ("help", "What can this bot do?"),
]

HELP_TEXT = (
    "Send me a photo of a coffee bag (tell me which container it's in: black, white, green or blue) "
    "and I'll add it to the menu. Send brew recipes or dial-in notes, e.g. \"V60 on the Onyx: 15g in, "
    "250g out, 22 clicks, 3:00, a bit sour\", and I'll update the brew methods. You can also ask me "
    "to change the layout of the display.\n\n"
    "/menu shows the current image\n/new starts a fresh conversation\n/undo reverts the last menu change"
)


@dataclass
class PendingApproval:
    future: asyncio.Future[bool]
    chat_id: int


@dataclass
class MediaGroupBuffer:
    messages: list[Message] = field(default_factory=list)


class TelegramChat:
    """Implements tools.ChatIO for one Telegram chat."""

    def __init__(self, app: "CoffeeBot", chat_id: int):
        self.app = app
        self.chat_id = chat_id

    async def send_photo(self, png: bytes, caption: str | None = None) -> None:
        await self.app.application.bot.send_photo(self.chat_id, photo=png, caption=caption)

    async def request_approval(self, command: str, reason: str) -> bool:
        token = secrets.token_hex(8)
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.app.pending_approvals[token] = PendingApproval(future, self.chat_id)
        keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Run", callback_data=f"approve:{token}:yes"),
                InlineKeyboardButton("❌ Don't run", callback_data=f"approve:{token}:no"),
            ]]
        )
        await self.app.application.bot.send_message(
            self.chat_id,
            f"Claude wants to run a command on the server:\n\n{command}\n\nWhy: {reason}",
            reply_markup=keyboard,
        )
        try:
            return await asyncio.wait_for(future, APPROVAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await self.app.application.bot.send_message(self.chat_id, "No answer in 5 minutes, so the command was not run.")
            return False
        finally:
            self.app.pending_approvals.pop(token, None)


def _prepare_image(data: bytes, media_type: str) -> tuple[bytes, str]:
    """Downscale/re-encode images that exceed the API's size limits."""
    if len(data) <= MAX_IMAGE_BYTES and media_type in SUPPORTED_IMAGE_TYPES:
        with Image.open(io.BytesIO(data)) as im:
            if max(im.size) <= 8000:
                return data, media_type
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        im.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return buf.getvalue(), "image/jpeg"


class CoffeeBot:
    def __init__(self, token: str, allowed_user_ids: frozenset[int], agent: CoffeeAgent, services: Services):
        self.agent = agent
        self.svc = services
        self.allowed = allowed_user_ids
        self.pending_approvals: dict[str, PendingApproval] = {}
        self._media_groups: dict[str, MediaGroupBuffer] = {}
        self._nightly_task: asyncio.Task | None = None

        self.application: Application = (
            ApplicationBuilder()
            .token(token)
            # Needed so approval button presses are handled while a Claude turn is waiting on them.
            .concurrent_updates(True)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        users = filters.User(user_id=list(allowed_user_ids))
        app = self.application
        app.add_handler(CommandHandler(["start", "help"], self.cmd_help, filters=users))
        app.add_handler(CommandHandler("menu", self.cmd_menu, filters=users))
        app.add_handler(CommandHandler("new", self.cmd_new, filters=users))
        app.add_handler(CommandHandler("undo", self.cmd_undo, filters=users))
        app.add_handler(CallbackQueryHandler(self.on_approval, pattern=r"^approve:"))
        content = filters.TEXT & ~filters.COMMAND | filters.PHOTO | filters.Document.IMAGE
        app.add_handler(MessageHandler(users & content, self.on_message))
        app.add_handler(MessageHandler(~users, self.on_stranger))

    def run(self) -> None:
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)

    # ---- lifecycle ----

    async def _post_init(self, app: Application) -> None:
        await app.bot.set_my_commands(BOT_COMMANDS)
        await self.svc.renderer.start()
        try:
            await self.svc.render()
            log.info("initial render done")
        except Exception:
            log.exception("initial render failed")
        self._nightly_task = asyncio.create_task(self._nightly_rerender())

    async def _post_shutdown(self, app: Application) -> None:
        if self._nightly_task:
            self._nightly_task.cancel()
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

    # ---- commands ----

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(HELP_TEXT)

    async def cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            result = await self.svc.render()
        except Exception as exc:
            await update.effective_message.reply_text(f"Render failed: {exc}")
            return
        await update.effective_message.reply_photo(result.preview_png, caption=f"Published to {PNG_2BIT}")

    async def cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.agent.reset(update.effective_chat.id)
        await update.effective_message.reply_text("Started a fresh conversation. The menu is unchanged.")

    async def cmd_undo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        async with self.svc.menu_lock:
            menu = self.svc.store.undo()
        if menu is None:
            await update.effective_message.reply_text("Nothing to undo.")
            return
        # Claude's history still describes the undone change; start fresh to avoid confusion.
        self.agent.reset(update.effective_chat.id)
        result = await self.svc.render(menu)
        await update.effective_message.reply_photo(result.preview_png, caption="Reverted the last menu change.")

    async def on_approval(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query.from_user.id not in self.allowed:
            await query.answer("Not allowed.")
            return
        _, token, answer = query.data.split(":")
        pending = self.pending_approvals.get(token)
        if pending is None or pending.future.done():
            await query.answer("This request has expired.")
            await query.edit_message_reply_markup(None)
            return
        approved = answer == "yes"
        pending.future.set_result(approved)
        await query.answer("Running…" if approved else "Cancelled.")
        await query.edit_message_text(f"{query.message.text}\n\n{'✅ Approved' if approved else '❌ Declined'}")

    async def on_stranger(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        log.warning("ignoring message from unauthorized user %s (%s)", user.id if user else "?", user.username if user else "")

    # ---- messages ----

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message.media_group_id:
            # Albums arrive as separate updates; gather them into one turn.
            group_id = message.media_group_id
            buffer = self._media_groups.get(group_id)
            if buffer is not None:
                buffer.messages.append(message)
                return
            buffer = self._media_groups[group_id] = MediaGroupBuffer([message])
            await asyncio.sleep(MEDIA_GROUP_WAIT_SECONDS)
            del self._media_groups[group_id]
            messages = buffer.messages
        else:
            messages = [message]

        try:
            content = await self._to_content(messages)
        except Exception as exc:
            log.exception("failed to read message")
            await message.reply_text(f"Sorry, I couldn't read that: {exc}")
            return
        if not content:
            return
        await self._run_turn(message, content)

    async def _to_content(self, messages: list[Message]) -> list[dict]:
        content: list[dict] = []
        texts: list[str] = []
        for m in messages:
            file_id, media_type = None, None
            if m.photo:
                file_id, media_type = m.photo[-1].file_id, "image/jpeg"  # largest size
            elif m.document and (m.document.mime_type or "").startswith("image/"):
                file_id, media_type = m.document.file_id, m.document.mime_type
            if file_id:
                tg_file = await self.application.bot.get_file(file_id)
                data, media_type = _prepare_image(bytes(await tg_file.download_as_bytearray()), media_type)
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode()},
                })
            if m.text:
                texts.append(m.text)
            if m.caption:
                texts.append(m.caption)
        if texts:
            content.append({"type": "text", "text": "\n".join(texts)})
        return content

    async def _run_turn(self, message: Message, content: list[dict]) -> None:
        chat_id = message.chat_id
        typing = asyncio.create_task(self._keep_typing(chat_id))
        try:
            reply = await self.agent.handle(chat_id, content, TelegramChat(self, chat_id))
        except Exception:
            log.exception("turn failed")
            reply = "Sorry, something went wrong on my end. Check the server logs."
        finally:
            typing.cancel()
        for i in range(0, len(reply), TELEGRAM_TEXT_LIMIT):
            await self.application.bot.send_message(chat_id, reply[i : i + TELEGRAM_TEXT_LIMIT])

    async def _keep_typing(self, chat_id: int) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                with contextlib.suppress(Exception):
                    await self.application.bot.send_chat_action(chat_id, ChatAction.TYPING)
                await asyncio.sleep(4.5)
