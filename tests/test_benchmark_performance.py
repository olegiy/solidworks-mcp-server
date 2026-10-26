import json
from copy import deepcopy

import httpx

from cadnext.provider import Provider
from cadnext.settings import Profile


async def test_benchmark_pairs_use_identical_inputs_and_never_execute_tools():
    from scripts.benchmark_performance import benchmark, synthetic_scenarios

    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        message = {"content": "ok"}
        if captured[-1]["stream"]:
            text = 'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
            return httpx.Response(200, text=text + 'data: [DONE]\n\n')
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "stop"}]})

    provider = Provider(Profile(name="P", base_url="http://localhost/v1", model="m", vision=True),
                        transport=httpx.MockTransport(respond))
    scenarios = synthetic_scenarios()
    original = deepcopy(scenarios)
    try:
        report = await benchmark(provider, scenarios[:1], repeats=2)
        assert report["cad_executed"] is False
        assert report["geometry_verified"] is None
        assert len(report["samples"]) == 8
        for compact in (False, True):
            rows = [row for row in report["samples"] if row["compact"] == compact]
            assert len({row["input_fingerprint"] for row in rows}) == 1
        for a, b in [(captured[0], captured[1]), (captured[2], captured[3])]:
            assert {k: v for k, v in a.items() if k not in {"stream", "stream_options"}} == {
                k: v for k, v in b.items() if k not in {"stream", "stream_options"}}
        assert scenarios == original
        assert "messages" not in report and "tools" not in report
        assert all(row["input_tokens"] is None for row in report["samples"])
    finally:
        await provider.aclose()


def test_summary_uses_nearest_rank_p90_and_preserves_missing_usage():
    from scripts.benchmark_performance import summarize

    rows = [{"scenario": "dimension", "stream": False, "compact": True,
             "duration_seconds": value, "request_bytes": 100, "input_tokens": None}
            for value in range(1, 11)]
    summary, = summarize(rows)
    assert summary["duration_median_seconds"] == 5.5
    assert summary["duration_p90_seconds"] == 9
    assert summary["input_tokens_total"] is None


def test_benchmark_totals_include_billed_errors_and_do_not_hide_missing_error_usage():
    from scripts.benchmark_performance import summarize

    rows = [{"scenario": "s", "stream": True, "compact": True,
             "duration_seconds": 1, "request_bytes": 100, "cost": .2, "input_tokens": 10},
            {"scenario": "s", "stream": True, "compact": True, "error": "provider_error",
             "duration_seconds": 2, "request_bytes": 100, "cost": .3, "input_tokens": 20}]
    summary, = summarize(rows)
    assert summary["cost_total"] == .5
    assert summary["input_tokens_total"] == 30
    assert summary["errors"] == 1
    rows[1]["input_tokens"] = None
    assert summarize(rows)[0]["input_tokens_total"] is None
