"""KohakuTPU's L2 -> L1 lowerers (docs/projects/kohakutpu/ir/l2.md §3): one
object a unit, its state kept across items, chunks out
(`kohakuaccel.ir.l2.lower.Chunk`)."""

from functools import cache

from kohakuaccel.ir.l2.lower import Chunk
from kohakuaccel.package import mover as PM
from kohakutpu.ir.l1 import vsched
from kohakutpu.ir.l1.cluster import Drain, Fill, Gemm
from kohakutpu.ir.l1.kernels import attention as AT
from kohakutpu.ir.l1.kernels import binary as BI
from kohakutpu.ir.l1.kernels import layernorm as LN
from kohakutpu.ir.l1.kernels import silu as SI
from kohakutpu.ir.l1.kernels import softmax as SM
from kohakutpu.ir.l1.kernels import stream
from kohakutpu.ir.l1.kernels.matmul import ENTRY, fills
from kohakutpu.isa.cluster import ISA

#: Entries a cluster fill moves a cycle when the four share memory (~30 B/cycle
#: MEASURED for distinct addresses, card_v9_1n), for the cost estimate.
FILL_BYTES_A_CYCLE = 7.5


class ClusterLowerer:
    """A cluster's L1 banks and its held fused DRAIN.

    Each side's two banks remember the operand bytes they hold. An operand a
    step marks `keep` is not filled again while its bank holds it; any other is
    filled into the bank the latest GEMM on that side did NOT read, so a fill
    never lands under a sweep still running.
    """

    def __init__(self, coord) -> None:
        self.coord = coord
        self.content = {0: [None, None], 1: [None, None]}
        self.kept = {0: [False, False], 1: [False, False]}
        self.last = {0: 1, 1: 1}
        self.ones = [False, False]
        self.held = None  # (Drain, item index)

    # ------------------------------------------------------------ operands
    def _operand(self, side, key, addr, entries, ops, keep=False, eoff=0) -> int:
        for bank in (0, 1):
            if self.content[side][bank] == key:
                self.kept[side][bank] = self.kept[side][bank] or keep
                return bank
        bank = 1 - self.last[side]
        ops += fills(addr, entries, side, bank, eoff)
        self.content[side][bank] = key
        self.kept[side][bank] = keep
        return bank

    def _forget_scratch(self) -> None:
        """An operand not kept is valid within its own item only."""
        for side in (0, 1):
            for bank in (0, 1):
                if not self.kept[side][bank]:
                    self.content[side][bank] = None

    def _release(self, ops, done) -> None:
        if self.held is not None:
            ops.append(self.held[0])
            done.append(self.held[1])
            self.held = None

    # --------------------------------------------------------------- items
    def lower(self, index, item) -> list:
        p = item.params
        if item.kind == "gemm":
            return [self._gemm(index, p)]
        steps = self._steps(item)
        return [self._tile(index, p, steps)]

    def _steps(self, item) -> list:
        """``(a key/addr/entries, aoff, b key/addr/entries, nk)`` a sweep."""
        p = item.params
        out = []
        if item.kind == "gemm_tile":
            for c in range(p["chunks"]):
                a = p["a_at"] + c * p["gm"] * p["nk"] * ENTRY
                b = p["b_at"] + c * p["gn"] * p["nk"] * ENTRY
                out.append(((a, p["gm"] * p["nk"]), 0, (b, p["gn"] * p["nk"]), p["nk"]))
        elif item.kind == "conv_tile":
            cbc, wp, row0 = p["cbc"], p["wp"], p["row0"]
            c = 0
            for kc in range(p["chunks"]):
                for dy in range(3):
                    a = p["a_at"] + kc * p["a_chunk"] + (row0 + dy * wp) * cbc * ENTRY
                    for dx in range(3):
                        b = p["b_at"] + c * p["gn"] * cbc * ENTRY
                        out.append(
                            (
                                (a, (p["gm"] + 2) * cbc),
                                dx * cbc,
                                (b, p["gn"] * cbc),
                                cbc,
                            )
                        )
                        c += 1
        else:
            raise ValueError(f"no cluster lowering for {item.kind!r}")
        return out

    def _tile(self, index, p, steps) -> Chunk:
        gm, gn, out = p["gm"], p["gn"], p["c_at"]
        biased = p.get("bias_at") is not None
        ops, done = [], []
        cycles = 0.0
        emit = False
        self._forget_scratch()
        if biased and not all(self.ones):
            for bank in (0, 1):
                ops.append(Fill(p["ones_at"], gm, sel=0, eoff=gm * p["nk"], fbank=bank))
            self.ones = [True, True]
        for n, ((akey, aent), aoff, (bkey, bent), nk) in enumerate(steps):
            filled = len(ops)
            abank = self._operand(0, akey, akey, aent, ops)
            bbank = self._operand(1, bkey, bkey, bent, ops)
            moved = sum(f.n for f in ops[filled:] if isinstance(f, Fill)) * ENTRY
            last = n == len(steps) - 1 and not biased
            emit = last and ISA.can_emit(int(n > 0), nk)
            if last:
                self._release(ops, done)
            ops.append(
                Gemm(
                    gm,
                    gn,
                    nk,
                    acc=n > 0,
                    aoff=aoff,
                    abank=abank,
                    bbank=bbank,
                    emit=emit,
                    addr=out,
                )
            )
            self.last[0], self.last[1] = abank, bbank
            cycles += max(gm * gn * nk / 2, moved / FILL_BYTES_A_CYCLE)
        if biased:
            q = 1 - self.last[1]
            nk = p["nk"]
            ops.append(Fill(p["bias_at"], gn, sel=1, eoff=gn * nk, fbank=q))
            self.content[1][q] = None
            self._release(ops, done)
            ops.append(
                Gemm(
                    gm,
                    gn,
                    1,
                    acc=True,
                    aoff=gm * nk,
                    boff=gn * nk,
                    abank=1 - self.last[0],
                    bbank=q,
                    emit=True,
                    addr=out,
                )
            )
            self.last[0], self.last[1] = 1 - self.last[0], q
            emit = True
            cycles += gm * gn / 2
        drain = Drain(out, gm * gn, fuse=emit)
        if emit and p.get("late", True):
            self.held = (drain, index)
        else:
            ops.append(drain)
            done.append(index)
        return Chunk(ops, tuple(done), cycles)

    def _gemm(self, index, p) -> Chunk:
        """One sweep, its result drained (never emitted): `a`/`b` are
        ``(addr, entries, keep)``."""
        ops, done = [], []
        self._release(ops, done)
        self._forget_scratch()
        a, b = p["a"], p["b"]
        abank = self._operand(0, (a[0], a[1]), a[0], a[1], ops, keep=a[2])
        bbank = self._operand(1, (b[0], b[1]), b[0], b[1], ops, keep=b[2])
        ops.append(Gemm(p["gm"], p["gn"], p["nk"], abank=abank, bbank=bbank))
        self.last[0], self.last[1] = abank, bbank
        ops.append(Drain(p["c_at"], p["gm"] * p["gn"]))
        done.append(index)
        return Chunk(ops, tuple(done), p["gm"] * p["gn"] * p["nk"] / 2 + 400)

    def finish(self) -> list:
        if self.held is None:
            return []
        drain, index = self.held
        self.held = None
        return [Chunk([drain], (index,), 0.0)]


