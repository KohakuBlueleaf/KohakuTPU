"""A cluster instance's L2 ops to its L1 words (docs/spec/l2-program.md §5).

    a = fill VIEW : mx7a[M, K] @A          FILLs of the view's runs into a bank
    b = fill VIEW : mx7b[N, K] @B
    a = recv vc[H] : mx7a[M, K] @A         what core H sent into this cluster
    c = zero : f32[M, N] @acc              the next gemm into c opens the tile
    c = gemm a, b[, c] : f32[M, N] @acc    one sweep (acc: chained into c)
    drain c -> VIEW                        the tile out as f16 sub-tiles
    drain c -> vc[H]                       ... into core H's L1 (a `recv` there)

Allocation: a fill takes the bank of its side that holds no live value and
that the latest sweep does not read. Order: program order; a drain of a tile
whose last sweep may emit (`ISA.can_emit`) becomes that sweep's `emit=` plus a
fused DRAIN placed just before the unit's next emitting sweep (it holds the
sequencer until its sub-tiles have left), else a plain DRAIN after the sweep.
"""

from dataclasses import dataclass, field

from kohakutpu.compiler.isa.cluster import ISA
from kohakutpu.compiler.l2 import memory as M
from kohakutpu.compiler.l2 import sync
from kohakutpu.compiler.l2.body import Op, value
from kohakutpu.language.text.syntax import Arrow, Int, Name, View

BANK = 256
ACC_SUBTILES = 4096
MAX_FILL = ISA.cfg.max_fill


@dataclass
class Line:
    """One L1 cluster word as text, and the item (segment) it came from.
    A fill of a shared view carries `share` (its mode) and `key` (the view, to
    match across instances); `peers` is set by whoever groups the instances."""

    text: str
    seg: int
    gemm: dict | None = None  # a sweep: its fields; `emit` set by a later drain
    share: str | None = None
    key: tuple | None = None
    peers: tuple = ()
    fill: bool = False
    #: a drain added into cluster `into`'s accumulator
    into: int | None = None
    drain: bool = False

    def render(self) -> str:
        if not self.peers:
            return self.text
        return f"{self.text} peers=({', '.join(f'mg[{p}]' for p in self.peers)})"


