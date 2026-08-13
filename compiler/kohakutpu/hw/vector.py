"""Vector-core machine code, and the CU instructions that carry it to a node.

`docs/isa/vector.md` §2 is the 32-bit instruction word; `vec_cu.v`'s header
comment is the 256-bit CU_INST payload that loads and runs one. Two encodings,
one file, because neither is useful without the other:

    IMEM  [255:252]=1  [251:243] addr   [31:0] word
    DESC  [255:252]=2  [251:249] ad     [248:246] fld   [245:212] value
    RUN   [255:252]=3  [251:243] start pc

A vector kernel is therefore a list of ordinary CU instructions -- the agent
stages and kicks them exactly as it stages a GEMM -- and `Kernel.flits` is the
whole translation.

FIELD WIDTHS ARE CHECKED. An oversized value shifts every field below it and
still produces a legal-looking word, which the core then decodes as some other
instruction; that is the one failure this encoding cannot report on its own.

PEER TRANSFERS, AND WHAT THE HOST CAN SEE OF THEM. `vdrain(..., node=(x, y))`
sends L1 to another CU instead of to memory, and `signal_on_complete` has the
receiver answer `SIG_DATA_RECEIVED`. That answer goes to the DESCRIPTOR'S
SOURCE:

* host -> CU, the source is the orchestrator (the dispatcher stamps it), so the
  ack lands in `NODE_STATUS[receiver]` and `Session.push_l1` waits on it.
* CU -> CU, the source is the SENDING CU, and nothing there consumes an ack --
  `vec_cu` drops any flit that is neither a read response nor `CU_DATA`. So the
  descriptor carries an ACK DESTINATION at `[215:208]` = ``{ack_y, ack_x}``,
  and a peer drain points it at the orchestrator. Zero means the source, which
  is what every host-originated burst uses and is unambiguous because `(0,0)`
  is a mesh corner and can never hold an endpoint.

For a `VDRAIN` the ack destination rides in the DESCRIPTOR'S BASE at `[23:16]`,
because the instruction word has one bit left and the base is 34 bits of which
a peer drain uses sixteen. So `desc(ad, base, dim(1, n))` with
``base = ack_y << 20 | ack_x << 16 | peer_l1_word``.

`VHALT` still means the flits were SENT, not landed -- `vec_core` waits on
`pipe_empty` and `fill_out`, neither of which knows a drain exists. The ack is
what tells the host the data arrived; the payload never leaves the mesh.
"""

from dataclasses import dataclass, field

FLIT_BITS = 288
CU_INST = 0x5
CU_TXN = 0x40

#: opcode by mnemonic, `docs/isa/vector.md` §3.
OPS = {
    "VMOV": 0x00,
    "VNEG": 0x01,
    "VABS": 0x02,
    "VADD": 0x03,
    "VSUB": 0x04,
    "VMUL": 0x05,
    "VFMA": 0x06,
    "VFNMA": 0x07,
    "VMAX": 0x08,
    "VMIN": 0x09,
    "VSEL": 0x0A,
    "VCMPLT": 0x0B,
    "VCMPGT": 0x0C,
    "VCMPEQ": 0x0D,
    "VEXP2": 0x0E,
    "VLOG2": 0x0F,
    "VINV": 0x10,
    "VRSQRT": 0x11,
    "VCVT": 0x12,
    "VRED": 0x13,
    "VLD": 0x14,
    "VST": 0x15,
    "VBCAST": 0x16,
    "VSHUF": 0x17,
    "VSETVL": 0x18,
    "VSETMODE": 0x19,
    "VSETI": 0x1A,
    "VLOOP": 0x1B,
    "VBAR": 0x1C,
    "VFILL": 0x1D,
    "VDRAIN": 0x1E,
    "VHALT": 0x1F,
}

#: operand source selectors, the `sa`/`sb`/`sc` fields.
SRC_V, SRC_S, SRC_C, SRC_K = 0, 1, 2, 3

#: `VMODE`, as `vec_core.v` numbers it: D4 is 2 and TREE is 3.
FLAT, D2, D4, TREE = 0, 1, 2, 3

