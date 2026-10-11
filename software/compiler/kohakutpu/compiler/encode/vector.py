"""V2 vector-core machine code, packed through the core's word formats
(`isa.core`), and the CU instructions that carry it to a core (IMEM / DESC /
RUN, laid out by `isa.vector.ISA`).

A math word names registers 0..31; its operand modes come from the config the
last `VCFG` set, applied only when its `om` bit is set. With `om` clear an op
reads every operand at chunk i on beat i, unswizzled and unpredicated.

A value too wide for its field raises `ISAError`: it would shift every field
below it and still decode as some other instruction.
"""

import struct

from kohakuaccel.compiler.isa import ISAError
from kohakutpu.compiler.isa import core as W
from kohakutpu.compiler.isa.core import (
    CFG_AINC,
    CFG_CHK,
    CFG_CHKVL,
    CFG_GETSTK,
    CFG_PINC,
    CFG_PPTR,
    CFG_PSTR,
    CFG_UINC,
    CFG_UPTR,
    CFG_USTR,
    CFG_XB,
    OPS,
    VBAR,
    VCFG,
    VDRAIN,
    VFILL,
    VHALT,
    VLOOP,
    VPACK,
    VSETI,
    VSETVL,
    VSKIPZ,
    VSYNC,
    VUNPK,
    W_MK,
)
from kohakutpu.compiler.isa.vector import ISA as VEC

FLIT_BITS = 288
CU_INST = 0x5
CU_TXN = 0x40

SRC_V, SRC_S, SRC_K = 0, 1, 3

#: VUNPK / VPACK modes.
F16, GT4, F32, MX7 = 0, 1, 2, 3

#: Crossbar modes (VCFG XB).
XM_NONE, XM_XOR, XM_ROT, XM_BCAST, XM_MERGE = 0, 1, 2, 3, 4

#: VSYNC waiters. W_A is the fill queue (VFILL), W_D the drain queue
#: (VDRAIN); W_MK records a mark (`vmark`).
W_FE, W_U, W_M, W_P, W_A, W_D = 0, 1, 2, 3, 4, 6
#: The kinds a waiter waits on. K_UNPACK / K_PACK are queue 0 of their
#: engine, K_UNPACK1 / K_PACK1 queue 1. A core built with DUALQ=0 runs queue
#: 1's instructions on queue 0 and counts K_UNPACK1 / K_PACK1 as K_UNPACK /
#: K_PACK.
K_FILL, K_UNPACK, K_MATH, K_PACK, K_DRAIN, K_UNPACK1, K_PACK1 = 0, 1, 2, 3, 4, 5, 6

REGS = 32
CHUNKS = 8
LANES = 16
VLMAX = 128
#: Instruction memory words; a RUN starts below 1 << VEC.cfg.addr_bits.
IMEM_WORDS = 1 << (VEC.cfg.addr_bits + VEC.cfg.addr_hi_bits)

#: Fault codes, so a SIG_FAULT reads as a reason.
FAULTS = {
    1: "F_DTYPE: a dtype is not FP16 or FP32",
    2: "F_VSRC: a chained instruction named a vector register source",
    3: "F_CHAIN: source C used outside D2/D4/TREE",
    4: "F_OPCODE: unknown opcode",
    5: "F_LEN: a VFILL/VDRAIN walk is longer than 256 entries",
    6: "F_LOOP: VLOOP nested more than one deep",
    7: "F_VL: VSETVL asked for 0 or more than VLMAX",
    8: "F_REDVL: a reduction with VL not a multiple of 16",
    9: "F_CUDATA: an inbound CU_DATA burst named a buffer or an L1 range this "
    "core does not have, or two senders' bursts interleaved",
    10: "F_GT4: a GT4 unpack/pack whose chunk count is not a multiple of 4",
    11: "F_SRC: operand selector 2 (v1's chain source) does not exist in V2",
    12: "F_MODE: unpack mode 3, or an MX7 pack not of 8 chunks at chunk base 0/1",
    13: "F_SYNC: VSYNC waiter or kind out of range",
}

_BY_CODE = {code: op for op, code in OPS.items()}


# ----------------------------------------------------------------- words
def math(op, vd, va=0, vb=0, vc=0, sa=SRC_V, sb=SRC_V, sc=SRC_V, om=False):
    """One math word; `op` a mnemonic or its opcode. S/K operands name
    S[reg & 15] / K[reg & 3]."""
    name = op if isinstance(op, str) else _BY_CODE[op]
    return W.MATHS[name].encode(
        vd=vd, va=va, vb=vb, vc=vc, sa=sa, sb=sb, sc=sc, om=int(om)
    )


def vcfg(sel, payload):
    """A VCFG word with its 23-bit payload raw."""
    return W.CFG.encode(sel=sel, payload=payload)


