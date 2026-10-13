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

from kohakutpu.compiler.emit import image as EI
from kohakutpu.compiler.l2 import banks, sync, timing
from kohakutpu.compiler.l2 import memory as M
from kohakutpu.compiler.l2 import modulo as MO
from kohakutpu.compiler.l2.body import For, If, Op, Par, When, const, span, value
from kohakutpu.language.text import module as TM
from kohakutpu.language.text.syntax import Float, Int, Name, NewAxis, TextError, View

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
    #: l1: words a register's unpack reads (8 f16, 16 f32), and in what mode
    #: ("" is the value's own mode)
    per: int = 8
    unpk: str = ""
    #: l1, rows mode (GT4 registers from a row-major band): words an L1 row
    #: holds; `bcast` when every row reads the one row loaded
    rows_w: int = 0
    bcast: bool = False
    #: row: the value's columns (a band value's width beside the loop's)
    cols: int = 0


def _mode(t) -> str:
    return "gt4" if t is not None and t.mode == "gt4" else "f16"


def _count(t) -> int:
    """Registers (or row-register slots) a value of type `t` takes: a row of
    128 each, or with GT4 a 4 x 32 block each; a statistic its rows."""
    if len(t.shape) == 2 and (_mode(t) == "gt4" or t.dtype in ("mx7a", "mx7b")):
        r, c = t.shape
        return r * c // ROW
    return t.shape[0]


def _l1val(d, t, buf: int) -> "Val":
    """The @l1 value a load or recv of type `t` makes, through Desc `d` (None
    for a transfer): how its registers unpack."""
    f32 = t.dtype == "f32"
    v = Val("l1", _count(t), buf=buf, mode=_mode(t), per=16 if f32 else 8)
    v.unpk = "f32" if f32 else ""
    if d is not None and d.rows_w:
        v.rows_w, v.bcast, v.unpk = d.rows_w, d.bcast, "f16"
    return v


def _pack_line(d: "Desc", buf: int, i: int, mode: str, dtype: str) -> str:
    """The pack of register `i` for a store through Desc `d`: in rows mode
    (a row-major band) chunk 4q + i to L1 row i, columns 16q.. of block j."""
    if d.rows_w:
        nb = d.rows_w // 2
        b, j = divmod(i, nb)
        return f"vpack {{0}} -> l1[{buf + 4 * b * d.rows_w + 2 * j}] f16 walk=({d.rows_w}, 1)"
    if dtype == "f32":
        return f"vpack {{0}} -> l1[{buf + i * 16}] f32"
    return f"vpack {{0}} -> l1[{buf + i * 8}] {mode}"


def _unpack_line(src: "Val", i: int) -> str:
    """The unpack of register `i` of an @l1 value: a GT4 / f16 / f32 run, or
    in rows mode band b's 32-column block j read across its four L1 rows
    (chunk 4q + i = row i, columns 16q.. of the block)."""
    if not src.rows_w:
        return f"vunpk {{0}} <- l1[{src.buf + i * src.per}] {src.unpk or src.mode}"
    nb = src.rows_w // 2
    b, j = divmod(i, nb)
    base = src.buf + (0 if src.bcast else 4 * b * src.rows_w) + 2 * j
    s0 = 0 if src.bcast else src.rows_w
    return f"vunpk {{0}} <- l1[{base}] f16 walk=({s0}, 1)"


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
    count: bool = False  # a send: its bursts counted by the receiver
    credit: bool = False  # a streamed send: sent under credits
    alt: object = None  # a streamed send: the odd trips' descriptor
    rows_w: int = 0  # rows mode: words an L1 row holds
    bcast: bool = False  # rows mode: one row, read by every row of the value
    shared: object = None  # a store packed over this load's buffers (no room)

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


#: How a stream loop lowers: "band" (one trip in one L1 tile, row groups
#: interleaved), "modulo" (trips software-pipelined, `modulo.py`, where it
#: covers the loop) or "auto" (the fewer modelled cycles over every trip).
STRATEGY = "auto"


