"""Analytic expectations catch false fits and misleading mesh quality reports."""

import hashlib

import numpy as np
import pytest

from tests.mesh_fixtures import make_plate_with_hole


def mesh_data(vertices, faces):
    from cadnext.mesh.contracts import MeshData

    v, f = np.asarray(vertices, dtype=float), np.asarray(faces, dtype=np.int64)
    return MeshData(v, f, hashlib.sha256(v.tobytes()+f.tobytes()).hexdigest(), "mm", 1.)


def cylinder_mesh(*, radius=7, height=25, angle=2*np.pi, noise=0):
    rng = np.random.default_rng(42)
    theta = np.linspace(0, angle, 97)
    vertices = [(r*np.cos(t), r*np.sin(t), z)
                for z in np.linspace(0, height, 5) for t, r in
                zip(theta, radius+rng.normal(0, noise, len(theta)), strict=True)]
    faces = []
    for row in range(4):
        for col in range(96):
            a = row*97+col
            faces.extend([(a, a+1, a+98), (a, a+98, a+97)])
    return mesh_data(vertices, faces)


def test_inspection_has_bounds_oriented_volume_and_stable_region_ids():
    from cadnext.mesh.analysis import inspect_mesh

    fixture = make_plate_with_hole()
    mesh = mesh_data(fixture.vertices, fixture.faces)
    summary = inspect_mesh(mesh)
    assert np.allclose(summary.bounds_mm, [[-20, -15, 0], [20, 15, 5]])
    assert summary.watertight and summary.winding_consistent
    assert summary.boundary_edges == 0 and summary.components == 1
    assert summary.volume_mm3 == pytest.approx((1200-25*np.pi)*5, rel=.001)
    assert summary.regions == inspect_mesh(mesh).regions
    assert len(summary.regions) >= 6
    assert all(r["region_id"].startswith(mesh.sha256[:16]) for r in summary.regions)


def test_open_mesh_reports_boundary_and_no_certified_volume():
    from cadnext.mesh.analysis import inspect_mesh

    summary = inspect_mesh(mesh_data([[0, 0, 0], [10, 0, 0], [0, 10, 0]], [[0, 1, 2]]))
    assert summary.boundary_edges == 3
    assert not summary.watertight
    assert summary.volume_mm3 is None


def test_symmetric_pca_axes_are_explicitly_ambiguous():
    from cadnext.mesh.analysis import inspect_mesh

    summary = inspect_mesh(cylinder_mesh())
    assert summary.pca_ambiguous
    assert summary.source_to_cad is None


def test_noisy_plane_fit_has_measured_residuals_and_normal():
    from cadnext.mesh.analysis import fit_region

    vertices = [[x, y, 3+.002*(-1)**(x+y)] for y in range(5) for x in range(5)]
    faces = [face for y in range(4) for x in range(4) for face in
             [(y*5+x, y*5+x+1, y*5+x+5), (y*5+x+1, y*5+x+6, y*5+x+5)]]
    result = fit_region(mesh_data(vertices, faces), "all", "plane", tolerance_mm=.1)
    assert result.usable
    assert result.used_points == 25
    assert result.parameters["origin_mm"][2] == pytest.approx(3, abs=.003)
    assert abs(result.parameters["normal"][2]) > .999
    assert 0 < result.rms_mm < .003
    assert result.max_residual_mm < .005


def test_circle_and_cylinder_fit_measure_radius_axis_and_coverage():
    from cadnext.mesh.analysis import fit_region

    cylinder = fit_region(cylinder_mesh(noise=.001), "all", "cylinder", tolerance_mm=.1)
    assert cylinder.usable
    assert cylinder.parameters["radius_mm"] == pytest.approx(7, abs=.005)
    assert abs(cylinder.parameters["axis"][2]) > .999
    assert cylinder.parameters["axial_span_mm"] == pytest.approx(25, abs=.01)
    assert cylinder.coverage["angle_degrees"] > 350
    assert cylinder.max_residual_mm < .01
    theta = np.linspace(0, 2*np.pi, 64, endpoint=False)
    # A triangulated ring, not a disk with a centre point contaminating the fit.
    ring = mesh_data([(5*np.cos(t), 5*np.sin(t), 2) for t in theta],
                     [(0, i, i+1) for i in range(1, 63)])
    circle = fit_region(ring, "all", "circle", tolerance_mm=.1)
    assert circle.usable
    assert circle.parameters["radius_mm"] == pytest.approx(5, abs=1e-8)
    assert np.allclose(circle.parameters["center_mm"], [0, 0, 2], atol=1e-8)


def test_tiny_arc_and_almost_collinear_points_are_not_usable():
    from cadnext.mesh.analysis import fit_region

    tiny = fit_region(cylinder_mesh(angle=.05), "all", "cylinder", tolerance_mm=.1)
    assert not tiny.usable and "insufficient_coverage" in tiny.degeneracies
    line = mesh_data([[0, 0, 0], [1, 1e-10, 0], [2, 0, 0]], [[0, 1, 2]])
    result = fit_region(line, "all", "plane", tolerance_mm=.1)
    assert not result.usable and "collinear" in result.degeneracies


def test_region_box_must_select_one_connected_surface():
    from cadnext.mesh.analysis import fit_region
    from cadnext.mesh.contracts import MeshError

    mesh = mesh_data([[-1, -1, 0], [1, -1, 0], [0, 1, 0],
                      [-1, -1, 5], [1, -1, 5], [0, 1, 5]], [[0, 1, 2], [3, 4, 5]])
    with pytest.raises(MeshError, match="связн"):
        fit_region(mesh, {"bounds_mm": [[-2, -2, -1], [2, 2, 6]]}, "plane", tolerance_mm=.1)
    result = fit_region(mesh, {"bounds_mm": [[-2, -2, -1], [2, 2, 1]],
                               "normal": [0, 0, 1], "angle_degrees": 5}, "plane", tolerance_mm=.1)
    assert result.usable and result.used_points == 3


def test_region_id_cannot_be_reused_with_different_source():
    from cadnext.mesh.analysis import fit_region, inspect_mesh
    from cadnext.mesh.contracts import MeshError

    original = cylinder_mesh()
    region = inspect_mesh(original).regions[0]["region_id"]
    with pytest.raises(MeshError):
        fit_region(cylinder_mesh(radius=8), region, "plane", tolerance_mm=.1)


@pytest.mark.parametrize("tolerance", [0, -1, float("nan"), True, 11])
def test_invalid_tolerance_never_starts_fitting(tolerance):
    from cadnext.mesh.analysis import fit_region
    from cadnext.mesh.contracts import MeshError

    with pytest.raises(MeshError):
        fit_region(cylinder_mesh(), "all", "plane", tolerance_mm=tolerance)
