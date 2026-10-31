"""Device adapter for an already booted OS graph session.

The caller owns the OS boot/transport and passes its ResidentGraph and unit map.
DISPATCH/AWAIT/BARRIER and optional synchronous MOVER/MWAIT lowering are supported;
other operations refuse before publication. Prepared logs are immutable until
closed. Constant writes become resident copies; variable inputs remain external.
Firmware remains the default Device backend.
"""

import json
import os

from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime import replay_budget
from kohakutpu.application.runtime.arena import OutOfMemory
from kohakutpu.application.runtime.memory import CardMemory
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.application.runtime.osgraph import (
    KDS_MOVE,
    KOS_MOVE_MAX_WORDS,
    TASK_BYTES,
    Graph,
    Task,
)


def movers_enabled(machine_json):
    """KOHAKUTPU_OS_MOVERS resolved per machine: mover steps travel in a DE
    stream (SYS_DE_RUN), so a machine without the dispatch engine lowers them
    as MOVERS=0 does."""
    engine = json.loads(machine_json)["machine"]["node"].get("engine", {})
    return (
        os.environ.get("KOHAKUTPU_OS_MOVERS", "0") == "1" and engine.get("depth", 0) > 0
    )


class PhysicalMemory:
    def __init__(self, io):
        self.io = io

    def write_block(self, address, data):
        self.io.write(address, data)

    def read_block(self, address, size):
        return self.io.read(address, size)


class Prepared:
    def __init__(self, graph, launches):
        self.graph, self.launches = graph, launches
        self.closed = False

    def matches(self, _log):
        return not self.closed

    def close(self):
        self.closed = True


