"""Hand-written L1 elementwise binary op (`a + b`, `a - b`, `a * b`) on every
vector core: the residual add and the gate multiply.

Two input streams (`stream` with ``inputs=2``), the result over input 0's slot.
A slice is two loads, one ALU op and a store, so the op is load/store bound in
the core and memory bound across the card: the floor is the fills and drains.
"""

from kohakutpu.hw import vector as V
from kohakutpu.ir.l1.kernels import stream
from kohakutpu.ir.l1.vector import Alu, Seti, Setmode, Setvl, Vld, Vst

S_VL = 0
SLICE = 8  # words in one VL=128 slice
#: Opcode and which field holds the second operand (vec_alu.v's muxes).
OPS = {"add": ("VADD", "vc"), "sub": ("VSUB", "vc"), "mul": ("VMUL", "vb")}


def head() -> list:
    return [Seti(S_VL, V.VLMAX), Setvl(S_VL), Setmode(V.FLAT)]


def body(slots, words: int, op: str, group: int, sets: int) -> list:
    """One region: ``slots[0] = slots[0] op slots[1]``, `group` slices a step,
    consecutive groups on rotating register sets."""
    if 2 * group * sets > 16:
        raise ValueError(
            f"{sets} sets of {group} slices need {2 * group * sets} registers"
        )
    name, field = OPS[op]
    a0, b0 = slots
    out = []
    for g, s0 in enumerate(range(0, words // SLICE, group)):
        sl = list(range(s0, min(s0 + group, words // SLICE)))
        base = (g % sets) * 2 * group
        x = {s: base + 2 * k for k, s in enumerate(sl)}
        y = {s: base + 2 * k + 1 for k, s in enumerate(sl)}
        out += [Vld(x[s], stream.AD_L1, a0 + s * SLICE) for s in sl]
        out += [Vld(y[s], stream.AD_L1, b0 + s * SLICE) for s in sl]
        out += [
            Alu(name, vd=x[s], va=x[s], **{"vb": x[s], "vc": x[s], field: y[s]})
            for s in sl
        ]
        out += [Vst(x[s], stream.AD_L1, a0 + s * SLICE) for s in sl]
    return out


def binary(prog, op, a, b, dst, n: int, words=128, group=4, sets=2, cores=0) -> int:
    """Queue ``dst = a op b`` over `n` fp16 elements; returns the image words."""
    units = prog.units("VC")[: cores or None]
    per = n // len(units)
    batch = words * 16
    if n % len(units) or per % batch:
        raise ValueError(f"{per} elements a core is not whole {batch}-element RUNs")
    dims = ((1, SLICE),)
    progs = stream.programs(
        head(), lambda slots: body(slots, words, op, group, sets), words, dims, inputs=2
    )
    size = 0
    for c, core in enumerate(units):
        off = c * per * 2
        size = stream.send(
            prog,
            core,
            progs,
            dims,
            words,
            (a + off, b + off),
            dst + off,
            per // batch,
            batch * 2,
        )
    prog.barrier()
    return size
