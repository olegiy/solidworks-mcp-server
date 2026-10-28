"""Reject known invalid SolidWorks arguments without executing user code or COM."""

import ast
from types import CodeType


class PythonPreflightError(ValueError):
    def __init__(self, message: str, line: int):
        super().__init__(message)
        self.python_line = line


def compile_python(code: str) -> CodeType:
    tree = ast.parse(code, filename="<cadnext-python>")
    compiled = compile(tree, "<cadnext-python>", "exec")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        args = node.args
        if isinstance(node.func, ast.Attribute):
            method = node.func.attr
        elif (isinstance(node.func, ast.Name) and node.func.id == "GET" and len(args) >= 2
              and isinstance(args[1], ast.Constant) and isinstance(args[1].value, str)):
            method = args[1].value
            args = args[2:]
        else:
            continue
        if method == "SelectByID2":
            position, keyword = 7, "Callout"
        elif method in {"GetArcEndCondition", "SetArcEndCondition"}:
            position, keyword = 0, "Index"
        else:
            continue
        argument = next((item.value for item in node.keywords if item.arg == keyword), None)
        if argument is None and len(args) > position:
            # Starred arguments make subsequent positional offsets unknowable.
            if not any(isinstance(item, ast.Starred) for item in args[:position + 1]):
                argument = args[position]
        if argument is None:
            continue
        try:
            value = ast.literal_eval(argument)
        except (ValueError, TypeError):
            # Never infer dynamic values, evaluate expressions, or rewrite code.
            continue
        if method == "SelectByID2" and value is None:
            raise PythonPreflightError(
                "SelectByID2: для Callout используй NOTHING (VT_DISPATCH), а не None.",
                node.lineno)
        if method != "SelectByID2" and (type(value) is not int or value not in (1, 2)):
            raise PythonPreflightError(
                f"{method}: Index должен быть 1 или 2. После изменения прочитай оба "
                "условия и фактический размер; при несовпадении остановись.", node.lineno)
    return compiled
