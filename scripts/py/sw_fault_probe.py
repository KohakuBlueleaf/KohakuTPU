"""Unit faults through the node dispatcher: each must FAIL its package by name.

    python scripts/py/sw_fault_probe.py [--build build/sw/vlt_card_v8t8_2n]

Boots the dispatcher on node 0 (queue in staging, units self-enumerated) and
runs, each as its own package, expecting status UNIT_FAULT and a usable node
after it (a NOP round trip):
  1. a vector kernel whose VSETVL asks for 0 lanes (vec_core F_VL, code 7);
  2. a cluster DRAIN into a reserved aperture, then a GEMM (the write is
     refused, so the unit's next completion is SIG_FAULT);
  3. the same into ANOTHER mesh's staging.
A legal DRAIN into the node's own staging runs alongside as the control.
"""

import argparse
import pathlib
import sys
import time

from kohakuaccel.node.boot import BootArgs, NodeBoot
from kohakuaccel.node.queue import NodeQueue
from kohakuaccel.package.build import PackageBuilder
from kohakuaccel.transport.rebase import UnitGlobal
from kohakuaccel.transport.verilator import VerilatorTransport
from kohakutpu.clock.card import load_board
from kohakutpu.host import board_map
from kohakutpu.hw import vector as V
from kohakutpu.isa import ISA

ROOT = pathlib.Path(__file__).resolve().parents[2]
OPERAND = 0x0040_0000  # one zeroed entry for the FILL, inside the model's DRAM
MG, VC = (1, 1), (1, 0)


def cluster(drain_addr: int) -> bytes:
    b = PackageBuilder(types={MG: "MG"})
    u = b.unit(MG)
    b.dispatch(
        u,
        [
            ISA.fill(addr=OPERAND, n=1),
            ISA.gemm(gm=1, gn=1, nk=1),
            ISA.drain(addr=drain_addr, n=1),
            ISA.gemm(gm=1, gn=1, nk=1),
            ISA.drain(addr=OPERAND + 0x1000, n=1),
        ],
    )
    return b.build().to_bytes()


def vector_fault() -> bytes:
    k = V.Kernel().seti(1, 0).emit(V.vsetvl(1), V.vhalt())
    words = [V.imem_flit(a, w) for a, w in sorted(k.words.items())] + [V.run_flit(0)]
    b = PackageBuilder(types={VC: "VC"})
    b.dispatch(b.unit(VC), words)
    return b.build().to_bytes()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default=str(ROOT / "build/sw/vlt_card_v8t8_2n"))
    ap.add_argument("--fw", default=str(ROOT / "build/fw/kohakutpu_node.elf"))
    a = ap.parse_args()
    m = board_map(load_board("multimesh_v8t8"))
    t = VerilatorTransport(build_dir=a.build)
    mem = UnitGlobal(t, m["dram"], m["mem"], m["size"], staging=True)
    q = NodeQueue(mem, 1 << 39, idle=lambda: t.run(600))
    q.init()
    NodeBoot(t, m["ctrl"][0], 0, sim=t).load(
        a.fw, BootArgs(queue=q.base, scan=2), "burn"
    )
    q.wait_ready()
    mem.write_block(OPERAND, bytes(256))
    cases = [
        ("vector VSETVL 0 (F_VL)", vector_fault(), "UNIT_FAULT"),
        ("DRAIN to own staging (control)", cluster(1 << 39 | 0x1000), "OK"),
        ("DRAIN to reserved aperture 0xF", cluster(1 << 39 | 0xF << 32), "UNIT_FAULT"),
        ("DRAIN to mesh 1's staging", cluster(1 << 39 | 1 << 36), "UNIT_FAULT"),
    ]
    fails = 0
    for name, pkg, want in cases:
        t0 = time.monotonic()
        c = q.wait(q.submit(pkg), check=False)
        alive = q.nop().ok
        ok = c.name == want and alive
        fails += not ok
        print(
            f"  {'ok  ' if ok else 'FAIL'} {name:32s} {c.describe()} "
            f"(want {want}; node usable after: {alive}) {time.monotonic() - t0:.1f}s",
            flush=True,
        )
    for line in q.read_stdout().strip().splitlines()[1:]:
        print(f"    node: {line}")
    t.close()
    print("PASS" if not fails else f"FAIL ({fails})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
