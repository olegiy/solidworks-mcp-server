import json

import httpx

from cadnext.agent import Agent
from cadnext.provider import Provider, ProviderError
from cadnext.settings import Profile
from tests.fake_mcp import FakeMcp, ok
from tests.test_agent import ScriptedProvider, reply, settle


async def test_provider_measures_actual_request_and_reported_usage():
    captured = []

    def respond(request):
        captured.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Готово"},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 120, "completion_tokens": 7,
            "prompt_tokens_details": {"cached_tokens": 100}, "cost": 0.0012}})

    provider = Provider(Profile(name="P", base_url="http://localhost/v1", model="m", vision=True),
                        transport=httpx.MockTransport(respond))
    try:
        await provider.complete([{"role": "user", "content": [
            {"type": "text", "text": "private-input"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}], [])
        metrics = provider.last_metrics
        assert metrics["request_bytes"] == len(captured[0].content)
        assert metrics["image_count"] == 1
        assert metrics["input_tokens"] == 120
        assert metrics["output_tokens"] == 7
        assert metrics["cached_tokens"] == 100
        assert metrics["cost"] == 0.0012
        assert metrics["duration_seconds"] >= 0
        assert metrics["first_token_seconds"] is None
        assert "private-input" not in json.dumps(metrics)
    finally:
        await provider.aclose()


async def test_provider_metrics_distinguish_missing_or_invalid_usage_from_zero():
    replies = [{}, {"prompt_tokens": True, "completion_tokens": -1,
                    "prompt_tokens_details": {"cached_tokens": "secret"}},
               {"prompt_tokens": 0, "completion_tokens": 0}]

    def respond(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"},
            "finish_reason": "stop"}], "usage": replies.pop(0)})

    provider = Provider(Profile(name="P", base_url="http://localhost/v1", model="m"),
                        transport=httpx.MockTransport(respond))
    try:
        for _ in range(2):
            await provider.complete([], [])
            assert provider.last_metrics["input_tokens"] is None
            assert provider.last_metrics["output_tokens"] is None
            assert provider.last_metrics["cached_tokens"] is None
        await provider.complete([], [])
        assert provider.last_metrics["input_tokens"] == 0
        assert provider.last_metrics["output_tokens"] == 0
    finally:
        await provider.aclose()


async def test_failed_request_resets_usage_and_keeps_only_safe_metrics():
    provider = Provider(Profile(name="P", base_url="http://localhost/v1", model="m"),
                        transport=httpx.MockTransport(lambda request:
                            httpx.Response(401, text="private-secret")))
    try:
        try:
            await provider.complete([], [])
        except ProviderError:
            pass
        assert provider.last_metrics["input_tokens"] is None
        assert provider.last_metrics["duration_seconds"] >= 0
        assert "private-secret" not in json.dumps(provider.last_metrics)
    finally:
        await provider.aclose()


async def test_run_separates_mutation_and_independent_verification_metrics():
    model = ScriptedProvider([reply(("set", "set_dimension", {"name": "D1", "value_mm": 62})),
                              reply(content="Готово")])
    mcp = FakeMcp([("set_dimension", ok({"unit": "mm"})),
                   ("get_dimension", ok({"name": "D1", "value": 62, "unit": "mm"}))])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Измени")
    await settle(agent, run)
    assert run.status == "completed"
    metrics = run.snapshot()["metrics"]
    assert metrics["model_calls"] == 2
    assert metrics["cad_calls"] == 1
    assert metrics["verification_calls"] == 1
    assert metrics["model_seconds"] >= 0
    assert metrics["cad_seconds"] >= 0
    assert metrics["verification_seconds"] >= 0
    assert metrics["input_tokens"] is None


def test_events_capture_small_state_at_emission_without_copying_history():
    from cadnext.agent import Run

    run = Run(session_id="s", profile_id="p", messages=[{"role": "user", "content": "private" * 1000}])
    run._status("running", "Starting")
    run.call_count = 3
    run._status("completed", "Done")
    assert run.events[0]["state"]["status"] == "running"
    assert run.events[0]["state"]["call_count"] == 0
    assert run.events[1]["state"]["call_count"] == 3
    assert "messages" not in run.events[0]["state"]
    assert "events" not in run.events[0]["state"]
    assert "private" not in json.dumps(run.events)


async def test_aggregate_usage_is_unknown_if_any_model_request_omits_it():
    model = ScriptedProvider([reply(("read", "get_dimension", {"name": "D1"})), reply(content="Done")])
    model.last_metrics = {"input_tokens": 10, "request_bytes": 200, "image_count": 0}
    original_complete = model.complete

    async def complete(messages, tools):
        response = await original_complete(messages, tools)
        if len(model.requests) == 2:
            model.last_metrics = {"input_tokens": None, "request_bytes": 200, "image_count": 0}
        return response

    model.complete = complete
    agent = Agent(FakeMcp([("get_dimension", ok({"value": 62}))]))
    run = agent.start(session_id="s", provider=model, message="Read")
    await settle(agent, run)
    assert run.metrics["input_tokens"] is None
    assert run.metrics["request_bytes"] == 400


async def test_mcp_catalog_is_cached_per_session_with_detached_results():
    from types import SimpleNamespace

    from cadnext.mcp import SolidWorksMcp

    calls = []

    class Session:
        async def list_tools(self, cursor):
            calls.append(cursor)
            return SimpleNamespace(tools=[SimpleNamespace(name="read", description="Read",
                inputSchema={"type": "object", "properties": {}})], nextCursor=None)

    mcp = SolidWorksMcp()
    mcp.session = Session()
    first = await mcp.tools()
    first[0]["function"]["name"] = "overwritten"
    second = await mcp.tools()
    assert second[0]["function"]["name"] == "read"
    assert calls == [None]
