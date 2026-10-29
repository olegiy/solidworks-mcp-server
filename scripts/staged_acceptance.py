"""Explicit live acceptance, confined to new documents and a fresh output folder."""

import argparse
import ast
import asyncio
import json
import tempfile
from pathlib import Path
from textwrap import dedent

from cadnext.cad_reference import get_example
from cadnext.construction import evaluate_checks, inspection_request
from cadnext.mcp import SolidWorksMcp

BRACKET_CHECKS = [
    {"kind": "feature", "name": "BracketBase", "type": "SMBaseFlange"},
    {"kind": "body_count", "expected": 1},
    {"kind": "sheet_metal", "thickness_mm": 2, "bend_radius_mm": 2},
]


HOLE_CODE = dedent('''
            bodies = model.GetBodies2(0, False)
            candidates = []
            for face in GET(bodies[0], "GetFaces"):
                surface = GET(face, "GetSurface")
                if GET(surface, "IsPlane"):
                    p = surface.PlaneParams
                    if abs(abs(p[1])-1) < 1e-7:
                        candidates.append(p[4])
            if not candidates:
                raise RuntimeError("Грань полки не найдена")
            face_y = min(candidates)
            model.ClearSelection2(True)
            if not model.Extension.SelectByID2("", "FACE", MM(30), face_y, MM(40), False, 0, NOTHING, 0):
                raise RuntimeError("Грань полки не выбрана")
            model.SketchManager.InsertSketch(True)
            hole_sketch = GET(model.SketchManager, "ActiveSketch")
            transform = hole_sketch.ModelToSketchTransform
            origin_feature = GET(model, "FirstFeature")
            while origin_feature is not None and GET(origin_feature, "GetTypeName2") != "OriginProfileFeature":
                origin_feature = GET(origin_feature, "GetNextFeature")
            if origin_feature is None:
                raise RuntimeError("Начало координат не найдено")
            origin_points = GET(GET(origin_feature, "GetSpecificFeature2"), "GetSketchPoints2")
            origin = origin_points[0]
            end_vertices = [v for v in GET(bodies[0], "GetVertices")
                if abs(GET(v, "GetPoint")[0]-MM(60)) < 1e-8
                and abs(GET(v, "GetPoint")[1]-face_y) < 1e-8
                and abs(GET(v, "GetPoint")[2]-MM(80)) < 1e-8]
            if len(end_vertices) != 1:
                raise RuntimeError("Торцевая вершина неоднозначна")
            for index, z_mm in enumerate((20., 60.), 1):
                local = TRANSFORM_POINT([MM(30), face_y, MM(z_mm)], list(transform.ArrayData))
                circle = model.SketchManager.CreateCircleByRadius(*local, MM(4.5))
                if circle is None:
                    raise RuntimeError("Окружность не создана")
                if not circle.Select4(False, NOTHING):
                    raise RuntimeError("Окружность не выбрана")
                with DIMENSIONS():
                    dim = model.AddDiameterDimension2(local[0]+MM(10), local[1]+MM(10), 0.)
                if dim is None:
                    raise RuntimeError("Диаметр не создан")
                dim.GetDimension2(0).Name = f"Hole{index}Diameter"
                center = GET(circle, "GetCenterPoint2")
                for axis, method, position in (
                    ("X", model.AddHorizontalDimension2, (local[0]/2, local[1]-MM(10), 0.)),
                    ("Z", model.AddVerticalDimension2, (local[0]+MM(10), local[1]/2, 0.))):
                    model.ClearSelection2(True)
                    if not center.Select4(False, NOTHING):
                        raise RuntimeError("Центр отверстия не выбран")
                    datum = end_vertices[0] if index == 2 and axis == "Z" else origin
                    if not datum.Select4(True, NOTHING):
                        raise RuntimeError("Опорная точка не выбрана")
                    with DIMENSIONS():
                        display = method(*position)
                    if display is None:
                        raise RuntimeError("Позиционный размер не создан")
                    display.GetDimension2(0).Name = f"Hole{index}{axis}"
            model.ClearSelection2(True)
            model.SketchManager.InsertSketch(True)
            feature = GET(model, "FirstFeature")
            profiles = []
            while feature is not None:
                if GET(feature, "GetTypeName2") == "ProfileFeature":
                    profiles.append(feature)
                feature = GET(feature, "GetNextFeature")
            if not profiles:
                raise RuntimeError("Эскиз отверстий не найден")
            profiles[-1].Name = "HoleSketch"
            result = "hole sketch"
        ''').strip()

