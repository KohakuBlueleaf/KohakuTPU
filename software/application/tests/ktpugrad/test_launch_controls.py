"""Launch controls: view forms are opt-in; the other three are on by default."""

import pytest

from kohakutpu.application.ktpugrad import run as R

CONTROLS = (
    ("KTPU_VIEW_FORMS", "view_forms", R.view_forms_enabled, False),
    ("KTPU_ELIDE_CONTIGUOUS", "elide_contiguous", R.elide_contiguous_enabled, True),
    ("KTPU_SINGLE_PACKAGE_AST", "single_package_ast", R.single_package_enabled, True),
    ("KTPU_HORIZONTAL_FUSION", "horizontal_fusion", R.horizontal_fusion_enabled, True),
)


@pytest.mark.parametrize("env,key,enabled,default", CONTROLS)
def test_launch_controls_default_and_override(monkeypatch, env, key, enabled, default):
    monkeypatch.delenv(env, raising=False)
    assert enabled() is default
    with R.knobs(**{key: not default}):
        assert enabled() is not default
        monkeypatch.setenv(env, "1" if default else "0")
        assert enabled() is default
        monkeypatch.delenv(env)
    assert enabled() is default
    monkeypatch.setenv(env, "0" if default else "1")
    assert enabled() is not default


@pytest.mark.parametrize(
    "env,key,enabled,default",
    [c for c in CONTROLS if c[1] != "single_package_ast"],
)
def test_public_controls_remain_live(monkeypatch, env, key, enabled, default):
    monkeypatch.delenv(env, raising=False)
    attr = key.upper()
    monkeypatch.setattr(R, attr, True)
    assert enabled()
    with R.knobs(**{key: False}):
        assert getattr(R, attr) is False
        assert not enabled()
    assert getattr(R, attr) is True and enabled()
