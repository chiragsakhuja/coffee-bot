"""Slack front-end: handlers driven with Slack-shaped payloads against a fake Web API client."""

import asyncio
import io
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from coffee_bot import bot as bot_module
from coffee_bot.bot import CoffeeBot, SlackChat, _prepare_image, escape, strip_mention, unescape
from coffee_bot.devices import DeviceRegistry
from coffee_bot.sandbox import Sandbox
from coffee_bot.store import MenuStore
from coffee_bot.tools import Services
from test_tools import FakeRenderer

ME, STRANGER = "U0ALLOWED1", "U0STRANGE1"


class FakeClient:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, method):
        async def call(**kwargs):
            self.calls.append((method, kwargs))
            if method == "chat_postMessage":
                return {"channel": kwargs["channel"], "ts": f"{len(self.calls)}.0"}
            if method == "conversations_open":
                return {"channel": {"id": "D_" + kwargs["users"]}}
            return {}
        return call

    def named(self, method):
        return [kw for m, kw in self.calls if m == method]


class FakeAgent:
    def __init__(self, reply="Done."):
        self.reply = reply
        self.turns = []
        self.resets = []

    async def handle(self, chat_id, content, chat):
        self.turns.append((chat_id, content))
        return self.reply

    def reset(self, chat_id):
        self.resets.append(chat_id)


@pytest.fixture
def make_bot(tmp_path):
    def make(agent=None):
        renderer = FakeRenderer()
        renderer.cache = None
        svc = Services(
            store=MenuStore(tmp_path / "data"),
            renderer=renderer,
            sandbox=Sandbox(tmp_path, (tmp_path,), (tmp_path,)),
            tz=ZoneInfo("UTC"),
            devices=DeviceRegistry(tmp_path / "data"),
        )
        b = CoffeeBot("xoxb-test", "xapp-test", frozenset({ME}), agent or FakeAgent(), svc)
        b.client = FakeClient()
        b.bot_user_id = "UBOT"
        return b
    return make


async def noop(*args, **kwargs):
    pass


def test_escape_roundtrip():
    s = "ls > out.txt && cat <file>"
    assert escape(s) == "ls &gt; out.txt &amp;&amp; cat &lt;file&gt;"
    assert unescape(escape(s)) == s


def test_strip_mention():
    assert strip_mention("<@UBOT> espresso is dialed", "UBOT") == "espresso is dialed"
    assert strip_mention("hey <@UOTHER>", "UBOT") == "hey <@UOTHER>"


def test_prepare_image_rotates_and_downscales():
    im = Image.new("RGB", (3000, 1000))
    exif = im.getexif()
    exif[0x0112] = 6  # rotate 90° CW on display
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    data, media_type = _prepare_image(buf.getvalue(), "image/jpeg")
    assert media_type == "image/jpeg"
    with Image.open(io.BytesIO(data)) as out:
        assert out.size == (667, 2000)  # upright, long side capped

    small = io.BytesIO()
    Image.new("RGB", (100, 100)).save(small, "PNG")
    assert _prepare_image(small.getvalue(), "image/png") == (small.getvalue(), "image/png")


async def test_dm_runs_a_turn_and_replies(make_bot):
    agent = FakeAgent("Added it <3 & saved.")
    b = make_bot(agent)
    await b.on_message({"channel_type": "im", "channel": "D1", "user": ME, "ts": "1.1", "text": "V60 &amp; 15g"})
    assert agent.turns == [("D1", [{"type": "text", "text": "V60 & 15g"}])]
    [post] = b.client.named("chat_postMessage")
    assert post["channel"] == "D1" and post["thread_ts"] is None
    assert post["text"] == "Added it &lt;3 &amp; saved."
    assert b.client.named("reactions_add")[0]["timestamp"] == "1.1"
    assert b.client.named("reactions_remove")[0]["timestamp"] == "1.1"


