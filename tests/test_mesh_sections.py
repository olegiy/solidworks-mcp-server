"""Sections retain holes, source faces, orientation and genuine open ends."""

import numpy as np
import pytest

from tests.mesh_fixtures import make_plate_with_hole, make_stepped_bushing
from tests.test_mesh_analysis import mesh_data


def frame(z=2.5):
    from cadnext.mesh.contracts import PlaneFrame

    return PlaneFrame((0, 0, z), (1, 0, 0), (0, 1, 0), (0, 0, 1))


def plate():
    f = make_plate_with_hole()
    return mesh_data(f.vertices, f.faces)


@pytest.mark.parametrize("z", [0, 2.5, 5])
def test_plate_section_keeps_rectangular_outside_and_round_hole(z):
    from cadnext.mesh.sections import section_mesh

    result = section_mesh(plate(), frame(z), tolerance_mm=.1)
    assert result.usable_for_sketch
    assert result.sha256 == plate().sha256
    assert len(result.chains) == 2
    outer = next(c for c in result.chains if c["role"] == "outer")
    hole = next(c for c in result.chains if c["role"] == "hole")
    assert outer["signed_area_mm2"] == pytest.approx(1200)
    assert hole["signed_area_mm2"] == pytest.approx(-25*np.pi, rel=.002)
    assert len(outer["segments"]) == 4
    assert all(s["kind"] == "line" for s in outer["segments"])
    assert len(hole["segments"]) == 2
    assert all(s["kind"] == "arc" for s in hole["segments"])
    assert all(s["radius_mm"] == pytest.approx(5) for s in hole["segments"])
    for chain in result.chains:
        assert chain["closed"]
        for i, segment in enumerate(chain["segments"]):
            following = chain["segments"][(i+1) % len(chain["segments"])]
            assert np.allclose(segment["end_mm"], following["start_mm"])
            assert segment["source_face_ids"]
            assert segment["segment_id"]
            assert segment["max_error_mm"] <= .025
            assert all(p[2] == pytest.approx(z) for p in
                       (segment["start_cad_mm"], segment["end_cad_mm"]))


@pytest.mark.parametrize("z,radius", [(10, 15), (25, 10)])
def test_bushing_cross_sections_measure_both_diameters(z, radius):
    from cadnext.mesh.sections import section_mesh

    f = make_stepped_bushing()
    result = section_mesh(mesh_data(f.vertices, f.faces), frame(z), tolerance_mm=.1)
    assert result.usable_for_sketch
    radii = sorted({round(s["radius_mm"], 5) for c in result.chains for s in c["segments"]})
    assert radii == [5, radius]


def test_open_section_is_not_closed_with_invented_edge():
    from cadnext.mesh.sections import section_mesh

    f = make_plate_with_hole()
    # Remove a strip of the outside wall spanning the section plane.
    triangles = f.vertices[f.faces]
    keep = ~np.all(np.isclose(triangles[:, :, 0], 20), axis=1)
    result = section_mesh(mesh_data(f.vertices, f.faces[keep]), frame(), tolerance_mm=.1)
    assert not result.usable_for_sketch
    open_chains = [c for c in result.chains if not c["closed"]]
    assert len(open_chains) == 1
    assert open_chains[0]["role"] == "open"
    assert not np.allclose(open_chains[0]["points_mm"][0], open_chains[0]["points_mm"][-1])


def test_tangent_and_empty_section_do_not_produce_usable_profile():
    from cadnext.mesh.contracts import PlaneFrame
    from cadnext.mesh.sections import section_mesh

    normal = np.array([1, 1, 0])/np.sqrt(2)
    tangent = PlaneFrame((20, 15, 2.5), (0, 0, 1), tuple(np.cross(normal, [0, 0, 1])), tuple(normal))
    result = section_mesh(plate(), tangent, tolerance_mm=.1)
    assert not result.usable_for_sketch
    assert not section_mesh(plate(), frame(10), tolerance_mm=.1).usable_for_sketch


def test_large_tolerance_does_not_hide_a_real_open_gap():
    from cadnext.mesh.sections import section_mesh

    mesh = mesh_data([[0, 0, -1], [10, 0, -1], [0, 0, 1],
                      [0, .001, -1], [10, .001, -1], [0, .001, 1]], [[0, 1, 2], [3, 4, 5]])
    result = section_mesh(mesh, frame(0), tolerance_mm=1)
    assert not result.usable_for_sketch
    assert len(result.chains) == 2
    assert all(not c["closed"] for c in result.chains)


@pytest.mark.parametrize("axes", [
    ((2, 0, 0), (0, 1, 0), (0, 0, 1)),
    ((1, 0, 0), (1, 1, 0), (0, 0, 1)),
    ((1, 0, 0), (0, 1, 0), (0, 0, -1)),
    ((1, 0, 0), (0, 1, 0), (0, 0, float("nan"))),
])
def test_invalid_plane_frames_are_rejected(axes):
    from cadnext.mesh.contracts import MeshError, PlaneFrame

    with pytest.raises(MeshError):
        PlaneFrame((0, 0, 0), *axes)


async def test_section_operation_runs_in_real_owned_worker(tmp_path):
    import asyncio
    from dataclasses import asdict

    from cadnext.mesh.worker_client import MeshWorkerClient
    from tests.test_mesh_worker import payload

    data = payload(tmp_path)
    data.update(frame=asdict(frame()), tolerance_mm=.1)
    client = MeshWorkerClient()
    try:
        result = await client.request("section", data, stop=asyncio.Event())
        assert result["section"]["usable_for_sketch"]
    finally:
        await client.aclose()


def test_self_intersecting_closed_contour_is_not_a_valid_sketch():
    from cadnext.mesh.sections import section_mesh

    polygon = [(0, 0), (4, 4), (0, 3), (3, 0)]
    vertices = [(x, y, z) for z in (-1, 1) for x, y in polygon]
    faces = []
    for i in range(4):
        j = (i+1) % 4
        faces.extend([(i, j, j+4), (i, j+4, i+4)])
    result = section_mesh(mesh_data(vertices, faces), frame(0), tolerance_mm=.1)
    assert not result.usable_for_sketch
    assert "self_intersection" in result.reasons


def test_rotated_frame_retains_inverse_mapping_and_deterministic_ids():
    from cadnext.mesh.contracts import PlaneFrame
    from cadnext.mesh.sections import section_mesh

    rotated = PlaneFrame((2, 3, 2.5), (0, 1, 0), (-1, 0, 0), (0, 0, 1))
    a = section_mesh(plate(), rotated, tolerance_mm=.1)
    b = section_mesh(plate(), rotated, tolerance_mm=.1)
    assert a == b
    assert a.usable_for_sketch
    for chain in a.chains:
        for segment in chain["segments"]:
            x, y = segment["start_mm"]
            assert np.allclose(segment["start_cad_mm"], [2-y, 3+x, 2.5])