def _best(body, par, env, name, fail, plan, land) -> tuple:
    """The candidate image (every band shape, the modulo schedule; `STRATEGY`)
    the core's timing (`timing.cycles`) of the whole image gives the fewest
    cycles, those cycles its cost."""
    best = None

    def take(got) -> None:
        nonlocal best
        c = timing.cycles(got[0], name, body.vregs, body.vcopies)
        if best is None or c < best[2]:
            best = (got[0], got[1], c, got[3], c)

    if STRATEGY != "modulo":
        # On a tie the narrower band with its unpacks ahead wins: the model does
        # not see the lookahead, and on the card it is worth 10% (silu, band 1).
        for band in (1, 2, 4, 8, 16):
            for remat in (False, True):
                for ahead in (True, False):
                    try:
                        take(
                            _lower(
                                body,
                                par,
                                env,
                                name,
                                fail,
                                band,
                                remat,
                                ahead,
                                plan,
                                land,
                            )
                        )
                    except Pressure:
                        continue
    if STRATEGY != "band":
        got = _modulo(body, par, env, name, fail, plan, land)
        if got is not None:
            take(got)
    if best is None:
        why = f" (modulo: {MODULO_WHY[0]})" if STRATEGY == "modulo" else ""
        fail(
            par.st,
            f"the stream loop needs more than {body.vregs} vector registers{why}",
        )
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
        if op.op == "recv" and inloop:
            out[id(op)] = _recv_stream(body, op, env, me, trips, fail, links)
            continue
        if op.op == "recv":
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
        to = f"mg[{dst.slices[0].value}]"
        stream = getattr(links, "stream", lambda *a: None)(me, to, side)
        if stream is not None:
            # One transfer a trip into the cluster's two slots, by parity.
            per = (r // 4) * (c // 32)
            if trips % 2:
                fail(op.st, "a streamed send runs an even number of trips")
            xs = [links.send(op.st, me, to, side, per, (r, c)) for _ in range(trips)]
            slots = (xs[0].at, xs[1].at if trips > 1 else xs[0].at)
            if any(x.at != slots[k % 2] for k, x in enumerate(xs)):
                fail(op.st, f"{to}'s ring takes two of these at a time")
            out[id(op)] = replace(
                xs[0], at=min(slots), size=per * len(set(slots)), slots=slots, per=per
            )
            continue
        shape = (r * trips, c)
        entries = (r // 4) * (c // 32) * trips
        out[id(op)] = links.send(op.st, me, to, side, entries, shape)
    return out


@dataclass
class _StreamIn:
    """A stream into this core: the trip's sub-tiles land in `slots` by
    parity, `size` words each, from `src`."""

    src: str
    dst: str
    slots: tuple
    size: int
    shape: tuple
    flow: bool = True


def _recv_stream(body, op, env, me, trips, fail, links) -> _StreamIn:
    """`x = recv mg[U] : f16[R, C] @l1(gt4)` in the stream loop: each trip
    takes the next tile cluster U streams into this core's ring."""
    t = op.type
    pos = op.st.positional()
    if t is None or t.space != "l1" or t.dtype != "f16" or len(pos) != 1:
        fail(op.st, "NAME = recv mg[U] : f16[R, C] @l1(gt4)")
    if _mode(t) != "gt4":
        fail(op.st, "a cluster streams sub-tiles: @l1(gt4)")
    src = value(body, op.st, pos[0], env)
    if not (isinstance(src, View) and src.name == "mg" and len(src.slices) == 1):
        fail(op.st, "a core receives from a cluster: recv mg[U]")
    unit = f"mg[{src.slices[0].value}]"
    words = t.shape[0] * t.shape[1] // 16
    if not hasattr(links, "take_stream"):  # costed alone: two slots at the top
        lo = L1_WORDS - 2 * words
        return _StreamIn(unit, me, (lo, lo + words), words, t.shape)
    if links.stream(unit, me, "l1") is None:
        fail(op.st, f"{unit} streams nothing into {me}")
    if trips % 2:
        fail(op.st, "a streamed recv runs an even number of trips")
    xs = [links.take_stream(op.st, unit, me, "l1", words) for _ in range(trips)]
    slots = (xs[0].at, xs[1].at if trips > 1 else xs[0].at)
    if any(x.at != slots[k % 2] for k, x in enumerate(xs)):
        fail(op.st, f"{unit}'s ring into {me} holds two of these tiles")
    return _StreamIn(unit, me, slots, words, t.shape)


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
            dest, _, srcs = tmpl.partition(",")
            reads = [syms[int(i)] for i in re.findall(r"\{(\d+)\}", srcs)]
            if "{0}[" in dest or " if=" in tmpl:
                reads = [*reads, syms[0]]  # a partial write keeps the rest: a read too
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
    g.code = banks.rotate(g.code, body.vcopies)
    cost = estimate(g.code)
    whole = estimate(_expand(g.code, len(ctx.trips) // 2))
    lines = _assign(g, ctx.pinned, fail, par.st, body.vregs)
    # One parameter a memory descriptor, its base the run's argument: the
    # instances of one shape share the image (one encoding, one IMEM load).
    mem = [d for d in ctx.descs if not d.to]
    params = [f"{d.buffer}+{d.off}" for d in mem]
    head = [f"image {name}({', '.join(f'p_a{d.ad}' for d in mem)})"]
    for d in ctx.descs:
        count = ", count=1" if d.count else ""
        base = f"peer({d.off}{count})" if d.to else f"p_a{d.ad}"
        head.append(f"    desc a{d.ad} = {base} walk=({d.walk})")
    for d in ctx.descs:
        if d.inc:
            head.append(f"    ainc a{d.ad} += {d.inc}")
    text = "\n".join(head + ["    " + ln for ln in _indent(lines)]) + "\n"
    return text, params, cost, ctx.accesses(), whole


#: IMEM words an image may take (a RUN starts below 512; the rest is the loop)
IMEM_ROOM = 1000
#: Raw lines one trip of the band lowering writes that the modulo emission
#: rebuilds itself (the L1 rings' fills, drains and syncs).
_REBUILT = ("vbar", "vsync pack on=drain", "vsync drain on=pack", "vdrain ")
#: Why the last `_modulo` call declined its loop (None when it did not).
MODULO_WHY: list = [None]
#: The stream tails the modulo lowering may emit (`modulo.TAILS`): "kernel"
#: over-reads each streamed input past its end, which the card's DRAM
#: answers (no buffer ends at its top).
MODULO_TAILS = MO.TAILS


def _no(why: str) -> None:
    MODULO_WHY[0] = why


def _modulo(body, par, env, name, fail, plan, land) -> tuple | None:
    """The stream loop software-pipelined (`modulo.py`): one trip's code from
    the band lowering with every row group in one band, scheduled as an
    iteration every II cycles; None where the loop is not a plain load ->
    compute -> store stream (a sticky block, a cluster stream, a banded
    unpack, a store of loaded bytes) or its image does not fit, the reason
    in MODULO_WHY."""
    MODULO_WHY[0] = None
    pre, loop = _split(par, fail)
    if loop is None:
        return _no("no stream loop")
    ops = _ops(loop.body)
    if any(isinstance(o, If) for o in loop.body):
        return _no("a sticky block")
    if any(o.op in ("recv", "send") for o in [*ops, *pre]):
        return _no("a cluster stream")
    ctx = _Ctx(body, env, fail, loop)
    ctx.ahead, ctx.band, ctx.remat = True, 1 << 10, False
    ctx.plan, ctx.land = plan or {}, land or L1_WORDS
    ctx.unit = _unit(body, par, env)
    ctx.pre = pre
    for op in loop.body:
        if isinstance(op, For):
            return _no("a nested loop")
        ctx.prepare(op, looped=True)
    if ctx.rel is not None or any(d.kind == "send" for d in ctx.descs):
        return _no("a banded unpack or a send")
    if any(d.shared is not None for d in ctx.descs):
        return _no("a store packed over a load (no L1 room for its own ring)")
    packed = {o.target for o in ops if o.op == "pack"}
    if any(
        o.op == "store" and o.st.positional()[2] not in {Name(t) for t in packed}
        for o in ops
    ):
        return _no("a store of loaded bytes")
    ctx.down = ctx.land
    for op in pre:
        ctx.prepare(op, looped=False)
    g = Gen()
    ctx.constants(g, [*pre, *ops])
    for op in pre:
        ctx.prologue(g, op)
    if any(op.op == "unpack" for op in pre):
        g.raw("vsync fe on=unpack")
    head_code = list(g.code)
    try:
        ctx.iteration(g, loop.body, 0, prefetch=False)
    except Pressure:
        return _no("one trip in one band exceeds the registers")
    placed = banks.rotate(g.code, body.vcopies)
    head_code, trip = placed[: len(head_code)], placed[len(head_code) :]
    for c in trip:
        if isinstance(c, str) and not c.startswith(_REBUILT):
            return _no(f"a raw line the emission does not rebuild: {c}")
    pinned = {s: r for r, s in enumerate(sorted(ctx.pinned, key=lambda x: int(x[1:])))}
    mops = [
        MO.parse(t, syms, body.vcopies)
        for t, syms in (c for c in trip if isinstance(c, tuple))
    ]
    if not mops:
        return _no("no vector ops in the trip")
    room = ctx.land - ctx.top
    # The stream's rings: the loop's loads and its stores (a load before the
    # loop is filled once, in the head).
    streamed = [d for d in ctx.descs if d in ctx.loads or d.kind == "store"]
    per_iter = sum(d.words for d in streamed)
    k_max = min(8, 2 + room // max(per_iter, 1))
    lower = max(sum(o.occ for o in mops if o.eng == e) for e in "MUP")
    ii_max = 4 * lower + 400
    lead = MO.lead(sum(d.words for d in ctx.loads))
    head = []
    for c in head_code:
        if isinstance(c, str):
            head.append(c)
        else:
            tmpl, syms = c
            head.append(tmpl.format(*(f"v{pinned[s]}" for s in syms)))
    mem = [d for d in ctx.descs if not d.to]
    params = [f"{d.buffer}+{d.off}" for d in mem]
    out = [f"image {name}({', '.join(f'p_a{d.ad}' for d in mem)})"]
    for d in ctx.descs:
        out.append(f"    desc a{d.ad} = p_a{d.ad} walk=({d.walk})")
    for d in ctx.descs:
        if d.inc:
            out.append(f"    ainc a{d.ad} += {d.inc}")
    trips = len(ctx.trips)
    ii_min, tried = 1, []
    while True:
        p = MO.plan(mops, body.vregs, pinned, k_max, ii_max, lead, ii_min)
        if p is None:
            return _no(
                f"no II up to {ii_max} colours in {body.vregs} registers (K <= {k_max})"
                f" with an image in IMEM: {', '.join(tried) or 'none planned'}"
            )
        top, rings = ctx.top, []
        for d in streamed:
            own = tuple(d.buf[: p.k])
            extra = tuple(top + i * d.words for i in range(p.k - len(own)))
            top += d.words * len(extra)
            rings.append(MO.Ring(d.ad, d.kind, d.buf[0], d.words, own + extra))
        best = None
        for tail in MODULO_TAILS:
            lines, cycles = MO.emit(p, rings, trips, pinned, tail)
            if lines is None or top > ctx.land:
                tried.append(
                    f"II {p.ii} K {p.k} {tail}: {'L1' if lines else 'no loop body'}"
                )
                continue
            text = (
                "\n".join(
                    out + ["    " + ln for ln in _indent([*head, *lines, "halt"])]
                )
                + "\n"
            )
            words = _imem_words(text, name, len(mem), body.vregs)
            tried.append(f"II {p.ii} K {p.k} S {p.stages} {tail}: {words} words")
            if words <= IMEM_ROOM and (best is None or cycles < best[1]):
                best = (text, cycles)
        if best is not None:
            ctx.top = top
            return best[0], params, best[1], ctx.accesses(), best[1]
        ii_min = p.ii + max(1, p.ii // 16)


def _imem_words(text: str, name: str, nparams: int, vregs: int) -> int:
    """The IMEM words image `name` of `text` encodes to (its configuration
    words included)."""
    mod = replace(TM.read(text, f"<{name}>"), vregs=vregs)
    words, _ = EI.image(mod, name, (0,) * nparams)
    return len(words)


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
        #: registers of the stream loop's widest row value
        self.tile = 1

    def index(self, v: "Val", r: int) -> int:
        """Which of `v`'s registers row register `r` of the loop reads: its
        own; a band value's (a 32-wide GT4 value beside wider ones: one
        register a band) the band's; a one-register value's only."""
        if v.rows >= self.tile:
            return r
        if self.per_band and v.cols == 32 * self.per_band and v.rows == self.per_band:
            # One band at the loop's width (a row broadcast before the loop).
            return r % self.per_band
        if self.per_band and v.rows * self.per_band == self.tile:
            return r // self.per_band
        if v.rows == 1:
            return 0
        raise ValueError(f"a {v.rows}-register value in a {self.tile}-register loop")

    def once(self, v: "Val", rows) -> list:
        """``[(row, register)]``: each of `v`'s registers `rows` covers, at
        the first row reading it."""
        out, seen = [], set()
        for r in rows:
            i = self.index(v, r)
            if i not in seen:
                seen.add(i)
                out.append((r, i))
        return out

    def accesses(self) -> list:
        u, out, trips = self.unit, [], len(self.trips)
        for d in self.descs:
            if d.to:
                x = self.plan[d.kind_op]
                key = (d.to, "AB"[d.side])
                if getattr(x, "flow", False):
                    # The receiver posts credits only after its own fills.
                    out.append(
                        sync.Access(u, True, key, x.at, x.at + x.size, flow=True)
                    )
                    continue
                # COUNTed: the cluster takes it by `fill ... recv`.
                out += [
                    sync.Access(u, True, key, x.at, x.at + x.size),
                    sync.Access(u, True, (d.to, "rx"), 0, 1),
                ]
                continue
            lo, hi = d.span(trips if d.inc else 1)
            out.append(sync.mem(u, d.kind == "store", d.buffer, lo, hi))
        for x in self.plan.values():
            if isinstance(x, _StreamIn):
                lo, hi = min(x.slots), max(x.slots) + x.size
                out.append(sync.Access(u, False, (u, "l1"), lo, hi, flow=True))
            elif x.dst == u:
                out.append(sync.Access(u, False, (u, "l1"), x.at, x.at + x.size))
        out.append(sync.Access(u, True, (u, "l1"), 0, max(self.top, 1)))
        if self.down < self.land:
            out.append(sync.Access(u, True, (u, "l1"), self.down, self.land))
        return out

    # ---------------------------------------------------------- memory
    def _walk(self, op, view: View, at, rows: bool = False):
        """``(buffer, byte offset, walk text, words, kind, rows_w, view
        rows)`` of a view: a row-major view is one run of whole rows (kind
        False); with `rows`, a band of a row-major f16 buffer whose L1 rows
        unpack as GT4 registers (kind "rows", `rows_w` words an L1 row; the
        rows a pitch apart); a view of a `tile=` buffer is bands of 4 rows,
        each a run of 4x4 sub-tiles (kind True)."""
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
                return (v.name, *_tiled(buf, ax), True, 0, 0)
            if buf.dtype in ("mx7a", "mx7b"):
                return (v.name, *_mx7(buf, ax), "mx7", 0, 0)
            off, dims, item = M.rowmajor_walk(buf, ax)
        except M.ViewError as e:
            self.fail(op.st, str(e))
        if rows:
            return (v.name, off, *self._rows_walk(op, buf, ax, dims, item))
        nbytes = item
        for n, _ in dims:
            nbytes *= n
        if item not in (2, 4) or not _contiguous(dims, item) or nbytes % 32:
            self.fail(
                op.st,
                "a row-major load or store view is contiguous f16 or f32, whole words",
            )
        return v.name, off, f"32, {nbytes // 32}", nbytes // 32, False, 0, 0

    def _rows_walk(self, op, buf, ax, dims, item) -> tuple:
        """``(walk, words, "rows", rows_w, view rows)`` of a band of a
        row-major f16 buffer: R rows a pitch apart, each W contiguous values
        (W a multiple of 32, a GT4 register's columns)."""
        shape = M.shape_of(ax)
        if item != 2:
            self.fail(op.st, "a GT4 band of a row-major buffer is f16")
        w = shape[-1]
        r = shape[0] if len(shape) == 2 else 1
        if w % 32:
            self.fail(
                op.st, f"a GT4 band is whole 32-column blocks; this view is {w} wide"
            )
        inner = [d for d in dims if d[1] == item]
        outer = [d for d in dims if d[1] != item]
        if (inner and inner[0][0] != w) or len(outer) > 1:
            self.fail(
                op.st, "a GT4 band of a row-major buffer is rows of contiguous columns"
            )
        row_words = w * 2 // 32
        if r == 1 or not outer:
            return f"32, {row_words * r}", row_words * r, "rows", row_words, r
        pitch = outer[0][1]
        if pitch % 32:
            self.fail(op.st, f"rows {pitch} bytes apart: a DMA walk steps whole words")
        if pitch == w * 2:
            return f"32, {row_words * r}", row_words * r, "rows", row_words, r
        return f"(32, {row_words}), ({pitch}, {r})", row_words * r, "rows", row_words, r

    def _packed(self, name):
        """The `pack` op that made `name`, in the loop or before it."""
        loop = _ops(self.loop.body) if self.loop is not None else []
        return next(
            (o for o in [*self.pre, *loop] if isinstance(o, Op) and o.target == name),
            None,
        )

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
            if t is None or t.space != "l1" or t.dtype not in ("f16", "f32"):
                self.fail(op.st, "a load is f16[R, C] or f32[R, C] @l1")
            view = pos[0] if isinstance(pos[0], View) else View(pos[0].text, ())
            item = 4 if t.dtype == "f32" else 2
            n = t.shape[0] * (t.shape[-1] if len(t.shape) == 2 else 1)
            want = n * item // 32
            rows = t.mode == "gt4"
        else:
            if len(pos) != 3 or not isinstance(pos[0], (View, Name)):
                self.fail(op.st, "store VIEW <- VALUE")
            view = pos[0] if isinstance(pos[0], View) else View(pos[0].text, ())
            want = None
            packed = self._packed(pos[2].text) if isinstance(pos[2], Name) else None
            rows = (
                packed is not None
                and packed.type is not None
                and (packed.type.mode == "gt4")
            )
        first = self.trips[0] if looped else None
        name, off, walk, words, kind, rows_w, vrows = self._walk(op, view, first, rows)
        bcast = (
            kind == "rows" and op.op == "load" and vrows == 1 and op.type.shape[0] > 1
        )
        if bcast:
            want = rows_w
        if want is not None and words != want:
            self.fail(op.st, f"the view holds {words} words, the value {want}")
        if op.op == "load" and kind == "mx7":
            self.fail(
                op.st, "an MXFP7 buffer is a cluster's operand: a core stores into it"
            )
        if op.op == "load" and bool(kind) != (op.type.mode == "gt4"):
            self.fail(op.st, "a tile= buffer loads @l1(gt4); a row-major one @l1")
        if (
            not kind
            and op.op == "load"
            and len(op.type.shape) == 2
            and op.type.shape[-1] != ROW
        ):
            self.fail(
                op.st,
                f"a row-major load is f16[R, {ROW}], or a GT4 band (@l1(gt4)) of "
                "whole 32-column blocks",
            )
        if op.op == "load" and len(op.type.shape) == 1 and op.type.dtype != "f16":
            self.fail(op.st, "a row statistic loads from f16")
        inc = 0
        if looped and len(self.trips) > 1:
            inc = self._walk(op, view, first + 1, rows)[1] - off
            end = self._walk(op, view, self.trips[-1], rows)[1]
            if end != off + inc * (len(self.trips) - 1):
                self.fail(op.st, "the view is not affine in the loop variable")
            if not -(1 << 17) <= inc < 1 << 17:
                self.fail(op.st, f"a {inc}-byte step: ainc takes 18 signed bits")
        if len(self.descs) == DESCS:
            self.fail(op.st, f"more than {DESCS} memory views")
        shared = None
        if looped and op.op == "store" and self.top + 2 * words > self.land:
            # No room for its own two buffers: the store packs over a load of
            # the trip whose bytes it replaces (same words, same L1 layout).
            shared = next(
                (
                    x
                    for x in self.loads
                    if x.words == words
                    and x.rows_w == (rows_w if kind == "rows" else 0)
                    and not any(s.shared is x for s in self.descs)
                ),
                None,
            )
        if shared is not None:
            buf = shared.buf
        elif looped:
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
        d.rows_w, d.bcast = (rows_w, bcast) if kind == "rows" else (0, False)
        d.shared = shared
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
            count=True,
        )
        # Only a stream's bursts are sent under credits.
        d.credit = getattr(x, "flow", False)
        if getattr(x, "slots", ()):
            # A stream: one descriptor a slot, the trips alternating.
            d.off, d.inc = 4 * x.slots[0], 0
            if len(self.descs) + 1 == DESCS:
                self.fail(op.st, f"more than {DESCS} memory views")
            d.alt = replace(d, ad=d.ad + 1, off=4 * x.slots[1])
            self.descs.append(d)
            self.descs.append(d.alt)
            self.of[id(op)] = d
            return
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
            # The cluster's drain is COUNTed: unpack once its granules landed.
            g.raw(f"vwait unpack rx n={x.size}")
            self.vals[op.target] = Val(
                "l1", _count(op.type), buf=x.at, mode=_mode(op.type)
            )
        elif op.op == "load":
            d = self.of[id(op)]
            g.raw(f"vfill a{d.ad} -> l1[{d.buf[0]}]")
            g.raw("vbar")
            self.vals[op.target] = _l1val(d, op.type, d.buf[0])
        elif op.op == "unpack":
            src = self._get(op, op.st.positional()[0], "l1")
            if len(op.type.shape) == 1:
                self.fail(op.st, "a row statistic unpacks in the stream loop")
            v = Val("row", _count(op.type), mode=src.mode, cols=op.type.shape[-1])
            for r in range(v.rows):
                s = g.sym()
                v.regs[r] = s
                self.pinned.add(s)
                g.emit(_unpack_line(src, r), s)
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
        # Row values made before the loop and read in it set its width too.
        before = [
            self.vals[a.name if isinstance(a, View) else a.text]
            for o in ops
            for a in o.st.positional()
            if isinstance(a, (Name, View))
            and (a.name if isinstance(a, View) else a.text) in self.vals
        ]
        before = [v for v in before if v.kind == "row" and v.mode == "gt4" and v.cols]
        if gt4:
            # A GT4 register is a 4-row band's 32 columns: a row statistic is
            # one register a band, every chunk holding its row's value.
            typed = [o for o in typed if len(o.type.shape) == 2]
            # One width for the rows; 32-wide values (one register a band:
            # a statistic stored or loaded back) beside them.
            widths = {o.type.shape[1] for o in typed if _mode(o.type) == "gt4"}
            widths |= {v.cols for v in before}
            wide = max(widths)
            if wide % 32 or widths - {wide, 32}:
                self.fail(ops[0].st, "GT4 values of one width, a multiple of 32")
            self.per_band = wide // 32
        rows = {_count(o.type) for o in typed}
        tile = max(rows | {v.rows for v in before if gt4})
        self.tile = tile
        # A 32-wide GT4 value beside wider rows takes one register a band; a
        # band at the loop's width (a row broadcast) one register a block.
        narrow = (
            {tile // self.per_band, self.per_band} if gt4 and self.per_band else set()
        )
        if any(r not in {tile, 1, *narrow} for r in rows):
            self.fail(
                ops[0].st, f"one register count in the stream loop, got {sorted(rows)}"
            )
        group = self.per_band if gt4 else 8 if reduces else min(4, tile)
        if tile % group:
            self.fail(ops[0].st, f"{tile} rows do not split into groups of {group}")
        stores = [o for o in ops if o.op in ("store", "send")]
        packed = {o.target for o in ops if o.op == "pack"}
        # A store of loaded bytes as they are: its drain reads a fill's buffer.
        direct = [
            o
            for o in stores
            if o.st.positional()[0 if o.op == "send" else 2]
            not in {Name(t) for t in packed}
        ]
        shared = [o for o in stores if self.of[id(o)].shared is not None]
        g.raw("vbar")
        if prefetch:
            g.raw("vsync fill on=unpack")
            if direct or shared:
                # The buffer refilled now was drained last trip.
                g.raw("vsync fill on=drain")
            for d in self.loads:
                g.raw(f"vfill a{d.ad} -> l1[{d.buf[1 - parity]}] rel")
        streams = [o for o in ops if o.op == "recv"]
        for o in ops:
            if o.op == "load":
                d = self.of[id(o)]
                self.vals[o.target] = _l1val(d, o.type, d.buf[parity])
            elif o.op == "recv":
                x = self.plan[id(o)]
                self.vals[o.target] = Val(
                    "l1", _count(o.type), buf=x.slots[parity], mode="gt4"
                )
                # The unpacks wait for the trip's sub-tiles to have landed.
                g.raw(f"vwait unpack rx n={x.size}")
        local: dict = {}
        groups = [range(k, k + group) for k in range(0, tile, group)]
        if self.band < len(groups) and len(groups) % self.band:
            raise Pressure
        width = min(self.band, len(groups))
        # The flag covers every row's math: one band holds them all.
        if any(isinstance(o, If) for o in body) and width != len(groups):
            raise Pressure
        bands = [groups[k : k + width] for k in range(0, len(groups), width)]
        fused = _fusions(ops)
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
                    o.op in ("load", "store", "send", "recv", "unpack", "pack")
                    or o.target in fused
                ):
                    continue
                if self.remat and first_reduce is not None and i > first_reduce:
                    self._reunpack(g, ops, o, rws, local, again)
                self._compute(g, o, grps, rws, local, fused)
            if k == 0:
                g.raw(f"vsync pack on=drain slack={len(stores)}")
                if shared:
                    # Packed over the trip's load: its unpacks have read it.
                    g.raw("vsync pack on=unpack")
            self._packs(g, ops, rws, local, parity)
        for o in streams:
            # Once unpacked, the slot goes back to the cluster as credits.
            x = self.plan[id(o)]
            g.raw("vsync drain on=unpack")
            g.raw(f"vpost {x.src} n={x.size}")
        g.raw("vsync drain on=pack")
        if direct:
            # This trip's fills have landed (the next trip's, just issued, may not).
            g.raw(f"vsync drain on=fill slack={len(self.loads) if prefetch else 0}")
        for o in stores:
            d = self.of[id(o)]
            if d.alt is not None and parity:
                d = d.alt
            name = o.st.positional()[0 if o.op == "send" else 2]
            src = self._get(o, name, "l1", local)
            at = d.buf[parity] if src.buf is None else src.buf
            peer = f" to={d.to} buf={d.side}" if d.to else ""
            if d.credit:
                g.raw(f"vwait drain cr n={d.words}")
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
            if len(o.type.shape) == 1:
                self._unpack_stat(g, o, src, rows, local)
                continue
            if _mode(o.type) != src.mode:
                self.fail(o.st, f"an @l1({src.mode}) value unpacks to @reg({src.mode})")
            v = local.setdefault(
                o.target,
                Val("row", _count(o.type), mode=src.mode, cols=o.type.shape[-1]),
            )
            for _, i in self.once(v, rows):
                s = g.sym()
                v.regs[i] = s
                g.emit(_unpack_line(src, i), s)

    def _unpack_stat(self, g, o, src, rows, local) -> None:
        """A row statistic loaded from memory (16 rows a word): each GT4 band's
        register, chunk 4q + i holding row i's value in every lane -- the
        band's four values broadcast across the lanes of chunks 0-3, then
        copied to 4-7."""
        if not self.per_band:
            self.fail(
                o.st, "a loaded row statistic is a GT4 band's (rows of @reg(gt4))"
            )
        v = local.setdefault(o.target, Val("stat", o.type.shape[0], mode="gt4"))
        words: dict = {}
        for b in sorted({r // self.per_band for r in rows}):
            w, lane = divmod(4 * b, 16)
            if w not in words:
                words[w] = g.sym()
                g.emit(f"vunpk {{0}} <- l1[{src.buf + w}] f16 n=1", words[w])
            d = g.sym()
            g.emit(
                f"vmul {{0}}[0:], k1, bcast({{1}}[0], lane={lane}, sh=0) vl=64",
                d,
                words[w],
            )
            g.emit("vmov {0}[4:], {0}[0:] vl=64", d)
            v.regs[b] = d

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
            cols = range(self.per_band)
            arg = pos[0]
            if isinstance(arg, View):
                # `x[:, C0:C1]`: the band's registers of those 32-column blocks.
                rows_s, cols_s = arg.slices if len(arg.slices) == 2 else (None, None)
                lo = value(self.body, o.st, cols_s.lo, self.env) if cols_s else None
                hi = value(self.body, o.st, cols_s.hi, self.env) if cols_s else None
                if (
                    rows_s is None
                    or rows_s.lo is not None
                    or rows_s.hi is not None
                    or not isinstance(lo, Int)
                    or not isinstance(hi, Int)
                    or lo.value % 32
                    or hi.value % 32
                    or not 0 <= lo.value < hi.value <= 32 * self.per_band
                ):
                    self.fail(
                        o.st, "a reduction over X[:, C0:C1]: blocks of 32 columns"
                    )
                cols = range(lo.value // 32, hi.value // 32)
                arg = Name(arg.name)
            # The reduced value's registers, or with a folded product its two
            # factors' (a pair a register).
            got = [self._get(o, t, "row", local) for t in fused.get(name, (arg,))]
            vals = [v for v in got if v is not None]  # `_get` fails on a missing one
            out.mode = "gt4"
            trees = []
            for grp in grps:
                band = grp[0] // self.per_band
                into = out.regs.get(band) if inplace else None
                rs = [r for r in grp if r % self.per_band in cols]
                regs = [
                    (
                        tuple(v.regs[self.index(v, r)] for v in vals)
                        if len(vals) == 2
                        else vals[0].regs[r]
                    )
                    for r in rs
                ]
                steps, out.regs[band] = _tree4(g, code, regs, into, len(vals) == 2)
                trees.append(steps)
            _interleave(g, trees)
            return
        trees = []
        for rws in grps:
            if len(rws) != 8:
                self.fail(o.st, "a reduction runs over groups of 8 rows")
            if name in fused:
                a, b = (self._get(o, t, "row", local) for t in fused[name])
                rows = [
                    (a.regs[self.index(a, r)], b.regs[self.index(b, r)]) for r in rws
                ]
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
                only = _read_once(ops, o)
                for r in rows:
                    t = g.sym()
                    # GT4 holds row i, column half q in chunk 4q + i; the
                    # quantiser takes lane i's 32 values from chunks 2i, 2i + 1.
                    if not (only and _quantiser_order(g, src.regs[r], t)):
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
            d = self.of[id(store[0])]
            for _, i in self.once(src, rows):
                g.emit(_pack_line(d, buf, i, mode, o.type.dtype), src.regs[i])
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
        d = self.of[id(store[0])]
        buf = d.buf[parity]
        bands = sorted({r // self.per_band for r in rows})
        for b in bands:
            g.emit(_pack_line(d, buf, b, "gt4", "f16"), stat.regs[b])
        local[o.target] = Val("l1", len(bands), buf=buf, mode="gt4")

    def _xlane(self, g, o, rows, local) -> None:
        """``y = xlane x, K``: lane l of every chunk takes lane l xor K of
        `x` (the crossbar on a multiply by 1.0: exact)."""
        t = o.type
        k = _xlane_k(o, self.fail)
        if len(t.shape) == 1:
            self.fail(o.st, "xlane runs on rows (f32[R, C] @reg)")
        out = local.setdefault(
            o.target, Val("row", _count(t), mode=_mode(t), cols=t.shape[-1])
        )
        src = o.st.positional()[0]
        for r, i in self.once(out, rows):
            v = self._get(o, src, "row", local)
            d = out.regs[i] if self.inplace and o.target in local else g.sym()
            g.emit(f"vmul {{0}}, k1, xor({{1}}, {k})", d, v.regs[self.index(v, r)])
            out.regs[i] = d

    def _math(self, g, o, rows, local) -> None:
        pos = o.st.positional()
        t = o.type
        if t is None or t.space != "reg" or o.target is None:
            self.fail(o.st, f"NAME = {o.op} ... : f32[..] @reg")
        if o.op == "xlane":
            self._xlane(g, o, rows, local)
            return
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
        out = local.setdefault(
            o.target, Val("row", _count(t), mode=_mode(t), cols=t.shape[-1])
        )
        for r, i in self.once(out, rows):
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
                    syms.append(v.regs[self.index(v, r)])
                    srcs.append(f"{{{len(syms)}}}")
            d = out.regs[i] if inplace else g.sym()
            if o.op in CMP:
                g.emit(f"{code} p1, {', '.join(srcs)}", d, *syms)
                g.emit("vmov {0}, k0", d)
                g.emit("vmov {0}, k1 if=p1", d)
            elif o.op == "sel":
                c, a, b = srcs
                g.emit(f"vsel {{0}}, {a}, {b}, {c}", d, *syms)
            else:
                g.emit(f"{code} {{0}}, {', '.join(srcs)}", d, *syms)
            out.regs[i] = d


def _xlane_k(o, fail) -> int:
    pos = o.st.positional()
    k = _number(pos[1]) if len(pos) == 2 else None
    if k is None or k != int(k) or not 0 < k < 16:
        fail(
            o.st, "xlane X, K: every 16 lanes of a chunk read lane l xor K (0 < K < 16)"
        )
    return int(k)


def _read_once(ops, pack) -> bool:
    """Whether `pack` is the only op reading its source value."""
    name = pack.st.positional()[0]
    name = name.text if isinstance(name, Name) else None
    readers = [
        u
        for u in ops
        if any(
            (isinstance(a, Name) and a.text == name)
            or (isinstance(a, View) and a.name == name)
            for a in u.st.positional()
        )
    ]
    return name is not None and readers == [pack]


_PLAIN = re.compile(r"^\{\d+\}$|^[sk]\d+$")


def _quantiser_order(g: Gen, sym: str, into: str) -> bool:
    """Rewrite the full-register op that made `sym` (read by nothing else) as
    eight one-chunk ops writing `into` in the MXFP7 quantiser's chunk order
    (chunk 2i + q from GT4 chunk 4q + i of every register operand). False
    when no such op is found."""
    at = None
    for j in range(len(g.code) - 1, -1, -1):
        c = g.code[j]
        if not isinstance(c, tuple) or sym not in c[1]:
            continue
        if c[1][0] == sym and at is None:
            at = j
            break
        return False
    if at is None:
        return False
    tmpl, syms = g.code[at]
    if " vl=" in tmpl or " if=" in tmpl or "[" in tmpl or sym in syms[1:]:
        return False
    op, _, rest = tmpl.partition(" ")
    parts = [p.strip() for p in rest.split(",")]
    if (
        op.startswith("vcmp")
        or parts[0] != "{0}"
        or not all(_PLAIN.match(p) for p in parts[1:])
    ):
        return False
    # The sources' symbols renumbered after the new destination.
    srcs = list(syms[1:])
    out = []
    for i in range(4):
        for q in range(2):
            ops_ = [
                f"{{{int(p[1:-1])}}}[{4 * q + i}:]" if p.startswith("{") else p
                for p in parts[1:]
            ]
            out.append(
                (f"{op} {{0}}[{2 * i + q}:], {', '.join(ops_)} vl=16", (into, *srcs))
            )
    g.code[at : at + 1] = out
    return True


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
                (isinstance(a, Name) and a.text == o.target)
                or (isinstance(a, View) and a.name == o.target)
                for a in u.st.positional()
            )
        ]
        a, b = o.st.positional()
        if (
            len(readers) == 1
            and readers[0].op == "rsum"
            and list(readers[0].st.positional()) == [Name(o.target)]
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


def _tree4(g, code, regs, into=None, fused=False) -> tuple:
    """A 4-row band's row reduction over its GT4 registers (row i, columns
    16q.. in chunk 4q + i): the registers folded pairwise, chunk 4 + i onto
    chunk i, the 16 lanes by an xor butterfly (8, 4, 2, 1: every lane ends
    with the total), chunks 0-3 copied to 4-7 -- so chunk 4q + i holds row
    i's value in every lane, an elementwise operand to the band's registers.
    With `fused`, `regs` are (a, b) pairs whose products are summed: the
    first fold is a multiply and a fused multiply-add, the product never
    held. ``(steps, the result's symbol)``."""
    steps, syms = [], list(regs)
    if fused:
        muls, fmas, nxt = [], [], []
        for k in range(0, len(syms), 2):
            d = g.sym()
            muls.append(("vmul {0}, {1}, {2}", d, *syms[k]))
            if k + 1 < len(syms):
                fmas.append(("vfma {0}, {1}, {2}, {0}", d, *syms[k + 1]))
            nxt.append(d)
        # Every multiply first: no add waits on the multiply just before it.
        steps.append(muls + fmas)
        syms = nxt
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


def _assign(g: Gen, pinned: set, fail, st, regs: int) -> list:
    """Physical registers v0..v{regs-1} over the emitted order: a symbol holds
    its register from its first to its last appearance; a symbol first
    appearing as an instruction's result (template position 0) takes the
    register of one whose last appearance is that instruction."""
    last: dict = {}
    for i, c in enumerate(g.code):
        if isinstance(c, tuple):
            for s in c[1]:
                last[s] = i
    reg: dict = {}
    free = list(range(regs))
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
