"""LLM conversation loop (OpenRouter, OpenAI-compatible chat completions): one append-only history per Slack conversation."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

import openai
from openai import AsyncOpenAI

from .models import MAX_COFFEES
from .tools import ChatIO, Services, build_tools

log = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MessageParam = dict  # OpenAI chat-completions message

IDLE_RESET_SECONDS = 6 * 60 * 60
MAX_ITERATIONS = 30

SYSTEM_PROMPT = f"""\
You run a small coffee bar's menu, shown on a TRMNL e-ink display (800x480, 4 shades of gray) in the \
user's kitchen. The user chats with you on Slack, sending photos and notes about coffee bags, brew \
recipes, and dial-in results. You keep the menu up to date with your tools; the display image is \
re-rendered automatically whenever the menu changes.

The menu
- Holds 1 to {MAX_COFFEES} coffees. Each has a roaster, a name, a roast date, tasting notes, the storage \
container it's in (black, white, green, or blue), optional origin/process/roast level, and a list of \
brew methods.
- Brew methods change over time as the user dials in. Use upsert_brew_method to record the latest \
recipe and set its status: "trying" for a first attempt, "dialing_in" while adjusting, "dialed" once \
the user says it's good. Keep brew notes short: they're printed on a small card. Put anything that \
doesn't fit the typed fields into params (for example bloom, pours, pressure profile, filter).

Reading bag photos
- Read the roaster, coffee name, tasting notes, origin, process, roast level, and roast date from the \
bag. Keep tasting notes as the short phrases printed on the bag.
- The roast date is required. If it isn't visible or legible, ask for it. Don't guess. If the bag \
only shows a day and month, assume the most recent past occurrence of that date.
- The container is almost never on the bag. If the user hasn't said which container the beans are \
in, ask before adding the coffee.
- If you're unsure about something that would end up on the display, ask a short question instead \
of guessing. Collect all your questions into one message.
- If the menu already has {MAX_COFFEES} coffees and a new one arrives, ask which to remove.

The display template
- The display is rendered from templates/menu.html.j2 (Jinja2) and templates/menu.css. When the user \
asks for layout or style changes, read those files, edit them with write_file, then call \
render_preview. Use only the colors #000, #555, #aaa, #fff. The panel has exactly four grays.
- After changing the menu or the template, call render_preview so the user sees the result.

The display device
- The TRMNL wakes up on a schedule, fetches the latest image from this server, then sleeps. Menu \
changes show up at its next wake. Use get_device_status for questions about the display (battery, \
Wi-Fi, last check-in) and set_refresh_schedule to change how often it refreshes or its overnight \
sleep window. Mention that shorter intervals use more battery.

Shell commands
- You can run shell commands in the project directory with run_shell_command. The user approves each \
one in Slack with a button. Only run commands when they clearly help with what the user asked.

Style
- Your replies go to Slack. Write plain text; if you need emphasis use Slack's *bold* (single \
asterisks), never Markdown headings, tables, or **double asterisks**. Keep replies short and \
friendly: confirm what changed in a sentence or two, or ask your questions.
"""


@dataclass
class Conversation:
    messages: list[MessageParam] = field(default_factory=list)
    last_active: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class CoffeeAgent:
    def __init__(self, client: AsyncOpenAI, model: str, services: Services):
        self.client = client
        self.model = model
        self.svc = services
        self._conversations: dict[str, Conversation] = {}

    def reset(self, chat_id: str) -> None:
        self._conversations.pop(chat_id, None)

    def _conversation(self, chat_id: str) -> Conversation:
        conv = self._conversations.get(chat_id)
        if conv is None or (
            not conv.lock.locked() and time.monotonic() - conv.last_active > IDLE_RESET_SECONDS
        ):
            conv = self._conversations[chat_id] = Conversation()
        return conv

    async def handle(self, chat_id: str, content: list[dict], chat: ChatIO) -> str:
        """Run one user turn (text and/or images) to completion and return the model's final text."""
        conv = self._conversation(chat_id)
        async with conv.lock:
            now = datetime.now(self.svc.tz).strftime("%Y-%m-%d %H:%M (%A)")
            header = f"[Sent {now}]"
            if not conv.messages:
                # Start of a conversation: include the current menu so the model doesn't need a get_menu round trip.
                menu_json = self.svc.store.load().model_dump_json(indent=2)
                header += f"\n<current_menu>\n{menu_json}\n</current_menu>"
            user_msg: MessageParam = {"role": "user", "content": [{"type": "text", "text": header}, *content]}

            history = [*conv.messages, user_msg]
            new_messages: list[MessageParam] = [user_msg]
            try:
                final = await self._run(history, new_messages, chat)
            except openai.APIStatusError as exc:
                log.exception("OpenRouter API error")
                return f"Sorry, the model API returned an error ({exc.status_code}). Try again in a moment."
            except openai.APIConnectionError:
                log.exception("OpenRouter connection error")
                return "Sorry, I couldn't reach the model API. Try again in a moment."

            # Only commit the turn if it finished cleanly, so the history stays valid (append-only).
            conv.messages.extend(new_messages)
            conv.last_active = time.monotonic()
            return final

    async def _run(self, history: list[MessageParam], new_messages: list[MessageParam], chat: ChatIO) -> str:
        tools = {t.name: t for t in build_tools(self.svc, chat)}
        specs = [t.spec() for t in tools.values()]

        choice = None
        for _ in range(MAX_ITERATIONS):
            response = await self.client.chat.completions.create(
                model=self.model,
                max_tokens=16000,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}, *history],
                tools=specs,
            )
            if not response.choices:
                return "Sorry, I didn't get a response."
            choice = response.choices[0]
            msg = choice.message
            if choice.finish_reason == "content_filter":
                # Drop the whole refused turn from history so the conversation can continue.
                new_messages.clear()
                return "Sorry, I can't help with that one."

            assistant: MessageParam = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                assistant["tool_calls"] = [
                    {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                    for c in msg.tool_calls
                ]
            history.append(assistant)
            new_messages.append(assistant)
            if not msg.tool_calls:
                break
            for call in msg.tool_calls:
                result = await self._run_tool(tools, call.function.name, call.function.arguments)
                tool_msg = {"role": "tool", "tool_call_id": call.id, "content": result}
                history.append(tool_msg)
                new_messages.append(tool_msg)
        else:
            # Hit MAX_ITERATIONS with tool results pending; add a closing assistant turn to keep history valid.
            closing = {"role": "assistant", "content": "(Stopped: too many steps in one turn.)"}
            history.append(closing)
            new_messages.append(closing)
            return "I stopped after too many steps. Tell me how you'd like to continue."

        text = (choice.message.content or "").strip()
        if choice.finish_reason == "length":
            text += "\n\n(My reply was cut off.)"
        return text or "Done."

    @staticmethod
    async def _run_tool(tools: dict, name: str, raw_arguments: str) -> str:
        """Run one tool call; errors go back to the model as text so it can correct itself."""
        tool = tools.get(name)
        if tool is None:
            return f"Error: unknown tool {name!r}"
        try:
            arguments = json.loads(raw_arguments or "{}")
            return await tool.call(arguments)
        except Exception as exc:
            if not isinstance(exc, ValueError):  # pydantic's ValidationError is a ValueError too
                log.exception("tool %s failed", name)
            return f"Error: {exc}"
