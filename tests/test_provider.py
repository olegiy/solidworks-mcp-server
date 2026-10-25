import json

import httpx
import pytest


@pytest.mark.parametrize("effort", [None, "none", "minimal", "low", "medium", "high", "xhigh", "max"])
async def test_reasoning_effort_is_sent_only_when_selected(effort):
    from cadnext.provider import Provider
    from cadnext.settings import Profile

    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "Готово"},
                                                     "finish_reason": "stop"}]})

    profile = Profile(name="P", base_url="http://localhost/v1", model="m", reasoning_effort=effort)
    provider = Provider(profile, transport=httpx.MockTransport(respond))
    try:
        await provider.complete([{"role": "user", "content": "Привет"}], [])
    finally:
        await provider.aclose()
    payload, = captured
    if effort is None:
        assert "reasoning_effort" not in payload
    else:
        assert payload["reasoning_effort"] == effort


async def test_tool_and_image_payload():
    from cadnext.provider import Provider
    from cadnext.settings import Profile

    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "read", "arguments": '{"x": 62}'}}]}, "finish_reason": "tool_calls"}]})

    client = Provider(Profile(name="P", base_url="http://localhost/v1", model="m", vision=True),
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
    from cadnext.privacy import PrivacyContext
    from cadnext.provider import Provider, ProviderError
    from cadnext.settings import Profile

    secret = "private-test-value"
    client = Provider(Profile(name="P", base_url="https://example.test/v1", model="m"),
                      secret=secret, privacy=PrivacyContext(local=False, consent=True),
                      transport=httpx.MockTransport(
                          lambda request: httpx.Response(401, text=secret)))
    try:
        with pytest.raises(ProviderError) as caught:
            await client.complete([], [])
        assert secret not in str(caught.value)
    finally:
        await client.aclose()


async def test_remote_cad_is_not_sent_without_consent():
    from cadnext.privacy import ConsentRequired
    from cadnext.provider import Provider
    from cadnext.settings import Profile

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={})

    client = Provider(Profile(name="P", base_url="https://example.test/v1", model="m"),
                      transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(ConsentRequired):
            await client.complete([{"role": "user", "content": "CAD data"}], [])
        assert requests == []
    finally:
        await client.aclose()


async def test_provider_filters_remote_tool_results_and_images():
    from cadnext.privacy import PrivacyContext
    from cadnext.provider import Provider
    from cadnext.settings import Profile

    payloads = []

    def respond(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "Готово"}, "finish_reason": "stop"}]})

    ctx = PrivacyContext(local=False, consent=True, sensitive=["Company"])
    client = Provider(Profile(name="P", base_url="https://example.test/v1", model="m"),
                      privacy=ctx, transport=httpx.MockTransport(respond))
    messages = [{"role": "tool", "tool_call_id": "c1", "content": json.dumps({
        "path": r"C:\Company\part.sldprt", "value": 62})}, {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]
    try:
        await client.complete(messages, [])
        assert "Company" not in str(payloads)
        assert "AAAA" not in str(payloads)
        assert json.loads(payloads[0]["messages"][0]["content"])["value"] == 62
        assert "Company" in str(messages)
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


async def test_models_and_manual_model_request_without_auth():
    from cadnext.provider import Provider
    from cadnext.settings import Profile

    seen = []

    def respond(request):
        seen.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "z"}, {"id": "a"}]})
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "Готово"}, "finish_reason": "stop"}]})

    client = Provider(Profile(name="P", base_url="http://localhost/v1/", model="manual"),
                      transport=httpx.MockTransport(respond))
    try:
        assert await client.list_models() == ["a", "z"]
        assert (await client.complete([{"role": "user", "content": "Привет"}], []))[
            "content"] == "Готово"
        assert json.loads(seen[1].content)["model"] == "manual"
        assert "authorization" not in seen[1].headers
        assert "tools" not in json.loads(seen[1].content)
    finally:
        await client.aclose()


async def test_openrouter_model_catalog_exposes_model_specific_reasoning_levels():
    from cadnext.provider import Provider
    from cadnext.settings import Profile

    def respond(request):
        assert request.url.path == "/api/v1/models"
        return httpx.Response(200, json={"data": [
            {"id": "deepseek/deepseek-v4.1-flash-1017", "reasoning": {
                "mandatory": False, "default_effort": "high",
                "supported_efforts": ["max", "high", "low", "none"]}},
            {"id": "plain/model"},
        ]})

    client = Provider(Profile(name="P", base_url="https://openrouter.ai/api/v1", model="manual"),
                      transport=httpx.MockTransport(respond))
    try:
        assert await client.list_model_catalog() == {"models": [
            {"id": "deepseek/deepseek-v4.1-flash-1017", "reasoning": {
                "mandatory": False, "default_effort": "high",
                "supported_efforts": ["max", "high", "low", "none"]}},
            {"id": "plain/model", "reasoning": None},
        ]}
        assert await client.list_models() == ["deepseek/deepseek-v4.1-flash-1017", "plain/model"]
    finally:
        await client.aclose()


