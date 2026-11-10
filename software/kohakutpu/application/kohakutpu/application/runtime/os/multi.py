"""Node-local KAOS residents behind the ordinary card execution contract."""

import hashlib
import json
import struct
import time
from typing import Any

from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.application.runtime.os import abi as A
from kohakutpu.application.runtime.os.card import attach_memory
from kohakutpu.application.runtime.os.graph import PreparedMultiGraph
from kohakutpu.application.runtime.split import BoundGraphArtifact, NodePart, SlotLeases
from kohakutpu.language.config import ENV


class MultiOSCard(OfflineCard):
    """The boot owner supplies residents, unit order and the shared host transport."""

    # Set by `attach_memory`: unit index by (mesh, x, y) and CU type by the same key.
    units: dict
    unit_types: dict

    graph_backend = "kaos"

    def __init__(self, board, sessions, units, memory, arena, *, resident_mode=None):
        super().__init__(
            board,
            nodes=tuple(sorted(sessions)),
            resident_mode=resident_mode,
            execute=self._execute,
        )
        self.sessions = sessions
        staging = {n: (a.lo, a.hi) for n, a in self.memory.staging.items()}
        attach_memory(self, units, memory, arena, staging)
        self.leases = SlotLeases()
        self.active = None
        self.active_source = None
        self.timeout_cycles = int(
            ENV.get("KOHAKUTPU_GRAPH_TIMEOUT_CYCLES", str(1 << 40))
        )
        self.profile_sys = ENV.get("KOHAKUTPU_OS_PROFILE_SYS", "0") == "1"
        self.wait_window = json.loads(self.machine_json)["machine"]["flaws"]["F-004"][
            "present"
        ]
        self.machine_digest = hashlib.blake2b(
            self.machine_json.encode(), digest_size=32
        ).hexdigest()

    def release_graph(self):
        if self.active is not None:
            self.active.close()
            self.active = self.active_source = None

    def prepare_split(self, prepared):
        if (
            self.active is None
            or self.active_source is not prepared
            or self.active.closed
        ):
            self.release_graph()
            self.active = prepared.prepare_kaos(
                self.sessions,
                self.units,
                self.leases,
                unit_types=self.unit_types,
                wait_window=self.wait_window,
            )
            self.active_source = prepared
        return self.active

    def result(self, results, seconds) -> dict[str, Any]:
        if self.profile_sys:
            for node, result in results.items():
                session = self.sessions[node]
                if "part_results" in result:
                    result["syscall_profile_scope"] = "terminal_part_only"
                result["syscall_profile"] = struct.unpack(
                    f"<{A.OSTPU_PROF_WORDS}Q",
                    session.io.read(
                        session.mailbox + A.OSTPU_OFF_PROF, 8 * A.OSTPU_PROF_WORDS
                    ),
                )
        cycles = max(r["cycles"] for r in results.values())
        phases = dict(
            backend="kaos",
            cycles=cycles,
            max_node_cycles=cycles,
            makespan_seconds=seconds,
            nodes=results,
        )
        return dict(cycles=cycles, phases=phases, nodes=results, steps=[])

    def _execute(self, _card, built):
        self.release_graph()
        artifact = BoundGraphArtifact(
            self.machine_digest,
            tuple(NodePart(n, p.to_bytes()) for n, (p, _) in built.items()),
        )
        self.active = PreparedMultiGraph(
            artifact,
            self.sessions,
            self.units,
            self.leases,
            unit_types=self.unit_types,
            wait_window=self.wait_window,
        )
        started = time.monotonic()
        results = self.active.submit(timeout_cycles=self.timeout_cycles).wait()
        return self.result(results, time.monotonic() - started)

    def close(self):
        self.release_graph()
        for session in self.sessions.values():
            session.close()
        return {}
