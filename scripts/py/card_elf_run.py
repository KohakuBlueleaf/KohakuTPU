"""Run one RV64 program on one node of the Verilator card; print its console.

    python scripts/py/card_elf_run.py build/rv64/hw_bundle.elf [--node 0] [--load axi]

Exit status is the program's exit word (0 = pass), or 2 if it did not finish.
"""

import argparse
import sys

from kohakuaccel.device import rv64load
from kohakuaccel.transport.verilator import VerilatorTransport
from kohakutpu.clock.card import load_board
from kohakutpu.host import board_map


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("elf")
    ap.add_argument("--node", type=int, default=0)
    ap.add_argument("--board", default="multimesh_v8t8")
    ap.add_argument("--build", default=None)
    ap.add_argument("--load", choices=("axi", "burn"), default="burn")
    ap.add_argument("--timeout", type=float, default=240.0)
    a = ap.parse_args()
    ctrl = board_map(load_board(a.board))["ctrl"][a.node]
    t = VerilatorTransport(build_dir=a.build, settle=2000)
    win = rv64load.LoadWindow(t, ctrl + rv64load.WINDOW_OFFSET)
    if a.load == "burn":
        cpu = [
            s
            for s, v, _, _ in t.arrays(f"u_mesh{a.node}.u_mag.u_pe.u_cpu")
            if v == "spad"
        ]
        win.burn_elf(a.elf, t, cpu[0])
    else:
        win.load_elf(a.elf)
    r = win.run(timeout=a.timeout, poll=lambda: t.run(2000))
    print(r["console"], end="")
    print(
        f"exit {r['exit']:#x} status {r['status']:#x} halted={r['halted']} ({r['seconds']:.1f}s)"
    )
    t.close()
    if not r["status"] & 0x8:
        return 2
    return 0 if r["exit"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
