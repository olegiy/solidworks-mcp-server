"""Local SolidWorks signatures and original examples; optional official help."""

import re
from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path

import httpx

from cadnext.cad_examples import EXAMPLES

_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*\.[A-Za-z_][A-Za-z_0-9]*$")
_TYPE_NAME = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")


def load_index(path: Path) -> dict:
    """Call on STA. Return only JSON data, never retain a type-info pointer."""
    import pythoncom

    library = pythoncom.LoadTypeLib(str(path))
    major, minor = library.GetLibAttr()[3:5]
    version = {"major": major, "minor": minor, "year": major + 1992}
    symbols = {}
    for index in range(library.GetTypeInfoCount()):
        info = library.GetTypeInfo(index)
        interface = library.GetDocumentation(index)[0]
        for member in range(info.GetTypeAttr().cFuncs):
            desc = info.GetFuncDesc(member)
            names = info.GetNames(desc.memid)
            symbol_id = f"{interface}.{names[0]}"
            entry = symbols.setdefault(symbol_id, {
                "symbol_id": symbol_id, "version": version, "overloads": []})
            entry["overloads"].append({
                "invocation": {1: "method", 2: "get", 4: "put", 8: "putref"}.get(desc[4], "method"),
                "parameters": [{"name": names[j + 1] if j + 1 < len(names) else f"arg{j + 1}",
                                "type_description": repr(arg[0]), "flags": arg[1]}
                               for j, arg in enumerate(desc[2])],
                "return_type_description": repr(desc[8][0]),
                "description": info.GetDocumentation(desc.memid)[1] or ""})
    constants_path = path.with_name("swconst.tlb")
    if constants_path.exists():
        constants = pythoncom.LoadTypeLib(str(constants_path))
        for index in range(constants.GetTypeInfoCount()):
            info = constants.GetTypeInfo(index)
            enum_name = constants.GetDocumentation(index)[0]
            for member in range(info.GetTypeAttr().cVars):
                desc = info.GetVarDesc(member)
                name = info.GetNames(desc.memid)[0]
                symbol_id = f"{enum_name}.{name}"
                symbols[symbol_id] = {"symbol_id": symbol_id, "version": version,
                                      "kind": "constant", "value": desc[1], "overloads": []}
    return {"version": version, "symbols": symbols}


def search(index: dict, query: str) -> list[dict]:
    terms = re.findall(r"[\w]+", query.lower())
    if not terms or len(query) > 300:
        raise ValueError("Укажите поисковый запрос до 300 символов.")
    ranked = []
    for symbol_id, entry in index["symbols"].items():
        key = symbol_id.lower()
        score = sum(3 if term == key.split(".")[-1] else 1 for term in terms if term in key)
        if score:
            ranked.append((score, symbol_id, entry))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [{"symbol_id": name, "version": item["version"]} for _, name, item in ranked[:20]]


def get_symbol(index: dict, symbol_id: str) -> dict:
    type_name = bool(_TYPE_NAME.fullmatch(symbol_id))
    if not type_name and not _SYMBOL.fullmatch(symbol_id):
        raise ValueError("Некорректный идентификатор API.")
    if type_name:
        members = [deepcopy(entry) for name, entry in sorted(index["symbols"].items())
                   if name.startswith(symbol_id + ".") and entry.get("kind") == "constant"]
        if members:
            return {"symbol_id": symbol_id, "version": deepcopy(index["version"]),
                    "kind": "enum", "members": members}
    if symbol_id not in index["symbols"]:
        member = symbol_id.rsplit(".", 1)[-1]
        return {"symbol_id": symbol_id, "version": deepcopy(index["version"]), "found": False,
                "message": "Символ отсутствует в установленной библиотеке; выберите существующий интерфейс.",
                "alternatives": [item for item in search(index, member)
                                 if type_name or item["symbol_id"].split(".")[1].lower() == member.lower()]}
    return deepcopy(index["symbols"][symbol_id])


class _HelpText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ignored = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "nav"}:
            self.ignored += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "nav"}:
            self.ignored = max(0, self.ignored - 1)

    def handle_data(self, data):
        if not self.ignored and data.strip():
            self.parts.append(data.strip())


def with_help(symbol: dict, *, include_help: bool, client=None) -> dict:
    result = deepcopy(symbol)
    if not include_help:
        return result
    if symbol.get("found") is False:
        result["help"] = {"available": False,
                          "message": "Локальный символ не найден; пояснение не запрашивалось."}
        return result
    symbol_id = symbol["symbol_id"]
    enum = symbol.get("kind") == "enum"
    if not (_TYPE_NAME if enum else _SYMBOL).fullmatch(symbol_id):
        raise ValueError("Некорректный идентификатор API.")
    interface, _, member = symbol_id.partition(".")
    year = int(symbol["version"]["year"])
    url = (f"https://help.solidworks.com/{year}/english/api/sldworksapi/"
           f"SOLIDWORKS.Interop.sldworks~SOLIDWORKS.Interop.sldworks.{interface}~{member}.html")
    if enum or symbol.get("kind") == "constant":
        url = (f"https://help.solidworks.com/{year}/english/api/swconst/"
               f"SOLIDWORKS.Interop.swconst~SOLIDWORKS.Interop.swconst.{interface}.html")
    result["help"] = {"url": url, "available": False}
    owned = client is None
    client = client or httpx.Client()
    try:
        # Refuse redirects, including when a supplied client follows them by default.
        with client.stream("GET", url, timeout=8, follow_redirects=False) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 512 * 1024:
                    raise ValueError("Справка превышает допустимый размер.")
        parser = _HelpText()
        parser.feed(body.decode("utf-8", errors="replace"))
        text = "\n".join(parser.parts)
        if (interface if enum else member).lower() not in text.lower():
            raise ValueError("Официальная страница не содержит запрошенный API.")
        result["help"].update(available=True, text=text[:24000],
                              source="official_help", executable=False)
    except (httpx.HTTPError, ValueError):
        result["help"]["message"] = "Официальное пояснение недоступно; локальная сигнатура сохранена."
    finally:
        if owned:
            client.close()
    return result


def search_examples(query: str) -> list[dict]:
    terms = re.findall(r"[\w]+", query.lower())
    if not terms or len(query) > 300:
        raise ValueError("Укажите поисковый запрос до 300 символов.")
    ranked = sorted(EXAMPLES, key=lambda item: -sum(
        term in (item["example_id"] + " " + item["keywords"] + " " + item["title"]).lower()
        for term in terms))
    return [{key: item[key] for key in ("example_id", "title", "verified_versions")}
            for item in ranked if any(term in (item["example_id"] + " " + item["keywords"]
                                              + " " + item["title"]).lower() for term in terms)][:10]


def get_example(example_id: str) -> dict:
    return deepcopy(next(item for item in EXAMPLES if item["example_id"] == example_id))
