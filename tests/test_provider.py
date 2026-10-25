import json

import httpx
import pytest


async def test_tool_and_image_payload():
    from cadnext.provider import Provider
    from cadnext.settings import Profile

    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "read", "arguments": '{"x": 62}'}}]}, "finish_reason": "tool_calls"}]})

    client = Provider(Profile(name="P", base_url="http://localhost/v1", model="m"),
                      transport=httpx.MockTransport(respond))
    try:
        response = await client.complete([{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}}
        ]}], [{"type": "function", "function": {"name": "read"}}])
        assert response["tool_calls"][0]["id"] == "c1"
        assert captured[0]["tools"][0]["function"]["name"] == "read"
        assert captured[0]["messages"][0]["content"][0]["type"] == "image_url"
    finally:
        await client.aclose()


async def test_provider_error_never_echoes_secret():
    from cadnext.provider import Provider, ProviderError
    from cadnext.settings import Profile

    secret = "private-test-value"
    client = Provider(Profile(name="P", base_url="https://example.test/v1", model="m"),
                      secret=secret, transport=httpx.MockTransport(
                          lambda request: httpx.Response(401, text=secret)))
    try:
        with pytest.raises(ProviderError) as caught:
            await client.complete([], [])
        assert secret not in str(caught.value)
    finally:
        await client.aclose()


async def test_models_failure_does_not_require_model_discovery():
    from cadnext.provider import Provider, ProviderError
    from cadnext.settings import Profile

    client = Provider(Profile(name="P", base_url="http://localhost/v1", model="manual"),
                      transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    try:
        with pytest.raises(ProviderError):
            await client.list_models()
        assert client.profile.model == "manual"
    finally:
        await client.aclose()
