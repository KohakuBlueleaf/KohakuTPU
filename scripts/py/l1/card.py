"""Run L1 programs on a card model: boot once, place arrays, run, time, read back.

    card = L1Card(build)            # boots node 0 with the queue firmware
    a = card.put(np.float16 array)  # unit-global byte address
    got = card.run(program)         # node cycles, per-step ends, VC RUN stamps
    card.get(a, nbytes)
    card.close()                    # VRES / VSTATE / VRUN from the model log

Timing: `cycles` is the package's own count (node clock); with `steps=True`
the firmware prints each step's end (KA_BOOT_F_STEPS), prints excluded.
"""

import pathlib
import re
import sys

import numpy as np
from kohakuaccel.machinespec import STAGE_BYTES
from kohakuaccel.node.boot import BootArgs, NodeBoot
from kohakuaccel.node.queue import NodeQueue
from kohakuaccel.package import engine
from kohakuaccel.transport.rebase import UnitGlobal
from kohakuaccel.transport.simdram import SimDram
from kohakuaccel.transport.verilator import VerilatorTransport
from kohakutpu.clock.card import load_board
from kohakutpu.host import Card, board_map
from kohakutpu.rt import Device

from kohakutpu import imem

ROOT = pathlib.Path(__file__).resolve().parents[3]
STAGING = 1 << 39
#: Host arrays below the model's 16 MB (it wraps); the queue sits in staging.
ARENA, ARENA_END = 0x0010_0000, 0x00F0_0000
Q_SIZE = 0x0010_0000
F_TIMING, F_STEPS = 2, 8


class L1Card:
    def __init__(
        self,
        build,
        fw=None,
        board="multimesh_v9",
        steps=False,
        queue=STAGING,
        imem_words=None,
        log=None,
    ) -> None:
        self.build = pathlib.Path(build)
        #: The xsim flow names a V2-core card's bench `..._v2`.
        if imem_words is None:
            v2 = self.build.name.endswith("_v2")
            imem_words = imem.IMEM_WORDS_V2 if v2 else imem.IMEM_WORDS
        fw = pathlib.Path(fw or ROOT / "build/fw/kohakutpu_node.elf")
        b = load_board(board)
        mp = board_map(b)
        self.t = VerilatorTransport(build_dir=self.build, log=log)
        self.host = SimDram(self.t, b, mp["mem"], mp["size"])
        card = Card.from_board(
            board, transport=self.host, which=[0], verify=False, reply_at_agent=True
        )
        self.mem = UnitGlobal(
            self.host, mp["dram"], mp["mem"], mp["size"], staging=True
        )
        self.q = NodeQueue(
            self.mem,
            queue,
            size=Q_SIZE,
            idle=lambda: self.t.run(600),
            spill=lambda n: self.alloc(n),
        )
        self.q.init()
        self.dev = Device(card, base=ARENA, size=ARENA_END - ARENA, node=self.q)
        self.machine = self.dev.machine
        self.fetch = self.dev.fetch_port if self.dev.bind_packages else None
        flags = F_TIMING | (F_STEPS if steps else 0)
        NodeBoot(self.t, mp["ctrl"][0], 0, sim=self.t).load(
            fw,
            BootArgs(
                queue=self.q.base, mesh=0, scan=card.mesh.caps.grid_hi + 1, flags=flags
            ),
            "burn",
        )
        self.q.wait_ready()
        self.top = ARENA
        #: The MAG staging store past the queue (2 MB a mesh, STAGE_BYTES).
        self.stage_top, self.stage_end = queue + Q_SIZE, STAGING + STAGE_BYTES
        self.runs = 0
        #: Each vector core's instruction memory and descriptors, across packages.
        self.resident = imem.Cores(imem_words)

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

    def put(self, array, space: str = "dram") -> int:
        raw = np.ascontiguousarray(array).tobytes()
        at = self.stage(len(raw)) if space == "stage" else self.alloc(len(raw))
        self.mem.write_block(at, raw)
        return at

    def write(self, at: int, array) -> None:
        self.mem.write_block(at, np.ascontiguousarray(array).tobytes())

    def get(self, at: int, nbytes: int) -> bytes:
        return self.mem.read_block(at, nbytes)

    # ------------------------------------------------------------- run
    def run(self, program) -> dict:
        b = program.build(self.fetch, self.resident)
        # Lowered here for the node's dispatch engine: the node copies the
        # entries into it without translating steps.
        pkg = engine.lower(b.build(defaults=False))
        done = self.q.run(pkg.to_bytes(), b.bindings())
        said = self.q.read_stdout().splitlines()
        steps = []
        phases = {}
        for ln in said:
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
        """Stop the model; the vector cores' counters from its log."""
        self.q.stop()
        self.t.close()
        return counters(self.t.model_log)


#: vec_core.v state numbers and opcodes, for a readable breakdown.
STATES = {
    0: "IDLE",
    4: "EXEC",
    9: "ALU",
    10: "RED",
    11: "RDRAIN",
    13: "RWAIT",
    17: "STR",
    18: "STW",
    19: "STD",
    24: "BAR",
    25: "WAITP",
    30: "MEM0",
    31: "AGW",
}
OPS = {v: k for k, v in __import__("kohakutpu.hw.vector", fromlist=["OPS"]).OPS.items()}


def report(stats: dict) -> None:
    """Each busy vector core's cycles by state and, in EXEC, by opcode."""
    for core, c in sorted(stats.items()):
        if not c["vres"].get("busy"):
            continue
        st = sorted(c["state"].items(), key=lambda kv: -kv[1])
        ex = sorted(c["exec"].items(), key=lambda kv: -kv[1])[:8]
        print(
            f"  {core}: busy {c['vres']['busy']} beats {c['vres']['beats']} | "
            + " ".join(f"{STATES.get(s, s)} {n}" for s, n in st if s)
            + " | exec "
            + " ".join(f"{OPS.get(o, o)} {n}" for o, n in ex)
        )
        starts = [t for k, t in c["runs"] if k == "start"]
        halts = [t for k, t in c["runs"] if k == "halt"]
        if starts and halts:
            runs = [h - s for s, h in zip(starts, halts, strict=False)]
            gaps = [s - h for h, s in zip(halts, starts[1:], strict=False)]
            print(f"    RUNs {len(runs)}: each {runs}")
            print(f"    gaps halt->start: {gaps}")


def counters(log: pathlib.Path) -> dict:
    """Per vector core: VRES fields, VSTATE/VEXEC histograms, VRUN stamps."""
    out: dict = {}
    for ln in log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"(VRES|VSTATE|VEXEC|VRUN) (\S+) (.*)", ln)
        if not m:
            continue
        core = m[2].split(".u_mesh0.")[-1].split(".")[0]
        c = out.setdefault(core, {"vres": {}, "state": {}, "exec": {}, "runs": []})
        rest = m[3].split()
        if m[1] == "VRES":
            c["vres"] = {rest[i]: int(rest[i + 1]) for i in range(0, len(rest) - 1, 2)}
        elif m[1] == "VSTATE":
            c["state"][int(rest[0])] = int(rest[1])
        elif m[1] == "VEXEC":
            c["exec"][int(rest[1])] = int(rest[2])
        else:
            c["runs"].append((rest[0], int(rest[1])))
    return out


if __name__ == "__main__":
    sys.exit("a library: see scripts/py/l1/primitives.py")
