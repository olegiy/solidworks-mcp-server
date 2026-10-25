"""Scripted CAD boundary; all production scheduling and verification stay real."""

import asyncio
from copy import deepcopy


def ok(value=None, images=()):
    return {"ok": True, "value": value, "images": list(images), "error": None, "uncertain": False}


def tool(name, properties=None, required=()):
    return {"type": "function", "function": {"name": name, "description": "Тестовый инструмент",
        "parameters": {"type": "object", "properties": properties or {},
                       "required": list(required), "additionalProperties": False}}}


CATALOG = [
    tool("get_dimension", {"name": {"type": "string"}}, ["name"]),
    tool("set_dimension", {"name": {"type": "string"}, "value_mm": {"type": "number"},
                           "rebuild": {"type": "boolean"}}, ["name", "value_mm"]),
    tool("get_feature_tree", {"limit": {"type": "integer"}}),
    tool("execute_python", {"code": {"type": "string"}}, ["code"]),
    tool("screenshot"), tool("create_part"), tool("create_assembly"),
    tool("list_open_documents"), tool("connect_solidworks"),
    tool("create_sketch", {"plane_name": {"type": "string"}}),
    tool("create_base_flange", {"thickness_mm": {"type": "number"},
                               "bend_radius_mm": {"type": "number"}}, ["thickness_mm"]),
    tool("get_sheet_metal_info"),
    tool("cut_through"), tool("add_mate"),
    tool("add_component", {"path": {"type": "string"}}, ["path"]), tool("get_components"),
    tool("sketch_line", {name: {"type": "number"} for name in ("x1_mm", "y1_mm", "x2_mm", "y2_mm")},
         ["x1_mm", "y1_mm", "x2_mm", "y2_mm"]),
    tool("save_document", {"save_as_path": {"type": "string"}}),
    tool("export_flat_pattern_dxf", {"output_path": {"type": "string"}}, ["output_path"]),
]


class FakeMcp:
    def __init__(self, steps=(), *, hold=None, catalog=None):
        self.steps = list(steps)
        self.calls = []
        self.catalog = deepcopy(CATALOG if catalog is None else catalog)
        self.hold = hold
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.uncertain = False

    async def tools(self):
        return deepcopy(self.catalog)

    async def call(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        if name == self.hold:
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        expected_name, result = self.steps.pop(0)
        assert name == expected_name, (name, expected_name)
        if isinstance(result, Exception):
            raise result
        return result(arguments) if callable(result) else deepcopy(result)
