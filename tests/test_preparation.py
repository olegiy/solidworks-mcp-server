import asyncio
import base64
import io
import json

import httpx
import pytest
from PIL import Image

from cadnext.provider import Provider, ProviderError
from cadnext.settings import Profile


def drawings():
    blocks = []
    for color in ("red", "blue", "green"):
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), color).save(buf, "PNG")
        blocks.append({"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}})
    return [{"role": "user", "content": blocks}]


async def test_independent_preparation_is_tool_free_bounded_and_input_bound():
    from cadnext.preparation import prepare_images

    active = maximum = 0
    payloads = []

    async def respond(request):
        nonlocal active, maximum
        payloads.append(json.loads(request.content))
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(.01)
        active -= 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "Thickness: 1 mm; uncertain"},
                                                     "finish_reason": "stop"}]})

    provider = Provider(Profile(name="P", base_url="http://localhost/v1", model="m", vision=True),
                        transport=httpx.MockTransport(respond))
    try:
        result = await prepare_images(provider, drawings(), is_current=lambda fingerprint: True)
        assert len(result["notes"]) == 3
        assert len(result["fingerprint"]) == 64
        assert maximum == 2
        assert all("tools" not in payload for payload in payloads)
        assert all(metric["image_count"] == 1 for metric in result["metrics"])
    finally:
        await provider.aclose()


async def test_stale_input_discards_all_prepared_notes():
    from cadnext.preparation import prepare_images

    current = True

    def respond(request):
        nonlocal current
        current = False
        return httpx.Response(200, json={"choices": [{"message": {"content": "stale-facts"},
                                                     "finish_reason": "stop"}]})

    provider = Provider(Profile(name="P", base_url="http://localhost/v1", model="m", vision=True),
                        transport=httpx.MockTransport(respond))
    try:
        result = await prepare_images(provider, drawings(), is_current=lambda fingerprint: current)
        assert result["stale"] is True
        assert result["notes"] == []
        assert len(result["metrics"]) == 3
    finally:
        await provider.aclose()


async def test_stop_cancels_only_preparation_and_releases_http_slots():
    from cadnext.preparation import prepare_images

    entered, stop, hold = asyncio.Event(), asyncio.Event(), asyncio.Event()
    active = 0

    async def respond(request):
        nonlocal active
        active += 1
        if active == 2:
            entered.set()
        try:
            await hold.wait()
        finally:
            active -= 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"},
                                                     "finish_reason": "stop"}]})

    provider = Provider(Profile(name="P", base_url="http://localhost/v1", model="m", vision=True),
                        transport=httpx.MockTransport(respond))
    task = asyncio.create_task(prepare_images(provider, drawings(),
                              is_current=lambda fingerprint: True, stop=stop))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        stop.set()
        result = await asyncio.wait_for(task, 2)
        assert result["cancelled"] is True and result["notes"] == []
        assert active == 0
        hold.set()
        assert (await provider.complete([], []))["content"] == "ok"
    finally:
        hold.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await provider.aclose()


async def test_preparation_rejects_unexpected_executable_tool_calls():
    from cadnext.preparation import prepare_images

    provider = Provider(Profile(name="P", base_url="http://localhost/v1", model="m", vision=True),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{
            "message": {"content": None, "tool_calls": [{"id": "bad", "type": "function",
                "function": {"name": "execute_python", "arguments": '{"code":"raise SystemExit"}'}}]},
            "finish_reason": "tool_calls"}]})))
    try:
        with pytest.raises(ProviderError):
            await prepare_images(provider, drawings(), is_current=lambda fingerprint: True)
    finally:
        await provider.aclose()
