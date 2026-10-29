"""Real API/agent/provider with deterministic HTTP and CAD boundaries for browser tests."""

import asyncio
import base64
import io
import json
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from PIL import Image

from cadnext.provider import Provider
from cadnext.settings import SettingsStore
from tests.fake_mcp import CATALOG, ok, tool
from tests.test_settings import MemorySecrets

_PNG_BUFFER = io.BytesIO()
Image.new("RGB", (1000, 750), "white").save(_PNG_BUFFER, "PNG")
PNG = base64.b64encode(_PNG_BUFFER.getvalue()).decode("ascii")


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
        self.active_document = 'Деталь1'
        self.document_path = str(Path(directory) / 'Деталь1.sldprt')
        self.dimensions = {'D1': 62.0, 'D2': 30.0}

    @asynccontextmanager
    async def mcp(self):
        self.mcp_entered = True
        try:
            yield self
        finally:
            self.mcp_closed = True

    async def tools(self):
        return [*CATALOG, tool("inspect_cad", {
            "sections": {"type": "array", "items": {"type": "string"}},
            "dimension_names": {"type": "array", "items": {"type": "string"}}}, ["sections"])]

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if arguments.get("name") == "wait":
            self.release.clear()
            self.entered.set()
            if self.delay is not None:
                await asyncio.sleep(self.delay)
            else:
                await self.release.wait()
        if name == "connect_solidworks":
            return ok({"revision": "33", "active_document": self.active_document})
        if name == "list_open_documents":
            documents = [] if self.active_document is None else [{
                "title": self.active_document, "path": self.document_path, "type": "part"}]
            return ok({"documents": documents})
        if name == "save_document":
            return ok({"saved": arguments.get("save_as_path") or self.document_path})
        if name == "screenshot":
            return ok(None, [{"data": PNG, "mimeType": "image/png"}])
        if name == "execute_python":
            return ok({"stdout": "Код выполнен", "result": "62"})
        if name == "get_dimension":
            value = self.dimensions.get(arguments.get("name"), 62.0)
            return ok({"name": arguments.get("name"), "value": value,
                       "value_mm": value, "unit": "mm"})
        if name == "set_dimension":
            self.dimensions[arguments["name"]] = arguments["value_mm"]
            return ok({"name": arguments["name"], "unit": "mm"})
        if name == "inspect_cad":
            return ok({"dimensions": [{"name": "Length@Profile", "value": 80,
                                       "unit": "mm", "driving": True}]})
        return ok({"name": arguments.get("name"), "value": 62, "value_mm": 62, "unit": "mm"})

    def tool_response(self, name, arguments, *, extra=None):
        calls = [{"id": f"call_{len(self.requests)}_{len(self.calls)}", "type": "function",
                  "function": {"name": name, "arguments": json.dumps(arguments)}}]
        for index, (extra_name, extra_arguments) in enumerate(extra or [], 1):
            calls.append({"id": f"call_{len(self.requests)}_{len(self.calls)}_{index}",
                          "type": "function", "function": {
                              "name": extra_name, "arguments": json.dumps(extra_arguments)}})
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": None, "tool_calls": calls},
            "finish_reason": "tool_calls"}]})

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
            tool_names = {item.get("function", {}).get("name")
                          for item in payload.get("tools", [])}
            latest = messages[-1]
            if any(message.get("content") == "stage-demo" for message in messages):
                completed = [message for message in messages if message["role"] == "tool"]
                sequence = [
                    ("set_construction_plan", {"stages": [
                        {"id": "profile", "title": "Профиль втулки", "expected_result": "Длина 80 мм"},
                        {"id": "body", "title": "Вращение", "expected_result": "Одно тело"}]}),
                    ("execute_python", {"code": "result = 80", "stage_id": "profile", "checks": [
                        {"kind": "dimension", "name": "Length@Profile", "value": 80, "unit": "mm"}]}),
                    ("execute_python", {"code": "result = 'body'", "stage_id": "body"}),
                ]
                if len(completed) < len(sequence):
                    name, arguments = sequence[len(completed)]
                    response = {"role": "assistant", "content": None, "tool_calls": [{
                        "id": f"stage_{len(completed)}", "type": "function", "function": {
                            "name": name, "arguments": json.dumps(arguments)}}]}
                else:
                    response = {"role": "assistant", "content": "Заданные свойства проверены."}
                return httpx.Response(200, json={"choices": [{"message": response,
                    "finish_reason": "tool_calls" if response.get("tool_calls") else "stop"}]})
            calls_by_id = {}
            for message in messages:
                for item in message.get("tool_calls", []):
                    calls_by_id[item.get("id")] = item.get("function", {}).get("name")
            request_index = max((index for index, message in enumerate(messages)
                                 if message.get("role") == "user"
                                 and isinstance(message.get("content"), str)
                                 and not str(message.get("content")).startswith("Серверная оценка")),
                                default=0)
            define = next((item for message in messages[request_index + 1:]
                           for item in message.get("tool_calls", [])
                           if item.get("function", {}).get("name") == "cadnext_define_goal"), None)
            define_id = define.get("id") if define else None
            define_result = next((message for message in messages
                                  if message.get("role") == "tool"
                                  and message.get("tool_call_id") == define_id), None)
            last_tool_name = calls_by_id.get(latest.get("tool_call_id")) if latest.get("role") == "tool" else None
            def message_text(content):
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    return " ".join(str(part.get("text") or "") for part in content
                                    if isinstance(part, dict) and part.get("type") == "text")
                return ""
            user_texts = [message_text(message.get("content")) for message in messages
                          if message.get("role") == "user"]
            request_texts = [text for text in user_texts
                             if text and not text.startswith("Серверная оценка")]
            original = request_texts[-1] if request_texts else ""

            scenario = original.lower()

            def image_echo():
                for message in reversed(messages):
                    if message.get("role") != "user" or not isinstance(message.get("content"), list):
                        continue
                    urls = [part.get("image_url", {}).get("url") for part in message["content"]
                            if isinstance(part, dict) and part.get("type") == "image_url"]
                    if not urls:
                        continue
                    try:
                        raw = base64.b64decode(str(urls[0]).split(",", 1)[1])
                        with Image.open(io.BytesIO(raw)) as image:
                            width, height = image.size
                    except Exception:
                        width, height = 0, 0
                    return (f"Получено изображений: {len(urls)}. "
                            f"Размер: {width} × {height}.")
                return None

            if "cadnext_connection_test" in tool_names:
                return self.tool_response("cadnext_connection_test", {})
            if tool_names == {"cadnext_report_visual_evaluation"}:
                return httpx.Response(200, json={"choices": [{"message": {
                    "role": "assistant", "content": None, "tool_calls": [{
                        "id": f"vision_{len(self.requests)}", "type": "function",
                        "function": {"name": "cadnext_report_visual_evaluation", "arguments": json.dumps({
                            "evaluations": [{"requirement_id": "v1", "status": "pass",
                                             "explanation": "Силуэт и скругление совпадают.",
                                             "image_ids": ["current-0"]}]})}}]},
                    "finish_reason": "tool_calls"}]})
            if "cadnext_define_goal" in tool_names and define is None and latest.get("role") == "user":
                if "уточн" in scenario:
                    kind, requirements, ambiguities = "cad", [
                        {"id": "d1", "description": "Длина", "source": "request",
                         "required": True, "weight": 1, "method": "cad", "expected": 50,
                         "tolerance": 0.001, "unit": "mm"}], ["Не указан радиус скругления"]
                elif "информ" in scenario or "справ" in scenario or "покажи снимок" in scenario:
                    kind, requirements, ambiguities = "informational", [], []
                elif "код" in scenario:
                    kind, requirements, ambiguities = "cad", [
                        {"id": "v1", "description": "Визуальная форма", "source": "request",
                         "required": True, "weight": 1, "method": "vision"}], []
                elif "оценка-70" in scenario or "70" in scenario:
                    self.dimensions.update({"D1": 62.0, "D2": 30.0})
                    kind, requirements, ambiguities = "cad", [
                        {"id": "d1", "description": "Длина 62 мм", "source": "request",
                         "required": True, "weight": 7, "method": "cad", "expected": 62,
                         "tolerance": 0.001, "unit": "mm"},
                        {"id": "d2", "description": "Высота 20 мм", "source": "request",
                         "required": True, "weight": 3, "method": "cad", "expected": 20,
                         "tolerance": 0.001, "unit": "mm"}], []
                elif "оценка-100" in scenario:
                    self.dimensions.update({"D1": 62.0, "D2": 30.0})
                    kind, requirements, ambiguities = "cad", [
                        {"id": "d1", "description": "Длина 62 мм", "source": "request",
                         "required": True, "weight": 1, "method": "cad", "expected": 62,
                         "tolerance": 0.001, "unit": "mm"}], []
                elif "визуал" in scenario:
                    self.dimensions["D1"] = 40.0
                    kind, requirements, ambiguities = "cad", [
                        {"id": "d1", "description": "Длина 62 мм", "source": "request",
                         "required": True, "weight": 1, "method": "cad", "expected": 62,
                         "tolerance": 0.001, "unit": "mm"},
                        {"id": "v1", "description": "Скругление", "source": "request",
                         "required": True, "weight": 1, "method": "vision"}], []
                elif "нет-прогресса" in scenario:
                    self.dimensions["D1"] = 40.0
                    kind, requirements, ambiguities = "cad", [
                        {"id": "d1", "description": "Длина 50 мм", "source": "request",
                         "required": True, "weight": 1, "method": "cad", "expected": 50,
                         "tolerance": 0.001, "unit": "mm"}], []
                else:
                    kind, requirements, ambiguities = "informational", [], []
                return httpx.Response(200, json={"choices": [{"message": {
                    "role": "assistant", "content": None, "tool_calls": [{
                        "id": f"define_{len(self.requests)}", "type": "function",
                        "function": {"name": "cadnext_define_goal", "arguments": json.dumps({
                            "kind": kind, "requirements": requirements,
                            "reference_ids": [], "ambiguities": ambiguities})}}]},
                    "finish_reason": "tool_calls"}]})
            if define_result is not None and latest is define_result:
                echo = image_echo() if not any(marker in scenario for marker in (
                    "визуал", "оценка", "нет-прогресса", "код", "уточн",
                    "покажи снимок")) else None
                if echo:
                    return httpx.Response(200, json={"choices": [{"message": {
                        "role": "assistant", "content": echo}, "finish_reason": "stop"}]})
                if "код" in scenario:
                    name, arguments = "execute_python", {"code": "result = 62\nprint(result)"}
                elif "покажи снимок" in scenario:
                    name, arguments = "screenshot", {}
                elif "оценка-70" in scenario or "70" in scenario:
                    name, arguments = "cadnext_bind_requirements", {"bindings": [
                        {"requirement_id": "d1", "tool": "get_dimension", "name": "D1"},
                        {"requirement_id": "d2", "tool": "get_dimension", "name": "D2"}]}
                elif "оценка-100" in scenario:
                    name, arguments = "cadnext_bind_requirements", {"bindings": [
                        {"requirement_id": "d1", "tool": "get_dimension", "name": "D1"}]}
                elif "визуал" in scenario or "нет-прогресса" in scenario:
                    name, arguments = "cadnext_bind_requirements", {"bindings": [
                        {"requirement_id": "d1", "tool": "get_dimension", "name": "D1"}]}
                elif "уточн" in scenario or "информ" in scenario:
                    return httpx.Response(200, json={"choices": [{"message": {
                        "role": "assistant", "content": "Это справочный ответ."},
                        "finish_reason": "stop"}]})
                else:
                    name, arguments = "get_dimension", {
                        "name": "wait" if "долго" in scenario else "D1"}
                return self.tool_response(name, arguments, extra=(
                    [("get_dimension", {"name": "D2"})]
                    if arguments.get("name") == "wait" else []))
            if last_tool_name == "cadnext_bind_requirements":
                if "оценка-70" in scenario or "70" in scenario:
                    return self.tool_response("get_dimension", {"name": "D1"}, extra=[
                        ("get_dimension", {"name": "D2"})])
                if "визуал" in scenario or "нет-прогресса" in scenario or "оценка-100" in scenario:
                    return self.tool_response("get_dimension", {"name": "D1"})
                return self.tool_response("get_dimension", {"name": "D1"})
            if messages and any("Серверная оценка" in str(message.get("content"))
                                for message in messages if message.get("role") == "user"):
                if "оценка-70" in scenario or "70" in scenario:
                    if self.dimensions.get("D2") != 20.0:
                        return self.tool_response("set_dimension", {"name": "D2", "value_mm": 20.0})
                return httpx.Response(200, json={"choices": [{"message": {
                    "role": "assistant", "content": "Готово."}, "finish_reason": "stop"}]})
            echo = image_echo() if define is None else None
            if echo:
                return httpx.Response(200, json={"choices": [{"message": {
                    "role": "assistant", "content": echo}, "finish_reason": "stop"}]})
            if define is None and any(message.get("role") == "assistant"
                                      and message.get("tool_calls") for message in messages):
                return httpx.Response(200, json={"choices": [{"message": {
                    "role": "assistant", "content": "Размер 62 мм. Готово."},
                    "finish_reason": "stop"}]})
            if last_tool_name in {"get_dimension", "set_dimension", "execute_python", "screenshot"}:
                return httpx.Response(200, json={"choices": [{"message": {
                    "role": "assistant", "content": "Размер 62 мм. Готово."},
                    "finish_reason": "stop"}]})
            if latest["role"] == "tool" or isinstance(latest.get("content"), list):
                return httpx.Response(200, json={"choices": [{"message": {
                    "role": "assistant", "content": "Размер 62 мм. Готово."},
                    "finish_reason": "stop"}]})
            if "код" in scenario:
                name, arguments = "execute_python", {"code": "result = 62\nprint(result)"}
            elif "снимок" in scenario:
                name, arguments = "screenshot", {}
            else:
                name, arguments = "get_dimension", {"name": "wait" if "долго" in scenario else "D1"}
            return self.tool_response(name, arguments, extra=(
                [("get_dimension", {"name": "D2"})] if arguments.get("name") == "wait" else []))

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
