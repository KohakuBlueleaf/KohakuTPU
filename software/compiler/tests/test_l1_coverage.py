"""L0 v2 steps and the L1 statements that reach every hardware feature. A
field a register sets is witnessed against the same field encoded statically:
the PATCHed word must equal the word the encoder makes with that value."""

import struct

import pytest

from kohakuaccel.compiler.package import engine
from kohakuaccel.compiler.package import mover as PM
from kohakuaccel.compiler.package.build import PackageBuilder
from kohakuaccel.compiler.package.format import (
    F_CONSUME,
    F_CTRL,
    F_IMM,
    F_MEM,
    FLAG_RX,
    REGS,
    Alu,
    Cond,
    Op,
    Package,
    PackageError,
)
from kohakuaccel.compiler.program import TMP
from kohakuaccel.simulation.package import Interpreter
from kohakutpu.compiler import build
from kohakutpu.compiler import target as T
from kohakutpu.compiler.emit import image as vector
from kohakutpu.compiler.encode import cluster as C
from kohakutpu.compiler.encode import vector as A
from kohakutpu.compiler.isa.cluster import ISA
from kohakutpu.language.text import module
from kohakutpu.language.text.syntax import TextError

MACHINE = T.machine()
MASK64 = (1 << 64) - 1


class Box:
    """A mailbox that records sends and completes each word at once."""

    def __init__(self) -> None:
        self.sent, self.done = [], []

    def send(self, x, y, payload):
        self.sent.append((x, y, payload))
        self.done.append((x, y, 0, 0))

    def drain(self):
        out, self.done = self.done, []
        return out


class Memory(dict):
    def read(self, addr, n):
        return b"".join(
            struct.pack("<Q", self.get(addr + k, 0)) for k in range(0, n, 8)
        )

    def write(self, addr, v):
        self[addr] = v


def run(pkg, memory=None, mover=None):
    mem = memory if memory is not None else Memory()
    box = Box()
    interp = Interpreter(
        box,
        memory=mem.read,
        store=mem.write,
        ctrl=lambda off: 0x5A00 | off,
        mover=mover or (lambda writes: None),
    )
    return interp.run(pkg), box, mem


def text(body: str, params="a: f16[64] @dram, b: f16[64] @dram"):
    return module.read(f"fn k.l1({params})\n" + body)


def prog(body: str, binds=None, **kw):
    return build.program(text(body, **kw), "k", binds or {"a": 0x1000, "b": 0x8000})


# ------------------------------------------------------------- the wire form
def test_new_steps_survive_the_wire_and_set_the_rx_flag():
    b = PackageBuilder(types={(1, 0): "MG"})
    u = b.unit((1, 0))
    b.ring(1, slot=3, amount=5, fence=True)
    b.wait_slot(3, 5, consume=True)
    b.store([(0x2000, 7), (0x2008, 1)], from_regs=True)
    b.ctrl([(0x1D0, 0x0102)])
    b.load(2, 0x3000, index=4)
    b.load(3, 0xE0, ctrl=True)
    b.alu(1, Alu.ADD, 2, 9)
    top = len(b.steps)
    b.patch(1, 0, 4, 34)
    b.jump(top, Cond.LTU, 1, 100)
    b.send(u, 7, [5 << 240])
    b.push(u, 0xAB << 248, 0x4000, 3, ack=True)
    b.recv(0x5000, 2, u)
    pkg = b.build()
    raw = pkg.to_bytes()
    back = Package.from_bytes(raw)
    assert back.steps == pkg.steps and back.payloads == pkg.payloads
    assert struct.unpack_from("<Q", raw, 16)[0] >> 32 & FLAG_RX
    ops = [s.op for s in back.steps]
    assert Op.RECV in ops and Op.JUMP in ops and Op.PATCH in ops
    ring = back.steps[0]
    assert engine.ring_value(ring) == 1 | 3 << 8 | 5 << 16 | 1 << 32
    assert back.steps[1].flags & F_CONSUME
    assert back.steps[5].flags & F_CTRL
    push = next(s for s in back.steps if s.op == Op.SEND and s.flags & F_MEM)
    assert back.payloads[(push.arg & 0xFFFF_FFFF) + 1] & MASK64 == 0x4000


