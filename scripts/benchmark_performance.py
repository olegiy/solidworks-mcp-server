"""Compare identical completion payloads; never connect to or mutate SolidWorks."""

import argparse
import asyncio
import base64
import io
import json
import math
import statistics
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

from cadnext.context import model_context
from cadnext.preparation import input_fingerprint, prepare_images
from cadnext.privacy import PrivacyContext
from cadnext.provider import Provider, ProviderError
from cadnext.settings import Profile, SettingsStore


def synthetic_scenarios():
    picture = Image.new("RGB", (800, 600), "white")
    ImageDraw.Draw(picture).rectangle((250, 220, 550, 380), outline="black", width=2)
    output = io.BytesIO()
    picture.save(output, "PNG")
    image = {"type": "image_url", "image_url": {
        "url": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()}}
    scenarios = []
    descriptions = {
        "sheet_metal": "Создай листовую деталь 60×40 мм, толщина 1 мм, радиус 1 мм.",
        "dimension": "Измени известный размер D1@Эскиз1 с 50 на 62 мм и проверь.",
        "assembly": "Проверь состав сборки и требуемые сопряжения без догадок.",
        "python": "Подготовь минимальное продолжение после известной ошибки Python.",
        "long_dialogue": "Сохрани требования к толщине 1 мм и проверь последнюю деталь.",
    }
    tools = [{"type": "function", "function": {"name": "get_feature_tree",
        "description": "Read the active document feature tree.", "parameters": {
            "type": "object", "properties": {}, "additionalProperties": False}}}]
    for name, description in descriptions.items():
        messages = [{"role": "system", "content":
            "This is a recorded CAD scenario. Return a brief next-step assessment. "
            "Do not claim the geometry was verified or that any action executed. "
            "features_delta updates the previous numbered observation; apply edits "
            "in descending start order: delete elements, then insert."},
            {"role": "user", "content": description}]
        if name in {"sheet_metal", "long_dialogue"}:
            messages.append({"role": "user", "content": [deepcopy(image)]})
        tree = [{"name": f"Sketch{i}", "type": "ProfileFeature"} for i in range(80)]
        for index in range(10 if name == "long_dialogue" else 2):
            call_id = f"recorded_{index}"
            messages.extend([
                {"role": "assistant", "content": None, "tool_calls": [{"id": call_id,
                    "type": "function", "function": {"name": "get_feature_tree", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": call_id, "content": json.dumps({
                    "ok": True, "value": {"features": tree}, "uncertain": False})},
                {"role": "user", "content": [deepcopy(image)], "_cadnext": {"source": "screenshot"}},
            ])
        messages.append({"role": "user", "content": "Кратко оцени следующий необходимый шаг."})
        scenarios.append({"name": name, "messages": messages, "tools": tools})
    return scenarios


def summarize(samples):
    summaries = []
    keys = sorted({(row["scenario"], row["compact"], row["stream"]) for row in samples})
    for scenario, compact, stream in keys:
        rows = [row for row in samples if (row["scenario"], row["compact"], row["stream"])
                == (scenario, compact, stream)]
        good = [row for row in rows if not row.get("error")]
        durations = sorted(row["duration_seconds"] for row in good)
        first = [row["first_token_seconds"] for row in good
                 if row.get("first_token_seconds") is not None]
        result = {"scenario": scenario, "compact": compact, "stream": stream,
                  "samples": len(rows), "errors": len(rows) - len(good),
                  "duration_median_seconds": statistics.median(durations) if durations else None,
                  "duration_p90_seconds": durations[math.ceil(len(durations) * .9) - 1] if durations else None,
                  "first_token_median_seconds": statistics.median(first) if first else None,
                  "request_bytes_median": statistics.median(row["request_bytes"] for row in good) if good else None}
        for key in ("input_tokens", "output_tokens", "cached_tokens", "cost"):
            values = [row.get(key) for row in rows]
            result[key + "_total"] = sum(values) if values and all(value is not None for value in values) else None
        summaries.append(result)
    return summaries


async def benchmark(provider, scenarios, *, repeats=3):
    samples = []
    for scenario in scenarios:
        messages, tools = scenario["messages"], scenario["tools"]
        for repeat in range(repeats):
            for compact in ((False, True) if repeat % 2 == 0 else (True, False)):
                # Alternate pair ordering to reduce warm-prefix and provider-order bias.
                for stream in ((False, True) if repeat % 2 == 0 else (True, False)):
                    projection = model_context(messages) if compact else [
                        {key: value for key, value in message.items() if key != "_cadnext"}
                        for message in messages]
                    row = {"scenario": scenario["name"], "repeat": repeat,
                        "stream": stream, "compact": compact,
                        "input_fingerprint": input_fingerprint({"messages": projection, "tools": tools})}
                    try:
                        response = await provider.complete(deepcopy(messages), deepcopy(tools),
                                                           stream=stream, compact=compact)
                        row["tool_calls_proposed"] = len(response.get("tool_calls", []))
                    except ProviderError:
                        row["error"] = "provider_error"
                    row.update(deepcopy(provider.last_metrics))
                    samples.append(row)
    return {"cad_executed": False, "geometry_verified": None, "samples": samples,
            "summary": summarize(samples)}


class _OfflineStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"Recorded scenario."},"finish_reason":"stop"}]}\n\n'
        yield b'data: [DONE]\n\n'


