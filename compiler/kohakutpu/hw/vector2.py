"""V2 vector-core machine code (`src/kohakutpu/vector2/v2_core.v`).

The 32-bit word and every field are in `docs/projects/kohakutpu/vector-core-v2.md`.
The CU_INST framing, descriptors and `Kernel` image are v1's
(:mod:`kohakutpu.hw.vector`), and so are VFILL / VDRAIN, whose encodings the
V2 core decodes unchanged.

A math word names registers 0..31 and its operand modes come from the
config the last `VCFG` set, applied only when its `om` bit is set; with `om`
clear an op reads every operand at chunk i on beat i, unswizzled and
unpredicated.
"""

from . import vector as V
from .vector import (  # noqa: F401
    Kernel,
    VectorEncodeError,
    desc_flit,
    dim,
    e8m15,
    run_flit,
)

_fit = V._fit

#: Math opcodes: v1's arithmetic group, plus EXP2D.
OPS = {k: v for k, v in V.OPS.items() if v <= 0x11}
OPS["VEXP2D"] = 0x12
VCFG, VUNPK, VPACK, VSYNC, VSKIPZ = 0x13, 0x14, 0x15, 0x16, 0x17
VSETVL, VSETI, VLOOP, VBAR, VHALT = 0x18, 0x1A, 0x1B, 0x1C, 0x1F

SRC_V, SRC_S, SRC_K = 0, 1, 3

#: VUNPK / VPACK modes.
F16, GT4, F32, MX7 = 0, 1, 2, 3

#: Crossbar modes (VCFG XB).
XM_NONE, XM_XOR, XM_ROT, XM_BCAST, XM_MERGE = 0, 1, 2, 3, 4

#: VCFG selectors.
CFG_CHK, CFG_XB, CFG_UPTR, CFG_UINC, CFG_USTR = 0, 1, 2, 3, 4
CFG_PPTR, CFG_PINC, CFG_PSTR, CFG_GETSTK = 5, 6, 7, 8

#: VSYNC waiters and the kinds they wait on.
#: W_A is the fill queue (VFILL), W_D the drain queue (VDRAIN); W_MK records
#: a mark (`vmark`).
W_FE, W_U, W_M, W_P, W_A, W_MK, W_D = 0, 1, 2, 3, 4, 5, 6
#: K_UNPACK / K_PACK are queue 0 of their engine, K_UNPACK1 / K_PACK1 queue 1.
#: A core built with DUALQ=0 runs queue 1's instructions on queue 0 and counts
#: K_UNPACK1 / K_PACK1 as K_UNPACK / K_PACK.
K_FILL, K_UNPACK, K_MATH, K_PACK, K_DRAIN, K_UNPACK1, K_PACK1 = 0, 1, 2, 3, 4, 5, 6

REGS = 32
CHUNKS = 8

FAULTS = dict(V.FAULTS)
FAULTS.update(
    {
        10: "F_GT4: a GT4 unpack/pack whose chunk count is not a multiple of 4",
        11: "F_SRC: operand selector 2 (v1's chain source) does not exist in V2",
        12: "F_MODE: unpack mode 3, or an MX7 pack not of 8 chunks at chunk base 0/1",
        13: "F_SYNC: VSYNC waiter or kind out of range",
    }
)


def math(op, vd, va=0, vb=0, vc=0, sa=SRC_V, sb=SRC_V, sc=SRC_V, om=False):
    """One math word. S/K operands name S[reg & 15] / K[reg & 3]."""
    code = OPS[op] if isinstance(op, str) else op
    return (
        _fit("op", code, 5) << 27
        | _fit("vd", vd, 5) << 22
        | _fit("va", va, 5) << 17
        | _fit("vb", vb, 5) << 12
        | _fit("vc", vc, 5) << 7
        | _fit("sa", sa, 2) << 5
        | _fit("sb", sb, 2) << 3
        | _fit("sc", sc, 2) << 1
        | (1 if om else 0)
    )


def vcfg(sel, payload):
    return VCFG << 27 | _fit("sel", sel, 4) << 23 | _fit("payload", payload, 23)


def _chk_bits(a, b, c, d):
    def nib(name, bs):
        base, st = bs
        return _fit(name + " base", base, 3) << 1 | _fit(name + " stride", st, 1)

    return nib("a", a) | nib("b", b) << 4 | nib("c", c) << 8 | nib("d", d) << 12


def chk(a=(0, 1), b=(0, 1), c=(0, 1), d=(0, 1)):
    """Chunk addressing: each operand's (base chunk, stride 0|1); d is vd."""
    return vcfg(CFG_CHK, _chk_bits(a, b, c, d))


CFG_CHKVL = 10


def chkvl(vl, a=(0, 1), b=(0, 1), c=(0, 1), d=(0, 1)):
    """VL (a literal, 1..128) and chunk addressing in one word."""
    if not 1 <= vl <= 128:
        raise VectorEncodeError(f"vl = {vl} is not 1..128")
    return vcfg(CFG_CHKVL, (vl - 1) << 16 | _chk_bits(a, b, c, d))


def xb(xm=XM_NONE, xk=0, sh=0, xc=False, pm=0, pr=0):
    """Crossbar on b (and c with `xc`), and the write predicate of om ops.

    xor/rot: lane l reads lane l^xk / (l+xk)%16. bcast: beat i reads lane
    (xk + (i >> sh)) % 16 into every lane. merge: pair-merge on bit xk.
    pm 0 writes all lanes, 1 those P[pr] sets, 2 the rest; a compare writes
    P[pr].
    """
    return vcfg(
        CFG_XB,
        _fit("xm", xm, 3)
        | _fit("xk", xk, 4) << 3
        | _fit("sh", sh, 3) << 7
        | (1 if xc else 0) << 10
        | _fit("pm", pm, 2) << 11
        | _fit("pr", pr, 2) << 13,
    )


