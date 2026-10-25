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


@pytest.mark.parametrize(("url", "local"), [
    ("http://localhost:8080/v1", True),
    ("http://127.2.3.4/v1", True),
    ("http://[::1]:8080/v1", True),
    ("http://192.168.1.2/v1", False),
    ("https://localhost.example.test/v1", False),
])
def test_only_loopback_is_local_by_default(url, local):
    from cadnext.settings import Profile

    assert Profile(name="P", base_url=url, model="m").local is local


@pytest.mark.parametrize("url", [
    "file:///tmp", "https://user:password@example.test/v1",
    "https://example.test/v1?api_key=secret", "http://", "http://host:bad/v1",
])
def test_invalid_or_secret_bearing_url_is_rejected(url):
    from cadnext.settings import Profile

    with pytest.raises(ValueError):
        Profile(name="P", base_url=url, model="m")


def test_explicit_local_server_and_address_change(tmp_path):
    from cadnext.settings import SettingsStore

    store = SettingsStore(tmp_path, MemorySecrets())
    profile = store.save({"name": "LAN", "base_url": "http://192.168.1.2/v1",
                          "model": "m", "local": True})
    assert profile.local
    changed = store.save({"base_url": "https://example.test/v1"}, profile.id)
    assert not changed.local


def test_replace_failure_preserves_disk_memory_and_secret(tmp_path, monkeypatch):
    from cadnext.settings import SettingsStore

    store = SettingsStore(tmp_path, MemorySecrets())
    profile = store.save({"name": "P", "base_url": "http://localhost/v1", "model": "m",
                          "api_key": "old-secret"})
    before = (tmp_path / "settings.json").read_bytes()

    def fail_replace(*args):
        raise OSError("simulated write failure")

    monkeypatch.setattr("cadnext.settings.os.replace", fail_replace)
    with pytest.raises(OSError):
        store.save({"model": "changed", "api_key": "new-secret"}, profile.id)
    assert (tmp_path / "settings.json").read_bytes() == before
    assert store.get(profile.id).model == "m"
    assert store.secret(profile.id) == "old-secret"
    assert list(tmp_path.iterdir()) == [tmp_path / "settings.json"]


def test_clearing_key_and_frozen_profile_snapshot(tmp_path):
    from dataclasses import FrozenInstanceError

    from cadnext.settings import SettingsStore

    store = SettingsStore(tmp_path, MemorySecrets())
    profile = store.save({"name": "P", "base_url": "http://localhost/v1", "model": "m",
                          "api_key": "old-secret"})
    store.save({"model": "new", "api_key": ""}, profile.id)
    assert profile.model == "m"
    assert store.secret(profile.id) is None
    with pytest.raises(FrozenInstanceError):
        profile.model = "changed"
