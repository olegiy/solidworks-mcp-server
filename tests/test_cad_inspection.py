from types import SimpleNamespace as NS

import pytest

from cadnext import cad_inspection as inspection
from cadnext import construction


def get(obj, name, *args):
    value = getattr(obj, name)
    return value(*args) if callable(value) else value


def test_repeated_subfeature_does_not_hide_later_siblings():
    thread = NS(Name="Thread", GetTypeName2="CosmeticThread", GetNextSubFeature=None)
    sketch_sub = NS(Name="Sketch", GetTypeName2="ProfileFeature", GetNextSubFeature=thread)
    boss = NS(Name="Boss", GetTypeName2="Revolution", GetFirstSubFeature=sketch_sub,
              GetNextFeature=None)
    sketch = NS(Name="Sketch", GetTypeName2="ProfileFeature", GetNextFeature=boss)
    model = NS(GetTitle="Part", GetType=1, FirstFeature=sketch)
    data = inspection.inspect_model(model, ["features"], [], get)
    assert [item["name"] for item in data["features"]] == ["Sketch", "Boss", "Thread"]
    assert data["truncated"] is False


def test_cyclic_sibling_chain_is_bounded_and_cannot_pass_checks():
    repeated = NS(Name="Sketch", GetTypeName2="ProfileFeature")
    repeated.GetNextSubFeature = repeated
    boss = NS(Name="Boss", GetTypeName2="Revolution", GetFirstSubFeature=repeated,
              GetNextFeature=None)
    sketch = NS(Name="Sketch", GetTypeName2="ProfileFeature", GetNextFeature=boss)
    model = NS(GetTitle="Part", GetType=1, FirstFeature=sketch)
    data = inspection.inspect_model(model, ["features"], [], get)
    assert data["truncated"] is True
    assert not construction.evaluate_checks([{"kind": "feature", "name": "Boss"}], data)[0]["passed"]


@pytest.mark.parametrize("api_type,legacy_type,expected", [
    ("ICE", "Cut", "Cut"), ("ICE", "Boss", "Boss"),
    ("Revolution", "legacy-value", "Revolution"),
])
def test_instant3d_feature_checks_use_underlying_type(api_type, legacy_type, expected):
    feature = NS(Name="Feature1", GetTypeName2=api_type, GetTypeName=legacy_type,
                 GetNextFeature=None)
    model = NS(GetTitle="Part", GetType=1, FirstFeature=feature)
    data = inspection.inspect_model(model, ["features"], [], get)
    check = {"kind": "feature", "name": "Feature1", "type": expected}
    assert construction.evaluate_checks([check], data)[0]["passed"]
    wrong = {**check, "type": "Cut" if expected != "Cut" else "Boss"}
    assert not construction.evaluate_checks([wrong], data)[0]["passed"]


def test_inspection_reads_driving_dimensions_angles_and_surfaces_without_bbox_proof():
    # Values read from installed SW2025 swconst.tlb, rather than web snippets.
    dims = {"OD@Profile": NS(SystemValue=.030, GetType=0, DrivenState=2),
            "A@Chamfer": NS(SystemValue=3.141592653589793/4, GetType=1, DrivenState=1)}
    surface = NS(IsCylinder=True, CylinderParams=[.03, 0, .02, 0, 1, 0, .0045])
    body = NS(IsSheetMetal=True, GetBodyBox=[0, 0, 0, .06, .04, .08],
              GetFaces=[NS(GetSurface=surface)])
    model = NS(GetTitle="Bracket", GetType=1, Parameter=dims.get, GetBodies2=lambda *a: [body])
    data = inspection.inspect_model(model, ["dimensions", "bodies", "surfaces"],
                                    list(dims), get)
    assert data["dimensions"] == [
        {"name": "OD@Profile", "value": 30., "unit": "mm", "driving": True},
        {"name": "A@Chamfer", "value": 45., "unit": "deg", "driving": False}]
    assert data["bodies"][0]["bbox_approximate"] is True
    assert data["surfaces"][0]["radius_mm"] == 4.5
    assert data["surfaces"][0]["axis_point_mm"] == [30, 0, 20]


