"""Explicit live acceptance, confined to new documents and a fresh output folder."""

import argparse
import ast
import asyncio
import json
import math
import tempfile
import time
from pathlib import Path
from textwrap import dedent

from cadnext.cad_examples import HOLE_PROFILE_CODE as HOLE_CODE
from cadnext.cad_examples import MATE_SELECTION_CODE, REVOLVED_PROFILE_CODE, SHAFT_PROFILE_CODE
from cadnext.cad_reference import get_example
from cadnext.construction import evaluate_checks, inspection_request
from cadnext.mcp import SolidWorksMcp

BRACKET_CHECKS = [
    {"kind": "feature", "name": "BracketBase", "type": "SMBaseFlange"},
    {"kind": "body_count", "expected": 1},
    {"kind": "sheet_metal", "thickness_mm": 2, "bend_radius_mm": 2},
]


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



def axial_joint_evidence(data):
    """Prove this two-body cylindrical joint from documented constraint geometry."""
    def require(condition):
        if not condition:
            raise AcceptanceFailure("Остаточная подвижность вала не подтверждена геометрией сопряжений.")

    require(not data.get("truncated"))
    components, mates = data.get("components", []), data.get("mates", [])
    require(len(components) == 2 and len(mates) == 2)
    require(all(not c["suppressed"] for c in components))
    require(sum(c["fixed"] for c in components) == 1)
    fixed = next(c for c in components if c["fixed"])
    require("bushing" in fixed["name"].lower())
    pair = sorted(c["name"] for c in components)
    require(sorted(m["type"] for m in mates) == [0, 1])
    for mate in mates:
        require(mate["suppressed"] is False and mate["error_code"] == 0)
        require(sorted(mate["components"]) == pair and len(mate["entities"]) == 2)
    concentric = next(m for m in mates if m["type"] == 1)
    coincident = next(m for m in mates if m["type"] == 0)
    require(concentric.get("lock_rotation") is False)
    # Installed SW2025 swMateEntityTypes_e: swMateCylinder=4, swMatePlane=3.
    require(all(e["reference_type"] == 4 for e in concentric["entities"]))
    require(all(e["reference_type"] == 3 for e in coincident["entities"]))
    entities = concentric["entities"] + coincident["entities"]
    vectors = []
    for entity in entities:
        direction, point = entity.get("direction"), entity.get("point_mm")
        require(isinstance(direction, list) and len(direction) == 3)
        require(isinstance(point, list) and len(point) == 3)
        require(all(type(v) in {int, float} and math.isfinite(v) for v in direction + point))
        length = math.hypot(*direction)
        require(length > 1e-12)
        vectors.append([v/length for v in direction])
    axis = vectors[0]

    def dot(a, b):
        return sum(x*y for x, y in zip(a, b, strict=True))

    require(all(abs(dot(axis, vector)) >= math.cos(math.radians(.01)) for vector in vectors[1:]))
    delta = [a-b for a, b in zip(entities[0]["point_mm"], entities[1]["point_mm"], strict=True)]
    along = dot(delta, axis)
    require(math.hypot(*(d-along*v for d, v in zip(delta, axis, strict=True))) <= .001)
    delta = [a-b for a, b in zip(entities[2]["point_mm"], entities[3]["point_mm"], strict=True)]
    require(abs(dot(delta, axis)) <= .001)
    return {"method": "analytic_cylindrical_joint", "remaining_translations": 0,
            "remaining_rotations": 1, "axis_direction": axis}

class AcceptanceFailure(RuntimeError):
    pass


class DirectAcceptance:
    def __init__(self, mcp, folder):
        self.mcp, self.folder = mcp, folder
        self.title = self.session = None
        self.report = {"status": "failed", "steps": [], "cases": {}}

    async def call(self, tool, **arguments):
        started = time.perf_counter()
        result = await self.mcp.call(tool, arguments)
        self.report["steps"].append({"tool": tool, "ok": result["ok"],
                                      "duration_seconds": time.perf_counter()-started,
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
        example = get_example("sheet-open-profile")
        await self.python(example["preparation_code"])
        await self.inspect(example["preparation_checks"])
        await self.python(example["code"])
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
        for path in (self.bushing_path, shaft_path):
            await self.python(f"part_path = {str(path)!r}\n" + get_example("assembly-insert")["code"])
        components = (await self.call("inspect_cad", sections=["components"]))["components"]
        self.report["cases"]["inserted_components"] = components
        bushing = next(c for c in components if "bushing" in c["name"].lower())
        shaft = next(c for c in components if "shaft" in c["name"].lower())
        for kind in ("concentric", "coincident"):
            await self.python(f"mate_kind = {kind!r}\n" + MATE_SELECTION_CODE)
            recipe = get_example("assembly-mates")["code"]
            if kind == "coincident":
                recipe = recipe.replace('mate_type, mate_name = 1, "Concentric"',
                                        'mate_type, mate_name = 0, "Coincident"')
            await self.python(recipe)
        checks = [{"kind": "component", "name": bushing["name"], "fixed": True},
                  {"kind": "component", "name": shaft["name"], "fixed": False}] + [
                  {"kind": "mate", "mate_type": n, "components": [bushing["name"], shaft["name"]]}
                  for n in (0, 1)]
        await self.inspect(checks)
        await self.save("bushing-shaft.sldasm")
        self.session = (await self.call("open_python_session"))["session_id"]
        data = await self.call("inspect_cad", sections=["components", "mates"])
        self.report.setdefault("mobility", []).append({**axial_joint_evidence(data), "observations": data})
        await self.python(dedent('''
            bushing = next(c for c in model.GetComponents(False) if "bushing" in c.Name2.lower())
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
        await self.save("bushing-shaft.sldasm")
        self.session = (await self.call("open_python_session"))["session_id"]
        data = await self.call("inspect_cad", sections=["components", "mates"])
        self.report["mobility"].append({**axial_joint_evidence(data), "observations": data})
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
