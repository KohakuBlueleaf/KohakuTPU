"""KohakuTPU v9: the machine the compiler emits for, and the answers the
language's passes ask of it (`TARGET`).

A v9 die is one mesh of 4 matmul clusters and 2 V2 vector cores; its node's
dispatcher sits at the agent.
"""

from kohakuaccel.compiler.machine import MachineSpec
from kohakutpu.compiler.encode import vector as V
from kohakutpu.language.target import Target

NAME = "ktpu.v9"
MG = ((1, 0), (1, 1), (2, 0), (2, 1))
VC = ((1, 2), (2, 2))
AGENT = (0, 1)
#: A unit's instruction FIFO: the most words in flight to it.
INST_DEPTH = 512
#: A vector core's L1, in 32-byte words.
VC_L1_WORDS = 512
#: Cycles from a vector op's last beat to a dependent op's first (the L1
#: scheduler's model).
VC_LATENCY = 20


def machine(mg=MG, vc=VC, agent=AGENT, mesh: int = 0) -> MachineSpec:
    """The v9 die's units, on mesh `mesh`."""
    return MachineSpec(
        name=NAME,
        units={"MG": tuple(mg), "VC": tuple(vc)},
        inst_depth=INST_DEPTH,
        agent=agent,
        default=mesh,
    )


def target(m: MachineSpec | None = None) -> Target:
    """What the language's passes ask of machine `m` (the v9 die by default)."""
    m = m or machine()
    return Target(
        name=NAME,
        units={kind: len(at) for kind, at in m.units.items()},
        lanes=V.LANES,
        chunks=V.CHUNKS,
        l1_words=VC_L1_WORDS,
        registers=V.REGS,
        vector_latency=VC_LATENCY,
    )


TARGET = target()

__all__ = ["MG", "NAME", "TARGET", "VC", "machine", "target"]
