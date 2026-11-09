"""Round h's card runner: every fn of a generated (`h/gen`) or given module on the
opened sysnodes, `--reps` runs each, one directory a configuration.

    python software/tests/perf/hrun.py --board multimesh_v9t6 --profile mid \
        --nodes 0,1,2,3 --gen q0 --reps 64 --measure \
        --out measurements/data/h1/q0.multimesh_v9t6.mid

OUT holds `module.ktpu` (what ran), `meta.json` (arguments, session, commit,
firmware image, boot flags, knobs, every die's clocks, the checker's sensitivity
controls, the sysnodes' bracketed cycle offsets before and after and their drift),
`runs.jsonl` (a row a run: session / name / rep / epoch / phase join keys, cycles,
each sysnode's cycles / phases / steps / fault, bind and redzone addresses, each
output's digest and check, the ledger's mismatches, each die's interlink counters,
the measure records' count, the physical intervals, and the verdict: structural,
precision, timing; a fn that raised leaves a row with `error`, a fn that must be
refused before issue one `admission` row and is never run), `timebase.jsonl` (each fn's offsets
before and after it, and their drift) and `units.jsonl` (with `--measure`, the
measure image's records, keyed like their row).

The runner refuses to start when a checker control (`check.sensitivity`) does not
come out as it must.
"""

import argparse
import collections
import json
import pathlib
import re
import sys
import time
import uuid

from h import check, gen, meta, runner

from kohakutpu.application.runtime.card import SiliconCard, clocks_at, open_transport
from kohakutpu.application.runtime.card.common import (
    FIRMWARE,
    MEASURE_FIRMWARE,
    parse_boot_flags,
)
from kohakutpu.application.runtime.card.measure import Ring
from kohakutpu.driver.clock.card import load_board
from kohakutpu.language import examples
from kohakutpu.stack import kernel

#: The exit status of a row stopped at its first sysnode TIMEOUT: the card's ring
#: state is suspect until every die is retuned, so later runs would only time out.
STUCK = 3


class Stuck(Exception):
    """A sysnode timed out: stop the row."""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", required=True)
    ap.add_argument("--profile", required=True, help="every die must be at it")
    ap.add_argument("--nodes", default="0", help="the sysnodes opened, comma separated")
    what = ap.add_mutually_exclusive_group(required=True)
    what.add_argument("--gen", help="a generator in h/gen")
    what.add_argument("--module", type=pathlib.Path, help="an L1 or L2 file")
    ap.add_argument("--outputs", default="", help="--module: its outputs, by name")
    ap.add_argument("--level", choices=("l1", "l2"), default="l1")
    ap.add_argument("--reps", type=int, default=1, help="runs a fn, a row each")
    ap.add_argument(
        "--flags",
        default="0",
        help="boot flags: V or N:V,.. (ka/boot/args.h: STEPS|TIMING 0xa)",
    )
    ap.add_argument("--faults", action="store_true", help="a sysnode's fault is a row")
    ap.add_argument("--measure", action="store_true", help="the measure image")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--only", default="", help="fns whose name matches")
    ap.add_argument("--transport", default="xdma")
    ap.add_argument("--check", default="warn")
    ap.add_argument("--out", type=pathlib.Path, required=True)
    a = ap.parse_args()
    if (a.out / "meta.json").exists():
        print(a.out, "exists: one run a configuration", flush=True)
        return 1
    controls = check.sensitivity()
    blind = check.insensitive(controls)
    if blind:
        print("checker controls not as they must be:", blind, flush=True)
        return 1
    sysnodes = tuple(int(n) for n in a.nodes.split(","))
    mod = gen.load(a.gen, sysnodes, a.board) if a.gen else None
    a.out.mkdir(parents=True, exist_ok=True)
    path = a.out / "module.ktpu"
    path.write_text(mod.source if mod else a.module.read_text("utf-8"), "utf-8")
    loaded = examples.load(path)
    specs = (
        mod.specs
        if mod
        else dict.fromkeys(
            loaded.names(), gen.Spec(tuple(a.outputs.split(",")) if a.outputs else ())
        )
    )
    # Every die at the profile, opened or not, before any sysnode boots.
    t, board = open_transport(a.transport), load_board(a.board)
    clocks = clocks_at(t, board, a.profile, range(board["meshes"]))
    card = SiliconCard(
        a.board,
        transport=t,
        nodes=sysnodes,
        profile=a.profile,
        check=a.check,
        fw=MEASURE_FIRMWARE if a.measure else FIRMWARE,
        boot_flags=parse_boot_flags(a.flags, sysnodes),
    )
    runner.initialise(card)
    session = uuid.uuid4().hex
    info = dict(
        args={k: str(v) for k, v in vars(a).items()},
        session=session,
        board=a.board,
        profile=a.profile,
        sysnodes=sysnodes,
        boot_flags=card.boot_flags,
        firmware=meta.image(card.fw),
        commit=meta.commit(),
        knobs=meta.knobs(),
        clocks=clocks,
        checker=controls,
        timebase=meta.timebase(card),
        started=time.time(),
    )
    (a.out / "meta.json").write_text(json.dumps(info, indent=1), "utf-8")
    ring = Ring(card) if a.measure else None
    try:
        with (
            (a.out / "runs.jsonl").open("w", encoding="utf-8") as rows,
            (a.out / "units.jsonl").open("w", encoding="utf-8") as units,
            (a.out / "timebase.jsonl").open("w", encoding="utf-8") as clocks_out,
        ):
            for name in loaded.names():
                if not re.search(a.only, name):
                    continue
                if specs[name].refused:
                    row = runner.admission(
                        card, loaded, name, a.level, specs[name], session
                    )
                    rows.write(json.dumps(row) + "\n")
                    print(name, "admission", row["verdict"]["structural"], flush=True)
                    continue
                cycles, verdicts = [], collections.Counter()
                before = meta.timebase(card)
                try:
                    c = kernel.compile(loaded, name, card.machine, level=a.level)
                    for row, records in runner.runs(
                        card, c, specs[name], a.seed, a.reps, a.faults, ring, session
                    ):
                        cycles.append(row["cycles"])
                        verdicts[row["verdict"]["structural"]] += 1
                        rows.write(json.dumps(row) + "\n")
                        units.writelines(json.dumps(r) + "\n" for r in records)
                        if any("TIMEOUT" in w for w in row["verdict"]["reasons"]):
                            raise Stuck(name)
                except Stuck:
                    raise
                except Exception as error:  # noqa: BLE001
                    row = dict(
                        session=session,
                        name=name,
                        rep=len(cycles),
                        error=repr(error)[:600],
                    )
                    rows.write(json.dumps(row) + "\n")
                after = meta.timebase(card)
                clocks_out.write(
                    json.dumps(
                        dict(
                            session=session,
                            name=name,
                            before=before,
                            after=after,
                            drift=meta.drift(before, after),
                        )
                    )
                    + "\n"
                )
                rows.flush()
                cycles.sort()
                mid = cycles[len(cycles) // 2] if cycles else None
                print(
                    name, len(cycles), "runs, median", mid, dict(verdicts), flush=True
                )
    except Stuck as stuck:
        info["stuck"] = str(stuck)
        print("STUCK at", stuck, flush=True)
        return STUCK
    finally:
        after = meta.timebase(card)
        info.update(
            timebase_after=after,
            drift=meta.drift(info["timebase"], after),
            ended=time.time(),
        )
        (a.out / "meta.json").write_text(json.dumps(info, indent=1), "utf-8")
        card.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
