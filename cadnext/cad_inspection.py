"""Bounded read-only CAD inspectors. Invoke exclusively through the STA worker."""

import math

SECTIONS = {"features", "dimensions", "bodies", "sheet_metal", "surfaces", "components", "mates"}
LIMIT = 1000


def _features(model, get, result):
    visited = set()

    def walk(feature, sub=False):
        while feature is not None:
            name = get(feature, "Name")
            if name in visited:
                return
            if len(visited) >= LIMIT:
                result["truncated"] = True
                return
            visited.add(name)
            yield feature
            # Real COM supports subfeatures; callers requesting trees must provide it.
            if hasattr(feature, "GetFirstSubFeature"):
                yield from walk(get(feature, "GetFirstSubFeature"), True)
            feature = get(feature, "GetNextSubFeature" if sub else "GetNextFeature")
    return walk(get(model, "FirstFeature"))


def inspect_model(model, sections, dimension_names, get):
    if (not isinstance(sections, list) or not sections or len(sections) > len(SECTIONS)
            or not set(sections) <= SECTIONS or len(dimension_names) > LIMIT
            or any(not isinstance(name, str) or not name or len(name) > 200 for name in dimension_names)):
        raise ValueError("Некорректные разделы инспекции.")
    kind = int(get(model, "GetType"))
    result = {"document": get(model, "GetTitle"), "document_type": kind, "truncated": False}
    if set(sections) & {"features", "sheet_metal", "mates"} or (
            "dimensions" in sections and not dimension_names):
        features = list(_features(model, get, result))
    else:
        features = []
    if "features" in sections:
        result["features"] = [{"name": get(f, "Name"), "type": get(f, "GetTypeName2")}
                              for f in features]
    if "dimensions" in sections:
        dimensions = {}
        if dimension_names:
            for name in dimension_names:
                dim = model.Parameter(name)
                if dim is not None:
                    dimensions[name] = dim
        else:
            for feature in features:
                display = get(feature, "GetFirstDisplayDimension")
                while display is not None:
                    if len(dimensions) >= LIMIT:
                        result["truncated"] = True
                        break
                    dim = display.GetDimension2(0)
                    name = get(dim, "FullName")
                    if name in dimensions:
                        break
                    dimensions[name] = dim
                    display = feature.GetNextDisplayDimension(display)
        result["dimensions"] = []
        for name, dim in dimensions.items():
            angular = int(get(dim, "GetType")) == 1  # swDimensionParamTypeDoubleAngular
            result["dimensions"].append({"name": name,
                "value": math.degrees(dim.SystemValue) if angular else dim.SystemValue*1000,
                "unit": "deg" if angular else "mm", "driving": dim.DrivenState == 2})
    if set(sections) & {"bodies", "surfaces"}:
        if kind != 1:
            raise ValueError("Инспекция тел и поверхностей требует активную деталь.")
        bodies = model.GetBodies2(0, False) or ()
        if len(bodies) > LIMIT:
            result["truncated"] = True
        if "bodies" in sections:
            result["bodies"] = [{"index": i, "sheet_metal": bool(get(body, "IsSheetMetal")),
                "bbox_mm": [v*1000 for v in get(body, "GetBodyBox")], "bbox_approximate": True}
                for i, body in enumerate(bodies[:LIMIT])]
        if "surfaces" in sections:
            result["surfaces"] = []
            for i, body in enumerate(bodies[:LIMIT]):
                for j, face in enumerate(get(body, "GetFaces") or ()):
                    if len(result["surfaces"]) >= LIMIT:
                        result["truncated"] = True
                        break
                    surface = get(face, "GetSurface")
                    item = {"body": i, "face": j, "kind": "other"}
                    if get(surface, "IsCylinder"):
                        params = surface.CylinderParams
                        item.update(kind="cylinder", axis_point_mm=[v*1000 for v in params[:3]],
                                    axis_direction=list(params[3:6]), radius_mm=params[6]*1000)
                    result["surfaces"].append(item)
    if "sheet_metal" in sections:
        result["sheet_metal"] = []
        for feature in features:
            if get(feature, "GetTypeName2") == "SheetMetal":
                definition = get(feature, "GetDefinition")
                result["sheet_metal"].append({"feature": get(feature, "Name"),
                    "thickness_mm": definition.Thickness*1000,
                    "bend_radius_mm": definition.BendRadius*1000})
    if "components" in sections:
        if kind != 2:
            raise ValueError("Инспекция компонентов требует активную сборку.")
        components = model.GetComponents(False) or ()
        result["components"] = []
        if len(components) > LIMIT:
            result["truncated"] = True
        for component in components[:LIMIT]:
            transform = component.Transform2
            values = list(transform.ArrayData) if transform is not None else None
            result["components"].append({"name": get(component, "Name2"),
                "path": get(component, "GetPathName"), "fixed": bool(get(component, "IsFixed")),
                "suppressed": bool(get(component, "IsSuppressed")),
                "rotation": values[:9] if values else None,
                "translation_mm": [v*1000 for v in values[9:12]] if values else None,
                "scale": values[12] if values else None})
    if "mates" in sections:
        if kind != 2:
            raise ValueError("Инспекция сопряжений требует активную сборку.")
        result["mates"] = []
        for feature in features:
            feature_type = get(feature, "GetTypeName2")
            if feature_type.startswith("Mate") and feature_type != "MateGroup":
                mate = get(feature, "GetSpecificFeature2")
                entities = [mate.MateEntity(i) for i in range(int(get(mate, "GetMateEntityCount")))]
                import pythoncom
                from win32com.client import VARIANT

                warning = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BOOL, False)
                error = feature.GetErrorCode2(warning)
                entity_data = []
                for entity in entities:
                    params = list(get(entity, "EntityParams") or ())
                    entity_data.append({
                        "reference_type": int(get(entity, "ReferenceType")),
                        "point_mm": [v*1000 for v in params[:3]] if len(params) >= 3 else None,
                        "direction": params[3:6] if len(params) >= 6 else None,
                        "radii_mm": [v*1000 for v in params[6:8]],
                    })
                result["mates"].append({"name": get(feature, "Name"), "type": int(mate.Type),
                    "lock_rotation": bool(get(feature, "GetDefinition").LockRotation)
                                     if int(mate.Type) == 1 else None,
                    "entities": entity_data,
                    "suppressed": bool(get(feature, "IsSuppressed")),
                    "alignment": int(mate.Alignment),
                    "error_code": error[0] if isinstance(error, tuple | list) else error,
                    "components": [get(entity.ReferenceComponent, "Name2") for entity in entities
                                   if entity.ReferenceComponent is not None]})
    return result