@pytest.mark.parametrize(
    "make, said",
    [
        (lambda b: b.wait_slot(16, 1), "slot 16"),
        (lambda b: b.ring(0, 0, amount=0), "amount 0"),
        (lambda b: b.alu(REGS, Alu.ADD), "alu"),
    ],
)
def test_the_builder_refuses_what_the_node_would(make, said):
    with pytest.raises(PackageError, match=said):
        make(PackageBuilder())


def test_a_jump_past_the_end_is_refused():
    b = PackageBuilder()
    b.jump(5)
    with pytest.raises(PackageError, match="jumps to 5"):
        b.build().to_bytes()


def test_engine_lowering_keeps_a_package_that_jumps_or_patches():
    b = PackageBuilder(types={(1, 0): "MG"}, credit=8)
    u = b.unit((1, 0))
    b.dispatch(u, [1, 2])
    b.alu(0, Alu.MOV, 0, 3)
    b.jump(0, Cond.EQ, 0, 0)
    pkg = b.build()
    assert engine.lower(pkg) is pkg
    b2 = PackageBuilder(types={(1, 0): "MG"}, credit=8)
    b2.dispatch(b2.unit((1, 0)), [1, 2])
    b2.ring(2, 1)
    low = engine.lower(b2.build())
    entries = [
        e for s in low.steps if s.op == Op.ENGINE for e in engine.entries(low, s)
    ]
    assert (engine.il_code(engine.IL_RING), 2 | 1 << 8 | 1 << 16 | 1 << 32) in entries


# ---------------------------------------------------- reference semantics
def test_registers_loop_and_patch_a_payload():
    """r0 counts 0..3; each pass PATCHes payload 0's field [40, 56) with
    r0 * 16 + 5 and sends it: four words, each the field's value that pass."""
    b = PackageBuilder(types={(1, 0): "MG"}, credit=8)
    u = b.unit((1, 0))
    top = len(b.steps)
    b.alu(1, Alu.MUL, 0, 16)
    b.alu(1, Alu.ADD, 1, 5)
    b.patch(1, 0, 40, 16)
    b.dispatch(u, [0xFF])
    b.alu(0, Alu.ADD, 0, 1)
    b.jump(top, Cond.LTU, 0, 4)
    res, box, _ = run(b.build())
    assert res.status == 0
    assert [w for _, _, w in box.sent] == [0xFF | (k * 16 + 5) << 40 for k in range(4)]
    assert res.regs[0] == 4


def test_load_store_ctrl_and_signed_compare():
    mem = Memory({0x3000 + 8 * 4: 0xFFFF_FFFF_FFFF_FFFE})
    b = PackageBuilder()
    b.alu(4, Alu.MOV, 0, 4)
    b.load(2, 0x3000 + 8 * 4 - 32, index=5)  # r5 = 0: plain
    b.alu(5, Alu.MOV, 0, 32)
    b.load(3, 0x3000, index=5)  # 0x3000 + 32
    b.load(6, 0xE0, ctrl=True)
    over = b.jump(0, Cond.GE, 3, 0)  # -2 >= 0 signed: not taken
    b.store([(0x6000, 3), (0x6008, 6)], from_regs=True)
    b.retarget(over, len(b.steps))
    res, _, mem = run(b.build(), mem)
    assert res.status == 0
    assert mem[0x6000] == 0xFFFF_FFFF_FFFF_FFFE and mem[0x6008] == 0x5A00 | 0xE0


def test_patch_a_step_count():
    b = PackageBuilder()
    b.alu(0, Alu.MOV, 0, 9)
    b.patch(0, 2, 32, 32, steps=True)
    b.settle(1)
    res, _, _ = run(b.build())
    assert res.events[0][3] == 9


# ------------------------------------------------------------ L1 text
def test_registers_labels_and_a_patched_cluster_address():
    p = prog("""
    r1 = set 0
    label top
    r2 = mul r1, 4096
    send mg[0]
        fill A0 <- a + r2 n=8
    r1 = add r1, 1
    goto top if r1 < 3
    wait mg[0]
""")
    pkg, _ = build.package(p)
    ops = [s.op for s in pkg.steps]
    assert ops.count(Op.PATCH) == 2  # the address's low 34 bits and high 6
    assert Op.ENGINE not in ops  # it jumps: the node queues it as it runs
    jump = next(s for s in pkg.steps if s.op == Op.JUMP)
    assert pkg.steps[jump.unit].op == Op.ALU  # the label: the first step after it
    res, box, _ = run(pkg)
    assert res.status == 0
    want = [C.Fill(0x1000 + k * 4096, 8).flits()[0] for k in range(3)]
    assert [w for _, _, w in box.sent] == want


