from contextlib import contextmanager
from types import SimpleNamespace as NS

import pytest

from cadnext.cad_geometry import transform_point
from cadnext.cad_reference import get_example
from scripts.staged_acceptance import (
    DEGREES_OF_FREEDOM_CODE,
    HOLE_CODE,
    REVOLVED_PROFILE_CODE,
    DirectAcceptance,
    direct,
)


@pytest.mark.parametrize("flags,accepted", [([1, 1, 0, 0, 0, 0], True),
                                          ([1, 1, 0, 0, 1, 1], False),
                                          ([0, 0, 0, 0, 0, 0], False)])
def test_acceptance_requires_only_one_axial_rotation(flags, accepted):
    def dofs(*args):
        for index, flag in enumerate(flags):
            args[2*index].value = flag
        args[3].value = NS(ArrayData=[0., 1., 0.])
        return 0

    env = {"shaft": NS(GetRemainingDOFs=dofs, GetConstrainedStatus=2),
           "byref_i4": lambda value=-1: NS(value=value),
           "VARIANT": lambda kind, value: NS(value=value),
           "pythoncom": NS(VT_BYREF=1, VT_DISPATCH=2), "GET": getattr}
    if accepted:
        exec(DEGREES_OF_FREEDOM_CODE, env)
        assert env["result"]["flags"] == flags
    else:
        with pytest.raises(RuntimeError, match="подвижность"):
            exec(DEGREES_OF_FREEDOM_CODE, env)


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

    exec(get_example("sheet-open-profile")["code"], {"model": model, "MM": lambda v: v/1000,
         "GET": get, "NOTHING": None, "DIMENSIONS": nullcontext})
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
