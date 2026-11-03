"""Round h Stage 0 (S0.1 shard-rate curves, S0.2 producer packing pairs) on one
sysnode, through ktpugrad: each case's graph (`cases`) at each R in one TinyJit,
called until it has replayed `--replays` times, every launch's card and measure
records labelled, the last replay's outputs checked.

    PYTHONPATH=software/tests/perf python -m h.s0.run --board multimesh_v9t8 \
        --profile mid --measure
    ... --device double --only 'proj.n192' --reps 1,2 --replays 2 --out DIR

`--sysnode k` runs on sysnode k (sysnodes 0..k opened, the others idle); `--only`
picks cases by name. OUT (default measurements/data/h1/s0.<board>.<profile>)
holds `meta.json` (arguments, commit, firmware image, knobs, every die's clocks,
the timebase before and after and its drift), `launches.jsonl` and `kernels/` (`record.Trace`: each launch, each
kernel's L2 and L1), `card.jsonl` (`record.card_rows`, each row with its case, R,
variant and call), `units.jsonl` (the measure ring, a record a launch, labelled,
each unit with its busy/retired change), `cases.jsonl` (a row a point: shapes,
calls and which replayed, how the ring met the labels, the check), and what
`summary` writes from them.
"""

import argparse
import json
import os
import pathlib
import re
import sys
import time

import record
from tinygrad.engine.jit import TinyJit
from tinygrad.tensor import Tensor

from h import meta
from h.s0 import cases, check, collect, summary
from kohakutpu.application import ktpugrad
from kohakutpu.application.ktpugrad.run import product
from kohakutpu.application.runtime import card as C
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.card import clocks_at
from kohakutpu.application.runtime.card.common import MEASURE_FIRMWARE
from kohakutpu.application.runtime.card.measure import Ring
from kohakutpu.driver.clock.card import load_board

#: Calls past the asked replays before a graph that never replays is given up.
SETTLE = 8


def forward(case: cases.Case, params: list):
    """R repetitions of `case` as one TinyJit, R = len(params)."""

    def run(*xs):
        return [
            case.one(xs[0 if case.shared_input else i], p).realize()
            for i, p in enumerate(params)
        ]

    return TinyJit(run)


def program(labels: list[dict], calls: list[dict], variant: str) -> list:
    """`variant`'s launches (name, kind) in its first replay, in log order."""
    replayed = [c["call"] for c in calls if c["variant"] == variant and c["replayed"]]
    return [
        [x["name"], x["kind"]]
        for x in labels
        if x["variant"] == variant and x["log"] >= 0 and [x["call"]] == replayed[:1]
    ]


def point(case, reps, refs, replays: int, col: collect.Collector, dev) -> dict:
    """`case` at R = len(reps): every variant called in rotating order until each
    has replayed `replays` times; the point's row, its card and ring rows."""
    R = len(reps)
    xs = [
        Tensor(r.x, device="KTPU").realize()
        for r in (reps[:1] if case.shared_input else reps)
    ]
    params = [
        {k: Tensor(v, device="KTPU").realize() for k, v in r.p.items()} for r in reps
    ]
    variants = ("packs", "nopacks") if case.paired else ("",)
    jits = {v: forward(case, params) for v in variants}
    seen, outs, calls, call = dict.fromkeys(variants, 0), {}, [], 0
    while min(seen.values()) < replays and call < replays + SETTLE:
        for v in variants if call % 2 == 0 else variants[::-1]:
            product.PRODUCER_PACKS = v != "nopacks"
            col.tag = dict(case=case.name, R=R, variant=v, call=call)
            before = col.replays
            outs[v] = jits[v](*xs)
            calls.append(dict(call=call, variant=v, replayed=col.replays > before))
            seen[v] += col.replays > before
        call += 1
    product.PRODUCER_PACKS = True
    got = {v: [o.numpy() for o in outs[v]] for v in variants}
    programs = {v: program(col.labels, calls, v) for v in variants}
    card, ring, joined = col.flush(dev)
    rows: list[dict] = [
        dict(
            case=case.name,
            family=case.family,
            full=case.full,
            share=case.share,
            shape=case.shape,
            body=case.body,
            R=R,
            variant=v,
            calls=[c for c in calls if c["variant"] == v],
            program=programs[v],
            ring=joined,
            check=check.checked(case, got[v], reps, refs[0][:R], refs[1][:R]),
        )
        for v in variants
    ]
    for r in rows:
        w = sum(x["unequal"] for x in r["check"]["witness"].values())
        print(case.name, R, r["variant"], "replays", seen[r["variant"]], "witness", w)
    return dict(rows=rows, card=card, ring=ring)


