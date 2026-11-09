"""The retained artifact cache owns validity, including failed re-preparation."""

from types import MappingProxyType, SimpleNamespace

import pytest

from kohakutpu.application.runtime.graph import split


@pytest.mark.parametrize("refuse_rebind", [False, True])
def test_artifact_epoch_is_committed_only_after_a_successful_bind(
    monkeypatch, refuse_rebind
):
    actions = []
    dev = SimpleNamespace(table_epoch=3, drain=lambda: actions.append("drain"))
    prepared = split.PreparedSplitGraph(dev, None, MappingProxyType({}), (), (), [])
    artifacts = [object(), object()]
    attempts = []
    refuse = False

    def bind(owner):
        assert owner is prepared
        actions.append("bind")
        attempts.append(dev.table_epoch)
        if refuse:
            raise ValueError("table proof refused")
        # A binder need not know how its caller caches the returned artifact.
        return artifacts[0 if dev.table_epoch == 3 else 1]

    monkeypatch.setattr(split, "bind", bind)
    assert prepared.bind_artifact() is artifacts[0]
    assert prepared.proof_epoch == 3
    backend = SimpleNamespace(close=lambda: actions.append("close"))
    prepared.backends.append(backend)
    arena = SimpleNamespace(unretain=lambda at: actions.append(("unretain", at)))
    ref = (arena, 256)
    prepared.references.append(ref)
    prepared.bound_references.append(ref)
    for _ in range(3):
        assert prepared.bind_artifact() is artifacts[0]
    assert attempts == [3]
    assert actions == ["bind"]
    assert prepared.backends == [backend]
    assert prepared.references == [ref]

    dev.table_epoch += 1
    refuse = refuse_rebind
    if refuse:
        with pytest.raises(ValueError, match="table proof refused"):
            prepared.bind_artifact()
        assert prepared.artifact is None
        assert prepared.proof_epoch == 3
        assert actions == ["bind", "drain", "close", ("unretain", 256), "bind"]
        refuse = False
    assert prepared.bind_artifact() is artifacts[1]
    assert prepared.proof_epoch == 4
    assert prepared.backends == []
    assert prepared.bound_references == []
    assert prepared.references == []
    before = list(actions)
    for _ in range(3):
        assert prepared.bind_artifact() is artifacts[1]
    assert actions == before
    assert attempts == ([3, 4, 4] if refuse_rebind else [3, 4])
    assert actions.count("close") == 1
    assert actions.count(("unretain", 256)) == 1
    assert actions.count("drain") == 1