#: `VLD`/`VST`/`VCVT` dtype. Only these two reach memory -- the core raises
#: F_DTYPE on the rest (`HANDOFF-vector-core.md` §2).
DT_FP16, DT_FP32 = 1, 2

#: `VRED` kinds, in the `vc` field.
RED = {
    "SUM": 0,
    "MAX": 1,
    "MIN": 2,
    "SUMSQ": 3,
    "DOT": 4,
    "EXPSUM": 5,
    "ANY": 6,
    "ALL": 7,
}

#: `vec_core.v` fault codes, so a SIG_FAULT reads as a reason.
FAULTS = {
    1: "F_DTYPE: VLD/VST/VCVT dtype is not FP16 or FP32",
    2: "F_VSRC: a chained instruction named a vector register source",
    3: "F_CHAIN: source C used outside D2/D4/TREE",
    4: "F_OPCODE: unknown opcode, or VRED outside TREE mode",
    5: "F_LEN: a VFILL/VDRAIN walk is longer than 256 entries",
    6: "F_LOOP: VLOOP nested more than one deep",
    7: "F_VL: VSETVL asked for 0 or more than VLMAX",
    8: "F_REDVL: VRED with VL not a multiple of 16",
    9: "F_CUDATA: an inbound CU_DATA burst named a buffer or an L1 range this "
    "core does not have, or two senders' bursts interleaved",
}

#: VLMAX, and the elements one register chunk holds. `vec_core.v` walks
#: `nchunk = ceil(VL / 16)` chunks per VLD/VST.
VLMAX = 128
LANES = 16

#: One L1 word and one memory word: 256 bits.
WORD_BYTES = 32


class VectorEncodeError(ValueError):
    """A value that does not fit its instruction field."""


def _fit(name, value, width):
    if not isinstance(value, int) or value < 0 or value >> width:
        raise VectorEncodeError(
            f"{name} = {value!r} does not fit {width} bits; a value too wide "
            "here shifts every field below it and still decodes as something"
        )
    return value


def alu(op, vd, va=0, vb=0, vc=0, sa=SRC_V, sb=SRC_V, sc=SRC_V, pr=0, pm=0):
    """One arithmetic word: `op vd, [sa]va, [sb]vb, [sc]vc`.

    `op` is a mnemonic or a raw opcode. The three source selectors are what
    make one opcode cover many operations (isa/vector.md §2), so they are
    ordinary arguments rather than something a caller has to bit-twiddle.
    """
    code = OPS[op] if isinstance(op, str) else op
    return (
        _fit("op", code, 5) << 27
        | _fit("sa", sa, 2) << 25
        | _fit("sb", sb, 2) << 23
        | _fit("sc", sc, 2) << 21
        | _fit("vd", vd, 4) << 17
        | _fit("va", va, 4) << 13
        | _fit("vb", vb, 4) << 9
        | _fit("vc", vc, 4) << 5
        | _fit("pr", pr, 2) << 3
        | _fit("pm", pm, 2) << 1
    )


def _addressed(op, vd, ad, off, dt=0):
    """The load/store overlay: dtype and a descriptor instead of three sources."""
    code = OPS[op] if isinstance(op, str) else op
    return (
        _fit("op", code, 5) << 27
        | _fit("dtype", dt, 3) << 24
        | _fit("ad", ad, 3) << 21
        | _fit("vd", vd, 4) << 17
        | _fit("offset", off, 14)
    )


def vld(vd, ad, off=0, dt=DT_FP16):
    """`vd = L1[A[ad]]`, converting from `dt`. The walk is `ceil(VL/16)` words."""
    return _addressed("VLD", vd, ad, off, dt)


def vst(vs, ad, off=0, dt=DT_FP16):
    """`L1[A[ad]] = vs`, converting to `dt`. The SOURCE register sits at `vd`."""
    return _addressed("VST", vs, ad, off, dt)


def vfill(ad, l1off=0):
    """Start an L1 fill from memory, descriptor `A[ad]`, landing at L1 `l1off`."""
    return _addressed("VFILL", 0, ad, l1off)


