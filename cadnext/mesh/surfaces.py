"""Connected analytic surface discovery and conservative engineering measurements."""

import hashlib
import json
import time
from dataclasses import asdict

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from trimesh.triangles import closest_point

from cadnext.mesh.analysis import adjacency, face_geometry
from cadnext.mesh.contracts import FitResult, MeshError, validate_tolerance
from cadnext.mesh.primitives import KINDS, basis, fit_primitive, span

VERSION = "analytic-surfaces-1"


def surface_id(mesh, indices, tolerance):
    key = [mesh.sha256, getattr(mesh, "context_id", ""), tolerance, VERSION]
    digest = hashlib.sha256(json.dumps(key).encode()+mesh.vertices.tobytes()+
                            np.asarray(indices, dtype="<i8").tobytes()).hexdigest()
    return "surface:"+digest


def _vertex_normals(mesh, indices):
    _, normals, areas = face_geometry(mesh)
    ids, inverse = np.unique(mesh.faces[indices], return_inverse=True)
    summed = np.zeros((len(ids), 3))
    np.add.at(summed, inverse.ravel(), np.repeat(normals[indices]*areas[indices, None], 3, axis=0))
    summed /= np.maximum(np.linalg.norm(summed, axis=1)[:, None], 1e-30)
    return ids, summed


def _radial_min(triangles):
    # Exact distance to the convex triangle, also valid for degenerate projections.
    a, b, c = np.moveaxis(triangles, 1, 0)
    ab, ac = b-a, c-a
    aa, bb, cc = np.sum(ab*ab, axis=1), np.sum(ab*ac, axis=1), np.sum(ac*ac, axis=1)
    d, e = -np.sum(a*ab, axis=1), -np.sum(a*ac, axis=1)
    determinant = aa*cc-bb*bb
    safe = np.maximum(determinant, 1e-30)
    u, v = (d*cc-e*bb)/safe, (e*aa-d*bb)/safe
    inside = (determinant > 1e-25) & (u >= 0) & (v >= 0) & (u+v <= 1)
    minimum = np.full(len(a), np.inf)
    minimum[inside] = np.linalg.norm((a+u[:, None]*ab+v[:, None]*ac)[inside], axis=1)
    for x, y in ((a, b), (b, c), (c, a)):
        edge = y-x
        t = np.clip(-np.sum(x*edge, axis=1)/np.maximum(np.sum(edge**2, axis=1), 1e-30), 0, 1)
        minimum = np.minimum(minimum, np.linalg.norm(x+t[:, None]*edge, axis=1))
    return minimum


def facet_bound(model, triangles, tolerance):
    kind = model.kind
    center, axis, sizes = model.decode(model.p)
    center = model.origin+center*model.scale
    residual = np.abs(model.distance(triangles.reshape(-1, 3))).reshape(-1, 3).max(axis=1)
    if kind == "plane":
        return float(residual.max()), True
    if kind in {"sphere", "cylinder"}:
        delta = triangles-center
        if kind == "cylinder":
            delta -= (delta @ axis)[:, :, None]*axis
        low = _radial_min(delta)
        high = np.linalg.norm(delta, axis=2).max(axis=1)
        r = abs(sizes[0])*model.scale
        return float(np.maximum(np.abs(low-r), np.abs(high-r)).max()), True
    # Linear interpolation error <= sup||Hessian|| * diameter^2 / 6.
    pending = triangles
    bound = 0.
    for depth in range(9):
        delta = pending-center
        z = delta @ axis
        radial = delta-z[:, :, None]*axis
        rho_min = _radial_min(radial)
        diameter = np.max(np.linalg.norm(pending-np.roll(pending, 1, axis=1), axis=2), axis=1)
        if kind == "cone":
            curvature = 1/np.maximum(rho_min, 1e-30)
            valid = rho_min > 1e-12
        else:
            centroid = pending.mean(axis=1)-center
            height = centroid @ axis
            rho = np.linalg.norm(centroid-height[:, None]*axis, axis=1)
            tube_min = np.hypot(rho-sizes[0]*model.scale, height)-diameter
            curvature = 1/np.maximum(tube_min, 1e-30)+1/np.maximum(rho_min, 1e-30)
            valid = (tube_min > 1e-12) & (rho_min > 1e-12)
        residual = np.abs(model.distance(pending.reshape(-1, 3))).reshape(-1, 3).max(axis=1)
        upper = residual+curvature*diameter**2/6
        done = valid & (upper <= tolerance/4)
        if np.any(done):
            bound = max(bound, float(upper[done].max()))
        pending = pending[~done]
        if not len(pending):
            return bound, True
        if np.any(residual[~done] > tolerance/4) or len(pending)*4 > 200000 or depth == 8:
            return max(bound, float(upper[~done].max())), False
        a, b, c = np.moveaxis(pending, 1, 0)
        ab, bc, ca = (a+b)/2, (b+c)/2, (c+a)/2
        pending = np.concatenate([np.stack(t, axis=1) for t in
                                  ((a, ab, ca), (ab, b, bc), (ca, bc, c), (ab, bc, ca))])
    return bound, False


