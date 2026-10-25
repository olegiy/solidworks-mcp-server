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


def test_uncertain_error_is_preserved():
    from cadnext.mcp import normalize_result

    result = normalize_result(CallToolResult(content=[TextContent(
        type="text", text=json.dumps({"status": "error", "error": "timeout",
                                      "error_code": "CAD_OPERATION_UNCERTAIN"})
    )]))
    assert not result["ok"]
    assert result["uncertain"]