@dataclass
class Val:
    kind: str  # "A" | "B" | "acc"
    shape: tuple
    bank: int = 0
    nk: int = 0
    gemm: Line | None = None  # acc: the sweep that last wrote it
    zero: bool = False
    at: int = -1  # the item that defined it
    entry: int = 0  # A / B: its first entry in the bank

    def span(self) -> tuple:
        """Entries of both banks of its side it occupies."""
        lo = self.bank * BANK + self.entry
        return lo, lo + (self.shape[0] // 4) * self.nk


@dataclass
class Stream:
    """One cluster's words: `lines`, cut into segments for the node to
    interleave, and fused drains to place; what they touch (`accesses`) and
    the bank of each side its last sweep reads (`reading`)."""

    unit: str = ""
    lines: list = field(default_factory=list)
    held: list = field(default_factory=list)  # (emitting gemm Line, drain text)
    accesses: list = field(default_factory=list)
    reading: dict = field(default_factory=dict)


def lower(body, inst, fail, fuse: bool = True, links=None, reading=None) -> Stream:
    """`inst`'s items (``(Op, env)``) as a `Stream`; `fail(st, msg)` refuses.
    Without `fuse` every drain is plain: a tile another cluster adds into
    must not leave before that addition. `links` pairs transfers with their
    `recv`s; `reading` is the banks the unit's previous stage left read."""
    vals: dict = {}
    s = Stream(f"mg[{inst.index}]")
    s.reading = dict(reading or {"A": None, "B": None})
    seg = 0
    cuts = set(inst.cuts)
    uses = _last_uses(inst.items)
    for i, (op, env) in enumerate(inst.items):
        if i in cuts and i:
            seg += 1
        if not isinstance(op, Op):
            fail(op.st, "a cluster runs ops only")
        st = op.st
        if op.op == "fill":
            _fill(body, s, vals, uses, i, op, env, seg, fail)
        elif op.op == "recv":
            _recv(s, vals, op, env, body, links, fail, i)
        elif op.op == "zero":
            t = _ty(op, fail, "acc")
            _acc_fits(op, t.shape, fail)
            vals[op.target] = Val("acc", t.shape, zero=True)
        elif op.op == "gemm":
            _gemm(s, vals, op, seg, fail)
        elif op.op == "drain":
            _drain(body, s, vals, op, env, seg, fail, fuse, links)
        else:
            fail(st, f"no cluster op {op.op!r}: fill, recv, zero, gemm, drain")
    return _place_held(s)


def _ty(op, fail, space):
    t = op.type
    if t is None or op.target is None:
        fail(op.st, f"NAME = {op.op} ... : TYPE @{space}")
    if t.space != space:
        fail(op.st, f"{op.op} makes a value @{space}, not @{t.space}")
    return t


def _acc_fits(op, shape, fail):
    m, n = shape
    if m % 4 or n % 4 or (m // 4) * (n // 4) > ACC_SUBTILES:
        fail(
            op.st,
            f"f32[{m}, {n}] @acc: whole 4x4 sub-tiles, at most {ACC_SUBTILES}",
        )


def _last_uses(items) -> dict:
    """Item index of a definition -> index of the last item reading that
    value (before the name is bound again); a value never read maps to its
    own definition."""
    out, defs = {}, {}
    for i, (op, _) in enumerate(items):
        for a in op.args:
            if isinstance(a, Name) and a.text in defs:
                out[defs[a.text]] = i
        if op.target is not None:
            defs[op.target] = i
            out.setdefault(i, i)
    return out


SHARE = ("port", "all", "cross")


def _fill(body, s, vals, uses, i, op, env, seg, fail):
    reading = s.reading
    pos = op.st.positional()
    kw = op.st.kwargs()
    if len(pos) == 1:
        pos = [value(body, op.st, pos[0], env)]
        if isinstance(pos[0], Name):
            pos = [View(pos[0].text, ())]
    if len(pos) != 1 or not isinstance(pos[0], View) or set(kw) - {"share"}:
        fail(
            op.st,
            "NAME = fill VIEW [share=port|all|cross] : mx7a[M, K] @A | mx7b[N, K] @B",
        )
    share = None
    if "share" in kw:
        if not isinstance(kw["share"], Name) or kw["share"].text not in SHARE:
            fail(op.st, f"share= {' | '.join(SHARE)}")
        share = kw["share"].text
    side = op.type.space if op.type else None
    t = _ty(op, fail, side if side in ("A", "B") else "A")
    want = {"A": "mx7a", "B": "mx7b"}[t.space]
    if t.dtype != want:
        fail(op.st, f"@{t.space} holds {want}, not {t.dtype}")
    rows, k = t.shape
    if rows % 4 or k % 32:
        fail(
            op.st, f"{want}[{rows}, {k}]: rows whole groups of 4, K whole blocks of 32"
        )
    nk = k // 32
    if (rows // 4) * nk > BANK:
        fail(
            op.st,
            f"{want}[{rows}, {k}] is {(rows // 4) * nk} entries; a bank holds {BANK}",
        )
    view = value(body, op.st, pos[0], env)
    buf = body.buffers.get(view.name)
    if buf is None:
        fail(op.st, f"no buffer {view.name}")
    try:
        ax = M.axes(view, buf.shape)
        if M.shape_of(ax) != t.shape:
            fail(op.st, f"the view is {M.shape_of(ax)}, the value {t.shape}")
        runs = M.fill_runs(buf, ax)
    except M.ViewError as e:
        fail(op.st, str(e))
    live = {
        v.bank for v in vals.values() if v.kind == t.space and uses.get(v.at, -1) > i
    }
    free = [b for b in (0, 1) if b not in live and b != reading[t.space]]
    if not free:
        fail(op.st, f"both {t.space} banks hold values still read")
    # Alternate: the bank the latest value of this side does not hold.
    prev = [v.bank for v in vals.values() if v.kind == t.space]
    bank = (
        free[0] if not prev or prev[-1] not in free or len(free) == 1 else 1 - prev[-1]
    )
    key = (view.name, tuple(runs)) if share else None
    entries = (rows // 4) * nk
    s.accesses += [
        sync.Access(
            s.unit, True, (s.unit, t.space), bank * BANK, bank * BANK + entries
        ),
        sync.Access(s.unit, True, (s.unit, "rx"), 0, 1),
    ]
    for off, n, eoff in runs:
        s.accesses.append(sync.mem(s.unit, False, view.name, off, off + n * M.ENTRY))
        while n:
            part = (
                n if n <= MAX_FILL else (n + 1) // 2 if n <= 2 * MAX_FILL else MAX_FILL
            )
            at = f" at={eoff}" if eoff else ""
            s.lines.append(
                Line(
                    f"fill {t.space}{bank} <- {view.name} + {off} n={part}{at}",
                    seg,
                    share=share,
                    key=key,
                    fill=True,
                )
            )
            off, n, eoff = off + part * M.ENTRY, n - part, eoff + part
    vals.pop(op.target, None)
    vals[op.target] = Val(t.space, t.shape, bank=bank, nk=nk, at=i)


def _recv(s, vals, op, env, body, links, fail, i):
    """`a = recv vc[H] : mx7a[M, K] @A`: the oldest transfer core H made
    into this cluster's side, not yet received."""
    t = op.type
    pos = op.st.positional()
    if t is None or t.space not in ("A", "B") or len(pos) != 1 or links is None:
        fail(op.st, "NAME = recv vc[H] : mx7a[M, K] @A | mx7b[N, K] @B")
    src = value(body, op.st, pos[0], env)
    if (
        not isinstance(src, View)
        or src.name != "vc"
        or len(src.slices) != 1
        or not isinstance(src.slices[0], Int)
    ):
        fail(op.st, "a cluster receives from a core: recv vc[H]")
    x = links.take(op.st, f"vc[{src.slices[0].value}]", s.unit, t.space)
    k = t.shape[1]
    want = {"A": "mx7a", "B": "mx7b"}[t.space]
    if t.dtype != want or x.shape != t.shape:
        fail(
            op.st,
            f"core sent {x.shape} into @{t.space}; this receives {t.dtype}{list(t.shape)}",
        )
    bank, entry = divmod(x.at, BANK)
    v = Val(t.space, t.shape, bank=bank, nk=k // 32, at=i, entry=entry)
    vals[op.target] = v
    s.accesses.append(sync.Access(s.unit, False, (s.unit, t.space), *v.span()))


def _gemm(s, vals, op, seg, fail):
    reading = s.reading
    t = _ty(op, fail, "acc")
    names = [a.text for a in op.args if isinstance(a, Name)]
    if len(names) != len(op.args) or len(names) not in (2, 3):
        fail(op.st, "NAME = gemm A, B[, ACC] : f32[M, N] @acc")
    a, b = (vals.get(n) for n in names[:2])
    if a is None or a.kind != "A" or b is None or b.kind != "B":
        fail(op.st, "gemm reads an @A value and an @B value of this cluster")
    m, n = t.shape
    _acc_fits(op, t.shape, fail)
    if a.shape[0] != m or b.shape[0] != n or a.nk != b.nk:
        fail(op.st, f"gemm of {a.shape} and {b.shape} into {t.shape}")
    if not ISA.legal_nk(a.nk):
        fail(
            op.st,
            f"K = {a.nk * 32}: a sweep takes 1 block or a whole number of {ISA.cfg.kstep}",
        )
    acc = 0
    if len(names) == 3:
        c = vals.get(names[2])
        if c is None or c.kind != "acc" or c.shape != t.shape:
            fail(op.st, f"{names[2]} is not an f32{list(t.shape)} @acc value")
        acc = 0 if c.zero else 1
    fields = {
        "gm": m // 4,
        "gn": n // 4,
        "nk": a.nk,
        "ab": a.bank,
        "bb": b.bank,
        "acc": acc,
        "aoff": a.entry,
        "boff": b.entry,
    }
    line = Line("", seg, gemm=fields)
    s.lines.append(line)
    reading["A"], reading["B"] = a.bank, b.bank
    s.accesses += [
        sync.Access(s.unit, False, (s.unit, "A"), *a.span()),
        sync.Access(s.unit, False, (s.unit, "B"), *b.span()),
    ]
    vals[op.target] = Val("acc", t.shape, gemm=line)


def _drain(body, s, vals, op, env, seg, fail, fuse, links):
    pos = op.st.positional()
    add = Name("add") in pos
    pos = [p for p in pos if p != Name("add")]
    if len(pos) != 3 or not isinstance(pos[0], Name) or pos[1] != Arrow("->"):
        fail(op.st, "drain ACC -> VIEW | drain ACC -> vc[H] | drain ACC -> mg[J] add")
    c = vals.get(pos[0].text)
    if c is None or c.kind != "acc" or c.gemm is None:
        fail(op.st, f"{pos[0].text} is no swept @acc value of this cluster")
    view = value(body, op.st, pos[2], env)
    if isinstance(view, Name):
        view = View(view.text, ())
    n = (c.shape[0] // 4) * (c.shape[1] // 4)
    if isinstance(view, View) and view.name in ("mg", "vc"):
        if len(view.slices) != 1 or not isinstance(view.slices[0], Int):
            fail(op.st, f"drain ACC -> {view.name}[J]: J a constant per instance")
        j = view.slices[0].value
        if view.name == "vc":
            if add or links is None:
                fail(op.st, "drain ACC -> vc[H]: the tile's sub-tiles into core H's L1")
            to = f"vc[{j}]"
            at = links.send(op.st, s.unit, to, "l1", n, c.shape).at
            s.lines.append(Line(f"drain n={n} to={to} l1={at}", seg, drain=True))
            s.accesses.append(sync.Access(s.unit, True, (to, "l1"), at, at + n, True))
            c.gemm = None
            return
        if not add:
            fail(
                op.st,
                "drain ACC -> mg[J] add: into cluster J's tile, at full precision",
            )
        s.lines.append(
            Line(f"drain n={n} to=mg[{j}] buf=2 l1=0", seg, into=j, drain=True)
        )
        s.accesses.append(sync.Access(s.unit, True, (f"mg[{j}]", "acc"), 0, 1, True))
        c.gemm = None
        return
    if add:
        fail(op.st, "`add` is a drain into another cluster's accumulator")
    if not isinstance(view, View) or view.name not in body.buffers:
        fail(op.st, f"drain to a memory view, got {view!r}")
    buf = body.buffers[view.name]
    try:
        ax = M.axes(view, buf.shape)
        if M.shape_of(ax) != c.shape:
            fail(op.st, f"the view is {M.shape_of(ax)}, the tile {c.shape}")
        off = M.drain_base(buf, ax)
    except M.ViewError as e:
        fail(op.st, str(e))
    g = c.gemm.gemm
    where = f"{view.name} + {off}"
    s.accesses.append(sync.mem(s.unit, True, view.name, off, off + n * M.SUBTILE))
    if fuse and ISA.can_emit(g["acc"], g["nk"]) and "emit" not in g:
        g["emit"] = where
        s.held.append((c.gemm, f"drain {where} n={n} fused"))
    else:
        s.lines.append(Line(f"drain {where} n={n}", seg, drain=True))
    c.gemm = None


def _place_held(s: Stream) -> Stream:
    """Each fused drain just before the unit's next emitting sweep, or last."""
    for line in s.lines:
        if line.gemm is not None:
            g = line.gemm
            emit = f" emit={g['emit']}" if "emit" in g else ""
            offs = "".join(f" {k}={g[k]}" for k in ("aoff", "boff") if g.get(k))
            line.text = (
                f"gemm {g['gm']}x{g['gn']}x{g['nk']} a=A{g['ab']} b=B{g['bb']} "
                f"acc={g['acc']}{offs}{emit}"
            )
    out = list(s.lines)
    for sweep, text in s.held:
        at = out.index(sweep)
        nxt = next(
            (
                j
                for j in range(at + 1, len(out))
                if out[j].gemm and "emit" in out[j].gemm
            ),
            None,
        )
        if nxt is None:
            out.append(Line(text, out[-1].seg))
        else:
            out.insert(nxt, Line(text, out[nxt].seg))
    s.lines = out
    return s


__all__ = ["Line", "Stream", "lower"]