REVOLVED_PROFILE_CODE = dedent('''
    inner_radius_mm, outer_radius_mm, length_mm = 8., 15., 40.
    sm = model.SketchManager
    axis = sm.CreateCenterLine(0., 0., 0., 0., MM(length_mm), 0.)
    points = [(inner_radius_mm, 0), (outer_radius_mm, 0),
              (outer_radius_mm, length_mm), (inner_radius_mm, length_mm)]
    lines = []
    for start, end in zip(points, points[1:] + points[:1]):
        line = sm.CreateLine(MM(start[0]), MM(start[1]), 0., MM(end[0]), MM(end[1]), 0.)
        if line is None:
            raise RuntimeError("Профиль вращения не создан")
        lines.append(line)
    with DIMENSIONS():
        for name, line, position, reference in (
                ("OuterRadius", lines[1], (MM(20), MM(20), 0.), axis),
                ("InnerRadius", lines[3], (MM(4), MM(20), 0.), axis),
                ("Length", lines[1], (MM(25), MM(20), 0.), None)):
            model.ClearSelection2(True)
            if not line.Select4(False, NOTHING):
                raise RuntimeError("Линия профиля не выбрана")
            if reference is not None and not reference.Select4(True, NOTHING):
                raise RuntimeError("Ось не выбрана")
            display = model.AddDimension2(*position)
            if display is None:
                raise RuntimeError("Управляющий размер профиля не создан")
            display.GetDimension2(0).Name = name
    model.ClearSelection2(True)
    sm.InsertSketch(True)
    profile = model.FeatureByPositionReverse(0)
    profile.Name = "BushingProfile"
    if not profile.Select2(False, 0):
        raise RuntimeError("Профиль не выбран")
    selection = GET(GET(model, "SelectionManager"), "CreateSelectData")
    selection.Mark = 16
    if not axis.Select4(True, selection):
        raise RuntimeError("Ось вращения не выбрана")
    result = "dimensioned profile ready"
''').strip()

CHAMFER_SELECTION_CODE = dedent('''
    body = model.GetBodies2(0, False)[0]
    edges = []
    for edge in GET(body, "GetEdges"):
        curve = GET(edge, "GetCurve")
        if GET(curve, "IsCircle"):
            p = curve.CircleParams
            if abs(p[6]-MM(15)) < 1e-8 and (abs(p[1]) < 1e-8 or abs(p[1]-MM(40)) < 1e-8):
                edges.append(edge)
    if len(edges) != 2:
        raise RuntimeError(f"Наружные торцевые кромки неоднозначны: {len(edges)}")
    model.ClearSelection2(True)
    for index, edge in enumerate(edges):
        if not edge.Select4(index > 0, NOTHING):
            raise RuntimeError("Кромка фаски не выбрана")
    result = "two outer edges selected"
''').strip()

SHAFT_PROFILE_CODE = dedent('''
    sm = model.SketchManager
    axis = sm.CreateCenterLine(0., MM(-10), 0., 0., MM(70), 0.)
    lines = sm.CreateCornerRectangle(0., 0., 0., MM(7.9), MM(60), 0.)
    if lines is None or len(lines) != 4:
        raise RuntimeError("Профиль вала не создан")
    with DIMENSIONS():
        for name, line, position in (("Radius", lines[0], (MM(4), MM(-10), 0.)),
                                     ("Length", lines[1], (MM(20), MM(30), 0.))):
            if not line.Select4(False, NOTHING):
                raise RuntimeError("Линия вала не выбрана")
            display = model.AddDimension2(*position)
            if display is None:
                raise RuntimeError("Размер вала не создан")
            display.GetDimension2(0).Name = name
    model.ClearSelection2(True)
    sm.InsertSketch(True)
    profile = model.FeatureByPositionReverse(0)
    profile.Name = "ShaftProfile"
    if not profile.Select2(False, 0):
        raise RuntimeError("Профиль вала не выбран")
    selection = GET(GET(model, "SelectionManager"), "CreateSelectData")
    selection.Mark = 16
    if not axis.Select4(True, selection):
        raise RuntimeError("Ось вала не выбрана")
    result = "shaft profile ready"
''').strip()

