"""Reject known invalid SolidWorks arguments without executing user code or COM."""

import ast
from types import CodeType

_UNKNOWN = object()


class PythonPreflightError(ValueError):
    def __init__(self, message: str, line: int):
        super().__init__(message)
        self.python_line = line


def _condition_calls(expression):
    if isinstance(expression, ast.Call):
        yield expression
    elif isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
        yield from _condition_calls(expression.operand)
    elif isinstance(expression, ast.BoolOp):
        for value in expression.values:
            yield from _condition_calls(value)
    elif isinstance(expression, ast.Compare):
        for value in [expression.left, *expression.comparators]:
            yield from _condition_calls(value)


def _literal_argument(args, keywords, position, name):
    argument = next((item.value for item in keywords if item.arg == name), None)
    if argument is None and len(args) > position:
        if not any(isinstance(item, ast.Starred) for item in args[:position + 1]):
            argument = args[position]
    try:
        return ast.literal_eval(argument)
    except (ValueError, TypeError):
        return _UNKNOWN


def compile_python(code: str) -> CodeType:
    tree = ast.parse(code, filename="<cadnext-python>")
    compiled = compile(tree, "<cadnext-python>", "exec")
    for condition in ast.walk(tree):
        if not isinstance(condition, (ast.If, ast.While, ast.Assert, ast.IfExp)):
            continue
        for call in _condition_calls(condition.test):
            direct = isinstance(call.func, ast.Attribute) and call.func.attr == "InsertSketch"
            wrapped = (isinstance(call.func, ast.Name) and call.func.id == "GET"
                       and len(call.args) >= 2 and isinstance(call.args[1], ast.Constant)
                       and call.args[1].value == "InsertSketch")
            if direct or wrapped:
                raise PythonPreflightError(
                    "InsertSketch возвращает void, а не признак успеха. Вызови его отдельно "
                    "один раз и проверь ActiveSketch до/после; не повторяй переключение.",
                    call.lineno)
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
        if method == "FeatureCut4":
            single = _literal_argument(args, node.keywords, 0, "Sd")
            second_end = _literal_argument(args, node.keywords, 4, "T2")
            second_depth = _literal_argument(args, node.keywords, 6, "D2")
            if (single is False and type(second_end) is int and second_end == 0
                    and type(second_depth) in (int, float) and second_depth == 0):
                raise PythonPreflightError(
                    "FeatureCut4: Sd=False включает вторую сторону, но T2=0 (Blind) "
                    "с D2=0 задаёт нулевую глухую глубину. Для одной стороны используй "
                    "Sd=True; для двух задай ненулевую глубину или другое условие. "
                    "Направление Dir проверь по нормали и стороне материала.", node.lineno)
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
