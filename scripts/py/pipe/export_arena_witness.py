"""Prebuild the prepared epilogue regression through the actual graph library path."""

import argparse
import json
from pathlib import Path

from export_folds import expression
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels import relayout as RL
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language import machine as MF
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--work",
        type=Path,
        required=True,
        help="pipe work tree: reads loop5/chains/2458.json, writes under loop6/",
    )
    args = ap.parse_args()
    base = args.work / "loop6"
    rec = json.loads((args.work / "loop5/chains/2458.json").read_text())
    r = rec["recipe"]
    mm = MM.Mm(**r["mm"])
    for board in ("vlt_v9t6s_1n", "multimesh_v9t6"):
        machine = machine_of(board, nodes=1, packing_backend="vector_mx7")
        hw = RL.hw_of(MF.load(board), machine.count("VC"))
        s = r["steps"]["1"][0]
        plan = RL.plan(
            s["n"],
            tuple(map(tuple, s["walk"])),
            s["at"],
            tuple(map(tuple, s["dwalk"])),
            s["dat"],
            s["m"],
            False,
            hw,
        )
        kernel, given = MM.mm_kernel(
            mm,
            expression(r["e"]),
            tuple(Operand(**o) for o in r["extras"]),
            Operand(**r["out"]),
            machine.count("MG"),
            machine.count("VC"),
            made={1: (plan,)},
            hw=hw,
        )
        graph = kernel.trace(given, machine=K.dsl_machine(machine)).graph_json
        result = rs.library_prebuild(
            str(base / "fixed-library"), graph, rs.machine_json(machine)
        )
        (base / f"arena-witness-{board}.json").write_text(
            json.dumps(
                {
                    "source": graph,
                    "key": json.loads(result["key"]),
                    "tables": rec["tables"],
                }
            )
        )
        print(board, result["key"], flush=True)


if __name__ == "__main__":
    main()
