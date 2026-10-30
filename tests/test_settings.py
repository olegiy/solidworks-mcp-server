import json

import pytest

from cadnext.settings import SettingsStore


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


@pytest.mark.parametrize("effort", [None, "low", "xhigh", "max"])
def test_reasoning_effort_persists_across_store_restart(tmp_path, effort):
    from cadnext.settings import SettingsStore

    secrets = MemorySecrets()
    store = SettingsStore(tmp_path, secrets)
    profile = store.save({"name": "P", "base_url": "http://localhost/v1", "model": "m",
                          "reasoning_effort": effort, "api_key": "saved-secret"})
    restored = SettingsStore(tmp_path, secrets)
    assert restored.get(profile.id).reasoning_effort == effort
    restored.save({"model": "another-model"}, profile.id)
    assert SettingsStore(tmp_path, secrets).get(profile.id).reasoning_effort == effort
    assert restored.secret(profile.id) == "saved-secret"
    restored.save({"reasoning_effort": None}, profile.id)
    assert SettingsStore(tmp_path, secrets).get(profile.id).reasoning_effort is None
    assert profile.reasoning_effort == effort


def test_legacy_profile_defaults_to_api_reasoning_setting(tmp_path):
    from cadnext.settings import SettingsStore

    (tmp_path / "settings.json").write_text(json.dumps({"version": 1, "profiles": [{
        "id": "old", "name": "P", "base_url": "http://localhost/v1", "model": "m"}]}))
    store = SettingsStore(tmp_path, MemorySecrets())
    assert store.get("old").reasoning_effort is None
    assert store.get("old").stream_responses is False


@pytest.mark.parametrize("value", [None, 1, "true"])
def test_invalid_streaming_flag_preserves_settings(tmp_path, value):
    store = SettingsStore(tmp_path, MemorySecrets())
    profile = store.save({"name": "P", "base_url": "http://localhost/v1", "model": "m"})
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.save({"stream_responses": value}, profile.id)
    assert store.path.read_bytes() == before
    assert store.get(profile.id).stream_responses is False


@pytest.mark.parametrize("effort", ["unknown", 123, {}])
def test_invalid_reasoning_setting_preserves_existing_profile(tmp_path, effort):
    from cadnext.settings import SettingsStore

    store = SettingsStore(tmp_path, MemorySecrets())
    profile = store.save({"name": "P", "base_url": "http://localhost/v1", "model": "m"})
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.save({"reasoning_effort": effort}, profile.id)
    assert store.path.read_bytes() == before
    assert store.get(profile.id).reasoning_effort is None


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


@pytest.mark.parametrize("new_url", [
    "https://another.example.test/v1",
    "https://example.test/other-api",
    "http://example.test/v1",
])
def test_address_change_clears_saved_key(tmp_path, new_url):
    from cadnext.settings import SettingsStore

    secrets = MemorySecrets()
    store = SettingsStore(tmp_path, secrets)
    profile = store.save({"name": "P", "base_url": "https://example.test/v1", "model": "m",
                          "api_key": "old-secret"})
    changed = store.save({"base_url": new_url}, profile.id)

    assert changed.base_url == new_url
    assert store.secret(profile.id) is None
    assert not SettingsStore(tmp_path, secrets).public(changed)["has_key"]


@pytest.mark.parametrize("new_url", ["https://example.test/v1", "https://example.test/v1/"])
def test_same_normalized_address_preserves_saved_key_and_local_flag(tmp_path, new_url):
    from cadnext.settings import SettingsStore

    store = SettingsStore(tmp_path, MemorySecrets())
    profile = store.save({"name": "P", "base_url": "https://example.test/v1", "model": "m",
                          "local": True, "api_key": "old-secret"})
    changed = store.save({"base_url": new_url}, profile.id)

    assert changed.base_url == "https://example.test/v1"
    assert changed.local
    assert store.secret(profile.id) == "old-secret"


def test_address_change_accepts_explicit_new_key(tmp_path):
    from cadnext.settings import SettingsStore

    store = SettingsStore(tmp_path, MemorySecrets())
    profile = store.save({"name": "P", "base_url": "https://example.test/v1", "model": "m",
                          "api_key": "old-secret"})
    store.save({"base_url": "https://another.example.test/v1", "api_key": "new-secret"}, profile.id)

    assert store.secret(profile.id) == "new-secret"


