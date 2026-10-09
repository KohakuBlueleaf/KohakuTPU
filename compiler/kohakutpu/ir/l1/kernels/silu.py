"""Hand-written L1 silu, ``x / (1 + 2**(-x*log2e))``, on every vector core.

The array is split across the cores, each running the streaming pipeline
(`stream`). Inside a region, `group` VL=128 slices run op-major on disjoint
registers -- loads, each ALU op across the group, stores -- because a load or
store does not overlap the lanes and a dependent op waits ~26 cycles.
"""

from kohakutpu.hw import vector as V
from kohakutpu.ir.l1.kernels import stream
from kohakutpu.ir.l1.vector import Alu, Seti, Setmode, Setvl, Vld, Vst

LOG2E = 1.4426950408889634
S_VL, S_NLOG2E = 0, 4
K_ONE = 1
SLICE = 8  # words in one VL=128 slice
#: ALU ops a slice takes, for a lane-efficiency figure.
ALU_OPS = 5


def head() -> list:
    return [
        Seti(S_VL, V.VLMAX),
        Seti(S_NLOG2E, V.e8m15(-LOG2E)),
        Setvl(S_VL),
        Setmode(V.FLAT),
    ]


def body(region: int, words: int, group: int, sets: int = 1) -> list:
    """One region's silu in place, `group` slices at a time, op-major.

    Consecutive groups rotate over `sets` register sets, so a group's loads and
    stores carry no false dependence on its neighbours' and `vsched` can overlap
    them with the ALU work; every unused ALU field names `vd`, which the issue
    hazard checks anyway.
    """
    if 2 * group * sets > 16:
        raise ValueError(
            f"{sets} sets of {group} slices need {2 * group * sets} registers"
        )
    out = []
    for g, s0 in enumerate(range(0, words // SLICE, group)):
        sl = list(range(s0, min(s0 + group, words // SLICE)))
        base = (g % sets) * 2 * group
        x = {s: base + 2 * k for k, s in enumerate(sl)}
        t = {s: base + 2 * k + 1 for k, s in enumerate(sl)}
        out += [Vld(x[s], stream.AD_L1, region + s * SLICE) for s in sl]
        out += [
            Alu("VMUL", vd=t[s], va=x[s], vb=S_NLOG2E, sb=V.SRC_S, vc=t[s]) for s in sl
        ]
        out += [Alu("VEXP2", vd=t[s], va=t[s], vb=t[s], vc=t[s]) for s in sl]
        out += [
            Alu("VADD", vd=t[s], va=t[s], vb=t[s], vc=K_ONE, sc=V.SRC_K) for s in sl
        ]
        out += [Alu("VINV", vd=t[s], va=t[s], vb=t[s], vc=t[s]) for s in sl]
        out += [Alu("VMUL", vd=t[s], va=x[s], vb=t[s], vc=t[s]) for s in sl]
        out += [Vst(t[s], stream.AD_L1, region + s * SLICE) for s in sl]
    return out


def silu(prog, src: int, dst: int, n: int, words=256, group=3, sets=2, cores=0) -> int:
    """Queue silu over `n` fp16 elements; returns the image words."""
    units = prog.units("VC")[: cores or None]
    per = n // len(units)
    batch = words * 16
    if n % len(units) or per % batch:
        raise ValueError(f"{per} elements a core is not whole {batch}-element RUNs")
    dims = ((1, SLICE),)
    progs = stream.programs(
        head(), lambda region: body(region, words, group, sets), words, dims
    )
    size = 0
    for c, core in enumerate(units):
        size = stream.send(
            prog,
            core,
            progs,
            dims,
            words,
            src + c * per * 2,
            dst + c * per * 2,
            per // batch,
            batch * 2,
        )
    prog.barrier()
    return size