def test_register_count_in_a_fill():
    p = prog(
        "    r3 = set 17\n    send mg[0]\n        fill B1 <- a n=r3\n    wait mg[0]\n"
    )
    res, box, _ = run(build.package(p)[0])
    assert res.status == 0
    assert [w for _, _, w in box.sent] == C.Fill(0x1000, 17, sel=1, fbank=1).flits()


def test_a_vector_descriptor_base_from_a_register():
    p = prog(
        "    r0 = set 0x40\n"
        "    send vc[0]\n        desc a1 = b + r0 walk=(32, 4)\n    wait vc[0]\n"
    )
    res, box, _ = run(build.package(p)[0])
    assert res.status == 0
    want = [A.desc_flit(1, 0, 0x8040)] + [
        A.desc_flit(1, f, v) for f, v in ((1, A.dim(32, 4)), (2, 0), (3, 0), (4, 0))
    ]
    # A package carries the 256-bit payload; the flit header is the mailbox's.
    assert [w for _, _, w in box.sent] == [w & ((1 << 256) - 1) for w in want]


def test_node_statements_reach_their_steps():
    p = prog("""
    ring mesh=1 slot=2 amount=3
    ring mesh=1 plain
    wait slot 2 n=3 consume
    wait bell 1 n=2
    store b <- 7
    r4 = ctrl sig
    store b + 8 <- r4
    ctrl xf_sel <- 0x0104
    signal 1 2
    settle 50
    push vc[1] buf=0 at=16 <- a n=4 wait
    recv n=2 -> b from=mg[1]
    query mg[0] index=2 -> b + 64
""")
    pkg = p.build().build()
    by = pkg.steps
    rings = [s for s in by if s.op == Op.RING]
    assert [(s.unit, s.count, s.arg) for s in rings] == [(1, 2, 3 | 1 << 16), (1, 0, 1)]
    (ws,) = [s for s in by if s.op == Op.WAIT_SLOT]
    assert (ws.unit, ws.count, ws.flags) == (2, 3, F_CONSUME)
    (wb,) = [s for s in by if s.op == Op.WAIT_BELL]
    assert (wb.unit, wb.count) == (1, 2)
    sends = [s for s in by if s.op == Op.SEND]
    push, query = sends
    desc = pkg.payloads[push.arg & 0xFFFF_FFFF]
    assert push.count == 4 and push.flags & F_MEM
    assert (desc >> 248, desc >> 232 & 0xFFFF, desc >> 224 & 0xFF, desc >> 216 & 1) == (
        0,
        16,
        3,
        1,
    )
    assert query.arg >> 32 == 7 and pkg.payloads[query.arg & 0xFFFF_FFFF] >> 240 == 2
    recvs = [s for s in by if s.op == Op.RECV]
    assert [s.count for s in recvs] == [2, 1]
    (ctl,) = [s for s in by if s.op == Op.CTRL]
    assert pkg.payloads[ctl.arg] & ((1 << 128) - 1) == 0x1D0 | 0x0104 << 64


def test_drain_into_l1b_the_accumulator_and_another_mesh():
    m2 = T.machine(nodes=2)
    p = build.programs(
        text("""
    node 0
        send mg[0]
            drain n=8 to=mg[1] buf=1 l1=4 signal
            drain n=8 to=mg[2] buf=2
            drain n=8 to=mesh1.mg[3] buf=2
            drain a n=16 mesh=1
"""),
        "k",
        {"a": 0x1000, "b": 0x8000},
        m2,
    )[0]
    (send,) = [s for s in p.steps if s[0] == "send"]
    l1b, acc, far, mem = send[3]
    mg = sorted(T.MG)
    assert (l1b.dst, l1b.buf, l1b.addr, l1b.dflags) == (mg[1], 1, 128, 1)
    assert (acc.dst, acc.buf) == (mg[2], C.BUF_ACC)
    assert (far.dst, far.mesh, far.fin, far.ack) == (
        T.SYS_PORTS[0],
        1,
        mg[3],
        T.SYS_REPLY,
    )
    word = far.flits()[0]
    got = ISA.DRAIN.decode(word)
    assert (got["dmesh"], got["dfin"], got["dbuf"]) == (1, mg[3][1] << 4 | mg[3][0], 2)
    assert (mem.mesh, mem.fin, mem.dst) == (1, T.SYS_PORTS[0], None)