async def test_openrouter_catalog_discards_unknown_reasoning_values():
    from cadnext.provider import Provider
    from cadnext.settings import Profile

    response = httpx.Response(200, json={"data": [{"id": "m", "reasoning": {
        "mandatory": True, "default_effort": "unsupported",
        "supported_efforts": ["high", "high", "budget_tokens", 7, "none"]}}]})
    client = Provider(Profile(name="P", base_url="https://openrouter.ai/api/v1", model="m"),
                      transport=httpx.MockTransport(lambda _: response))
    try:
        assert await client.list_model_catalog() == {"models": [{"id": "m", "reasoning": {
            "mandatory": True, "default_effort": None,
            "supported_efforts": ["high"]}}]}
    finally:
        await client.aclose()


async def test_openrouter_catalog_removes_none_for_mandatory_reasoning():
    from cadnext.provider import Provider
    from cadnext.settings import Profile

    response = httpx.Response(200, json={"data": [{"id": "m", "reasoning": {
        "mandatory": True, "default_effort": "high",
        "supported_efforts": ["high", "none"]}}]})
    client = Provider(Profile(name="P", base_url="https://openrouter.ai/api/v1", model="m"),
                      transport=httpx.MockTransport(lambda _: response))
    try:
        assert await client.list_model_catalog() == {"models": [{"id": "m", "reasoning": {
            "mandatory": True, "default_effort": "high", "supported_efforts": ["high"]}}]}
    finally:
        await client.aclose()


async def test_openrouter_catalog_ignores_malformed_reasoning_list():
    from cadnext.provider import Provider
    from cadnext.settings import Profile

    response = httpx.Response(200, json={"data": [{"id": "m", "reasoning": {
        "mandatory": "yes", "default_effort": "high", "supported_efforts": None}}]})
    client = Provider(Profile(name="P", base_url="https://openrouter.ai/api/v1", model="m"),
                      transport=httpx.MockTransport(lambda _: response))
    try:
        assert await client.list_model_catalog() == {"models": [{"id": "m", "reasoning": {
            "mandatory": False, "default_effort": None, "supported_efforts": None}}]}
    finally:
        await client.aclose()


async def test_non_openrouter_catalog_keeps_reasoning_metadata_out_of_response():
    from cadnext.provider import Provider
    from cadnext.settings import Profile

    response = httpx.Response(200, json={"data": [{"id": "m", "reasoning": {
        "mandatory": True, "supported_efforts": ["max"]}}]})
    client = Provider(Profile(name="P", base_url="https://provider.test/v1", model="m"),
                      transport=httpx.MockTransport(lambda _: response))
    try:
        assert await client.list_model_catalog() == {"models": [{"id": "m", "reasoning": None}]}
    finally:
        await client.aclose()


@pytest.mark.parametrize("response", [
    {"choices": []},
    {"choices": [{"message": {"content": "cut"}, "finish_reason": "length"}]},
    {"choices": [{"message": {"content": None, "tool_calls": [{"id": "a",
        "type": "function", "function": {"name": "read", "arguments": "bad"}}]},
        "finish_reason": "tool_calls"}]},
    {"choices": [{"message": {"content": None, "tool_calls": [{"id": "a",
        "type": "function", "function": {"name": "read", "arguments": '{"value": NaN}'}}]},
        "finish_reason": "tool_calls"}]},
])
async def test_invalid_or_truncated_reply_stops(response):
    from cadnext.provider import Provider, ProviderError
    from cadnext.settings import Profile

    client = Provider(Profile(name="P", base_url="http://localhost/v1", model="m"),
                      transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)))
    try:
        with pytest.raises(ProviderError):
            await client.complete([], [])
    finally:
        await client.aclose()


async def test_transport_error_is_safe_and_not_retried():
    from cadnext.provider import Provider, ProviderError
    from cadnext.settings import Profile

    requests = []

    def fail(request):
        requests.append(request)
        raise httpx.ConnectError("private-test-value", request=request)

    client = Provider(Profile(name="P", base_url="http://localhost/v1", model="m"),
                      secret="private-test-value", transport=httpx.MockTransport(fail))
    try:
        with pytest.raises(ProviderError) as caught:
            await client.complete([], [])
        assert "private-test-value" not in str(caught.value)
        assert len(requests) == 1
        assert requests[0].headers["authorization"] == "Bearer private-test-value"
    finally:
        await client.aclose()


