import pytest


def test_paths_are_masked_and_restored():
    from cadnext.privacy import PrivacyContext

    ctx = PrivacyContext(local=False, consent=True)
    raw = {"path": r"C:\Company\secret\part.SLDPRT", "value": 62}
    filtered = ctx.outgoing(raw)
    assert "Company" not in str(filtered)
    assert filtered["value"] == 62
    assert ctx.restore(filtered) == raw


def test_remote_requires_consent():
    from cadnext.privacy import ConsentRequired, PrivacyContext

    with pytest.raises(ConsentRequired):
        PrivacyContext(local=False).outgoing({"cad": "data"})


def test_local_does_not_need_consent():
    from cadnext.privacy import PrivacyContext

    raw = {"path": r"C:\Company\part.sldprt"}
    assert PrivacyContext(local=True).outgoing(raw) == raw


def test_sensitive_strings_are_removed_from_all_message_roles():
    from cadnext.privacy import PrivacyContext

    ctx = PrivacyContext(local=False, consent=True, sensitive=["PrivateCompany"])
    messages = [{"role": role, "content": "PrivateCompany"}
                for role in ("user", "assistant", "tool")]
    assert "PrivateCompany" not in str(ctx.outgoing(messages))


@pytest.mark.parametrize("path", [
    r"C:\Company Files\secret\part.SLDPRT", r"\\server\Company\part.sldprt",
    "/home/company/part.sldprt",
])
def test_paths_in_text_and_json_arguments_are_restored(path):
    import json

    from cadnext.privacy import PrivacyContext

    ctx = PrivacyContext(local=False, consent=True)
    raw = {"content": f'Файл: "{path}"', "arguments": json.dumps({"path": path})}
    filtered = ctx.outgoing(raw)
    assert "company" not in str(filtered).lower()
    restored = ctx.restore(filtered)
    assert restored["content"] == raw["content"]
    assert json.loads(restored["arguments"]) == {"path": path}
    assert raw["content"] == f'Файл: "{path}"'


