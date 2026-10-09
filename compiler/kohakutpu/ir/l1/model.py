"""L1 programs run offline: lowered to a package, run on the unit models.

The same lowering a card runs (`Program.build`), interpreted by
`package.interp.LocalNode` over the L0 unit models (`model.ClusterUnit`,
`model.VectorUnit`, the mover model) -- no runtime above them -- so an L1 kernel
is checked for correctness with no card. Unit coordinates default to the v9
die's (4 clusters, 2 vector cores).
"""

import numpy as np
from kohakuaccel.machinespec import MachineSpec
from kohakuaccel.package.interp import LocalNode
from kohakuaccel.package.units import machine_units
from kohakuaccel.sim import MEM_BASE
from kohakuaccel.sim.mailbox import SimMailbox
from kohakutpu.model import ClusterUnit, Mesh, VectorUnit, run_move

V9_MG = ((1, 0), (1, 1), (2, 0), (2, 1))
V9_VC = ((1, 2), (2, 2))


class L1Model:
    def __init__(self, mg=V9_MG, vc=V9_VC, agent=(0, 1), size: int = 1 << 24) -> None:
        cores = {c: VectorUnit() for c in vc}
        units = {c: ClusterUnit() for c in mg}
        for unit in units.values():
            unit.peers = cores
        units.update(cores)
        self.card = Mesh(units=units)
        self.machine = MachineSpec(
            name="kohakutpu-l1",
            units={"MG": tuple(mg), "VC": tuple(vc)},
            inst_depth=512,
            agent=agent,
        )
        self.mem = self.card.mem
        self.node = LocalNode(
            SimMailbox(self.card),
            machine_units(self.machine),
            mover=lambda writes: run_move(writes, self.mem),
        )
        self.base, self.top, self.end = (
            MEM_BASE + 0x10_0000,
            MEM_BASE + 0x10_0000,
            MEM_BASE + size,
        )
        self.resident: dict = {}

    def alloc(self, nbytes: int, align: int = 256) -> int:
        at = -(-self.top // align) * align
        if at + nbytes > self.end:
            raise MemoryError(f"{nbytes} B past the model's memory")
        self.top = at + nbytes
        return at

    def put(self, data) -> int:
        raw = (
            data
            if isinstance(data, (bytes, bytearray))
            else np.ascontiguousarray(data).tobytes()
        )
        at = self.alloc(len(raw))
        self.mem.write(at - MEM_BASE, bytes(raw))
        return at

    def get(self, at: int, nbytes: int) -> bytes:
        return self.mem.read(at - MEM_BASE, nbytes)

    def run(self, program) -> object:
        """Lower `program` and run it; raises on a failed package."""
        b = program.build(None, self.resident)
        pkg = b.build(defaults=False)
        return self.node.run(pkg.to_bytes(), b.bindings())
