import pytest

from cadnext.cad_geometry import transform_point


def test_transform_point_uses_solidworks_row_vectors_scale_and_translation():
    matrix = [0,1,0,-1,0,0,0,0,1, .1,.2,.3,2,0,0,0]
    assert transform_point([.01,.02,.03], matrix) == pytest.approx([.06,.22,.36])


def test_front_face_transform_maps_depth_to_sketch_y():
    matrix = [1,0,0,0,0,-1,0,1,0, 0,0,0,1,0,0,0]
    assert transform_point([.03,0,.02], matrix) == pytest.approx([.03,.02,0])
