"""Vector-core kernels for the elementwise and normalising ops, as builders.

All three return instructions and descriptors only -- WHERE the operands live is
the caller's, which is what lets `ktpu.hw.chain` point them at planned addresses
instead of uploading and reading back around every call.

The 14-bit signed `offset` in VLD/VST is added to the descriptor's address
(`vec_core.v:586`), which is what lets chunks unroll with no descriptor rewrite.

CONSTANTS LIVE IN SCALAR REGISTERS, not K3: `VSETI ..to_k` always writes kreg[3]
(`vec_core.v:862`), so two arbitrary constants would cost a VSETI per chunk.
"""

from ktpu.hw import vector as V

LOG2E = 1.4426950408889634
GELU_SCALE = 0.7978845608028654
GELU_CUBIC = 0.044715

#: fp16 elements in one 256-bit L1/memory word, and words one VL=128 pass walks.
WORD_ELEMS = V.WORD_BYTES // 2
CHUNK_WORDS = V.VLMAX // WORD_ELEMS

#: kreg indices seeded by the core: 0.0, 1.0, -1.0. kreg[3] is VSETI's.
K_ZERO, K_ONE, K_NEG1 = 0, 1, 2

#: scalar registers. S0 carries VL, S1..S3 are VRED destinations, and constants
#: start above them -- a constant at S1 is silently eaten by the first VRED.
S_VL = 0
S_CONST0 = 4

#: `vec_core` L1_DEPTH on every shipped top.
L1_WORDS = 512

#: MEASURED on ship_3x2, unexplained: a 352..480-word footprint corrupts the
#: OUTPUT buffer; <=320 and exactly 512 are clean (scratch/sdxl-fwd/gn_sweep.py).
L1_SAFE = 320


def require_l1(what, words):
    """Refuse an L1 footprint in the band that silently corrupts the output.

    Raises ValueError naming the footprint and the two usable ranges. Refusing
    is the whole point: in the band the kernel completes, signals success and
    returns wrong numbers, which is the failure this project spends its guards on.
    """
    if words > L1_WORDS:
        raise ValueError(f"{what}: {words} L1 words, L1 has {L1_WORDS}")
    if L1_SAFE < words < L1_WORDS:
        raise ValueError(
            f"{what}: {words} L1 words is inside the measured bad band "
            f"({L1_SAFE + 1}..{L1_WORDS - 1}), where the card returns wrong "
            f"data in the output buffer and reports success. Use a footprint of "
            f"{L1_SAFE} words or fewer, or exactly {L1_WORDS}."
        )


class Asm:
    """An instruction list with named scalar constants, assembled once."""

    def __init__(self):
        self.words = []
        self.consts = {}

    def emit(self, *w):
        self.words += list(w)
        return self

    def const(self, value: float) -> int:
        """A scalar register holding `value`, allocated on first use."""
        key = float(value)
        if key not in self.consts:
            self.consts[key] = len(self.consts) + S_CONST0
        return self.consts[key]

    def preamble_consts(self):
        out = []
        for value, reg in self.consts.items():
            out += [V.vseti(reg), V.e8m15(value)]
        return out


def sK(reg):
    return {"sb": V.SRC_K, "vb": reg}


def _silu(a, vin, vout):
    """`x * sigmoid(x)` in five ALU ops."""
    c = a.const(-LOG2E)
    a.emit(
        V.alu("VMUL", vd=vout, va=vin, vb=c, sb=V.SRC_S),
        V.alu("VEXP2", vd=vout, va=vout),
        V.alu("VADD", vd=vout, va=vout, vc=K_ONE, sc=V.SRC_K),
        V.alu("VINV", vd=vout, va=vout),
        V.alu("VMUL", vd=vout, va=vin, vb=vout),
    )


