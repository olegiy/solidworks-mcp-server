"""Original Python recipes; version labels require recorded direct live execution."""

from textwrap import dedent

HOLE_PROFILE_CODE = dedent('''
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
            sketch_to_model = list(transform.Inverse.ArrayData)
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
                    position = TRANSFORM_POINT([local[0]+MM(10), local[1]+MM(10), 0.], sketch_to_model)
                    dim = model.AddDiameterDimension2(*position)
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
                        display = method(*TRANSFORM_POINT(list(position), sketch_to_model))
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

# scripts.staged_acceptance, live-direct-library-clean; see docs/verification.md.
_LIVE_EXECUTED = {"sheet-open-profile", "revolution", "chamfer",
                  "assembly-insert", "assembly-mates"}


def _example(example_id, title, keywords, parameters, prerequisites, api_symbols,
             expected_features, checks, code):
    result = {"example_id": example_id, "title": title, "keywords": keywords,
            "parameters": parameters, "prerequisites": prerequisites,
            "api_symbols": api_symbols, "expected_features": expected_features,
            "checks": checks, "code": dedent(code).strip(),
            "verified_versions": ["SolidWorks 2025 SP5 (33.5.0)"] if example_id in _LIVE_EXECUTED else []}
    if example_id == "revolution":
        result.update(preparation_code=REVOLVED_PROFILE_CODE, preparation_checks=[
            {"kind": "dimension", "name": f"{name}@BushingProfile", "value": value, "unit": "mm"}
            for name, value in (("OuterRadius", 15), ("InnerRadius", 8), ("Length", 40))])
    if example_id == "sheet-open-profile":
        profile, marker, base = result["code"].partition('allowance = GET(fm, "CreateCustomBendAllowance")')
        if not marker:
            raise ValueError("Sheet recipe is missing its stage boundary")
        result.update(preparation_code=profile + '\nresult = "profile ready"', code=marker + base,
            preparation_checks=[{"kind": "feature", "name": "BracketProfile", "type": "ProfileFeature"},
                                {"kind": "body_count", "expected": 0}] + [
                {"kind": "dimension", "name": f"{name}@BracketProfile", "value": value, "unit": "mm"}
                for name, value in (("LegAOuter", 60), ("LegBOuter", 40))])
    if example_id == "holes-transform":
        result.update(code=HOLE_PROFILE_CODE, next_tool="cut_through", prerequisites=[
            "Новая тестовая листовая основа: наружные полки 60×40, длина 80, T2/R2; эскиз закрыт.",
            "Система модели: полка вдоль X, длина вдоль Z. Сначала прочитай поверхности/вершины.",
            "Код создаёт размерный эскиз на найденной грани; затем вызови cut_through отдельным этапом.",
            "Второй центр привязан размером 20 мм к дальнему торцу: изменение длины сохранит отступ."],
            declared_checks=[{"kind": "feature", "name": "HoleSketch", "type": "ProfileFeature"}] + [
                {"kind": "dimension", "name": f"{name}@HoleSketch", "value": value, "unit": "mm"}
                for name, value in (("Hole1Diameter", 9), ("Hole2Diameter", 9),
                                    ("Hole1X", 30), ("Hole2X", 30), ("Hole1Z", 20), ("Hole2Z", 20))])
        result["api_symbols"].extend(["IEntity.Select4", "IModelDoc2.AddDiameterDimension2",
            "IModelDoc2.AddHorizontalDimension2", "IModelDoc2.AddVerticalDimension2"])
        result["prerequisites"].append(
            "Выбор вершин/граней: IEntity.Select4; сигнатуры IVertex.Select4 в typelib нет.")
    if example_id == "assembly-insert":
        result.update(shaft_preparation_code=SHAFT_PROFILE_CODE,
            shaft_prerequisites=["НОВАЯ деталь вала с активным эскизом на Спереди.",
                "Код создаёт профиль Ø15,8×60 с осью; затем отдельным этапом выполни code рецепта revolution.",
                "При вращении назови элемент ShaftRevolve; сохрани новую деталь перед вставкой в сборку."],
            shaft_checks=[{"kind": "dimension", "name": "Length@ShaftProfile", "value": 60, "unit": "mm"},
                          {"kind": "body_count", "expected": 1},
                          {"kind": "cylinder", "radius_mm": 7.9, "count": 1}])
    if example_id == "assembly-mates":
        result.update(selection_code=MATE_SELECTION_CODE,
            selection_parameters={"mate_kind": ["concentric", "coincident"]},
            selection_prerequisites=["Активная новая сборка: компоненты с bushing/shaft в именах.",
                "Втулка закреплена, вал свободен. Втулка Øвнутр16, вал Ø15,8; общая ось Y, торцы y=0.",
                "Сначала задай mate_kind и выполни selection_code отдельным этапом, затем add_mate с этим типом.",
                "Для coincident заново выполни selection_code с mate_kind='coincident'.",
                "Прочитай inspect_cad components/mates, проверь lock_rotation=False у концентрического сопряжения."])
    return result


EXAMPLES = [
    _example("sheet-open-profile", "Открытый профиль листовой детали",
             "sheet metal open profile кронштейн листовой сгиб", {"thickness_mm": 2,
             "inside_radius_mm": 2, "outer_legs_mm": [60, 40], "length_mm": 80},
             ["Новая деталь, активный эскиз на плоскости Спереди; оси эскиза проверены.",
              "Выполни preparation_code отдельным этапом, прочитай preparation_checks, затем code новым этапом.",
              "Полки заданы по наружным поверхностям; положение толщины проверить после построения."],
             ["ISketchManager.CreateLine", "IFeatureManager.InsertSheetMetalBaseFlange2",
              "IModelDoc2.AddDimension2", "ISketchManager.FullyDefineSketch"],
             ["BracketProfile", "BracketBase", "SheetMetal", "FlatPattern"],
             ["Управляющие размеры полок и длины", "Толщина 2 мм, внутренний радиус 2 мм"],
             '''
             thickness_mm, radius_mm, length_mm = 2., 2., 80.
             leg_a_mm, leg_b_mm = 60., 40.
             sm, fm = model.SketchManager, model.FeatureManager
             # With this contour orientation and ThickenDir=False, thickness grows inward.
             line_a = sm.CreateLine(MM(leg_a_mm), 0., 0., 0., 0., 0.)
             line_b = sm.CreateLine(0., 0., 0., 0., MM(leg_b_mm), 0.)
             if line_a is None or line_b is None:
                 raise RuntimeError("Профиль не создан")
             with DIMENSIONS():
                 if not line_a.Select4(False, NOTHING):
                     raise RuntimeError("Не выбрана первая линия")
                 dim_a = model.AddDimension2(MM(30), MM(-10), 0.)
                 if dim_a is None:
                     raise RuntimeError("Не создан управляющий размер полки")
                 dim_a.GetDimension2(0).Name = "LegAOuter"
                 if not line_b.Select4(False, NOTHING):
                     raise RuntimeError("Не выбрана вторая линия")
                 dim_b = model.AddDimension2(MM(-10), MM(20), 0.)
                 if dim_b is None:
                     raise RuntimeError("Не создан второй размер полки")
                 dim_b.GetDimension2(0).Name = "LegBOuter"
             model.ClearSelection2(True)
             GET(sm, "InsertSketch", True)
             profile = model.FeatureByPositionReverse(0)
             if profile is None or GET(profile, "GetTypeName2") != "ProfileFeature":
                 raise RuntimeError("Эскиз профиля не найден")
             profile.Name = "BracketProfile"
             if not profile.Select2(False, 0):
                 raise RuntimeError("Эскиз профиля не выбран")
             allowance = GET(fm, "CreateCustomBendAllowance")
             allowance.KFactor = 0.5
             base = fm.InsertSheetMetalBaseFlange2(MM(thickness_mm), False, MM(radius_mm),
                 MM(length_mm), 0., False, 0, 0, 1, allowance, False, 0, MM(.5), MM(.5), .5,
                 True, False, False, False)
             if base is None:
                 raise RuntimeError("Базовая кромка не создана")
             base.Name = "BracketBase"
             result = {"feature": base.Name, "requires_geometry_readback": True}
             '''),
    _example("holes-transform", "Отверстия в системе координат грани",
             "holes transform отверстия координаты sketch", {"diameter_mm": 9,
             "positions_model_mm": [[30, 0, 20], [30, 0, 60]]},
             ["Активен эскиз на выбранной плоской грани; точки заданы в системе модели.",
              "Затем нужны управляющие размеры центра и диаметра, нормальный сквозной вырез."],
             ["ISketch.ModelToSketchTransform", "IMathTransform.ArrayData",
              "ISketchManager.CreateCircleByRadius", "IFeatureManager.FeatureCut4"],
             ["HoleSketch", "HoleCut"], ["Две цилиндрические поверхности Ø9 и положение их осей"],
             '''
             sketch = GET(model.SketchManager, "ActiveSketch")
             if sketch is None:
                 raise RuntimeError("Нет активного эскиза")
             transform = sketch.ModelToSketchTransform
             diameter_mm = 9.
             centers_model_mm = [(30., 0., 20.), (30., 0., 60.)]
             circles = []
             for center in centers_model_mm:
                 local = TRANSFORM_POINT([MM(v) for v in center], list(transform.ArrayData))
                 circle = model.SketchManager.CreateCircleByRadius(*local, MM(diameter_mm/2))
                 if circle is None:
                     raise RuntimeError("Окружность не создана")
                 circles.append(circle)
             result = {"circles": len(circles), "coordinates": "model_to_sketch"}
             '''),
    _example("revolution", "Втулка вращением размерного профиля",
             "bushing revolution втулка вращение", {"outer_diameter_mm": 30,
             "inner_diameter_mm": 16, "length_mm": 40},
             ["Новая деталь с активным эскизом Спереди: сначала выполни preparation_code отдельным этапом и прочитай preparation_checks.",
              "Затем выполни code отдельным этапом: он требует подготовленный замкнутый размерный профиль.",
              "Ось вращения выбрана с mark=16; размеры радиусов 15 и 8 мм, длины 40 мм."],
             ["IFeatureManager.FeatureRevolve2"], ["BushingRevolve"],
             ["Одно тело", "Управляющие OD/ID/Length", "Цилиндры Ø30 и Ø16"],
             '''
             import math
             feature = model.FeatureManager.FeatureRevolve2(True, True, False, False,
                 False, False, 0, 0, 2*math.pi, 0., False, False, 0., 0., 0,
                 0., 0., True, True, True)
             if feature is None:
                 raise RuntimeError("Вращение не создано")
             feature.Name = "BushingRevolve"
             result = feature.Name
             '''),
    _example("chamfer", "Размерная фаска на выбранных кромках",
             "chamfer фаска", {"distance_mm": 1, "angle_deg": 45},
             ["Нужные наружные торцевые кромки выбраны по прочитанной геометрии.",
              "Сигнатура и swChamferType_e сверены с локальной библиотекой типов."],
             ["IFeatureManager.InsertFeatureChamfer"], ["OuterChamfer"],
             ["Управляющий размер фаски 1 мм и угол 45°"],
             '''
             import math
             distance_mm, angle_deg = 1., 45.
             feature = model.FeatureManager.InsertFeatureChamfer(4, 1, MM(distance_mm),
                 math.radians(angle_deg), 0., 0., 0., 0.)
             if feature is None:
                 raise RuntimeError("Фаска не создана")
             feature.Name = "OuterChamfer"
             result = feature.Name
             '''),
    _example("assembly-insert", "Вставка компонента в новую сборку",
             "assembly component insert сборка компонент вставка", {"path": "<saved-part.sldprt>"},
             ["Новая сборка активна; путь part_path задан, деталь сохранена и открыта."],
             ["IAssemblyDoc.AddComponent5", "IComponent2.Transform2"], ["Bushing-1"],
             ["Путь и количество компонентов", "Матрица положения и закрепление втулки"],
             '''
             from pathlib import Path
             if not Path(part_path).is_file():
                 raise RuntimeError("Файл компонента отсутствует")
             component = model.AddComponent5(part_path, 0, "", False, "", 0., 0., 0.)
             if component is None:
                 raise RuntimeError("Компонент не вставлен")
             result = {"component": component.Name2, "path": GET(component, "GetPathName")}
             '''),
    _example("assembly-mates", "Сопряжения втулки и вала",
             "assembly concentric coincident mates сборка вал сопряжения", {"mate_type": 1},
             ["Для концентричности выбраны две цилиндрические грани с mark=1.",
              "Для совпадения торцов отдельно выбрать две плоские грани; втулку закрепить.",
              "Для совпадения повторить фрагмент с mate_type=0 и новым именем."],
             ["IAssemblyDoc.AddMate5", "IMate2.GetMateEntityCount", "IComponent2.GetMates"],
             ["Concentric", "Coincident"], ["Два сопряжения без ошибок", "Только осевое вращение вала"],
             '''
             mate_type, mate_name = 1, "Concentric"
             swAddMateError_NoError = 1
             error = byref_i4()
             mate = model.AddMate5(mate_type, 0, False, 0., 0., 0., 0., 0., 0.,
                 0., 0., False, False, 0, error)
             if mate is None or error.value != swAddMateError_NoError:
                 raise RuntimeError(f"Ошибка сопряжения: {error.value}")
             result = {"mate_type": mate_type, "name": mate_name}
             '''),
]
