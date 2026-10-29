from contextlib import contextmanager
from types import SimpleNamespace as NS

import pytest

from cadnext.cad_geometry import transform_point
from cadnext.cad_reference import get_example
from scripts.staged_acceptance import (
    HOLE_CODE,
    REVOLVED_PROFILE_CODE,
    DirectAcceptance,
    direct,
)


@pytest.mark.parametrize("defect", [None, "locked", "extra", "off_axis", "angled_end", "offset_end",
                                    "suppressed", "fixed_shaft", "missing_entities"])
def test_axial_joint_proof_uses_actual_constraints_and_entity_geometry(defect):
    from copy import deepcopy

    from scripts.staged_acceptance import axial_joint_evidence

    cylinder = {"reference_type": 4, "point_mm": [0, 0, 0], "direction": [0, 1, 0], "radii_mm": [8, 0]}
    plane = {"reference_type": 3, "point_mm": [0, 0, 0], "direction": [0, -1, 0], "radii_mm": [0, 0]}
    pair = ["bushing-1", "shaft-1"]
    data = {"components": [{"name": pair[0], "fixed": True, "suppressed": False},
                           {"name": pair[1], "fixed": False, "suppressed": False}],
            "mates": [{"type": 1, "lock_rotation": False, "error_code": 0, "suppressed": False,
                       "components": pair, "entities": [deepcopy(cylinder), deepcopy(cylinder)]},
                      {"type": 0, "error_code": 0, "suppressed": False,
                       "components": pair, "entities": [deepcopy(plane), deepcopy(plane)]}]}
    if defect == "locked":
        data["mates"][0]["lock_rotation"] = True
    elif defect == "extra":
        data["mates"].append(deepcopy(data["mates"][1]))
    elif defect == "off_axis":
        data["mates"][0]["entities"][1]["point_mm"] = [1, 0, 0]
    elif defect == "angled_end":
        data["mates"][1]["entities"][0]["direction"] = [1, 0, 0]
    elif defect == "offset_end":
        data["mates"][1]["entities"][1]["point_mm"] = [0, 1, 0]
    elif defect == "suppressed":
        data["mates"][0]["suppressed"] = True
    elif defect == "fixed_shaft":
        data["components"][1]["fixed"] = True
    elif defect == "missing_entities":
        data["mates"][0]["entities"] = []
    if defect:
        with pytest.raises(RuntimeError):
            axial_joint_evidence(data)
    else:
        assert axial_joint_evidence(data) == {"method": "analytic_cylindrical_joint",
            "remaining_translations": 0, "remaining_rotations": 1, "axis_direction": [0, 1, 0]}


def test_revolved_profile_handles_select_data_as_com_property():
    from contextlib import nullcontext

    selection = NS(Mark=0)
    selected = []
    line = NS(Select4=lambda append, data: selected.append(data) or True)
    sm = NS(CreateCenterLine=lambda *args: line, CreateLine=lambda *args: line,
            InsertSketch=lambda flag: None)
    profile = NS(Name="Sketch", Select2=lambda *args: True)
    model = NS(SketchManager=sm, ClearSelection2=lambda flag: None,
               AddDimension2=lambda *args: NS(GetDimension2=lambda index: NS(Name="D")),
               FeatureByPositionReverse=lambda index: profile,
               SelectionManager=NS(CreateSelectData=selection))

    def get(obj, name, *args):
        value = getattr(obj, name)
        return value(*args) if callable(value) else value

    exec(REVOLVED_PROFILE_CODE, {"model": model, "GET": get, "MM": lambda v: v/1000,
                               "NOTHING": None, "DIMENSIONS": nullcontext})
    assert selected[-1] is selection
    assert selection.Mark == 16


async def test_acceptance_forwards_named_dimension_argument(tmp_path):
    class Cad:
        async def call(self, tool, arguments):
            assert tool == "set_dimension"
            assert arguments == {"name": "D2@BracketBase", "value_mm": 100}
            return {"ok": True, "value": {"changed": True}}

    result = await DirectAcceptance(Cad(), tmp_path).call(
        "set_dimension", name="D2@BracketBase", value_mm=100)
    assert result == {"changed": True}


async def test_bracket_reads_profile_before_separate_sheet_metal_mutation(tmp_path):
    snippets, inspections = [], []

    class Captured(DirectAcceptance):
        async def new_part(self):
            pass

        async def python(self, code):
            snippets.append(code)
            if len(snippets) == 2:
                raise RuntimeError("captured second stage")

        async def inspect(self, checks):
            inspections.append(checks)

        async def vertices(self, expected):
            pass

        async def call(self, tool, **arguments):
            return {}

    with pytest.raises(RuntimeError, match="captured second stage"):
        await Captured(None, tmp_path).bracket()
    assert "InsertSheetMetalBaseFlange2" not in snippets[0]
    assert "InsertSheetMetalBaseFlange2" in snippets[1]
    assert {"kind": "body_count", "expected": 0} in inspections[0]
    assert {"kind": "feature", "name": "BracketProfile", "type": "ProfileFeature"} in inspections[0]


async def test_assembly_directly_executes_component_library_recipe(tmp_path):
    snippets = []

    class Captured(DirectAcceptance):
        async def new_part(self):
            pass

        async def python(self, code):
            snippets.append(code)
            if 'component = model.AddComponent5' in code:
                raise RuntimeError("captured library insertion")

        async def inspect(self, checks):
            pass

        async def save(self, name):
            return tmp_path / name

        async def active(self):
            pass

        async def call(self, tool, **arguments):
            if tool == "create_assembly":
                return {"created": "TestAssembly"}
            if tool == "open_python_session":
                return {"session_id": "test"}
            if tool == "add_component":
                pytest.fail("The original component recipe must be executed directly")
            return {}

    test = Captured(None, tmp_path)
    test.bushing_path = tmp_path / "bushing.sldprt"
    with pytest.raises(RuntimeError, match="captured library insertion"):
        await test.assembly()
    assert snippets[-1].endswith(get_example("assembly-insert")["code"])