class OSCard(OfflineCard):
    def __init__(self, board, session, units):
        super().__init__(board)
        self.session, self.units = session, units
        self.unit_types = {
            (mesh.index, *coord): int.from_bytes(kind.encode(), "big")
            for mesh in self.machine.meshes
            for kind, coords in mesh.units.items()
            for coord in coords
        }
        self.mem = PhysicalMemory(session.io)
        self.memory = CardMemory(
            (session.data_pa, session.data_pa + session.data_bytes), {}
        )
        self.dram_bytes = session.data_bytes
        self.resident_program = None
        self.last_result = None
        self.submissions = 0
        self.segment = int(os.environ.get("KOHAKUTPU_OS_SEGMENT", "1"))
        self.compact = os.environ.get("KOHAKUTPU_OS_COMPACT", "0") == "1"
        self.movers = movers_enabled(self.machine_json)
        self.prune = os.environ.get("KOHAKUTPU_OS_PRUNE", "0") == "1"
        self.stamps = os.environ.get("KOHAKUTPU_OS_STAMPS", "0") == "1"
        self.de_stream = os.environ.get("KOHAKUTPU_OS_DE_STREAM", "0") == "1"
        self.de_fetch = os.environ.get("KOHAKUTPU_OS_DE_FETCH", "1") == "1"
        self.de_fuse = os.environ.get("KOHAKUTPU_OS_DE_FUSE", "0") == "1"
        self.launch_ring = os.environ.get("KOHAKUTPU_OS_LAUNCH_RING", "0") == "1"

    def _execute(self, graph, repeats=1):
        self.session.prepare(graph)
        self.resident_program = graph
        return self._run(repeats)

    def _run(self, repeats):
        self._publishing()
        self.last_result = self.session.run(repeats)
        self.submissions += 1
        return self.last_result

    def run(self, program, faults=False):
        progs = program if isinstance(program, dict) else {0: program}
        progs = {0 if n is None else n: p for n, p in progs.items()}
        self.verify(progs)
        multi = tuple(progs) != (0,)
        graph = Graph(
            1 if multi else self.segment,
            self.compact,
            self.movers,
            self.prune,
            False if multi else self.de_stream,
            self.de_fetch,
            False,
        )
        log_entry = {}
        for n, p in sorted(progs.items()):
            raw = p.bound(False)
            B.admit(raw, self.machine_json, n)
            graph.package(raw, [], self.units, unit_types=self.unit_types)
            log_entry[n] = (raw, ())
        if self.log is not None:
            self.log.append(("run", log_entry))
        result = self._execute(graph)
        self.runs += 1
        return dict(cycles=result["cycles"], phases=result, steps=[])

    def prepare_recorded(
        self,
        log,
        budget,
        depth=1,
        fraction=replay_budget.DEFAULT_FRACTION,
        overcommit=False,
    ):
        """The card contract of runtime/card.py. A resident graph has no
        submission queue and streams nothing, so `depth` and `fraction` are
        validated only: the whole recording is pinned, and its limit is
        `budget` bytes when given (the whole task region under `overcommit`),
        else the task region."""
        if not 0.0 <= fraction <= 1.0:
            raise ValueError("replay package fraction must be in [0, 1]")
        if depth is not None and depth < 1:
            raise ValueError("queue_depth must be positive or None")
        multi = any(ev[0] == "run" and tuple(ev[1]) != (0,) for ev in log)
        use_de = self.de_stream and not multi
        use_fuse = use_de and self.de_fuse and not self.stamps and not self.launch_ring
        graph, frontier, launches = (
            Graph(
                1 if multi else self.segment,
                self.compact,
                self.movers,
                self.prune,
                use_de,
                self.de_fetch,
                use_fuse,
            ),
            (),
            0,
        )
        if self.launch_ring:
            frontier = graph.launch_mark(frontier, (1 << 64) - 1)
        for ev in log:
            if ev[0] == "write":
                address, data = ev[1:]
                offset = address - self.session.data_pa
                if (
                    offset < 0
                    or offset + len(data) > self.session.data_bytes
                    or (address | len(data)) & 31
                ):
                    raise ValueError("captured write must name whole OS arena words")
                if use_fuse:
                    frontier = graph.move_de(data, address, after=frontier)
                    continue
                for at in range(0, len(data), KOS_MOVE_MAX_WORDS * 32):
                    chunk = data[at : at + KOS_MOVE_MAX_WORDS * 32]
                    t = graph.add(
                        Task(
                            KDS_MOVE,
                            words=(
                                0,
                                self.session.data_ref + offset + at,
                                len(chunk) // 32,
                                0,
                            ),
                            pred=frontier,
                        )
                    )
                    graph.streams[t] = chunk
                    frontier = (t,)
                continue
            if ev[0] != "run":
                raise ValueError(
                    "OS resident logs contain runs or constant writes, not reads"
                )
            if self.stamps:
                frontier = graph.stamp(frontier, 2 * launches)
            branches = []
            for n, (raw, bindings) in sorted(ev[1].items()):
                if bindings:
                    raise ValueError("OSCard capture requires host-bound package bytes")
                B.admit(raw, self.machine_json, n)
                branches.extend(
                    graph.package(
                        raw,
                        [],
                        self.units,
                        after=frontier,
                        unit_types=self.unit_types,
                    )
                )
            frontier = (
                tuple(branches) if len(branches) <= 1 else graph.join(tuple(branches))
            )
            if self.stamps:
                frontier = graph.stamp(frontier, 2 * launches + 1)
            if self.launch_ring:
                frontier = graph.launch_mark(frontier, launches)
            launches += 1
        region = self.session.capacity * TASK_BYTES
        if budget is None or overcommit:
            budget = region
        if (
            len(graph.tasks) * TASK_BYTES + sum(map(len, graph.streams.values()))
            > budget
        ):
            raise OutOfMemory("OS recording exceeds its resident byte budget")
        self.session.prepare(graph)
        self.resident_program = graph
        return Prepared(graph, launches)

    def prepare_execution(self, _prepared):
        raise ValueError(
            "OSCard resident graphs already submit once per step; execution lists are a firmware queue mode"
        )

    def replay(self, log, repeats=1):
        prepared = log.prepared
        if prepared is None or prepared.closed:
            raise ValueError("prepare the pinned OS recording before replay")
        if self.resident_program is not prepared.graph:
            # Switching resident programs uploads tasks; repeated calls to one do not.
            self.session.prepare(prepared.graph)
            self.resident_program = prepared.graph
        result = self._run(repeats)
        self.runs += prepared.launches * repeats
        return [result["cycles"]]

    def close(self):
        self.session.close()
        return {}
