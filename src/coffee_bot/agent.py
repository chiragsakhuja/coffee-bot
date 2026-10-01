"""Claude conversation loop: one append-only history per Telegram chat."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaMessageParam

from .models import MAX_COFFEES
from .tools import ChatIO, Services, build_tools

log = logging.getLogger(__name__)

IDLE_RESET_SECONDS = 6 * 60 * 60
MAX_ITERATIONS = 30

SYSTEM_PROMPT = f"""\
You run a small coffee bar's menu, shown on a TRMNL e-ink display (800x480, 4 shades of gray) in the \
user's kitchen. The user chats with you on Telegram, sending photos and notes about coffee bags, brew \
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
one in Telegram. Only run commands when they clearly help with what the user asked.

Style
- Your replies go to Telegram as plain text. Don't use Markdown formatting. Keep replies short and \
friendly: confirm what changed in a sentence or two, or ask your questions.
"""


@dataclass
class Conversation:
    messages: list[BetaMessageParam] = field(default_factory=list)
    last_active: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class CoffeeAgent:
    def __init__(self, client: AsyncAnthropic, model: str, services: Services):
        self.client = client
        self.model = model
        self.svc = services
        self._conversations: dict[int, Conversation] = {}

    def reset(self, chat_id: int) -> None:
        self._conversations.pop(chat_id, None)

    def _conversation(self, chat_id: int) -> Conversation:
        conv = self._conversations.get(chat_id)
        if conv is None or (
            not conv.lock.locked() and time.monotonic() - conv.last_active > IDLE_RESET_SECONDS
        ):
            conv = self._conversations[chat_id] = Conversation()
        return conv

    async def handle(self, chat_id: int, content: list[dict], chat: ChatIO) -> str:
        """Run one user turn (text and/or images) to completion and return Claude's final text."""
        conv = self._conversation(chat_id)
        async with conv.lock:
            now = datetime.now(self.svc.tz).strftime("%Y-%m-%d %H:%M (%A)")
            header = f"[Sent {now}]"
            if not conv.messages:
                # Start of a conversation: include the current menu so Claude doesn't need a get_menu round trip.
                menu_json = self.svc.store.load().model_dump_json(indent=2)
                header += f"\n<current_menu>\n{menu_json}\n</current_menu>"
            user_msg: BetaMessageParam = {"role": "user", "content": [{"type": "text", "text": header}, *content]}

            history = [*conv.messages, user_msg]
            new_messages: list[BetaMessageParam] = [user_msg]
            try:
                final = await self._run(history, new_messages, chat)
            except anthropic.APIStatusError as exc:
                log.exception("Anthropic API error")
                return f"Sorry, the Claude API returned an error ({exc.status_code}). Try again in a moment."
            except anthropic.APIConnectionError:
                log.exception("Anthropic connection error")
                return "Sorry, I couldn't reach the Claude API. Try again in a moment."

            # Only commit the turn if it finished cleanly, so the history stays valid (append-only).
            conv.messages.extend(new_messages)
            conv.last_active = time.monotonic()
            return final

    async def _run(self, history: list[BetaMessageParam], new_messages: list[BetaMessageParam], chat: ChatIO) -> str:
        runner = self.client.beta.messages.tool_runner(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=build_tools(self.svc, chat),
            messages=history,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            cache_control={"type": "ephemeral"},
            # Server-side fallback: if a safety classifier declines, the API retries on a suitable model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            max_iterations=MAX_ITERATIONS,
        )

        last = None
        async for message in runner:
            last = message
            # Mirror the history; the runner keeps its own copy and doesn't expose it.
            new_messages.append(message.to_param())
            tool_response = await runner.generate_tool_call_response()  # cached, tools run once
            if tool_response is not None:
                new_messages.append(tool_response)

        if last is None:
            return "Sorry, I didn't get a response."
        if last.stop_reason == "refusal":
            # Drop the whole refused turn from history so the conversation can continue.
            new_messages.clear()
            return "Sorry, I can't help with that one."
        if new_messages[-1]["role"] == "user":
            # Hit max_iterations with tool results pending; add a closing assistant turn to keep history valid.
            new_messages.append({"role": "assistant", "content": "(Stopped: too many steps in one turn.)"})
            return "I stopped after too many steps. Tell me how you'd like to continue."

        text = "\n".join(b.text for b in last.content if b.type == "text").strip()
        if last.stop_reason == "max_tokens":
            text += "\n\n(My reply was cut off.)"
        return text or "Done."