def test_node_blocks_are_one_program_per_node():
    ps = build.programs(
        text("""
    node 0
        send mg[0]
            fill A0 <- a n=4
        ring mesh=1 slot=0
    node 1
        wait slot 0 n=1 consume
        send mg[0]
            fill A0 <- b n=4
"""),
        "k",
        {"a": 0x1000, "b": 0x8000},
        T.machine(nodes=2),
    )
    assert sorted(ps) == [0, 1]
    pkg1, _ = build.package(ps[1])
    assert {u.mesh for u in pkg1.units} == {1}


def test_fill_peers_and_gemm_anchor():
    p = prog(
        "    send mg[0]\n        fill A0 <- a n=8 peers=(mg[1], mg[2])\n"
        "        gemm 8x8x2 a=A0 b=B0 anchor=38\n"
    )
    fill, gemm = next(s for s in p.steps if s[0] == "send")[3]
    f = ISA.FILL.decode(fill.flits()[0])
    mg = sorted(T.MG)
    assert f["npeer"] == 2
    assert f["peers"] == C.coord_byte(mg[1]) | C.coord_byte(mg[2]) << 8
    assert ISA.GEMM.decode(gemm.flits()[0])["anchor"] == 38


# ------------------------------------------------------------- the mover
@pytest.mark.parametrize(
    "line, want",
    [
        (
            "copy w(a, (4, 256), (8, 32)) -> w(b, (8, 32), (4, 256)) burst=2",
            PM.move(
                PM.COPY,
                (0x1000, [(4, 256), (8, 32)]),
                (0x8000, [(8, 32), (4, 256)]),
                flags=PM.flag_byte(True, 2),
            ),
        ),
        (
            "copy w(a, (6, 32, 1, 1), win1=(-1, 4)) -> w(b, (6, 32)) pad=0x3c00 ew=16",
            PM.move(
                PM.COPY,
                PM.Walk(0x1000, ((6, 32, 1, 1),), ((-1, 4), (0, 0))),
                (0x8000, [(6, 32)]),
                ewidth=PM.W16,
                imm=0x3C00,
            ),
        ),
        (
            "fill w(b, (16, 32)) value=0x3c003c00 ew=16",
            PM.fill((0x8000, [(16, 32)]), 0x3C003C00, PM.W16),
        ),
        ("gen w(b, (16, 32)) seed=99", PM.generate((0x8000, [(16, 32)]), 99)),
        (
            "gather a -> w(b, (12, 32)) index=b + 4K n=3 pitch=512 words=4 fp",
            PM.gather(
                0x1000, (0x8000 + 4096, 3), (0x8000, [(12, 32)]), (512, 4), fp=True
            ),
        ),
        (
            "scatter w(a, (12, 32)) -> b index=a + 8K n=3 pitch=256 words=4",
            PM.scatter((0x1000, [(12, 32)]), 0x8000, (0x1000 + 8192, 3), (256, 4)),
        ),
        (
            "quant w(a, (16, 32)) -> w(b, (2, 128)) bpack",
            PM.convert_walk((0x1000, [(16, 32)]), (0x8000, [(2, 128)]), 1, 1),
        ),
        (
            "xform w(a, (8, 32)) -> w(b, (8, 32)) id=0",
            PM.convert_walk((0x1000, [(8, 32)]), (0x8000, [(8, 32)]), 0, 0),
        ),
    ],
)
def test_mover_ops_are_the_framework_moves(line, want):
    p = prog(f"    move\n        {line}\n")
    (mv,) = [s for s in p.steps if s[0] == "move"]
    assert mv[1] == want


def test_a_mover_count_from_a_register_is_patched():
    p = prog(
        "    r2 = set 5\n    move\n        gather a -> w(b, (20, 32)) index=b n=r2 pitch=128 words=4\n"
    )
    pkg, _ = build.package(p)
    seen = []
    res, _, _ = run(pkg, mover=seen.append)
    assert res.status == 0
    (writes,) = seen
    (idx,) = [v for r, v in writes if r == PM.R_INDEX]
    assert idx == 0x8000 | 5 << 40


