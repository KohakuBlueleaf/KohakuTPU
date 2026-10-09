"""The vector-core scheduler (`ir.l1.vsched`): its timing model against the
card's measured rates, and every reordering against a symbolic execution.

The symbolic executor below is written from `vec_alu.v`'s operand muxes, not
from `vsched`: an order is legal when every register, predicate, L1 word and
drained run ends up holding the same expression as in the program's own order.
"""

import random

import pytest
from kohakutpu.hw import vector as V
from kohakutpu.ir.l1 import vsched
from kohakutpu.ir.l1.kernels import silu as SI
from kohakutpu.ir.l1.kernels import softmax as SM
from kohakutpu.ir.l1.kernels import stream
from kohakutpu.ir.l1.vector import Alu, Bar, Vdrain, Vfill, Vld, Vshuf, Vst

#: The sources each opcode reads, from vec_alu.v's va/vb/vc muxes.
OPERANDS = {
    **dict.fromkeys(("VMOV", "VNEG", "VABS", "VEXP2", "VLOG2", "VINV", "VRSQRT"), "a"),
    **dict.fromkeys(("VADD", "VSUB"), "ac"),
    **dict.fromkeys(("VMUL", "VMAX", "VMIN", "VCMPLT", "VCMPGT", "VCMPEQ"), "ab"),
    **dict.fromkeys(("VFMA", "VFNMA", "VSEL"), "abc"),
}


def execute(code, walks: dict, fills: dict):
    """Final registers, predicates, L1 words and drained runs, as expressions."""
    reg, pred, l1, drained = {}, {}, {}, []

    def words(inst):
        base, offs = walks[inst.ad]
        return [base + inst.off + o for o in offs]

    for n, inst in enumerate(code):
        if isinstance(inst, Alu):
            fields = {
                "a": (inst.va, inst.sa),
                "b": (inst.vb, inst.sb),
                "c": (inst.vc, inst.sc),
            }
            args = tuple(
                reg.get(r, ("r0", r)) if sel == V.SRC_V else ("const", sel, r)
                for r, sel in (fields[k] for k in OPERANDS[inst.op])
            )
            if inst.pm:
                # A predicated write keeps the lanes it does not write.
                args += (pred.get(inst.pr, ("p0", inst.pr)), inst.pm)
                if not inst.op.startswith("VCMP"):
                    args += (reg.get(inst.vd, ("r0", inst.vd)),)
            if inst.op.startswith("VCMP"):
                pred[inst.pr] = (inst.op, args)
            else:
                reg[inst.vd] = (inst.op, args)
        elif isinstance(inst, Vld):
            reg[inst.vd] = ("vld", tuple(l1.get(w, ("l1", w)) for w in words(inst)))
        elif isinstance(inst, Vst):
            for k, w in enumerate(words(inst)):
                l1[w] = ("vst", reg.get(inst.vs, ("r0", inst.vs)), k)
        elif isinstance(inst, Vshuf):
            out = ("vshuf", reg.get(inst.va, ("r0", inst.va)), inst.srot)
            if inst.pm:
                out += (pred.get(inst.pr, ("p0", inst.pr)), inst.pm)
                out += (reg.get(inst.vd, ("r0", inst.vd)),)
            reg[inst.vd] = out
        elif isinstance(inst, Vfill):
            for k in range(fills[inst.ad]):
                l1[inst.l1 + k] = ("fill", inst.ad, k)
        elif isinstance(inst, Vdrain):
            drained.append(
                (
                    inst.ad,
                    tuple(
                        l1.get(inst.l1 + k, ("l1", inst.l1 + k))
                        for k in range(fills[inst.ad])
                    ),
                )
            )
    return reg, pred, l1, sorted(drained)


def same_program(a, b, l1map) -> bool:
    return execute(a, l1map.walks, l1map.fills) == execute(b, l1map.walks, l1map.fills)


