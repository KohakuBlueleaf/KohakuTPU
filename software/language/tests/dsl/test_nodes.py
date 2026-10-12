"""Several nodes in the DSL, both ways: a par wider than one node spans the
nodes (shared memory through the Xache: no node named), and a kernel naming
another node's unit (a cross-node connection: an accumulator add)."""

import pytest

import kohakutpu.language.dsl as kt
from kohakutpu.language.text import module as M

TWO = kt.Machine(mg=4, vc=2, nodes=2)


@kt.kernel
def wide(x=kt.In("R", 128), y=kt.Out("R", 128)):
    cores = kt.machine().total("vc")
    per = x.shape[0] // cores
    for c in kt.par(cores, on=kt.vc):
        for t in kt.range(per // 8):
            r = c * per + t * 8
            kt.store(y[r : r + 8, :], kt.load(x[r : r + 8, :], gt4=True) * 2.0)


def test_a_par_wider_than_a_node_spans_the_nodes():
    one = wide.trace({"x": ((64, 128), "f16")})
    two = wide.trace({"x": ((64, 128), "f16")}, machine=TWO)
    assert "node[" not in one.text
    assert "par n1 in 0..2 on node[n1]" in two.text
    assert "par c1 in 0..2 on vc[c1]" in two.text
    # Instance n1*2 + c1 of the four takes 16 rows: node n1's share, then its own.
    assert "load x[n1*32 + c1*16 + t1*8 : +8]" in two.text
    M.read(two.text, "<wide>")


def test_anywhere_instances_split_evenly_over_the_nodes():
    @kt.kernel
    def anyk(x=kt.In("R", 128), y=kt.Out("R", 128)):
        for u in kt.par(8, on=kt.vc, at=kt.ANY):
            r = u * 8
            kt.store(y[r : r + 8, :], kt.load(x[r : r + 8, :], gt4=True))

    text = anyk.trace({"x": ((64, 128), "f16")}, machine=TWO).text
    assert "par n1 in 0..2 on node[n1]" in text and "on vc[any]" in text


def test_a_par_that_does_not_fill_whole_nodes_is_refused():
    @kt.kernel
    def odd(x=kt.In("R", 128), y=kt.Out("R", 128)):
        for c in kt.par(3, on=kt.vc):
            kt.store(
                y[c * 8 : c * 8 + 8, :], kt.load(x[c * 8 : c * 8 + 8, :], gt4=True)
            )

    with pytest.raises(kt.DSLError, match="do not split evenly"):
        odd.trace({"x": ((64, 128), "f16")}, machine=TWO)


@kt.kernel
def addacross(
    x=kt.In("M", "K", dtype="mx7a", layout=dict(gm=16, nk=2)),
    w=kt.In("N", "K", dtype="mx7b", layout=dict(gn=64, nk=2)),
    h=kt.Out("M", "N", layout=dict(tile=(16, 64))),
):
    for n in kt.par(2, on=kt.node):
        for u in kt.par(1, on=kt.mg):
            acc = kt.zero(64, 256)
            for k in kt.range(4):
                k0 = n * 256 + k * 64
                acc = kt.gemm(
                    kt.fill(x[0:64, k0 : k0 + 64]), kt.fill(w[0:256, k0 : k0 + 64]), acc
                )
            with kt.when(n == 0):
                kt.drain(acc, h[0:64, 0:256])
            with kt.when(n == 1):
                kt.drain(acc, kt.node[0].mg[u], add=True)


def test_a_cross_node_add_names_the_far_node():
    tr = addacross.trace(
        {"x": ((64, 512), "f16"), "w": ((256, 512), "f16")}, machine=TWO
    )
    assert "drain acc1 -> mg[u1] add node=0" in tr.text
    M.read(tr.text, "<addacross>")


def test_another_nodes_unit_takes_only_an_add():
    @kt.kernel
    def bad(
        x=kt.In("M", "K", dtype="mx7a", layout=dict(gm=16, nk=2)),
        w=kt.In("N", "K", dtype="mx7b", layout=dict(gn=64, nk=2)),
        h=kt.Out("M", "N"),
    ):
        for u in kt.par(1, on=kt.mg):
            acc = kt.gemm(kt.fill(x[0:64, 0:64]), kt.fill(w[0:256, 0:64]))
            kt.drain(acc, kt.node[1].mg[0])

    with pytest.raises(kt.DSLError, match="a tile is added"):
        bad.trace({"x": ((64, 64), "f16"), "w": ((256, 64), "f16")}, machine=TWO)


def test_a_node_unit_needs_a_node_index():
    with pytest.raises(kt.DSLError):
        _ = kt.node.mg
