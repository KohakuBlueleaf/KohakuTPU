"""Run node programs on the Verilated card: boot once, place arrays, run, time,
read back.

    card = VerilatorCard(build)         # boots node 0 with the queue firmware
    a = card.put(np.float16 array)      # unit-global byte address
    got = card.run(program)             # node cycles, per-step ends, phases
    card.get(a, nbytes)
    card.close()                        # each vector core's counters

`cycles` is the package's own count (node clock); with `steps=True` the
firmware prints each step's end (KA_BOOT_F_STEPS), prints excluded. The
machine programs are built for must be the card's: the enumerated units are
checked against it at boot.
"""

import json
import pathlib
import re

import numpy as np
from kohakuaccel.compiler.machine import STAGE_BYTES
from kohakuaccel.driver.node.boot import BootArgs, NodeBoot
from kohakuaccel.driver.node.queue import NodeQueue
from kohakuaccel.driver.transport.rebase import UnitGlobal
from kohakuaccel.driver.transport.simdram import SimDram
from kohakuaccel.driver.transport.verilator import VerilatorTransport
from kohakutpu.compiler import build as B
from kohakutpu.compiler import imem
from kohakutpu.compiler import target as T
from kohakutpu.driver import units as _units  # noqa: F401  (names MG and VC)
from kohakutpu.driver.clock.card import BOARDS, load_board
from kohakutpu.driver.host import Card, board_map

ROOT = pathlib.Path(__file__).resolve().parents[5]
#: Host arrays below the model's 16 MB (it wraps).
ARENA, ARENA_END = 0x0010_0000, 0x00F0_0000
#: The node queue, at the start of mesh 0's staging store.
Q_SIZE = 0x0010_0000
F_TIMING, F_STEPS = 2, 8


class CardMismatch(RuntimeError):
    """The card's enumerated units are not the machine programs are built for."""


