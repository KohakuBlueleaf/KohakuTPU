"""Laid-read repitch consumes a current form, never stale flat storage."""

from types import SimpleNamespace

import numpy as np
import pytest
from tinygrad.tensor import Tensor

from kohakutpu.application.ktpugrad import run as R
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime.storage import FLAT


def witness(monkeypatch, stale):
    dev = D.Device(kind="offline", board="multimesh_v9t6")
    # This witness exercises the materialized producer, not the independent
    # direct dense-repitch elision (its Access/range inputs are intentionally stubbed).
    monkeypatch.setattr(R, "elide_rule", lambda rule: False)
    monkeypatch.setattr(R, "_size", lambda axes: axes[0])
    monkeypatch.setattr(R, "_laid_plan", lambda *args: None)
    made = []

    def bare(dev, plan, bufs):
        st = bufs["laid"]
        key = FM.rows_key(40, 64)
        st.form(key, 36 * 64 * 2)
        st.wrote(key)
        made.append(st)
        return []

    def current(dev, st, dtype, n):
        assert FM.padded(st) is not None
        if not stale:
            st.made(FLAT)
        return st.addr

    def repitch(dev, src, *args):
        assert FLAT in made[0].valid, "repitch read stale flat storage"

    monkeypatch.setattr(R, "_bare", bare)
    monkeypatch.setattr(FM, "f16", current)
    monkeypatch.setattr(FM, "repitch", repitch)
    out, _ = R._laid_read(
        dev, None, SimpleNamespace(slot=0), [36], [40], 64, {}, "laid"
    )
    out.free()


def test_laid_read_materializes_current_form(monkeypatch):
    witness(monkeypatch, False)


def test_mutant_laid_read_stale_flat_is_caught(monkeypatch):
    with pytest.raises(AssertionError, match="stale flat"):
        witness(monkeypatch, True)


def test_hinted_laid_read_on_the_card(dev, monkeypatch):
    """Packing teaches the transpose producer rows; a later sum needs flat."""
    rng = np.random.default_rng(12)
    y = rng.standard_normal((40, 36)).astype(np.float16)
    x = rng.standard_normal((36, 40)).astype(np.float16)
    monkeypatch.setattr(FM, "PREPARED", False)
    (Tensor(y, device="KTPU").T @ Tensor(x, device="KTPU").T).numpy()
    want = (y.astype(np.float32) * 3).sum(0)
    for prepared in (False, True):
        monkeypatch.setattr(FM, "PREPARED", prepared)
        got = (Tensor(y, device="KTPU") * 3).sum(0).numpy()
        assert np.isfinite(got).all()
        assert np.abs(got - want).max() < 0.002 * np.abs(want).max()
