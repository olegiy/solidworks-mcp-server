"""Two directions and mandatory regions catch missing and extra geometry."""

import numpy as np
import pytest
import trimesh

from tests.mesh_fixtures import make_plate_with_hole
from tests.test_mesh_analysis import mesh_data


def box(extents=(40, 30, 5), offset=(0, 0, 2.5)):
    geometry = trimesh.creation.box(extents=extents)
    return mesh_data(geometry.vertices+offset, geometry.faces)


def options(**overrides):
    return {"tolerance_mm": .1, "chord_tolerance_mm": .01, "precision_known": True,
            "method": "area_weighted_triangle_surface_v1",
            "samples": 1000, "region_samples": 128, "seed": 42,
            "frame_id": "frame", "document_identity": "doc", "revision": 7, "run_id": "run",
            "cad_checks_passed": True, **overrides}


def test_identical_surfaces_with_different_triangulations_pass_deterministically():
    from cadnext.mesh.compare import compare_surfaces

    original = box()
    v = original.vertices.tolist()
    f = []
    for triangle in original.faces:
        center = len(v)
        v.append(original.vertices[triangle].mean(axis=0).tolist())
        a, b, c = triangle
        f.extend([(a, b, center), (b, c, center), (c, a, center)])
    other = mesh_data(v, f)
    result = compare_surfaces(original, other, options=options())
    assert result.status == "pass"
    assert result.reference_to_cad["sampled_max_mm"] < 1e-10
    assert result.cad_to_reference["sampled_max_mm"] < 1e-10
    assert result == compare_surfaces(original, other, options=options())
    assert result.method == "area_weighted_triangle_surface_v1"


@pytest.mark.parametrize("candidate", [box(offset=(1, 0, 2.5)), box(extents=(50, 30, 5)),
                                      box(extents=(40, 30, 4))])
def test_translation_extra_surface_and_missing_wall_fail(candidate):
    from cadnext.mesh.compare import compare_surfaces

    result = compare_surfaces(box(), candidate, options=options())
    assert result.status == "fail" and result.violating_regions


def test_missing_small_hole_fails_region_even_when_global_fraction_exceeds_99_percent():
    from cadnext.mesh.compare import compare_surfaces

    fixture = make_plate_with_hole()
    vertices = fixture.vertices.copy()
    bore = np.linalg.norm(vertices[:, :2], axis=1) < 5.1
    vertices[bore, :2] *= .02
    reference = mesh_data(vertices, fixture.faces)
    result = compare_surfaces(reference, box(), options=options())
    assert result.status == "fail"
    assert result.reference_to_cad["fraction_within_tolerance"] > .99
    assert result.violating_regions


@pytest.mark.parametrize("overrides", [
    {"precision_known": False}, {"chord_tolerance_mm": .03}, {"cad_checks_passed": False},
])
def test_missing_precision_or_cad_checks_never_pass(overrides):
    from cadnext.mesh.compare import compare_surfaces

    result = compare_surfaces(box(), box(), options=options(**overrides))
    assert result.status == "unknown" and result.reasons


def test_open_reference_and_incomplete_required_regions_remain_unknown():
    from cadnext.mesh.compare import compare_surfaces

    original = box()
    opened = mesh_data(original.vertices, original.faces[:-1])
    assert compare_surfaces(opened, original, options=options()).status == "unknown"
    result = compare_surfaces(original, original, options=options(required_region_ids=["missing"]))
    assert result.status == "unknown" and "unconfirmed_regions" in result.reasons
