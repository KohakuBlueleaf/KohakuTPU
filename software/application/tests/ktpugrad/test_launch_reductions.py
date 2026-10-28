"""Single-package sums obey the stored-partial absolute error budget."""

import json
import math
import os
from pathlib import Path

import numpy as np
import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.ktpugrad import run as R
from kohakutpu.application.runtime import rows as RW
from kohakutpu.application.runtime import tree as TR
from kohakutpu.application.runtime.storage import FLAT, Storage


@pytest.mark.parametrize("kind,shape", [("wide", (16, 2368)), ("column", (400, 32))])
def test_single_package_precision_and_mutants(dev, kind, shape, tmp_path):
    rng = np.random.default_rng(2026)
    values = rng.uniform(0.1, 1.0, shape)
    if kind == "wide":
        # Distinct partial members: aliasing one half to the other changes the sum.
        values *= np.linspace(0.125, 8.0, shape[1])[None, :]
    values = values.astype(np.float16)
    axis = 1 if kind == "wide" else 0
    reference = values.astype(np.float32).sum(axis=axis)
    terms = shape[axis]
    depth = math.ceil(math.log2(terms)) + 2
    unit = 2**-11
    # NUM lowered reduction budget: pre-cancellation magnitude, f16/E8 and CPU-f32 terms.
    gamma = depth * unit / (1 - depth * unit)
    budget = (gamma + terms * 2**-24 + depth * 2**-15) * np.abs(
        values.astype(float)
    ).sum(axis=axis) + terms * depth * 2**-25
    src, dst = Storage(dev, values.nbytes), Storage(dev, max(256, reference.nbytes))
    run = RW.run if kind == "wide" else TR.column
    operand = Operand("full", pitch=shape[1])
    out = Operand("row") if kind == "wide" else Operand("full", pitch=shape[1])
    records = []
    try:
        dev.write(src.addr, values.tobytes())
        for enabled, mutant in [
            (False, None),
            (True, None),
            (True, "drop_member"),
            (True, "wrong_order"),
            (True, "alias_members"),
        ]:
            dev.write(dst.addr, bytes(dst.nbytes))
            mark = len(dev.runs)
            with R.knobs(single_package_ast=enabled, mutant=mutant):
                run(
                    dev,
                    X.Reduce("sum", X.Load(0)),
                    [operand],
                    {"in0": src.addr},
                    *shape,
                    out,
                    dst.addr,
                )
            actual = np.frombuffer(
                dev.read(dst.addr, reference.size * 2), np.float16
            ).astype(float)
            absolute = np.abs(actual - reference)
            passed = bool(np.all(absolute <= budget))
            records.append(
                dict(
                    kind=kind,
                    enabled=enabled,
                    mutant=mutant,
                    passed=passed,
                    launches=len(dev.runs) - mark,
                    cycles=sum(r.cycles for r in dev.runs[mark:]),
                    max_abs=float(absolute.max()),
                    max_rel=float(absolute.max() / reference.max()),
                    max_budget=float(budget.max()),
                )
            )
            if mutant is None:
                assert passed, records[-1]
            else:
                assert not passed, records[-1]
        # The records go to KTPU_REDUCTION_EVIDENCE when set.
        dest = Path(os.environ.get("KTPU_REDUCTION_EVIDENCE") or tmp_path)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / f"reduction-{kind}.json").write_text(
            json.dumps(records, indent=2), encoding="utf-8"
        )
    finally:
        src.free()
        dst.free()


def test_view_snapshot_survives_source_update(dev):
    source, destination = Storage(dev, 256), Storage(dev, 256)
    try:
        original = np.arange(128, dtype=np.float16).tobytes()
        dev.write(source.addr, original)
        form = destination.share_form(("snapshot",), source.addr, 256, src_st=source)
        dev.write(source.addr, bytes(256))
        source.wrote(FLAT)
        assert dev.read(form.addr, 256) == original
        assert form.addr != source.addr and form.owned
        refreshed = destination.share_form(
            ("snapshot",), source.addr, 256, src_st=source
        )
        assert refreshed.addr == form.addr
        assert dev.read(refreshed.addr, 256) == bytes(256)
    finally:
        source.free()
        destination.free()