def _offline_response(request):
    if json.loads(request.content).get("stream"):
        return httpx.Response(200, stream=_OfflineStream())
    return httpx.Response(200, json={"choices": [{"message": {"content": "Recorded scenario."},
                                                "finish_reason": "stop"}]})


async def run(args):
    if args.live:
        settings = SettingsStore()
        profiles = settings.list()
        if args.profile_id:
            profile = settings.get(args.profile_id)
        elif len(profiles) == 1:
            profile = profiles[0]
        else:
            raise ValueError("Укажите --profile-id: требуется однозначный выбор подключения.")
        secret = settings.secret(profile.id)
        transport = None
    else:
        profile = Profile(name="Offline", base_url="http://localhost/v1", model="offline", vision=True)
        secret, transport = None, httpx.MockTransport(_offline_response)
    if args.effort:
        profile = replace(profile, reasoning_effort=args.effort)
    privacy = PrivacyContext(local=profile.local, vision=profile.vision,
                             session_id="performance-benchmark", profile_id=profile.id)
    privacy.grant(session_id="performance-benchmark", profile_id=profile.id, images=profile.vision)
    provider = Provider(profile, secret, privacy=privacy, transport=transport)
    try:
        scenarios = synthetic_scenarios() if not args.input else json.loads(
            Path(args.input).read_text(encoding="utf-8-sig"))
        if args.scenario:
            scenarios = [scenario for scenario in scenarios if scenario["name"] == args.scenario]
        if not isinstance(scenarios, list) or not scenarios:
            raise ValueError("Нет сценариев для сравнения.")
        report = await benchmark(provider, scenarios, repeats=args.repeats)
        report["mode"] = "live_api_no_cad" if args.live else "offline_transport_only"
        report["reasoning_effort"] = profile.reasoning_effort
        if args.parallel_preparation:
            messages = [{"role": "user", "content": [
                deepcopy(synthetic_scenarios()[0]["messages"][2]["content"][0]) for _ in range(3)]}]
            fingerprint = input_fingerprint(messages)
            preparation = []
            for parallel in (False, True):
                started = time.perf_counter()
                result = await prepare_images(provider, messages,
                    is_current=lambda value: value == fingerprint, parallel=parallel)
                preparation.append({"parallel": parallel, "duration_seconds": time.perf_counter() - started,
                    "requests": len(result["metrics"]), "metrics": result["metrics"],
                    "stale": result["stale"], "cancelled": result["cancelled"]})
            report["preparation"] = preparation
        return report
    finally:
        await provider.aclose()


def main():
    parser = argparse.ArgumentParser(description="Сравнение LLM без CAD-вызовов; по умолчанию offline.")
    parser.add_argument("--live", action="store_true", help="Отправить сценарии выбранному API.")
    parser.add_argument("--profile-id")
    parser.add_argument("--scenario")
    parser.add_argument("--input", help="Локальный JSON-массив name/messages/tools; в отчёт история не попадает.")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--effort", choices=("none", "minimal", "low", "medium", "high", "xhigh", "max"))
    parser.add_argument("--parallel-preparation", action="store_true")
    parser.add_argument("--output", help="Путь для числового JSON-отчёта.")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 20:
        parser.error("--repeats должен быть от 1 до 20.")
    try:
        report = asyncio.run(run(args))
    except (ValueError, KeyError, OSError, RuntimeError):
        parser.exit(1, "Не удалось выполнить сравнение. Проверьте профиль и формат сценариев.\n")
    encoded = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2)
    if args.output:
        Path(args.output).write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
