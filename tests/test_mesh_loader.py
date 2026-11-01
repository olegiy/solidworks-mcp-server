"""Mesh inputs must preserve geometry without repairing or resolving resources."""

import hashlib
import struct
from dataclasses import replace

import numpy as np
import pytest
import trimesh

from tests.mesh_fixtures import make_plate_with_hole


def load(tmp_path, raw, *, format="stl", units="mm", limits=None):
    from cadnext.mesh.contracts import MeshLimits
    from cadnext.mesh.loader import load_mesh

    path = tmp_path / f"input.{format}"
    path.write_bytes(raw)
    return load_mesh(path, format=format, units=units, limits=limits or MeshLimits())


def encoded_plate(format="stl"):
    f = make_plate_with_hole()
    raw = trimesh.Trimesh(vertices=f.vertices, faces=f.faces, process=False).export(file_type=format)
    return raw.encode() if isinstance(raw, str) else raw


@pytest.mark.parametrize("format", ["stl", "stl_ascii", "obj"])
def test_load_keeps_dimensions_source_hash_and_read_only_arrays(tmp_path, format):
    raw = encoded_plate(format)
    mesh = load(tmp_path, raw, format="obj" if format == "obj" else "stl")
    assert mesh.sha256 == hashlib.sha256(raw).hexdigest()
    assert np.allclose(mesh.vertices.min(axis=0), [-20, -15, 0], atol=1e-5)
    assert np.allclose(mesh.vertices.max(axis=0), [20, 15, 5], atol=1e-5)
    assert not mesh.vertices.flags.writeable
    assert not mesh.faces.flags.writeable
    assert mesh.units == "mm"


def test_binary_stl_header_solid_is_not_misread_as_ascii(tmp_path):
    raw = encoded_plate()
    mesh = load(tmp_path, b"solid "+raw[6:])
    assert len(mesh.faces) == 544


def test_obj_negative_indices_and_inches_are_normalized(tmp_path):
    mesh = load(tmp_path, b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf -3 -2 -1\n", format="obj", units="in")
    assert mesh.scale_to_mm == 25.4
    assert np.allclose(mesh.vertices.max(axis=0), [25.4, 25.4, 0])
    assert len(mesh.faces) == 1


@pytest.mark.parametrize("raw", [
    b"", b"not a mesh", b"solid bad\nfacet normal 0 0 1\nendfacet\nendsolid bad",
    b"X"*80 + struct.pack("<I", 250001),
])
def test_invalid_or_oversize_stl_is_rejected(tmp_path, raw):
    from cadnext.mesh.contracts import MeshError

    with pytest.raises(MeshError):
        load(tmp_path, raw)


@pytest.mark.parametrize("raw", [
    b"v nan 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n",
    b"v inf 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n",
    b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 0 2 3\n",
    b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 4\n",
    b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3 1\n",
    b"v 0 0 0\nv 1 0 0\nv 2 0 0\nf 1 2 3\n",
    b"v 0 0 0\nv 1 0 0\nv 0 1 0\n",
    b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1/garbage 2 3\n",
])
def test_invalid_obj_geometry_is_rejected(tmp_path, raw):
    from cadnext.mesh.contracts import MeshError

    with pytest.raises(MeshError):
        load(tmp_path, raw, format="obj")


def test_obj_never_reads_materials_or_urls(tmp_path, monkeypatch):
    import urllib.request

    def forbidden(*args, **kwargs):
        pytest.fail("Mesh parser attempted external resource access")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    raw = b"mtllib ../../secret.mtl\nmtllib https://example.invalid/a.mtl\n"
    raw += b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n"
    assert len(load(tmp_path, raw, format="obj").faces) == 1


def test_two_disconnected_components_are_rejected(tmp_path):
    from cadnext.mesh.contracts import MeshError

    raw = b"v 0 0 0\nv 1 0 0\nv 0 1 0\nv 3 0 0\nv 4 0 0\nv 3 1 0\nf 1 2 3\nf 4 5 6\n"
    with pytest.raises(MeshError) as error:
        load(tmp_path, raw, format="obj")
    assert error.value.code == "multiple_components"


def test_limits_are_enforced_before_expensive_geometry_allocation(tmp_path):
    from cadnext.mesh.contracts import MeshError, MeshLimits

    raw = encoded_plate()
    for limits in (replace(MeshLimits(), max_bytes=8), replace(MeshLimits(), max_triangles=1),
                   replace(MeshLimits(), max_vertices=3)):
        with pytest.raises(MeshError):
            load(tmp_path, raw, limits=limits)


def test_unknown_units_and_formats_are_not_guessed(tmp_path):
    from cadnext.mesh.contracts import MeshError

    for format, units in (("stl", "auto"), ("ply", "mm")):
        with pytest.raises(MeshError):
            load(tmp_path, encoded_plate(), format=format, units=units)


def test_overlong_line_is_bounded(tmp_path):
    from cadnext.mesh.contracts import MeshError, MeshLimits

    with pytest.raises(MeshError):
        load(tmp_path, b"#" + b"x"*1024, format="obj",
             limits=replace(MeshLimits(), max_line_bytes=128))
