"""Request-scoped interface language for user-facing messages."""

from contextvars import ContextVar

SUPPORTED = {"ru", "en"}
_LANGUAGE = ContextVar("cadnext_language", default="ru")


def language_from_header(value: str | None) -> str:
    if not value:
        return "ru"
    for part in value.split(","):
        tag = part.split(";", 1)[0].strip().lower()
        if tag.startswith("ru"):
            return "ru"
        if tag.startswith("en"):
            return "en"
    return "ru"


def set_language(value: str | None) -> str:
    language = value if value in SUPPORTED else language_from_header(value)
    _LANGUAGE.set(language)
    return language


def get_language() -> str:
    return _LANGUAGE.get()


def t(ru: str, en: str) -> str:
    return en if _LANGUAGE.get() == "en" else ru
