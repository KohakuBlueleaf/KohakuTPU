"""Placing tasks on a machine whose meshes each own their memory.

An address is `(mesh, base)`, because the same `base` on two meshes names two
different bytes. What may alias, what may share a fetch, and where a task is
allowed to run all follow from that one fact.

The board described here is the shape of the real one: four meshes, and `mesh_1`
carries no vector core.
"""

import pytest
from kohakuaccel.backend import Backend
from kohakuaccel.compile import compile
from kohakuaccel.ir import ScheduleIR
from kohakuaccel.ir.l2 import Policy, Region
from kohakuaccel.machinespec import MachineSpec, MeshSpec

CLUSTERS = ((1, 1), (2, 1))
CORES = ((2, 0),)

CARD = MachineSpec(
    name="four",
    meshes=tuple(
        MeshSpec(
            index=i,
            units={"MG": CLUSTERS} if i == 1 else {"MG": CLUSTERS, "VC": CORES},
            mem_ports=((1, 0),),
            agent=(1, 0),
        )
        for i in range(4)
    ),
)


class B(Backend):
    def encode(self, task, ctx):
        return [0] * task.flits


def placed(schedule, machine=CARD):
    """Compile `schedule` and return `(meshes, placement)`."""
    compile(schedule, machine, B())
    return schedule.meshes, schedule.placement


# ------------------------------------------------------------- what an address is
def test_the_same_base_on_two_meshes_is_two_different_regions():
    here, there = Region(0, 64, mesh=0), Region(0, 64, mesh=1)
    assert here != there
    assert not here.overlaps(there)


def test_a_write_and_a_read_on_different_meshes_are_not_ordered():
    """False edges here would serialise meshes that never touch each other."""
    s = ScheduleIR()
    s.task("MG", writes=(Region(0, 64, mesh=0),), mesh=0)
    s.task("MG", reads=(Region(0, 64, mesh=1),), mesh=1)
    assert s.infer_edges() == 0


def test_the_same_region_on_one_mesh_still_orders():
    s = ScheduleIR()
    s.task("MG", writes=(Region(0, 64, mesh=2),), mesh=2)
    s.task("MG", reads=(Region(0, 64, mesh=2),), mesh=2)
    assert s.infer_edges() == 1


# --------------------------------------------------------- where a task may run
def test_a_task_runs_where_its_operands_are():
    s = ScheduleIR()
    s.task("MG", reads=(Region(0, 64, mesh=2),))
    meshes, _ = placed(s)
    assert meshes == [2]


def test_writes_decide_when_a_task_reads_nothing():
    s = ScheduleIR()
    s.task("MG", writes=(Region(0, 64, mesh=3),))
    meshes, _ = placed(s)
    assert meshes == [3]


def test_reads_decide_before_writes():
    """A unit fetches through its own memory agent; a result may go elsewhere."""
    s = ScheduleIR()
    s.task("MG", reads=(Region(0, 64, mesh=2),), writes=(Region(0, 64, mesh=3),))
    meshes, _ = placed(s)
    assert meshes == [2]


def test_an_explicit_mesh_wins_over_the_operands():
    s = ScheduleIR()
    s.task("MG", reads=(Region(0, 64, mesh=2),), mesh=3)
    meshes, _ = placed(s)
    assert meshes == [3]


def test_reading_several_meshes_without_naming_one_is_refused():
    s = ScheduleIR()
    s.task("MG", reads=(Region(0, 64, mesh=0), Region(0, 64, mesh=1)))
    with pytest.raises(ValueError, match=r"reads meshes \[0, 1\] and names none"):
        placed(s)


def test_a_one_mesh_machine_ignores_the_field_entirely():
    """Every schedule written before there were four meshes still places."""
    one = MachineSpec(name="one", units={"MG": CLUSTERS})
    s = ScheduleIR()
    s.task("MG", reads=(Region(0, 64, mesh=3),))
    meshes, _ = placed(s, one)
    assert meshes == [0]


# ----------------------------------------------------------------- the balancing
def test_load_is_accumulated_per_mesh_not_per_coordinate():
    """The same coordinate on two meshes is two units, so two load counters.

    Three tasks on mesh_0 fill (1,1) twice and (2,1) once. A single counter per
    coordinate would then push mesh_1's task onto (2,1) -- balancing it against
    work on a mesh it cannot see.
    """
    s = ScheduleIR()
    for _ in range(3):
        s.task("MG", mesh=0)
    s.task("MG", mesh=1)
    meshes, where = placed(s)
    assert meshes == [0, 0, 0, 1]
    assert where[3] == (1, 1)


# ------------------------------------------------------------ refusing a mesh
def test_a_unit_type_that_mesh_lacks_names_the_meshes_that_have_it():
    s = ScheduleIR()
    s.task("VC", mesh=1)
    with pytest.raises(ValueError, match=r"mesh_1 has no 'VC'"):
        placed(s)


def test_pinning_is_checked_against_that_mesh():
    s = ScheduleIR()
    s.task("MG", mesh=1, policy=Policy.PINNED, coord=(9, 9))
    with pytest.raises(ValueError, match=r"carries no 'MG' unit on mesh_1"):
        placed(s)


# ---------------------------------------------------------------- the interlink
def test_a_result_addressed_to_another_mesh_is_recorded_as_remote():
    """`place.remote` is the traffic an interlink has to carry, and the set a
    backend without one has to refuse."""
    s = ScheduleIR()
    s.task("MG", reads=(Region(0, 64, mesh=1),), writes=(Region(0, 64, mesh=3),))
    s.task("MG", reads=(Region(0, 64, mesh=1),), writes=(Region(64, 64, mesh=1),))
    result = compile(s, CARD, B())
    assert result.context.notes["place.remote"] == {0: (3,)}


def test_a_fetch_is_not_shared_across_meshes():
    """Coalescing hands a fetch extra destinations, and those are coordinates."""
    s = ScheduleIR()
    s.task("MG", reads=(Region(0, 64, mesh=0),), mesh=0)
    s.task("MG", reads=(Region(0, 64, mesh=1),), mesh=1)
    result = compile(s, CARD, B())
    assert result.context.notes["coalesce.peers"] == {}


def test_a_fetch_is_still_shared_within_one_mesh():
    s = ScheduleIR()
    s.task("MG", reads=(Region(0, 64, mesh=1),), mesh=1)
    s.task("MG", reads=(Region(0, 64, mesh=1),), mesh=1)
    result = compile(s, CARD, B())
    assert result.context.notes["coalesce.peers"] == {0: ((2, 1),)}
