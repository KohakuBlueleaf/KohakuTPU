"""A cluster's instruction stream reordered so result writes overlap the next tile.

A fused DRAIN only waits for the sub-tiles its emitting sweep already handed out
(mx_cluster_node_pump, `drain_fused`), and the next emitting GEMM waits for the
same thing before it opens a batch. In between, the next tile's FILLs and
non-emitting sweeps can run while those writes leave -- measured on the v9 card,
1024x512x1024: DRAIN 24-30% of cluster time -> 5-6%, 66.4% -> 70.9% of peak.
"""

from kohakutpu.isa.cluster import ISA, OP_DRAIN, OP_FILL, OP_GEMM


def _op(word: int) -> int:
    return (word >> 252) & 0xF


def node_drain(word: int) -> bool:
    """Whether `word` is a DRAIN into a NoC node (a fused epilogue's transfer)."""
    return _op(word) == OP_DRAIN and bool(ISA.DRAIN.decode(word).get("dnode", 0))


def late_drains(words: list[int]) -> list[int]:
    """`words` with each fused DRAIN moved to just before the next emitting GEMM.

    A DRAIN never moves past a FILL into a bank the emitting sweep reads while
    that sweep may still run, nor past any other DRAIN. Any later GEMM waits for
    the sweep's issue to finish (mx_cluster_cu S_GEMM), so past one the banks
    are free. The words themselves are unchanged; only fused DRAINs move.
    """
    out: list[int] = []
    pending = None
    reads = None  # (A bank, B bank) the last emitting sweep may still read
    for w in words:
        op = _op(w)
        if op == OP_DRAIN:
            fused = ISA.DRAIN.decode(w).get("fuse", 0)
            if pending is not None:
                out.append(pending)
                pending = None
            if fused and reads is not None:
                pending = w
                continue
        elif pending is not None:
            if op == OP_GEMM and ISA.GEMM.decode(w).get("emit", 0):
                out.append(pending)
                pending = None
            elif op == OP_FILL and reads is not None:
                f = ISA.FILL.decode(w)
                if f.get("fbank", 0) == reads[f.get("sel", 0)]:
                    out.append(pending)
                    pending = None
        if op == OP_GEMM:
            g = ISA.GEMM.decode(w)
            reads = (g.get("abank", 0), g.get("bbank", 0)) if g.get("emit", 0) else None
        out.append(w)
    if pending is not None:
        out.append(pending)
    return out