def test_the_timing_model_reproduces_the_measured_primitives():
    """scripts/py/l1/primitives.py on card_v9_1n, VL 128."""
    l1 = vsched.L1Map(walks={0: (0, range(8))})
    n = 64
    per = lambda code: vsched.cycles(code, l1) / n
    assert per([Alu("VADD", vd=1 + i % 12) for i in range(n)]) == pytest.approx(
        11.6, abs=0.4
    )
    assert per(
        [Alu("VADD", vd=1 + (i + 1) % 2, va=1 + i % 2) for i in range(n)]
    ) == pytest.approx(26.0, abs=0.5)
    assert per([Vld(1 + i % 12, 0, (i % 16) * 8) for i in range(n)]) == pytest.approx(
        14.4, abs=0.5
    )
    assert per(
        [Vst(1 + i % 12, 0, 256 + (i % 16) * 8) for i in range(n)]
    ) == pytest.approx(13.4, abs=0.5)
    pairs = [
        x
        for i in range(n // 2)
        for x in (Vld(1 + i % 6, 0, (i % 16) * 8), Alu("VADD", vd=8 + i % 6))
    ]
    assert per(pairs) * 2 == pytest.approx(22.4, abs=1.0)
    assert per([Vshuf(1 + i % 12, 0, 1) for i in range(n)]) == pytest.approx(
        27.0, abs=0.5
    )


def _random_program(rng, n: int) -> list:
    ops = list(OPERANDS)
    code = []
    for _ in range(n):
        kind = rng.random()
        if kind < 0.2:
            code.append(Vld(rng.randrange(16), 0, rng.randrange(8) * 8))
        elif kind < 0.35:
            code.append(Vst(rng.randrange(16), 0, rng.randrange(8) * 8))
        elif kind < 0.4:
            pm = rng.choice((0, 1, 2))
            code.append(
                Vshuf(
                    rng.randrange(16),
                    rng.randrange(16),
                    rng.randrange(4),
                    pr=rng.randrange(1, 4) if pm else 0,
                    pm=pm,
                )
            )
        else:
            sels = [rng.choice((V.SRC_V, V.SRC_V, V.SRC_S, V.SRC_K)) for _ in range(3)]
            code.append(
                Alu(
                    rng.choice(ops),
                    rng.randrange(16),
                    rng.randrange(16),
                    rng.randrange(16),
                    rng.randrange(4) if sels[2] == V.SRC_K else rng.randrange(16),
                    *sels,
                    pr=rng.randrange(4),
                    pm=rng.choice((0, 0, 1)),
                )
            )
    return code


@pytest.mark.parametrize("seed", range(12))
def test_a_schedule_keeps_every_value_of_a_random_program(seed):
    """Dense register and L1 reuse: every hazard kind occurs many times."""
    rng = random.Random(seed)
    l1 = vsched.L1Map(walks={0: (0, range(8))})
    code = _random_program(rng, 80)
    order = vsched.schedule(code, l1)
    assert sorted(map(repr, order)) == sorted(map(repr, code))
    assert same_program(code, order, l1)


def _merge_program(rng, n: int) -> list:
    """Predicated shuffles and ALU ops over four registers: a write that keeps
    its unwritten lanes reads its destination, and here nearly every op does."""
    code = []
    for _ in range(n):
        r = rng.randrange(4)
        if rng.random() < 0.6:
            code.append(
                Vshuf(
                    r, rng.randrange(4), rng.randrange(4), pr=rng.randrange(1, 4), pm=1
                )
            )
        else:
            code.append(
                Alu(
                    "VADD",
                    r,
                    rng.randrange(4),
                    r,
                    rng.randrange(4),
                    pr=1,
                    pm=rng.choice((0, 1)),
                )
            )
    return code


@pytest.mark.parametrize("seed", range(8))
def test_a_schedule_keeps_every_predicated_merge(seed):
    rng = random.Random(100 + seed)
    l1 = vsched.L1Map(walks={0: (0, range(8))})
    code = _merge_program(rng, 40)
    assert same_program(code, vsched.schedule(code, l1), l1)


@pytest.mark.parametrize("group,sets", [(3, 2), (6, 1), (2, 4)])
def test_the_silu_pipeline_body_is_reordered_without_changing_a_value(group, sets):
    dims = ((1, SI.SLICE),)
    l1 = stream.l1_map(dims, 256)
    code = [Bar(), Vdrain(stream.D[1], 256), Vfill(stream.F[0][1], 256)] + SI.body(
        0, 256, group, sets
    )
    order = vsched.schedule(code, l1)
    assert same_program(code, order, l1)
    assert vsched.cycles(order, l1) < vsched.cycles(code, l1)


def test_a_fill_and_every_load_stay_behind_the_barrier():
    """VBAR waits for EVERY fill in flight: the next region's fill issued before
    it would be waited for too, and a load before it reads a region not yet in."""
    dims = ((1, SI.SLICE),)
    l1 = stream.l1_map(dims, 256)
    code = [Bar(), Vdrain(stream.D[1], 256), Vfill(stream.F[0][1], 256)] + SI.body(
        0, 256, 3, 2
    )
    order = vsched.schedule(code, l1)
    bar = next(i for i, x in enumerate(order) if isinstance(x, Bar))
    assert all(i > bar for i, x in enumerate(order) if isinstance(x, (Vfill, Vld)))


def test_the_softmax_body_is_reordered_without_changing_a_value():
    w = 16
    l1 = stream.l1_map(((w, SM.ROWS),), SM.ROWS * w)
    code = SM.steps([0], w, 8)
    assert same_program(code, vsched.schedule(code, l1), l1)


def test_scheduling_never_makes_the_model_slower():
    rng = random.Random(99)
    l1 = vsched.L1Map(walks={0: (0, range(8))})
    for _ in range(6):
        code = _random_program(rng, 60)
        assert vsched.cycles(vsched.schedule(code, l1), l1) <= vsched.cycles(code, l1)
