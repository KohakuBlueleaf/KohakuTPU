"""The vector core's limits, its assembler, and the kernels that have run.

`MapKernel` is the AUTHORITY for the emitter: its bodies are what the card
has executed, and `tests/test_vecemit.py` pins the generic emitter to them
word for word. `L1_SAFE` is MEASURED -- see the comment on it.
"""

from kohakutpu.hw import vector as V

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
    "geglu": (
        2,
        lambda a, v, o: (_gelu(a, v[1], 8), a.emit(V.alu("VMUL", vd=o, va=v[0], vb=8))),
    ),
    "mul": (2, lambda a, v, o: a.emit(V.alu("VMUL", vd=o, va=v[0], vb=v[1]))),
    "add": (2, lambda a, v, o: a.emit(V.alu("VADD", vd=o, va=v[0], vc=v[1]))),
    "affine": (
        3,
        lambda a, v, o: a.emit(V.alu("VFMA", vd=o, va=v[0], vb=v[1], vc=v[2])),
    ),
    "copy": (1, lambda a, v, o: a.emit(V.alu("VMOV", vd=o, va=v[0]))),
    "div": (
        2,
        lambda a, v, o: a.emit(
            V.alu("VINV", vd=8, va=v[1]), V.alu("VMUL", vd=o, va=v[0], vb=8)
        ),
    ),
}


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
        self.image = (
            pre + body + [V.vdrain(self.ad_drain, self.nin * self.bw), V.vhalt()]
        )

    def static_descs(self):
        """Descriptors that never move: the L1 read/write windows."""
        out = []
        for i in range(self.nin):
            out.append(V.desc_flit(self.ad_ld[i], 0, i * self.bw))
            out.append(V.desc_flit(self.ad_ld[i], 1, V.dim(1, CHUNK_WORDS)))
        out.append(V.desc_flit(self.ad_st, 0, self.nin * self.bw))
        out.append(V.desc_flit(self.ad_st, 1, V.dim(1, CHUNK_WORDS)))
        for i in range(self.nin):
            out.append(V.desc_flit(self.ad_fill[i], 1, V.dim(V.WORD_BYTES, self.bw)))
        out.append(V.desc_flit(self.ad_drain, 1, V.dim(V.WORD_BYTES, self.bw)))
        return out

    def batch_flits(self, srcs, dst, b):
        """Move every DRAM base to batch `b` and run one pass."""
        step = self.batch * 2
        out = [
            V.desc_flit(self.ad_fill[i], 0, srcs[i] + b * step) for i in range(self.nin)
        ]
        out.append(V.desc_flit(self.ad_drain, 0, dst + b * step))
        out.append(V.run_flit(0))
        return out


def imem_flits(image, at=0):
    """One IMEM flit per instruction word, from `at`."""
    return [V.imem_flit(at + i, w) for i, w in enumerate(image)]
