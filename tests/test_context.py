import base64
import io
import json
from copy import deepcopy

from PIL import Image, ImageDraw


def image_url(image):
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def test_only_marked_old_cad_views_are_superseded_without_mutating_local_history():
    from cadnext.context import model_context

    def picture(url, **metadata):
        return {"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}],
                **metadata}

    messages = [picture("drawing"), picture("old-view", _cadnext={"source": "screenshot"}),
                {"role": "tool", "tool_call_id": "c1", "content": '{"value": 62, "unit": "mm"}'},
                picture("current-view", _cadnext={"source": "screenshot"})]
    original = deepcopy(messages)
    result = model_context(messages)
    urls = [block["image_url"]["url"] for message in result
            if isinstance(message.get("content"), list) for block in message["content"]
            if block["type"] == "image_url"]
    assert urls == ["drawing", "current-view"]
    assert all("_cadnext" not in message for message in result)
    assert result[2]["tool_call_id"] == "c1"
    assert json.loads(result[2]["content"]) == {"value": 62, "unit": "mm"}
    assert messages == original


def test_repeated_trees_use_exact_deltas_and_errors_remain_complete():
    from cadnext.context import model_context

    before = [{"name": f"Feature{i}", "type": "ProfileFeature"} for i in range(30)]
    after = [*before[1:], {"name": "Cut1", "type": "Cut"}]
    messages = [{"role": "tool", "tool_call_id": str(i), "content": json.dumps({
        "ok": True, "value": {"features": tree}, "verification": {"status": "verified"}})}
        for i, tree in enumerate([before, before, after])]
    failed = {"role": "tool", "tool_call_id": "failed", "content": json.dumps({
        "ok": False, "error": "No retry", "value": {"features": after},
        "verification": {"status": "failed", "actual": 61, "expected": 62, "unit": "mm"}})}
    messages.append(failed)
    original = deepcopy(messages)
    result = model_context(messages)
    first, second, third = [json.loads(message["content"])["value"] for message in result[:3]]
    assert first["features"] == before
    assert second["features_delta"]["edits"] == []
    assert third["features_delta"]["edits"] == [
        {"start": 0, "delete": 1, "insert": []},
        {"start": 30, "delete": 0, "insert": [{"name": "Cut1", "type": "Cut"}]}]
    assert result[-1] == failed
    assert len(json.dumps(result)) < len(json.dumps(messages))
    assert messages == original


def test_tool_protocol_and_requirement_text_are_kept_verbatim():
    from cadnext.context import model_context

    messages = [{"role": "system", "content": "Use mm and degrees"},
                {"role": "user", "content": "Keep thickness 1.25 mm"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1",
                 "type": "function", "function": {"name": "set_dimension",
                 "arguments": '{"name":"D1@Sketch1","value_mm":62}'}}]}]
    assert model_context(messages) == messages


def test_nested_tree_observations_have_unique_reconstructable_references():
    from cadnext.context import model_context

    value = {"features": [{"name": "Main", "type": "Cut"}],
             "inspection": {"features": [{"name": "Other", "type": "Profile"}]}}
    message = {"role": "tool", "tool_call_id": "nested", "content": json.dumps(value)}
    projected = json.loads(model_context([message])[0]["content"])
    assert projected["inspection"]["features_observation"] == 1
    assert projected["features_observation"] == 2


def test_crop_removes_only_empty_margins_and_retains_original_pixels():
    from cadnext.images import optimize_image_url

    drawing = Image.new("RGB", (600, 400), "white")
    ImageDraw.Draw(drawing).rectangle((200, 150, 399, 249), outline="black", width=1)
    url = image_url(drawing)
    optimized = optimize_image_url(url)
    assert len(optimized) < len(url)
    with Image.open(io.BytesIO(base64.b64decode(optimized.split(",", 1)[1]))) as result:
        assert result.size == (216, 116)
        assert result.getpixel((8, 8)) == (0, 0, 0)
        assert result.getpixel((7, 8)) == (255, 255, 255)
        assert result.getpixel((207, 107)) == (0, 0, 0)
    assert drawing.size == (600, 400)


def test_edge_marks_faint_lines_and_transparency_survive_crop():
    from cadnext.images import optimize_image_url

    drawing = Image.new("RGBA", (600, 400), (255, 0, 0, 0))
    pen = ImageDraw.Draw(drawing)
    pen.rectangle((200, 150, 399, 249), outline=(0, 0, 0, 255))
    pen.point((1, 150), fill=(254, 254, 254, 255))
    optimized = optimize_image_url(image_url(drawing))
    with Image.open(io.BytesIO(base64.b64decode(optimized.split(",", 1)[1]))) as result:
        assert result.width == 408
        assert result.getpixel((1, 8)) == (254, 254, 254, 255)
        assert result.getpixel((0, 0))[3] == 0


def test_nonwhite_content_and_invalid_images_are_not_guessed_or_rescaled():
    from cadnext.images import optimize_image_url

    url = image_url(Image.new("RGB", (50, 20), "gray"))
    assert optimize_image_url(url) == url
    assert optimize_image_url("data:image/png;base64,AAAA") == "data:image/png;base64,AAAA"


def test_low_alpha_nonwhite_mark_is_not_mistaken_for_empty_white_margin():
    from cadnext.images import optimize_image_url

    drawing = Image.new("RGBA", (600, 400), "white")
    pen = ImageDraw.Draw(drawing)
    pen.rectangle((200, 150, 399, 249), outline="black")
    pen.point((1, 150), fill=(254, 254, 254, 1))
    optimized = optimize_image_url(image_url(drawing))
    with Image.open(io.BytesIO(base64.b64decode(optimized.split(",", 1)[1]))) as result:
        assert result.width == 408
        assert result.getpixel((1, 8)) == (254, 254, 254, 1)
