"""The matmul cluster's instructions, declared as a field table.

The authority is ``src/kohakutpu/matmul/mx_cluster_cu.v``. Field widths come
from :class:`IsaConfig`. Payload only, 256 bits; the routing header belongs to
the driver.
"""

from dataclasses import dataclass

from kohakuaccel.compiler.isa import Field, InstFormat, InstSet

OP_FILL = 1
OP_GEMM = 2
OP_DRAIN = 3


@dataclass(frozen=True)
class IsaConfig:
    """Field widths and machine constants the encoding depends on.

    `anchor` is ``2 * SBIAS``, which cancels both stored scale biases in
    ``sa + sb - anchor``. `max_fill` is 255 because the memory request's
    streaming count is 8 bits and 256 wraps to 0, which memory coerces to 1.
    `max_peers` is 3 because the RTL's emitter is a fixed four-destination mux.
    """

    payload_bits: int = 256
    op_bits: int = 4
    #: The address is SPLIT. `addr_bits` stays where it was and the high bits sit
    #: in a tail field, so widening moved no other field; see `_tail`.
    addr_bits: int = 34
    addr_hi_bits: int = 6
    n_bits: int = 16
    grid_bits: int = 8
    pos_bits: int = 4
    peer_bits: int = 24

    anchor: int = 40
    kblock: int = 32  # elements sharing one MXFP7 scale
    lanes: int = 4  # elements per sub-tile edge
    mem_word_bytes: int = 32

    max_fill: int = 255
    max_peers: int = 3
    #: K blocks one sweep issue carries: two on the double-pumped manager
    #: (mx_cluster_mgr_pump.v KSTEP), one on the single. The larger bounds both.
    kstep: int = 2


DEFAULT = IsaConfig()


def _tail(cfg: IsaConfig) -> list[Field]:
    """Every field below the opcode, shared by all three instructions."""
    g, p = cfg.grid_bits, cfg.pos_bits
    return [
        Field("addr", cfg.addr_bits, default=0, doc="operand or result byte address"),
        Field("n", cfg.n_bits, default=0, doc="entries to fill, sub-tiles to drain"),
        Field("sel", 1, default=0, doc="0 = L1 A, 1 = L1 B"),
        Field("acc", 1, default=0, doc="chain into the tile the last GEMM left"),
        Field("gm", g, default=0),
        Field("gn", g, default=0),
        # 1, not 0: a FILL or DRAIN carries it harmlessly, and the RTL benches'
        # payloads (scripts/py/gen_prog.py) were built with it so.
        Field("nk", g, default=1),
        Field("anchor", g, default=cfg.anchor),
        Field("peers", cfg.peer_bits, default=0, doc="{y,x} sharers, peer 0 low"),
        Field("npeer", 2, default=0),
        # Reserved; positional, so it holds `eoff` and every field below in
        # place.
        Field("rsvd141", 1, default=0, doc="reserved; MUST be 0"),
        Field("eoff", g, default=0, doc="where a FILL lands in L1"),
        Field("aoff", g, default=0, doc="where a sweep reads A"),
        Field("boff", g, default=0, doc="where a sweep reads B"),
        Field("emit", 1, default=0, doc="hand sub-tiles out as they complete"),
        Field("fuse", 1, default=0, doc="turn the following DRAIN into a barrier"),
        Field("abank", 1, default=0, doc="ninth bit of the A entry index"),
        Field("bbank", 1, default=0),
        Field("fbank", 1, default=0),
        Field("dnode", 1, default=0, doc="drain to a node rather than to memory"),
        Field("dst_x", p, default=0),
        Field("dst_y", p, default=0),
        Field("dbuf", g, default=0),
        Field("dflags", g, default=0),
        Field("dack_y", p, default=0),
        Field("dack_x", p, default=0),
        Field("dmesh", 2, default=0),
        Field("dfin", g, default=0, doc="{y,x} in the far mesh; nonzero = remote"),
        # LAST, so `addr`'s high bits cost no other field a bit position. All
        # zero is mesh 0 DRAM, which is what every pre-40-bit encoding carries.
        Field("addr_hi", cfg.addr_hi_bits, default=0, doc="addr[39:34]"),
    ]


