"""A vector-core instance's L2 ops to a V2 image (docs/spec/l2-program.md §5).

    gl = load g : f16[1, 128] @l1             before the loop: loaded once
    for t in 0..T                             the stream: a hardware loop
        xl = load VIEW : f16[R, 128] @l1      filled one iteration ahead
        v = unpack xl : f32[R, 128] @reg      one register a row
        e = mul v, -1.44 : f32[R, 128] @reg   one instruction a row
        m = rmax e : f32[R] @reg              the merge tree: lane r of chunk 0
        p = exp2d e, m[:, *] : f32[R, 128]    a statistic broadcast to its row
        ol = pack p : f16[R, 128] @l1
        store VIEW <- ol                      drained after the iteration's packs

A loop's loads and stores get two L1 buffers and the loop body is two
iterations; a view affine in the loop variable is a descriptor with `ainc`.
Rows go in groups (8 with a reduction, else 4): each op runs over the group's
rows before the next op, and a group's unpacks are issued before the previous
group's math. A result takes the register of a source read here for the last
time. `h = recv mg[U] : f16[R, C] @l1(gt4)` is a tile a cluster drained into
this L1 (before the loop; the loop unpacks row bands of it through the
unpack pointer); `send q -> mg[I] side=A` peer-drains packed MXFP7 rows into
a cluster's bank (a `recv` there).
"""

import re
from dataclasses import dataclass, field, replace

from kohakutpu.compiler.l2 import memory as M
from kohakutpu.compiler.l2 import sync
from kohakutpu.compiler.l2.body import For, If, Op, Par, When, const, span, value
from kohakutpu.language.text.syntax import Float, Int, Name, NewAxis, TextError, View

REGS = 32
L1_WORDS = 512
DESCS = 8
ROW = 128
UNARY = {"exp2": "vexp2", "inv": "vinv", "rsqrt": "vrsqrt", "log2": "vlog2"}
UNARY |= {"neg": "vneg", "abs": "vabs", "mov": "vmov"}
BINARY = {"add": "vadd", "sub": "vsub", "mul": "vmul", "max": "vmax", "min": "vmin"}
BINARY |= {"exp2d": "vexp2d"}
TERNARY = {"fma": "vfma", "fnma": "vfnma"}
#: `m = cmplt a, b`: 1.0 where a < b, else 0.0 (a compare into a predicate,
#: then a predicated move). `sel m, a, b`: a where m is not 0, else b.
CMP = {"cmplt": "vcmplt", "cmpgt": "vcmpgt", "cmpeq": "vcmpeq"}
REDUCE = {"rmax": "vmax", "rsum": "vadd"}
KREG = {0.0: "k0", 1.0: "k1", -1.0: "k2"}
#: The scalar registers of the loop count and of the sticky flag.
LOOP_S = 15
STK_S = 14


def _ops(body) -> list:
    """The ops of a stream loop's body, an `if sticky` block's included."""
    out = []
    for o in body:
        out += _ops(o.body) if isinstance(o, If) else [o]
    return out


@dataclass
class Val:
    kind: str  # "l1" | "row" | "stat"
    rows: int  # registers a row value takes (a GT4 register is 4 x 32)
    buf: int = 0  # l1: word offset
    regs: dict = field(default_factory=dict)  # row: register -> sym; stat: group -> sym
    mode: str = "f16"  # l1 / row: "f16" (one row a register) or "gt4"


def _mode(t) -> str:
    return "gt4" if t is not None and t.mode == "gt4" else "f16"


def _count(t) -> int:
    """Registers (or row-register slots) a value of type `t` takes: a row of
    128 each, or with GT4 a 4 x 32 block each; a statistic its rows."""
    if len(t.shape) == 2 and (_mode(t) == "gt4" or t.dtype in ("mx7a", "mx7b")):
        r, c = t.shape
        return r * c // ROW
    return t.shape[0]


def _broadcasts(o) -> bool:
    return any(
        isinstance(a, View) and a.slices and isinstance(a.slices[-1], NewAxis)
        for a in o.st.positional()
    )


class Gen:
    """Instructions: ``(template, syms)`` -- `{i}` in the template is the
    register of ``syms[i]`` -- or plain text."""

    def __init__(self) -> None:
        self.n = 0
        self.code: list = []

    def sym(self) -> str:
        self.n += 1
        return f"%{self.n}"

    def emit(self, tmpl: str, *syms) -> None:
        self.code.append((tmpl, syms))

    def raw(self, text: str) -> None:
        self.code.append(text)


@dataclass
class Desc:
    ad: int
    buffer: str | None  # None: a peer drain into a cluster
    off: int  # bytes into the buffer; a peer drain's first granule
    words: int
    inc: int
    buf: tuple  # L1 word offset per parity
    walk: str = ""
    to: str = ""  # a peer drain's cluster and its buffer id
    side: int = 0
    kind: str = "load"  # "load" | "store" | "send"
    kind_op: int = 0  # a send: id() of its op

    def extent(self) -> int:
        """Bytes one walk spans."""
        pairs = re.findall(r"\((-?\d+), (\d+)\)", self.walk) or [
            tuple(self.walk.split(", "))
        ]
        return 32 + sum((int(n) - 1) * int(s) for s, n in pairs)

    def span(self, trips: int) -> tuple:
        lo, hi = self.off, self.off + self.extent()
        step = self.inc * (trips - 1)
        return (lo + min(0, step), hi + max(0, step))


class Pressure(Exception):
    """More than the core's registers live at once."""


def lower(body, par: Par, env: dict, name: str, fail, links=None, land=None) -> tuple:
    """``(image text, its parameters, accesses)`` for one vector instance of
    `par` (`env` binds its variable): of the shapes whose registers fit -- how
    many row groups run together (`band`), whether a value is re-unpacked
    after a reduction rather than held (`remat`), whether the next band's
    unpacks go ahead -- the one `estimate` times fastest. `links` pairs its
    `recv`s and `send`s with the clusters'; its own L1 is below `land`."""
    par = _resolve(body, par, env)
    plan = _transfers(body, par, env, fail, links)
    best = _best(body, par, env, name, fail, plan, land)
    return best[0], best[1], best[3]


def _resolve(body, par, env):
    """`par` with each `when` kept or dropped for this instance (its
    condition is a constant of the instance; a loop variable is refused)."""

    def go(stmts):
        out = []
        for s in stmts:
            if isinstance(s, When):
                try:
                    keep = const(body, s.st, s.cond, env)
                except TextError:
                    body.reader.fail(
                        s.st, "a when in a vector instance is constant per instance"
                    )
                if keep:
                    out += go(s.body)
            elif isinstance(s, (For, If)):
                out.append(replace(s, body=go(s.body)))
            else:
                out.append(s)
        return out

    return replace(par, body=go(par.body))


