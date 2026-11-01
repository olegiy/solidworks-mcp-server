"""Deterministic sampled surface distances; never a certified Hausdorff maximum."""

import math

import numpy as np
import trimesh

from cadnext.mesh.analysis import _region_id, _regions, adjacency, face_geometry, inspect_mesh
from cadnext.mesh.contracts import MeshComparison, MeshError, validate_tolerance


def _sample(triangles, areas, count, rng):
    indices = rng.choice(len(triangles), size=count, p=areas/areas.sum())
    uv = rng.random((count, 2))
    uv[uv.sum(axis=1) > 1] = 1-uv[uv.sum(axis=1) > 1]
    selected = triangles[indices]
    return selected[:, 0]+uv[:, :1]*(selected[:, 1]-selected[:, 0])+uv[:, 1:]*(selected[:, 2]-selected[:, 0])


def _distances(query, points):
    # Broad triangles can produce many candidates. Keep each query batch bounded.
    return np.concatenate([query.on_surface(points[i:i+256])[1] for i in range(0, len(points), 256)])


def _statistics(distances, threshold):
    return {"samples": len(distances), "rms_mm": float(np.sqrt(np.mean(distances**2))),
            "p95_mm": float(np.quantile(distances, .95)), "sampled_max_mm": float(distances.max()),
            "fraction_within_tolerance": float(np.mean(distances <= threshold))}


def compare_surfaces(reference, cad, *, options) -> MeshComparison:
    tolerance = validate_tolerance(options["tolerance_mm"])
    chord = options.get("chord_tolerance_mm", 0)
    if type(chord) not in (int, float) or not math.isfinite(chord) or chord < 0:
        raise MeshError("invalid_precision", "Некорректная погрешность CAD-триангуляции.")
    samples, region_samples, seed = (options.get("samples", 50000), options.get("region_samples", 256),
                                      options.get("seed", 20261101))
    if (type(samples) is not int or not 1 <= samples <= 50000 or type(region_samples) is not int
            or not 1 <= region_samples <= 2048 or type(seed) is not int or not 0 <= seed < 2**32):
        raise MeshError("invalid_sampling", "Некорректные параметры выборки поверхностей.")
    reasons = []
    for label, mesh in (("reference", reference), ("cad", cad)):
        summary = inspect_mesh(mesh)
        if not summary.watertight or not summary.winding_consistent:
            reasons.append(f"{label}_not_closed_or_oriented")
    if options.get("precision_known") is not True or chord > tolerance/4:
        reasons.append("unknown_or_insufficient_cad_precision")
    if options.get("cad_checks_passed") is not True:
        reasons.append("cad_checks_unconfirmed")
    threshold = max(0, tolerance-chord)
    rng = np.random.default_rng(seed)
    queries = {id(mesh): trimesh.proximity.ProximityQuery(
        trimesh.Trimesh(vertices=mesh.vertices, faces=mesh.faces, process=False)) for mesh in (reference, cad)}
    stats, regions, violations, known_ids = [], [], [], set()
    for direction, source, target in (("reference_to_cad", reference, cad), ("cad_to_reference", cad, reference)):
        triangles, normals, areas = face_geometry(source)
        query = queries[id(target)]
        distances = _distances(query, _sample(triangles, areas, samples, rng))
        stats.append(_statistics(distances, threshold))
        if stats[-1]["sampled_max_mm"] > threshold:
            violations.append(direction)
        pairs, _, _ = adjacency(source)
        groups = _regions(source, normals, pairs)
        if len(groups) > 512:
            reasons.append("region_limit")
        for indices in groups[:512]:
            region_id = _region_id(source, indices)
            if source is reference:
                known_ids.add(region_id)
            region_stats = _statistics(_distances(query, _sample(triangles[indices], areas[indices],
                                                                region_samples, rng)), threshold)
            regions.append({"region_id": region_id, "direction": direction, **region_stats})
            if region_stats["sampled_max_mm"] > threshold:
                violations.append(f"{direction}:{region_id}")
    if not set(options.get("required_region_ids", [])) <= known_ids:
        reasons.append("unconfirmed_regions")
    status = "unknown" if reasons else "fail" if violations else "pass"
    return MeshComparison(reference.sha256, cad.sha256, options["frame_id"], options["document_identity"],
                          options["revision"], options["run_id"], tolerance, chord,
                          "area_weighted_triangle_surface_v1", seed, *stats, regions, violations,
                          status, tuple(sorted(set(reasons))))
