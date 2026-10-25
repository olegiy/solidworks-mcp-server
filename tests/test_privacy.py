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
