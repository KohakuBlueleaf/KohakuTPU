"""Several nodes through L2 -> L1: cross-node order through the shared
memory (a ring and a slot wait where one node reads what another wrote in
an earlier phase), and the cross-node accumulator add (`drain ACC -> mg[J]
add node=M`)."""

import re

import pytest

from kohakutpu.compiler import kernel as K
from kohakutpu.compiler import target as T
from kohakutpu.compiler.l2 import pipeline
from kohakutpu.language import examples
from kohakutpu.language.examples import dsl as ex
from kohakutpu.language.text import module as M
from kohakutpu.language.text.syntax import TextError

TWO = T.machine(nodes=2)


def node_lines(l1: str, n: int) -> list:
    """The node-level lines of `node n` (unit words dropped)."""
    out, inside = [], False
    for ln in l1.splitlines():
        if re.match(r"^    node \d+$", ln):
            inside = ln.strip() == f"node {n}"
            continue
        if inside and re.match(r"^        \S", ln):
            out.append(ln.strip())
    return out


def test_the_l2_cross_node_split_k_lowers_to_the_handshake():
    m = examples.load(examples.example("xsplitk", "l2"))
    l1 = pipeline.compile_l1(m, "xsplitk", TWO).source
    src, dst = node_lines(l1, 0), node_lines(l1, 1)
    # The target opens its tile, then lets the source go.
    assert dst.index("ring mesh=0 slot=9") > dst.index("wait mg[0]")
    # The source waits for that ring, then adds and marks behind the add.
    assert "wait slot 9 consume n=1" in src
    assert re.search(r"drain n=4096 to=mesh1\.mg\[0\] buf=2 l1=0", l1)
    assert re.search(r"drain count n=4 to=mesh1\.mg\[0\] buf=0 l1=0", l1)
    assert "fill A0 recv n=1" in l1


def test_the_dsl_split_k_adds_from_every_other_node_one_at_a_time():
    e = ex.EXAMPLES["splitk"]
    c = K.specialise(e.kernel, e.given, machine=TWO, **e.consts)
    src = node_lines(c.l1_text, 1)
    adds = [i for i, ln in enumerate(src) if ln.startswith("wait slot 8")]
    assert len(adds) == 2
    # The second add waits for the first cluster's words to have gone.
    assert "wait mg[0]" in src[adds[0] : adds[1]]


def test_a_node_reading_another_nodes_writes_waits_for_its_ring():
    e = ex.EXAMPLES["linear"]
    c = K.specialise(e.kernel, e.given, machine=TWO, **e.consts)
    n0, n1 = node_lines(c.l1_text, 0), node_lines(c.l1_text, 1)
    # Node 0's clusters wrote the scratch node 1's cores read.
    ring = n0.index("ring mesh=1 slot=0")
    assert any(ln.startswith("wait mg[") for ln in n0[:ring])
    assert n1[0] == "wait slot 0 consume n=1"


def _bshare(rows: int, share: str = "") -> str:
    return f"""target ktpu.v9

fn bs.l2(x: mx7a[128, 512] @dram(gm=16, nk=2), w: mx7b[{rows}, 512] @dram(gn={rows // 4}, nk=2), y: f16[128, {rows}] @dram(tile=16x{rows // 4}))
    par n in 0..1 on node[n]
        par u in 0..2 on mg[u]
            acc = zero : f32[64, {rows}] @acc
            for k in 0..8
                a = fill x[u*64 : +64, k*64 : +64] : mx7a[64, 64] @A
                b = fill w[0 : +{rows}, k*64 : +64]{share} : mx7b[{rows}, 64] @B
                acc = gemm a, b, acc : f32[64, {rows}] @acc
            drain acc -> y[u*64 : +64, :]
"""


def test_shared_b_fills_stay_within_what_a_follower_takes():
    small = pipeline.compile_l1(M.read(_bshare(128), "<bs>"), "bs", T.machine(nodes=1))
    large = pipeline.compile_l1(M.read(_bshare(256), "<bs>"), "bs", T.machine(nodes=1))
    assert "peers=" in small.source  # 64 entries: shared
    assert "peers=" not in large.source  # 128: each cluster fills its own
    with pytest.raises(TextError, match="shared B fill of 128 entries"):
        pipeline.compile_l1(
            M.read(_bshare(256, " share=cross"), "<bs>"), "bs", T.machine(nodes=1)
        )


def test_one_node_programs_carry_no_handshake():
    e = ex.EXAMPLES["mlp"]
    c = K.specialise(e.kernel, e.given, machine=T.machine(nodes=1), **e.consts)
    assert "ring mesh" not in c.l1_text and "wait slot" not in c.l1_text
