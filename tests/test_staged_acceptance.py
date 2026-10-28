from contextlib import contextmanager
from types import SimpleNamespace as NS

from scripts.staged_acceptance import HOLE_CODE, direct


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
        return object()

    sketch = NS(ModelToSketchTransform=object())
    sm = NS(ActiveSketch=sketch, InsertSketch=lambda flag: None,
            CreateCircleByRadius=lambda *args: NS(Select4=lambda *args: True))
    body = NS(GetFaces=[NS(GetSurface=NS(IsPlane=True, PlaneParams=[0,1,0,0,0,0]))])
    model = NS(GetBodies2=lambda *args: [body], ClearSelection2=lambda flag: None,
               Extension=NS(SelectByID2=lambda *args: True), SketchManager=sm,
               AddDiameterDimension2=diameter)
    utility = NS(CreatePoint=lambda xyz: NS(MultiplyTransform=lambda transform:
                                         NS(ArrayData=[xyz[0], xyz[2], 0])))

    def get(obj, name, *args):
        value = getattr(obj, name)
        return value(*args) if callable(value) else value

    exec(HOLE_CODE, {"GET": get, "model": model, "swApp": NS(GetMathUtility=utility),
                     "MM": lambda value: value/1000, "NOTHING": None, "DIMENSIONS": dimensions})
    assert len(diameters) == 2
    assert enabled is True


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
