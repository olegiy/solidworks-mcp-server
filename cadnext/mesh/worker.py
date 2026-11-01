"""Bounded JSON-lines geometry worker. No COM, network, or arbitrary code protocol."""

import json
import sys
from dataclasses import asdict

from cadnext.mesh.transport import ResultTransport

IPC_LIMIT = 1024 * 1024


def dispatch(operation, payload, cache=None):
    # Wait for the parent to install its Job Object before loading native libraries.
    from cadnext.mesh.analysis import fit_region, inspect_mesh
    from cadnext.mesh.contracts import MeshError
    from cadnext.mesh.prepared import MeshCache

    if operation not in {"inspect", "fit", "section", "compare"}:
        raise MeshError("unknown_operation", "Неизвестная операция анализа сетки.")
    cache = cache or MeshCache()
    mesh = cache.get(payload)

    def catalog():
        from cadnext.mesh.surfaces import surface_catalog

        tolerance = payload["tolerance_mm"]
        key = ("surfaces", tolerance)
        if key not in cache.derived:
            cache.derived[key] = surface_catalog(mesh, tolerance)
        return cache.derived[key]

    if operation == "inspect":
        if payload.get("catalog", "regions") == "surfaces":
            summary = dict(catalog())
            offset = payload.get("offset", 0)
            if type(offset) is not int or not 0 <= offset <= 1000000:
                raise MeshError("invalid_offset", "Некорректная страница поверхностей.")
            summary.update(surfaces_total=len(summary["surfaces"]), surfaces_offset=offset)
            summary["surfaces"] = summary["surfaces"][offset:offset+100]
            return {"summary": summary}
        if "summary" not in cache.derived:
            cache.derived["summary"] = asdict(inspect_mesh(mesh))
        summary = dict(cache.derived["summary"])
        offset = payload.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= 1000000:
            raise MeshError("invalid_offset", "Некорректная страница областей сетки.")
        summary["regions_total"] = len(summary["regions"])
        summary["regions_offset"] = offset
        summary["regions"] = summary["regions"][offset:offset+100]
        return {"summary": summary}
    if operation == "fit":
        region = payload["region"]
        records = catalog() if isinstance(region, dict) and ("surface_id" in region or "seed_mm" in region) else None
        return {"fit": asdict(fit_region(mesh, payload["region"], payload["kind"],
                                         tolerance_mm=payload["tolerance_mm"], catalog=records))}
    if operation == "section":
        from cadnext.mesh.contracts import PlaneFrame
        from cadnext.mesh.sections import section_mesh

        return {"section": asdict(section_mesh(mesh, PlaneFrame(**payload["frame"]),
                                              tolerance_mm=payload["tolerance_mm"]))}
    if operation == "compare":
        import hashlib
        from pathlib import Path

        import numpy as np

        from cadnext.mesh.compare import compare_surfaces
        from cadnext.mesh.contracts import MeshData

        with Path(payload["cad_path"]).open("rb") as stream:
            raw = stream.read(100*1024*1024+1)
        if len(raw) > 100*1024*1024 or hashlib.sha256(raw).hexdigest() != payload["cad_sha256"]:
            raise MeshError("hash_mismatch", "Проверочная CAD-сетка изменилась или превышает лимит.")
        geometry = json.loads(raw)
        vertices = np.asarray(geometry["vertices_mm"], dtype=float)
        faces = np.asarray(geometry["faces"], dtype=float)
        if (vertices.ndim != 2 or vertices.shape[1] != 3 or not 0 < len(vertices) <= 250000
                or faces.ndim != 2 or faces.shape[1] != 3 or not 0 < len(faces) <= 250000
                or not np.isfinite(vertices).all() or not np.isfinite(faces).all()
                or np.any(faces != np.floor(faces)) or faces.min() < 0 or faces.max() >= len(vertices)):
            raise MeshError("invalid_snapshot", "Некорректная геометрия проверочной CAD-сетки.")
        vertices, inverse = np.unique(vertices, axis=0, return_inverse=True)
        cad = MeshData(vertices, inverse[faces.astype(int)], payload["cad_sha256"], "mm", 1.)
        return {"comparison": asdict(compare_surfaces(mesh, cad, options=payload["options"]))}
    raise MeshError("unavailable", "Эта операция анализа сетки пока недоступна.")


def main():
    cache = None
    transport = ResultTransport()
    while raw := sys.stdin.buffer.readline(IPC_LIMIT+1):
        if len(raw) > IPC_LIMIT:
            return 2
        try:
            request = json.loads(raw)
            payload = request["payload"]
            response = {"request_id": request["request_id"], "sha256": payload["sha256"],
                        "frame_id": payload["frame_id"]}
        except (KeyError, TypeError, ValueError):
            return 2
        try:
            if request["operation"] == "result_page":
                response.update(transport.page(payload))
            elif request["operation"] == "forget":
                if cache is not None:
                    cache.clear(payload.get("owner"))
                transport.result = None
                response["forgotten"] = True
            else:
                if cache is None:
                    from cadnext.mesh.prepared import MeshCache
                    cache = MeshCache()
                response.update(dispatch(request["operation"], payload, cache))
        except Exception as exc:
            from cadnext.mesh.contracts import MeshError

            response["error"] = {"code": exc.code if isinstance(exc, MeshError) else "analysis_failed",
                                 "message": str(exc) if isinstance(exc, MeshError) else
                                 "Не удалось обработать геометрию сетки."}
        encoded = transport.encode(response)
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
