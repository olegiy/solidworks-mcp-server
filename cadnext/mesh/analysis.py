"""Measured regions and geometric least-squares fits, without geometry repair."""

import hashlib
from dataclasses import replace

import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from cadnext.mesh.contracts import FitResult, MeshError, MeshSummary, validate_tolerance


def face_geometry(mesh):
    if hasattr(mesh, "geometry"):
        return mesh.geometry
    triangles = mesh.vertices[mesh.faces]
    cross = np.cross(triangles[:, 1]-triangles[:, 0], triangles[:, 2]-triangles[:, 0])
    lengths = np.linalg.norm(cross, axis=1)
    if np.any(lengths == 0) or not np.isfinite(lengths).all():
        raise MeshError("degenerate_mesh", "В сетке есть вырожденные треугольники.")
    return triangles, cross / lengths[:, None], lengths / 2


def adjacency(mesh):
    if hasattr(mesh, "topology"):
        return mesh.topology
    edges = mesh.faces[:, [[0, 1], [1, 2], [2, 0]]].reshape((-1, 2))
    keys = np.sort(edges, axis=1)
    _, inverse, counts = np.unique(keys, axis=0, return_inverse=True, return_counts=True)
    order = np.argsort(inverse, kind="stable")
    starts = np.cumsum(counts)-counts
    shared = starts[counts == 2]
    pairs = np.column_stack((order[shared]//3, order[shared+1]//3))
    # Paired edges must traverse in opposite directions for consistent winding.
    consistent = bool(np.all(edges[order[shared]] == edges[order[shared+1]][:, ::-1]))
    return pairs, counts, consistent


def _labels(count, pairs):
    graph = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(count, count))
    return connected_components(graph, directed=False)


def _region_id(mesh, indices):
    digest = hashlib.sha256(np.asarray(indices, dtype="<i8").tobytes()).hexdigest()[:16]
    return f"{mesh.sha256[:16]}:{digest}"


def _regions(mesh, normals, pairs):
    coplanar = np.einsum("ij,ij->i", normals[pairs[:, 0]], normals[pairs[:, 1]]) > np.cos(np.deg2rad(1))
    _, labels = _labels(len(mesh.faces), pairs[coplanar])
    # Stable ordering independent of sparse graph traversal.
    order = np.argsort(labels, kind="stable")
    return np.split(order, np.flatnonzero(np.diff(labels[order]))+1)


def inspect_mesh(mesh) -> MeshSummary:
    triangles, normals, areas = face_geometry(mesh)
    pairs, counts, consistent = adjacency(mesh)
    components, _ = _labels(len(mesh.faces), pairs)
    watertight = bool(np.all(counts == 2))
    volume = float(np.einsum("ij,ij->i", triangles[:, 0],
                             np.cross(triangles[:, 1], triangles[:, 2])).sum()/6)
    eigenvalues = np.linalg.eigvalsh(np.cov(mesh.vertices.T))
    ambiguous = bool(np.any(np.diff(eigenvalues) < max(eigenvalues[-1], 1e-12)*.03))
    regions = []
    for indices in _regions(mesh, normals, pairs):
        points = mesh.vertices[np.unique(mesh.faces[indices])]
        normal = np.average(normals[indices], axis=0, weights=areas[indices])
        regions.append({"region_id": _region_id(mesh, indices), "triangles": len(indices),
                        "area_mm2": float(areas[indices].sum()), "normal": normal.tolist(),
                        "bounds_mm": [points.min(axis=0).tolist(), points.max(axis=0).tolist()]})
    return MeshSummary(mesh.sha256, [mesh.vertices.min(axis=0).tolist(), mesh.vertices.max(axis=0).tolist()],
                       len(mesh.vertices), len(mesh.faces), int(components), int(np.sum(counts == 1)),
                       int(np.sum(counts > 2)), watertight, consistent,
                       abs(volume) if watertight and consistent else None, float(areas.sum()),
                       ambiguous, None, regions)


def select_region(mesh, region):
    triangles, normals, areas = face_geometry(mesh)
    pairs, _, _ = adjacency(mesh)
    if region == "all":
        indices = np.arange(len(mesh.faces))
    elif isinstance(region, str):
        matches = [ids for ids in _regions(mesh, normals, pairs) if _region_id(mesh, ids) == region]
        if not matches:
            raise MeshError("unknown_region", "Область отсутствует в этой версии эталона.")
        indices = matches[0]
    elif isinstance(region, dict):
        try:
            bounds = np.asarray(region["bounds_mm"], dtype=float)
            if bounds.shape != (2, 3) or not np.isfinite(bounds).all() or np.any(bounds[0] > bounds[1]):
                raise ValueError
            mask = np.all((triangles >= bounds[0]) & (triangles <= bounds[1]), axis=(1, 2))
            if "normal" in region:
                normal = np.asarray(region["normal"], dtype=float)
                angle = float(region.get("angle_degrees", 5))
                if (normal.shape != (3,) or not np.isfinite(normal).all() or
                        np.linalg.norm(normal) < 1e-12 or not 0 < angle <= 90):
                    raise ValueError
                mask &= (normals @ (normal/np.linalg.norm(normal))) >= np.cos(np.deg2rad(angle))
            indices = np.flatnonzero(mask)
        except (KeyError, TypeError, ValueError):
            raise MeshError("invalid_region", "Некорректный бокс или фильтр нормалей области.") from None
    else:
        raise MeshError("invalid_region", "Укажите ID или ограничивающий бокс области.")
    if not len(indices):
        raise MeshError("empty_region", "В выбранной области нет целых треугольников.")
    mask = np.zeros(len(mesh.faces), dtype=bool)
    mask[indices] = True
    selected_pairs = pairs[mask[pairs].all(axis=1)]
    remap = np.full(len(mesh.faces), -1, dtype=int)
    remap[indices] = np.arange(len(indices))
    count, _ = _labels(len(indices), remap[selected_pairs])
    if count != 1:
        raise MeshError("disconnected_region", "Для подгонки нужна одна связная поверхность.")
    points = mesh.vertices[np.unique(mesh.faces[indices])]
    return points, normals[indices], float(areas[indices].sum()/areas.sum()), _region_id(mesh, indices)


def _canonical(vector):
    return vector if vector[np.argmax(np.abs(vector))] >= 0 else -vector


def _angular_span(xy):
    angles = np.sort(np.mod(np.arctan2(xy[:, 1], xy[:, 0]), 2*np.pi))
    return float(np.rad2deg(2*np.pi-np.diff(np.r_[angles, angles[0]+2*np.pi]).max()))


def _circle(xy, tolerance):
    centered = xy-xy.mean(axis=0)
    a = np.column_stack((2*centered, np.ones(len(xy))))
    initial, *_ = np.linalg.lstsq(a, (centered**2).sum(axis=1), rcond=None)
    center = initial[:2]+xy.mean(axis=0)
    radius = np.linalg.norm(xy-center, axis=1).mean()
    solution = least_squares(lambda p: np.linalg.norm(xy-p[:2], axis=1)-p[2],
                             [*center, radius], loss="soft_l1", f_scale=tolerance/4, max_nfev=200)
    return solution.x[:2], abs(float(solution.x[2])), solution.success


def fit_region(mesh, region, kind, *, tolerance_mm, catalog=None) -> FitResult:
    tolerance = validate_tolerance(tolerance_mm)
    if kind in {"cylinder", "cone", "sphere", "torus"} or (isinstance(region, dict) and
                                               ("seed_mm" in region or "surface_id" in region)):
        from cadnext.mesh.primitives import KINDS
        from cadnext.mesh.surfaces import measure_surface, select_surface

        if kind not in KINDS:
            raise MeshError("invalid_fit", "Для выбора поверхности нужен аналитический примитив.")
        if isinstance(region, dict) and ("seed_mm" in region or "surface_id" in region):
            indices = select_surface(mesh, region, kind, tolerance, catalog=catalog)
            return measure_surface(mesh, indices, kind, tolerance, compete=True)
        # Preserve strict whole-triangle selection for the legacy forms.
        _, _, _, selected_id = select_region(mesh, region)
        triangles, normals, _ = face_geometry(mesh)
        if region == "all":
            indices = np.arange(len(mesh.faces))
        elif isinstance(region, str):
            indices = next(ids for ids in _regions(mesh, normals, adjacency(mesh)[0])
                           if _region_id(mesh, ids) == selected_id)
        else:
            bounds = np.asarray(region["bounds_mm"])
            mask = np.all((triangles >= bounds[0]) & (triangles <= bounds[1]), axis=(1, 2))
            if "normal" in region:
                normal = np.asarray(region["normal"])
                mask &= normals @ (normal/np.linalg.norm(normal)) >= np.cos(np.deg2rad(region.get("angle_degrees", 5)))
            indices = np.flatnonzero(mask)
        return replace(measure_surface(mesh, indices, kind, tolerance, trim=False, compete=True), region_id=selected_id)
    if kind not in {"plane", "circle"}:
        raise MeshError("invalid_fit", "Выберите плоскость, окружность, цилиндр, конус, сферу или тор.")
    points, normals, area_fraction, region_id = select_region(mesh, region)
    origin = points.mean(axis=0)
    centered = points-origin
    _, singular, axes = np.linalg.svd(centered, full_matrices=False)
    degeneracies = []
    if len(points) < 3 or singular[1] <= max(singular[0], 1e-12)*1e-6:
        degeneracies.append("collinear")
    parameters = {}
    coverage = {"area_fraction": area_fraction}
    if kind == "plane":
        normal = _canonical(axes[-1])
        residuals = centered @ normal
        parameters = {"origin_mm": origin.tolist(), "normal": normal.tolist()}
    elif kind == "circle":
        xy = centered @ axes[:2].T
        center, radius, converged = _circle(xy, tolerance)
        residuals = np.hypot(np.linalg.norm(xy-center, axis=1)-radius, centered @ axes[-1])
        parameters = {"center_mm": (origin+center @ axes[:2]).tolist(), "radius_mm": radius,
                      "normal": _canonical(axes[-1]).tolist()}
        coverage["angle_degrees"] = _angular_span(xy-center)
        if not converged:
            degeneracies.append("nonconverged")
    if coverage.get("angle_degrees", 360) < 30:
        degeneracies.append("insufficient_coverage")
    maximum = float(np.abs(residuals).max())
    if maximum > tolerance/4:
        degeneracies.append("excessive_residual")
    return FitResult(kind, region_id, parameters, len(points), float(np.sqrt(np.mean(residuals**2))),
                     maximum, coverage, tuple(degeneracies), not degeneracies)