def vdrain(ad, l1off=0, node=None, buf_id=0, signal=False):
    """Start an L1 drain from L1 word `l1off`, descriptor `A[ad]`.

    With `node` unset the sink is memory and `A[ad]` walks byte addresses --
    the encoding every kernel written before peer drains existed produces, bit
    for bit. Pass `node` as `(x, y)` and the sink is that CU's `CU_DATA` port
    instead: `A[ad]`'s BASE becomes the destination offset in 32-byte granules
    and its BOUNDS become the count, because one `CU_DATA` descriptor covers a
    contiguous run (`docs/isa/vector.md` §5.1). A strided `A[ad]` is therefore
    not a peer drain.

    `buf_id` is the destination buffer, defined by whatever CU receives it -- a
    vector core has one flat L1 and faults on anything but 0. `signal` asks the
    sink to answer `SIG_DATA_RECEIVED` once the last word lands, which is how a
    reader knows the data is there. WHERE that answer goes is `A[ad]`'s base
    bits `[23:16]` -- see this module's docstring -- and for a peer drain it
    must name the orchestrator, or the ack goes to this core and is dropped.

    Raises :class:`VectorEncodeError` if any field does not fit. `l1off` is
    checked at NINE bits, not the 14 the offset field holds: above that it
    would run into `buf_id`.
    """
    word = _addressed("VDRAIN", 0, ad, _fit("l1off", l1off, 9))
    if node is None:
        return word
    x, y = node
    return (
        word
        | (1 if signal else 0) << 25
        | 1 << 24
        | _fit("dst_x", x, 4) << 17
        | _fit("dst_y", y, 4) << 13
        | _fit("buf_id", buf_id, 4) << 9
    )


def vbar():
    """Wait until every outstanding VFILL has retired."""
    return OPS["VBAR"] << 27


def vhalt():
    """Signal done to the agent. What a DRAIN is to a cluster."""
    return OPS["VHALT"] << 27


def vsetvl(sreg):
    """`VL = min(S[sreg], VLMAX)`. Faults on 0 or above VLMAX."""
    return OPS["VSETVL"] << 27 | _fit("va", sreg, 4) << 13


def vsetmode(mode):
    """`VMODE = mode`: FLAT / D2 / D4 / TREE."""
    return OPS["VSETMODE"] << 27 | _fit("mode", mode, 4) << 13


def vseti(sreg, to_k=False):
    """The first of TWO words; the caller appends the 24-bit immediate itself.

    `to_k` writes K3 rather than a scalar register, which is the only way to
    get an arbitrary constant into a `K` operand.
    """
    return (
        OPS["VSETI"] << 27
        | (SRC_K if to_k else SRC_V) << 25
        | _fit("vd", sreg, 4) << 17
    )


def vloop(sreg, body):
    """Hardware loop: `count = S[sreg]`, `body` instruction words follow."""
    return OPS["VLOOP"] << 27 | _fit("va", sreg, 4) << 13 | _fit("body", body, 4) << 9


def vred(sreg, va, kind="SUM", vb=0, pr=0):
    """Tree-reduce `va` into `S[sreg]`. TREE mode only, and VL % 16 == 0.

    `vb` carries EXPSUM's vector destination and is otherwise unused; `pr`
    names the predicate register ANY/ALL reduce.
    """
    k = RED[kind] if isinstance(kind, str) else kind
    return (
        OPS["VRED"] << 27
        | _fit("vd", sreg, 4) << 17
        | _fit("va", va, 4) << 13
        | _fit("vb", vb, 4) << 9
        | _fit("kind", k, 4) << 5
        | _fit("pr", pr, 2) << 3
    )


def vbcast(vd, sreg):
    """`vd = S[sreg]` in every lane."""
    return (
        OPS["VBCAST"] << 27
        | SRC_S << 25
        | _fit("vd", vd, 4) << 17
        | _fit("va", sreg, 4) << 13
    )


def e8m15(x: float) -> int:
    """A Python float as the lane's own 24-bit format: FP32 with 15 mantissa bits.

    `vec_core.v` seeds K1 with 0x3F8000 for 1.0 and K2 with 0xBF8000 for -1.0,
    which is FP32's top 24 bits, so this is a truncation and not a conversion.
    """
    import struct

    return struct.unpack("<I", struct.pack("<f", float(x)))[0] >> 8