def test_images_require_separate_consent_and_vision():
    from cadnext.privacy import PrivacyContext

    raw = [{"role": "user", "content": [
        {"type": "text", "text": "Снимок"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}]
    for vision, image_consent, allowed in [(True, False, False), (False, True, False),
                                           (True, True, True)]:
        ctx = PrivacyContext(local=False, consent=True, vision=vision,
                             image_consent=image_consent)
        assert ("AAAA" in str(ctx.outgoing(raw))) is allowed
    assert len(raw[0]["content"]) == 2


def test_consent_is_bound_to_session_and_profile():
    from cadnext.privacy import ConsentRequired, PrivacyContext

    ctx = PrivacyContext(local=False, session_id="s1", profile_id="p1")
    with pytest.raises(ConsentRequired):
        ctx.grant(session_id="s2", profile_id="p1")
    ctx.grant(session_id="s1", profile_id="p1")
    assert ctx.outgoing("данные") == "данные"
    other = PrivacyContext(local=False, session_id="s1", profile_id="p2")
    with pytest.raises(ConsentRequired):
        other.outgoing("данные")


def test_repeated_filtering_keeps_stable_tokens_and_valid_urls():
    from cadnext.privacy import PrivacyContext

    ctx = PrivacyContext(local=False, consent=True)
    raw = {"path": r"C:\Company\part.sldprt", "url": "https://example.test/v1"}
    first = ctx.outgoing(raw)
    assert first == ctx.outgoing(raw)
    assert first["url"] == raw["url"]
    assert ctx.restore(first) == raw


def test_local_dictionary_keys_remain_unchanged():
    from cadnext.privacy import PrivacyContext

    raw = {r"C:\Company\part.sldprt": 62}
    assert PrivacyContext(local=True).outgoing(raw) == raw


def test_sensitive_filter_does_not_corrupt_path_tokens():
    from cadnext.privacy import PrivacyContext

    ctx = PrivacyContext(local=False, consent=True, sensitive=["СКРЫТО"])
    raw = {"file": r"C:\data\part.sldprt"}
    assert ctx.restore(ctx.outgoing(raw)) == raw


def test_path_token_from_prose_restores_only_the_path():
    from cadnext.privacy import PrivacyContext

    ctx = PrivacyContext(local=False, consent=True)
    filtered = ctx.outgoing(r"Файл C:\Company\part.sldprt готов.")
    token = filtered.split()[1]
    assert ctx.restore({"path": token}) == {"path": r"C:\Company\part.sldprt"}


@pytest.mark.parametrize("path", [
    "C:/Users/test/Documents/Фланец.SLDPRT",
    r"C:\Client Projects\part.sldprt",
    r"\\server\Client Projects\part.sldprt",
])
def test_sentence_period_after_cad_path_does_not_enter_tool_argument(path):
    from cadnext.privacy import PrivacyContext

    ctx = PrivacyContext(local=False, consent=True)
    prose = f"Открой {path}. Затем покажи деталь и проверь размеры."
    filtered = ctx.outgoing(prose)
    token = filtered.split()[1].removesuffix(".")
    assert ctx.restore({"path": token}) == {"path": path}
    assert "Затем покажи" in filtered
    assert ctx.restore(filtered) == prose


@pytest.mark.parametrize("path", [
    r"C:\Client Projects\version.2\part.SLDPRT",
    r"\\server\Client Projects\part.sldprt",
    "C:/Client Projects/part.sldprt",
])
def test_cad_path_boundary_precedes_decimal_measurements_in_following_prose(path):
    from cadnext.privacy import PrivacyContext

    ctx = PrivacyContext(local=False, consent=True)
    prose = f"Открой {path}. SHA256 abc123. Объём 478388.5960025985. Проверь."
    filtered = ctx.outgoing(prose)
    token = filtered.split()[1].removesuffix(".")
    assert ctx.restore({"path": token}) == {"path": path}
    assert "Объём 478388.5960025985" in filtered
    assert ctx.restore(filtered) == prose


@pytest.mark.parametrize('path', [
    r'C:\Client Projects\SecretCustomer\prototype.sldprt',
    r'\\server\Client Projects\SecretCustomer\prototype.sldprt',
    r'C:\Client Projects\SecretCustomer',
    r'C:\Client, Inc\SecretCustomer\prototype.sldprt',
    r'C:\Client; Projects\SecretCustomer\prototype.sldprt',
])
def test_unquoted_windows_path_with_spaces_is_fully_masked_in_all_roles(path):
    from cadnext.privacy import PrivacyContext

    context = PrivacyContext(local=False, consent=True)
    messages = [{'role': role, 'content': f'Открой {path}'}
                for role in ('user', 'assistant', 'tool')]
    filtered = context.outgoing(messages)
    assert 'SecretCustomer' not in str(filtered)
    assert 'Projects' not in str(filtered)
    assert context.restore(filtered) == messages


def test_image_json_text_is_removed_without_consent():
    import json

    from cadnext.privacy import PrivacyContext

    ctx = PrivacyContext(local=False, consent=True)
    filtered = ctx.outgoing(json.dumps({"data": "AAAA", "mimeType": "image/png"}))
    assert "AAAA" not in str(filtered)


def test_schema_properties_named_like_keywords_keep_their_types():
    from cadnext.privacy import PrivacyContext

    ctx = PrivacyContext(local=False, consent=True, sensitive=["string"])
    tools = [{"type": "function", "function": {"name": "read", "parameters": {
        "type": "object", "properties": {"title": {"type": "string",
        "description": "string"}}, "required": ["title"]}}}]
    parameters = ctx.outgoing_tools(tools)[0]["function"]["parameters"]
    assert parameters["properties"]["title"]["type"] == "string"
    assert parameters["properties"]["title"]["description"] != "string"