MATE_SELECTION_CODE = dedent('''
    components = model.GetComponents(False)
    if len(components) != 2:
        raise RuntimeError("Ожидалось два компонента")
    bushing = next(c for c in components if "bushing" in c.Name2.lower())
    shaft = next(c for c in components if "shaft" in c.Name2.lower())
    if not GET(bushing, "IsFixed") or GET(shaft, "IsFixed"):
        raise RuntimeError("Неверное закрепление компонентов")
    selected_faces = []
    for component, radius_mm in ((bushing, 8), (shaft, 7.9)):
        part = GET(component, "GetModelDoc2")
        faces = []
        for body in part.GetBodies2(0, False):
            for face in GET(body, "GetFaces"):
                surface = GET(face, "GetSurface")
                if mate_kind == "concentric":
                    suitable = GET(surface, "IsCylinder") and abs(surface.CylinderParams[6]-MM(radius_mm)) < 1e-8
                else:
                    suitable = GET(surface, "IsPlane") and abs(surface.PlaneParams[4]) < 1e-8
                if suitable:
                    faces.append(face)
        if len(faces) != 1:
            raise RuntimeError(f"Грани сопряжения неоднозначны: {len(faces)}")
        entity = component.GetCorrespondingEntity(faces[0])
        if entity is None:
            raise RuntimeError("Грань в контексте сборки не найдена")
        selected_faces.append(entity)
    model.ClearSelection2(True)
    selection = GET(GET(model, "SelectionManager"), "CreateSelectData")
    selection.Mark = 1
    for index, face in enumerate(selected_faces):
        if not face.Select4(index > 0, selection):
            raise RuntimeError("Грань сопряжения не выбрана")
    result = "mate faces selected"
''').strip()

DEGREES_OF_FREEDOM_CODE = dedent('''
    args = []
    for _ in range(6):
        args.extend([byref_i4(-1), VARIANT(pythoncom.VT_BYREF | pythoncom.VT_DISPATCH, None)])
    status = shaft.GetRemainingDOFs(*args)
    flags = [args[i].value for i in range(0, 12, 2)]
    vectors = [list(args[i].value.ArrayData) if args[i].value is not None else None
               for i in range(1, 12, 2)]
    if status != 0 or flags != [1, 1, 0, 0, 0, 0]:
        raise RuntimeError(f"Остаточная подвижность вала не подтверждена: {status}, {flags}")
    direction = vectors[1]
    if direction is None or abs(abs(direction[1])-1) > 1e-8 or abs(direction[0])+abs(direction[2]) > 1e-8:
        raise RuntimeError("Ось свободного вращения не совпадает с осью вала")
    result = {"status": status, "flags": flags, "vectors": vectors,
              "constrained_status": GET(shaft, "GetConstrainedStatus")}
''').strip()

class AcceptanceFailure(RuntimeError):
    pass


