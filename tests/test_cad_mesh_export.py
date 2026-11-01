"""Read-only tessellation uses exact identities and rejects incomplete snapshots."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import solidworks_mcp_server as server


class Tessellation:
    def __init__(self):
        self.NeedFaceFacetMap = False
        self.ImprovedQuality = False
        self.CurveChordTolerance = 0
        self.SurfacePlaneTolerance = 0

    def Tessellate(self):
        return True

    def GetFacetCount(self):
        return 1

    def GetVertexCount(self):
        return 3

    def GetFaceFacets(self, face):
        assert self.NeedFaceFacetMap
        return [17]  # IDs need not start at zero or be contiguous.

    def GetFacetFins(self, facet):
        assert facet == 17
        return [4, 8, 12]

    def GetFinVertices(self, fin):
        return {4: [9, 11], 8: [11, 23], 12: [23, 9]}[fin]

    def GetVertexPoint(self, vertex):
        return {9: [0, 0, 0], 11: [.01, 0, 0], 23: [0, .02, 0]}[vertex]


def fake_cad():
    tess = Tessellation()
    face = SimpleNamespace(GetNextFace=None)
    body = SimpleNamespace(GetTessellation=lambda empty: tess, GetFirstFace=face)
    document = SimpleNamespace(GetType=1, GetUpdateStamp=42, GetBodies2=lambda kind, visible: [body])
    app = SimpleNamespace(ActiveDoc=document, GetDocuments=[document])
    return app, document, tess


def test_export_is_in_sta_and_writes_mm_snapshot_with_unknown_precision(tmp_path, monkeypatch):
    app, document, tess = fake_cad()
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(server, "_ensure_executor", lambda: executor)
    monkeypatch.setattr(server, "_poisoned", False)
    monkeypatch.setattr(server, "_pending", {"fut": None, "started": 0, "what": ""})
    threads = []

    def cad_app():
        threads.append(threading.get_ident())
        return app

    monkeypatch.setattr(server, "_app", cad_app)
    monkeypatch.setattr(server, "_verification_mesh_root", lambda: tmp_path)
    session = json.loads(server.open_python_session())["session_id"]
    try:
        result = json.loads(server.export_verification_mesh(session, .01, 100))
        assert result["status"] == "ok", result
        snapshot = result["snapshot"]
        assert snapshot["revision"] == 42
        assert snapshot["document_identity"]
        assert snapshot["precision_known"] is False
        assert snapshot["chord_tolerance_mm"] == pytest.approx(.01)
        assert threads and set(threads) != {threading.get_ident()}
        from pathlib import Path

        raw = json.loads(Path(snapshot["path"]).read_text())
        assert raw["vertices_mm"] == [[0, 0, 0], [10, 0, 0], [0, 20, 0]]
        assert raw["faces"] == [[0, 1, 2]]
        assert tess.CurveChordTolerance == .00001
    finally:
        server.close_python_session(session)
        executor.shutdown()


@pytest.mark.parametrize("failure", ["no_body", "refusal", "changed", "switch", "limit", "foreign_session"])
def test_export_rejects_missing_or_changed_cad_and_cleans_artifact(tmp_path, monkeypatch, failure):
    app, document, tess = fake_cad()
    monkeypatch.setattr(server, "_app", lambda: app)
    monkeypatch.setattr(server, "com_call", lambda job, **kwargs: job())
    monkeypatch.setattr(server, "_verification_mesh_root", lambda: tmp_path)
    session = json.loads(server.open_python_session())["session_id"]
    if failure == "no_body":
        document.GetBodies2 = lambda *args: []
    elif failure == "refusal":
        tess.Tessellate = lambda: False
    elif failure == "changed":
        tess.Tessellate = lambda: setattr(document, "GetUpdateStamp", 43) or True
    elif failure == "switch":
        tess.Tessellate = lambda: setattr(app, "ActiveDoc", SimpleNamespace()) or True
    elif failure == "limit":
        tess.GetFacetCount = lambda: 999999
    try:
        response = json.loads(server.export_verification_mesh(
            "foreign" if failure == "foreign_session" else session, .01, 100))
        assert response["status"] == "error"
        assert response["error_code"] == "CAD_ERROR"
        assert not list(tmp_path.iterdir())
    finally:
        server.close_python_session(session)


def test_hidden_export_is_never_offered_to_model():
    from cadnext.construction import model_tools
    from tests.fake_mcp import tool

    catalog = model_tools([tool("export_verification_mesh")])
    assert "export_verification_mesh" not in {t["function"]["name"] for t in catalog}


def test_cooperative_stop_prevents_snapshot_publication(tmp_path):
    from cadnext.cad_mesh_export import export_mesh

    app, document, _ = fake_cad()
    calls = 0

    def stopping():
        nonlocal calls
        calls += 1
        return calls > 10

    with pytest.raises(RuntimeError):
        export_mesh(app, document, get=server._get, identity=lambda obj: str(id(obj)),
                    empty=None, root=tmp_path, chord_tolerance_mm=.01, max_triangles=100,
                    stop=stopping)
    assert not list(tmp_path.iterdir())


def test_unknown_com_outcome_is_preserved_for_hidden_export(monkeypatch):
    def unknown(job):
        raise server.ComOutcomeUnknown("Исход неизвестен")

    monkeypatch.setattr(server, "com_call", unknown)
    response = json.loads(server.export_verification_mesh("session", .01))
    assert response["error_code"] == "CAD_OPERATION_UNCERTAIN"