def test_address_change_write_failure_restores_profile_and_saved_key(tmp_path, monkeypatch):
    from cadnext.settings import SettingsStore

    store = SettingsStore(tmp_path, MemorySecrets())
    profile = store.save({"name": "P", "base_url": "https://example.test/v1", "model": "m",
                          "api_key": "old-secret"})
    before = store.path.read_bytes()

    def fail_replace(*args):
        raise OSError("simulated write failure")

    monkeypatch.setattr("cadnext.settings.os.replace", fail_replace)
    with pytest.raises(OSError):
        store.save({"base_url": "https://another.example.test/v1"}, profile.id)

    assert store.path.read_bytes() == before
    assert store.get(profile.id) == profile
    assert store.secret(profile.id) == "old-secret"
    assert list(tmp_path.iterdir()) == [store.path]


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

def _vision_store(tmp_path):
    from cadnext.settings import SettingsStore

    store = SettingsStore(tmp_path, MemorySecrets())
    vision = store.save({"name": "Vision", "base_url": "https://vision.example.test/v1",
                         "model": "v1", "vision": True, "api_key": "secret-vision"})
    return store, vision


def test_vision_routing_requires_existing_vision_profile(tmp_path):
    from cadnext.settings import SettingsStore

    store = SettingsStore(tmp_path, MemorySecrets())
    text = store.save({"name": "Text", "base_url": "https://example.test/v1", "model": "m"})
    with pytest.raises(ValueError):
        store.set_vision_routing(text.id, approved=True)
    with pytest.raises(ValueError):
        store.set_vision_routing(None, approved=True)
    with pytest.raises(KeyError):
        store.set_vision_routing("missing", approved=True)
    assert store.vision_routing is None


def test_vision_routing_persists_without_secret_and_survives_rename(tmp_path):
    store, vision = _vision_store(tmp_path)
    store.set_vision_routing(vision.id, approved=True)
    routing = store.vision_routing
    assert routing["profile_id"] == vision.id and routing["approved"] is True
    assert "secret-vision" not in (tmp_path / "settings.json").read_text()
    store.save({"name": "Renamed"}, vision.id)
    assert store.vision_routing == routing
    assert SettingsStore(tmp_path, MemorySecrets()).vision_routing == routing
    store.save({"api_key": "another-secret"}, vision.id)
    assert SettingsStore(tmp_path, MemorySecrets()).vision_routing == routing


@pytest.mark.parametrize("change", [
    {"base_url": "https://other.example.test/v1"},
    {"model": "v2"},
    {"vision": False},
    {"local": True},
])
def test_vision_routing_invalidated_by_connection_change(tmp_path, change):
    store, vision = _vision_store(tmp_path)
    store.set_vision_routing(vision.id, approved=True)
    store.save(change, vision.id)
    assert store.vision_routing is None
    assert SettingsStore(tmp_path, MemorySecrets()).vision_routing is None


def test_vision_routing_revoked_and_profile_delete_invalidates(tmp_path):
    store, vision = _vision_store(tmp_path)
    store.set_vision_routing(vision.id, approved=True)
    store.set_vision_routing(vision.id, approved=False)
    assert store.vision_routing is None
    store.set_vision_routing(vision.id, approved=True)
    store.delete(vision.id)
    assert store.vision_routing is None


def test_vision_routing_write_failure_keeps_previous_state(tmp_path, monkeypatch):
    store, vision = _vision_store(tmp_path)
    store.set_vision_routing(vision.id, approved=True)
    before = (tmp_path / "settings.json").read_bytes()
    routing = store.vision_routing

    def fail_replace(*args):
        raise OSError("simulated write failure")

    monkeypatch.setattr("cadnext.settings.os.replace", fail_replace)
    with pytest.raises(OSError):
        store.set_vision_routing(vision.id, approved=False)
    assert (tmp_path / "settings.json").read_bytes() == before
    assert store.vision_routing == routing


def test_legacy_settings_without_routing_fields_are_compatible(tmp_path):
    from cadnext.settings import SettingsStore

    (tmp_path / "settings.json").write_text(json.dumps({"version": 1, "profiles": [{
        "id": "old", "name": "P", "base_url": "http://localhost/v1", "model": "m",
        "vision": True}]}))
    store = SettingsStore(tmp_path, MemorySecrets())
    assert store.vision_routing is None
    store.set_vision_routing("old", approved=True)
    assert store.vision_routing["profile_id"] == "old"


def test_stale_routing_fingerprint_is_ignored(tmp_path):
    store, vision = _vision_store(tmp_path)
    store.set_vision_routing(vision.id, approved=True)
    data = json.loads((tmp_path / "settings.json").read_text())
    data["vision_routing_fingerprint"] = "0" * 64
    (tmp_path / "settings.json").write_text(json.dumps(data))
    assert SettingsStore(tmp_path, MemorySecrets()).vision_routing is None
