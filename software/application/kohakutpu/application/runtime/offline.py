"""A card with nothing attached. Every run goes through the whole chain a
card's run does -- the static checker's gate, the dispatch scheduler, the
L0 package bytes as the node would read them -- and the card's memory is
host memory. What a run then does with the built packages is `execute`'s:
nothing (compile only: the arrays keep what the host wrote), or a model.

    card = OfflineCard("multimesh_v9t8")          # compile only
    a = card.alloc(n); card.write(a, raw)
    card.run(program)                              # checked, scheduled, encoded
"""

from kohakuaccel.compiler.machine import STAGE_BYTES
from kohakutpu.application.runtime.card import ARENA, Q_SIZE, NodeCard
from kohakutpu.application.runtime.memory import CardMemory
from kohakutpu.compiler import build as B
from kohakutpu.compiler import imem
from kohakutpu.compiler import target as T
from kohakutpu.driver.clock.card import load_board
from kohakutpu.driver.host import board_map

#: The DRAM an offline card holds (the Verilated card's 256 MB).
DRAM = 0x1000_0000
PAGE = 1 << 20


class HostMemory:
    """Unit-global memory as 1 MB pages, made on first touch (zeros)."""

    def __init__(self) -> None:
        self.pages: dict[int, bytearray] = {}

    def _page(self, n: int) -> bytearray:
        page = self.pages.get(n)
        if page is None:
            page = self.pages[n] = bytearray(PAGE)
        return page

    def write_block(self, addr: int, data: bytes) -> None:
        data = bytes(data)
        at = 0
        while at < len(data):
            n, off = divmod(addr + at, PAGE)
            k = min(PAGE - off, len(data) - at)
            self._page(n)[off : off + k] = data[at : at + k]
            at += k

    def read_block(self, addr: int, nbytes: int) -> bytes:
        out = bytearray()
        while len(out) < nbytes:
            n, off = divmod(addr + len(out), PAGE)
            k = min(PAGE - off, nbytes - len(out))
            page = self.pages.get(n)
            out += page[off : off + k] if page is not None else bytes(k)
        return bytes(out)

    def read64(self, addr: int) -> int:
        return int.from_bytes(self.read_block(addr, 8), "little")

    def write64(self, addr: int, value: int) -> None:
        self.write_block(addr, (value & (1 << 64) - 1).to_bytes(8, "little"))


class OfflineCard:
    """`board`'s machine over `nodes`, nothing attached. `execute(card,
    built)` (``built``: ``{node: (Package, bindings)}``) runs the packages
    and returns the run's result; None compiles only."""

    def __init__(
        self,
        board: str,
        nodes: tuple[int, ...] = (0,),
        check: str = "refuse",
        execute=None,
    ) -> None:
        self.board = load_board(board)
        mp = board_map(self.board)
        span = max(nodes) + 1
        self.machine = T.machine_of(self.board, nodes=span if span > 1 else None)
        b = self.board
        self.fetch = tuple(b["fetch_port"]) if "fetch_port" in b else None
        self.nodes = tuple(nodes)
        self.check = check
        self.checked: dict = {}
        self.execute = execute
        self.mem = HostMemory()
        self.line = mp["granule"] or 32
        self.paint = False
        self.memory = CardMemory(
            (ARENA, DRAM - Q_SIZE),
            {
                n: (
                    self.machine.stage_addr(0, mesh=n) + Q_SIZE,
                    self.machine.stage_addr(0, mesh=n) + STAGE_BYTES,
                )
                for n in self.nodes
            },
        )
        self.log: list | None = None
        self.runs = 0
        self.residents = {n: imem.Cores(imem.IMEM_WORDS) for n in self.nodes}
        self.resident = self.residents[self.nodes[0]]

    # NodeCard's memory, logging and checker, as they are.
    alloc = NodeCard.alloc
    free = NodeCard.free
    mark = NodeCard.mark
    release = NodeCard.release
    stage = NodeCard.stage
    place = NodeCard.place
    put = NodeCard.put
    write = NodeCard.write
    get = NodeCard.get
    record = NodeCard.record
    recorded = NodeCard.recorded
    verify = NodeCard.verify

    def drop_spilled(self) -> None:
        """No queue holds a package here."""

    def run(self, program, faults: bool = False) -> dict:
        single = not isinstance(program, dict)
        progs = {self.nodes[0]: program} if single else program
        progs = {self.nodes[0] if n is None else n: p for n, p in progs.items()}
        self.verify(progs)
        built = B.packages(progs, self.machine, self.fetch, self.residents)
        raw = {n: (pkg.to_bytes(), tuple(b)) for n, (pkg, b) in built.items()}
        if self.log is not None:
            self.log.append(("run", raw))
        self.runs += 1
        if self.execute is not None:
            return self.execute(self, built)
        if single:
            return {
                "cycles": 0,
                "steps": [],
                "phases": {},
                "pkg": built[self.nodes[0]][0],
            }
        out = {
            n: {"cycles": 0, "steps": [], "phases": {}, "pkg": p}
            for n, (p, _) in built.items()
        }
        return {"cycles": 0, "nodes": out}

    def replay(self, log: list) -> list:
        """A recorded log again: its writes land, its runs are runs of the
        packages it holds (`execute` given them), each 0 cycles here."""
        if any(ev[0] == "read" for ev in log):
            raise ValueError("a log that reads the card cannot be replayed")
        cycles = []
        for ev in log:
            if ev[0] == "write":
                self.mem.write_block(ev[1], ev[2])
            elif ev[0] == "run":
                cycles.append(0)
        self.runs += sum(1 for ev in log if ev[0] == "run")
        return cycles

    def close(self) -> dict:
        return {}


__all__ = ["DRAM", "HostMemory", "OfflineCard"]
