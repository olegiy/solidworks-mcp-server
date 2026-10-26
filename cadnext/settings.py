"""Immutable connection snapshots and atomic, secret-free settings."""

import ipaddress
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from threading import RLock
from typing import Literal, get_args
from urllib.parse import urlsplit
from uuid import uuid4

from cadnext.i18n import t

SERVICE = "cad_agent_next"
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]


@dataclass(frozen=True)
class Profile:
    name: str
    base_url: str
    model: str
    id: str = field(default_factory=lambda: uuid4().hex)
    vision: bool = False
    local: bool | None = None
    reasoning_effort: ReasoningEffort | None = None

    def __post_init__(self):
        if self.reasoning_effort is not None and self.reasoning_effort not in get_args(ReasoningEffort):
            raise ValueError(t("Неизвестная сила рассуждения.", "Unknown reasoning effort."))
        if any(not isinstance(value, str) or not value.strip()
               for value in (self.id, self.name, self.base_url, self.model)):
            raise ValueError(t("Название, адрес и модель должны быть заполнены.",
                                   "Name, address, and model must be filled in."))
        try:
            url = urlsplit(self.base_url)
            valid = (url.scheme in {"http", "https"} and url.hostname
                     and not url.username and not url.password and not url.query
                     and not url.fragment and not any(c.isspace() for c in self.base_url))
            _ = url.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(t("Нужен HTTP(S)-адрес API без пароля, параметров и фрагмента.",
                               "An HTTP(S) API address without a password, query, or fragment is required."))
        if not isinstance(self.vision, bool) or (
                self.local is not None and not isinstance(self.local, bool)):
            raise ValueError(t("Признаки подключения должны быть логическими значениями.",
                                   "Connection flags must be boolean values."))
        if self.local is None:
            host = url.hostname.lower()
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = host == "localhost"
            object.__setattr__(self, "local", local)
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))


class SettingsStore:
    def __init__(self, directory: Path | None = None, secrets=None):
        if directory is None:
            directory = Path(os.environ["LOCALAPPDATA"]) / SERVICE
        self.path = Path(directory) / "settings.json"
        if secrets is None:
            from keyring.backends.Windows import WinVaultKeyring

            secrets = WinVaultKeyring()
        self._secrets = secrets
        self._lock = RLock()
        self._profiles: dict[str, Profile] = {}
        self._python_execution_allowed = False
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data["version"] != 1:
                    raise ValueError
                permission = data.get("python_execution_allowed", False)
                if type(permission) is not bool:
                    raise ValueError
                self._python_execution_allowed = permission
                profiles = [Profile(**item) for item in data["profiles"]]
                self._profiles = {p.id: p for p in profiles}
                if len(self._profiles) != len(profiles):
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                raise ValueError(t("Файл настроек повреждён; исходный файл сохранён.",
                               "The settings file is corrupted; the original file was preserved.")) from None

    def list(self) -> list[Profile]:
        with self._lock:
            return list(self._profiles.values())

    def get(self, profile_id: str) -> Profile:
        with self._lock:
            if profile_id not in self._profiles:
                raise KeyError(t("Подключение не найдено.", "Connection not found."))
            return self._profiles[profile_id]

    def secret(self, profile_id: str) -> str | None:
        with self._lock:
            try:
                return self._secrets.get_password(SERVICE, profile_id)
            except Exception:
                raise RuntimeError(t("Не удалось прочитать ключ из Windows Credential Manager.",
                                 "Could not read the key from Windows Credential Manager.")) from None

    def public(self, profile: Profile) -> dict:
        return {**asdict(profile), "has_key": bool(self.secret(profile.id))}

    def _set_secret(self, profile_id: str, value: str | None):
        try:
            if value:
                self._secrets.set_password(SERVICE, profile_id, value)
            elif self._secrets.get_password(SERVICE, profile_id) is not None:
                self._secrets.delete_password(SERVICE, profile_id)
        except Exception:
            raise RuntimeError(t("Не удалось изменить ключ в Windows Credential Manager.",
                                 "Could not change the key in Windows Credential Manager.")) from None

    @property
    def python_execution_allowed(self) -> bool:
        with self._lock:
            return self._python_execution_allowed

    def set_python_execution_allowed(self, allowed: bool):
        if type(allowed) is not bool:
            raise ValueError(t("Разрешение Python должно быть логическим значением.",
                               "Python permission must be a boolean value."))
        with self._lock:
            try:
                self._write(self._profiles, python_execution_allowed=allowed)
            except OSError:
                raise OSError(t("Не удалось сохранить разрешение Python; настройка не изменена.",
                            "Could not save the Python permission; the setting was not changed.")) from None
            self._python_execution_allowed = allowed

    def _write(self, profiles: dict[str, Profile], *, python_execution_allowed=None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".settings-", suffix=".tmp", delete=False) as f:
                temporary = Path(f.name)
                permission = (self._python_execution_allowed if python_execution_allowed is None
                              else python_execution_allowed)
                json.dump({"version": 1, "profiles": [asdict(p) for p in profiles.values()],
                           "python_execution_allowed": permission},
                          f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _commit(self, profiles: dict[str, Profile], profile_id: str,
                change_key: bool, key: str | None):
        old_key = self.secret(profile_id) if change_key else None
        if change_key:
            self._set_secret(profile_id, key)
        try:
            self._write(profiles)
        except OSError:
            if change_key:
                self._set_secret(profile_id, old_key)
            raise OSError(t("Не удалось записать настройки; предыдущий файл сохранён.",
                            "Could not write the settings; the previous file was preserved.")) from None
        self._profiles = profiles

    def save(self, values: dict, profile_id: str | None = None) -> Profile:
        with self._lock:
            allowed = {"name", "base_url", "model", "vision", "local", "api_key", "reasoning_effort"}
            if set(values) - allowed:
                raise ValueError(t("Неизвестное поле настроек подключения.", "Unknown connection setting field."))
            key = values.get("api_key")
            if "api_key" in values and key is not None and not isinstance(key, str):
                raise ValueError(t("Ключ должен быть строкой.", "The key must be a string."))
            previous = self.get(profile_id) if profile_id is not None else None
            fields = asdict(previous) if previous else {}
            fields.update({k: v for k, v in values.items() if k != "api_key"})
            try:
                profile = Profile(**fields)
            except TypeError:
                raise ValueError(t("Укажите название, адрес API и модель.",
                                   "Provide a name, an API address, and a model.")) from None
            address_changed = previous is not None and profile.base_url != previous.base_url
            if address_changed and "local" not in values:
                profile = replace(profile, local=None)
            profiles = {**self._profiles, profile.id: profile}
            self._commit(profiles, profile.id, "api_key" in values or address_changed, key)
            return profile

    def delete(self, profile_id: str):
        with self._lock:
            self.get(profile_id)
            profiles = {key: value for key, value in self._profiles.items() if key != profile_id}
            self._commit(profiles, profile_id, True, None)