async def test_ignores_strangers_bots_and_edits(make_bot):
    agent = FakeAgent()
    b = make_bot(agent)
    await b.on_message({"channel_type": "im", "channel": "D1", "user": STRANGER, "ts": "1", "text": "hi"})
    await b.on_message({"channel_type": "im", "channel": "D1", "bot_id": "B1", "ts": "2", "text": "hi"})
    await b.on_message({"channel_type": "im", "channel": "D1", "user": ME, "subtype": "message_changed", "ts": "3"})
    await b.on_message({"channel_type": "channel", "channel": "C1", "user": ME, "ts": "4", "text": "hi"})
    assert agent.turns == [] and b.client.calls == []


async def test_mention_replies_in_thread(make_bot):
    agent = FakeAgent()
    b = make_bot(agent)
    await b.on_mention({"channel": "C1", "user": ME, "ts": "5.5", "text": "<@UBOT> espresso is dialed"})
    assert agent.turns == [("C1", [{"type": "text", "text": "espresso is dialed"}])]
    assert b.client.named("chat_postMessage")[0]["thread_ts"] == "5.5"


async def test_photo_is_downloaded_and_sent_as_image(make_bot, monkeypatch):
    agent = FakeAgent()
    b = make_bot(agent)
    png = io.BytesIO()
    Image.new("RGB", (10, 10)).save(png, "PNG")

    async def download(f):
        assert f["url_private_download"] == "https://files.slack.com/x.png"
        return png.getvalue()

    monkeypatch.setattr(b, "_download", download)
    await b.on_message({
        "channel_type": "im", "channel": "D1", "user": ME, "ts": "1", "subtype": "file_share", "text": "blue bin",
        "files": [
            {"mimetype": "image/png", "url_private_download": "https://files.slack.com/x.png"},
            {"mimetype": "application/pdf", "url_private_download": "https://files.slack.com/x.pdf"},
        ],
    })
    [(_, content)] = agent.turns
    assert [c["type"] for c in content] == ["image", "text"]
    assert content[0]["source"]["media_type"] == "image/png"


async def test_approval_button_resolves_pending_command(make_bot):
    b = make_bot()
    chat = SlackChat(b, "D1")
    task = asyncio.create_task(chat.request_approval("df -h > /tmp/x", "check disk"))
    await asyncio.sleep(0)
    [post] = b.client.named("chat_postMessage")
    section, actions = post["blocks"]
    assert "df -h &gt; /tmp/x" in section["text"]["text"]
    token = actions["elements"][0]["value"]

    body = {
        "user": {"id": STRANGER}, "actions": [{"action_id": "approve_yes", "value": token}],
        "channel": {"id": "D1"}, "message": {"ts": "9.9", "blocks": post["blocks"]},
    }
    responses = []

    async def respond(**kwargs):
        responses.append(kwargs)

    await b.on_approval(noop, body, respond)
    assert not task.done() and responses[0]["text"] == "Not allowed."

    body["user"]["id"] = ME
    await b.on_approval(noop, body, respond)
    assert await task is True
    [update] = b.client.named("chat_update")
    assert update["text"].endswith("✅ Approved") and update["blocks"][0]["type"] == "section"


async def test_approval_timeout(make_bot, monkeypatch):
    monkeypatch.setattr(bot_module, "APPROVAL_TIMEOUT_SECONDS", 0.01)
    b = make_bot()
    assert await SlackChat(b, "D1").request_approval("ls", "list") is False
    assert "No answer" in b.client.named("chat_update")[0]["text"]
    assert b.pending_approvals == {}


