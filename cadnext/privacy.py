"""Per-dialogue consent, reversible text filtering and a separate image gate."""

import json
import re
from uuid import uuid4

_START = r"(?:[A-Za-z]:[\\/]|\\\\|/(?!/))"
_PATH = (
    rf'(?P<quote>["\'])(?P<path>{_START}[^<>"\'\r\n|?*]+)(?P=quote)'
    r'|(?:(?<!\w)[A-Za-z]:[\\/]|\\\\)[^\s<>"\'|?*,;]+'
    r'|(?<![\w:/])/(?!/)[\w.~-]+(?:/[\w.~-]+)*'
)
_DROP = object()


class ConsentRequired(PermissionError):
    pass


class PrivacyContext:
    def __init__(self, *, local: bool, consent: bool = False, sensitive=(),
                 vision: bool = False, image_consent: bool = False,
                 session_id: str = "", profile_id: str = ""):
        self.local = local
        self.vision = vision
        self._scope = (session_id, profile_id)
        self._profile_binding = None
        self._consent = consent
        self._image_consent = image_consent
        self._sensitive = sorted({s for s in sensitive if s}, key=len, reverse=True)
        self._prefix = f"[СКРЫТО_{uuid4().hex}_"
        self._tokens: dict[str, str] = {}

    @property
    def session_id(self):
        return self._scope[0]

    @property
    def profile_id(self):
        return self._scope[1]

    def grant(self, *, session_id: str, profile_id: str, images: bool = False):
        if not session_id or not profile_id or (session_id, profile_id) != self._scope:
            raise ConsentRequired("Согласие относится к другому диалогу или подключению.")
        self._consent = True
        self._image_consent = images

    def bind_profile(self, profile_id: str, *, base_url: str, model: str):
        if self.profile_id and self.profile_id != profile_id:
            raise ValueError("Контекст приватности относится к другому подключению.")
        binding = (profile_id, base_url, model)
        if self._profile_binding is not None and self._profile_binding != binding:
            raise ValueError("Подключение изменено; создайте новый контекст согласия.")
        self._profile_binding = binding
        self._scope = (self.session_id, profile_id)

    def bind_session(self, session_id: str):
        if self.session_id and self.session_id != session_id:
            raise ValueError("Контекст приватности относится к другому диалогу.")
        self._scope = (session_id, self.profile_id)

    def _token(self, value: str) -> str:
        if value not in self._tokens:
            self._tokens[value] = f"{self._prefix}{len(self._tokens) + 1}]"
        return self._tokens[value]

    def _text(self, value: str, restore: bool) -> str:
        # Tool arguments and JSON tool results often contain escaped Windows paths.
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except ValueError:
                pass
            else:
                filtered = self._walk(parsed, restore)
                return json.dumps(None if filtered is _DROP else filtered, ensure_ascii=False)
        if restore:
            for original, token in self._tokens.items():
                value = value.replace(token, original)
            return value
        if re.match(_START, value) and not any(c in value for c in '\r\n"\''):
            return self._token(value)
        # One substitution pass avoids filtering newly generated tokens again.
        existing = [re.escape(token) for token in self._tokens.values()]
        patterns = [*existing, _PATH, *(re.escape(s) for s in self._sensitive)]

        def replace(match):
            if match.group() in self._tokens.values():
                return match.group()
            if match.group("path") is not None:
                quote = match.group("quote")
                return quote + self._token(match.group("path")) + quote
            return self._token(match.group())

        return re.sub("|".join(patterns), replace, value)

    def _walk(self, value, restore: bool):
        if isinstance(value, dict):
            if not restore and (value.get("type") == "image_url"
                                or ("data" in value and "mimeType" in value)):
                return value.copy() if self.vision and (
                    self.local or self._image_consent) else _DROP
            result = {}
            for key, item in value.items():
                transformed = self._walk(item, restore)
                if transformed is not _DROP:
                    filtered_key = key if self.local and not restore else self._text(key, restore)
                    result[filtered_key] = transformed
            return result
        if isinstance(value, list):
            return [item for source in value if (item := self._walk(source, restore)) is not _DROP]
        if isinstance(value, str):
            return value if self.local and not restore else self._text(value, restore)
        return value

    def outgoing(self, value):
        if not self.local and not self._consent:
            raise ConsentRequired("Разрешите передачу CAD-данных для этого диалога и подключения.")
        result = self._walk(value, False)
        return None if result is _DROP else result

    def _argument_values(self, value):
        # Argument names belong to the tool schema; only their values are data.
        if isinstance(value, dict):
            return {key: self._argument_values(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._argument_values(item) for item in value]
        return self.outgoing(value)

    def outgoing_messages(self, messages: list[dict]) -> list[dict]:
        self.outgoing(None)
        result = []
        for message in messages:
            filtered = {key: value if key in {"role", "name", "tool_call_id"}
                        else self.outgoing(value)
                        for key, value in message.items() if key not in {"content", "tool_calls"}}
            if "content" in message:
                content = message["content"]
                if isinstance(content, list):
                    blocks = []
                    for block in content:
                        if block.get("type") == "text":
                            blocks.append({"type": "text", "text": self.outgoing(block["text"])})
                        elif block.get("type") == "image_url":
                            image = self.outgoing(block)
                            if image is not None:
                                blocks.append(image)
                        else:
                            raise ValueError("Неподдерживаемый тип содержимого сообщения.")
                    filtered["content"] = blocks or "Изображение не передано модели."
                else:
                    filtered["content"] = self.outgoing(content)
            if "tool_calls" in message:
                filtered["tool_calls"] = []
                for call in message["tool_calls"]:
                    function = call["function"]
                    arguments = self._argument_values(json.loads(function["arguments"]))
                    filtered["tool_calls"].append({**call, "function": {
                        **function, "arguments": json.dumps(arguments, ensure_ascii=False)}})
            result.append(filtered)
        return result

    def outgoing_tools(self, tools: list[dict]) -> list[dict]:
        self.outgoing(None)

        def schema(value):
            if isinstance(value, dict):
                result = {}
                for key, item in value.items():
                    if key in {"properties", "patternProperties", "$defs", "definitions"}:
                        result[key] = {name: schema(child) for name, child in item.items()}
                    elif key in {"description", "title", "default", "const", "enum"}:
                        result[key] = self.outgoing(item)
                    else:
                        result[key] = schema(item)
                return result
            if isinstance(value, list):
                return [schema(item) for item in value]
            return value

        # Names, required fields, and types are protocol, not free-form CAD text.
        return [{**tool, "function": {
            **tool["function"],
            **({"description": self.outgoing(tool["function"]["description"])}
               if "description" in tool["function"] else {}),
            **({"parameters": schema(tool["function"]["parameters"])}
               if "parameters" in tool["function"] else {}),
        }} for tool in tools]

    def restore(self, value):
        return self._walk(value, True)
