"""A refused hook installation leaves the previous lowering usable."""

import sys

import pytest

from kohakutpu.application.ktpugrad import seam
from kohakutpu.application.ktpugrad.device import MODULE

ABSENT = object()


def snapshot():
    mappings = [
        (vars(seam), "_saved"),
        (vars(seam), "FUSE_MODE"),
        (vars(seam), "FUSE_BUDGET"),
        (seam.rangeify.DEVICE_MAX_BUFS, seam.NAME),
        (vars(seam.realize), seam.SEAM),
        (vars(seam.tensor), seam.RUN),
        (vars(seam.graph_walk), "ENABLED"),
        (vars(seam.rangeify), "pm_remove_bufferize"),
        (vars(seam.rangeify), "earliest_rewrites"),
        (vars(seam.tensor), "create_linear_with_vars"),
        (vars(seam.tensor), "_apply_map_to_tensors"),
        (vars(seam.tensor), "transform_to_call"),
        (vars(seam.eager_schedule), "ENABLED"),
        (vars(seam.graph_walk), "REWRITE"),
        (vars(seam.call_capture), "ENABLED"),
        (sys.modules, MODULE),
    ]
    module = sys.modules.get(MODULE)
    if module is not None:
        mappings.append((vars(module), "KTPUDevice"))
    return (
        [(v, v.value) for v, _ in seam._PINS],
        seam.CapturedJit.__call__,
        [(m, k, m.get(k, ABSENT)) for m, k in mappings],
    )


def restore(state):
    pins, captured, mappings = state
    for v, old in pins:
        v.value = old
    seam.CapturedJit.__call__ = captured
    for mapping, key, old in mappings:
        if old is ABSENT:
            mapping.pop(key, None)
        else:
            mapping[key] = old


@pytest.mark.parametrize("installed", [False, True])
@pytest.mark.parametrize("failure", ["mode", "budget", "refresh"])
def test_failed_install_restores_previous_hooks(monkeypatch, installed, failure):
    original = snapshot()
    decisions = dict(seam.fusion.DECISIONS)
    try:
        seam.uninstall()
        monkeypatch.setenv(seam.FUSE_ENV, "elementwise")
        if installed:
            seam.install()
        seam.rangeify.DEVICE_MAX_BUFS[seam.NAME] = 13
        before = snapshot()
        seam.fusion.DECISIONS["transaction witness"] = 17

        def refuse(*args):
            raise ValueError("injected installation failure")

        if failure == "mode":
            monkeypatch.setenv(seam.FUSE_ENV, "invalid")
        elif failure == "budget":
            monkeypatch.setattr(seam.fusion.FusionBudget, "environment", refuse)
        else:

            def refresh():
                seam.graph_walk.ENABLED = not seam.graph_walk.ENABLED
                raise ValueError("injected installation failure")

            monkeypatch.setattr(seam, "refresh_knobs", refresh)
        with pytest.raises(ValueError):
            seam.install()
        pins, captured, mappings = before
        assert all(v.value == old for v, old in pins)
        assert seam.CapturedJit.__call__ is captured
        for mapping, key, old in mappings:
            assert mapping.get(key, ABSENT) is old
        assert seam.fusion.DECISIONS["transaction witness"] == 17
    finally:
        restore(original)
        seam.fusion.DECISIONS.clear()
        seam.fusion.DECISIONS.update(decisions)
