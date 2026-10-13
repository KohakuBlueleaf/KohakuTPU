"""KohakuTPU v9: the machine the compiler emits for.

A v9 die is one mesh of 4 matmul clusters and 2 V2 vector cores around one
system node (`src/kohakutpu/top/maps/mesh_1x2_4+2.txt`); a v9 card chains
several dies, one system node each.
"""

from kohakuaccel.compiler.machine import MachineSpec, MeshSpec

NAME = "ktpu.v9"
MG = ((1, 0), (1, 1), (1, 2), (1, 3))
VC = ((2, 1), (2, 2))
#: The system node's two ports, one per router row on the west edge
#: (`ktpu_ship_1x2_4c2v_1m_nol2` MEM_X/MEM_Y and MEM_X1/MEM_Y1). Both serve
#: every flit type; a flit for another mesh crosses at either.
SYS_PORTS = ((0, 1), (0, 2))
#: Where units answer the system node's control path: port 0's coordinate
#: (mag.v, `ORC_X(MEM_X)`), which the hub accepts on either port.
SYS_REPLY = SYS_PORTS[0]
#: A unit's instruction FIFO: the most words in flight to it.
INST_DEPTH = 512
#: Die shapes by name: (MG, VC). Both put the system node's ports at
#: SYS_PORTS. "6+2" is `ktpu_ship_2x2_6c2v_1m_nol2` (maps/mesh_2x2_6+2.txt):
#: clusters 0-2 on port (0,1), 3-5 on (0,2), cores at the column-2 edges.
DIES = {
    "4+2": (MG, VC),
    "6+2": (((1, 0), (1, 1), (2, 1), (1, 2), (2, 2), (1, 3)), ((2, 0), (2, 3))),
}


def machine(
    mg=None,
    vc=None,
    reply=SYS_REPLY,
    mesh: int = 0,
    nodes: int | None = None,
    die: str = "4+2",
) -> MachineSpec:
    """The units of a `die`-shaped die, seen from mesh `mesh`; with `nodes`, a
    card of that many identical dies (meshes 0..nodes-1, a chain). `mg`/`vc`
    override the die's unit lists."""
    shape_mg, shape_vc = DIES[die]
    units = {"MG": tuple(mg or shape_mg), "VC": tuple(vc or shape_vc)}
    which = range(nodes) if nodes else (mesh,)
    return MachineSpec(
        name=NAME,
        inst_depth=INST_DEPTH,
        meshes=tuple(MeshSpec(i, units, SYS_PORTS, reply) for i in which),
        default=mesh,
        links=tuple((i, i + 1) for i in range((nodes or 1) - 1)),
    )


__all__ = ["DIES", "MG", "NAME", "SYS_PORTS", "SYS_REPLY", "VC", "machine"]
