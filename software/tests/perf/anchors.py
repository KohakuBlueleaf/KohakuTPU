"""The ground truth of `params.json`: every anchor kernel on a Verilated card, a few
hundred thousand cycles in all, a JSON line a kernel. With `--trace`, a recorded
workload's small kernels instead: the three-way reading of model, Verilator and card
that tells a timing error in the core from what only the card's memory costs.

    python software/tests/perf/anchors.py --build MODEL --board multimesh_v9t6 --out OUT
    python software/tests/perf/anchors.py ... --trace TRACE --most 40000 --each 8
    python software/tests/perf/anchors.py --profile mid --board multimesh_v9t6 --out OUT
"""

import argparse
import collections
import json
import pathlib
import re

from kohakutpu.application.runtime.card import SiliconCard, VerilatorCard
from kohakutpu.application.tools import l1
from kohakutpu.language import examples
from kohakutpu.stack import kernel

HERE = pathlib.Path(__file__).parent


def authored(only: str, card: bool, module: pathlib.Path | None):
    paths = sorted((HERE / "anchors").glob("*.ktpu"))
    if card:
        paths += sorted((HERE / "anchors" / "memory").glob("*.ktpu"))
    for path in [module] if module else paths:
        loaded = examples.load(path)
        for fn in loaded.names():
            if re.search(only, fn):
                yield fn, loaded


def recorded(trace: pathlib.Path, most: int, each: int):
    """Up to `each` kernels a kind whose card run stays under `most` cycles, spread
    over the sizes that are there, largest first: every one fits the Verilator tier."""
    kinds = collections.defaultdict(dict)
    for line in (trace / "card.jsonl").read_text("utf-8").splitlines():
        r = json.loads(line)
        kinds[r["kind"]].setdefault(r["name"], r["steps"])
    for kind in sorted(kinds):
        small = sorted(
            (n for n, s in kinds[kind].items() if s <= most),
            key=lambda n: -kinds[kind][n],
        )
        for name in small[:: max(1, len(small) // each)][:each]:
            text = (trace / "kernels" / f"{name}.l1").read_text("utf-8")
            yield name, text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build")
    ap.add_argument("--profile", help="run on the card, at this clock profile")
    ap.add_argument("--board", required=True)
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--nodes", default="0", help="card nodes, comma separated")
    ap.add_argument("--module", type=pathlib.Path, help="one L1 file, not the anchors")
    ap.add_argument("--trace", type=pathlib.Path)
    ap.add_argument("--most", type=int, default=40000)
    ap.add_argument("--each", type=int, default=6)
    a = ap.parse_args()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    if a.profile:
        nodes = tuple(int(n) for n in a.nodes.split(","))
        card = SiliconCard(
            a.board, transport="xdma", nodes=nodes, profile=a.profile, check="warn"
        )
    else:
        log = a.out.with_suffix(".model.log")
        card = VerilatorCard(a.build, board=a.board, log=log, check="warn")
    kernels = (
        recorded(a.trace, a.most, a.each)
        if a.trace
        else authored(a.only, bool(a.profile), a.module)
    )
    with a.out.open("w", encoding="utf-8") as out:
        for fn, source in kernels:
            try:
                c = kernel.compile(source, fn, card.machine, level="l1")
                got = l1.run_compiled(card, c, {})
                row = dict(
                    name=fn,
                    cycles=got["cycles"],
                    cold=got["cold"],
                    phases=got["phases"],
                )
            except Exception as error:  # noqa: BLE001
                row = dict(name=fn, error=repr(error)[:300])
            print(row, flush=True)
            out.write(json.dumps(row) + "\n")
            out.flush()
    card.close()


if __name__ == "__main__":
    main()
