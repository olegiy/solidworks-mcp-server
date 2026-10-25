import json

import pytest


class MemorySecrets:
    def __init__(self):
        self.data = {}

    def get_password(self, service, name):
        return self.data.get((service, name))

    def set_password(self, service, name, value):
        self.data[service, name] = value

    def delete_password(self, service, name):
        self.data.pop((service, name), None)


def test_settings_persist_without_a_key(tmp_path):
    from cadnext.settings import SettingsStore

    store = SettingsStore(tmp_path, MemorySecrets())
    profile = store.save({"name": "Local", "base_url": "http://127.0.0.1:8081/v1",
                          "model": "local-model"})
    assert profile.local
    restored = SettingsStore(tmp_path, MemorySecrets()).get(profile.id)
    assert restored.model == "local-model"
    assert store.secret(profile.id) is None


def test_key_only_in_new_keyring_namespace(tmp_path):
    from cadnext.settings import SettingsStore

    secrets = MemorySecrets()
    store = SettingsStore(tmp_path, secrets)
    secret = "private-test-value"
    profile = store.save({"name": "Remote", "base_url": "https://example.test/v1",
                          "model": "m", "api_key": secret})
    assert secret not in (tmp_path / "settings.json").read_text()
    assert secrets.data[("cad_agent_next", profile.id)] == secret
    assert "api_key" not in store.public(profile)
    assert store.public(profile)["has_key"]


def test_invalid_settings_do_not_overwrite_existing_file(tmp_path):
    from cadnext.settings import SettingsStore

    store = SettingsStore(tmp_path, MemorySecrets())
    profile = store.save({"name": "P", "base_url": "http://localhost:8080/v1", "model": "m"})
    before = json.loads((tmp_path / "settings.json").read_text())
    with pytest.raises(ValueError):
        store.save({"name": "", "base_url": "file:///tmp", "model": ""}, profile.id)
    assert json.loads((tmp_path / "settings.json").read_text()) == before


def test_update_keeps_key_and_delete_removes_it(tmp_path):
    from cadnext.settings import SettingsStore

    store = SettingsStore(tmp_path, MemorySecrets())
    profile = store.save({"name": "P", "base_url": "https://example.test/v1", "model": "m",
                          "api_key": "private-test-value"})
    store.save({"model": "new-model"}, profile.id)
    assert store.secret(profile.id) == "private-test-value"
    store.delete(profile.id)
    assert store.list() == []
    assert store.secret(profile.id) is None