class VerilatorCard:
    def __init__(
        self,
        build,
        fw=None,
        board="multimesh_v9",
        steps=False,
        machine=None,
        log=None,
    ) -> None:
        self.build = pathlib.Path(build)
        self.machine = machine or T.machine()
        fw = pathlib.Path(fw or ROOT / "build/fw/kohakutpu_node.elf")
        b = load_board(board)
        mp = board_map(b)
        #: The memory port a fetched dispatch reads through, from the board.
        self.fetch = tuple(b["fetch_port"]) if "fetch_port" in b else None
        self.t = VerilatorTransport(self.build, log=log)
        self.host = SimDram(self.t, b, mp["mem"], mp["size"])
        card = Card.from_board(
            board, transport=self.host, which=[0], verify=False, reply_at_agent=True
        )
        self.check_units(card.mesh)
        self.mem = UnitGlobal(
            self.host, mp["dram"], mp["mem"], mp["size"], staging=True
        )
        staging = self.machine.stage_addr(0)
        self.q = NodeQueue(
            self.mem,
            staging,
            size=Q_SIZE,
            idle=lambda: self.t.run(600),
            spill=lambda n: self.alloc(n),
        )
        self.q.init()
        flags = F_TIMING | (F_STEPS if steps else 0)
        args = BootArgs(
            queue=self.q.base, mesh=0, scan=card.mesh.caps.grid_hi + 1, flags=flags
        )
        NodeBoot(self.t, mp["ctrl"][0], 0, sim=self.t).load(fw, args, "burn")
        self.q.wait_ready()
        self.top = ARENA
        self.stage_top, self.stage_end = staging + Q_SIZE, staging + STAGE_BYTES
        self.runs = 0
        #: Each vector core's instruction memory and descriptors, across packages.
        self.resident = imem.Cores(imem.IMEM_WORDS)

    def check_units(self, mesh) -> None:
        """Raise :class:`CardMismatch` unless mesh 0's units are the machine's."""
        for kind, want in self.machine.units.items():
            got = mesh.coords(kind)
            if tuple(got) != tuple(want):
                raise CardMismatch(
                    f"the card's {kind} units are {list(got)}, the machine's "
                    f"{list(want)}: a program built for one runs wrong on the other"
                )

    # ---------------------------------------------------------- memory
    def alloc(self, nbytes: int, align: int = 256) -> int:
        at = -(-self.top // align) * align
        if at + nbytes > ARENA_END:
            raise MemoryError(f"{nbytes} B past the arena ({ARENA_END - at} left)")
        self.top = at + nbytes
        return at

    def stage(self, nbytes: int, align: int = 256) -> int:
        """`nbytes` of the MAG staging store, past the queue."""
        at = -(-self.stage_top // align) * align
        if at + nbytes > self.stage_end:
            raise MemoryError(f"{nbytes} B past staging ({self.stage_end - at} left)")
        self.stage_top = at + nbytes
        return at

    def place(self, nbytes: int, space: str = "dram") -> int:
        """`nbytes` in DRAM or, for ``space == "stage"``, in staging."""
        return self.stage(nbytes) if space == "stage" else self.alloc(nbytes)

    def put(self, array, space: str = "dram") -> int:
        raw = np.ascontiguousarray(array).tobytes()
        at = self.place(len(raw), space)
        self.mem.write_block(at, raw)
        return at

    def write(self, at: int, raw: bytes) -> None:
        self.mem.write_block(at, raw)

    def get(self, at: int, nbytes: int) -> bytes:
        return self.mem.read_block(at, nbytes)

    # ------------------------------------------------------------- run
    def run(self, program) -> dict:
        """Run `program` once: ``{"cycles", "steps", "phases", "pkg"}``."""
        pkg, bindings = B.package(program, self.fetch, self.resident)
        done = self.q.run(pkg.to_bytes(), bindings)
        steps, phases = [], {}
        for ln in self.q.read_stdout().splitlines():
            if ln.startswith("PKGT "):
                words = ln.split()[1:]
                phases = {
                    words[i]: int(words[i + 1]) for i in range(0, len(words) - 1, 2)
                }
            if ln.startswith("PKGS "):
                first, last, at = (int(v) for v in ln.split()[1:4])
                steps = [] if first == 0 else steps
                what = [
                    f"{s.op.name} u{s.unit} n{s.count}"
                    for s in pkg.steps[first : last + 1]
                ]
                steps.append((first, last, at, ", ".join(what)))
        self.runs += 1
        return {"cycles": done.cycles, "steps": steps, "phases": phases, "pkg": pkg}

    def close(self) -> dict:
        """Stop the model; each vector core's counters from its log."""
        self.q.stop()
        self.t.close()
        return counters(self.t.model_log)


def counters(log: pathlib.Path) -> dict:
    """Per vector core: the VRES totals and the V2MATH issue breakdown that
    `v2_core.v` prints at the end of simulation."""
    out: dict = {}
    for ln in log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"(VRES|V2MATH) (\S+) (.*)", ln)
        if not m:
            continue
        core = m[2].split(".u_mesh0.")[-1].split(".")[0]
        rest = m[3].split()
        fields = {rest[i]: int(rest[i + 1]) for i in range(0, len(rest) - 1, 2)}
        out.setdefault(core, {})["vres" if m[1] == "VRES" else "math"] = fields
    return out


def report(stats: dict) -> list:
    """One line per busy vector core."""
    lines = []
    for core, c in sorted(stats.items()):
        res, math = c.get("vres", {}), c.get("math", {})
        if not res.get("busy"):
            continue
        lines.append(
            f"{core}: "
            + " ".join(f"{k} {v}" for k, v in res.items())
            + " | math "
            + " ".join(f"{k} {v}" for k, v in math.items())
        )
    return lines


def boards() -> list:
    """Every board file's name."""
    return sorted(json.loads(p.read_text())["name"] for p in BOARDS.glob("*.json"))


__all__ = ["CardMismatch", "VerilatorCard", "counters", "report"]
