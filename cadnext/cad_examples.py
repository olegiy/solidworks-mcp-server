"""Original, inspectable Python recipes. No recipe is live-certified by default."""

from textwrap import dedent


def _example(example_id, title, keywords, parameters, prerequisites, api_symbols,
             expected_features, checks, code):
    return {"example_id": example_id, "title": title, "keywords": keywords,
            "parameters": parameters, "prerequisites": prerequisites,
            "api_symbols": api_symbols, "expected_features": expected_features,
            "checks": checks, "code": dedent(code).strip(), "verified_versions": []}


EXAMPLES = [
    _example("sheet-open-profile", "Открытый профиль листовой детали",
             "sheet metal open profile кронштейн листовой сгиб", {"thickness_mm": 2,
             "inside_radius_mm": 2, "outer_legs_mm": [60, 40], "length_mm": 80},
             ["Новая деталь, активный эскиз на плоскости Спереди; оси эскиза проверены.",
              "Полки заданы по наружным поверхностям; положение толщины проверить после построения."],
             ["ISketchManager.CreateLine", "IFeatureManager.InsertSheetMetalBaseFlange2",
              "IModelDoc2.AddDimension2", "ISketchManager.FullyDefineSketch"],
             ["BracketProfile", "BracketBase", "SheetMetal", "FlatPattern"],
             ["Управляющие размеры полок и длины", "Толщина 2 мм, внутренний радиус 2 мм"],
             '''
             thickness_mm, radius_mm, length_mm = 2., 2., 80.
             leg_a_mm, leg_b_mm = 60., 40.
             sm, fm = model.SketchManager, model.FeatureManager
             # The open contour is on the inside; outward thickness adds T to each leg.
             line_a = sm.CreateLine(MM(leg_a_mm-thickness_mm), 0., 0., 0., 0., 0.)
             line_b = sm.CreateLine(0., 0., 0., 0., MM(leg_b_mm-thickness_mm), 0.)
             if line_a is None or line_b is None:
                 raise RuntimeError("Профиль не создан")
             with DIMENSIONS():
                 if not line_a.Select4(False, NOTHING):
                     raise RuntimeError("Не выбрана первая линия")
                 dim_a = model.AddDimension2(MM(30), MM(-10), 0.)
                 if dim_a is None:
                     raise RuntimeError("Не создан управляющий размер полки")
                 dim_a.GetDimension2(0).Name = "LegAInside"
                 if not line_b.Select4(False, NOTHING):
                     raise RuntimeError("Не выбрана вторая линия")
                 dim_b = model.AddDimension2(MM(-10), MM(20), 0.)
                 if dim_b is None:
                     raise RuntimeError("Не создан второй размер полки")
                 dim_b.GetDimension2(0).Name = "LegBInside"
             model.ClearSelection2(True)
             GET(sm, "InsertSketch", True)
             profile = model.FeatureByPositionReverse(0)
             if profile is None or GET(profile, "GetTypeName2") != "ProfileFeature":
                 raise RuntimeError("Эскиз профиля не найден")
             profile.Name = "BracketProfile"
             if not profile.Select2(False, 0):
                 raise RuntimeError("Эскиз профиля не выбран")
             base = fm.InsertSheetMetalBaseFlange2(MM(thickness_mm), False, MM(radius_mm),
                 MM(length_mm), 0., False, 0, 0, 0, NOTHING, True, 0, 0., 0., 0.,
                 False, True, True, True)
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
             ["ISketch.ModelToSketchTransform", "IMathPoint.MultiplyTransform",
              "ISketchManager.CreateCircleByRadius", "IFeatureManager.FeatureCut4"],
             ["HoleSketch", "HoleCut"], ["Две цилиндрические поверхности Ø9 и положение их осей"],
             '''
             sketch = GET(model.SketchManager, "ActiveSketch")
             if sketch is None:
                 raise RuntimeError("Нет активного эскиза")
             transform = sketch.ModelToSketchTransform
             utility = GET(swApp, "GetMathUtility")
             diameter_mm = 9.
             centers_model_mm = [(30., 0., 20.), (30., 0., 60.)]
             circles = []
             for center in centers_model_mm:
                 point = utility.CreatePoint(tuple(MM(v) for v in center))
                 local = point.MultiplyTransform(transform).ArrayData
                 circle = model.SketchManager.CreateCircleByRadius(*local, MM(diameter_mm/2))
                 if circle is None:
                     raise RuntimeError("Окружность не создана")
                 circles.append(circle)
             result = {"circles": len(circles), "coordinates": "model_to_sketch"}
             '''),
    _example("revolution", "Втулка вращением размерного профиля",
             "bushing revolution втулка вращение", {"outer_diameter_mm": 30,
             "inner_diameter_mm": 16, "length_mm": 40},
             ["Активный замкнутый прямоугольный профиль с управляющими размерами.",
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
             error = byref_i4()
             mate = model.AddMate5(mate_type, 0, False, 0., 0., 0., 0., 0., 0.,
                 0., 0., False, False, 0, error)
             if mate is None or error.value != 0:
                 raise RuntimeError(f"Ошибка сопряжения: {error.value}")
             result = {"mate_type": mate_type, "name": mate_name}
             '''),
]
