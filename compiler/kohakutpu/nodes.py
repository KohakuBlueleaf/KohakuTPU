"""Several sysnodes over ONE shared memory, driven as one device.

v9's dies reach one flat memory through the Xache, so a value written by one
node's units is read by another's at the same address: nothing is copied
between nodes, and the only cross-node cost is ORDER. This runtime presents
every node's mesh as one machine (`MachineSpec.dispatchers`): a stage's
instances are cut into one contiguous share per node, each share joins that
node's own package, and a stage that may read what another node wrote waits
for that node's doorbell first. Kernels, ops and the tinygrad seam see one
device and change nothing.

A stage whose instances name their units (`nodes`, `acks`: a fused epilogue's
cluster-to-core drains) runs whole on one node, together with the stage that
receives it.
"""

from dataclasses import replace

from kohakuaccel.memory import Arena
from kohakuaccel.package import engine as engine_package
from kohakuaccel.rt import Runtime
from kohakutpu.isa.fields import FIELDS
from kohakutpu.isa.vecemit import BATCH_BYTES
from kohakutpu.rt import Holder

#: The doorbell tag a stage barrier rings with.
BARRIER_TAG = 0x5B


class Nodes(Holder, Runtime):
    """Node-dispatched `kohakutpu.rt.Device`s, one per mesh, as one runtime.

    `devices` must each have a node queue and identical unit tables. The arena
    is this runtime's own, `[base, base + size)` in the shared memory, and
    every tensor lives there; a device's own arena stays unused. Raises
    :class:`ValueError` for an empty list, a device without a node, or meshes
    that differ.
    """

    def __init__(self, devices, base: int, size: int) -> None:
        self.devices = tuple(devices)
        if not self.devices:
            raise ValueError("a node group needs at least one device")
        first = self.devices[0]
        for d in self.devices:
            if d.node is None:
                raise ValueError(f"{d!r} dispatches from the host; a group needs nodes")
            if d.machine.mesh().units != first.machine.mesh().units:
                raise ValueError(
                    f"mesh {d.machine.default} carries {d.machine.mesh().units}, "
                    f"mesh {first.machine.default} {first.machine.mesh().units}: "
                    f"a stage's instances are dealt as if every mesh were the same"
                )
        machine = replace(first.machine, dispatchers=len(self.devices))
        arena = Arena(
            machine.global_addr(base), size, align=BATCH_BYTES, mesh=machine.default
        )
        super().__init__(machine, arena, first.transport, first.ctrl)
        self.node = first.node
        self.fields = FIELDS
        #: Devices whose writes no other device has waited for yet.
        self._dirty: set[int] = set()
        #: Where the last pinned stage ran, so its receiving stage follows it.
        self._pinned = 0
        self._turn = 0
        self.counters["barriers"] = 0

    # -------------------------------------------------------------- dispatch
    def dispatch(
        self, payloads: dict, unit: str, name: str = "kernel", nodes=None, acks=None
    ):
        """One stage: its instances cut over the devices, each share appended
        to that device's open package. Returns the rounds the shares took."""
        if acks is not None:
            self._pinned = self._turn % len(self.devices)
            self._turn += 1
            return self._on([self._pinned], [payloads], unit, name, nodes, acks)
        if nodes is not None:
            return self._on([self._pinned], [payloads], unit, name, nodes, acks)
        keys = sorted(payloads)
        n = min(len(keys), len(self.devices))
        cut = [keys[i * len(keys) // n : (i + 1) * len(keys) // n] for i in range(n)]
        shares = [{k: payloads[k] for k in part} for part in cut]
        return self._on(list(range(n)), shares, unit, name, None, None)

    def _on(self, which, shares, unit, name, nodes, acks) -> int:
        self._order(set(which))
        rounds = 0
        for i, share in zip(which, shares):
            rounds = max(
                rounds, self.devices[i].dispatch(share, unit, name, nodes, acks)
            )
        self._dirty = set(which)
        self.counters["dispatches"] += 1
        return rounds

    def move(self, writes, name: str = "move") -> None:
        """A mover move on the first device, after every write it may read."""
        self._order({0})
        self.devices[0].move(writes, name)
        self._dirty = {0}

    def _order(self, readers: set) -> None:
        """Make each of `readers` wait for every dirty device but itself.

        A doorbell is rung only once the ringing node's units have completed,
        and a completion is posted once its writes are acknowledged, so a bell
        orders memory. Transitive: a node that waited and then rings carries
        the writes it waited for.
        """
        pairs = [(w, r) for w in sorted(self._dirty) for r in sorted(readers) if w != r]
        if not pairs:
            return
        meshes = [d.machine.default for d in self.devices]
        for w, r in pairs:
            self.devices[w]._package().ring(meshes[r], BARRIER_TAG)
        for w, r in pairs:
            self.devices[r]._package().wait_bell(meshes[w], 1)
        for r in {r for _, r in pairs}:
            self.devices[r]._package().barrier()
        self.counters["barriers"] += 1

    # ------------------------------------------------------------- execution
    def flush(self) -> None:
        """Submit every device's open package, then wait for all of them."""
        jobs = []
        for d in self.devices:
            if d.__dict__.get("_fused") is not None:
                raise RuntimeError(
                    f"{d._fused[3]}: a fused cluster stage has no epilogue"
                )
            b, d._pending = d._pending, None
            if b is None or not len(b):
                continue
            built = b.build(defaults=False)
            if d.engine_packages:
                built = engine_package.lower(built)
            pkg = built.to_bytes()
            if d.keep_packages:
                d.packages.append(pkg)
            d.counters["packages"] = d.counters.get("packages", 0) + 1
            jobs.append((d, d.node.submit(pkg, b.bindings())))
        for d, tag in jobs:
            d.last_completion = d.node.wait(tag)
        self._dirty = set()

    def __repr__(self) -> str:
        return (
            f"Nodes({len(self.devices)} x {self.machine.count('MG')} MG + "
            f"{self.machine.count('VC')} VC, {self.arena.used:,}/"
            f"{self.arena.size:,} bytes used)"
        )
