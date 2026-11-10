"""Immutable bound node packages and receiver-scoped retained event ownership."""

import hashlib
import struct
import threading
from dataclasses import dataclass
from functools import cached_property
from typing import Protocol

from kohakuaccel.driver.node.queue.group import PreparedQueueGroup
from kohakutpu.application.runtime.os import abi as A


@dataclass(frozen=True)
class NodePart:
    node: int
    package: bytes


class GraphArtifact(Protocol):
    """Read-only transport view, shared by a complete stage and a sequence."""

    @property
    def parts(self) -> tuple[NodePart, ...]: ...

    @property
    def digest(self) -> bytes: ...

    @property
    def manifest(self) -> bytes: ...

    @property
    def slot_leases(self) -> frozenset[tuple[int, int]]: ...


class RetainedArtifact(GraphArtifact, Protocol):
    """An ordered, nonempty collection of independently admitted stages."""

    @property
    def stages(self) -> tuple[GraphArtifact, ...]: ...

    @property
    def package_lists(self) -> dict[int, tuple[bytes, ...]]: ...


@dataclass(frozen=True)
class BoundGraphArtifact:
    """One address-bound program, independent of its submission transport.

    The binding key includes allocation generations and physical extents. The
    manifest carries the compiler's access, layout and event-lifetime proofs.
    Neither adapter allocates slots or rebinds addresses in these bytes.
    """

    machine_digest: str
    parts: tuple[NodePart, ...]
    binding_key: bytes = b""
    manifest: bytes = b""

    def __post_init__(self):
        nodes = {part.node for part in self.parts}
        if not self.parts or len(nodes) != len(self.parts):
            raise ValueError("a bound graph needs unique node parts")
        for part in self.parts:
            if not 0 <= part.node < 256:
                raise ValueError("invalid sysnode identity")
            header = struct.unpack_from(A.PKG_HDR_FMT, part.package)
            if header[A.KA_PH_COUNTS] >> 32:
                raise ValueError("bound node parts must not contain relocations")
        # Events are already coloured by the whole-graph compiler.
        _ = self.slot_leases

    @property
    def slot_leases(self) -> frozenset[tuple[int, int]]:
        return self._slot_leases

    @cached_property
    def _slot_leases(self) -> frozenset[tuple[int, int]]:
        resources = set()
        balance: dict[tuple[int, int], int] = {}
        nodes = {part.node for part in self.parts}
        for part in self.parts:
            h = struct.unpack_from(A.PKG_HDR_FMT, part.package)
            for i in range((h[A.KA_PH_COUNTS2] >> 16) & 0xFFFF):
                word, arg = struct.unpack_from(
                    "<QQ", part.package, h[A.KA_PH_OFF_STEP] + 16 * i
                )
                op, unit, count = word & 255, (word >> 16) & 65535, word >> 32
                if op == A.KA_OP_END:
                    break
                if op == A.KA_OP_ENGINE:
                    for entry in range(count):
                        group = struct.unpack_from(
                            "<4Q",
                            part.package,
                            h[A.KA_PH_OFF_PAY] + 32 * (arg + entry // 3),
                        )
                        code = (group[0] >> (8 * (entry % 3))) & A.KOS_DE_CODE_MASK
                        if code == A.KOS_DE_IL_RING:
                            value = group[entry % 3 + 1]
                            receiver, slot = value & 3, (value >> 8) & 255
                            if receiver not in nodes or not (
                                A.KOS_SLOT_USER <= slot < A.KOS_SLOT_LIMIT
                            ):
                                raise ValueError(
                                    "engine ring is outside its user-slot grant"
                                )
                            resources.add((receiver, slot))
                            key = (receiver, slot)
                            balance[key] = balance.get(key, 0) + (
                                ((value >> 16) & 65535) or 1
                            )
                    continue
                if op in (A.KA_OP_CTRL, A.KA_OP_JUMP, A.KA_OP_PATCH):
                    raise ValueError(
                        "dynamic event effects require a graph lifetime proof"
                    )
                if op == A.KA_OP_RING:
                    receiver, slot = unit, count
                elif op == A.KA_OP_WAIT_SLOT:
                    receiver, slot = part.node, unit
                else:
                    continue
                if (
                    receiver not in nodes
                    or not A.KOS_SLOT_USER <= slot < A.KOS_SLOT_LIMIT
                ):
                    raise ValueError("graph event is outside its user-slot grant")
                key = (receiver, slot)
                resources.add(key)
                if op == A.KA_OP_RING:
                    balance[key] = balance.get(key, 0) + ((arg & 65535) or 1)
                elif (word >> 8) & A.KA_STEP_F_CONSUME:
                    balance[key] = balance.get(key, 0) - count
        if any(balance.values()):
            raise ValueError("graph completion must consume every released event token")
        return frozenset(resources)

    @property
    def digest(self) -> bytes:
        return self._digest

    @cached_property
    def _digest(self) -> bytes:
        digest = hashlib.blake2b(digest_size=32)
        fields = [self.machine_digest.encode(), self.binding_key, self.manifest]
        for part in self.parts:
            fields.extend((bytes([part.node]), part.package))
        for data in fields:
            digest.update(struct.pack("<Q", len(data)))
            digest.update(data)
        return digest.digest()

    @property
    def stages(self) -> tuple["BoundGraphArtifact", ...]:
        return (self,)

    @property
    def package_lists(self) -> dict[int, tuple[bytes, ...]]:
        return {part.node: (part.package,) for part in self.parts}


class SlotLeases:
    """One pool per card, shared by its firmware and KAOS adapters.

    Retention owns leases, not merely publication. A failed epoch keeps them
    until the card reset destroys the pool; close is not a reset operation.
    """

    def __init__(self):
        self._owners: dict[tuple[int, int], object] = {}
        self._lock = threading.Lock()

    def acquire(self, resources: frozenset[tuple[int, int]], owner: object) -> None:
        with self._lock:
            if any(resource in self._owners for resource in resources):
                raise RuntimeError("graph event slots are already leased")
            self._owners.update(dict.fromkeys(resources, owner))

    def release(self, owner: object) -> None:
        with self._lock:
            self._owners = {
                key: value for key, value in self._owners.items() if value is not owner
            }


class PreparedFirmwareGraph:
    """The firmware adapter owns the same bound bytes, pins and receiver leases."""

    def __init__(
        self,
        artifact: RetainedArtifact,
        queues,
        leases: SlotLeases,
        references=(),
        *,
        timeout_cycles=1 << 26,
    ):
        self._lock = threading.RLock()
        self.artifact = artifact
        self.leases = leases
        self.references = []
        self.closed = False
        leases.acquire(artifact.slot_leases, self)
        try:
            for arena, address in references:
                arena.retain_pinned(address)
                self.references.append((arena, address))
            self.group = PreparedQueueGroup(
                artifact.package_lists,
                queues,
                timeout_cycles,
            )
        except BaseException:
            for arena, address in self.references:
                arena.unretain(address)
            leases.release(self)
            raise

    def submit(self):
        return self.group.submit()

    def run(self):
        return self.group.run()

    def close(self):
        with self._lock:
            if self.closed:
                return
            self.group.close()
            for arena, address in self.references:
                arena.unretain(address)
            self.references.clear()
            self.leases.release(self)
            self.closed = True