def measure_surface(mesh, indices, kind, tolerance, *, trim=True):
    tolerance = validate_tolerance(tolerance)
    indices = np.sort(np.asarray(indices, dtype=int))
    vertices, normals = _vertex_normals(mesh, indices)
    points = mesh.vertices[vertices]
    model = fit_primitive(points, normals, kind, tolerance)
    before = np.abs(model.distance(points))
    rejected = before > tolerance/4
    if not trim or rejected.sum() > int(len(points)*.02):
        rejected[:] = False
    if np.any(rejected):
        model = fit_primitive(points[~rejected], normals[~rejected], kind, tolerance)
    residual = np.abs(model.distance(points))
    omitted = np.isin(mesh.faces[indices], vertices[rejected]).any(axis=1)
    used = indices[~omitted]
    triangles, _, areas = face_geometry(mesh)
    degeneracies = []
    fraction = float(areas[indices[omitted]].sum()/areas[indices].sum())
    if fraction > .05:
        degeneracies.append("excluded_area")
    if not len(used):
        used = indices
        degeneracies.append("empty_support")
    pairs = adjacency(mesh)[0]
    selected = np.zeros(len(mesh.faces), bool)
    selected[used] = True
    pairs = pairs[selected[pairs].all(axis=1)]
    remap = np.full(len(mesh.faces), -1)
    remap[used] = np.arange(len(used))
    graph = coo_matrix((np.ones(len(pairs)), (remap[pairs[:, 0]], remap[pairs[:, 1]])), shape=(len(used), len(used)))
    if connected_components(graph, directed=False)[0] != 1:
        degeneracies.append("disconnected_support")
    maximum = float(residual[~rejected].max())
    if maximum > tolerance/4:
        degeneracies.append("excessive_residual")
    bound, confirmed = facet_bound(model, triangles[used], tolerance)
    if not confirmed or bound > tolerance/4:
        degeneracies.append("facet_error")
    if not model.converged:
        degeneracies.append("nonconverged")
    if model.condition > 1e8:
        degeneracies.append("ill_conditioned")
    center, axis, sizes = model.decode(model.p)
    delta = (points[~rejected]-model.origin)/model.scale-center
    coverage = {"area_fraction": float(areas[indices].sum()/areas.sum()), "facet_bound_mm": bound,
                "bound_confirmed": confirmed, "condition_number": model.condition,
                "excluded_points": int(rejected.sum()), "excluded_faces": int(omitted.sum()),
                "excluded_area_fraction": fraction, "max_before_exclusion_mm": float(before.max())}
    parameters = model.parameters()
    if kind in {"cylinder", "cone", "torus"}:
        xy = delta @ basis(axis)
        z = delta @ axis
        coverage["angle_degrees"] = span(xy)
        minimum = {"cylinder": 30, "cone": 60, "torus": 90}[kind]
        if coverage["angle_degrees"] < minimum:
            degeneracies.append("insufficient_coverage")
        if kind == "cone":
            change = np.ptp(z)*abs(sizes[0])*model.scale
            coverage.update(axial_span_mm=float(np.ptp(z)*model.scale), radius_change_mm=float(change))
            if min(np.ptp(z)*model.scale, change) <= 2*tolerance:
                degeneracies.append("ambiguous_cylinder")
            if np.min(z) < 0:
                degeneracies.append("opposite_cone_nappe")
        elif kind == "cylinder":
            parameters["axial_span_mm"] = float(np.ptp(z)*model.scale)
            if parameters["axial_span_mm"] <= 2*tolerance:
                degeneracies.append("insufficient_axial_span")
        else:
            coverage["minor_angle_degrees"] = span(np.column_stack((np.linalg.norm(xy, axis=1)-sizes[0], z)))
            if coverage["minor_angle_degrees"] < 90:
                degeneracies.append("insufficient_coverage")
            if not sizes[0] > sizes[1] > 0:
                degeneracies.append("non_ring_torus")
    elif kind == "sphere":
        directions = delta/np.maximum(np.linalg.norm(delta, axis=1)[:, None], 1e-30)
        eigenvalues = np.linalg.eigvalsh(np.cov(directions.T))
        coverage["direction_spread"] = eigenvalues.tolist()
        # A 60-degree patch must span at least a unit chord in two independent directions.
        _, _, axes = np.linalg.svd(directions-directions.mean(axis=0), full_matrices=False)
        ranges = np.ptp(directions @ axes.T, axis=0)
        if min(ranges[:2]) < 1:
            degeneracies.append("insufficient_coverage")
    result = FitResult(kind, surface_id(mesh, indices, tolerance), parameters, int((~rejected).sum()),
                       float(np.sqrt(np.mean(residual[~rejected]**2))), maximum, coverage,
                       tuple(sorted(set(degeneracies))), not degeneracies)
    return result


