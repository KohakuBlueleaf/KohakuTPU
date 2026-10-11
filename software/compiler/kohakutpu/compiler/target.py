"""KohakuTPU v9: the machine the compiler emits for.

A v9 die is one mesh of 4 matmul clusters and 2 V2 vector cores; its node's
dispatcher sits at the agent. A v9 card chains several dies, one node each.
"""

from kohakuaccel.compiler.machine import MachineSpec, MeshSpec

NAME = "ktpu.v9"
MG = ((1, 0), (1, 1), (2, 0), (2, 1))
VC = ((1, 2), (2, 2))
AGENT = (0, 1)
#: MAG's two ports, one on each router's row end (`ktpu_ship_2x1_4c2v_1m_nol2`
#: MEM_X/MEM_Y and MEM_X1/MEM_Y1); a flit for another mesh crosses at either.
MEM_PORTS = ((0, 1), (3, 1))
#: A unit's instruction FIFO: the most words in flight to it.
INST_DEPTH = 512


def machine(
    mg=MG, vc=VC, agent=AGENT, mesh: int = 0, nodes: int | None = None
) -> MachineSpec:
    """The v9 die's units, seen from mesh `mesh`; with `nodes`, a card of that
    many identical dies (meshes 0..nodes-1, a chain)."""
    units = {"MG": tuple(mg), "VC": tuple(vc)}
    which = range(nodes) if nodes else (mesh,)
    return MachineSpec(
        name=NAME,
        inst_depth=INST_DEPTH,
        meshes=tuple(MeshSpec(i, units, MEM_PORTS, agent) for i in which),
        default=mesh,
        links=tuple((i, i + 1) for i in range((nodes or 1) - 1)),
    )


__all__ = ["AGENT", "MEM_PORTS", "MG", "NAME", "VC", "machine"]
