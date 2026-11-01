"""Read bounded triangle-only input without repair, materials, or resolvers."""

import hashlib
import io
import struct
from pathlib import Path

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from cadnext.mesh.contracts import MeshData, MeshError, MeshLimits, input_scale


def _invalid():
    return MeshError("invalid_geometry", "Сетка повреждена или содержит неподдерживаемую геометрию.")


def _lines(raw, limits):
    stream = io.BytesIO(raw)
    while line := stream.readline(limits.max_line_bytes + 1):
        if len(line) > limits.max_line_bytes:
            raise MeshError("line_limit", "Строка файла сетки превышает допустимый размер.")
        try:
            yield line.decode("utf-8-sig").split("#", 1)[0].split()
        except UnicodeDecodeError:
            raise _invalid() from None


def _binary_stl(raw, limits):
    count = struct.unpack_from("<I", raw, 80)[0]
    if count > limits.max_triangles:
        raise MeshError("triangle_limit", "В сетке слишком много треугольников.")
    if not count or len(raw) != 84 + 50 * count:
        raise _invalid()
    dtype = np.dtype([("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attr", "<u2")])
    records = np.frombuffer(raw, dtype=dtype, offset=84, count=count)
    return records["vertices"].reshape((-1, 3)).astype(np.float64), np.arange(count*3).reshape((-1, 3))


def _ascii_stl(raw, limits):
    vertices = []
    stage = "solid"
    triangle_vertices = 0
    ended = False
    for fields in _lines(raw, limits):
        if not fields:
            continue
        key = fields[0]
        if ended:
            raise _invalid()
        if stage == "solid" and key == "solid":
            stage = "facet"
        elif stage == "facet" and key == "facet" and len(fields) == 5 and fields[1] == "normal":
            if len(vertices) // 3 >= limits.max_triangles:
                raise MeshError("triangle_limit", "В сетке слишком много треугольников.")
            try:
                if not np.isfinite([float(v) for v in fields[2:]]).all():
                    raise _invalid()
            except ValueError:
                raise _invalid() from None
            stage = "loop"
        elif stage == "facet" and key == "endsolid":
            ended = True
        elif stage == "loop" and fields == ["outer", "loop"]:
            stage = "vertex"
            triangle_vertices = 0
        elif stage == "vertex" and key == "vertex" and len(fields) == 4:
            try:
                vertices.append(tuple(float(v) for v in fields[1:]))
            except ValueError:
                raise _invalid() from None
            triangle_vertices += 1
            if triangle_vertices == 3:
                stage = "endloop"
        elif stage == "endloop" and fields == ["endloop"]:
            stage = "endfacet"
        elif stage == "endfacet" and fields == ["endfacet"]:
            stage = "facet"
        else:
            raise _invalid()
    if not ended or not vertices:
        raise _invalid()
    return np.asarray(vertices, dtype=np.float64), np.arange(len(vertices)).reshape((-1, 3))


def _obj(raw, limits):
    vertices, faces = [], []
    for fields in _lines(raw, limits):
        if not fields:
            continue
        if fields[0] == "v":
            if len(fields) != 4:
                raise _invalid()
            if len(vertices) >= limits.max_vertices:
                raise MeshError("vertex_limit", "В сетке слишком много вершин.")
            try:
                vertices.append(tuple(float(v) for v in fields[1:]))
            except ValueError:
                raise _invalid() from None
        elif fields[0] == "f":
            if len(fields) != 4:
                raise MeshError("non_triangular_obj", "В OBJ нужны треугольные грани. Триангулируйте модель при экспорте.")
            if len(faces) >= limits.max_triangles:
                raise MeshError("triangle_limit", "В сетке слишком много треугольников.")
            face = []
            for token in fields[1:]:
                try:
                    parts = token.split("/")
                    if len(parts) > 3 or any(part and not int(part) for part in parts):
                        raise _invalid()
                    index = int(parts[0])
                except (ValueError, OverflowError):
                    raise _invalid() from None
                resolved = index-1 if index > 0 else len(vertices)+index
                if index == 0 or not 0 <= resolved < len(vertices):
                    raise _invalid()
                face.append(resolved)
            faces.append(face)
        # Materials, textures, groups, normals, and comments carry no geometry here.
    if not vertices or not faces:
        raise _invalid()
    return np.asarray(vertices, dtype=np.float64), np.asarray(faces, dtype=np.int64)


def load_mesh(path: Path, *, format: str, units: str, limits: MeshLimits | None = None) -> MeshData:
    limits = limits or MeshLimits()
    with Path(path).open("rb") as stream:
        raw = stream.read(limits.max_bytes + 1)
    return load_mesh_bytes(raw, format=format, units=units, limits=limits)


def load_mesh_bytes(raw: bytes, *, format: str, units: str, limits: MeshLimits | None = None) -> MeshData:
    limits = limits or MeshLimits()
    scale = input_scale(format, units)
    if not raw:
        raise _invalid()
    if len(raw) > limits.max_bytes:
        raise MeshError("byte_limit", "Файл сетки превышает допустимый размер.")
    if format == "obj":
        vertices, faces = _obj(raw, limits)
    else:
        binary = (len(raw) >= 84 and (b"\0" in raw[:84] or not raw.lstrip().startswith(b"solid")
                  or len(raw) == 84 + 50 * struct.unpack_from("<I", raw, 80)[0]))
        vertices, faces = _binary_stl(raw, limits) if binary else _ascii_stl(raw, limits)
    if not np.isfinite(vertices).all() or np.max(np.abs(vertices)) > 1e9 / scale:
        raise _invalid()
    # Exact coincident vertices share topology; positions and faces are never smoothed/repaired.
    vertices, inverse = np.unique(vertices * scale, axis=0, return_inverse=True)
    faces = inverse[faces]
    used, inverse = np.unique(faces, return_inverse=True)
    vertices, faces = vertices[used], inverse.reshape((-1, 3))
    if len(vertices) > limits.max_vertices:
        raise MeshError("vertex_limit", "В сетке слишком много вершин.")
    triangles = vertices[faces]
    areas = np.linalg.norm(np.cross(triangles[:, 1]-triangles[:, 0],
                                    triangles[:, 2]-triangles[:, 0]), axis=1)
    if np.any(areas == 0) or not np.isfinite(areas).all():
        raise _invalid()
    edges = faces[:, [0, 1, 1, 2, 2, 0]].reshape((-1, 2))
    graph = coo_matrix((np.ones(len(edges), dtype=np.uint8), (edges[:, 0], edges[:, 1])),
                       shape=(len(vertices), len(vertices)))
    if connected_components(graph, directed=False, return_labels=False) != 1:
        raise MeshError("multiple_components", "В файле несколько отдельных компонентов. Выберите одну деталь.")
    vertices.setflags(write=False)
    faces.setflags(write=False)
    return MeshData(vertices, faces, hashlib.sha256(raw).hexdigest(), units, scale)
