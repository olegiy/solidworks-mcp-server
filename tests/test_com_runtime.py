import ast
import inspect
import json
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import solidworks_mcp_server as server


def test_python_error_reports_line_without_repeating_partial_changes(monkeypatch):
    rebuilds = []

    class Model:
        changed = False

        @property
        def EditRebuild3(self):
            rebuilds.append(True)
            return True

    model = Model()
    monkeypatch.setattr(server, "_app", lambda: SimpleNamespace(ActiveDoc=model))
    monkeypatch.setattr(server, "_nothing", lambda: None)
    monkeypatch.setattr(server, "com_call", lambda job: job())
    response = json.loads(server.execute_python(
        'model.changed = True\nmodel.EditRebuild3()\nmodel.changed = False'))
    assert response["status"] == "error"
    assert response["error"] == "'bool' object is not callable"
    assert response["python_line"] == 2
    assert response["error_type"] == "TypeError"
    assert response["error_code"] == "CAD_ERROR"
    assert model.changed is True
    assert rebuilds == [True]


def test_python_syntax_error_reports_line_before_any_changes(monkeypatch):
    model = SimpleNamespace(changed=False)
    monkeypatch.setattr(server, "_app", lambda: SimpleNamespace(ActiveDoc=model))
    monkeypatch.setattr(server, "_nothing", lambda: None)
    monkeypatch.setattr(server, "com_call", lambda job: job())
    response = json.loads(server.execute_python('model.changed = True\nif :\n    pass'))
    assert response["status"] == "error"
    assert response["python_line"] == 2
    assert response["error_type"] == "SyntaxError"
    assert response["error_code"] == "CAD_ERROR"
    assert model.changed is False


def test_documented_python_inspection_reads_body_box_and_point_objects(monkeypatch):
    default_calls = []

    class Point:
        def __init__(self, x, y, z):
            self.X, self.Y, self.Z = x, y, z

        def __call__(self):
            default_calls.append(True)
            raise RuntimeError("COM point has no default member")

    line = SimpleNamespace(GetType=0, GetStartPoint2=Point(.001, .002, 0),
                           GetEndPoint2=Point(.061, .002, 0))
    sketch = SimpleNamespace(GetSketchSegments=[line, SimpleNamespace(GetType=1)])
    feature = SimpleNamespace(Name="Эскиз1", GetTypeName2="ProfileFeature",
                              GetSpecificFeature2=sketch, GetNextFeature=None)
    body = SimpleNamespace(GetBodyBox=(0, -.002, 0, .062, .04, .062))
    model = SimpleNamespace(GetType=1, GetBodies2=lambda kind, visible: [body], FirstFeature=feature)
    monkeypatch.setattr(server, "_app", lambda: SimpleNamespace(ActiveDoc=model))
    monkeypatch.setattr(server, "_nothing", lambda: None)
    monkeypatch.setattr(server, "com_call", lambda job: job())
    # Execute the example actually supplied to the model, against COM-shaped objects.
    example = re.search(r"```python\n(.*?)\n```", inspect.getdoc(server.execute_python), re.S)
    assert example is not None, "The model needs an executable inspection example"
    response = json.loads(server.execute_python(example.group(1)))
    assert response["status"] == "ok", response
    result = ast.literal_eval(response["result"])
    assert result["bbox_mm"] == [[0, -2, 0, 62, 40, 62]]
    assert result["bbox_approximate"] is True
    assert result["sketch_name"] == "Эскиз1"
    assert result["segments"] == [{"type": "line", "start_mm": [1, 2, 0], "end_mm": [61, 2, 0]},
                                  {"type": "unsupported", "segment_type": 1}]
    assert default_calls == []


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


@pytest.mark.parametrize("sheet_metal_bodies,normal_cut", [
    ([False], False), ([True], True), ([True, False], False),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_cut_through_uses_sheet_metal_flags_only_for_sheet_metal_bodies(
        monkeypatch, sheet_metal_bodies, normal_cut, reverse):
    calls = []
    selections = []

    def cut(*args):
        calls.append(args)
        if args[17] != normal_cut or args[26] != normal_cut:
            return None
        return SimpleNamespace(Name="Cut1")

    model = SimpleNamespace(
        GetBodies2=lambda kind, visible: [SimpleNamespace(IsSheetMetal=flag)
                                         for flag in sheet_metal_bodies],
        ClearSelection2=lambda clear: None,
        FeatureManager=SimpleNamespace(FeatureCut4=cut))
    monkeypatch.setattr(server, "_model", lambda *args: model)
    monkeypatch.setattr(server, "_feature_names", lambda *args: [("Sketch4", "ProfileFeature")])
    monkeypatch.setattr(server, "_select", lambda *args: selections.append(args[1:]))
    monkeypatch.setattr(server, "com_call", lambda job: job())

    response = json.loads(server.cut_through(reverse=reverse))
    assert response["status"] == "ok", response
    assert response["feature"] == "Cut1"
    assert selections == [("Sketch4", "SKETCH")]
    assert len(calls) == 1
    assert len(calls[0]) == 27
    assert calls[0][:5] == (True, False, reverse, 1, 0)


@pytest.mark.parametrize("raises", [False, True])
def test_failed_cut_through_is_not_retried(monkeypatch, raises):
    calls = []

    def cut(*args):
        calls.append(args)
        if raises:
            raise RuntimeError("COM failure")
        return None

    model = SimpleNamespace(
        GetBodies2=lambda kind, visible: [SimpleNamespace(IsSheetMetal=False)],
        ClearSelection2=lambda clear: None,
        FeatureManager=SimpleNamespace(FeatureCut4=cut))
    monkeypatch.setattr(server, "_model", lambda *args: model)
    monkeypatch.setattr(server, "_select", lambda *args: True)
    monkeypatch.setattr(server, "com_call", lambda job: job())

    response = json.loads(server.cut_through("Sketch4", reverse=True))
    assert response["status"] == "error"
    assert response["error_code"] == "CAD_ERROR"
    assert len(calls) == 1
    if raises:
        assert response["error"] == "COM failure"
    else:
        assert "Sketch4" in response["error"]
        assert "reverse=True" in response["error"]
        assert "FeatureCut4" in response["error"]
        assert "None" in response["error"]


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
