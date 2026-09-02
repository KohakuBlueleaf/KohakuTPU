"""The fabric walk and the reduce schedule, against the card's real topology.

Framework only: no device, no kernel, no card. What is being pinned is that
every hop a reduce schedules crosses exactly ONE link, because a transfer two
links out does not arrive slowly on a fabric without forwarding -- it does not
arrive, and nothing reports it.

Run as ``python -m tests.test_collective`` from ``compiler/``.
"""

import itertools

from kohakuaccel.collective import converge, hops_to
from kohakuaccel.machinespec import MachineSpec, MeshSpec

#: The card: four meshes down the SLR stack, mesh i in SLR i, adjacent ones
#: joined (rt.CHAIN). TWISTED is a fabric whose walk is not index order.
CHAIN = ((0, 1), (1, 2), (2, 3))
RING = CHAIN + ((3, 0),)
TWISTED = ((0, 1), (1, 3), (3, 2))


def card(links=CHAIN, n=4) -> MachineSpec:
    return MachineSpec(
        name="test",
        meshes=tuple(MeshSpec(i, {"MG": ((0, 0),)}) for i in range(n)),
        links=links,
    )


def test_fabric_order_follows_the_links_not_the_indices() -> None:
    """The whole reason `path` exists: the walk is read off the links."""
    assert card().path() == (0, 1, 2, 3)
    assert card(TWISTED).path() == (0, 1, 3, 2)


def test_every_hop_crosses_one_link_wherever_the_sum_lands() -> None:
    spec = card()
    walk = spec.path()
    for into in walk:
        hops = converge(walk, into)
        assert len(hops) == len(walk) - 1, into
        for src, dst in hops:
            assert spec.mesh_hops(src, dst) == 1, f"{src}->{dst} for into={into}"


def test_a_hops_source_has_already_absorbed_everything_beyond_it() -> None:
    """The ordering that makes the sum right, checked by replaying it."""
    walk = card().path()
    for into in walk:
        held = {m: {m} for m in walk}
        for src, dst in converge(walk, into):
            held[dst] |= held[src]
        assert held[into] == set(walk), into


def test_converging_inward_halves_the_depth() -> None:
    walk = card().path()
    assert hops_to(walk, walk[0]) == 3
    assert hops_to(walk, walk[-1]) == 3
    assert hops_to(walk, walk[1]) == 2
    assert hops_to(walk, walk[2]) == 2
    for into in walk:
        assert hops_to(walk, into) == max(
            len([h for h in converge(walk, into) if h[0] in walk[: walk.index(into)]]),
            len(
                [
                    h
                    for h in converge(walk, into)
                    if h[0] in walk[walk.index(into) + 1 :]
                ]
            ),
        )


def test_a_ring_walks_as_a_chain_with_one_link_unused() -> None:
    """A ring without forwarding cannot reach its diagonal; the walk avoids it."""
    spec = card(RING)
    walk = spec.path()
    assert len(walk) == 4 and set(walk) == {0, 1, 2, 3}
    joined = {frozenset(p) for p in RING}
    used = {frozenset(p) for p in itertools.pairwise(walk)}
    assert used < joined, "a walk of four meshes uses three of the ring's four links"


def test_a_fork_is_refused_rather_than_resolved() -> None:
    star = ((0, 1), (0, 2), (0, 3))
    try:
        card(star).path()
    except ValueError as exc:
        assert "not a path" in str(exc) and "mesh 0" in str(exc)
        return
    raise AssertionError("a star was walked as if it were a chain")


def test_a_disconnected_group_is_refused() -> None:
    try:
        card(((0, 1), (2, 3))).path()
    except ValueError as exc:
        assert "not connected" in str(exc)
        return
    raise AssertionError("two separate segments were walked as one")


def test_no_links_means_complete_and_index_order() -> None:
    """What every caller assumed before a topology could be stated."""
    assert card(links=()).path() == (0, 1, 2, 3)


def test_a_subset_walks_on_its_own_induced_links() -> None:
    assert card().path([0, 1]) == (0, 1)
    assert card().path([1, 2, 3]) == (1, 2, 3)
    assert card(TWISTED).path([1, 3, 2]) == (1, 3, 2)
    try:
        card().path([0, 2])
    except ValueError as exc:
        assert "not connected" in str(exc)
        return
    raise AssertionError("meshes 0 and 2 are two links apart and were walked as one")


def test_a_destination_off_the_walk_is_refused() -> None:
    try:
        converge((0, 1, 3, 2), 7)
    except ValueError as exc:
        assert "not on the path" in str(exc)
        return
    raise AssertionError("a sum was scheduled into a mesh that is not in the group")


def test_two_meshes_are_one_hop_and_that_is_plan_reduce() -> None:
    """The chain schedule degenerates to the existing two-mesh one, not past it."""
    assert converge(card(n=2, links=((0, 1),)).path(), 1) == [(0, 1)]


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as exc:
            bad += 1
            print(f"  FAIL  {t.__name__}: {exc}")
    print(f"  {len(tests) - bad}/{len(tests)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
