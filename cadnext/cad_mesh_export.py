"""Read-only ITessellation extraction. The caller must own the COM STA."""

import hashlib
import json
import math
import os
import time
from pathlib import Path
from uuid import uuid4


def export_mesh(app, document, *, get, identity, empty, root, chord_tolerance_mm,
                max_triangles, stop=lambda: False):
    if (type(chord_tolerance_mm) not in (int, float) or not math.isfinite(chord_tolerance_mm)
            or not 0 < chord_tolerance_mm <= 2.5 or type(max_triangles) is not int
            or not 1 <= max_triangles <= 250000):
        raise ValueError("Некорректный допуск или предел CAD-триангуляции.")
    deadline = time.monotonic()+45
    remaining_reads = 500000

    def read(obj, member, *args):
        nonlocal remaining_reads
        remaining_reads -= 1
        if remaining_reads < 0 or time.monotonic() > deadline or stop():
            raise RuntimeError("Извлечение CAD-сетки остановлено по лимиту или запросу.")
        return get(obj, member, *args)

    document_id = identity(document)
    revision = read(document, "GetUpdateStamp")
    if type(revision) is not int or read(document, "GetType") != 1:
        raise ValueError("Для проверки нужна деталь с доступной ревизией CAD.")

    def check_document():
        active = app.ActiveDoc
        if (active is None or identity(active) != document_id or
                read(document, "GetUpdateStamp") != revision):
            raise RuntimeError("Документ или ревизия CAD изменились во время извлечения.")

    bodies = read(document, "GetBodies2", 0, False) or ()
    if not 1 <= len(bodies) <= 64:
        raise ValueError("В детали нет твёрдых тел или превышен предел их количества.")
    vertices, faces = [], []
    for body in bodies:
        check_document()
        tess = read(body, "GetTessellation", empty)
        if tess is None:
            raise RuntimeError("SolidWorks не предоставил объект триангуляции.")
        tess.NeedFaceFacetMap = True
        tess.ImprovedQuality = True
        tess.CurveChordTolerance = chord_tolerance_mm/1000
        tess.SurfacePlaneTolerance = chord_tolerance_mm/1000
        if not read(tess, "Tessellate"):
            raise RuntimeError("SolidWorks отказал в триангуляции тела.")
        count = read(tess, "GetFacetCount")
        vertex_count = read(tess, "GetVertexCount")
        if (type(count) is not int or not 0 < count <= max_triangles-len(faces) or
                type(vertex_count) is not int or not 0 < vertex_count <= 250000 or
                count*10 > remaining_reads):
            raise ValueError("CAD-сетка превышает предел извлечения.")
        for member in ("CurveChordTolerance", "SurfacePlaneTolerance"):
            tolerance = read(tess, member)
            if type(tolerance) not in (int, float) or not math.isfinite(tolerance) or tolerance <= 0:
                raise ValueError("SolidWorks вернул некорректный допуск триангуляции.")
        facet_ids, seen_faces, vertex_ids = set(), set(), {}
        face = read(body, "GetFirstFace")
        while face is not None:
            face_id = identity(face)
            if face_id in seen_faces or len(seen_faces) >= 10000:
                raise RuntimeError("Некорректный или слишком длинный обход поверхностей CAD.")
            seen_faces.add(face_id)
            for facet in read(tess, "GetFaceFacets", face) or ():
                if facet in facet_ids:
                    raise ValueError("SolidWorks вернул повторный ID треугольника.")
                facet_ids.add(facet)
                if len(facet_ids) > count:
                    raise ValueError("Число полученных треугольников не совпадает с отчётом API.")
                fins = read(tess, "GetFacetFins", facet)
                if fins is None or len(fins) != 3:
                    raise ValueError("В CAD-сетке обнаружена нетреугольная грань.")
                edges = [tuple(read(tess, "GetFinVertices", fin)) for fin in fins]
                ids = set().union(*edges)
                if len(ids) != 3 or any(len(edge) != 2 or edge[0] == edge[1] for edge in edges):
                    raise ValueError("Некорректные рёбра CAD-триангуляции.")
                a, b = edges[0]
                c, = ids-{a, b}
                triangle = []
                for vertex in (a, b, c):
                    if vertex not in vertex_ids:
                        point = read(tess, "GetVertexPoint", vertex)
                        if (point is None or len(point) != 3 or
                                any(type(v) not in (int, float) or not math.isfinite(v) for v in point)):
                            raise ValueError("Некорректная координата CAD-сетки.")
                        vertex_ids[vertex] = len(vertices)
                        vertices.append([v*1000 for v in point])
                    triangle.append(vertex_ids[vertex])
                faces.append(triangle)
            face = read(face, "GetNextFace")
        if len(facet_ids) != count:
            raise RuntimeError("CAD-триангуляция извлечена не полностью.")
        check_document()
    check_document()
    snapshot_id = uuid4().hex
    folder = Path(root)
    folder.mkdir(parents=True, exist_ok=True)
    temporary, final = folder / f"{snapshot_id}.tmp", folder / f"{snapshot_id}.json"
    try:
        raw = json.dumps({"vertices_mm": vertices, "faces": faces}, allow_nan=False,
                         separators=(",", ":")).encode()
        if len(raw) > 100*1024*1024:
            raise ValueError("Артефакт CAD-сетки превышает предел размера.")
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        check_document()
        os.replace(temporary, final)
        return {"snapshot_id": snapshot_id, "document_identity": document_id, "revision": revision,
                "sha256": hashlib.sha256(raw).hexdigest(), "chord_tolerance_mm": chord_tolerance_mm,
                "precision_known": False, "path": str(final), "triangles": len(faces),
                "precision_reason": "Погрешность всей поверхности ITessellation не подтверждена."}
    except BaseException:
        temporary.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        raise