class DirectAcceptance:
    def __init__(self, mcp, folder):
        self.mcp, self.folder = mcp, folder
        self.title = self.session = None
        self.report = {"status": "failed", "steps": [], "cases": {}}

    async def call(self, tool, **arguments):
        result = await self.mcp.call(tool, arguments)
        self.report["steps"].append({"tool": tool, "ok": result["ok"],
                                      "uncertain": result.get("uncertain", False)})
        if not result["ok"]:
            self.report["last_error"] = result
            raise AcceptanceFailure(result.get("error") or "Ошибка инструмента.")
        return result.get("value")

    async def active(self):
        data = await self.call("connect_solidworks")
        if data.get("active_document") != self.title:
            raise AcceptanceFailure("Активный тестовый документ изменён; следующие действия запрещены.")

    async def save(self, filename):
        await self.close_session()
        await self.active()
        path = (self.folder / f"{self.folder.name}-{filename}").resolve()
        await self.call("save_document", save_as_path=str(path))
        self.title = (await self.call("connect_solidworks"))["active_document"]
        if not path.is_file():
            raise AcceptanceFailure("Тестовый файл не найден после сохранения.")
        return path

    async def close_session(self):
        if self.session:
            await self.call("close_python_session", session_id=self.session)
            self.session = None

    async def new_part(self):
        await self.close_session()
        before = (await self.call("list_open_documents"))["documents"]
        created = await self.call("create_part")
        self.title = created["created"]
        if self.title in {item["title"] for item in before}:
            raise AcceptanceFailure("Создание новой детали не подтверждено.")
        await self.active()
        features = (await self.call("get_feature_tree", limit=1000))["features"]
        plane = next(item["name"] for item in features if item["type"] == "RefPlane")
        await self.call("create_sketch", plane_name=plane)
        self.session = (await self.call("open_python_session"))["session_id"]

    async def python(self, code):
        await self.active()
        number = len(self.report["steps"])
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / f"stage-{number:03}.py").write_text(code, encoding="utf-8")
        result = await self.call("execute_python", code=code, session_id=self.session)
        self.report.setdefault("executions", []).append({"code_file": f"stage-{number:03}.py",
                                                        "document": self.title, "result": result})
        return result

    async def vertices(self, expected):
        result = await self.python('result = [list(GET(v, "GetPoint")) for v in '
                                  'GET(model.GetBodies2(0, False)[0], "GetVertices")]')
        points = ast.literal_eval(result["result"])
        spans = [(max(p[i] for p in points)-min(p[i] for p in points))*1000 for i in range(3)]
        self.report.setdefault("exact_vertex_spans_mm", []).append({"document": self.title, "spans": spans})
        if any(abs(a-b) > .001 for a, b in zip(spans, expected, strict=True)):
            raise AcceptanceFailure(f"Размеры по вершинам отличаются: {spans} != {expected}")

    async def inspect(self, checks):
        await self.active()
        data = await self.call("inspect_cad", **inspection_request(checks))
        results = evaluate_checks(checks, data)
        self.report.setdefault("evidence", []).append({"document": self.title, "checks": results})
        if not all(check["passed"] for check in results):
            raise AcceptanceFailure("Заявленные свойства тестовой детали не подтверждены.")
        return data

    async def bracket(self):
        await self.new_part()
        code = get_example("sheet-open-profile")["code"]
        await self.python(code)
        await self.inspect(BRACKET_CHECKS)
        await self.vertices([60, 40, 80])
        data = await self.call("inspect_cad", sections=["features", "dimensions", "surfaces"])
        self.report["cases"]["bracket_base"] = data
        await self.python(dedent('''
            features = []
            f = GET(model, "FirstFeature")
            while f is not None:
                if GET(f, "GetTypeName2") == "ProfileFeature":
                    features.append(f)
                f = GET(f, "GetNextFeature")
            if len(features) != 1:
                raise RuntimeError("Эскиз профиля неоднозначен")
            features[0].Name = "BracketProfile"
            depth = model.Parameter("D2@BracketBase")
            if depth is None or abs(depth.SystemValue-MM(80)) > 1e-6:
                raise RuntimeError("Управляющая глубина 80 мм не найдена")
            if not GET(model, "EditRebuild3"):
                raise RuntimeError("Ошибка перестроения")
            result = {"depth_mm": depth.SystemValue*1000}
        ''').strip())
        await self.inspect([{"kind": "dimension", "name": "D2@BracketBase", "value": 80, "unit": "mm"}])
        # Hole stage deliberately discovers a planar face before sketch creation.
        await self.python(HOLE_CODE)
        await self.active()
        cut = await self.call("cut_through")
        self.report["cases"]["bracket_cut"] = cut
        await self.inspect(BRACKET_CHECKS + [
            {"kind": "cylinder", "radius_mm": 4.5, "count": 1,
             "axis_point_mm": [30, 0, z], "axis_direction": [0, 1, 0]} for z in (20, 60)])
        await self.inspect([{"kind": "dimension", "name": f"Hole{i}{axis}@HoleSketch",
                            "value": value, "unit": "mm"}
                           for i, z in ((1, 20), (2, 20))
                           for axis, value in (("X", 30), ("Z", z), ("Diameter", 9))])
        await self.active()
        await self.call("set_dimension", name="D2@BracketBase", value_mm=100)
        await self.inspect([{"kind": "dimension", "name": "D2@BracketBase", "value": 100, "unit": "mm"}])
        await self.inspect([{"kind": "cylinder", "radius_mm": 4.5, "count": 1,
                            "axis_point_mm": [30, 0, z], "axis_direction": [0, 1, 0]}
                           for z in (20, 80)])
        await self.vertices([60, 40, 100])
        await self.python(dedent('''
            flat = None
            f = GET(model, "FirstFeature")
            while f is not None:
                if GET(f, "GetTypeName2") == "FlatPattern":
                    flat = f
                f = GET(f, "GetNextFeature")
            if flat is None:
                raise RuntimeError("Развёртка не найдена")
            flat.Name = "FlatPattern"
            if not flat.SetSuppression2(1, 1, NOTHING) or not GET(model, "EditRebuild3"):
                raise RuntimeError("Развёртка не перестроена")
            result = "flat pattern active"
        ''').strip())
        flat = await self.call("inspect_cad", sections=["surfaces", "bodies"])
        self.report["cases"]["bracket_flat"] = flat
        if (len(flat["bodies"]) != 1 or any(s.get("kind") == "cylinder" and
                abs(s.get("radius_mm", 0)-2) < .001 for s in flat["surfaces"])):
            raise AcceptanceFailure("Развёртка не подтверждена.")
        await self.python('''
if not flat.SetSuppression2(0, 1, NOTHING) or not GET(model, "EditRebuild3"):
    raise RuntimeError("Согнутое состояние не восстановлено")
result = "folded state restored"
'''.strip())
        await self.save("bracket.sldprt")
        self.report["cases"]["bracket"] = "passed_declared_checks"

    async def bushing(self):
        await self.new_part()
        await self.python(REVOLVED_PROFILE_CODE)
        await self.python(get_example("revolution")["code"])
        await self.inspect([{"kind": "body_count", "expected": 1},
                            {"kind": "feature", "name": "BushingRevolve", "type": "Revolution"}])
        await self.python(CHAMFER_SELECTION_CODE)
        await self.python(get_example("chamfer")["code"])
        await self.inspect([{"kind": "feature", "name": "OuterChamfer"},
                           {"kind": "body_count", "expected": 1},
                           {"kind": "dimension", "name": "D1@OuterChamfer", "value": 1, "unit": "mm"},
                           {"kind": "dimension", "name": "D2@OuterChamfer", "value": 45, "unit": "deg"}] + [
            {"kind": "dimension", "name": f"{name}@BushingProfile", "value": value, "unit": "mm"}
            for name, value in (("OuterRadius", 15), ("InnerRadius", 8), ("Length", 40))] + [
            {"kind": "cylinder", "radius_mm": radius, "count": 1,
             "axis_point_mm": [0, 0, 0], "axis_direction": [0, 1, 0]} for radius in (8, 15)])
        self.report["cases"]["bushing_dimensions"] = await self.call("inspect_cad", sections=["dimensions"])
        self.bushing_path = await self.save("bushing.sldprt")
        self.report["cases"]["bushing"] = "passed_declared_checks"

    async def assembly(self):
        await self.new_part()
        await self.python(SHAFT_PROFILE_CODE)
        await self.python(get_example("revolution")["code"].replace("BushingRevolve", "ShaftRevolve"))
        await self.inspect([{"kind": "body_count", "expected": 1},
            {"kind": "cylinder", "radius_mm": 7.9, "count": 1},
            {"kind": "dimension", "name": "Length@ShaftProfile", "value": 60, "unit": "mm"}])
        shaft_path = await self.save("shaft.sldprt")
        created = await self.call("create_assembly")
        self.title = created["created"]
        await self.active()
        self.session = (await self.call("open_python_session"))["session_id"]
        await self.call("add_component", path=str(self.bushing_path))
        await self.call("add_component", path=str(shaft_path))
        components = (await self.call("inspect_cad", sections=["components"]))["components"]
        self.report["cases"]["inserted_components"] = components
        bushing = next(c for c in components if "bushing" in c["name"].lower())
        shaft = next(c for c in components if "shaft" in c["name"].lower())
        for kind in ("concentric", "coincident"):
            await self.python(f"mate_kind = {kind!r}\n" + MATE_SELECTION_CODE)
            await self.call("add_mate", mate_type=kind, alignment="aligned")
        checks = [{"kind": "component", "name": bushing["name"], "fixed": True},
                  {"kind": "component", "name": shaft["name"], "fixed": False}] + [
                  {"kind": "mate", "mate_type": n, "components": [bushing["name"], shaft["name"]]}
                  for n in (0, 1)]
        await self.inspect(checks)
        await self.python(DEGREES_OF_FREEDOM_CODE)
        await self.python(dedent('''
            part = GET(bushing, "GetModelDoc2")
            radius = part.Parameter("OuterRadius@BushingProfile")
            if radius is None or abs(radius.SystemValue-MM(15)) > 1e-8:
                raise RuntimeError("Исходный радиус втулки не подтверждён")
            radius.SystemValue = MM(17)
            if not GET(part, "EditRebuild3") or not GET(model, "EditRebuild3"):
                raise RuntimeError("Ошибка перестроения втулки или сборки")
            radii = [GET(face, "GetSurface").CylinderParams[6]*1000
                     for body in part.GetBodies2(0, False) for face in GET(body, "GetFaces")
                     if GET(GET(face, "GetSurface"), "IsCylinder")]
            if len(radii) != 2 or any(abs(a-b) > .001 for a, b in zip(sorted(radii), [8, 17])):
                raise RuntimeError("Цилиндры втулки после редактирования не подтверждены")
            errors, warnings = byref_i4(), byref_i4()
            if not part.Save3(1, errors, warnings):
                raise RuntimeError(f"Втулка не сохранена: {errors.value}")
            result = {"outer_radius_mm": radius.SystemValue*1000}
        ''').strip())
        await self.inspect(checks)
        await self.python(DEGREES_OF_FREEDOM_CODE)
        await self.save("bushing-shaft.sldasm")
        self.report["cases"]["assembly"] = "passed_declared_checks"


