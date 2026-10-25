import base64
import io
import json
import random

import pytest
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin

from tests.test_app import profile, start, wait
from tests.web_fixture import Backend


@pytest.fixture
def api(tmp_path):
    from cadnext.app import create_app

    backend = Backend(tmp_path)
    app = create_app(settings=backend.settings, mcp_factory=backend.mcp,
                     provider_factory=backend.provider)
    with TestClient(app, base_url="http://127.0.0.1:8876", raise_server_exceptions=False) as client:
        yield client, backend


def attachment(*, image_format="PNG", size=(3, 2), metadata=False, animated=False):
    image = Image.new("RGB", size, "red")
    output = io.BytesIO()
    options = {}
    if metadata:
        info = PngImagePlugin.PngInfo()
        info.add_text("private", "Company secret metadata")
        if image_format == "PNG":
            options["pnginfo"] = info
        else:
            exif = Image.Exif()
            exif[270] = "Company secret metadata"
            options["exif"] = exif
    if animated:
        options.update(save_all=True, append_images=[Image.new("RGB", size, "blue")], duration=10)
    image.save(output, format=image_format, **options)
    return {"name": "private-drawing.png", "mimeType": {
        "PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[image_format],
        "data": base64.b64encode(output.getvalue()).decode("ascii")}


def test_user_image_reaches_real_provider_with_masked_text_and_survives_reload(api):
    client, backend = api
    saved = profile(client, base_url="https://example.test/v1", vision=True)
    image = attachment()
    text = r"Объясни C:\Company\part.sldprt private-project"
    response = start(client, saved["id"], text, attachments=[image], sensitive=["private-project"])
    assert response.status_code == 202, response.text
    result = wait(client, response.json()["id"])
    assert result["status"] == "completed"
    user = next(event["data"] for event in result["events"]
                if event["type"] == "message" and event["data"]["role"] == "user")
    assert user["content"] == text
    assert user["attachments"][0]["name"] == image["name"]
    content = backend.requests[0]["messages"][-1]["content"]
    assert content[0]["type"] == "text"
    assert content[1] == {"type": "image_url", "image_url": {
        "url": "data:image/png;base64," + user["attachments"][0]["data"]}}
    outgoing = json.dumps(backend.requests)
    assert "Company" not in outgoing and "private-project" not in outgoing
    assert image["name"] not in outgoing
    assert client.get("/api/runs?session_id=session-1").json()[0] == result
    second = start(client, saved["id"], "Продолжай", sensitive=["private-project"])
    assert second.status_code == 202
    wait(client, second.json()["id"])
    assert content in [message["content"] for message in backend.requests[1]["messages"]]


def test_image_only_request_is_accepted_but_empty_request_is_rejected(api):
    client, backend = api
    saved = profile(client, vision=True)
    assert start(client, saved["id"], "   ").status_code == 422
    response = start(client, saved["id"], "", attachments=[attachment()])
    assert response.status_code == 202, response.text
    assert wait(client, response.json()["id"])["status"] == "completed"
    assert backend.requests[0]["messages"][-1]["content"][0]["type"] == "image_url"


def test_images_require_vision_before_provider_or_cad(api):
    client, backend = api
    response = start(client, profile(client, vision=False)["id"], attachments=[attachment()])
    assert response.status_code == 422
    assert "изображ" in response.json()["detail"].lower()
    assert "vision" in response.json()["detail"].lower()
    assert backend.requests == [] and backend.calls == [] and backend.profiles == []
    assert client.get("/api/runs").json() == []


@pytest.mark.parametrize("case", ["base64", "corrupt", "mime", "svg", "count", "file_size",
                                  "total_size", "side", "pixels", "animated", "empty_name"])
def test_invalid_images_are_rejected_without_provider_requests_or_cad(api, case):
    client, backend = api
    images = [attachment()]
    if case == "base64":
        images[0]["data"] = "not base64!"
    elif case == "corrupt":
        images[0]["data"] = base64.b64encode(b"not an image").decode()
    elif case == "mime":
        images[0]["mimeType"] = "image/jpeg"
    elif case == "svg":
        images[0].update(mimeType="image/svg+xml", data=base64.b64encode(b"<svg/>").decode())
    elif case == "count":
        images *= 4
    elif case in {"file_size", "total_size"}:
        raw = base64.b64decode(images[0]["data"])
        length = 2 * 1024 * 1024 + 1 if case == "file_size" else 1536 * 1024
        images[0]["data"] = base64.b64encode(raw + b"\0" * (length - len(raw))).decode()
        if case == "total_size":
            images *= 3
    elif case == "side":
        images = [attachment(size=(8193, 1))]
    elif case == "pixels":
        images = [attachment(size=(4097, 4097))]
    elif case == "animated":
        images = [attachment(animated=True)]
    elif case == "empty_name":
        images[0]["name"] = ""
    response = start(client, profile(client, vision=True)["id"], attachments=images)
    assert response.status_code == 422, response.text
    assert any(word in response.json()["detail"].lower() for word in ("изображ", "вложен", "файл"))
    assert backend.requests == [] and backend.calls == []
    assert client.get("/api/runs").json() == []


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP"])
def test_image_metadata_is_removed_and_pixels_remain_readable(api, image_format):
    client, backend = api
    original = attachment(image_format=image_format, metadata=True)
    response = start(client, profile(client, vision=True)["id"], attachments=[original])
    assert response.status_code == 202, response.text
    result = wait(client, response.json()["id"])
    sanitized = result["events"][0]["data"]["attachments"][0]
    with Image.open(io.BytesIO(base64.b64decode(sanitized["data"]))) as image:
        image.load()
        assert image.size == (3, 2)
        assert not image.getexif()
        assert "private" not in image.info
    assert "Company" not in str(base64.b64decode(sanitized["data"]))
    assert sanitized["mimeType"] == original["mimeType"]
    assert backend.requests[0]["messages"][-1]["content"][-1]["image_url"]["url"].startswith(
        f'data:{original["mimeType"]};base64,')


def test_jpeg_exif_orientation_is_applied_before_metadata_removal(api):
    client, backend = api
    with Image.new("RGB", (3, 2), "red") as image:
        exif = Image.Exif()
        exif[274] = 6
        output = io.BytesIO()
        image.save(output, format="JPEG", exif=exif)
    value = {"name": "phone.jpg", "mimeType": "image/jpeg",
             "data": base64.b64encode(output.getvalue()).decode()}
    response = start(client, profile(client, vision=True)["id"], attachments=[value])
    assert response.status_code == 202, response.text
    result = wait(client, response.json()["id"])
    sanitized = result["events"][0]["data"]["attachments"][0]
    with Image.open(io.BytesIO(base64.b64decode(sanitized["data"]))) as image:
        image.load()
        assert image.size == (2, 3)
        assert not image.getexif()
    content = backend.requests[0]["messages"][-1]["content"]
    assert content[-1]["image_url"]["url"] == "data:image/jpeg;base64," + sanitized["data"]


def test_image_history_overflow_is_rejected_before_new_run_or_cad(api):
    client, backend = api
    saved = profile(client, vision=True)
    first = start(client, saved["id"], "Описание", attachments=[attachment()])
    assert first.status_code == 202, first.text
    result = wait(client, first.json()["id"])
    assert result["status"] == "completed"
    requests_before = len(backend.requests)
    calls_before = len(backend.calls)
    client.app.state.agent.max_history_bytes = len(json.dumps(result["messages"], ensure_ascii=False).encode()) + 10
    response = start(client, saved["id"], "Ещё изображение", attachments=[attachment()])
    assert response.status_code == 422
    assert "истори" in response.json()["detail"].lower()
    assert len(backend.requests) == requests_before and len(backend.calls) == calls_before
    assert len(client.get("/api/runs").json()) == 1


def test_reencoded_image_size_is_bounded_even_when_source_is_under_limit(api):
    client, backend = api
    pixels = random.Random(1).randbytes(1000 * 1000)
    with Image.frombytes("L", (1000, 1000), pixels) as image:
        output = io.BytesIO()
        image.save(output, format="PNG")
    assert len(output.getvalue()) < 2 * 1024 * 1024
    value = {"name": "noise.png", "mimeType": "image/png",
             "data": base64.b64encode(output.getvalue()).decode()}
    response = start(client, profile(client, vision=True)["id"], attachments=[value])
    assert response.status_code == 422
    assert "после обработки" in response.json()["detail"]
    assert backend.requests == [] and backend.calls == []


async def test_combined_model_reply_and_image_history_limit_prevents_cad():
    from cadnext.agent import Agent
    from tests.fake_mcp import FakeMcp, ok
    from tests.test_agent import ScriptedProvider, reply, settle

    model = ScriptedProvider([reply(("read", "get_dimension", {"name": "D1"}))], vision=True)
    mcp = FakeMcp([("get_dimension", ok({"value_mm": 62}))])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Прочитай", attachments=[attachment()])
    agent.max_history_bytes = len(json.dumps(run.messages, ensure_ascii=False).encode()) + 10
    await settle(agent, run)
    assert run.status == "failed"
    assert mcp.calls == []
    assert len(model.requests) == 1
