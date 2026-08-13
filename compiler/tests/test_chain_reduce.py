"""What `plan_chain_reduce` emits, checked without a card or a real kernel.

`kohakutpu.meshes` binds ``ops.matmul`` at import, so this installs its own
before importing it: a compiler test that pulls in a shipped kernel is testing
that kernel's tiling choices too, and then a library change breaks it and says
nothing about the compiler.

Run as ``python -m tests.test_chain_reduce`` from ``compiler/``.
"""

import sys
import types

_stub = types.ModuleType("kohakutpu.ops")


class _Plan:
    grid = (1, 1)


class _Matmul:
    name = "matmul:test"

    @staticmethod
    def plan(*args, **knobs):
        return _Plan()

    def __call__(self, *args, **knobs):
        return ("ran", args, knobs)


_stub.matmul = _Matmul()

# The stub is UNINSTALLED once `meshes` has bound it: sys.modules is global, and
# leaving it in place makes every later test in the session import the fake.
sys.modules["kohakutpu.ops"] = _stub
try:
    from kohakutpu.meshes import PARTIAL, MeshGroup, Plan, Sharded, Spec, Step
finally:
    import kohakutpu

    for _name in ("kohakutpu.ops", "kohakutpu.meshes"):
        sys.modules.pop(_name, None)
    for _attr in ("ops", "meshes"):
        if getattr(kohakutpu, _attr, None) is not None:
            delattr(kohakutpu, _attr)

from kohakuaccel.machinespec import MachineSpec, MeshSpec

CHAIN = ((0, 1), (1, 3), (3, 2))


class _Part:
    def __init__(self, mesh: int) -> None:
        self.mesh = mesh


class _Arena:
    base, size = 0, 1 << 20


class _Device:
    """Enough of a Device for planning: a machine, an arena and a card flag."""

    def __init__(self, machine) -> None:
        self.machine = machine
        self.arena = _Arena()
        self.transport = object()
        self.mesh = object()


def group(n: int = 4) -> MeshGroup:
    spec = MachineSpec(
        name="test",
        meshes=tuple(MeshSpec(i, {"MG": ((i, 0),)}) for i in range(n)),
        links=tuple(p for p in CHAIN if max(p) < n),
    )
    return MeshGroup([_Device(spec.for_mesh(i)) for i in range(n)])


def split(g: MeshGroup, shape=(64, 256)) -> Sharded:
    return Sharded(g, Spec.on(1), [_Part(m) for m in g.meshes], shape)


def test_the_compute_plan_comes_first_and_opens_every_tile() -> None:
    g = group()
    plans = g.plan_chain_reduce(split(g), split(g))
    head = plans[0]
    assert head.into is None, "the compute plan aims nothing across a link"
    assert [s.rank for s in head.steps] == [0, 1, 2, 3]
    assert all(s.node is not None for s in head.steps), "every step must be pinned"


def test_there_is_one_plan_per_hop_and_each_is_one_step() -> None:
    g = group()
    plans = g.plan_chain_reduce(split(g), split(g))
    assert len(plans) == 4, "one compute plan and three hops over four meshes"
    for hop in plans[1:]:
        assert len(hop.steps) == 1
        assert hop.opens is False, "an earlier plan's GEMM opened the tile"
        assert hop.into is not None and hop.tile is not None


def test_every_hop_is_between_fabric_neighbours() -> None:
    g = group()
    for into in range(4):
        for hop in g.plan_chain_reduce(split(g), split(g), into=into)[1:]:
            assert g.adjacent(hop.steps[0].rank, hop.into), f"{hop} for into={into}"


def test_the_last_hop_lands_on_the_destination() -> None:
    g = group()
    for into in range(4):
        plans = g.plan_chain_reduce(split(g), split(g), into=into)
        assert plans[-1].into == into
        assert plans[-1].tile == g.devices[into].machine.coords("MG")[0]


def test_the_default_destination_is_the_far_end_of_the_walk() -> None:
    g = group()
    assert g.chain_order() == [0, 1, 3, 2]
    assert g.plan_chain_reduce(split(g), split(g))[-1].into == 2


def test_a_named_node_is_what_the_hop_aims_at() -> None:
    g = group()
    plans = g.plan_chain_reduce(split(g), split(g), into=0, nodes={1: (5, 5)})
    aimed = [h.tile for h in plans[1:] if h.into == 1]
    assert aimed == [(5, 5)], aimed


def test_a_pairing_needing_no_reduce_is_refused() -> None:
    g = group()
    copied = Sharded(g, Spec(), [_Part(m) for m in g.meshes], (64, 256))
    try:
        g.plan_chain_reduce(copied, copied)
    except ValueError as exc:
        assert "contraction split" in str(exc)
        return
    raise AssertionError("a copied pairing was planned as a reduce")


def test_a_reduce_over_one_mesh_is_refused() -> None:
    g = group(1)
    try:
        g.plan_chain_reduce(split(g), split(g))
    except ValueError as exc:
        assert "crosses no link" in str(exc)
        return
    raise AssertionError("a one-mesh chain reduce was planned")


def test_run_still_refuses_a_reduce_that_does_not_open_its_tile() -> None:
    """The guard that `opens=False` steps past, checked to still bite elsewhere."""
    g = group()
    bad = Plan(
        (Step(1, lambda: None, (), {}, None),), PARTIAL, (4, 4), into=0, tile=(0, 0)
    )
    try:
        g.run(bad)
    except ValueError as exc:
        assert "65504" in str(exc)
    else:
        raise AssertionError("a reduce plan ran without opening its destination tile")
    g.run(Plan(bad.steps, PARTIAL, (4, 4), into=0, tile=(0, 0), opens=False))


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as exc:
            bad += 1
            print(f"  FAIL  {type(exc).__name__}: {t.__name__}: {exc}")
    print(f"  {len(tests) - bad}/{len(tests)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