def _gelu(a, vin, vout, tmp=9):
    """`x * (1 - 1/(2^(c2 (x + c1 x^3)) + 1))`, the tanh form with tanh folded.

    `0.5 x (1 + tanh u)` is `x (1 - 1/(e^2u + 1))`, which is one exponential and
    no separate tanh -- the same rearrangement `dsl.library.tanh` makes.
    """
    c1 = a.const(GELU_CUBIC)
    c2 = a.const(2.0 * LOG2E * GELU_SCALE)
    a.emit(
        V.alu("VMUL", vd=tmp, va=vin, vb=vin),
        V.alu("VMUL", vd=tmp, va=tmp, vb=c1, sb=V.SRC_S),
        V.alu("VADD", vd=tmp, va=tmp, vc=K_ONE, sc=V.SRC_K),
        V.alu("VMUL", vd=tmp, va=tmp, vb=vin),
        V.alu("VMUL", vd=tmp, va=tmp, vb=c2, sb=V.SRC_S),
        V.alu("VEXP2", vd=tmp, va=tmp),
        V.alu("VADD", vd=tmp, va=tmp, vc=K_ONE, sc=V.SRC_K),
        V.alu("VINV", vd=tmp, va=tmp),
        V.alu("VSUB", vd=tmp, va=K_ONE, sa=V.SRC_K, vc=tmp),
        V.alu("VMUL", vd=vout, va=vin, vb=tmp),
    )


BODIES = {
    # name -> (n inputs, builder(asm, [vin...], vout))
    "silu": (1, lambda a, v, o: _silu(a, v[0], o)),
    "gelu": (1, lambda a, v, o: _gelu(a, v[0], o)),
    "geglu": (2, lambda a, v, o: (_gelu(a, v[1], 8), a.emit(
        V.alu("VMUL", vd=o, va=v[0], vb=8)))),
    "mul": (2, lambda a, v, o: a.emit(V.alu("VMUL", vd=o, va=v[0], vb=v[1]))),
    "add": (2, lambda a, v, o: a.emit(V.alu("VADD", vd=o, va=v[0], vc=v[1]))),
    "affine": (3, lambda a, v, o: a.emit(
        V.alu("VFMA", vd=o, va=v[0], vb=v[1], vc=v[2]))),
    "copy": (1, lambda a, v, o: a.emit(V.alu("VMOV", vd=o, va=v[0]))),
    "div": (2, lambda a, v, o: a.emit(
        V.alu("VINV", vd=8, va=v[1]), V.alu("VMUL", vd=o, va=v[0], vb=8))),
}


def flash_rows_kernel(rows, cols, scale):
    """One online-softmax step for `rows` query rows against one key block.

    Emits, per row: the block's row max folded into the running max, the
    correction `2^(m_prev - m_new)`, the exponentiated scores, and the running
    denominator. Returns `(image, static_descs, batch_flits)`; the four outputs
    are contiguous in L1 so one VDRAIN carries all of them.

    `scale` is already in log2 space, so `exp2` takes its operand directly.
    """
    w = cols // WORD_ELEMS
    rw = rows * w
    require_l1(f"flash {rows}x{cols}", 7 * rw)
    AD_FS, AD_FM, AD_FL, AD_DR = 0, 1, 2, 3
    AD_S, AD_M, AD_L, AD_O = 4, 5, 6, 7
    a = Asm()
    csc = a.const(scale)

    pre = [V.vseti(S_VL), cols] + a.preamble_consts()
    pre += [
        V.vsetvl(S_VL), V.vsetmode(V.FLAT),
        V.vfill(AD_FS, 0), V.vfill(AD_FM, rw), V.vfill(AD_FL, 2 * rw), V.vbar(),
    ]
    body = []
    for r in range(rows):
        o = r * w
        body += [
            V.vld(0, AD_S, o),
            V.alu("VMUL", vd=0, va=0, vb=csc, sb=V.SRC_S),
            V.vsetmode(V.TREE), V.vred(1, 0, "MAX"), V.vsetmode(V.FLAT),
            V.vbcast(1, 1),
            V.vld(2, AD_M, o),
            V.alu("VMAX", vd=3, va=1, vb=2),
            V.alu("VSUB", vd=4, va=2, vc=3),
            V.alu("VEXP2", vd=4, va=4),
            V.alu("VSUB", vd=5, va=0, vc=3),
            V.alu("VEXP2", vd=5, va=5),
            V.vsetmode(V.TREE), V.vred(2, 5, "SUM"), V.vsetmode(V.FLAT),
            V.vbcast(6, 2),
            V.vld(7, AD_L, o),
            V.alu("VFMA", vd=8, va=7, vb=4, vc=6),
            V.vst(5, AD_O, o),
            V.vst(4, AD_O, rw + o),
            V.vst(3, AD_O, 2 * rw + o),
            V.vst(8, AD_O, 3 * rw + o),
        ]
    image = pre + body + [V.vdrain(AD_DR, 3 * rw), V.vhalt()]

    static = []
    for ad, base in ((AD_S, 0), (AD_M, rw), (AD_L, 2 * rw), (AD_O, 3 * rw)):
        static += [V.desc_flit(ad, 0, base), V.desc_flit(ad, 1, V.dim(1, w))]
    for ad in (AD_FS, AD_FM, AD_FL):
        static.append(V.desc_flit(ad, 1, V.dim(V.WORD_BYTES, rw)))
    static.append(V.desc_flit(AD_DR, 1, V.dim(V.WORD_BYTES, 4 * rw)))

    def batch(bs, bm, bl, bo, i):
        step = rows * cols * 2
        return [
            V.desc_flit(AD_FS, 0, bs + i * step),
            V.desc_flit(AD_FM, 0, bm + i * step),
            V.desc_flit(AD_FL, 0, bl + i * step),
            V.desc_flit(AD_DR, 0, bo + i * 4 * step),
            V.run_flit(0),
        ]

    return image, static, batch