def total(body, par, env, fail) -> int:
    """The issue model's cycles over every trip of the shape `lower` picks
    (the retile pass compares row tiles by it); transfers land where no
    other unit's code decides."""
    par = _resolve(body, par, env)
    plan = _transfers(body, par, env, fail, _Alone())
    me = _unit(body, par, env)
    land = L1_WORDS - _Alone.POOL if any(x.dst == me for x in plan.values()) else None
    return _best(body, par, env, "k", fail, plan, land)[4]


@dataclass
class _Landing:
    src: str
    dst: str
    side: str
    at: int
    size: int
    shape: tuple


class _Alone:
    """Transfers for costing one vector instance by itself: a `recv` lands
    at the top of the L1, a `send` at a bank's first entry."""

    POOL = 256

    def take(self, st, src, dst, side):
        return None

    def send(self, st, src, dst, side, size, shape):
        return _Landing(src, dst, side, 0, size, tuple(shape))


def _best(body, par, env, name, fail, plan, land) -> tuple:
    best = None
    # On a tie the narrower band with its unpacks ahead wins: the model does
    # not see the lookahead, and on the card it is worth 10% (silu, band 1).
    for band in (1, 2, 4, 8, 16):
        for remat in (False, True):
            for ahead in (True, False):
                try:
                    got = _lower(
                        body, par, env, name, fail, band, remat, ahead, plan, land
                    )
                except Pressure:
                    continue
                if best is None or got[2] < best[2]:
                    best = got
    if best is None:
        fail(par.st, "the stream loop needs more than 32 vector registers")
    return best


def _unit(body, par, env) -> str:
    return f"vc[{value(body, par.st, par.index, env).value}]"


