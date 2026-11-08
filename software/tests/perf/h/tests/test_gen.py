"""The round h generators, offline: every fn of each compiles for four sysnodes on
both boards and passes the static checker, and each fn's reference answers its
outputs at their sizes from inputs that fit its parameters."""

import collections
import functools

import numpy as np
import pytest

from h import gen
from h.placement import buffers
from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.card.common import MACHINES
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.application.tools import l1
from kohakutpu.language.text import module as M
from kohakutpu.stack import kernel

ROWS = (
    "q0",
    "p1",
    "p2",
    "p3",
    "p4",
    "p5",
    "p6",
    "p7",
    "p8",
    "p9",
    "p10",
    "p11",
    "p12",
    "p13",
    "p14",
    "p15",
    "p16",
    "p18",
    "p19",
    "c1",
    "c2",
    "c3",
    "c4",
    "c5",
)
#: Fns compiled one an item in their own test (an 8192-trip stamp is ~15 s cold).
SEPARATE = ("c2_i79_",)
BOARDS = ("multimesh_v9t6", "multimesh_v9t8")
SYSNODES = (0, 1, 2, 3)
#: Buffers 64 KiB apart from 1 MiB; staging from its own offset past the queue.
DRAM, STAGE, ALIGN = 0x10_0000, 0x8_0000, 0x1_0000


def placed(layout, machine) -> dict:
    """Each parameter at an address the runtime could give it."""
    binds, at, staged = {}, DRAM, collections.Counter()
    for p, b in layout:
        size = -(-b.nbytes // ALIGN) * ALIGN
        if b.space == "stage":
            n = b.layout["node"]
            binds[p] = machine.stage_addr(STAGE + staged[n], n)
            staged[n] += size
        else:
            binds[p], at = at, at + size
    return binds


@pytest.mark.parametrize("board", BOARDS)
@pytest.mark.parametrize("row", ROWS)
def test_every_fn_compiles_on_four_sysnodes_and_the_checker_passes_it(row, board):
    mod = gen.load(row, SYSNODES, board)
    text = M.read(mod.source, f"<{row}>")
    machine = machine_of(board, nodes=len(SYSNODES))
    json = (MACHINES / f"{board}.json").read_text("utf-8")
    assert sorted(mod.specs) == sorted(text.names())
    for name, spec in mod.specs.items():
        if name.startswith(SEPARATE):
            continue
        if spec.refused:
            with pytest.raises(Exception, match=spec.refused):
                compiled = kernel.compile(text, name, machine, level="l1")
                progs = compiled.programs(placed(buffers(compiled, json), machine))
                B.findings(progs, "refuse")
            continue
        compiled = kernel.compile(text, name, machine, level="l1")
        progs = compiled.programs(placed(buffers(compiled, json), machine))
        try:
            found = B.findings(progs, "refuse")
        except B.Refused as refused:
            pytest.fail(f"{name}: {refused}")
        assert found == [], name


@functools.cache
def loaded(row: str):
    """A row's generated module on the first board and its parsed text."""
    mod = gen.load(row, SYSNODES, BOARDS[0])
    return mod, M.read(mod.source, f"<{row}>")


#: Every fn of every row, one reference test each (the largest take seconds).
FNS = [(row, name) for row in ROWS for name in sorted(loaded(row)[0].specs)]


@pytest.mark.parametrize("row,name", FNS)
def test_each_reference_answers_every_output_at_its_size(row, name):
    mod, text = loaded(row)
    spec = mod.specs[name]
    json = (MACHINES / f"{BOARDS[0]}.json").read_text("utf-8")
    layout = dict(l1.bufs(text, name, json))
    assert set(spec.outputs) <= set(layout)
    assert set(spec.owners) <= set(spec.outputs)
    given = spec.inputs(1) if spec.inputs else {}
    for p, v in given.items():
        assert p not in spec.outputs, p
        host = np.prod(layout[p].host_shape) * np.dtype(layout[p].host_dtype).itemsize
        assert np.asarray(v).nbytes == host, p
    if spec.ledger:
        assert "slots" in spec.outputs
        assert layout["slots"].nbytes == len(spec.ledger.slots) * 8
    if spec.reference is None:
        return
    rng = np.random.default_rng(7)
    ins = {p: l1.seeded(b, rng) for p, b in layout.items() if p not in spec.outputs}
    want = spec.reference(ins | given | {"epoch": 1})
    assert set(want) == set(spec.outputs) - {"slots"}
    for p, ref in want.items():
        assert np.asarray(ref).size == np.prod(layout[p].host_shape), p


def test_c4_emits_the_witness_with_sysnode_1_observing_before_it_reads():
    mod = gen.load("c4", SYSNODES, BOARDS[0])
    late = mod.source[mod.source.index("fn c4_frontier_late.l1(") :]
    node1 = late[late.index("    node 1") :]
    assert node1.index("wait slot") < node1.index("store seen") < node1.index("send vc")
    node0 = late[late.index("    node 0") : late.index("    node 1")]
    assert node0.index("run spin(") < node0.index("run release_frontier_")


def test_ring_amounts_past_the_field_are_refused_at_compile():
    machine = machine_of(BOARDS[0], nodes=2)
    for amount in (0, 1 << 16):
        src = (
            "target ktpu.v9\nfn e.l1(unused: u64[4] @dram)\n    node 0\n"
            f"        ring mesh=1 slot=1 amount={amount}\n    node 1\n"
            f"        wait slot 1 n=1 consume\n"
        )
        with pytest.raises(Exception, match="L0.STEP.033"):
            kernel.compile(M.read(src, "<e>"), "e", machine, level="l1").programs(
                {"unused": DRAM}
            )