def _patches(mesh):
    _, normals, _ = face_geometry(mesh)
    pairs = adjacency(mesh)[0]
    angles = np.arccos(np.clip(np.sum(normals[pairs[:, 0]]*normals[pairs[:, 1]], axis=1), -1, 1))
    curved = angles[(angles > np.deg2rad(.05)) & (angles < np.deg2rad(25))]
    threshold = min(np.deg2rad(25), max(np.deg2rad(3), 2*float(np.median(curved)))) if len(curved) else np.deg2rad(3)
    smooth = pairs[angles < threshold]
    graph = coo_matrix((np.ones(len(smooth)), (smooth[:, 0], smooth[:, 1])), shape=(len(mesh.faces), len(mesh.faces)))
    _, labels = connected_components(graph, directed=False)
    order = np.argsort(labels, kind="stable")
    return np.split(order, np.flatnonzero(np.diff(labels[order]))+1)


def _grow(mesh, indices, kind, tolerance, available):
    triangles, normals, _ = face_geometry(mesh)
    pairs = adjacency(mesh)[0]
    selected = np.zeros(len(triangles), bool)
    selected[indices] = True
    for _ in range(6):
        vertices, vertex_normals = _vertex_normals(mesh, np.flatnonzero(selected))
        model = fit_primitive(mesh.vertices[vertices], vertex_normals, kind, tolerance)
        if np.quantile(np.abs(model.distance(mesh.vertices[vertices])), .98) > tolerance/4:
            break
        boundary = pairs[selected[pairs[:, 0]] != selected[pairs[:, 1]]].ravel()
        candidates = np.unique(boundary[~selected[boundary] & available[boundary]])
        if not len(candidates):
            break
        candidate_triangles = triangles[candidates]
        distance = np.abs(model.distance(candidate_triangles.reshape(-1, 3))).reshape(-1, 3).max(axis=1)
        centers = candidate_triangles.mean(axis=1)
        step = max(tolerance*1e-3, 1e-7)
        gradient = np.column_stack([(model.distance(centers+np.eye(3)[i]*step)-
                                     model.distance(centers-np.eye(3)[i]*step))/(2*step) for i in range(3)])
        agreement = np.abs(np.einsum("ij,ij->i", gradient, normals[candidates]))
        accepted = candidates[(distance <= tolerance/4) & (agreement >= np.cos(np.deg2rad(15)))]
        if not len(accepted):
            break
        selected[accepted] = True
    return np.flatnonzero(selected)


