"""Accumulate one SSE completion; no partial result is executable."""

import json


class CompletionStream:
    def __init__(self):
        self.text = []
        self.has_content = False
        self.calls = {}
        self.finish = None
        self.usage = None
        self.done = False

    def feed(self, event: str) -> bool:
        if event == "[DONE]":
            self.done = True
            return False
        data = json.loads(event)
        if not isinstance(data, dict) or "error" in data:
            raise ValueError("Invalid stream")
        if isinstance(data.get("usage"), dict):
            self.usage = data["usage"]
        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise ValueError("Invalid choices")
        if not choices:
            if self.usage is None:
                raise ValueError("Empty stream event")
            return False
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get("index", 0) != 0:
            raise ValueError("Invalid choice")
        delta = choice.get("delta")
        if not isinstance(delta, dict) or (self.finish is not None and delta):
            raise ValueError("Invalid delta")
        if delta.get("role", "assistant") != "assistant":
            raise ValueError("Invalid role")
        generated = False
        content = delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise ValueError("Invalid text")
            self.has_content = True
            self.text.append(content)
            generated |= bool(content)
        fragments = delta.get("tool_calls") or []
        if not isinstance(fragments, list):
            raise ValueError("Invalid calls")
        for fragment in fragments:
            index = fragment.get("index")
            if type(index) is not int or not 0 <= index < 1000:
                raise ValueError("Invalid call index")
            call = self.calls.setdefault(index, {"id": "", "type": "function", "function": {
                "name": "", "arguments": ""}})
            if fragment.get("type", "function") != "function":
                raise ValueError("Invalid call type")
            if fragment.get("id") is not None:
                if not isinstance(fragment["id"], str):
                    raise ValueError("Invalid call id")
                call["id"] += fragment["id"]
                generated |= bool(fragment["id"])
            function = fragment.get("function", {})
            if not isinstance(function, dict):
                raise ValueError("Invalid function")
            for key in ("name", "arguments"):
                value = function.get(key)
                if value is not None:
                    if not isinstance(value, str):
                        raise ValueError("Invalid function fragment")
                    call["function"][key] += value
                    generated |= bool(value)
        generated |= any(isinstance(delta.get(key), str) and bool(delta[key])
                         for key in ("reasoning_content", "reasoning"))
        if choice.get("finish_reason") is not None:
            if self.finish is not None:
                raise ValueError("Repeated finish")
            self.finish = choice["finish_reason"]
        return generated

    def result(self):
        if not self.done or self.finish is None or sorted(self.calls) != list(range(len(self.calls))):
            raise ValueError("Incomplete stream")
        message = {"content": "".join(self.text) if self.has_content else None}
        if self.calls:
            message["tool_calls"] = [self.calls[index] for index in sorted(self.calls)]
        return {"choices": [{"message": message, "finish_reason": self.finish}], "usage": self.usage}
