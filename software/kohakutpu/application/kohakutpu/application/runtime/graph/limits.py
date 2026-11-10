"""Exact retained-program resource accounting, before any transport publication.

Package limits belong to the writer and receiving machine, not to a model's
batch size or an arbitrary number of launches. Resident capacity includes the
variable instruction blobs, not just the fixed task records. This module does
not grant a memory proof or relax receiving-board admission.
"""

import json
import struct
from dataclasses import asdict, dataclass

from kohakutpu.application.runtime.os import abi as A
from kohakutpu.application.runtime.os.graph import Graph

U16 = (1 << 16) - 1
U32 = (1 << 32) - 1


class CapacityExceeded(ValueError):
    """Only resource exhaustion is eligible for a retained join split."""

    def __init__(self, node, resource, used, limit):
        self.node, self.resource = node, resource
        self.used, self.limit = used, limit
        super().__init__(f"node {node}: {resource} {used} exceeds limit {limit}")


def _bounded(node, resource, used, limit):
    if used > limit:
        raise CapacityExceeded(node, resource, used, limit)


@dataclass(frozen=True)
class PackageUsage:
    units: int
    buffers: int
    steps: int
    relocations: int
    payloads: int
    ack_reserve: int = 0
    stored_bytes: int = 0

    def __post_init__(self):
        if any(type(value) is not int or value < 0 for value in asdict(self).values()):
            raise ValueError("package counts must be nonnegative integers")

    @property
    def wire_bytes(self):
        # Read packages retain their actual declared extent. Only a counts-only
        # prediction uses the writer's canonical independent section alignment.
        if self.stored_bytes:
            return self.stored_bytes
        sizes = (
            16 * self.units,
            32 * self.buffers,
            16 * self.steps,
            16 * self.relocations,
            32 * self.payloads,
        )
        return A.KA_PKG_HEADER + sum((size + 31) // 32 * 32 for size in sizes)

    def check(self, machine, node=0, *, bound=True):
        data = json.loads(machine) if isinstance(machine, str) else machine
        fw = data.get("machine", data)["fw"]
        for name in ("units", "buffers", "steps", "ack_reserve"):
            _bounded(node, "package." + name, getattr(self, name), U16)
        for name in ("payloads", "relocations"):
            _bounded(node, "package." + name, getattr(self, name), U32)
        _bounded(node, "package.bytes", self.wire_bytes, U32)
        _bounded(
            node,
            "RUL.005.units",
            self.units,
            min(fw["max_pkg_units"], fw["engine_units"], fw["max_units"]),
        )
        _bounded(node, "RUL.100.buffers", self.buffers, fw["max_buffers"])
        if bound and self.relocations:
            # A missing binding is not cured by splitting the graph.
            raise ValueError(f"node {node}: firmware requires host-bound relocations")
        return self

    @classmethod
    def read(cls, raw):
        if len(raw) < A.KA_PKG_HEADER:
            raise ValueError("truncated package header")
        header = struct.unpack_from(A.PKG_HDR_FMT, raw)
        expected = A.KA_PKG_MAGIC | A.KA_PKG_VERSION << 32 | A.KA_PKG_HEADER << 48
        if header[A.KA_PH_MAGIC] != expected:
            raise ValueError("unsupported package magic/version/header")
        total = header[A.KA_PH_SIZE] & U32
        if total < A.KA_PKG_HEADER or total > len(raw) or total % 8:
            raise ValueError("invalid package byte extent")
        counts, counts2 = header[A.KA_PH_COUNTS], header[A.KA_PH_COUNTS2]
        usage = cls(
            (counts2 >> 32) & U16,
            counts2 & U16,
            (counts2 >> 16) & U16,
            counts >> 32,
            counts & U32,
            counts2 >> 48,
            stored_bytes=total,
        )
        sections = (
            (A.KA_PH_OFF_UNIT, usage.units, 16),
            (A.KA_PH_OFF_BUF, usage.buffers, 32),
            (A.KA_PH_OFF_STEP, usage.steps, 16),
            (A.KA_PH_OFF_REL, usage.relocations, 16),
            (A.KA_PH_OFF_PAY, usage.payloads, 32),
        )
        for offset, count, size in sections:
            start = header[offset]
            # Reader admission uses the declared extents, not the writer's
            # canonical 32-byte padding. Empty tables may have offset zero;
            # even an empty table must still have start <= total.
            if start + count * size > total:
                raise ValueError("package table exceeds its byte extent")
        return usage


@dataclass(frozen=True)
class ResidentUsage:
    tasks: int
    payload_bytes: int

    @property
    def record_bytes(self):
        return self.tasks * A.OSTPU_TASK_BYTES

    @property
    def packed_bytes(self):
        return self.record_bytes + self.payload_bytes

    @property
    def required_slots(self):
        # ostpu's data arena must start on a 256-byte line.
        return ((self.packed_bytes + 255) // 256) * 256 // A.OSTPU_TASK_BYTES

    @classmethod
    def of(cls, graph):
        if any(
            type(index) is not int or not 0 <= index < len(graph.tasks)
            for index in graph.streams
        ):
            raise ValueError("stream names a task outside the graph")
        return cls(len(graph.tasks), sum(len(raw) for raw in graph.streams.values()))

    def check(self, capacity, node=0):
        if type(capacity) is not int or not 0 < capacity <= A.KDS_MAXTASKS:
            raise ValueError("invalid advertised resident task capacity")
        if not self.tasks:
            raise ValueError("retained resident graph is empty")
        _bounded(node, "resident.tasks", self.tasks, capacity)
        _bounded(
            node, "resident.bytes", self.packed_bytes, capacity * A.OSTPU_TASK_BYTES
        )
        return self


def inspect_artifact(artifact, machine, *, residents=None, units=None, **options):
    """Return exact per-node costs, optionally admitting the actual KAOS image.

    Translation uses the same options and node IDs as PreparedMultiGraph. No
    package, descriptor or task is uploaded. In particular, do not use the
    recorder's default capacity instead of the resident's advertised capacity.
    """
    result = {}
    for part in artifact.parts:
        usage = PackageUsage.read(part.package).check(machine, part.node)
        result[part.node] = {**asdict(usage), "package_bytes": len(part.package)}
    if residents is not None:
        if units is None:
            raise ValueError("KAOS admission requires its boot unit map")
        node_ids = {
            part.node: residents[part.node].data_ref >> 56 for part in artifact.parts
        }
        graphs = Graph.package_parts(artifact, units, node_ids=node_ids, **options)
        for node, graph in graphs.items():
            usage = ResidentUsage.of(graph)
            result[node]["resident"] = {
                **asdict(usage),
                "record_bytes": usage.record_bytes,
                "packed_bytes": usage.packed_bytes,
                "required_slots": usage.required_slots,
                "capacity": residents[node].capacity,
            }
            usage.check(residents[node].capacity, node)
    return result
