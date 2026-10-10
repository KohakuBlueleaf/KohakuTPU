"""Node dispatch costs from hand-written L1 (rebuild plan, goal item 2).

    python scripts/py/l1/dispatch_prims.py --build build/v9prof/vlt_card_v9_1n

Each case is one package of DISPATCH steps (one per unit, `n` GEMM words each:
a sweep retires on issue and needs no operands) and a barrier. Reported: the
package's phases (header, bind, steps, barrier) and each step's end from the
firmware's step trace, so a fixed per-request cost and a per-word cost read
apart.
"""

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from card import L1Card
from kohakutpu.ir.l1 import Program
from kohakutpu.ir.l1.cluster import Gemm


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument(
        "--queue",
        type=lambda v: int(v, 0),
        default=None,
        help="package queue address (default: staging)",
    )
    ap.add_argument("--words", default="1,16,64,200")
    ap.add_argument("--units", default="1,2,4")
    ap.add_argument(
        "--steps",
        action="store_true",
        help="trace each step (its prints shift every later time)",
    )
    a = ap.parse_args()
    card = L1Card(
        a.build, steps=a.steps, **({"queue": a.queue} if a.queue is not None else {})
    )
    mgs = Program(card.machine).units("MG")
    for units in (int(v) for v in a.units.split(",")):
        for n in (int(v) for v in a.words.split(",")):
            p = Program(card.machine)
            for u in mgs[:units]:
                p.send(u, *[Gemm(1, 1, 2) for _ in range(n)])
            p.barrier()
            card.run(p)
            got = card.run(p)
            ends = [
                at for _, _, at, what in got["steps"] if what.startswith("DISPATCH")
            ]
            ph = got["phases"]
            print(
                f"units {units} words {n:3d}: total {got['cycles']:6d}  head {ph['head']} "
                f"bind {ph['bind']} steps {ph['steps']} barrier {ph['barrier']}  "
                f"dispatch ends {ends}",
                flush=True,
            )
    card.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