def _chunk_fields(a, b, c, d) -> dict:
    out = {}
    for x, (base, stride) in zip("abcd", (a, b, c, d), strict=True):
        out[f"{x}_base"], out[f"{x}_stride"] = base, stride
    return out


def chk(a=(0, 1), b=(0, 1), c=(0, 1), d=(0, 1)):
    """Chunk addressing: each operand's (base chunk, stride 0|1); d is vd."""
    return W.CHK.encode(**_chunk_fields(a, b, c, d))


def chkvl(vl, a=(0, 1), b=(0, 1), c=(0, 1), d=(0, 1)):
    """VL (a literal, 1..128) and chunk addressing in one word."""
    if not 1 <= vl <= VLMAX:
        raise ISAError(f"vl = {vl} is not 1..{VLMAX}")
    return W.CHKVL.encode(vl1=vl - 1, **_chunk_fields(a, b, c, d))


def xb(xm=XM_NONE, xk=0, sh=0, xc=False, pm=0, pr=0):
    """Crossbar on b (and c with `xc`), and the write predicate of om ops.

    xor/rot: lane l reads lane l^xk / (l+xk)%16. bcast: beat i reads lane
    (xk + (i >> sh)) % 16 into every lane. merge: pair-merge on bit xk.
    pm 0 writes all lanes, 1 those P[pr] sets, 2 the rest; a compare writes
    P[pr].
    """
    return W.XB.encode(xm=xm, xk=xk, sh=sh, xc=int(xc), pm=pm, pr=pr)


def cfg_ptr(sel, value):
    """UPTR / UINC / PPTR / PINC = `value`."""
    return W.POINTERS[sel].encode(ptr=value)


def cfg_stride(sel, s0=1, s1=4):
    """USTR / PSTR: walk word w is at base + (w % 4) * s0 + (w // 4) * s1;
    a stride is 9 bits, so a negative one wraps."""
    return W.STRIDES[sel].encode(s0=s0 % 512, s1=s1 % 512)


def ainc(ad, inc):
    """Descriptor `ad`'s DMA increment, bytes: each `rel` VFILL/VDRAIN on it
    walks from base + offset, then offset += inc. Offsets restart at RUN."""
    return W.AINC.encode(ad=ad, inc=inc)


def getstk(sreg):
    """S[sreg] = 1.0 if any EXP2D overflowed since the last read, else 0; clears."""
    return W.GETSTK.encode(sreg=sreg)


def _walk(fmt, reg, off, n, mode, cb, rel, q):
    if mode == GT4 and n % 4:
        raise ISAError(f"GT4 walks whole groups of 4 chunks, not {n}")
    return fmt.encode(reg=reg, off=off, n1=n - 1, mode=mode, cb=cb, rel=int(rel), q=q)


def vunpk(vd, off, n=CHUNKS, mode=F16, cb=0, rel=False, q=0):
    """vd chunks cb.. = L1 words off + i*USTR (UPTR + off when `rel`), through
    unpack queue `q`."""
    return _walk(W.UNPK, vd, off, n, mode, cb, rel, q)


def vpack(vs, off, n=CHUNKS, mode=F16, cb=0, rel=False, q=0):
    """L1 words off + i*PSTR (PPTR + off when `rel`) = vs chunks cb.., through
    pack queue `q`."""
    return _walk(W.PACK, vs, off, n, mode, cb, rel, q)


def vpack_mx7(vs, off, b_layout=False, rel=False, q=0):
    """One MXFP7 L1 entry from all of vs: words off + w*s0 (w = 0..3) = the
    quantiser's entry for lane r = chunks 2r, 2r+1 (k = (chunk & 1)*16 + vector
    lane), A packing, or B packing with `b_layout`."""
    return _walk(W.PACK, vs, off, CHUNKS, MX7, 1 if b_layout else 0, rel, q)


def vsync(waiter, on=0, slack=0, mark=None, q=0):
    """`waiter` waits until every `on` instruction dispatched before, except
    the youngest `slack` of them, has finished. With `mark`, it waits instead
    for what that mark recorded (see `vmark`). A W_U / W_P waiter is queue `q`
    of that engine."""
    marked = {} if mark is None else {"marked": 1, "mark": mark}
    return W.SYNC.encode(waiter=waiter, on=on, slack=slack, q=q, **marked)


def vmark(mark, on, slack=0):
    """Mark `mark` = every `on` instruction dispatched so far (but the youngest
    `slack`); a later VSYNC with this mark waits for exactly those."""
    return W.MARK.encode(mark=mark, on=on, slack=slack)


def vskipz(sreg, n):
    """Skip the next `n` words when S[sreg] is zero."""
    return W.SKIPZ.encode(sreg=sreg, skip=n)


