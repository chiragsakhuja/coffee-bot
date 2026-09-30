"""Drives CoffeeAgent through the real SDK tool runner against a mocked Messages API."""

import json
from zoneinfo import ZoneInfo

import httpx2 as httpx
import pytest
from anthropic import AsyncAnthropic

from coffee_bot.agent import CoffeeAgent
from coffee_bot.sandbox import Sandbox
from coffee_bot.store import MenuStore
from coffee_bot.tools import Services
from test_tools import FakeChat, FakeRenderer


def api_message(content, stop_reason):
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


@pytest.fixture
def svc(tmp_path):
    return Services(
        store=MenuStore(tmp_path / "data"),
        renderer=FakeRenderer(),
        sandbox=Sandbox(tmp_path, (tmp_path,), (tmp_path,)),
        tz=ZoneInfo("UTC"),
    )


async def test_tool_turn_then_followup(svc):
    requests = []
    replies = [
        api_message([{
            "type": "tool_use", "id": "toolu_1", "name": "add_coffee",
            "input": {"roaster": "Onyx", "name": "Geisha", "roast_date": "2026-09-20",
                      "container": "green", "tasting_notes": ["jasmine"]},
        }], "tool_use"),
        api_message([{"type": "text", "text": "Added Onyx Geisha."}], "end_turn"),
        api_message([{"type": "text", "text": "It's in the green container."}], "end_turn"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.headers, json.loads(request.content)))
        return httpx.Response(200, json=replies[len(requests) - 1])

    client = AsyncAnthropic(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    agent = CoffeeAgent(client, "claude-opus-5-5", svc)
    chat = FakeChat()

    reply = await agent.handle(1, [{"type": "text", "text": "new bag, green bin"}], chat)
    assert reply == "Added Onyx Geisha."
    assert svc.store.load().coffees[0].container == "green"

    headers, body = requests[0]
    assert "server-side-fallback-2026-07-01" in headers["anthropic-beta"]
    assert body["fallbacks"] == "default"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "medium"}
    assert "<current_menu>" in body["messages"][0]["content"][0]["text"]

    # Tool result was sent back with the tool_use id
    tool_result = requests[1][1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "toolu_1"

    # Second turn resends the full, valid history (user, assistant, user, assistant, user)
    reply = await agent.handle(1, [{"type": "text", "text": "which bin?"}], chat)
    assert reply == "It's in the green container."
    roles = [m["role"] for m in requests[2][1]["messages"]]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    assert "<current_menu>" not in requests[2][1]["messages"][-1]["content"][0]["text"]


async def test_refusal_is_not_committed(svc):
    def handler(request):
        return httpx.Response(200, json=api_message([], "refusal"))

    client = AsyncAnthropic(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    agent = CoffeeAgent(client, "claude-opus-5-5", svc)
    reply = await agent.handle(1, [{"type": "text", "text": "hi"}], FakeChat())
    assert "can't help" in reply
    assert agent._conversations[1].messages == []
