"""Real API/agent/provider with deterministic HTTP and CAD boundaries for browser tests."""

import asyncio
import base64
import json
import sys
import tempfile
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image

from cadnext.provider import Provider
from cadnext.settings import SettingsStore
from tests.fake_mcp import CATALOG, ok
from tests.test_settings import MemorySecrets

PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a"
       "d1sAAAAASUVORK5CYII=")


class Backend:
    def __init__(self, directory, *, delay=None):
        self.settings = SettingsStore(directory, MemorySecrets())
        self.calls = []
        self.requests = []
        self.model_requests = []
        self.profiles = []
        self.closed = 0
        self.mcp_entered = False
        self.mcp_closed = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.delay = delay
        self.uncertain = False

    @asynccontextmanager
    async def mcp(self):
        self.mcp_entered = True
        try:
            yield self
        finally:
            self.mcp_closed = True

    async def tools(self):
        return CATALOG

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if arguments.get("name") == "wait":
            self.entered.set()
            if self.delay is not None:
                await asyncio.sleep(self.delay)
            else:
                await self.release.wait()
        if name == "screenshot":
            return ok(None, [{"data": PNG, "mimeType": "image/png"}])
        if name == "execute_python":
            return ok({"stdout": "Код выполнен", "result": "62"})
        return ok({"name": arguments.get("name"), "value": 62, "value_mm": 62, "unit": "mm"})

    def provider(self, profile, secret=None, *, privacy):
        self.profiles.append(profile)

        def respond(request):
            if request.url.path.endswith("/models"):
                self.model_requests.append(request)
                item = {"id": "fake-model"}
                if profile.base_url.startswith("https://openrouter.ai"):
                    item["reasoning"] = {"mandatory": False, "default_effort": "high",
                                          "supported_efforts": ["max", "high", "low", "none"]}
                return httpx.Response(200, json={"data": [item]})
            payload = json.loads(request.content)
            self.requests.append(payload)
            messages = payload["messages"]
            latest = messages[-1]
            content = latest.get("content", "")
            if payload.get("tools", [{}])[0].get("function", {}).get("name") == "cadnext_connection_test":
                name, arguments = "cadnext_connection_test", {}
            elif isinstance(content, list) and not any(message["role"] == "tool" for message in messages):
                images = [part for part in content if part.get("type") == "image_url"]
                with Image.open(BytesIO(base64.b64decode(images[0]["image_url"]["url"].split(",", 1)[1]))) as image:
                    image.load()
                    width, height = image.size
                answer = f"Получено изображений: {len(images)}. Размер: {width} × {height}."
                return httpx.Response(200, json={"choices": [{"message": {
                    "role": "assistant", "content": answer}, "finish_reason": "stop"}]})
            elif latest["role"] == "tool" or isinstance(content, list):
                return httpx.Response(200, json={"choices": [{"message": {
                    "role": "assistant", "content": "Размер 62 мм. Готово."}, "finish_reason": "stop"}]})
            elif "код" in content.lower():
                name, arguments = "execute_python", {"code": "result = 62\nprint(result)"}
            elif "снимок" in content.lower():
                name, arguments = "screenshot", {}
            else:
                name = "get_dimension"
                arguments = {"name": "wait" if "долго" in content.lower() else "D1"}
            calls = [{"id": f"call_{len(self.requests)}", "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments)}}]
            if arguments.get("name") == "wait":
                calls.append({"id": f"next_{len(self.requests)}", "type": "function", "function": {
                    "name": "get_dimension", "arguments": '{"name":"D2"}'}})
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": None, "tool_calls": calls}, "finish_reason": "tool_calls"}]})

        backend = self

        class RecordingProvider(Provider):
            async def aclose(self):
                await super().aclose()
                backend.closed += 1

        return RecordingProvider(profile, secret, privacy=privacy, transport=httpx.MockTransport(respond))


if __name__ == "__main__":
    import uvicorn

    from cadnext.app import create_app

    with tempfile.TemporaryDirectory(prefix="cadnext-browser-test-") as directory:
        backend = Backend(Path(directory), delay=2)
        app = create_app(settings=backend.settings, mcp_factory=backend.mcp,
                         provider_factory=backend.provider)
        uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), access_log=False)
