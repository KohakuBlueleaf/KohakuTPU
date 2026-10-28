"""Capture live convert edges and generator geometry, without compiling."""

import argparse
import json
from pathlib import Path
from unittest.mock import patch

from templates import prebuild
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import launch_reductions as LR


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("output", type=Path)
    args = ap.parse_args()
    events, layouts, quants, trees, queries = [], [], [], [], []
    dev = D.attach(D.Device(kind="offline", board="multimesh_v9t6", classes=False))
    relayout, quant, tree = FM.relayout, MM.quant_kernel, LR.column_tree_kernel
    select = FM.costs.prepared_quant
    eligible = FM.costs.has_steps
    eligibility = []

    def layout(device, src, n, walk, at, dwalk, m, dst, dat=0, keep=False):
        start = len(events)
        result = relayout(device, src, n, walk, at, dwalk, m, dst, dat, keep)
        layouts.append(
            dict(
                start=start,
                stop=len(events),
                src=src,
                n=n,
                walk=walk,
                at=at,
                dwalk=dwalk,
                m=m,
                dst=dst,
                dat=dat,
            )
        )
        return result

    def packing(src, rows, cols, rp, cp, side, g, nk, batches=1, **kw):
        result = quant(src, rows, cols, rp, cp, side, g, nk, batches, **kw)
        quants.append(
            dict(
                index=len(events),
                name=result[0].name,
                rows=rows,
                cols=cols,
                rp=rp,
                cp=cp,
                side=side,
                g=g,
                nk=nk,
                batches=batches,
                pitch=src.pitch,
                offset=src.offset,
            )
        )
        return result

    def column(e, operands, R, W, out, cores, **kwargs):
        result = tree(e, operands, R, W, out, cores, **kwargs)
        trees.append(
            dict(
                index=len(events),
                name=result[0].name,
                e=FM.generated.expr(e),
                operands=[vars(o) for o in operands],
                R=R,
                W=W,
                out=vars(out),
            )
        )
        return result

    def selection(steps, src, rows, cols, rp, cp, side, g, nk, batches, board):
        result = select(steps, src, rows, cols, rp, cp, side, g, nk, batches, board)
        queries.append(
            dict(
                index=len(events),
                steps=FM.costs.steps_recipe(steps),
                quant=FM.costs.quant_recipe(
                    src, rows, cols, rp, cp, side, g, nk, batches
                ),
                selected=result,
            )
        )
        return result

    def admission(steps, board):
        result = eligible(steps, board)
        eligibility.append(
            dict(index=len(events), steps=FM.costs.steps_recipe(steps), selected=result)
        )
        return result

    with (
        patch.object(FM.costs, "has_steps", admission),
        patch.object(FM.costs, "prepared_quant", selection),
        patch.object(FM, "relayout", layout),
        patch.object(MM, "quant_kernel", packing),
        patch.object(LR, "column_tree_kernel", column),
    ):
        doc = prebuild.capture(dev, 8, edge_events=events)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, data in (
        ("capture", doc),
        ("events", events),
        ("layouts", layouts),
        ("quants", quants),
        ("trees", trees),
        ("queries", queries),
        ("eligibility", eligibility),
    ):
        (args.output / f"{name}.json").write_text(json.dumps(data))
    print("events", len(events), "layouts", len(layouts), "quant calls", len(quants))


if __name__ == "__main__":
    main()
