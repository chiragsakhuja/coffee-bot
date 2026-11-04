"""Drives CoffeeAgent through the real OpenAI SDK against a mocked OpenRouter chat-completions API."""

import json
from zoneinfo import ZoneInfo

import httpx2 as httpx
import pytest
from openai import AsyncOpenAI

from coffee_bot.agent import CoffeeAgent
from coffee_bot.devices import DeviceRegistry
from coffee_bot.sandbox import Sandbox
from coffee_bot.store import MenuStore
from coffee_bot.tools import Services
from test_tools import FakeChat, FakeRenderer


def api_message(content=None, finish_reason="stop", tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "gen-1", "object": "chat.completion", "created": 0, "model": "anthropic/claude-opus-5-5",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }


def make_client(handler):
    return AsyncOpenAI(
        base_url="https://openrouter.test/api/v1", api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.fixture
def svc(tmp_path):
    return Services(
        store=MenuStore(tmp_path / "data"),
        renderer=FakeRenderer(),
        sandbox=Sandbox(tmp_path, (tmp_path,), (tmp_path,)),
        tz=ZoneInfo("UTC"),
        devices=DeviceRegistry(tmp_path / "data"),
    )


async def test_tool_turn_then_followup(svc):
    requests = []
    args = {"roaster": "Onyx", "name": "Geisha", "roast_date": "2026-09-20",
            "container": "green", "tasting_notes": ["jasmine"]}
    replies = [
        api_message(None, "tool_calls", [{
            "id": "call_1", "type": "function",
            "function": {"name": "add_coffee", "arguments": json.dumps(args)},
        }]),
        api_message("Added Onyx Geisha."),
        api_message("It's in the green container."),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=replies[len(requests) - 1])

    agent = CoffeeAgent(make_client(handler), "anthropic/claude-opus-5-5", svc)
    chat = FakeChat()

    reply = await agent.handle("D1", [{"type": "text", "text": "new bag, green bin"}], chat)
    assert reply == "Added Onyx Geisha."
    assert svc.store.load().coffees[0].container == "green"

    body = requests[0]
    assert body["model"] == "anthropic/claude-opus-5-5"
    assert body["messages"][0]["role"] == "system"
    assert "<current_menu>" in body["messages"][1]["content"][0]["text"]
    tool_names = {t["function"]["name"] for t in body["tools"]}
    assert {"add_coffee", "render_preview", "set_refresh_schedule"} <= tool_names
    add = next(t for t in body["tools"] if t["function"]["name"] == "add_coffee")["function"]
    assert add["parameters"]["required"] == ["roaster", "name", "roast_date", "container", "tasting_notes"]
    assert add["parameters"]["properties"]["container"]["enum"] == ["black", "white", "green", "blue"]
    assert "YYYY-MM-DD" in add["parameters"]["properties"]["roast_date"]["description"]

    # Tool result was sent back with the tool call id
    tool_result = requests[1]["messages"][-1]
    assert tool_result["role"] == "tool" and tool_result["tool_call_id"] == "call_1"
    assert "onyx-geisha" in tool_result["content"]

    # Second turn resends the full, valid history
    reply = await agent.handle("D1", [{"type": "text", "text": "which bin?"}], chat)
    assert reply == "It's in the green container."
    roles = [m["role"] for m in requests[2]["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert "<current_menu>" not in requests[2]["messages"][-1]["content"][0]["text"]


async def test_tool_errors_go_back_to_the_model(svc):
    requests = []
    bad = {"roaster": "A", "name": "B", "roast_date": "last week", "container": "green", "tasting_notes": []}
    replies = [
        api_message(None, "tool_calls", [{
            "id": "call_1", "type": "function", "function": {"name": "add_coffee", "arguments": json.dumps(bad)},
        }]),
        api_message("What's the roast date?"),
    ]

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=replies[len(requests) - 1])

    agent = CoffeeAgent(make_client(handler), "m", svc)
    reply = await agent.handle("D1", [{"type": "text", "text": "hi"}], FakeChat())
    assert reply == "What's the roast date?"
    assert "YYYY-MM-DD" in requests[1]["messages"][-1]["content"]
    assert svc.store.load().coffees == []


async def test_content_filter_is_not_committed(svc):
    def handler(request):
        return httpx.Response(200, json=api_message("", "content_filter"))

    agent = CoffeeAgent(make_client(handler), "m", svc)
    reply = await agent.handle("D1", [{"type": "text", "text": "hi"}], FakeChat())
    assert "can't help" in reply
    assert agent._conversations["D1"].messages == []


async def test_api_error_is_reported(svc):
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "bad key", "code": 401}})

    agent = CoffeeAgent(make_client(handler), "m", svc)
    reply = await agent.handle("D1", [{"type": "text", "text": "hi"}], FakeChat())
    assert "401" in reply
