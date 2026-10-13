"""The static checker (`kohakutpu.compiler.verify`,
docs/projects/kohakutpu/verify.md):
the examples verify clean, and each hazard it claims to catch is caught on a
program built to have exactly that hazard."""

import pytest

from kohakutpu.compiler import build, verify
from kohakutpu.compiler import target as T
from kohakutpu.compiler.layout.buffer import Buffer
from kohakutpu.language import examples
from kohakutpu.language.text import module as _module


def programs(m, nodes: int = 1):
    """The body's programs, every parameter bound to an address of its own."""
    machine = T.machine(nodes=nodes)
    fn = m.names()[0]
    binds, at, st = {}, 0x0100_0000, 0x1000
    for p, t in m.body(fn, "l1").params:
        b = Buffer.of(t)
        if b.space == "stage":
            binds[p] = st
            st += -(-b.nbytes // 256) * 256
        else:
            binds[p] = at
            at += -(-b.nbytes // 4096) * 4096
    progs = build.programs(m, fn, binds, machine)
    return (progs[None] if list(progs) == [None] else progs), machine


def kinds(text: str) -> list:
    prog, machine = programs(_module.read(text, "t.ktpu"))
    return [(f.kind, f.text) for f in verify.check(prog, machine)]


L1 = [
    n
    for n in examples.names()
    if n not in ("silu2", "ring", "remote", "xsplitk", "matmul2", "xpeer")
]


@pytest.mark.parametrize(
    "name",
    [
        "add",
        "softmax",
        "layernorm",
        "silu",
        "matmul",
        "longk",
        "splitk",
        "tonode",
        "push",
        "memops",
    ],
)
def test_examples_verify_clean(name):
    prog, machine = programs(examples.load(examples.example(name)))
    assert verify.check(prog, machine) == []


@pytest.mark.parametrize(
    "name", ["silu2", "ring", "remote", "xsplitk", "matmul2", "xpeer"]
)
def test_multinode_examples_verify_clean(name):
    prog, machine = programs(examples.load(examples.example(name)), nodes=2)
    assert verify.check(prog, machine) == []


def _splitk(old: str, new: str) -> list:
    text = examples.example("splitk").read_text(encoding="utf-8")
    assert old in text
    return kinds(text.replace(old, new))


def test_an_add_ordered_by_a_settle_races():
    # Without the marker's take, only time orders the drain of the sum after
    # the add: no unit counts an accumulator add landing.
    got = _splitk("        fill A0 recv n=1\n", "")
    got = [g for g in got if g[0] == "race"] or got
    assert any(
        k == "race" and t.startswith("acc ") and "DRAIN" in t for k, t in got
    ), got


def test_a_marker_never_taken_is_a_rest_finding():
    got = _splitk("        fill A0 recv n=1\n", "        fill A0 <- x n=1\n")
    assert any(k == "rest" and "entries landed on side A" in t for k, t in got), got


# A cluster streams 16 sub-tiles into vc[0]'s L1 under credits: `open` posts
# the credits, `sink` waits for the granules.
STREAM = """target ktpu.v9

image open()
    vpost mg[0] n={credits}
    halt

image sink()
    {sink}vwait fe rx n={wait}
    halt

fn s.l1(x: mx7a[4, 64] @dram(gm=1, nk=2), w: mx7b[64, 64] @dram(gn=16, nk=2))
    send vc[0]
        run open()
    send mg[0]
        fill A0 <- x n=2
        fill B0 <- w n=32
        gemm 1x16x2 a=A0 b=B0 emit to=vc[0] l1=0 count credit
        drain n=16 to=vc[0] l1=0 fused count credit
    send vc[0]
        run sink()
"""


def stream(credits=16, wait=16, sink="") -> list:
    return kinds(STREAM.format(credits=credits, wait=wait, sink=sink))


def test_the_stream_verifies_clean():
    assert stream() == []


def test_a_stream_short_of_credits_deadlocks():
    got = stream(credits=8)
    assert any(k == "deadlock" and "credits" in t for k, t in got), got


def test_credits_left_over_are_found():
    # 8 a run, over the two runs checked.
    got = stream(credits=24)
    assert ("rest", "16 credits received and never spent") in got, got


def test_granules_never_waited_for_are_found():
    got = stream(wait=8)
    assert ("rest", "16 granules landed and never waited for") in got, got


def test_a_write_unordered_with_a_landing_races():
    # The core writes the words the stream lands on before it waits for them.
    got = stream(sink="desc a0 = 0 walk=(32, 16)\n    vfill a0 -> l1[0]\n    ")
    assert any(k == "race" and "VFILL" in t and "burst" in t for k, t in got), got


def test_waiting_more_than_lands_deadlocks():
    got = stream(wait=24)
    assert any(k == "deadlock" and "VWAIT" in t or "vwait" in t for k, t in got), got


# Both cores peer-drain into mg[0]'s L1 A with nothing ordering the two.
TWO = """target ktpu.v9

image put(at)
    desc a0 = peer(at, count=1) walk=(32, 16)
    vdrain a0 <- l1[0] to=mg[0] buf=0
    halt

fn s.l1(x: mx7a[4, 64] @dram(gm=1, nk=2))
    send vc[0]
        run put(0)
    send vc[1]
        run put(16)
    send mg[0]
        fill A0 recv n=8
"""


def test_two_unordered_senders_into_one_unit_are_found():
    got = kinds(TWO)
    assert any(k == "port" and "interleave" in t for k, t in got), got


def test_the_gate_refuses_a_port_finding_and_warn_runs_it():
    prog, machine = programs(_module.read(TWO, "t.ktpu"))
    with pytest.raises(verify.Refused) as e:
        verify.gate(prog, machine, mode="refuse")
    assert any(f.kind == "port" for f in e.value.findings)
    assert any(f.kind == "port" for f in verify.gate(prog, machine, mode="warn"))
    assert verify.gate(prog, machine, mode="off") == []


def test_the_gate_reads_its_cache_and_the_key_follows_the_program(tmp_path):
    clean = STREAM.format(credits=16, wait=16, sink="")
    prog, machine = programs(_module.read(clean, "t.ktpu"))
    assert verify.gate(prog, machine, mode="refuse", cache=tmp_path) == []
    (entry,) = tmp_path.glob("*.json")
    assert entry.stem == verify.key(prog, machine)
    # A finding planted in the entry is what the next gate decides from.
    entry.write_text('[["port", "mg(0:1,0)", "planted"]]', encoding="utf-8")
    with pytest.raises(verify.Refused, match="planted"):
        verify.gate(prog, machine, mode="refuse", cache=tmp_path)
    other, _ = programs(
        _module.read(STREAM.format(credits=24, wait=16, sink=""), "t.ktpu")
    )
    assert verify.key(other, machine) != entry.stem


def test_the_gate_passes_a_clean_stream():
    text = STREAM.format(credits=16, wait=16, sink="")
    prog, machine = programs(_module.read(text, "t.ktpu"))
    assert verify.gate(prog, machine, mode="refuse") == []


def test_a_rel_transfer_steps_by_its_own_ainc():
    # An earlier image leaves AINC 512 on a0; the next image's `rel` drains
    # step by 0 because the encoder states it.
    text = """target ktpu.v9

image big()
    desc a0 = peer(0, count=1) walk=(32, 16)
    ainc a0 += 512
    halt

image put()
    desc a0 = peer(0, count=1) walk=(32, 16)
    vdrain a0 <- l1[0] rel to=mg[0] buf=0
    vdrain a0 <- l1[0] rel to=mg[0] buf=0
    halt

fn s.l1(x: mx7a[4, 64] @dram(gm=1, nk=2))
    send vc[0]
        run big()
    send vc[0]
        run put()
    send mg[0]
        fill A0 recv n=8
"""
    assert [k for k, _ in kinds(text) if k in ("bounds", "deadlock")] == []
