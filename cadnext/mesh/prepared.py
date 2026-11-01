"""Single-owner preparation; bytes are verified before every reuse."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from cadnext.mesh.analysis import adjacency, face_geometry
from cadnext.mesh.contracts import (
    IDENTITY_TRANSFORM,
    MeshData,
    MeshError,
    MeshLimits,
    MeshReference,
)
from cadnext.mesh.loader import load_mesh_bytes

PREPARATION_VERSION = 1


@dataclass(frozen=True)
class PreparedMesh(MeshData):
    geometry: tuple
    topology: tuple
    context_id: str = ""


def prepare(mesh, context_id=""):
    geometry, topology = face_geometry(mesh), adjacency(mesh)
    for array in (*geometry, *topology[:2], mesh.vertices, mesh.faces):
        array.setflags(write=False)
    return PreparedMesh(mesh.vertices, mesh.faces, mesh.sha256, mesh.units, mesh.scale_to_mm,
                        geometry, topology, context_id)


class MeshCache:
    def __init__(self):
        self.key = None
        self.mesh = None
        self.derived = {}

    def clear(self, owner=None):
        if owner is None or self.key is not None and self.key[0] == owner:
            self.key, self.mesh = None, None
            self.derived.clear()

    def get(self, payload):
        path = Path(payload["path"]).resolve()
        with path.open("rb") as stream:
            raw = stream.read(MeshLimits().max_bytes+1)
        if len(raw) > MeshLimits().max_bytes:
            self.clear()
            raise MeshError("byte_limit", "Файл сетки превышает допустимый размер.")
        digest = hashlib.sha256(raw).hexdigest()
        if digest != payload["sha256"]:
            self.clear()
            raise MeshError("hash_mismatch", "Исходный файл эталона изменился.")
        transform = payload.get("source_to_cad", IDENTITY_TRANSFORM)
        key = (payload.get("owner", "standalone"), str(path), digest, payload["format"],
               payload["units"], payload["frame_id"], json.dumps(transform), PREPARATION_VERSION)
        if key != self.key:
            self.clear()
            mesh = load_mesh_bytes(raw, format=payload["format"], units=payload["units"])
            reference = MeshReference("internal", digest, payload["format"], payload["units"],
                                      payload.get("tolerance_mm", .1), transform)
            matrix = np.asarray(reference.source_to_cad)
            mesh = MeshData(mesh.vertices @ matrix[:3, :3].T+matrix[:3, 3], mesh.faces,
                            digest, mesh.units, mesh.scale_to_mm)
            self.mesh, self.key = prepare(mesh, payload["frame_id"]), key
        return self.mesh