class MapKernel:
    """An elementwise pass over `nin` equally-shaped fp16 arrays in DRAM.

    L1 holds one batch of every input followed by the output, so a batch is
    `chunks * VLMAX` elements and `(nin + 1) * batch_words` L1 words.
    """

    def __init__(self, name, chunks=8):
        self.name = name
        self.nin, build = BODIES[name]
        self.chunks = chunks
        self.batch = chunks * V.VLMAX
        self.bw = self.batch // WORD_ELEMS
        require_l1(f"map {name} x{chunks}", (self.nin + 1) * self.bw)
        if self.bw > 256:
            raise ValueError(f"{name}: a {self.bw}-word fill exceeds the 256 limit")

        self.ad_fill = list(range(self.nin))
        self.ad_ld = [self.nin + i for i in range(self.nin)]
        self.ad_st = 2 * self.nin
        self.ad_drain = 2 * self.nin + 1
        if self.ad_drain > 7:
            raise ValueError(f"{name}: needs {self.ad_drain + 1} descriptors, have 8")

        a = Asm()
        body = []
        for j in range(chunks):
            off = j * CHUNK_WORDS
            for i in range(self.nin):
                body.append(V.vld(i, self.ad_ld[i], off))
            mark = len(a.words)
            build(a, list(range(self.nin)), 7)
            body += a.words[mark:]
            body.append(V.vst(7, self.ad_st, off))
        pre = [V.vseti(S_VL), V.VLMAX]
        pre += a.preamble_consts()
        pre += [V.vsetvl(S_VL), V.vsetmode(V.FLAT)]
        pre += [V.vfill(self.ad_fill[i], i * self.bw) for i in range(self.nin)]
        pre += [V.vbar()]
        self.image = pre + body + [
            V.vdrain(self.ad_drain, self.nin * self.bw), V.vhalt()
        ]

    def static_descs(self):
        """Descriptors that never move: the L1 read/write windows."""
        out = []
        for i in range(self.nin):
            out.append(V.desc_flit(self.ad_ld[i], 0, i * self.bw))
            out.append(V.desc_flit(self.ad_ld[i], 1, V.dim(1, CHUNK_WORDS)))
        out.append(V.desc_flit(self.ad_st, 0, self.nin * self.bw))
        out.append(V.desc_flit(self.ad_st, 1, V.dim(1, CHUNK_WORDS)))
        for i in range(self.nin):
            out.append(V.desc_flit(self.ad_fill[i], 1,
                                   V.dim(V.WORD_BYTES, self.bw)))
        out.append(V.desc_flit(self.ad_drain, 1, V.dim(V.WORD_BYTES, self.bw)))
        return out

    def batch_flits(self, srcs, dst, b):
        """Move every DRAM base to batch `b` and run one pass."""
        step = self.batch * 2
        out = [V.desc_flit(self.ad_fill[i], 0, srcs[i] + b * step)
               for i in range(self.nin)]
        out.append(V.desc_flit(self.ad_drain, 0, dst + b * step))
        out.append(V.run_flit(0))
        return out


