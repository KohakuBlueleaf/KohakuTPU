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
#: A vector core's fill on the node's memory path: (cycles before its first
#: word, cycles a 32-byte word), alone and with clusters streaming beside it.
#: Measured on card_v9t3_1n (a core's serial fills of 2 KB and 256 B): alone
#: 142.6 / 86.3 cycles, beside a four-cluster matmul 664 / 371. Alone takes
#: two cycles a word: the node's 32 B/cycle read bus split by the two cores.
MEM = {"alone": (78, 2), "shared": (330, 5)}
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
    mem: dict | None = None,
) -> MachineSpec:
    """The units of a `die`-shaped die, seen from mesh `mesh`; with `nodes`, a
    card of that many identical dies (meshes 0..nodes-1, a chain). `mg`/`vc`
    override the die's unit lists. The vector cores are build `core`
    (`CORES`); `vregs` / `vcopies` override its register count (`v2_core.v`
    2**RW: 32, 64 or 128) and its register file's read copies a bank
    (RF_COPIES: 2, 3 or 4). `mem` overrides MEM, the fill costs a vector
    lowering prefetches against (`mem`)."""
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
        params={
            "VC": {"regs": vregs, "copies": vcopies},
            **({"MEM": dict(mem)} if mem is not None else {}),
        },
    )


def machine_of(
    board: dict, nodes: int | None = None, mesh: int = 0, **kw
) -> MachineSpec:
    """`machine` for a board file: its die's units and system-node ports when
    it carries them (scripts/py/gen_board.py), the 4+2 die otherwise."""
    units = board.get("units")
    if not units:
        return machine(mesh=mesh, nodes=nodes, **kw)
    ports = tuple(tuple(p) for p in board["sys_ports"])
    if ports != SYS_PORTS:
        raise ValueError(
            f"{board['name']}: system-node ports {ports}, the compiler's {SYS_PORTS}"
        )
    return machine(
        mg=[tuple(p) for p in units["MG"]],
        vc=[tuple(p) for p in units["VC"]],
        mesh=mesh,
        nodes=nodes,
        **kw,
    )


def vregs(m: MachineSpec) -> int:
    """`m`'s vector core register count."""
    return m.params.get("VC", {}).get("regs", VREGS)


def vcopies(m: MachineSpec) -> int:
    """`m`'s vector core register file read copies a bank."""
    return m.params.get("VC", {}).get("copies", VCOPIES)


def mem(m: MachineSpec, shared: bool = False) -> tuple:
    """``(latency, cycles a word)`` of a vector core's fill on `m`: the node's
    memory path to the cores alone, or `shared` with clusters streaming
    beside them."""
    return tuple(m.params.get("MEM", MEM)["shared" if shared else "alone"])


__all__ = [
    "CORE",
    "CORES",
    "DIES",
    "MEM",
    "MG",
    "NAME",
    "SYS_PORTS",
    "SYS_REPLY",
    "VC",
    "VCOPIES",
    "VREGS",
    "machine",
    "machine_of",
    "mem",
    "vcopies",
    "vregs",
]