async def direct(mcp, folder, case="all"):
    test = DirectAcceptance(mcp, folder)
    try:
        await test.call("connect_solidworks")
        if case in {"all", "bracket"}:
            await test.bracket()
        if case in {"all", "bushing"}:
            await test.bushing()
        if case == "all":
            await test.assembly()
        test.report["status"] = "passed"
        test.report["scope"] = case
    except Exception as error:
        test.report["error"] = str(error)
        test.report["error_type"] = type(error).__name__
    finally:
        try:
            await test.close_session()
        except AcceptanceFailure as error:
            test.report["cleanup_error"] = str(error)
    return test.report


async def main():
    parser = argparse.ArgumentParser(description="Приёмка поэтапного CAD-агента на новых документах.")
    parser.add_argument("--mutate-new-documents", action="store_true",
                        help="Разрешить создание новых тестовых деталей; существующие не изменяются.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", choices=["all", "bracket", "bushing"], default="all")
    args = parser.parse_args()
    if not args.mutate_new_documents:
        parser.error("Укажите --mutate-new-documents для живого построения.")
    folder = args.output or Path(tempfile.mkdtemp(prefix="cadnext-staged-"))
    folder.mkdir(parents=True, exist_ok=True)
    if any(folder.iterdir()):
        parser.error("Каталог результатов должен быть пустым.")
    async with SolidWorksMcp() as mcp:
        report = await direct(mcp, folder, args.case)
        # Publish evidence before transport shutdown, which must await an unknown worker.
        (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"каталог": str(folder), **report}, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
