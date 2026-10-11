"""The numerics report with an `rtl` source: the same packages on a Verilated
card, through the driver's node queue, beside the unit models.

    python scripts/py/numerics_rtl.py --build build/v9pt/vlt_card_v9_1n -o DIR
        [--fw build/fw/kohakutpu_node.elf] [--case NAME ...] [--seed N]

Operands and results move through SimDram's DRAM backdoor; node 0 is booted
once with the queue firmware.
"""

import argparse
import pathlib
import sys

from kohakuaccel.node.boot import BootArgs, NodeBoot
from kohakuaccel.node.queue import NodeQueue
from kohakuaccel.package import engine as engine_package
from kohakuaccel.transport.rebase import UnitGlobal
from kohakuaccel.transport.simdram import SimDram
from kohakuaccel.transport.verilator import VerilatorTransport
from kohakutpu.clock.card import load_board
from kohakutpu.host import Card, board_map
from kohakutpu.ir import numerics
from kohakutpu.rt import Device

ROOT = pathlib.Path(__file__).resolve().parents[2]


class CardModel:
    """A Verilated card (`build`: its `vlt_card_*` directory), node 0 booted
    with the queue firmware once. The same driver path as `scripts/py/l1/card.py`."""

    STAGING = 1 << 39
    #: Below the model's 16 MB (it wraps); the queue sits in staging.
    ARENA, ARENA_END = 0x0010_0000, 0x00F0_0000
    Q_SIZE = 0x0010_0000
    F_TIMING = 2
    #: Lower each package for the node's dispatch engine (package/engine.py).
    ENGINE = False

    def __init__(self, build, fw=None, board="multimesh_v9") -> None:
        fw = pathlib.Path(fw or ROOT / "build/fw/kohakutpu_node.elf")
        b = load_board(board)
        mp = board_map(b)
        self.t = VerilatorTransport(build_dir=pathlib.Path(build))
        host = SimDram(self.t, b, mp["mem"], mp["size"])
        card = Card.from_board(
            board, transport=host, which=[0], verify=False, reply_at_agent=True
        )
        self.mem = UnitGlobal(host, mp["dram"], mp["mem"], mp["size"], staging=True)
        self.q = NodeQueue(
            self.mem, self.STAGING, size=self.Q_SIZE, idle=lambda: self.t.run(600)
        )
        self.q.init()
        dev = Device(
            card, base=self.ARENA, size=self.ARENA_END - self.ARENA, node=self.q
        )
        self.machine = dev.machine
        self.fetch = dev.fetch_port if dev.bind_packages else None
        NodeBoot(self.t, mp["ctrl"][0], 0, sim=self.t).load(
            fw,
            BootArgs(
                queue=self.q.base,
                mesh=0,
                scan=card.mesh.caps.grid_hi + 1,
                flags=self.F_TIMING,
            ),
            "burn",
        )
        self.q.wait_ready()
        self.top = self.ARENA
        #: Each vector core's instruction memory and descriptors, across packages.
        self.resident: dict = {}
        #: Node cycles of the packages run since `fresh` (their completions').
        self.cycles = 0

    def fresh(self) -> "CardModel":
        """The arena empty again (the card and its resident images kept)."""
        self.top = self.ARENA
        self.cycles = 0
        return self

    def alloc(self, nbytes: int, align: int = 256) -> int:
        at = -(-self.top // align) * align
        if at + nbytes > self.ARENA_END:
            raise MemoryError(f"{nbytes} B past the arena")
        self.top = at + nbytes
        return at

    def write(self, at: int, raw: bytes) -> None:
        self.mem.write_block(at, raw)

    def get(self, at: int, nbytes: int) -> bytes:
        return self.mem.read_block(at, nbytes)

    def run(self, program) -> None:
        b = program.build(self.fetch, self.resident)
        pkg = b.build(defaults=False)
        if self.ENGINE:
            pkg = engine_package.lower(pkg)
        done = self.q.run(pkg.to_bytes(), b.bindings())
        self.cycles += done.cycles

    def close(self) -> None:
        self.q.stop()
        self.t.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True, help="a vlt_card_* directory")
    ap.add_argument("--fw", help="the node firmware ELF")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--case", action="append", default=[])
    a = ap.parse_args(argv)
    card = CardModel(a.build, a.fw)
    try:
        rows = numerics.report(a.seed, rtl=card, only=a.case)
    finally:
        card.close()
    sys.stdout.write(numerics.markdown(rows))
    for p in numerics.write(rows, pathlib.Path(a.out)):
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
