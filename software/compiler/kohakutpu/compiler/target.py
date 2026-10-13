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


#: Vector core builds by name (scripts/tcl/v8t3/00_config.tcl VEC_CORE):
#: (register count 2**RW, register-file read copies a bank).
CORES = {"v2": (32, 4), "v21": (128, 2)}
#: The core a machine has when it names none.
CORE = "v2"
VREGS, VCOPIES = CORES[CORE]


def machine(
    mg=None,
    vc=None,
    reply=SYS_REPLY,
    mesh: int = 0,
    nodes: int | None = None,
    die: str = "4+2",
    core: str = CORE,
    vregs: int | None = None,
    vcopies: int | None = None,
) -> MachineSpec:
    """The units of a `die`-shaped die, seen from mesh `mesh`; with `nodes`, a
    card of that many identical dies (meshes 0..nodes-1, a chain). `mg`/`vc`
    override the die's unit lists. The vector cores are build `core`
    (`CORES`); `vregs` / `vcopies` override its register count (`v2_core.v`
    2**RW: 32, 64 or 128) and its register file's read copies a bank
    (RF_COPIES: 2, 3 or 4)."""
    regs, copies = CORES[core]
    vregs = regs if vregs is None else vregs
    vcopies = copies if vcopies is None else vcopies
    shape_mg, shape_vc = DIES[die]
    units = {"MG": tuple(mg or shape_mg), "VC": tuple(vc or shape_vc)}
    which = range(nodes) if nodes else (mesh,)
    return MachineSpec(
        name=NAME,
        inst_depth=INST_DEPTH,
        meshes=tuple(MeshSpec(i, units, SYS_PORTS, reply) for i in which),
        default=mesh,
        links=tuple((i, i + 1) for i in range((nodes or 1) - 1)),
        params={"VC": {"regs": vregs, "copies": vcopies}},
    )


def vregs(m: MachineSpec) -> int:
    """`m`'s vector core register count."""
    return m.params.get("VC", {}).get("regs", VREGS)


def vcopies(m: MachineSpec) -> int:
    """`m`'s vector core register file read copies a bank."""
    return m.params.get("VC", {}).get("copies", VCOPIES)


__all__ = [
    "CORE",
    "CORES",
    "DIES",
    "MG",
    "NAME",
    "SYS_PORTS",
    "SYS_REPLY",
    "VC",
    "VCOPIES",
    "VREGS",
    "machine",
    "vcopies",
    "vregs",
]