# --------------------------------------------------------------- the vector
def test_explicit_config_lines_and_what_the_encoder_then_skips():
    m = module.read(
        "image k()\n"
        "    chkvl vl=64 a=(0, 1) b=(2, 0)\n"
        "    xb mode=xor k=4\n"
        "    vmul v1, v2[0:], xor(v3[2], 4) vl=64\n"
        "    ustr 2 8\n"
        "    vunpk v4 <- l1[0] f16 walk=(2, 8)\n"
        "    halt\n"
    )
    words, _ = vector.image(m, "k", ())
    head = [A.chkvl(64, (0, 1), (2, 0)), A.xb(A.XM_XOR, 4)]
    assert words[:2] == head
    # The math word follows at once: the core already holds its config.
    assert words[2] == A.math("vmul", 1, 2, 3, om=True)
    assert words[3] == A.cfg_stride(A.CFG_USTR, 2, 8)
    assert words[4] == A.vunpk(4, 0, 8, A.F16)


def test_a_peer_drain_descriptor_names_ack_mesh_and_fin():
    got = vector.desc(
        lambda st, msg: (_ for _ in ()).throw(AssertionError(msg)),
        module.read(
            "fn k.l1(a: f16[4] @dram)\n    desc a3 = peer(5, ack=(0, 1), mesh=2, fin=(1, 2)) walk=(32, 8)\n"
        )
        .body("k", "l1")
        .stmts[0],
    )
    assert got[(3, 0)] == 5 | 0x10 << 16 | 2 << 24 | 0x21 << 26


def test_images_and_descriptors_name_units_on_the_target():
    """`to=mg[1]`, `ack=mg[0]`, `fin=mesh1.vc[0]` encode as the coordinates
    the machine gives those units (and fin= implies mesh=1)."""
    form = (
        "image k()\n"
        "    vdrain a3 <- l1[0] to={to} buf=1 signal\n"
        "    halt\n"
        "fn k.l1(a: f16[64] @dram, b: f16[64] @dram)\n"
        "    node 0\n"
        "        send vc[0]\n"
        "            desc a3 = peer(5, ack={ack}, {far}) walk=(32, 8)\n"
        "            run k()\n"
    )
    mg0, mg1 = sorted(MACHINE.coords("MG"))[:2]
    vc0 = min(MACHINE.coords("VC"))
    names = form.format(to="mg[1]", ack="mg[0]", far="fin=mesh1.vc[0]")
    coords = form.format(to=mg1, ack=mg0, far=f"mesh=1, fin={vc0}")
    machine = T.machine(nodes=2)
    binds = {"a": 0x1000, "b": 0x8000}
    got = [
        build.package(build.programs(module.read(t), "k", binds, machine)[0])[0]
        for t in (names, coords)
    ]
    assert got[0].to_bytes() == got[1].to_bytes()


def test_an_image_drain_names_only_this_mesh():
    t = (
        "image k()\n    vdrain a3 <- l1[0] to=mesh1.mg[0]\n    halt\n"
        "fn k.l1(a: f16[64] @dram)\n    node 0\n        send vc[0]\n            run k()\n"
    )
    with pytest.raises(TextError, match="memory port"):
        build.programs(module.read(t), "k", {"a": 0x1000}, T.machine(nodes=2))


@pytest.mark.parametrize(
    "body, said",
    [
        ("    goto nowhere\n", "no such label"),
        ("    r15 = set 1\n", "r0..r14"),
        ("    r1 = add r1, r2 * 4\n", "ALU"),
        (
            "    label x\n    t = move posted\n        copy a -> b bytes=32\n    wait moves t\n",
            "past a label",
        ),
    ],
)
def test_l1_refusals(body, said):
    with pytest.raises((TextError, PackageError, ValueError), match=said):
        p = prog(body)
        build.package(p)


def test_a_node_sends_only_into_its_own_mesh():
    body = "    node 0\n        send mesh1.mg[0]\n            fill A0 <- a n=1\n"
    with pytest.raises(TextError, match="its own"):
        build.programs(text(body), "k", {"a": 0x1000, "b": 0x8000}, T.machine(nodes=2))


def test_tmp_register_is_the_last():
    assert TMP == REGS - 1


def test_jump_imm_flag():
    b = PackageBuilder()
    b.jump(0, Cond.EQ, 1, 7)
    assert b.steps[0].flags & F_IMM and b.steps[0].arg == 7