async def test_native_tools_rejection_is_diagnosed_without_body_leak():
    from cadnext.provider import Provider, ProviderError
    from cadnext.settings import Profile

    client = Provider(Profile(name="P", base_url="http://localhost/v1", model="m"),
                      transport=httpx.MockTransport(lambda _: httpx.Response(400, json={
                          "error": {"message": "tools not supported private-test-value"}})))
    try:
        with pytest.raises(ProviderError) as caught:
            await client.complete([], [{"type": "function", "function": {"name": "read"}}])
        assert "инструмент" in str(caught.value).lower()
        assert "private-test-value" not in str(caught.value)
    finally:
        await client.aclose()


async def test_unbound_consent_cannot_be_reused_for_another_profile():
    from cadnext.privacy import PrivacyContext
    from cadnext.provider import Provider
    from cadnext.settings import Profile

    ctx = PrivacyContext(local=False, consent=True)
    first = Provider(Profile(name="A", base_url="https://example.test/v1", model="m"),
                     privacy=ctx)
    try:
        with pytest.raises(ValueError):
            second = Provider(Profile(name="B", base_url="https://other.test/v1", model="m"),
                              privacy=ctx)
            await second.aclose()
    finally:
        await first.aclose()


async def test_consent_cannot_be_reused_after_profile_address_change():
    from dataclasses import replace

    from cadnext.privacy import PrivacyContext
    from cadnext.provider import Provider
    from cadnext.settings import Profile

    profile = Profile(name="A", base_url="https://first.test/v1", model="m")
    ctx = PrivacyContext(local=False, session_id="s1", profile_id=profile.id)
    first = Provider(profile, privacy=ctx)
    ctx.grant(session_id="s1", profile_id=profile.id)
    try:
        with pytest.raises(ValueError):
            second = Provider(replace(profile, base_url="https://second.test/v1"), privacy=ctx)
            await second.aclose()
    finally:
        await first.aclose()


def test_privacy_cannot_enable_vision_for_a_text_profile():
    from cadnext.privacy import PrivacyContext
    from cadnext.provider import Provider
    from cadnext.settings import Profile

    with pytest.raises(ValueError):
        Provider(Profile(name="A", base_url="http://localhost/v1", model="m", vision=False),
                 privacy=PrivacyContext(local=True, vision=True))


async def test_confidential_strings_do_not_change_tool_protocol():
    from cadnext.privacy import PrivacyContext
    from cadnext.provider import Provider
    from cadnext.settings import Profile

    payloads = []

    def respond(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "Готово"}, "finish_reason": "stop"}]})

    client = Provider(Profile(name="P", base_url="https://example.test/v1", model="m"),
                      privacy=PrivacyContext(local=False, consent=True,
                                             sensitive=["tool", "read", "path"]),
                      transport=httpx.MockTransport(respond))
    messages = [{"role": "assistant", "content": None, "tool_calls": [{"id": "tool_1",
        "type": "function", "function": {"name": "read", "arguments": '{"path":"tool"}'}}]},
        {"role": "tool", "tool_call_id": "tool_1", "content": "tool read path"}]
    tools = [{"type": "function", "function": {"name": "read", "description": "tool read",
        "parameters": {"type": "object", "properties": {"path": {
            "type": "string", "description": "path"}}, "required": ["path"]}}}]
    try:
        await client.complete(messages, tools)
        sent = payloads[0]
        assert sent["messages"][0]["tool_calls"][0]["id"] == "tool_1"
        assert sent["messages"][0]["tool_calls"][0]["function"]["name"] == "read"
        arguments = json.loads(sent["messages"][0]["tool_calls"][0]["function"]["arguments"])
        assert "path" in arguments
        assert arguments["path"] != "tool"
        assert sent["messages"][1]["role"] == "tool"
        assert sent["messages"][1]["tool_call_id"] == "tool_1"
        assert "tool read path" not in sent["messages"][1]["content"]
        function = sent["tools"][0]["function"]
        assert function["name"] == "read"
        assert function["parameters"]["required"] == ["path"]
        assert "path" in function["parameters"]["properties"]
        assert function["parameters"]["properties"]["path"]["type"] == "string"
        assert function["description"] != "tool read"
    finally:
        await client.aclose()
