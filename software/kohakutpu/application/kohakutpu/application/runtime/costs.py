"""Measured, per-recipe alternatives; no kernel-name or shape special cases.

A profile is deployment data, not a compiler tuning switch. Unmeasured recipes
keep their original path. Each record contains the competing measured costs
and a value witness; the cheaper alternative alone is admitted.
"""

import json
import os
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=8)
def _profile(path):
    doc = json.loads(Path(path).read_text())
    admitted = {}
    for encoded, record in doc["recipes"].items():
        if record["verified"] and record["alternative"] < record["original"]:
            kind, data = json.loads(encoded)
            admitted.setdefault((kind, data["board"]), {})[encoded] = (
                data["recipe"],
                record["alternative"],
            )
    return doc["schedule"], admitted


def _records(kind, board):
    path = os.environ.get("KTPU_COST_PROFILE")
    if path is None:
        return {}
    expected, admitted = _profile(path)
    schedule = dict(
        finite_batch=int(os.environ.get("KTPU_PIPE_FINITE_BATCH", "4")),
        overlap=os.environ.get("KTPU_PIPE_OVERLAP", "1") == "1",
    )
    if os.environ.get("KTPU_QUANT_CLASSIFIER_OVERLAP", "0") == "1":
        schedule["classifier_overlap"] = True
    if os.environ.get("KTPU_QUANT_PIPELINE", "1") == "1":
        schedule["pipeline"] = True
    if os.environ.get("KTPU_QUANT_ZERO_CLASSIFIER", "1") == "1":
        schedule["zero_classifier"] = True
    if os.environ.get("KTPU_QUANT_STREAM_AXES", "1") == "1":
        schedule["stream_axes"] = True
    if os.environ.get("KTPU_QUANT_FILL_OVERLAP", "1") == "1":
        schedule["fill_overlap"] = True
    if os.environ.get("KTPU_PULL_TREE", "1") == "1":
        schedule["pull_tree"] = True
    if os.environ.get("KTPU_QUANT_ZERO_REUSE", "1") == "1":
        schedule["zero_reuse"] = True
    if os.environ.get("KTPU_CLASS_ROWS_DIRECT", "0") == "1":
        schedule["class_rows_direct"] = True
    if os.environ.get("KTPU_CLASS_ROWS_ROUTE", "0") == "1":
        schedule["class_rows_route"] = True
    band = int(os.environ.get("KTPU_CLASS_BAND", "4"))
    if band != 1:
        schedule["class_band"] = band
    return admitted.get((kind, board), {}) if expected == schedule else {}


def key(kind, recipe):
    return json.dumps([kind, recipe], sort_keys=True, separators=(",", ":"))


def choose(kind, recipe, board):
    return key(kind, dict(board=board, recipe=recipe)) in _records(kind, board)


def quant_recipe(src, rows, cols, rp, cp, side, g, nk, batches):
    return dict(
        src=dict(dtype=src.dtype, pitch=src.pitch, offset=src.offset),
        rows=rows,
        cols=cols,
        rp=rp,
        cp=cp,
        side=side,
        g=g,
        nk=nk,
        batches=batches,
    )


def prepared_quant(steps, src, rows, cols, rp, cp, side, g, nk, batches, board):
    """Find the cheapest measured layout-equivalent packing read.

    Destination row pitch is an intermediate choice. Compare its logical
    (row, column) map instead, retaining source strides and packed layout.
    """
    requested = steps_recipe(steps)
    wanted = packing_map(requested[-1], dict(pitch=src.pitch, offset=src.offset), cols)
    match = dict(rows=rows, rp=rp, cp=cp, side=side, g=g, nk=nk, batches=batches)
    candidates = []
    for recipe, cost in _records("prepared_quant", board).values():
        q = recipe["quant"]
        if (
            recipe["steps"][:-1] == requested[:-1]
            and all(q[k] == v for k, v in match.items())
            and wanted is not None
            and packing_map(recipe["steps"][-1], q["src"], q["cols"]) == wanted
        ):
            candidates.append((cost, recipe))
    return min(candidates, key=lambda x: x[0])[1] if candidates else None


def packing_map(step, src, cols):
    pitch = src["pitch"]
    dat = step["dat"] - src["offset"]
    row0, col0 = divmod(dat, pitch)
    axes = []
    for (n, s), (nd, d) in zip(step["walk"], step["dwalk"]):
        if n != nd:
            return None
        r, c = divmod(d, pitch)
        axes.append((n, s, r, c))
    if col0 + sum((n - 1) * c for n, _, _, c in axes) >= min(cols, pitch):
        return None
    return step["n"], step["at"], row0, col0, tuple(axes)


def has_steps(steps, board):
    """Do not defer a producer unless a measured winning consumer can take it."""
    wanted = steps_recipe(steps)
    return any(
        recipe["steps"] == wanted
        for recipe, _ in _records("prepared_quant", board).values()
    )


def steps_recipe(steps):
    return [
        dict(
            n=s.n,
            walk=[list(a) for a in s.walk],
            at=s.at,
            dwalk=[list(a) for a in s.dwalk],
            dat=s.dat,
            m=s.m,
        )
        for s in steps
    ]