async def test_pairing_flow(make_bot):
    b = make_bot()
    device = b.svc.devices.add_pending("AA:BB:CC:DD:EE:01", "1.8.6", "og")
    await b.notify_pairing(device)
    [post] = b.client.named("chat_postMessage")
    assert post["channel"] == f"D_{ME}"
    assert post["blocks"][1]["elements"][0]["action_id"] == "pair_yes"

    body = {
        "user": {"id": ME}, "actions": [{"action_id": "pair_yes", "value": device.friendly_id}],
        "channel": {"id": post["channel"]}, "message": {"ts": "9.9", "blocks": post["blocks"]},
    }
    await b.on_pairing(noop, body, noop)
    assert b.svc.devices.by_friendly_id(device.friendly_id).status == "approved"
    assert "Approved" in b.client.named("chat_update")[0]["text"]

    await b.on_pairing(noop, body, noop)  # second click
    assert "Already handled (approved)" in b.client.named("chat_update")[1]["text"]


async def test_slash_commands(make_bot):
    agent = FakeAgent()
    b = make_bot(agent)
    responses = []

    async def respond(text):
        responses.append(text)

    await b.on_command(noop, {"user_id": ME, "channel_id": "D1", "text": "new"}, respond)
    assert agent.resets == ["D1"]

    await b.on_command(noop, {"user_id": ME, "channel_id": "D1", "text": "menu"}, respond)
    [upload] = b.client.named("files_upload_v2")
    assert upload["channel"] == "D1" and upload["file"] == b"png"

    await b.on_command(noop, {"user_id": ME, "channel_id": "D1", "text": "forget nope"}, respond)
    assert "No device with friendly ID NOPE" in b.client.named("chat_postMessage")[-1]["text"]

    await b.on_command(noop, {"user_id": STRANGER, "channel_id": "D1", "text": "undo"}, respond)
    assert "allowlist" in responses[0]


async def test_bolt_routes_payloads_to_handlers(make_bot, monkeypatch):
    """Real Bolt middleware + routing, with the Slack Web API stubbed out."""
    from slack_bolt.request.async_request import AsyncBoltRequest
    from slack_sdk.web.async_client import AsyncWebClient
    from slack_sdk.web.async_slack_response import AsyncSlackResponse

    async def api_call(self, api_method, **kwargs):  # used by Bolt's own auth.test
        data = {"ok": True, "user_id": "UBOT", "bot_id": "B1", "team_id": "T1", "user": "coffee-bot"}
        return AsyncSlackResponse(client=self, http_verb="POST", api_url=api_method, req_args={},
                                  data=data, headers={}, status_code=200)

    monkeypatch.setattr(AsyncWebClient, "api_call", api_call)
    agent = FakeAgent()
    b = make_bot(agent)
    device = b.svc.devices.add_pending("AA:BB:CC:DD:EE:02")

    async def dispatch(body):
        resp = await b.app.async_dispatch(AsyncBoltRequest(body=body, mode="socket_mode"))
        assert resp.status == 200, resp.body
        for _ in range(100):  # event listeners run after the ack
            await asyncio.sleep(0.01)
            if not any(not t.done() for t in asyncio.all_tasks() if t is not asyncio.current_task()):
                break

    await dispatch({
        "type": "event_callback", "team_id": "T1", "api_app_id": "A1",
        "event": {"type": "message", "channel_type": "im", "channel": "D1", "user": ME, "ts": "1.1", "text": "hello"},
    })
    assert agent.turns == [("D1", [{"type": "text", "text": "hello"}])]

    await dispatch({"command": "/coffee", "text": "new", "user_id": ME, "channel_id": "D1", "team_id": "T1",
                    "response_url": "https://hooks.slack.com/x"})
    assert agent.resets == ["D1"]

    await dispatch({
        "type": "block_actions", "team": {"id": "T1"}, "user": {"id": ME}, "channel": {"id": "D1"},
        "message": {"ts": "2.2", "blocks": []}, "response_url": "https://hooks.slack.com/x",
        "actions": [{"type": "button", "action_id": "pair_yes", "value": device.friendly_id}],
    })
    assert b.svc.devices.by_friendly_id(device.friendly_id).status == "approved"