def measured(case: cases.Case, reps: list[int], a, col: collect.Collector, dev):
    """Each point of `case`; a point that raised is a row with its `error`, and the
    launches it made are kept."""
    every = [case.rep(a.seed, i) for i in range(max(reps))]
    refs = check.references(case, every)
    for R in reps:
        try:
            yield point(case, every[:R], refs, a.replays, col, dev)
        except Exception as error:  # noqa: BLE001
            product.PRODUCER_PACKS = True
            print(case.name, R, "raised", repr(error)[:300], flush=True)
            card, ring, joined = col.flush(dev)
            row = dict(case=case.name, R=R, ring=joined, error=repr(error)[:600])
            yield dict(rows=[row], card=card, ring=ring)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", required=True)
    ap.add_argument("--profile", default="mid", help="every die must be at it")
    ap.add_argument("--device", choices=("card", "double"), default="card")
    ap.add_argument("--transport", default="xdma")
    ap.add_argument("--sysnode", type=int, default=0, help="the one that runs")
    ap.add_argument("--only", default="", help="cases whose name matches")
    ap.add_argument("--reps", default="1,8,64", help="repetitions R a point")
    ap.add_argument("--replays", type=int, default=20, help="warm replays a point")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--measure", action="store_true", help="the measure image")
    ap.add_argument("--library", type=pathlib.Path, help="prebuilt kernels")
    ap.add_argument("--out", type=pathlib.Path)
    a = ap.parse_args()
    out = a.out or summary.ROOT / "measurements/data/h1" / f"s0.{a.board}.{a.profile}"
    if (out / "meta.json").exists():
        print(out, "exists: one run a configuration", flush=True)
        return 1
    os.environ["KOHAKUTPU_PROFILE"] = a.profile
    if a.measure:
        C.FIRMWARE = MEASURE_FIRMWARE
    trace = record.Trace(out)
    record.recording(trace, a.library, True, record.Profile(None))
    col = collect.Collector(a.sysnode)
    on_card = a.device == "card"
    dev = D.Device(
        a.device,
        board=a.board,
        nodes=a.sysnode + 1,
        transport=a.transport if on_card else None,
        library=a.library,
        library_only=a.library is not None,
    )
    board = load_board(a.board)
    info: dict = dict(
        args={k: str(v) for k, v in vars(a).items()},
        board=a.board,
        profile=a.profile,
        sysnode=a.sysnode,
        opened=list(range(a.sysnode + 1)),
        commit=meta.commit(),
        knobs=meta.knobs(),
        firmware=meta.image(dev.card.fw) if on_card else None,
        clocks=(
            clocks_at(dev.card.t, board, a.profile, range(board["meshes"]))
            if on_card
            else None
        ),
        timebase=meta.timebase(dev.card) if on_card else None,
        started=time.time(),
    )
    (out / "meta.json").write_text(json.dumps(info, indent=1), "utf-8")
    col.ring = Ring(dev.card) if a.measure else None
    reps = [int(r) for r in a.reps.split(",")]
    ktpugrad.install()
    try:
        with (
            D.attached(dev),
            dev.on_node(a.sysnode),
            (out / "cases.jsonl").open("w", encoding="utf-8") as points,
            (out / "card.jsonl").open("w", encoding="utf-8") as cards,
            (out / "units.jsonl").open("w", encoding="utf-8") as units,
        ):
            for case in cases.table().values():
                if not re.search(a.only, case.name):
                    continue
                for got in measured(case, reps, a, col, dev):
                    points.writelines(json.dumps(r) + "\n" for r in got["rows"])
                    cards.writelines(json.dumps(r) + "\n" for r in got["card"])
                    units.writelines(json.dumps(r) + "\n" for r in got["ring"])
                    for f in (points, cards, units):
                        f.flush()
    finally:
        if on_card:
            after = meta.timebase(dev.card)
            info.update(timebase_after=after, drift=meta.drift(info["timebase"], after))
        info.update(ended=time.time(), counters=dict(dev.counters))
        trace.save(info)
        dev.close()
    summary.summarise(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
