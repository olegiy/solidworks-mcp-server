import json

from mcp.types import CallToolResult, ImageContent, TextContent


def test_nested_error_is_failure():
    from cadnext.mcp import normalize_result

    result = normalize_result(CallToolResult(content=[TextContent(
        type="text", text=json.dumps({"status": "error", "error": "failed"})
    )]))
    assert not result["ok"]
    assert result["error"] == "failed"


def test_image_is_preserved():
    from cadnext.mcp import normalize_result

    result = normalize_result(CallToolResult(content=[ImageContent(
        type="image", data="aGVsbG8=", mimeType="image/png"
    )]))
    assert result["ok"]
    assert result["images"] == [{"data": "aGVsbG8=", "mimeType": "image/png"}]


async def test_tools_list_over_real_stdio():
    from cadnext.mcp import SolidWorksMcp

    async with SolidWorksMcp() as client:
        tools = await client.tools()
        assert len(tools) == 27
        assert "execute_python" in {tool["function"]["name"] for tool in tools}


async def test_stdio_shutdown_never_terminates_a_still_running_worker(tmp_path, monkeypatch):
    import asyncio

    import mcp.client.stdio as transport

    from cadnext.mcp import SolidWorksMcp

    release = tmp_path / 'release'
    script = tmp_path / 'slow_server.py'
    script.write_text('''import threading, time
from pathlib import Path
from mcp.server.fastmcp import FastMCP
mcp = FastMCP('slow')
@mcp.tool()
def slow() -> str:
    def worker():
        while not Path(RELEASE).exists():
            time.sleep(0.01)
    threading.Thread(target=worker).start()
    return '{"status":"error","error_code":"CAD_OPERATION_UNCERTAIN"}'
mcp.run()
'''.replace('RELEASE', repr(str(release))), encoding='utf-8')
    forced = []

    async def intercepted_terminate(process):
        forced.append(True)
        await process.wait()

    monkeypatch.setattr(transport, 'PROCESS_TERMINATION_TIMEOUT', 0.05)
    monkeypatch.setattr(transport, '_terminate_process_tree', intercepted_terminate)

    async def release_later():
        await asyncio.sleep(0.3)
        release.touch()

    releaser = None
    try:
        async with SolidWorksMcp(script=script) as client:
            result = await client.call('slow', {})
            assert result['uncertain']
            releaser = asyncio.create_task(release_later())
    finally:
        release.touch()
        if releaser is not None:
            await releaser
    assert not forced


def test_uncertain_error_is_preserved():
    from cadnext.mcp import normalize_result

    result = normalize_result(CallToolResult(content=[TextContent(
        type="text", text=json.dumps({"status": "error", "error": "timeout",
                                      "error_code": "CAD_OPERATION_UNCERTAIN"})
    )]))
    assert not result["ok"]
    assert result["uncertain"]


async def test_screenshot_timeout_latches_uncertainty_over_real_stdio(tmp_path):
    from cadnext.mcp import ROOT, SolidWorksMcp

    script = tmp_path / 'screenshot_timeout.py'
    script.write_text(f'''import sys
sys.path.insert(0, {str(ROOT)!r})
import solidworks_mcp_server as server
def timeout(job):
    raise server.ComOutcomeUnknown('Исход неизвестен')
server.com_call = timeout
server.mcp.run()
''', encoding='utf-8')
    async with SolidWorksMcp(script=script) as client:
        result = await client.call('screenshot', {})
        assert not result['ok']
        assert result['uncertain']
        assert client.uncertain
        assert (await client.call('get_feature_tree', {}))['uncertain']
