"""A point's outputs against CPU f32 and simulated MXFP7 (`checks.accuracy`), by
group (row tile or head), and its exact witnesses. Statistics only: no threshold
decides anything here."""

import numpy as np
from tinygrad.tensor import Tensor

from h.s0.cases import Case, Rep
from kohakutpu.application import ktpugrad
from kohakutpu.application.checks.accuracy import (
    error_stats,
    mxfp7_operands,
    output_report,
)


def on_cpu(case: Case, rep: Rep) -> np.ndarray:
    x = Tensor(rep.x.astype(np.float32), device="CPU")
    p = {k: Tensor(v.astype(np.float32), device="CPU") for k, v in rep.p.items()}
    return case.one(x, p).numpy()


def references(case: Case, reps: list[Rep]) -> tuple[list, list]:
    """Each repetition's CPU f32 output and its output with every matrix operand
    read as MXFP7, on tinygrad's own lowering."""
    ktpugrad.uninstall()
    f32 = [on_cpu(case, r) for r in reps]
    with mxfp7_operands():
        mx = [on_cpu(case, r) for r in reps]
    ktpugrad.install()
    return f32, mx


def witnessed(case: Case, outs: list[np.ndarray], reps: list[Rep]) -> dict:
    """Per witness: elements compared, how many differ (NaN equals NaN), the
    largest difference."""
    got: dict = {}
    for o, rep in zip(outs, reps, strict=True):
        for label, have, want in case.witness(o, rep):
            h, w = have.astype(np.float64), want.astype(np.float64)
            same = (h == w) | (np.isnan(h) & np.isnan(w))
            e = got.setdefault(label, dict(elements=0, unequal=0, max_abs=0.0))
            e["elements"] += int(same.size)
            e["unequal"] += int((~same).sum())
            e["max_abs"] = max(e["max_abs"], float(np.abs(h - w)[~same].max(initial=0)))
    return got


def joined(case: Case, arrays, label: str) -> np.ndarray:
    """Group `label` of every repetition's array, flat."""
    return np.concatenate([case.groups(a)[label].ravel() for a in arrays])


def checked(case: Case, outs: list, reps: list[Rep], f32: list, mx: list) -> dict:
    """The point's outputs (one a repetition) against both references, whole and
    by group, its nonfinite count and witnesses."""
    got = np.stack([o.astype(np.float32) for o in outs])
    report = output_report(got, np.stack(f32), np.stack(mx))
    report.pop("top1_f32")
    report.pop("top1_mxfp7")
    groups = {}
    for label in case.groups(outs[0]):
        g, w, m = (joined(case, a, label) for a in (got, f32, mx))
        groups[label] = dict(card_f32=error_stats(g, w), card_mxfp7=error_stats(g, m))
    return dict(
        report,
        nonfinite=int((~np.isfinite(got)).sum()),
        groups=groups,
        witness=witnessed(case, outs, reps),
    )