CFG_AINC = 9


def cfg_ptr(sel, value):
    return vcfg(sel, _fit("pointer", value, 13))


def cfg_stride(sel, s0=1, s1=4):
    """USTR / PSTR: walk word w is at base + (w % 4) * s0 + (w // 4) * s1."""
    return vcfg(sel, _fit("s0", s0 % 512, 9) | _fit("s1", s1 % 512, 9) << 9)


def ainc(ad, inc):
    """Descriptor `ad`'s DMA increment, bytes: each `rel` VFILL/VDRAIN on it
    walks from base + offset, then offset += inc. Offsets restart at RUN."""
    return vcfg(CFG_AINC, _fit("ad", ad, 3) << 20 | (inc & ((1 << 18) - 1)))


def getstk(sreg):
    """S[sreg] = 1.0 if any EXP2D overflowed since the last read, else 0; clears."""
    return vcfg(CFG_GETSTK, _fit("sreg", sreg, 4))


def _walk(op, reg, off, n, mode, cb, rel, q):
    if mode == GT4 and n % 4:
        raise VectorEncodeError(f"GT4 walks whole groups of 4 chunks, not {n}")
    return (
        op << 27
        | _fit("reg", reg, 5) << 22
        | _fit("mode", mode, 2) << 20
        | _fit("chunks-1", n - 1, 3) << 17
        | _fit("chunk base", cb, 3) << 14
        | (1 if rel else 0) << 13
        | _fit("queue", q, 1) << 12
        | _fit("offset", off, 12)
    )


def vunpk(vd, off, n=8, mode=F16, cb=0, rel=False, q=0):
    """vd chunks cb.. = L1 words off + i*USTR (UPTR + off when `rel`), through
    unpack queue `q`."""
    return _walk(VUNPK, vd, off, n, mode, cb, rel, q)


def vpack(vs, off, n=8, mode=F16, cb=0, rel=False, q=0):
    """L1 words off + i*PSTR (PPTR + off when `rel`) = vs chunks cb.., through
    pack queue `q`."""
    return _walk(VPACK, vs, off, n, mode, cb, rel, q)


def vpack_mx7(vs, off, b_layout=False, rel=False, q=0):
    """One MXFP7 L1 entry from all of vs: words off + w*s0 (w = 0..3) = the
    quantiser's entry for lane r = chunks 2r, 2r+1 (k = (chunk & 1)*16 + vector
    lane), A packing, or B packing with `b_layout`."""
    return _walk(VPACK, vs, off, 8, MX7, 1 if b_layout else 0, rel, q)


def vsync(waiter, on=0, slack=0, mark=None, q=0):
    """`waiter` waits until every `on` instruction dispatched before, except
    the youngest `slack` of them, has finished. With `mark`, it waits instead
    for what that mark recorded (see `vmark`). A W_U / W_P waiter is queue `q`
    of that engine."""
    return (
        VSYNC << 27
        | _fit("waiter", waiter, 3) << 24
        | _fit("on", on, 3) << 21
        | _fit("slack", slack, 6) << 15
        | (0 if mark is None else 1 << 14 | _fit("mark", mark, 3) << 11)
        | _fit("queue", q, 1) << 10
    )


def vmark(mark, on, slack=0):
    """Mark `mark` = every `on` instruction dispatched so far (but the youngest
    `slack`); a later VSYNC with this mark waits for exactly those."""
    return vsync(W_MK, on, slack) | _fit("mark", mark, 3) << 11


def vskipz(sreg, n):
    return VSKIPZ << 27 | _fit("sreg", sreg, 4) << 22 | _fit("skip", n, 8)


def vsetvl(sreg):
    return VSETVL << 27 | _fit("sreg", sreg, 4) << 17


def vseti(sreg, to_k=False):
    return VSETI << 27 | _fit("sreg", sreg, 4) << 22 | (1 if to_k else 0)


def vloop(sreg, body):
    return VLOOP << 27 | _fit("sreg", sreg, 4) << 17 | _fit("body", body, 10) << 7


def vbar(q=0):
    """Unpack queue `q` waits for every VFILL dispatched before."""
    return VBAR << 27 | _fit("queue", q, 1) << 10


def vhalt():
    return VHALT << 27


def vfill(ad, l1off=0, rel=False):
    """v1's VFILL; `rel` adds descriptor `ad`'s running offset (see `ainc`)."""
    return V.vfill(ad, l1off) | (1 << 26 if rel else 0)


def vdrain(ad, l1off=0, rel=False, **peer):
    return V.vdrain(ad, l1off, **peer) | (1 << 26 if rel else 0)


#: Instruction memory words; RUN starts below 512 (its pc field is 9 bits).
IMEM_WORDS = 1024


def imem_flit(addr: int, word: int) -> int:
    """v1's IMEM flit, with address bit 9 in flit bit 242."""
    _fit("imem addr", addr, 10)
    return V.imem_flit(addr & 511, word) | (addr >> 9) << 242


class Kernel2(Kernel):
    """`Kernel` with the V2 two-word VSETI and 1024-word instruction memory."""

    def seti(self, sreg, value, to_k=False):
        return self.emit(vseti(sreg, to_k), value & 0xFFFFFF)

    def flits(self) -> list[int]:
        out = [desc_flit(ad, fld, v) for (ad, fld), v in sorted(self.descs.items())]
        out += [imem_flit(a, w) for a, w in sorted(self.words.items())]
        out.append(run_flit(self.start))
        return out