class CuIsa:
    """The three cluster instructions, built for one :class:`IsaConfig`."""

    def __init__(self, cfg: IsaConfig = DEFAULT) -> None:
        self.cfg = cfg
        op = lambda code: [Field("op", cfg.op_bits, const=code)]
        self.FILL = InstFormat(
            "FILL",
            op(OP_FILL) + _tail(cfg),
            width=cfg.payload_bits,
            doc="stream `n` entries from `addr` into L1 A (sel=0) or B (sel=1)",
        )
        self.GEMM = InstFormat(
            "GEMM",
            op(OP_GEMM) + _tail(cfg),
            width=cfg.payload_bits,
            doc="sweep gm x gn sub-tiles over nk K-blocks",
        )
        self.DRAIN = InstFormat(
            "DRAIN",
            op(OP_DRAIN) + _tail(cfg),
            width=cfg.payload_bits,
            doc="write `n` sub-tiles to `addr`, or to a node when dnode",
        )
        self.set = InstSet("kohakutpu-cu", [self.FILL, self.GEMM, self.DRAIN])

    def fill(self, addr: int, n: int, sel: int = 0, **kw) -> int:
        """A FILL payload.

        Raises :class:`ValueError` above ``cfg.max_fill``: the count wraps to 0,
        which memory coerces to 1, so the unit waits forever for entries nobody
        asked for and the result is zeros with nothing reported.
        """
        if n > self.cfg.max_fill:
            raise ValueError(
                f"FILL of {n} entries exceeds the {self.cfg.max_fill}-entry "
                f"streaming count; split it"
            )
        return self.FILL.encode(n=n, sel=sel, **self.split_addr(addr), **kw)

    def gemm(self, gm: int, gn: int, nk: int, **kw) -> int:
        """A GEMM payload."""
        return self.GEMM.encode(gm=gm, gn=gn, nk=nk, **kw)

    def can_emit(self, acc: int, nk: int) -> bool:
        """Whether a GEMM may hand its sub-tiles out (`emit`).

        An emitting issue must not also OPEN its tile: there is no load-and-emit
        accumulator op, the load wins, and the fused DRAIN waits forever for
        sub-tiles that never come (mx_cluster_mgr_pump.v, `cmd_op`). A sweep's
        first issue opens the tile unless it accumulates, and it is also the
        last when `nk` fits one issue.
        """
        return bool(acc) or nk > self.cfg.kstep

    def legal_nk(self, nk: int) -> bool:
        """Whether a GEMM may sweep `nk` K-blocks: one, or a whole number of issues.

        MEASURED on card_v9_1n (pumped manager, two K-blocks an issue): an odd
        nk >= 3 ends its sweep in a single-block pass and sub-tile (0, 0) comes
        out wrong -- nk 3 and 5, every tile, every cluster; nk 1 and 2 exact.
        """
        return nk == 1 or nk % self.cfg.kstep == 0

    def drain(self, addr: int, n: int, **kw) -> int:
        """A DRAIN payload."""
        return self.DRAIN.encode(n=n, **self.split_addr(addr), **kw)

    def split_addr(self, addr: int) -> dict:
        """Split a 40-bit address into its two encoded fields.

        Returns ``{"addr": low, "addr_hi": high}``. Raises :class:`ValueError`
        if `addr` does not fit, because the silent alternative is an address
        that decodes as a different mesh or as a command aperture.
        """
        cfg = self.cfg
        total = cfg.addr_bits + cfg.addr_hi_bits
        if not 0 <= addr < (1 << total):
            raise ValueError(f"address {addr:#x} does not fit {total} bits")
        return {
            "addr": addr & ((1 << cfg.addr_bits) - 1),
            "addr_hi": addr >> cfg.addr_bits,
        }

    def peers(self, nodes) -> dict:
        """Encode up to ``cfg.max_peers`` sharer node indices as fields.

        The lowest index issues the memory descriptor and the rest receive.
        Raises :class:`ValueError` above ``cfg.max_peers``.
        """
        nodes = list(nodes)
        if len(nodes) > self.cfg.max_peers:
            raise ValueError(
                f"at most {self.cfg.max_peers} peers per fill, got {len(nodes)}"
            )
        packed = 0
        for i, node in enumerate(nodes):
            packed |= (node & 0xFF) << (i * 8)
        return {"peers": packed, "npeer": len(nodes)}


ISA = CuIsa()