def test_open_profile_uses_direction_one_and_explicit_bend_allowance():
    from contextlib import nullcontext

    profile = NS(Name="Sketch", Select2=lambda *args: True)
    profile.GetTypeName2 = "ProfileFeature"
    line = NS(Select4=lambda *args: True)
    dimension = NS(GetDimension2=lambda *args: NS(Name="D1"))
    allowance = NS(KFactor=0)
    calls = []

    def flange(*args):
        calls.append(args)
        assert args[8] == 1
        assert args[9] is allowance
        assert allowance.KFactor == .5
        return NS(Name="Base")

    sm = NS(CreateLine=lambda *args: line, InsertSketch=lambda *args: None)
    fm = NS(CreateCustomBendAllowance=lambda: allowance, InsertSheetMetalBaseFlange2=flange)
    model = NS(SketchManager=sm, FeatureManager=fm, AddDimension2=lambda *args: dimension,
               ClearSelection2=lambda *args: None, FeatureByPositionReverse=lambda index: profile)

    def get(obj, name, *args):
        value = getattr(obj, name)
        return value(*args) if callable(value) else value

    example = get_example("sheet-open-profile")
    env = {"model": model, "MM": lambda v: v/1000,
           "GET": get, "NOTHING": None, "DIMENSIONS": nullcontext}
    exec(example["preparation_code"], env)
    assert not calls
    exec(example["code"], env)
    assert len(calls) == 1
    assert calls[0][0] == pytest.approx(.002)


def test_hole_diameters_use_nonmodal_dimension_context():
    enabled = True
    diameters = []

    @contextmanager
    def dimensions():
        nonlocal enabled
        previous, enabled = enabled, False
        try:
            yield
        finally:
            enabled = previous

    def diameter(*args):
        if enabled:
            raise RuntimeError("SolidWorks would open a modal dimension editor")
        diameters.append(args)
        return NS(GetDimension2=lambda index: NS(Name="D1"))

    sketch = NS(ModelToSketchTransform=NS(ArrayData=[1,0,0,0,0,-1,0,1,0,0,0,0,1,0,0,0]))
    sm = NS(ActiveSketch=sketch, InsertSketch=lambda flag: None,
            CreateCircleByRadius=lambda *args: NS(Select4=lambda *args: True,
                GetCenterPoint2=NS(Select4=lambda *args: True)))
    body = NS(GetFaces=[NS(GetSurface=NS(IsPlane=True, PlaneParams=[0,1,0,0,0,0]))])
    body.GetVertices = [NS(GetPoint=[.06, 0., .08], Select4=lambda *args: True)]
    model = NS(GetBodies2=lambda *args: [body], ClearSelection2=lambda flag: None,
               Extension=NS(SelectByID2=lambda *args: True), SketchManager=sm,
               AddDiameterDimension2=diameter,
               AddHorizontalDimension2=diameter, AddVerticalDimension2=diameter,
               FeatureByPositionReverse=lambda index: NS(Name="Sketch"),
               GetEquationMgr=NS(Add2=lambda *args: 0))
    model.FirstFeature = NS(GetTypeName2="OriginProfileFeature",
        GetSpecificFeature2=NS(GetSketchPoints2=[NS(Select4=lambda *args: True)]))
    profile = NS(Name="Sketch6", GetTypeName2="ProfileFeature", GetNextFeature=None)
    model.FirstFeature.GetNextFeature = profile
    utility = NS(CreatePoint=lambda xyz: NS(MultiplyTransform=lambda transform:
                                         NS(ArrayData=[xyz[0], xyz[2], 0])))

    def get(obj, name, *args):
        value = getattr(obj, name)
        return value(*args) if callable(value) else value

    exec(HOLE_CODE, {"GET": get, "model": model, "swApp": NS(GetMathUtility=utility),
                     "MM": lambda value: value/1000, "NOTHING": None, "DIMENSIONS": dimensions,
                     "TRANSFORM_POINT": transform_point})
    assert len(diameters) == 6
    assert enabled is True
    assert profile.Name == "HoleSketch"


async def test_direct_acceptance_stops_on_creation_error_without_retry(tmp_path):
    class Cad:
        def __init__(self):
            self.calls = []

        async def call(self, name, arguments):
            self.calls.append(name)
            if name == "create_part":
                return {"ok": False, "value": None, "error": "failed", "uncertain": True}
            value = {"active_document": None} if name == "connect_solidworks" else {"documents": []}
            return {"ok": True, "value": value, "images": [], "error": None, "uncertain": False}

    cad = Cad()
    report = await direct(cad, tmp_path)
    assert report["status"] == "failed"
    assert cad.calls == ["connect_solidworks", "list_open_documents", "create_part"]


async def test_direct_acceptance_records_harness_error_and_does_not_continue(tmp_path):
    class Cad:
        async def call(self, tool, arguments):
            if tool == "create_part":
                raise TypeError("harness failure")
            return {"ok": True, "value": {"documents": []}}

    report = await direct(Cad(), tmp_path)
    assert report["status"] == "failed"
    assert report["error"] == "harness failure"
