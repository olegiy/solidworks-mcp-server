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
    if operation == "inspect":
        return {"summary": asdict(inspect_mesh(mesh))}
    if operation == "fit":
        return {"fit": asdict(fit_region(mesh, payload["region"], payload["kind"],
                                         tolerance_mm=payload["tolerance_mm"]))}
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