def surface_catalog(mesh, tolerance, *, budget_s=25):
    tolerance = validate_tolerance(tolerance)
    started = time.monotonic()
    output, assigned = [], set()
    complete = True
    triangles, _, areas = face_geometry(mesh)
    patches = sorted(_patches(mesh), key=lambda ids: (-float(areas[ids].sum()), int(ids[0])))
    for indices in patches:
        if time.monotonic()-started >= budget_s:
            complete = False
            break
        indices = np.asarray([i for i in indices if i not in assigned], dtype=int)
        if len(indices) < 2:
            continue
        available = np.ones(len(mesh.faces), bool)
        available[list(assigned)] = False
        hypotheses = [(_grow(mesh, indices, kind, tolerance, available), kind) for kind in KINDS]
        candidates = [measure_surface(mesh, ids, kind, tolerance) for ids, kind in hypotheses]
        acceptable = [r for r in candidates if r.max_residual_mm <= tolerance/4 and
                      "ill_conditioned" not in r.degeneracies and "non_ring_torus" not in r.degeneracies]
        if not acceptable:
            continue
        # Prefer the least complex fitting family; retain genuinely competing hypotheses.
        best = min(acceptable, key=lambda r: (not r.usable, KINDS.index(r.kind)))
        indices = hypotheses[KINDS.index(best.kind)][0]
        alternatives = [r.kind for r in acceptable if r.kind != best.kind and r.usable and
                        best.usable and r.rms_mm <= max(best.rms_mm*2, tolerance/100)]
        record = {**asdict(best), "surface_id": best.region_id, "area_mm2": float(areas[indices].sum()),
                  "bounds_mm": [triangles[indices].min(axis=(0, 1)).tolist(), triangles[indices].max(axis=(0, 1)).tolist()],
                  "source_face_ids": indices.tolist(), "alternatives": alternatives}
        if alternatives:
            record.update(usable=False, degeneracies=[*record["degeneracies"], "ambiguous_models"])
        output.append(record)
        assigned.update(indices.tolist())
    remaining = np.array(sorted(set(range(len(mesh.faces)))-assigned), dtype=int)
    return {"surfaces": output, "complete": complete, "algorithm": VERSION,
            "unrecognized_triangles": len(remaining), "unrecognized_area_mm2": float(areas[remaining].sum())}


def select_surface(mesh, region, kind, tolerance, *, catalog=None):
    catalog = surface_catalog(mesh, tolerance) if catalog is None else catalog
    if "surface_id" in region:
        records = [r for r in catalog["surfaces"] if r["surface_id"] == region["surface_id"]]
        if not records:
            raise MeshError("unknown_surface", "Поверхность отсутствует в текущем контексте эталона.")
        return np.asarray(records[0]["source_face_ids"])
    seed = np.asarray(region["seed_mm"], dtype=float)
    if seed.shape != (3,) or not np.isfinite(seed).all():
        raise MeshError("invalid_region", "Некорректная начальная точка.")
    triangles, normals, _ = face_geometry(mesh)
    near = closest_point(triangles, np.broadcast_to(seed, (len(triangles), 3)))
    distances = np.linalg.norm(near-seed, axis=1)
    face = int(np.argmin(distances))
    if distances[face] > tolerance:
        raise MeshError("seed_distance", f"Расстояние до сетки {distances[face]:.9g} мм; ближайшая точка {near[face].tolist()}.")
    records = [r for r in catalog["surfaces"] if face in r["source_face_ids"]]
    indices = np.asarray(records[0]["source_face_ids"]) if records else next(ids for ids in _patches(mesh) if face in ids)
    allowed = np.ones(len(mesh.faces), dtype=bool)
    if "bounds_mm" in region:
        bounds = np.asarray(region["bounds_mm"], float)
        if bounds.shape != (2, 3) or not np.isfinite(bounds).all() or np.any(bounds[0] > bounds[1]):
            raise MeshError("invalid_region", "Некорректный ограничивающий бокс.")
        allowed &= np.all((triangles >= bounds[0]) & (triangles <= bounds[1]), axis=(1, 2))
    if "axis" in region:
        axis = np.asarray(region["axis"], float)
        if axis.shape != (3,) or not np.isfinite(axis).all() or np.linalg.norm(axis) < 1e-12:
            raise MeshError("invalid_region", "Некорректное направление оси.")
        axis /= np.linalg.norm(axis)
        if records and "axis" in records[0]["parameters"]:
            if abs(np.dot(axis, records[0]["parameters"]["axis"])) < np.cos(np.deg2rad(.5)):
                raise MeshError("axis_mismatch", "Ось поверхности не совпадает с ограничением поиска.")
    side = region.get("side")
    if side and records:
        params = records[0]["parameters"]
        center = np.asarray(params.get("center_mm", params.get("origin_mm", seed)))
        radial = triangles.mean(axis=1)-center
        if "axis" in params:
            axis = np.asarray(params["axis"])
            radial -= (radial @ axis)[:, None]*axis
        sign = np.einsum("ij,ij->i", normals, radial)
        allowed &= sign > 0 if side == "outer" else sign < 0
    if not allowed[face]:
        raise MeshError("empty_region", "Начальная точка исключена ограничениями поиска.")
    mask = np.zeros(len(mesh.faces), bool)
    mask[indices] = True
    mask &= allowed
    pairs = adjacency(mesh)[0]
    pairs = pairs[mask[pairs].all(axis=1)]
    graph = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(len(mask), len(mask)))
    _, labels = connected_components(graph, directed=False)
    return np.flatnonzero(mask & (labels == labels[face]))
