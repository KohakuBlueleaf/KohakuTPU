"""A machine whose meshes are not alike, and what placement may ask of it.

The board this describes: `mesh_0/2/3` are `ktpu_ship_2x2_6c2v_1m` -- six
clusters and two vector cores -- and `mesh_1` is `ktpu_ship_2x1_6c0v_1m`, six
clusters and NO vector core. So a machine description with one flat unit table
cannot say where a softmax may run, and says the wrong thing about mesh_1.

The kernels are FIXTURES (rule 2b). What is under test is which unit a stage
needs and when that is discovered -- not any shipped kernel's tiling.
"""

import pytest
from kohakuaccel.lang import iface
from kohakuaccel.machinespec import MachineSpec, MeshSpec
from kohakutpu.lang import LangError

from tests.fixtures import Shaped, mm, mm_silu, rownorm

CLUSTERS = ((1, 1), (2, 1), (1, 2), (2, 2), (1, 3), (2, 3))
CORES = ((2, 0), (2, 4))

#: The v3 card, as `Card` enumerates it.
CARD = MachineSpec(
    name="kohakutpu",
    meshes=tuple(
        MeshSpec(
            index=i,
            units={"MG": CLUSTERS} if i == 1 else {"MG": CLUSTERS, "VC": CORES},
            agent=(1, 0),
        )
        for i in range(4)
    ),
    inst_depth=512,
)


def compile_on(fn, machine, bound: dict, **knobs):
    return fn.compile(machine, iface.solve(fn.signature, bound), **knobs)


# --------------------------------------------------------- the one-mesh shape
def test_a_flat_unit_table_still_describes_one_mesh():
    """Every caller written before there were four meshes passes `units=`."""
    one = MachineSpec(name="one", units={"MG": CLUSTERS, "VC": CORES}, agent=(1, 0))
    assert one.coords("MG") == CLUSTERS
    assert one.coords("VC") == CORES
    assert [m.index for m in one.meshes] == [0]
    assert one.mesh().agent == (1, 0)


def test_a_unit_type_no_mesh_carries_says_so():
    one = MachineSpec(name="one", units={"MG": CLUSTERS})
    with pytest.raises(ValueError, match="no mesh on this machine carries 'VC'"):
        one.coords("VC")


# ---------------------------------------------------------------- per mesh
def test_the_default_mesh_is_the_one_the_flat_accessors_answer_for():
    assert CARD.default == 0
    assert CARD.units == {"MG": CLUSTERS, "VC": CORES}
    assert CARD.coords("VC") == CORES


def test_mesh_1_has_clusters_and_no_vector_cores():
    assert CARD.coords("MG", mesh=1) == CLUSTERS
    assert CARD.count("MG", mesh=1) == 6
    assert CARD.count("VC", mesh=1) == 0
    assert not CARD.has("VC", mesh=1)
    assert CARD.meshes_with("VC") == (0, 2, 3)


def test_refusing_a_vector_stage_on_mesh_1_names_where_it_could_run():
    """A refusal that does not say where else is a dead end, not a diagnosis."""
    with pytest.raises(ValueError, match=r"mesh_1 has no 'VC' units"):
        CARD.coords("VC", mesh=1)
    with pytest.raises(ValueError, match=r"carrying 'VC': \[0, 2, 3\]"):
        CARD.coords("VC", mesh=1)


def test_for_mesh_moves_the_default_and_keeps_the_others_visible():
    one = CARD.for_mesh(1)
    assert one.coords("MG") == CLUSTERS
    assert one.meshes_with("VC") == (0, 2, 3)
    with pytest.raises(ValueError, match="mesh_1 has no 'VC'"):
        one.coords("VC")


def test_a_mesh_this_machine_does_not_have_is_refused():
    with pytest.raises(ValueError, match="has no mesh 7"):
        CARD.coords("MG", mesh=7)


# ----------------------------------------------------------- the address map
def test_a_local_address_says_which_mesh_it_is_local_to():
    """Bits 33:32 clear mean mesh 0 to the hardware, wherever they are issued.

    Measured on the card: an ordinary matmul on mesh_2 emitting bare 32-bit
    addresses raises `IL_F_RD_REMOTE` on every FILL, and is right only because
    an undecoded remote address aliases to local DRAM instead of routing.
    """
    assert CARD.global_addr(0x1800_0000) == 0x1800_0000
    assert CARD.global_addr(0x1800_0000, mesh=2) == 0x2_1800_0000
    assert CARD.for_mesh(2).global_addr(0x1800_0000) == 0x2_1800_0000
    assert MachineSpec.addr_mesh(0x2_1800_0000) == 2


def test_an_address_wider_than_one_mesh_is_refused():
    with pytest.raises(ValueError, match="does not fit one mesh's 4 GB"):
        CARD.global_addr(0x1_0000_0000)


def test_an_address_on_a_mesh_that_does_not_exist_is_refused():
    with pytest.raises(ValueError, match="has no mesh 7"):
        CARD.global_addr(0x1000, mesh=7)


# ------------------------------------------------- what mesh_1 can still run
MM = {"a": Shaped((32, 64)), "b": Shaped((64, 64))}


def test_a_matmul_compiles_on_the_mesh_with_no_vector_cores():
    """The failure this whole change exists for: `Device(Card(which=[1]))`.

    A pure matmul never dispatches to a vector core, and used to die asking for
    one anyway -- so six perfectly good clusters were unreachable.
    """
    got = compile_on(mm, CARD.for_mesh(1), MM)
    assert [s.unit for s in got.stages] == ["MG"]


def test_a_fused_epilogue_on_that_mesh_is_refused_and_says_where_to_go():
    """The epilogue NAMES a core, so this was always caught while compiling."""
    assert [s.unit for s in compile_on(mm_silu, CARD, MM).stages] == ["MG", "VC"]
    with pytest.raises((LangError, ValueError), match=r"mesh_1 has no 'VC'"):
        compile_on(mm_silu, CARD.for_mesh(1), MM, gm=4, gn=8, nk=2)


def test_an_unfused_vector_stage_is_refused_at_COMPILE_time():
    """Not at dispatch, which is where this used to be found.

    A fused epilogue names a core while compiling and so was caught; an
    ordinary row reduction names no coordinate, so a kernel like this compiled
    clean for a mesh with no vector core and died only once it was handed
    operands. A placement pass asking "can this run here" got the wrong answer.
    """
    bound = {"x": Shaped((64, 64))}
    assert compile_on(rownorm, CARD, bound).stages
    with pytest.raises(LangError, match=r"mesh_1 has no 'VC' units"):
        compile_on(rownorm, CARD.for_mesh(1), bound)


def test_the_refusal_names_the_meshes_that_could_run_it():
    with pytest.raises(LangError, match=r"carrying 'VC': \[0, 2, 3\]"):
        compile_on(rownorm, CARD.for_mesh(1), {"x": Shaped((64, 64))})
