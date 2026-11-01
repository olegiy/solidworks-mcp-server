"""Complete triangle coverage cannot be replaced by global area percentages."""

from dataclasses import asdict

import numpy as np
import pytest
import trimesh

from tests.test_mesh_analysis import mesh_data
from tests.test_mesh_compare import box, options
from tests.test_mesh_evidence import reference, snapshot


def v2(**overrides):
    return options(method="stratified_triangle_surface_v2", **overrides)


def test_v2_covers_every_triangle_despite_thousands_of_legacy_regions():
    from cadnext.mesh.compare import compare_surfaces

    geometry = trimesh.creation.icosphere(subdivisions=3)
    mesh = mesh_data(geometry.vertices, geometry.faces)
    result = compare_surfaces(mesh, mesh, options=v2(samples=32))
    assert result.status == "pass", result.reasons
    assert result.method == "stratified_triangle_surface_v2"
    for direction in ("reference_to_cad", "cad_to_reference"):
        coverage = result.coverage[direction]
        assert coverage["complete"]
        assert coverage["total_faces"] == coverage["checked_faces"] == len(mesh.faces)
        assert coverage["partitions"] <= 512
        records = [r for r in result.regions if r["direction"] == direction]
        assert sum(r["samples"] for r in records) == len(mesh.faces)


def test_spatial_partitions_are_deterministic_disjoint_and_exhaustive():
    from cadnext.mesh.compare import spatial_partitions

    centers = np.zeros((2401, 3))
    groups = spatial_partitions(centers)
    assert len(groups) <= 512
    assert sorted(np.concatenate(groups).tolist()) == list(range(2401))
    assert [g.tolist() for g in groups] == [g.tolist() for g in spatial_partitions(centers)]


def test_centroid_violation_is_not_hidden_by_random_samples(monkeypatch):
    from cadnext.mesh import compare

    a, b = box(), box(offset=(1, 0, 2.5))
    monkeypatch.setattr(compare, "_sample", lambda triangles, areas, count, rng: np.tile([0, 0, 0], (count, 1)))
    result = compare.compare_surfaces(a, b, options=v2())
    assert result.reference_to_cad["sampled_max_mm"] < .09
    assert result.status == "fail"
    assert result.violating_regions


def test_required_legacy_region_gets_additional_256_points():
    from cadnext.mesh.analysis import inspect_mesh
    from cadnext.mesh.compare import compare_surfaces

    mesh = box()
    required = inspect_mesh(mesh).regions[0]["region_id"]
    result = compare_surfaces(mesh, mesh, options=v2(required_region_ids=[required]))
    assert result.status == "pass"
    assert result.required_regions[0]["region_id"] == required
    assert result.required_regions[0]["samples"] == 256
    assert result.required_regions[0]["complete"]
    missing = compare_surfaces(mesh, mesh, options=v2(required_region_ids=["missing"]))
    assert missing.status == "unknown"


@pytest.mark.parametrize("damage", ["coverage", "required", "method"])
def test_independent_gate_rejects_incomplete_or_missing_authoritative_evidence(damage):
    from cadnext.mesh.compare import compare_surfaces
    from cadnext.mesh.evidence import validate_mesh_evidence

    record = asdict(compare_surfaces(box(), box(), options=v2()))
    ref = {**reference(), "required_region_ids": ["bore"] if damage == "required" else []}
    if damage == "coverage":
        record["coverage"]["cad_to_reference"]["checked_faces"] -= 1
    if damage == "method":
        record["method"] = "unsupported"
    assert validate_mesh_evidence(record, ref, snapshot()) == "unknown"
