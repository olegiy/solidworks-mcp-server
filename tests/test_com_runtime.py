import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import solidworks_mcp_server as server


def test_timeout_blocks_followup_even_after_call_finishes(monkeypatch):
    gate = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(server, "_ensure_executor", lambda: executor)
    monkeypatch.setattr(server, "_pending", {"fut": None, "started": 0, "what": ""})
    monkeypatch.setattr(server, "_poisoned", False, raising=False)
    monkeypatch.setattr(server, "COM_TIMEOUT", 0.02)
    try:
        with pytest.raises(RuntimeError):
            server.com_call(lambda: gate.wait(2))
        gate.set()
        server._pending["fut"].result(timeout=2)
        with pytest.raises(RuntimeError, match="неизвестен"):
            server.com_call(lambda: "must not execute")
    finally:
        gate.set()
        executor.shutdown(wait=True)


def test_angular_dimension_conversion(monkeypatch):
    dimension = SimpleNamespace(SystemValue=math.pi / 2, GetType=3)
    model = SimpleNamespace(Parameter=lambda name: dimension, EditRebuild3=True)
    monkeypatch.setattr(server, "_model", lambda *args: model)
    monkeypatch.setattr(server, "com_call", lambda job: job())
    result = json.loads(server.get_dimension("D1@Sketch1"))
    assert result.get("unit") == "deg"
    assert result.get("value") == pytest.approx(90)
    result = json.loads(server.set_dimension("D1@Sketch1", 45))
    assert result["status"] == "ok"
    assert dimension.SystemValue == pytest.approx(math.pi / 4)


def test_screenshot_temp_cleanup(monkeypatch, tmp_path):
    from PIL import Image

    paths = []

    def save(path, width, height):
        paths.append(path)
        Image.new("RGB", (10, 10)).save(path)
        return True

    monkeypatch.setattr(server.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(server, "_model", lambda: SimpleNamespace(SaveBMP=save))
    monkeypatch.setattr(server, "com_call", lambda job: job())
    server.screenshot(zoom_to_fit=False)
    server.screenshot(zoom_to_fit=False)
    assert len(set(paths)) == 2
    assert list(tmp_path.iterdir()) == []


def test_failed_save_is_not_retried(monkeypatch, tmp_path):
    calls = []

    def fail(*args):
        calls.append("save")
        return False

    model = SimpleNamespace(Extension=SimpleNamespace(SaveAs=fail),
                            SaveAs3=fail, SaveAs2=fail, SaveAs=fail)
    monkeypatch.setattr(server, "_model", lambda: model)
    monkeypatch.setattr(server, "com_call", lambda job: job())
    result = json.loads(server.save_document(str(tmp_path / "part.sldprt")))
    assert result["status"] == "error"
    assert len(calls) == 1


def test_attach_never_launches_or_switches_process(monkeypatch):
    import win32com.client

    app = SimpleNamespace(GetProcessID=123, Visible=True)
    monkeypatch.setattr(server, "_sw", {"app": None})
    monkeypatch.setattr(server, "_running_sw_pids", lambda: [123])
    monkeypatch.setattr(win32com.client, "GetActiveObject", lambda name: app)
    assert server._app() is app
    app.GetProcessID = 456
    with pytest.raises(RuntimeError):
        server._app()
