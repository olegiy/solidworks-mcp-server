import asyncio
import json

import httpx
import pytest

from cadnext.provider import Provider, ProviderError
from cadnext.settings import Profile


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, text):
        self.data = text.encode()

    async def __aiter__(self):
        for offset in range(0, len(self.data), 7):
            yield self.data[offset:offset + 7]
            await asyncio.sleep(0)


def sse(chunks, done=True):
    return "".join("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n" for chunk in chunks) + (
        "data: [DONE]\n\n" if done else "")


async def test_stream_assembles_native_tools_utf8_and_usage_before_returning():
    captured = []
    chunks = [
        {"choices": [{"delta": {"role": "assistant", "content": "Готов"}}]},
        {"choices": [{"delta": {"content": "о", "tool_calls": [{"index": 0, "id": "c1",
          "type": "function", "function": {"name": "get_", "arguments": '{"name":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {
          "name": "dimension", "arguments": '"D1@Эскиз1"}'}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 12,
                                   "prompt_tokens_details": {"cached_tokens": 30}}},
    ]

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, stream=BytesStream(sse(chunks)))

    provider = Provider(Profile(name="P", base_url="http://localhost/v1", model="m"),
                        transport=httpx.MockTransport(respond))
    try:
        message = await provider.complete([], [], stream=True)
        assert captured[0]["stream"] is True
        assert message["content"] == "Готово"
        assert message["tool_calls"] == [{"id": "c1", "type": "function", "function": {
            "name": "get_dimension", "arguments": '{"name":"D1@Эскиз1"}'}}]
        assert provider.last_metrics["input_tokens"] == 40
        assert provider.last_metrics["cached_tokens"] == 30
        assert 0 <= provider.last_metrics["first_token_seconds"] <= provider.last_metrics["duration_seconds"]
    finally:
        await provider.aclose()


@pytest.mark.parametrize("body", [
    sse([{"choices": [{"delta": {"content": "partial"}, "finish_reason": "stop"}]}], done=False),
    sse([{"choices": [{"delta": {"content": "partial"}, "finish_reason": "length"}]}]),
    sse([{"error": {"message": "private-server-secret"}}]),
    sse([{"choices": [{"delta": {"tool_calls": [{"index": -1}]}, "finish_reason": "tool_calls"}]}]),
    sse([{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c", "type": "function",
        "function": {"name": "get_dimension", "arguments": '{"name":'}}]},
        "finish_reason": "tool_calls"}]}]),
])
async def test_incomplete_or_failed_stream_never_returns_a_partial_command(body):
    provider = Provider(Profile(name="P", base_url="http://localhost/v1", model="m"),
                        transport=httpx.MockTransport(lambda request:
                            httpx.Response(200, stream=BytesStream(body))))
    try:
        with pytest.raises(ProviderError) as error:
            await provider.complete([], [], stream=True)
        assert "private-server-secret" not in str(error.value)
    finally:
        await provider.aclose()


async def test_all_completion_requests_share_a_two_request_limit():
    active = maximum = requests = 0
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(request):
        nonlocal active, maximum, requests
        requests += 1
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            entered.set()
        await release.wait()
        active -= 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"},
                                                     "finish_reason": "stop"}]})

    provider = Provider(Profile(name="P", base_url="http://localhost/v1", model="m"),
                        transport=httpx.MockTransport(respond))
    tasks = [asyncio.create_task(provider.complete([], [])) for _ in range(6)]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(0)
        assert requests == 2
        release.set()
        assert len(await asyncio.gather(*tasks)) == 6
        assert maximum == 2
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await provider.aclose()


async def test_reported_usage_is_kept_when_stream_ends_without_done():
    body = sse([{"choices": [{"delta": {"content": "partial"}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3,
                                            "cost": .002}}], done=False)
    provider = Provider(Profile(name="P", base_url="http://localhost/v1", model="m"),
        transport=httpx.MockTransport(lambda request:
            httpx.Response(200, stream=BytesStream(body))))
    try:
        with pytest.raises(ProviderError):
            await provider.complete([], [], stream=True)
        assert provider.last_metrics["input_tokens"] == 10
        assert provider.last_metrics["cost"] == .002
    finally:
        await provider.aclose()
