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


def test_cosmetic_thread_inspection_reads_definition_not_feature_name():
    definition = NS(Standard=8, StandardType="Machine Threads", Size="M24",
                    Diameter=.024, DiameterType=2, BlindDepth=.044, EndCondition=0,
                    ApplyThread=0, ThreadCallout="custom note")
    thread = NS(Name="Actual thread", GetTypeName2="CosmeticThread", GetNextFeature=None,
                GetDefinition=definition, IsSuppressed=False, GetErrorCode2=lambda warning: 0)
    cylinder = NS(Name="M24x3 thread", GetTypeName2="Revolution", GetNextFeature=thread)
    model = NS(GetTitle="Part", GetType=1, FirstFeature=cylinder)
    data = inspection.inspect_model(model, ["cosmetic_threads"], [], get)
    assert data["cosmetic_threads"] == [{
        "name": "Actual thread", "type": "CosmeticThread", "standard": 8,
        "standard_type": "Machine Threads", "size": "M24", "diameter_mm": 24.,
        "diameter_type": 2, "blind_depth_mm": 44., "end_condition": 0,
        "apply_thread": 0, "callout": "custom note", "suppressed": False,
        "error_code": 0, "warning": False}]


def test_cosmetic_thread_checks_reject_cylinder_named_as_thread():
    check = {"kind": "cosmetic_thread", "name": "M24", "diameter_mm": 24.,
             "diameter_type": 2, "end_condition": 0, "blind_depth_mm": 44.,
             "size": "M24", "standard": 8}
    assert construction.inspection_request([check]) == {
        "sections": ["cosmetic_threads"], "dimension_names": []}
    cylinder = NS(Name="M24", GetTypeName2="Revolution", GetNextFeature=None)
    data = inspection.inspect_model(NS(GetTitle="Part", GetType=1, FirstFeature=cylinder),
                                    ["cosmetic_threads"], [], get)
    assert construction.evaluate_checks([check], data)[0]["passed"] is False


@pytest.mark.parametrize("changed", [{}, {"diameter_mm": 24.01}, {"blind_depth_mm": 43.9},
    {"size": ""}, {"standard": -2}, {"suppressed": True}, {"error_code": 1},
    {"diameter_type": 3}, {"end_condition": 1}, {"warning": True}, {"type": "Revolution"}])
def test_cosmetic_thread_checks_compare_actual_values(changed):
    check = {"kind": "cosmetic_thread", "name": "Thread", "diameter_mm": 24.,
             "diameter_type": 2, "end_condition": 0, "blind_depth_mm": 44.,
             "size": "M24", "standard": 8}
    item = {key: value for key, value in check.items() if key != "kind"}
    item.update(type="CosmeticThread", suppressed=False, error_code=0, warning=False)
    item.update(changed)
    data = {"cosmetic_threads": [item]}
    assert construction.evaluate_checks([check], data)[0]["passed"] is (not changed)
    data["truncated"] = True
    assert construction.evaluate_checks([check], data)[0]["passed"] is False


def test_hole_standard_number_is_not_a_cosmetic_thread_standard():
    check = {"kind": "cosmetic_thread", "name": "Thread", "diameter_mm": 24.,
             "diameter_type": 2, "end_condition": 0, "standard": 20}
    # Toolbox's GOST hole standard 20 is absent from swCosmeticStandardType_e.
    with pytest.raises(ValueError):
        construction.inspection_request([check])


def test_unknown_observed_cosmetic_standard_cannot_pass_without_standard_expectation():
    check = {"kind": "cosmetic_thread", "name": "Thread", "diameter_mm": 24.,
             "diameter_type": 2, "end_condition": 0}
    item = {key: value for key, value in check.items() if key != "kind"}
    item.update(type="CosmeticThread", standard=20, suppressed=False, error_code=0, warning=False)
    assert not construction.evaluate_checks([check], {"cosmetic_threads": [item]})[0]["passed"]


@pytest.mark.parametrize("in_history", [False, True])
def test_saved_legacy_thread_checks_remain_readable_without_allowing_new_checks(in_history):
    expected = {"kind": "cosmetic_thread", "name": "Thread", "diameter_mm": 24.,
                "diameter_type": 2, "end_condition": 0, "standard": 20}
    stage = {"id": "thread", "title": "Thread", "expected_result": "Thread exists",
             "status": "done", "tool_call_ids": ["old_call"],
             "checks": [{"expected": expected, "observed": [], "passed": False}]}
    stages = [] if in_history else [stage]
    history = [{"revision": 1, "stages": [stage]}] if in_history else []
    restored = construction.validate_saved_plan(stages, history)
    assert restored == (stages, history)
    assert expected["standard"] == 20
    with pytest.raises(ValueError):
        construction.inspection_request([expected])


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
