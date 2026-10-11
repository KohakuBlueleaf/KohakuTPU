"""L2 -> L1 for bodies with cluster instances: gemm tiles and vector tasks.

A tile over several K chunks streams out of its last sweep (`emit=`) and is
collected by a fused drain in the cluster's next command: its fills and
sweeps but the last, the previous tile's fused drain, a mark (that tile
drained), the last sweep. A one-chunk tile cannot emit: its command is its
fills, its sweep and a plain drain, then a mark. The node body runs in
rounds: round r sends every cluster's r-th tile (after waiting for the vector
tasks it reads), then every vector task whose tiles were drained `lag` rounds
back, so clusters have work queued before the dispatcher blocks.
"""

from kohakutpu.language.l2 import nodes as L
from kohakutpu.language.lower.l2.emit import LowerError
from kohakutpu.language.lower.l2.fused import flash, library
from kohakutpu.language.lower.l2.fused.epilogue import Epilogue
from kohakutpu.language.lower.l2.fused.model import overlaps
from kohakutpu.language.lower.l2.fused.tasks import Unroll
from kohakutpu.language.text.writer import Text


def _addr(ref) -> str:
    if isinstance(ref, str):
        return ref
    name, off = ref
    return f"{name} + {off}" if off else name


class _Node:
    def __init__(self, node: Text, units: Unroll, lag) -> None:
        self.node, self.u, self.lag = node, units, lag
        self.round = 0
        self.round_of: dict = {}
        self.marks = 0
        #: unit -> index of the latest mark waited for.
        self.waited: dict = {}
        self.mark_at: dict = {}
        #: cluster -> its tile still streaming out, for the next command's drain.
        self.open: dict = {}
        #: cluster -> chunks filled so far (the next fill's bank parity).
        self.bank: dict = {}

    def mark(self, unit: str) -> str:
        name = f"e{self.marks}"
        self.mark_at[name] = (unit, self.marks)
        self.round_of[name] = self.round
        self.marks += 1
        self.node(f"{name} = mark {unit}")
        return name

    def wait(self, tokens) -> None:
        latest: dict = {}
        for t in tokens:
            unit, n = self.mark_at[t]
            if n > latest.get(unit, (None, -1))[1]:
                latest[unit] = (t, n)
        for unit, (t, n) in sorted(latest.items()):
            if n > self.waited.get(unit, -1):
                self.node(f"wait {unit} upto={t}")
                self.waited[unit] = n

    def tile(self, t) -> None:
        unit = f"mg[{t.unit}]"
        deps = [
            v
            for v in self.u.vec
            if any(overlaps(w, r) for w in v.writes for r in t.reads)
        ]
        for v in deps:
            if v.done is None:
                self.force(v)
        self.wait([v.done for v in deps])
        a, b = t.a, t.b
        fa, fb = 2 * t.gm, 2 * t.gn
        sa, sb = 4 * t.gm * 64, 4 * t.gn * 64
        prev = self.open.pop(t.unit, None)
        last = t.chunks - 1
        # Banks alternate across the cluster's chunks, tile to tile, so a fill
        # never lands in the bank the sweep before it reads.
        bank = self.bank.get(t.unit, 0)
        self.bank[t.unit] = bank + t.chunks
        self.node(f"send {unit}")
        with self.node.block():
            for k in range(t.chunks):
                x = (bank + k) % 2
                self.node(f"fill A{x} <- {_addr((a[0], a[1] + k * sa))} n={fa}")
                self.node(f"fill B{x} <- {_addr((b[0], b[1] + k * sb))} n={fb}")
                if k < last or not last:
                    acc = " acc" if k else ""
                    self.node(f"gemm {t.gm}x{t.gn}x2 a=A{x} b=B{x}{acc}")
            if prev is not None:
                self.node(f"drain {_addr(prev.out)} n={prev.gm * prev.gn} fused")
            if not last:
                self.node(f"drain {_addr(t.out)} n={t.gm * t.gn}")
        if prev is not None or not last:
            mk = self.mark(unit)
            if prev is not None:
                prev.drained = mk
            if not last:
                t.drained = mk
        if last:
            x = (bank + last) % 2
            self.node(f"send {unit}")
            with self.node.block():
                self.node(f"gemm {t.gm}x{t.gn}x2 a=A{x} b=B{x} acc emit={_addr(t.out)}")
            self.open[t.unit] = t

    def close(self, cluster: int) -> None:
        prev = self.open.pop(cluster, None)
        if prev is None:
            return
        unit = f"mg[{cluster}]"
        self.node(f"send {unit}")
        with self.node.block():
            self.node(f"drain {_addr(prev.out)} n={prev.gm * prev.gn} fused")
        prev.drained = self.mark(unit)

    def force(self, v) -> None:
        """Send `v` now: a tile about to be sent reads what it writes."""
        for t in v.tiles:
            if t.drained is None:
                self.close(t.unit)
        for w in v.after:
            if w.done is None:
                self.force(w)
        if any(t.drained is None for t in v.tiles):
            raise LowerError("a tile reads what a vector task writes from later tiles")
        self.pending.remove(v)
        self.vector(v)

    def vector(self, v) -> None:
        self.wait([t.drained for t in v.tiles] + [w.done for w in v.after])
        unit = f"vc[{v.unit}]"
        self.node(f"send {unit}")
        with self.node.block():
            self.node(f"run {v.image}({', '.join(_addr(x) for x in v.args)})")
        v.done = self.mark(unit)

    def run(self, order: str) -> None:
        seqs: dict = {}
        for t in self.u.tiles:
            seqs.setdefault(t.unit, []).append(t)
        self.pending = list(self.u.vec)
        self.next = {unit: 0 for unit in seqs}
        if order == "consumer":
            self.consumer(seqs)
        rounds = max(len(s) for s in seqs.values())
        for r in range(rounds):
            self.round = r
            for unit in sorted(seqs):
                if self.next[unit] <= r < len(seqs[unit]):
                    self.tile(seqs[unit][r])
                    self.next[unit] = r + 1
            self.ready(r)
        self.round = rounds
        for unit in sorted(seqs):
            self.close(unit)
        self.ready(
            rounds + max(self.lag_of(v) for v in self.u.vec) if self.u.vec else rounds
        )
        if self.pending:
            raise LowerError("vector tasks left with undrained tiles")

    def depth(self, v, memo: dict) -> int:
        """0 for a task reading no other vector task's output (directly or
        through a tile), else one more than the deepest it reads."""
        if id(v) not in memo:
            memo[id(v)] = 0
            deps = list(v.after)
            for t in v.tiles:
                deps += [
                    w
                    for w in self.u.vec
                    if any(overlaps(x, r) for x in w.writes for r in t.reads)
                ]
            memo[id(v)] = max(
                (1 + self.depth(w, memo) for w in deps if w is not v), default=0
            )
        return memo[id(v)]

    def consumer(self, seqs: dict) -> None:
        """By dependence depth, then each core's order: level k sends the
        tiles every core's k-th task of the depth reads (and the tiles before
        them on their clusters), then those tasks."""
        memo: dict = {}
        by_depth: dict = {}
        for v in self.u.vec:
            by_depth.setdefault(self.depth(v, memo), []).append(v)
        levels = []
        for _, tasks in sorted(by_depth.items()):
            cores: dict = {}
            for v in tasks:
                cores.setdefault(v.unit, []).append(v)
            for k in range(max(len(q) for q in cores.values())):
                levels.append([q[k] for _, q in sorted(cores.items()) if k < len(q)])
        for level in levels:
            for v in level:
                for t in v.tiles:
                    seq = seqs[t.unit]
                    while self.next[t.unit] <= seq.index(t):
                        self.tile(seq[self.next[t.unit]])
                        self.next[t.unit] += 1
            for v in level:
                if v in self.pending:
                    self.force(v)

    def lag_of(self, v) -> int:
        return v.lag if self.lag is None else self.lag

    def ready(self, r: int) -> None:
        """Send the vector tasks whose tiles were drained `lag` rounds back."""
        for v in list(self.pending):
            if v not in self.pending:
                continue
            ok = all(
                t.drained and self.round_of[t.drained] <= r - self.lag_of(v)
                for t in v.tiles
            )
            if ok and all(w.done for w in v.after):
                self.pending.remove(v)
                self.vector(v)