def vsetvl(sreg):
    return W.SETVL.encode(sreg=sreg)


def vseti(sreg, to_k=False):
    """The first of two words; the 24-bit immediate is the second."""
    return W.SETI.encode(sreg=sreg, to_k=int(to_k))


def vloop(sreg, body):
    return W.LOOP.encode(sreg=sreg, body=body)


def vbar(q=0):
    """Unpack queue `q` waits for every VFILL dispatched before."""
    return W.BAR.encode(q=q)


def vhalt():
    return W.HALT.encode()


def vfill(ad, l1off=0, rel=False):
    """Fill L1 from memory through descriptor `ad`, landing at L1 word
    `l1off`; `rel` adds the descriptor's running offset (see `ainc`)."""
    return W.FILL.encode(ad=ad, off=l1off, rel=int(rel))


def vdrain(ad, l1off=0, rel=False, node=None, buf_id=0, signal=False):
    """Drain L1 from word `l1off` through descriptor `ad`; `rel` as `vfill`.

    With `node` unset the sink is memory and `A[ad]` walks byte addresses.
    With `node` = ``(x, y)`` the sink is that CU's `CU_DATA` port: `A[ad]`'s
    base is the destination offset in 32-byte granules and its bound the
    count, so a strided `A[ad]` is not a peer drain. `signal` has the sink
    answer `SIG_DATA_RECEIVED` once the last word lands, at the coordinate in
    `A[ad]`'s base bits [23:16] (``ack_y << 20 | ack_x << 16 | l1 word``), which
    a peer drain points at the orchestrator: zero means the sender, and a
    sending core drops it.
    """
    peer = {}
    if node is not None:
        peer = {
            "node": 1,
            "dst_x": node[0],
            "dst_y": node[1],
            "buf_id": buf_id,
            "signal": int(signal),
        }
    return W.DRAIN.encode(ad=ad, off=l1off, rel=int(rel), **peer)


def e8m15(x: float) -> int:
    """A Python float as the lane's 24-bit format: FP32 with 15 mantissa bits
    (FP32's top 24 bits; `v2_core.v` seeds K1 = 0x3F8000 for 1.0)."""
    return struct.unpack("<I", struct.pack("<f", float(x)))[0] >> 8


# --------------------------------------------------------- CU instructions
def _flit(payload: int) -> int:
    """A 256-bit payload as a CU_INST flit; the agent stamps the destination.
    `last` stays clear: a last flit makes the node report SIG_BATCH_COMPLETE,
    whose argument is the program id rather than a RUN's cycle count."""
    out = payload & ((1 << 256) - 1)
    out |= CU_INST << (FLIT_BITS - 16 - 4)
    out |= CU_TXN << (FLIT_BITS - 20 - 8)
    return out


def imem_flit(addr: int, word: int) -> int:
    """Load one instruction word at `addr` of instruction memory."""
    return _flit(VEC.imem(addr, word))


def desc_flit(ad: int, fld: int, value: int) -> int:
    """Write one descriptor field: 0 is the base (a 40-bit address), 1..4
    the dims (`dim`)."""
    return _flit(VEC.desc_base(ad, value) if fld == 0 else VEC.desc(ad, fld, value))


def run_flit(pc: int = 0) -> int:
    """Start the image at `pc`. Retires when it reaches VHALT."""
    return _flit(VEC.run(pc))


def dim(stride: int, bound: int) -> int:
    """One `(stride, bound)` pair as the descriptor field wants it; a negative
    stride is legal (`vec_agu` reads it as signed 18 bits)."""
    if not 0 <= bound < 1 << 16:
        raise ISAError(f"bound = {bound} does not fit 16 bits")
    return (stride & ((1 << 18) - 1)) << 16 | bound


__all__ = [
    "CFG_AINC",
    "CFG_CHK",
    "CFG_CHKVL",
    "CFG_GETSTK",
    "CFG_PINC",
    "CFG_PPTR",
    "CFG_PSTR",
    "CFG_UINC",
    "CFG_UPTR",
    "CFG_USTR",
    "CFG_XB",
    "FAULTS",
    "IMEM_WORDS",
    "OPS",
    "VBAR",
    "VCFG",
    "VDRAIN",
    "VFILL",
    "VHALT",
    "VLOOP",
    "VPACK",
    "VSETI",
    "VSETVL",
    "VSKIPZ",
    "VSYNC",
    "VUNPK",
    "W_MK",
    "ainc",
    "chk",
    "chkvl",
    "desc_flit",
    "dim",
    "e8m15",
    "imem_flit",
    "math",
    "run_flit",
    "vcfg",
    "vdrain",
    "vfill",
    "vpack",
    "vpack_mx7",
    "vsync",
    "vunpk",
    "xb",
]