def test_inspection_reads_components_and_mates_in_subfeatures():
    mate = NS(Type=1, Alignment=0, GetMateEntityCount=2,
              MateEntity=lambda i: NS(ReferenceComponent=NS(Name2=["Bushing-1", "Shaft-1"][i]),
                  ReferenceType=2, EntityParams=[0, .02, 0, 0, 1, 0, .008, 0]))
    sub = NS(Name="Concentric", GetTypeName2="MateConcentric", GetSpecificFeature2=mate,
             GetFirstSubFeature=None, GetNextSubFeature=None, GetErrorCode2=lambda warning: 0,
             IsSuppressed=False, GetDefinition=NS(LockRotation=False))
    group = NS(Name="Mates", GetTypeName2="MateGroup", GetFirstSubFeature=sub,
               GetNextFeature=None)
    component = NS(Name2="Bushing-1", GetPathName="bushing.sldprt", IsFixed=True,
                   IsSuppressed=False, Transform2=NS(ArrayData=[1,0,0,0,1,0,0,0,1,.001,0,0,1,0,0,0]))
    model = NS(GetTitle="Assembly", GetType=2, FirstFeature=group,
               GetComponents=lambda top: [component])
    data = inspection.inspect_model(model, ["components", "mates"], [], get)
    assert data["components"][0]["translation_mm"] == [1, 0, 0]
    assert data["components"][0]["fixed"] is True
    assert data["mates"][0]["components"] == ["Bushing-1", "Shaft-1"]
    assert data["mates"][0]["type"] == 1
    assert data["mates"][0]["lock_rotation"] is False
    assert data["mates"][0]["entities"][0]["point_mm"] == [0, 20, 0]
    assert data["mates"][0]["entities"][0]["radii_mm"] == [8, 0]
    checks = [{"kind": "mate", "mate_type": 1, "components": ["Bushing-1", "Shaft-1"]}]
    assert construction.evaluate_checks(checks, data)[0]["passed"]
    unlocked = [{**checks[0], "lock_rotation": False}]
    assert construction.evaluate_checks(unlocked, data)[0]["passed"]
    sub.GetDefinition.LockRotation = True
    locked = inspection.inspect_model(model, ["mates"], [], get)
    assert not construction.evaluate_checks(unlocked, locked)[0]["passed"]
    sub.IsSuppressed = True
    suppressed = inspection.inspect_model(model, ["mates"], [], get)
    assert not construction.evaluate_checks(checks, suppressed)[0]["passed"]


def test_checks_require_driving_dimensions_and_actual_geometry_with_tolerances():
    checks = [{"kind": "dimension", "name": "Length@Profile", "value": 80., "unit": "mm"},
              {"kind": "body_count", "expected": 1}]
    request = construction.inspection_request(checks)
    assert set(request["sections"]) == {"dimensions", "bodies"}
    data = {"dimensions": [{"name": "Length@Profile", "value": 80.0009,
                            "unit": "mm", "driving": True}], "bodies": [{"index": 0}]}
    result = construction.evaluate_checks(checks, data)
    assert all(item["passed"] for item in result)
    data["dimensions"][0]["driving"] = False
    assert construction.evaluate_checks(checks, data)[0]["passed"] is False
    data["dimensions"][0].update(driving=True, value=80.002)
    assert construction.evaluate_checks(checks, data)[0]["passed"] is False


def test_hole_check_compares_axis_lines_instead_of_arbitrary_cylinder_origin():
    checks = [{"kind": "cylinder", "radius_mm": 4.5, "count": 1,
               "axis_point_mm": [30, 0, 20], "axis_direction": [0, 1, 0]}]
    data = {"surfaces": [{"kind": "cylinder", "radius_mm": 4.5,
                           "axis_point_mm": [30, 500, 20], "axis_direction": [0, -1, 0]}]}
    assert construction.evaluate_checks(checks, data)[0]["passed"]
    data["surfaces"][0]["axis_point_mm"][0] = 30.1
    assert not construction.evaluate_checks(checks, data)[0]["passed"]


@pytest.mark.parametrize("check", [
    {"kind": "body_count", "expected": -1},
    {"kind": "dimension", "name": "X", "value": True, "unit": "mm"},
    {"kind": "python", "code": "mutate()"},
    {"kind": "cylinder", "radius_mm": 4.5, "count": 1, "axis_direction": [0,0,0]},
])
def test_invalid_checks_are_rejected_before_mutation(check):
    with pytest.raises(ValueError):
        construction.inspection_request([check])