# --------------------------------------------------------------- vector core
@cache
def _stream_programs(body: tuple, words: int) -> tuple:
    """The stream images for a body spec, and the L1 walk and resident size."""
    kind = body[0]
    if kind == "silu":
        _, group, sets = body
        dims = ((1, SI.SLICE),)
        progs = stream.programs(
            SI.head(), lambda s: SI.body(s[0], words, group, sets), words, dims
        )
        return tuple(progs), dims, 0, 1, ()
    if kind == "binary":
        _, op, group, sets = body
        dims = ((1, BI.SLICE),)
        progs = stream.programs(
            BI.head(),
            lambda s: BI.body(s, words, op, group, sets),
            words,
            dims,
            inputs=2,
        )
        return tuple(progs), dims, 0, 2, ()
    if kind == "softmax":
        _, cols, rows, block = body
        w = cols // 16
        dims = ((w, SM.ROWS),)

        def sm_body(s):
            bases = [s[0] + k * SM.ROWS * w for k in range(rows // SM.ROWS)]
            return [
                x
                for g in range(0, len(bases), SM.STEPS)
                for x in SM.steps(bases[g : g + SM.STEPS], w, block)
            ]

        return tuple(stream.programs(SM.head(), sm_body, words, dims)), dims, 0, 1, ()
    if kind == "layernorm":
        _, cols, rows, eps = body
        w = cols // 16
        res = stream.resident_at(words, 1)
        dims = ((w, LN.ROWS),)

        def ln_body(s):
            return [
                x
                for k in range(rows // LN.ROWS)
                for x in LN.step(s[0] + k * LN.ROWS * w, w, res)
            ]

        progs = stream.programs(
            LN.head(cols, eps),
            ln_body,
            words,
            dims,
            resident=2 * w,
            walks={LN.AD_B: LN.BROADCAST},
        )
        setup = (LN.Desc(LN.AD_B, 0), LN.Dims(LN.AD_B, LN.BROADCAST))
        return tuple(progs), dims, 2 * w, 1, setup
    raise ValueError(f"no vector body {kind!r}")


@cache
def _run_cycles(body: tuple, words: int) -> float:
    progs, dims, resident, inputs, _ = _stream_programs(body, words)
    walks = {LN.AD_B: LN.BROADCAST} if body[0] == "layernorm" else None
    return vsched.cycles(progs[1], stream.l1_map(dims, words, inputs, resident, walks))


class VectorLowerer:
    """A vector core: streams, and attention's RUNs over its local storage."""

    def __init__(self, coord) -> None:
        self.coord = coord
        self.attention = None  # the (gm, p16, o, idx) its images were set up for

    def lower(self, index, item) -> list:
        p = item.params
        if item.kind == "vec_stream":
            body, words = tuple(p["body"]), p["words"]
            progs, dims, resident, _, setup = _stream_programs(body, words)
            ops, _ = stream.stream_ops(
                progs,
                dims,
                words,
                p["srcs"],
                p["dst"],
                p["runs"],
                p["step"],
                resident_src=p.get("resident_at"),
                resident=resident,
                setup=setup,
                sink=p.get("sink"),
            )
            cycles = _run_cycles(body, words) * p["runs"] + 600
            return [Chunk(ops, (index,), cycles)]
        if item.kind == "vec_run":
            gm = p["gm"]
            want = (gm, p["p16_at"], p["o_at"], p["idx_at"])
            ops = []
            if self.attention != want:
                ops += AT.setup_ops(gm, p["p16_at"], p["o_at"], p["idx_at"])
                self.attention = want
            ops += AT.run_ops(gm, p["run"], p.get("in_at"))
            cost = {"init": 300, "softmax": 4100, "update": 1300, "final": 700}
            return [Chunk(ops, (index,), float(cost[p["run"]]))]
        raise ValueError(f"no vector lowering for {item.kind!r}")

    def finish(self) -> list:
        return []


def mover(item) -> list:
    """A mover item's register writes."""
    if item.kind == "quantise":
        p = item.params
        return PM.convert(p["src"], p["dst"], p["entries"])
    raise ValueError(f"no mover lowering for {item.kind!r}")


LOWERERS = {"MG": ClusterLowerer, "VC": VectorLowerer}