# --------------------------------------------------------------------------
# CU instructions
# --------------------------------------------------------------------------
def _flit(payload: int, last: bool = False) -> int:
    """Wrap a 256-bit payload as a CU_INST flit. The agent stamps the destination.

    `last` is left CLEAR by every caller here on purpose: it makes the node
    report SIG_BATCH_COMPLETE, whose argument is the program id rather than
    `exec_result` -- and `exec_result` for a RUN is the kernel's cycle count,
    the only timing the machine will tell anybody.
    """
    out = payload & ((1 << 256) - 1)
    out |= CU_INST << (FLIT_BITS - 16 - 4)
    out |= CU_TXN << (FLIT_BITS - 20 - 8)
    if last:
        out |= 1 << (FLIT_BITS - 28 - 1)
    return out


def imem_flit(addr: int, word: int) -> int:
    """Load one instruction word into the core's instruction memory."""
    return _flit(1 << 252 | _fit("imem addr", addr, 9) << 243 | _fit("word", word, 32))


def desc_flit(ad: int, fld: int, value: int) -> int:
    """Write one address-descriptor field: 0 is the base, 1..4 are the dims.

    A dim's value is `{stride[17:0], bound[15:0]}`, and a bound of 0 reads as 1
    so an unused dimension needs no encoding (`vec_agu.v`).
    """
    return _flit(
        2 << 252
        | _fit("ad", ad, 3) << 249
        | _fit("fld", fld, 3) << 246
        | _fit("descriptor value", value, 34) << 212
    )


def run_flit(pc: int = 0) -> int:
    """Start the kernel at `pc`. Retires when it reaches VHALT."""
    return _flit(3 << 252 | _fit("start pc", pc, 9) << 243)


def dim(stride: int, bound: int) -> int:
    """One `(stride, bound)` pair, packed as the descriptor field wants it.

    A NEGATIVE stride is legal -- `vec_agu` reads it as signed 18 bits -- so it
    is masked here rather than refused.
    """
    return (stride & ((1 << 18) - 1)) << 16 | _fit("bound", bound, 16)


@dataclass
class Kernel:
    """An instruction memory image, a set of descriptors, and where to start.

    `at` places words explicitly so two kernels can share one image, which is
    what the core's `RUN pc` is for; `emit` appends at the next free address.
    """

    words: dict = field(default_factory=dict)
    descs: dict = field(default_factory=dict)
    start: int = 0
    _next: int = 0

    def emit(self, *words) -> "Kernel":
        for w in words:
            self.at(self._next, w)
        return self

    def at(self, addr: int, word: int) -> "Kernel":
        if addr in self.words:
            raise VectorEncodeError(f"instruction memory {addr} is already written")
        self.words[addr] = word
        self._next = max(self._next, addr + 1)
        return self

    def seti(self, sreg, value, to_k=False) -> "Kernel":
        """`VSETI` and its immediate, which the core fetches as the next word."""
        return self.emit(vseti(sreg, to_k), value & 0xFFFFFF)

    def desc(self, ad: int, base: int, *dims) -> "Kernel":
        """Descriptor `ad`: a base and up to four `dim(stride, bound)` values."""
        if len(dims) > 4:
            raise VectorEncodeError(f"descriptor {ad} has {len(dims)} dims, max 4")
        self.descs[(ad, 0)] = base
        for i, d in enumerate(dims):
            self.descs[(ad, i + 1)] = d
        return self

    def flits(self) -> list[int]:
        """Every CU instruction, descriptors first, then code, then the RUN.

        Order matters only for the RUN, which must arrive last; the node
        executes its instruction FIFO strictly in order, so nothing else needs
        a barrier.
        """
        out = [desc_flit(ad, fld, v) for (ad, fld), v in sorted(self.descs.items())]
        out += [imem_flit(a, w) for a, w in sorted(self.words.items())]
        out.append(run_flit(self.start))
        return out