def _transfers(body, par, env, fail, links) -> dict:
    """``{id(op): Transfer}`` for each `recv` (the oldest transfer into this
    core from that cluster) and each `send` (its landing, allocated now)."""
    out: dict = {}
    me = _unit(body, par, env)
    pre, loop = _split(par, fail)
    trips = len(span(body, loop, env)) if loop is not None else 1
    looped = [(op, True) for op in (loop.body if loop else ())]
    for op, inloop in [*((op, False) for op in pre), *looped]:
        if not isinstance(op, Op) or op.op not in ("recv", "send"):
            continue
        if links is None:
            fail(op.st, f"{op.op}: a transfer between units needs the node lowering")
        pos = op.st.positional()
        if op.op == "recv":
            if inloop:
                fail(op.st, "recv is before the stream loop")
            t = op.type
            if t is None or t.space != "l1" or t.dtype != "f16" or len(pos) != 1:
                fail(op.st, "NAME = recv mg[U] : f16[R, C] @l1(gt4)")
            src = value(body, op.st, pos[0], env)
            if not (
                isinstance(src, View) and src.name == "mg" and len(src.slices) == 1
            ):
                fail(op.st, "a core receives from a cluster: recv mg[U]")
            x = links.take(op.st, f"mg[{src.slices[0].value}]", me, "l1")
            if x is None:  # costed alone: at the top of the L1
                words = t.shape[0] * t.shape[1] // 16
                x = _Landing(
                    f"mg[{src.slices[0].value}]",
                    me,
                    "l1",
                    L1_WORDS - words,
                    words,
                    t.shape,
                )
            if x.shape != t.shape or _mode(t) != "gt4":
                fail(op.st, f"the cluster sent f16{list(x.shape)} sub-tiles: @l1(gt4)")
            out[id(op)] = x
            continue
        kw = op.st.kwargs()
        if (
            len(pos) != 3
            or not isinstance(pos[0], Name)
            or str(kw.get("side", Name("A")).text) not in ("A", "B")
        ):
            fail(op.st, "send VALUE -> mg[I] side=A|B")
        if not inloop:
            fail(op.st, "send is in the stream loop")
        dst = value(body, op.st, pos[2], env)
        if not (isinstance(dst, View) and dst.name == "mg" and len(dst.slices) == 1):
            fail(op.st, "a core sends into a cluster: send VALUE -> mg[I] side=A|B")
        packed = next((o for o in _ops(loop.body) if o.target == pos[0].text), None)
        if (
            packed is None
            or packed.op != "pack"
            or packed.type.dtype
            not in (
                "mx7a",
                "mx7b",
            )
        ):
            fail(op.st, "a core sends a packed MXFP7 value into a cluster's bank")
        r, c = packed.type.shape
        side = kw.get("side", Name("A")).text
        shape = (r * trips, c)
        entries = (r // 4) * (c // 32) * trips
        to = f"mg[{dst.slices[0].value}]"
        out[id(op)] = links.send(op.st, me, to, side, entries, shape)
    return out


#: The V2 core `estimate` models (bench trace): an in-order front end into
#: per-engine queues `QUEUE` deep; a dependent math op waits `MATH_LATENCY`
#: after its source issues; unpack words and math beats share the register
#: file's write port; an unpack or pack walks 8 words.
MATH_LATENCY = 20
QUEUE = 8
WALK = 8


def estimate(code: list) -> int:
    """Cycles of a symbolic body: each instruction is dispatched in order, no
    earlier than a free slot in its engine's queue; an engine runs its queue
    in order, an op starting once its sources are ready."""
    ready: dict = {}
    done = {"u": 0, "m": 0, "p": 0}
    starts = {"u": [], "m": [], "p": []}
    wport = 0
    dispatch = 0
    for c in code:
        if not isinstance(c, tuple):
            continue
        tmpl, syms = c
        op = tmpl.split()[0]
        eng = "u" if op == "vunpk" else "p" if op == "vpack" else "m"
        q = starts[eng]
        dispatch = max(dispatch + 1, q[-QUEUE] if len(q) >= QUEUE else 0)
        start = max(dispatch, done[eng])
        if eng == "u":
            start = max(start, wport)
            done[eng] = wport = start + WALK
            ready[syms[0]] = done[eng]
        elif eng == "p":
            start = max(start, ready.get(syms[0], 0))
            done[eng] = start + WALK
        else:
            vl = int(tmpl.split(" vl=")[1].split()[0]) if " vl=" in tmpl else 128
            reads = syms[1:]
            if "{0}[" in tmpl.split(",")[0] or " if=" in tmpl:
                reads = syms  # a partial write keeps the rest: a read too
            start = max([start, wport, *(ready.get(s, 0) for s in reads)])
            done[eng] = wport = start + max(1, vl // 16)
            if not op.startswith("vcmp"):
                ready[syms[0]] = start + MATH_LATENCY
        q.append(start)
    return max(done.values())


def _lower(
    body, par, env, name, fail, band, remat, ahead, plan=None, land=None
) -> tuple:
    pre, loop = _split(par, fail)
    ctx = _Ctx(body, env, fail, loop)
    ctx.ahead, ctx.band, ctx.remat = ahead, band, remat
    ctx.plan, ctx.land = plan or {}, land or L1_WORDS
    ctx.unit = _unit(body, par, env)
    ctx.pre = pre
    for op in loop.body if loop else ():
        if isinstance(op, For):
            fail(op.st, "a loop inside the stream loop: write `for ... unroll`")
        ctx.prepare(op, looped=True)
    # Before the loop, buffers are dead once unpacked (`vsync fe on=unpack`
    # precedes the loop's first fill): they sit at the top, over the loop's.
    ctx.down = ctx.land
    for op in pre:
        ctx.prepare(op, looped=False)
    g = Gen()
    ctx.constants(g, [*pre, *_ops(loop.body if loop else ())])
    if loop is not None and any(isinstance(o, If) for o in loop.body):
        g.raw(f"getstk s{STK_S}")  # clears a sticky flag a past run left
    for op in pre:
        ctx.prologue(g, op)
    if any(op.op == "unpack" for op in pre):
        g.raw("vsync fe on=unpack")
    if ctx.rel is not None:
        _, r_name, first, _ = ctx.rel
        g.raw(f"uptr {ctx.vals[r_name].buf + 8 * first}")
        g.raw("uinc 8")
    if loop is not None:
        for d in ctx.loads:
            g.raw(f"vfill a{d.ad} -> l1[{d.buf[0]}] rel")
        passes, rest = divmod(len(ctx.trips), 2)
        if passes:
            g.raw(f"seti s{LOOP_S} = {passes}")
            g.raw(f"loop s{LOOP_S}")
            g.raw("{")
            ctx.iteration(g, loop.body, 0, prefetch=True)
            ctx.iteration(g, loop.body, 1, prefetch=bool(rest) or passes > 1)
            g.raw("}")
        if rest:
            ctx.iteration(g, loop.body, 0, prefetch=False)
    g.raw("halt")
    cost = estimate(g.code)
    whole = estimate(_expand(g.code, len(ctx.trips) // 2))
    lines = _assign(g, ctx.pinned, fail, par.st)
    params = sorted(ctx.buffers)
    # Parameters prefixed: a buffer may share a register's name (a0, v1, s2).
    head = [f"image {name}({', '.join(f'p_{p}' for p in params)})"]
    for d in ctx.descs:
        base = f"peer({d.off})" if d.to else f"p_{d.buffer} + {d.off}"
        head.append(f"    desc a{d.ad} = {base} walk=({d.walk})")
    for d in ctx.descs:
        if d.inc:
            head.append(f"    ainc a{d.ad} += {d.inc}")
    text = "\n".join(head + ["    " + ln for ln in _indent(lines)]) + "\n"
    return text, params, cost, ctx.accesses(), whole


def _expand(code: list, passes: int) -> list:
    """`code` with its VLOOP body written out `passes` times."""
    if "loop s15" not in code:
        return code
    a = code.index("loop s15") + 1
    depth = 0
    for b in range(a, len(code)):
        depth += {"{": 1, "}": -1}.get(code[b] if isinstance(code[b], str) else "", 0)
        if depth == 0:
            break
    return code[:a] + code[a + 1 : b] * max(passes, 1) + code[b + 1 :]


def _split(par: Par, fail) -> tuple:
    pre, loop = [], None
    for s in par.body:
        if isinstance(s, For):
            if loop is not None:
                fail(s.st, "one stream loop a vector instance")
            loop = s
        elif loop is not None:
            fail(s.st, "nothing after the stream loop")
        else:
            pre.append(s)
    return pre, loop


class _Ctx:
    def __init__(self, body, env, fail, loop) -> None:
        self.body, self.env, self.fail, self.loop = body, env, fail, loop
        self.trips = span(body, loop, env) if loop is not None else range(1)
        self.descs: list = []
        self.loads: list = []
        self.of: dict = {}
        self.top = 0
        self.vals: dict = {}
        self.scalars: dict = {}
        self.pinned: set = set()
        self.buffers: set = set()
        self.ahead = True
        self.band = 1
        self.remat = False
        self.down = L1_WORDS
        self.land = L1_WORDS
        self.plan: dict = {}
        self.unit = ""
        self.pre: list = []
        #: the loop's unpack of row bands of a value before the loop, through
        #: the unpack pointer: (op, value, first register, registers a trip)
        self.rel = None
        #: inside an `if sticky` block: results overwrite their names' registers
        self.inplace = False
        #: GT4 rows: the registers of a 4-row band (0: rows are registers)
        self.per_band = 0

    def accesses(self) -> list:
        u, out, trips = self.unit, [], len(self.trips)
        for d in self.descs:
            if d.to:
                x = self.plan[d.kind_op]
                key = (d.to, "AB"[d.side])
                out += [
                    sync.Access(u, True, key, x.at, x.at + x.size, True),
                    sync.Access(u, True, (d.to, "rx"), 0, 1, True),
                ]
                continue
            lo, hi = d.span(trips if d.inc else 1)
            out.append(sync.mem(u, d.kind == "store", d.buffer, lo, hi))
        for x in self.plan.values():
            if x.dst == u:
                out.append(sync.Access(u, False, (u, "l1"), x.at, x.at + x.size))
        out.append(sync.Access(u, True, (u, "l1"), 0, max(self.top, 1)))
        if self.down < self.land:
            out.append(sync.Access(u, True, (u, "l1"), self.down, self.land))
        return out

    # ---------------------------------------------------------- memory
    def _walk(self, op, view: View, at):
        """``(buffer, byte offset, walk text, words, gt4)`` of a view: a
        row-major f16 view is one run of whole rows; a view of a `tile=`
        buffer is bands of 4 rows, each a run of 4x4 sub-tiles (GT4)."""
        env = dict(self.env)
        if self.loop is not None and at is not None:
            env[self.loop.var] = at
        v = value(self.body, op.st, view, env)
        if isinstance(v, Name):
            v = View(v.text, ())
        buf = self.body.buffers.get(v.name)
        if buf is None:
            self.fail(op.st, f"no buffer {v.name}")
        try:
            ax = M.axes(v, buf.shape)
            if "tile" in buf.layout:
                return (v.name, *_tiled(buf, ax), True)
            if buf.dtype in ("mx7a", "mx7b"):
                return (v.name, *_mx7(buf, ax), "mx7")
            off, dims, item = M.rowmajor_walk(buf, ax)
        except M.ViewError as e:
            self.fail(op.st, str(e))
        nbytes = item
        for n, _ in dims:
            nbytes *= n
        if item != 2 or not _contiguous(dims, item):
            self.fail(
                op.st, "a row-major load or store view is contiguous f16, whole rows"
            )
        return v.name, off, f"32, {nbytes // 32}", nbytes // 32, False

    def prepare(self, op, looped: bool) -> None:
        if isinstance(op, Op) and op.op == "send":
            self._send(op)
            return
        if isinstance(op, Op) and op.op == "unpack" and looped:
            src = op.st.positional()[0] if op.st.positional() else None
            if isinstance(src, View):
                self._band_view(op, src)
            return
        if not isinstance(op, Op) or op.op not in ("load", "store"):
            return
        pos = op.st.positional()
        if op.op == "load":
            t = op.type
            if len(pos) != 1 or not isinstance(pos[0], (View, Name)):
                self.fail(op.st, "NAME = load VIEW : f16[R, C] @l1")
            if t is None or t.space != "l1" or t.dtype != "f16":
                self.fail(op.st, "a load is f16[R, C] @l1")
            view = pos[0] if isinstance(pos[0], View) else View(pos[0].text, ())
            want = t.shape[0] * t.shape[-1] * 2 // 32
        else:
            if len(pos) != 3 or not isinstance(pos[0], (View, Name)):
                self.fail(op.st, "store VIEW <- VALUE")
            view = pos[0] if isinstance(pos[0], View) else View(pos[0].text, ())
            want = None
        first = self.trips[0] if looped else None
        name, off, walk, words, gt4 = self._walk(op, view, first)
        if want is not None and words != want:
            self.fail(op.st, f"the view holds {words} words, the value {want}")
        if op.op == "load" and gt4 == "mx7":
            self.fail(
                op.st, "an MXFP7 buffer is a cluster's operand: a core stores into it"
            )
        if op.op == "load" and gt4 != (op.type.mode == "gt4"):
            self.fail(op.st, "a tile= buffer loads @l1(gt4); a row-major one @l1")
        if not gt4 and op.op == "load" and op.type.shape[-1] != ROW:
            self.fail(op.st, f"a row-major load is f16[R, {ROW}]")
        inc = 0
        if looped and len(self.trips) > 1:
            inc = self._walk(op, view, first + 1)[1] - off
            end = self._walk(op, view, self.trips[-1])[1]
            if end != off + inc * (len(self.trips) - 1):
                self.fail(op.st, "the view is not affine in the loop variable")
            if not -(1 << 17) <= inc < 1 << 17:
                self.fail(op.st, f"a {inc}-byte step: ainc takes 18 signed bits")
        if len(self.descs) == DESCS:
            self.fail(op.st, f"more than {DESCS} memory views")
        if looped:
            buf = tuple(self._alloc(words, op) for _ in range(2))
        else:
            self.down -= words
            # Dead once unpacked, so over the loop's buffers -- unless the
            # loop unpacks bands of it.
            kept = self.rel is not None and self.rel[1] == op.target
            if self.down < (self.top if kept else 0):
                self.fail(op.st, f"L1 holds {self.land} words for this core's buffers")
            buf = (self.down,)
        d = Desc(len(self.descs), name, off, words, inc, buf, walk, kind=op.op)
        self.descs.append(d)
        self.buffers.add(name)
        self.of[id(op)] = d
        if op.op == "load" and looped:
            self.loads.append(d)

    def _send(self, op) -> None:
        """A peer drain of each trip's packed rows into the cluster's bank, the
        trips one after another (a granule is a quarter entry)."""
        x = self.plan[id(op)]
        name = op.st.positional()[0].text
        packed = next(o for o in _ops(self.loop.body) if o.target == name)
        words = _count(packed.type) * 4
        if len(self.descs) == DESCS:
            self.fail(op.st, f"more than {DESCS} memory views")
        buf = tuple(self._alloc(words, op) for _ in range(2))
        side = "AB".index(op.st.kwargs().get("side", Name("A")).text)
        d = Desc(
            len(self.descs),
            None,
            4 * x.at,
            words,
            words if len(self.trips) > 1 else 0,
            buf,
            f"32, {words}",
            to=x.dst,
            side=side,
            kind="send",
            kind_op=id(op),
        )
        self.descs.append(d)
        self.of[id(op)] = d

    def _band_view(self, op, view: View) -> None:
        """`unpack H[ROWS, :]` of a value made before the loop: each trip's
        rows follow the last trip's, read through the unpack pointer."""
        src = next((o for o in self.pre if o.target == view.name), None)
        if src is None or src.op not in ("recv", "load") or src.type is None:
            self.fail(op.st, f"{view.name}: a value made before the stream loop")
        if self.rel is not None:
            self.fail(op.st, "one banded unpack a loop (one unpack pointer)")
        shape = src.type.shape
        firsts = []
        for t in (self.trips[0], self.trips[-1]):
            v = value(self.body, op.st, view, {**self.env, self.loop.var: t})
            try:
                ax = M.axes(v, shape)
            except M.ViewError as e:
                self.fail(op.st, str(e))
            (lo, _, _), (c0, c, _) = ax
            if c0 or c != shape[1] or M.shape_of(ax) != op.type.shape:
                self.fail(op.st, "a banded unpack takes whole rows of the value")
            firsts.append(lo * shape[1] // ROW)
        per = _count(op.type)
        if firsts[1] - firsts[0] != per * (len(self.trips) - 1):
            self.fail(op.st, "each trip's rows follow the last trip's")
        if _mode(op.type) != _mode(src.type):
            self.fail(
                op.st,
                f"an @l1({_mode(src.type)}) value unpacks to @reg({_mode(src.type)})",
            )
        self.rel = (op, view.name, firsts[0], per)

    def _alloc(self, words: int, op) -> int:
        at, self.top = self.top, self.top + words
        if self.top > self.land:
            self.fail(op.st, f"L1 holds {self.land} words; the buffers need {self.top}")
        return at

    # -------------------------------------------------------- constants
    def constants(self, g: Gen, ops) -> None:
        for op in ops:
            if not isinstance(op, Op):
                continue
            for a in op.st.positional():
                f = _number(a)
                if f is None or f in KREG or f in self.scalars:
                    continue
                if len(self.scalars) == STK_S:
                    self.fail(op.st, f"more than {STK_S} scalar constants")
                s = f"s{len(self.scalars)}"
                self.scalars[f] = s
                g.raw(f"seti {s} = {f!r}")

    def scalar(self, f: float) -> str:
        return KREG.get(f) or self.scalars[f]

    # --------------------------------------------------------- prologue
    def prologue(self, g: Gen, op) -> None:
        if op.op == "recv":
            x = self.plan[id(op)]
            self.vals[op.target] = Val(
                "l1", _count(op.type), buf=x.at, mode=_mode(op.type)
            )
        elif op.op == "load":
            d = self.of[id(op)]
            g.raw(f"vfill a{d.ad} -> l1[{d.buf[0]}]")
            g.raw("vbar")
            self.vals[op.target] = Val(
                "l1", _count(op.type), buf=d.buf[0], mode=_mode(op.type)
            )
        elif op.op == "unpack":
            src = self._get(op, op.st.positional()[0], "l1")
            v = Val("row", _count(op.type), mode=src.mode)
            for r in range(v.rows):
                s = g.sym()
                v.regs[r] = s
                self.pinned.add(s)
                g.emit(f"vunpk {{0}} <- l1[{src.buf + r * 8}] {src.mode}", s)
            self.vals[op.target] = v
        else:
            self.fail(op.st, f"before the stream loop: load and unpack, not {op.op}")

    def _get(self, op, t, kind=None, local=None):
        if not isinstance(t, Name):
            self.fail(op.st, f"wanted a value name, got {t!r}")
        v = (local or {}).get(t.text) or self.vals.get(t.text)
        if v is None:
            self.fail(op.st, f"no value {t.text}")
        if kind is not None and v.kind != kind:
            self.fail(op.st, f"{t.text} is a {v.kind} value, not {kind}")
        return v

    # -------------------------------------------------------- iteration
    def iteration(self, g: Gen, body, parity: int, prefetch: bool) -> None:
        ops = _ops(body)
        if any(isinstance(o, If) for o in body):
            self._check_if(body)
        reduces = any(o.op in REDUCE for o in ops)
        gt4 = any(o.type and o.type.mode == "gt4" for o in ops)
        typed = [o for o in ops if o.type and o.type.space in ("l1", "reg")]
        if gt4:
            # A GT4 register is a 4-row band's 32 columns: a row statistic is
            # one register a band, every chunk holding its row's value.
            typed = [o for o in typed if len(o.type.shape) == 2]
            # One width for the rows; 32-wide values (one register a band:
            # a statistic stored or loaded back) beside them.
            widths = {o.type.shape[1] for o in typed if _mode(o.type) == "gt4"}
            wide = max(widths)
            if wide % 32 or widths - {wide, 32}:
                self.fail(ops[0].st, "GT4 values of one width, a multiple of 32")
            self.per_band = wide // 32
        rows = {_count(o.type) for o in typed}
        tile = max(rows)
        if any(r not in (tile, 1) for r in rows):
            self.fail(
                ops[0].st, f"one register count in the stream loop, got {sorted(rows)}"
            )
        group = self.per_band if gt4 else 8 if reduces else min(4, tile)
        if tile % group:
            self.fail(ops[0].st, f"{tile} rows do not split into groups of {group}")
        stores = [o for o in ops if o.op in ("store", "send")]
        g.raw("vbar")
        if prefetch:
            g.raw("vsync fill on=unpack")
            for d in self.loads:
                g.raw(f"vfill a{d.ad} -> l1[{d.buf[1 - parity]}] rel")
        for o in ops:
            if o.op == "load":
                self.vals[o.target] = Val(
                    "l1",
                    _count(o.type),
                    buf=self.of[id(o)].buf[parity],
                    mode=_mode(o.type),
                )
        local: dict = {}
        groups = [range(k, k + group) for k in range(0, tile, group)]
        if self.band < len(groups) and len(groups) % self.band:
            raise Pressure
        width = min(self.band, len(groups))
        # The flag covers every row's math: one band holds them all.
        if any(isinstance(o, If) for o in body) and width != len(groups):
            raise Pressure
        bands = [groups[k : k + width] for k in range(0, len(groups), width)]
        fused = {} if gt4 else _fusions(ops)
        first_reduce = next(
            (i for i, o in enumerate(body) if getattr(o, "op", "") in REDUCE), None
        )
        if self.ahead:
            self._unpacks(g, ops, _rows(bands[0]), local)
        for k, grps in enumerate(bands):
            rws = _rows(grps)
            if not self.ahead:
                self._unpacks(g, ops, rws, local)
            elif k + 1 < len(bands):
                self._unpacks(g, ops, _rows(bands[k + 1]), local)
            again = set()
            for i, o in enumerate(body):
                if isinstance(o, If):
                    g.raw("vsync fe on=math")
                    g.raw(f"getstk s{STK_S}")
                    g.raw(f"skipz s{STK_S}")
                    g.raw("{")
                    self.inplace = True
                    for b in o.body:
                        self._compute(g, b, grps, rws, local, fused)
                    self.inplace = False
                    g.raw("}")
                    continue
                if (
                    o.op in ("load", "store", "send", "unpack", "pack")
                    or o.target in fused
                ):
                    continue
                if self.remat and first_reduce is not None and i > first_reduce:
                    self._reunpack(g, ops, o, rws, local, again)
                self._compute(g, o, grps, rws, local, fused)
            if k == 0:
                g.raw(f"vsync pack on=drain slack={len(stores)}")
            self._packs(g, ops, rws, local, parity)
        g.raw("vsync drain on=pack")
        for o in stores:
            d = self.of[id(o)]
            name = o.st.positional()[0 if o.op == "send" else 2]
            src = self._get(o, name, "l1", local)
            at = d.buf[parity] if src.buf is None else src.buf
            peer = f" to={d.to} buf={d.side}" if d.to else ""
            g.raw(f"vdrain a{d.ad} <- l1[{at}] rel{peer}")

    def _compute(self, g, o, grps, rws, local, fused) -> None:
        if o.op in REDUCE:
            self._reduces(g, o, grps, local, fused)
        else:
            self._math(g, o, rws, local)

    def _check_if(self, body) -> None:
        """One `if sticky` block of math whose new names stay inside it: on
        the skipped path they hold nothing."""
        blocks = [o for o in body if isinstance(o, If)]
        if len(blocks) > 1:
            self.fail(blocks[1].st, "one if sticky block a stream loop")
        block = blocks[0]
        at = body.index(block)
        before = {o.target for o in body[:at] if isinstance(o, Op)}
        before |= set(self.vals)
        inner = set()
        for o in _ops(block.body):
            if o.op in ("load", "store", "send", "unpack", "pack"):
                self.fail(o.st, "if sticky holds math and reductions")
            if o.target not in before:
                inner.add(o.target)
        for o in _ops(body[at + 1 :]):
            for a in o.st.positional():
                n = a.name if isinstance(a, View) else getattr(a, "text", None)
                if n in inner:
                    self.fail(o.st, f"{n} is made only inside if sticky")

    def _unpacks(self, g, ops, rows, local, only=None) -> None:
        for o in ops:
            if o.op != "unpack" or (only is not None and o.target != only):
                continue
            if self.rel is not None and self.rel[0] is o:
                mode = self.vals[self.rel[1]].mode
                v = local.setdefault(o.target, Val("row", _count(o.type), mode=mode))
                for r in rows:
                    s = g.sym()
                    v.regs[r] = s
                    g.emit(f"vunpk {{0}} <- l1[0] {mode} rel", s)
                continue
            src = self._get(o, o.st.positional()[0], "l1", local)
            if _mode(o.type) != src.mode:
                self.fail(o.st, f"an @l1({src.mode}) value unpacks to @reg({src.mode})")
            v = local.setdefault(o.target, Val("row", _count(o.type), mode=src.mode))
            for r in rows:
                s = g.sym()
                v.regs[r] = s
                g.emit(f"vunpk {{0}} <- l1[{src.buf + r * 8}] {src.mode}", s)

    def _reunpack(self, g, ops, o, rows, local, again) -> None:
        """Past a reduction, an unpacked row value read again is unpacked
        again from L1 rather than held through the reduction."""
        # A banded unpack advances the pointer: it is read once a trip.
        unpacked = {
            u.target
            for u in ops
            if u.op == "unpack" and (self.rel is None or self.rel[0] is not u)
        }
        for a in o.st.positional():
            n = (
                a.name
                if isinstance(a, View)
                else a.text if isinstance(a, Name) else None
            )
            if n in unpacked and n not in again:
                again.add(n)
                self._unpacks(g, ops, rows, local, only=n)

    def _reduces(self, g, o, grps, local, fused) -> None:
        """One reduction over every group of the band, the trees' steps
        interleaved."""
        pos = o.st.positional()
        if len(pos) != 1 or o.type is None or len(o.type.shape) != 1:
            self.fail(o.st, f"NAME = {o.op} ROWS : f32[R] @reg")
        code = REDUCE[o.op]
        name = pos[0].text if isinstance(pos[0], Name) else None
        inplace = self.inplace and o.target in local
        out = local.setdefault(o.target, Val("stat", o.type.shape[0]))
        if self.per_band:
            src = self._get(o, pos[0], "row", local)
            out.mode = "gt4"
            trees = []
            for grp in grps:
                band = grp[0] // self.per_band
                into = out.regs.get(band) if inplace else None
                steps, out.regs[band] = _tree4(
                    g, code, [src.regs[r] for r in grp], into
                )
                trees.append(steps)
            _interleave(g, trees)
            return
        trees = []
        for rws in grps:
            if len(rws) != 8:
                self.fail(o.st, "a reduction runs over groups of 8 rows")
            if name in fused:
                a, b = (self._get(o, t, "row", local) for t in fused[name])
                rows = [(_reg(a, r), _reg(b, r)) for r in rws]
            else:
                src = self._get(o, pos[0], "row", local)
                rows = [src.regs[r] for r in rws]
            into = out.regs[rws[0] // 8] if inplace else None
            steps, result = _tree(g, code, rows, name in fused, into)
            trees.append(steps)
            out.regs[rws[0] // 8] = result
        _interleave(g, trees)

    def _packs(self, g, ops, rows, local, parity) -> None:
        for o in ops:
            if o.op != "pack":
                continue
            stat = self._get(o, o.st.positional()[0], None, local)
            if stat.kind == "stat":
                self._pack_stat(g, o, ops, stat, rows, local, parity)
                continue
            src = self._get(o, o.st.positional()[0], "row", local)
            store = [
                s
                for s in ops
                if (s.op == "store" and s.st.positional()[2] == Name(o.target))
                or (s.op == "send" and s.st.positional()[0] == Name(o.target))
            ]
            if len(store) != 1:
                self.fail(o.st, "a packed value is stored or sent once")
            buf = self.of[id(store[0])].buf[parity]
            if o.type.dtype in ("mx7a", "mx7b"):
                if src.mode != "gt4":
                    self.fail(
                        o.st, "an MXFP7 pack takes 4 x 32 blocks: a @reg(gt4) value"
                    )
                for r in rows:
                    t = g.sym()
                    # GT4 holds row i, column half q in chunk 4q + i; the
                    # quantiser takes lane i's 32 values from chunks 2i, 2i + 1.
                    for i in range(4):
                        for q in range(2):
                            g.emit(
                                f"vmov {{0}}[{2 * i + q}:], {{1}}[{4 * q + i}:] vl=16",
                                t,
                                src.regs[r],
                            )
                    g.emit(f"vpack {{0}} -> l1[{buf + r * 4}] {o.type.dtype}", t)
                local[o.target] = Val("l1", _count(o.type), buf=buf, mode=o.type.dtype)
                continue
            mode = _mode(o.type)
            for r in rows:
                g.emit(f"vpack {{0}} -> l1[{buf + r * 8}] {mode}", src.regs[r])
            local[o.target] = Val("l1", _count(o.type), buf=buf, mode=mode)

    def _pack_stat(self, g, o, ops, stat, rows, local, parity) -> None:
        """A GT4 band statistic as 4 x 32 sub-tiles (`pack m : f16[4k, 32]
        @l1(gt4)`): its register, every chunk its row's value, packed as a
        GT4 block -- each row's value in all 32 columns."""
        if not self.per_band or _mode(o.type) != "gt4" or o.type.shape[1] != 32:
            self.fail(o.st, "a statistic packs from GT4 rows as f16[4k, 32] @l1(gt4)")
        store = [
            s for s in ops if s.op == "store" and s.st.positional()[2] == Name(o.target)
        ]
        if len(store) != 1:
            self.fail(o.st, "a packed value is stored once")
        buf = self.of[id(store[0])].buf[parity]
        bands = sorted({r // self.per_band for r in rows})
        for b in bands:
            g.emit(f"vpack {{0}} -> l1[{buf + b * 8}] gt4", stat.regs[b])
        local[o.target] = Val("l1", len(bands), buf=buf, mode="gt4")

    def _math(self, g, o, rows, local) -> None:
        pos = o.st.positional()
        t = o.type
        if t is None or t.space != "reg" or o.target is None:
            self.fail(o.st, f"NAME = {o.op} ... : f32[..] @reg")
        code = (
            UNARY.get(o.op)
            or BINARY.get(o.op)
            or TERNARY.get(o.op)
            or CMP.get(o.op)
            or ("vsel" if o.op == "sel" else None)
        )
        if code is None:
            self.fail(o.st, f"no vector op {o.op!r}")
        want = 1 if o.op in UNARY else 2 if o.op in BINARY or o.op in CMP else 3
        if len(pos) != want:
            self.fail(o.st, f"{o.op} takes {want} operands")
        # In an `if sticky` block a result overwrites its name's registers,
        # so the skipped path leaves the value it had.
        inplace = self.inplace and o.target in local
        if len(t.shape) == 1:
            if o.op in CMP or o.op == "sel":
                self.fail(o.st, f"{o.op} runs on rows (f32[R, C] @reg)")
            out = local.setdefault(o.target, Val("stat", t.shape[0]))
            # A row statistic: lanes 0..7 of chunk 0 for 8 rows; with GT4
            # rows, a whole register a band.
            per, sel, vl = (
                (self.per_band, "", "") if self.per_band else (8, "[0:]", " vl=16")
            )
            if self.per_band:
                out.mode = "gt4"
            for grp in sorted({r // per for r in rows}):
                syms, srcs = [], []
                for a in pos:
                    f = _number(a)
                    if f is not None:
                        srcs.append(self.scalar(f))
                        continue
                    v = self._get(o, a, "stat", local)
                    syms.append(v.regs[grp])
                    srcs.append(f"{{{len(syms)}}}{sel}")
                d = out.regs[grp] if inplace else g.sym()
                g.emit(f"{code} {{0}}{sel}, {', '.join(srcs)}{vl}", d, *syms)
                out.regs[grp] = d
            return
        out = local.setdefault(o.target, Val("row", _count(t), mode=_mode(t)))
        for r in rows:
            syms, srcs = [], []
            for a in pos:
                f = _number(a)
                if f is not None:
                    srcs.append(self.scalar(f))
                elif (
                    isinstance(a, View)
                    and a.slices
                    and isinstance(a.slices[-1], NewAxis)
                ):
                    v = self._get(o, Name(a.name), "stat", local)
                    if self.per_band:
                        # Every chunk of a band's statistic is its row's value.
                        syms.append(v.regs[r // self.per_band])
                        srcs.append(f"{{{len(syms)}}}")
                        continue
                    syms.append(v.regs[r // 8])
                    srcs.append(f"bcast({{{len(syms)}}}[0], lane={r % 8}, sh=3)")
                else:
                    v = self._get(o, a, "row", local)
                    syms.append(_reg(v, r))
                    srcs.append(f"{{{len(syms)}}}")
            d = out.regs[r] if inplace else g.sym()
            if o.op in CMP:
                g.emit(f"{code} p1, {', '.join(srcs)}", d, *syms)
                g.emit("vmov {0}, k0", d)
                g.emit("vmov {0}, k1 if=p1", d)
            elif o.op == "sel":
                c, a, b = srcs
                g.emit(f"vsel {{0}}, {a}, {b}, {c}", d, *syms)
            else:
                g.emit(f"{code} {{0}}, {', '.join(srcs)}", d, *syms)
            out.regs[r] = d


def _reg(v: Val, r: int) -> str:
    """Row r's register; a one-row value is every row's."""
    return v.regs[r] if v.rows > 1 else v.regs[0]


def _rows(groups) -> list:
    return [r for grp in groups for r in grp]


def _fusions(ops) -> dict:
    """``{product: (a, b)}`` for `p = mul a, b` read only by one `rsum`: the
    reduction folds the product (multiply, then fused multiply-add) and the
    product is never formed."""
    out = {}
    for o in ops:
        if (
            o.op != "mul"
            or o.target is None
            or o.type is None
            or len(o.type.shape) != 2
        ):
            continue
        readers = [
            u
            for u in ops
            if any(
                isinstance(a, Name) and a.text == o.target for a in u.st.positional()
            )
        ]
        a, b = o.st.positional()
        if (
            len(readers) == 1
            and readers[0].op == "rsum"
            and isinstance(a, Name)
            and isinstance(b, Name)
        ):
            out[o.target] = (a, b)
    return out


def _interleave(g, trees) -> None:
    """Several trees' steps, step by step: each step's instructions of every
    tree before the next step (independent work fills the latency)."""
    for s in range(len(trees[0])):
        longest = max(len(t[s]) for t in trees)
        for i in range(longest):
            for t in trees:
                if i < len(t[s]):
                    g.emit(*t[s][i])


def _tree4(g, code, regs, into=None) -> tuple:
    """A 4-row band's row reduction over its GT4 registers (row i, columns
    16q.. in chunk 4q + i): the registers folded pairwise, chunk 4 + i onto
    chunk i, the 16 lanes by an xor butterfly (8, 4, 2, 1: every lane ends
    with the total), chunks 0-3 copied to 4-7 -- so chunk 4q + i holds row
    i's value in every lane, an elementwise operand to the band's registers.
    ``(steps, the result's symbol)``."""
    steps, syms = [], list(regs)
    while len(syms) > 1:
        level, nxt = [], []
        for k in range(0, len(syms) - 1, 2):
            d = g.sym()
            level.append((f"{code} {{0}}, {{1}}, {{2}}", d, syms[k], syms[k + 1]))
            nxt.append(d)
        steps.append(level)
        syms = nxt + ([syms[-1]] if len(syms) % 2 else [])
    d = into or g.sym()
    steps.append([(f"{code} {{0}}[0:], {{1}}[0:], {{1}}[4:] vl=64", d, syms[0])])
    for k in (8, 4, 2, 1):
        steps.append([(f"{code} {{0}}[0:], {{0}}[0:], xor({{0}}, {k}) vl=64", d)])
    steps.append([("vmov {0}[4:], {0}[0:] vl=64", d)])
    return steps, d


def _tree(g, code, rows, fused, into=None) -> tuple:
    """The merge tree over 8 rows as steps of independent instructions, and
    the symbol whose chunk 0 lane r is row r's reduction (`into` when given).
    `rows` are row registers, or with `fused` (a, b) pairs whose product is
    summed."""
    t = [g.sym() for _ in range(4)]
    first = []
    for i in range(8):
        c = 4 * (i // 4)
        x = rows[2 * (i % 4) + i // 4]
        if fused:
            a, b = x
            first.append(
                (f"vmul {{0}}[{c}:], {{1}}[4:], {{2}}[4:] vl=64", t[i % 4], a, b)
            )
            first.append(
                (
                    f"vfma {{0}}[{c}:], {{1}}[0:], {{2}}[0:], {{0}}[{c}:] vl=64",
                    t[i % 4],
                    a,
                    b,
                )
            )
        else:
            first.append(
                (f"{code} {{0}}[{c}:], {{1}}[0:], {{1}}[4:] vl=64", t[i % 4], x)
            )
    if fused:
        # The multiplies of all rows first, then the adds: no add waits on
        # the multiply just before it.
        first = first[0::2] + first[1::2]
    second = []
    for i in range(8):
        c = 4 * (i // 4)
        second.append(
            (f"{code} {{0}}[{c}:], {{0}}[{c}:], {{0}}[{c + 2}:] vl=32", t[i % 4])
        )
    acc = g.sym()
    third = []
    for r in range(8):
        c = 4 * (r % 2)
        third.append(
            (f"{code} {{0}}[{r}:], {{1}}[{c}:], {{1}}[{c + 1}:] vl=16", acc, t[r // 2])
        )
    m, d = g.sym(), into or g.sym()
    steps = [
        first,
        second,
        third,
        [(f"{code} {{0}}, {{1}}, merge({{1}}, 8)", m, acc)],
        [(f"{code} {{0}}, {{0}}, merge({{0}}[4:], 4) vl=64", m)],
        [(f"{code} {{0}}, {{0}}, merge({{0}}[2:], 2) vl=32", m)],
        [(f"{code} {{0}}[0:], {{1}}, merge({{1}}[1:], 1) vl=16", d, m)],
    ]
    return steps, d


def _tiled(buf, ax) -> tuple:
    """``(byte offset, walk text, words)`` of a view of a `tile=` buffer:
    R/4 bands of C/16 consecutive sub-tiles, the bands equally spaced (one
    tile's sub-rows)."""
    base, ((r0, rows), (c0, cols)) = M._slab(buf, ax)
    if r0 % 4 or c0 % 16 or rows % 4 or cols % 32:
        raise M.ViewError("a GT4 view is bands of 4 rows by 32 columns")
    sr, sc = r0 // 4, c0 // 4
    first = M.subtile_address(buf, sr, sc)
    per = cols // 4
    if M.subtile_address(buf, sr, sc + per - 1) != first + (per - 1) * M.SUBTILE:
        raise M.ViewError(
            "a band's sub-tiles are one run: the columns stay in one tile"
        )
    bands = rows // 4
    stride = M.subtile_address(buf, sr + 1, sc) - first if bands > 1 else 0
    for b in range(bands):
        if M.subtile_address(buf, sr + b, sc) != first + b * stride:
            raise M.ViewError(
                "the view's bands are not equally spaced (rows cross a tile)"
            )
    walk = f"32, {per}" if bands == 1 else f"(32, {per}), ({stride}, {bands})"
    return base + first, walk, per * bands


def _mx7(buf, ax) -> tuple:
    """``(byte offset, walk text, words)`` of a view of an MXFP7 buffer packed
    tile-major (`tensor.pack`): bands of 4 rows (one lane group each), each
    band's 32-value blocks in K order -- `nk` consecutive entries a packed
    chunk, the chunks `gm * nk` entries apart; the bands nk entries apart.
    The rows stay in one packed tile."""
    g = buf.layout["gm" if buf.dtype == "mx7a" else "gn"]
    nk = buf.layout["nk"]
    base, ((r0, rows), (k0, cols)) = M._slab(buf, ax)
    chunk = nk * 32
    if r0 % 4 or rows % 4 or k0 % chunk or cols % chunk:
        raise M.ViewError(
            f"an MXFP7 store is bands of 4 rows by whole chunks of {chunk}"
        )
    g0, bands = r0 // 4, rows // 4
    if g0 // g != (g0 + bands - 1) // g:
        raise M.ViewError(f"the rows cross a packed tile of {4 * g} rows")
    chunks = buf.shape[-1] // chunk
    t, c0 = g0 // g, k0 // chunk
    first = ((t * chunks + c0) * g + (g0 - t * g)) * nk * M.ENTRY
    dims = [(32, 4 * nk)]
    if cols > chunk:
        dims.append((g * nk * M.ENTRY, cols // chunk))
    if bands > 1:
        dims.append((nk * M.ENTRY, bands))
    if len(dims) == 1:
        walk = f"32, {4 * nk}"
    else:
        walk = ", ".join(f"({s}, {n})" for s, n in dims)
    return base + first, walk, 4 * nk * (cols // chunk) * bands


def _contiguous(dims, item) -> bool:
    want = item
    for n, stride in reversed(dims):
        if stride != want:
            return False
        want *= n
    return True


def _number(a):
    if isinstance(a, (Float, Int)):
        return float(a.value)
    return None


def _assign(g: Gen, pinned: set, fail, st) -> list:
    """Physical registers over the emitted order: a symbol holds its register
    from its first to its last appearance; a symbol first appearing as an
    instruction's result (template position 0) takes the register of one whose
    last appearance is that instruction."""
    last: dict = {}
    for i, c in enumerate(g.code):
        if isinstance(c, tuple):
            for s in c[1]:
                last[s] = i
    reg: dict = {}
    free = list(range(REGS))
    out = []
    for i, c in enumerate(g.code):
        if isinstance(c, str):
            out.append(c)
            continue
        tmpl, syms = c
        dying = [s for s in syms[1:] if last[s] == i and s in reg and s not in pinned]
        for j, s in enumerate(syms):
            if s in reg:
                continue
            if j == 0 and dying:
                reg[s] = reg[dying.pop(0)]
                continue
            if not free:
                raise Pressure
            reg[s] = free.pop(0)
        out.append(tmpl.format(*(f"v{reg[s]}" for s in syms)))
        held = {reg[s] for s in syms if last[s] > i or s in pinned}
        for s in set(syms):
            if (
                last[s] == i
                and s not in pinned
                and reg[s] not in held
                and reg[s] not in free
            ):
                free.append(reg[s])
        free.sort()
    return out


def _indent(lines: list) -> list:
    out, depth = [], 0
    for ln in lines:
        if ln == "{":
            depth += 1
        elif ln == "}":
            depth -= 1
        else:
            out.append("    " * depth + ln)
    return out


__all__ = ["lower"]