def group_norm_kernel(nelem, chunks, eps=1e-5):
    """One group of a GroupNorm, start to finish, in a single RUN.

    Pass 1 sums the group into a vector accumulator and tree-reduces it; pass 2
    accumulates the squared deviation from that mean; pass 3 normalises and
    applies the per-element gamma/beta the host has already broadcast. The mean
    and the reciprocal deviation stay in vector registers throughout, so nothing
    round-trips through memory between passes.

    TWO PASSES, NOT `E[x^2] - E[x]^2`. The lane carries 16 significand bits and
    that identity cancels exactly where a normalisation is used.
    """
    bw = nelem // WORD_ELEMS
    if nelem % V.VLMAX or chunks * V.VLMAX != nelem:
        raise ValueError(f"group of {nelem} is not {chunks} x VLMAX")
    require_l1(f"group norm of {nelem}", 4 * bw)
    AD_FX, AD_FG, AD_FB, AD_DR = 0, 1, 2, 3
    AD_X, AD_G, AD_B, AD_O = 4, 5, 6, 7
    a = Asm()
    inv_n = a.const(1.0 / nelem)
    c_eps = a.const(eps)

    pre = [V.vseti(S_VL), V.VLMAX] + a.preamble_consts()
    pre += [
        V.vsetvl(S_VL), V.vsetmode(V.FLAT),
        V.vfill(AD_FX, 0), V.vfill(AD_FG, bw), V.vfill(AD_FB, 2 * bw), V.vbar(),
    ]

    body = []
    for j in range(chunks):
        off = j * CHUNK_WORDS
        body.append(V.vld(0, AD_X, off))
        body.append(V.alu("VMOV", vd=10, va=0) if j == 0
                    else V.alu("VADD", vd=10, va=10, vc=0))
    body += [
        V.vsetmode(V.TREE), V.vred(1, 10, "SUM"), V.vsetmode(V.FLAT),
        V.vbcast(11, 1),
        V.alu("VMUL", vd=11, va=11, vb=inv_n, sb=V.SRC_S),
    ]
    for j in range(chunks):
        off = j * CHUNK_WORDS
        body.append(V.vld(0, AD_X, off))
        body.append(V.alu("VSUB", vd=1, va=0, vc=11))
        body.append(V.alu("VMUL", vd=12, va=1, vb=1) if j == 0
                    else V.alu("VFMA", vd=12, va=1, vb=1, vc=12))
    body += [
        V.vsetmode(V.TREE), V.vred(2, 12, "SUM"), V.vsetmode(V.FLAT),
        V.vbcast(13, 2),
        V.alu("VMUL", vd=13, va=13, vb=inv_n, sb=V.SRC_S),
        V.alu("VADD", vd=13, va=13, vc=c_eps, sc=V.SRC_S),
        V.alu("VRSQRT", vd=13, va=13),
    ]
    for j in range(chunks):
        off = j * CHUNK_WORDS
        body += [
            V.vld(0, AD_X, off),
            V.vld(2, AD_G, off),
            V.vld(3, AD_B, off),
            V.alu("VSUB", vd=1, va=0, vc=11),
            V.alu("VMUL", vd=1, va=1, vb=13),
            V.alu("VFMA", vd=4, va=1, vb=2, vc=3),
            V.vst(4, AD_O, off),
        ]
    image = pre + body + [V.vdrain(AD_DR, 3 * bw), V.vhalt()]

    static = []
    for ad, base in ((AD_X, 0), (AD_G, bw), (AD_B, 2 * bw), (AD_O, 3 * bw)):
        static.append(V.desc_flit(ad, 0, base))
        static.append(V.desc_flit(ad, 1, V.dim(1, CHUNK_WORDS)))
    for ad in (AD_FX, AD_FG, AD_FB, AD_DR):
        static.append(V.desc_flit(ad, 1, V.dim(V.WORD_BYTES, bw)))

    def batch(bx, bg, bb, bo, g):
        step = nelem * 2
        return [
            V.desc_flit(AD_FX, 0, bx + g * step),
            V.desc_flit(AD_FG, 0, bg + g * step),
            V.desc_flit(AD_FB, 0, bb + g * step),
            V.desc_flit(AD_DR, 0, bo + g * step),
            V.run_flit(0),
        ]

    return image, static, batch


def imem_flits(image, at=0):
    return [V.imem_flit(at + i, w) for i, w in enumerate(image)]


def dispatch_all(s, node, flits, timeout, window=None):
    """Send `flits` in order, split into staging windows. Returns dispatch count.

    Instruction memory and descriptors are core state, so a split anywhere is
    safe as long as order is kept: a RUN in a later window still sees what an
    earlier one loaded.
    """
    window = window or s.board.stage_flits
    n = 0
    for i in range(0, len(flits), window):
        st = V.run_kernel(s.link, node, flits[i: i + window], timeout)
        if st["fault"]:
            raise RuntimeError(f"vector fault: {st['why']}")
        n += 1
    return n
