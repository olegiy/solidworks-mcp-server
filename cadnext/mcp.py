"""Official SDK transport and one normalization boundary for MCP results."""

import json
import os
import sys
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent


def normalize_result(result) -> dict:
    texts = [block.text for block in result.content if block.type == "text"]
    images = [
        {"data": block.data, "mimeType": block.mimeType}
        for block in result.content if block.type == "image"
    ]
    value = getattr(result, "structuredContent", None)
    if texts:
        try:
            value = json.loads(texts[0])
        except ValueError:
            value = "\n".join(texts)
    failed = bool(result.isError) or (isinstance(value, dict)
                                     and value.get("status") == "error")
    uncertain = isinstance(value, dict) and (
        value.get("error_code") == "CAD_OPERATION_UNCERTAIN"
        or value.get("uncertain") is True
    )
    error = (value.get("error") if isinstance(value, dict) else value) if failed else None
    return {"ok": not failed, "value": value, "images": images,
            "error": str(error or "Ошибка инструмента") if failed else None,
            "uncertain": uncertain}


class SolidWorksMcp:
    def __init__(self, script: Path | None = None):
        self.script = script or ROOT / "solidworks_mcp_server.py"
        self.session: ClientSession | None = None
        self._stack = AsyncExitStack()
        self.uncertain = False

    async def __aenter__(self):
        parameters = StdioServerParameters(
            command=sys.executable, args=[str(self.script)], cwd=str(ROOT),
            env={"PYTHONUTF8": "1", "SW_MCP_CALL_LOG": "off",
                 "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", str(Path.home()))},
        )
        try:
            streams = await self._stack.enter_async_context(stdio_client(parameters))
            self.session = await self._stack.enter_async_context(
                ClientSession(*streams, read_timeout_seconds=timedelta(seconds=70)))
            await self.session.initialize()
            return self
        except BaseException:
            await self._stack.aclose()
            raise

    async def __aexit__(self, *args):
        await self._stack.aclose()

    async def tools(self) -> list[dict]:
        if not self.session:
            raise RuntimeError("MCP не подключён")
        tools, cursor = [], None
        while True:
            result = await self.session.list_tools(cursor=cursor)
            tools.extend({"type": "function", "function": {
                "name": tool.name, "description": tool.description or "",
                "parameters": tool.inputSchema,
            }} for tool in result.tools)
            cursor = result.nextCursor
            if not cursor:
                return tools

    async def call(self, name: str, arguments: dict) -> dict:
        if self.uncertain:
            return {"ok": False, "value": None, "images": [], "uncertain": True,
                    "error": "Результат предыдущей операции неизвестен. Проверьте SolidWorks."}
        if not self.session:
            raise RuntimeError("MCP не подключён")
        try:
            result = normalize_result(await self.session.call_tool(name, arguments))
        except Exception:
            result = {"ok": False, "value": None, "images": [], "uncertain": True,
                      "error": "Соединение MCP прервано; результат CAD-операции неизвестен."}
        self.uncertain = self.uncertain or result["uncertain"]
        return result
