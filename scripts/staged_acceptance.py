"""Explicit live acceptance, confined to new documents and a fresh output folder."""

import argparse
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
            utility = GET(swApp, "GetMathUtility")
            for z_mm in (20., 60.):
                local = utility.CreatePoint((MM(30), face_y, MM(z_mm))).MultiplyTransform(transform).ArrayData
                circle = model.SketchManager.CreateCircleByRadius(*local, MM(4.5))
                if circle is None:
                    raise RuntimeError("Окружность не создана")
                if not circle.Select4(False, NOTHING):
                    raise RuntimeError("Окружность не выбрана")
                with DIMENSIONS():
                    dim = model.AddDiameterDimension2(local[0]+MM(10), local[1]+MM(10), 0.)
                if dim is None:
                    raise RuntimeError("Диаметр не создан")
            model.ClearSelection2(True)
            model.SketchManager.InsertSketch(True)
            result = "hole sketch"
        ''').strip()

class AcceptanceFailure(RuntimeError):
    pass


class DirectAcceptance:
    def __init__(self, mcp, folder):
        self.mcp, self.folder = mcp, folder
        self.title = self.session = None
        self.report = {"status": "failed", "steps": [], "cases": {}}

    async def call(self, name, **arguments):
        result = await self.mcp.call(name, arguments)
        self.report["steps"].append({"tool": name, "ok": result["ok"],
                                      "uncertain": result.get("uncertain", False)})
        if not result["ok"]:
            raise AcceptanceFailure(result.get("error") or "Ошибка инструмента.")
        return result.get("value")

    async def active(self):
        data = await self.call("connect_solidworks")
        if data.get("active_document") != self.title:
            raise AcceptanceFailure("Активный тестовый документ изменён; следующие действия запрещены.")

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
        return await self.call("execute_python", code=code, session_id=self.session)

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
        await self.active()
        await self.call("set_dimension", name="D2@BracketBase", value_mm=100)
        await self.inspect([{"kind": "dimension", "name": "D2@BracketBase", "value": 100, "unit": "mm"}])
        self.report["cases"]["bracket"] = "passed_declared_checks"


async def direct(mcp, folder):
    test = DirectAcceptance(mcp, folder)
    try:
        await test.call("connect_solidworks")
        await test.bracket()
        # Additional cases are recorded individually; never infer their success from a base flange.
        test.report["status"] = "partial"
        test.report["remaining"] = ["bushing", "assembly", "outer_leg_geometry", "hole_position_dimensions"]
    except (AcceptanceFailure, StopIteration) as error:
        test.report["error"] = str(error)
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
    args = parser.parse_args()
    if not args.mutate_new_documents:
        parser.error("Укажите --mutate-new-documents для живого построения.")
    folder = args.output or Path(tempfile.mkdtemp(prefix="cadnext-staged-"))
    folder.mkdir(parents=True, exist_ok=True)
    if any(folder.iterdir()):
        parser.error("Каталог результатов должен быть пустым.")
    async with SolidWorksMcp() as mcp:
        report = await direct(mcp, folder)
        # Publish evidence before transport shutdown, which must await an unknown worker.
        (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"каталог": str(folder), **report}, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