def bound(u: Unroll) -> str:
    """`vector` when the busiest core's lane work (lane-element ops / 16)
    exceeds the busiest cluster's multiply-adds / 1024, else `cluster`."""
    core: dict = {}
    for v in u.vec:
        core[v.unit] = core.get(v.unit, 0) + v.work / 16
    cl: dict = {}
    for t in u.tiles:
        macs = (4 * t.gm) * (4 * t.gn) * (64 * t.chunks)
        cl[t.unit] = cl.get(t.unit, 0) + macs / 1024
    return (
        "vector"
        if max(core.values(), default=0) > max(cl.values(), default=0)
        else "cluster"
    )


def lower(
    module,
    name,
    body: L.Body,
    out: Text,
    node: Text,
    lag: int | None = None,
    order: str | None = None,
    transposed=(),
) -> list:
    """Images into `out`, the node body into `node`; the L1 parameters.
    `lag`: rounds a vector task waits past its last drain; by default an
    epilogue's is one less than the tiles it reads (MLP 0 and SwiGLU 1 measure
    best). `order`: `rounds` (every cluster's next tile in turn) or `consumer`
    (the tiles of each core's next vector task first); by default
    `consumer` when the vector cores bound the kernel. `transposed`:
    parameters the host holds transposed (`t=1`)."""
    heads = flash.match(body)
    if heads:
        return flash.lower(body, heads, out, node)
    u = Unroll(body)
    if order is None:
        order = "consumer" if bound(u) == "vector" else "rounds"
    forms = {}
    for v in u.vec:
        if v.inst is None:
            continue
        names, ops, result = v.inst
        key = (tuple(names), tuple(ops), result)
        if key not in forms:
            img = f"{name}_epi{len(forms)}"
            forms[key] = img
            Epilogue(img, list(names), list(ops), result).text(out)
        v.image = forms[key]
    if u.library:
        for line in library.text(u.library).splitlines():
            out(line)
        out("")
    _Node(node, u, lag).run(order)
    params = [(n, u.layouts.typed(n)) for n, _ in body.params]
    params += [(n, u.layouts.typed(n)) for n in u.scratch]
    for n, slots in u.slots.items():
        rows, cols = u.drained_shape[n]
        t = L.Type("f16", (rows * len(slots), cols), "dram")
        u.layouts.types[n] = t
        u.layouts.want(n, tile=(rows // 4, cols // 4))
        params.append((n, u.layouts.typed(n)))
    return [
        (
            (n, L.Type(t.dtype, t.shape, t.space, (*t.layout, ("t", 1))))
            if n in transposed
            else (n, t)
        )
        for n, t in params
    ]


__all__ = ["lower"]
