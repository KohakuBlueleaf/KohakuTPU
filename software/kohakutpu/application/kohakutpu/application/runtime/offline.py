"""A card with nothing attached. Every run goes through the whole chain a
card's run does -- the static checker's gate, the dispatch scheduler, the
L0 package bytes as the node would read them -- and the card's memory is
host memory. What a run then does with the built packages is `execute`'s:
nothing (compile only: the arrays keep what the host wrote), or a model.

    card = OfflineCard("multimesh_v9t8")          # compile only
    a = card.alloc(n); card.write(a, raw)
    card.run(program)                              # checked, scheduled, encoded
"""

import os

from kohakuaccel.driver.node.queue import layout as QL
from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.card import (
    _UNSET,
    ARENA,
    Q_SIZE,
    RESIDENT_ENV,
    NodeCard,
    ResidentMode,
    parse_resident_mode,
)
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.application.runtime.memory import CardMemory
from kohakutpu.compiler import _rs
from kohakutpu.driver.clock.card import load_board
from kohakutpu.driver.host import board_map

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

    _eager_batch = None
    native_direct = None
    native_direct_suspended = False
    _publication_epoch = 0
    resident_mode = NodeCard.resident_mode
    submit_cache = NodeCard.submit_cache
    admission_cached = NodeCard.admission_cached
    host = None

    def __init__(
        self,
        board: str,
        nodes: tuple[int, ...] = (0,),
        check: str = "refuse",
        execute=None,
        resident_mode: str | ResidentMode | None | object = _UNSET,
    ) -> None:
        self.board = load_board(board)
        mp = board_map(self.board)
        span = max(nodes) + 1
        self.machine = machine_of(self.board, nodes=span)
        self.machine_json = self.machine.machine_json
        self.queue_layout = QL.Layout.from_machine(self.machine_json)
        b = self.board
        self.fetch = tuple(b["fetch_port"]) if "fetch_port" in b else None
        #: The sysnode clock a run's cycles count, at the board's idle profile.
        self.sys_hz = b["profiles"][b["idle_profile"]]["sys"] * 1e6
        self.nodes = tuple(nodes)
        self.check = check
        self.checked: dict = {}
        self.execute = execute
        self.dram_bytes = mp["size"]
        self.mem = HostMemory()
        self.line = mp["granule"] or 32
        self.paint = False
        self.memory = CardMemory(
            (ARENA, mp["arena_limit"]),
            {
                n: (
                    self.machine.stage_addr(0, mesh=n) + Q_SIZE,
                    self.machine.stage_addr(0, mesh=n) + self.machine.stage_bytes,
                )
                for n in self.nodes
            },
        )
        self.log: list | None = None
        self.runs = 0
        self.queues = {}
        self._eager_batch = None
        self.native_direct = None
        self.native_direct_suspended = False
        self._publication_epoch = 0
        self._resident_mode = parse_resident_mode(
            os.environ.get(RESIDENT_ENV) if resident_mode is _UNSET else resident_mode
        )
        self._admission_cached = B.admission_cached()
        self.residents = {n: _rs.Cores(self.machine_json) for n in self.nodes}
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
    _forget_residents = NodeCard._forget_residents
    record = NodeCard.record
    recorded = NodeCard.recorded
    verify = NodeCard.verify
    publication_epoch = NodeCard.publication_epoch
    _publishing = NodeCard._publishing

    def drop_spilled(self) -> None:
        """No queue holds a package here."""

    def run(self, program, faults: bool = False) -> dict:
        single = not isinstance(program, dict)
        progs = B.placed(program, self.nodes)
        self.verify(progs)
        both = B.built(
            progs,
            self.machine,
            self.fetch,
            self.residents,
            resident=self.resident_mode.resident,
            cached=self.admission_cached,
        )
        built = {n: (pkg, b) for n, (_, pkg, b) in both.items()}
        raw = {n: (pkg.to_bytes(), tuple(b)) for n, (pkg, b) in built.items()}
        if self.log is not None:
            self.log.append(("run", raw))
        self.runs += 1
        if self.execute is not None:
            self._publishing()
            try:
                return self.execute(self, built)
            except Exception:
                for n in progs:
                    self.residents[n].forget()
                raise
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

    def submit_wire(self, raw, debug):
        """Native preparation already checked admission; execute the resulting bytes."""
        if self._eager_batch is not None and not self._eager_batch._submitting:
            return self._eager_batch.wire(raw, debug)
        if self.log is not None:
            self.log.append(("run", raw))
        self.runs += 1
        built = {
            n: (B.Package(data, self.machine_json, n), bindings)
            for n, (data, bindings) in raw.items()
        }
        if self.execute is not None:
            self._publishing()
            return self.execute(self, built)
        return {"cycles": 0, "steps": [], "phases": {}}

    def replay(self, log: list) -> list:
        """A recorded log again: its writes land, its runs are runs of the
        packages it holds (`execute` given them), each 0 cycles here."""
        if any(ev[0] == "read" for ev in log):
            raise ValueError("a log that reads the card cannot be replayed")
        self._forget_residents(self.nodes)
        cycles = []
        for ev in log:
            if ev[0] == "write":
                self.mem.write_block(ev[1], ev[2])
            elif ev[0] == "run":
                for n, (raw, bindings) in ev[1].items():
                    B.admit(
                        raw,
                        self.machine_json,
                        n,
                        tuple(bindings),
                        cached=self.admission_cached,
                    )
                cycles.append(0)
        self.runs += sum(1 for ev in log if ev[0] == "run")
        return cycles

    def close(self) -> dict:
        return {}


__all__ = ["HostMemory", "OfflineCard"]
