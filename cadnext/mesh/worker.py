"""Bounded JSON-lines geometry worker. No COM, network, or arbitrary code protocol."""

import json
import sys
from dataclasses import asdict

IPC_LIMIT = 1024 * 1024


def dispatch(operation, payload):
    # Wait for the parent to install its Job Object before loading native libraries.
    from cadnext.mesh.analysis import fit_region, inspect_mesh
    from cadnext.mesh.contracts import MeshError
    from cadnext.mesh.loader import load_mesh

    if operation not in {"inspect", "fit", "section", "compare"}:
        raise MeshError("unknown_operation", "Неизвестная операция анализа сетки.")
    mesh = load_mesh(payload["path"], format=payload["format"], units=payload["units"])
    if mesh.sha256 != payload["sha256"]:
        raise MeshError("hash_mismatch", "Исходный файл эталона изменился.")
    if "source_to_cad" in payload:
        import numpy as np

        from cadnext.mesh.contracts import MeshData, MeshReference

        reference = MeshReference("internal", mesh.sha256, payload["format"], payload["units"],
                                  payload["tolerance_mm"], payload["source_to_cad"])
        transform = np.asarray(reference.source_to_cad)
        mesh = MeshData(mesh.vertices @ transform[:3, :3].T+transform[:3, 3],
                        mesh.faces, mesh.sha256, mesh.units, mesh.scale_to_mm)
    if operation == "inspect":
        summary = asdict(inspect_mesh(mesh))
        offset = payload.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= 1000000:
            raise MeshError("invalid_offset", "Некорректная страница областей сетки.")
        summary["regions_total"] = len(summary["regions"])
        summary["regions_offset"] = offset
        summary["regions"] = summary["regions"][offset:offset+100]
        return {"summary": summary}
    if operation == "fit":
        return {"fit": asdict(fit_region(mesh, payload["region"], payload["kind"],
                                         tolerance_mm=payload["tolerance_mm"]))}
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
            response.update(dispatch(request["operation"], payload))
        except Exception as exc:
            from cadnext.mesh.contracts import MeshError

            response["error"] = {"code": exc.code if isinstance(exc, MeshError) else "analysis_failed",
                                 "message": str(exc) if isinstance(exc, MeshError) else
                                 "Не удалось обработать геометрию сетки."}
        encoded = json.dumps(response, allow_nan=False, ensure_ascii=True).encode()+b"\n"
        if len(encoded) > IPC_LIMIT:
            response = {key: response[key] for key in ("request_id", "sha256", "frame_id")}
            response["error"] = {"code": "ipc_limit", "message": "Результат анализа слишком велик."}
            encoded = json.dumps(response).encode()+b"\n"
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
