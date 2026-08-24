"""Recording a kernel: a grid, some loops, and statements the project defines.

The framework owns the structure -- grid, loop, trace-once, symbolic bounds --
and none of the vocabulary: a statement's `kind` is an opaque string here.

A loop is RECORDED, not unrolled: `for k in loop(n)` yields one index and the
body runs once. Unrolling happens at compile time, when `n` is a number.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from kohakuaccel.lang.dims import Dim

_active: list = []


class RecordError(RuntimeError):
    """A kernel construct used outside a trace, or in the wrong place."""


@dataclass
class Stmt:
    """One recorded operation. `kind` is the project's vocabulary.

    `grid` is set only when the statement was written OUTSIDE a stage, and is
    then the extent the statement needs on its own.
    """

    kind: str
    args: dict = field(default_factory=dict)
    grid: tuple | None = None
    names: tuple = ()
    #: What this writes and reads. A unit does not wait between the programs of
    #: one stage, so a read-after-write inside one reads stale bytes: 79% error.
    writes: str | None = None
    reads: tuple = ()

    def __str__(self) -> str:
        inner = " ".join(f"{k}={v}" for k, v in self.args.items())
        return f"{self.kind} {inner}"


@dataclass
class Loop:
    """A counted loop whose bound may be symbolic until compile time."""

    var: "Index"
    count: object
    body: list = field(default_factory=list)

    def __str__(self) -> str:
        return f"loop {self.var} < {self.count}"


@dataclass
class Grid:
    """Instances of the kernel body, one per point. `axes` may be symbolic.

    `barrier` records whether the AUTHOR demanded a stage boundary here. When
    false the compiler may band this with a neighbour that wants the same split.
    `formed` records who chose the EXTENT, which is a different question: an
    extent the compiler derived is one it may revise, and the author's is not.
    """

    axes: tuple
    index: tuple
    body: list = field(default_factory=list)
    barrier: bool = True
    #: True when the COMPILER made this grid around statements that carried
    #: their own extent, rather than the author opening it.
    formed: bool = False


class _Arith(Dim):
    """Index arithmetic, recorded rather than evaluated."""

    def __mul__(self, other):
        return Affine("mul", self, other)

    __rmul__ = __mul__

    def __add__(self, other):
        return Affine("add", self, other)

    __radd__ = __add__

    def __sub__(self, other):
        return Affine("sub", self, other)


@dataclass(frozen=True)
class Affine(_Arith):
    """An arithmetic combination of indices and integers."""

    op: str
    lhs: object
    rhs: object

    def resolve(self, env: dict) -> int:
        return _INDEX_OPS[self.op](_value(self.lhs, env), _value(self.rhs, env))

    def free(self) -> set:
        return set()

    def __str__(self) -> str:
        return f"({self.lhs} {self.op} {self.rhs})"


@dataclass(frozen=True)
class Index(_Arith):
    """A grid or loop index. Symbolic while tracing, an integer once compiled."""

    name: str

    def resolve(self, env: dict) -> int:
        if self.name not in env:
            raise RecordError(f"index {self.name} is not bound at this point")
        return int(env[self.name])

    def free(self) -> set:
        return set()

    def __str__(self) -> str:
        return self.name


class Trace:
    """What one tracing of a kernel produced: a sequence of stages.

    Each grid the body opens is a stage -- a set of independent instances on one
    kind of unit. Stages run in order with a barrier between them.
    """

    def __init__(self, name: str, knobs: dict | None = None) -> None:
        self.name = name
        #: The kernel's block parameters, so a statement given no tiling of its
        #: own takes the kernel's rather than a built-in default.
        self.knobs: dict = dict(knobs or {})
        #: Stages and the loops around them. A loop OUT HERE repeats a whole
        #: SEQUENCE of stages, not statements within one.
        self.top: list = []
        self.outer: list = [self.top]
        self.stack: list = []
        #: The Grid nodes `stack` holds bodies of. A statement that has to size a
        #: buffer over the whole tiling needs the extents, not just the body.
        self.grids: list = []
        self.loops: list = []
        #: Internal buffers, name -> symbolic shape. Somewhere for a value to
        #: live between stages when it needs one; no caller ever sees it.
        self.buffers: dict = {}
        #: Internal buffers with FIXED contents, name -> value. A mask is built
        #: at trace time, so the compiler uploads it; no caller passes one.
        self.tables: dict = {}
        #: Where a buffer should live, name -> the project's tier name. Only the
        #: ones that asked; a machine with no such tier places them anyway.
        self.tiers: dict = {}
        self._ids = 0

    def declare(self, prefix: str, shape: tuple, tier: str | None = None) -> str:
        """Register an internal buffer and return the name it is known by."""
        name = self.fresh(prefix)
        self.buffers[name] = tuple(shape)
        if tier:
            self.tiers[name] = tier
        return name

    def declare_table(self, prefix: str, shape: tuple, value) -> str:
        """Register an internal buffer whose contents are known now."""
        name = self.declare(prefix, shape)
        self.tables[name] = value
        return name

    @property
    def stages(self) -> list:
        """Every stage the body opened, ignoring how often a loop repeats it."""
        return [g for g in _walk(self.top)]

    @property
    def grid(self) -> "Grid | None":
        """The only stage, for a kernel that has one."""
        found = self.stages
        return found[0] if len(found) == 1 else None

    def innermost(self) -> "Index | None":
        """The index of the loop currently being recorded, if any."""
        return self.loops[-1] if self.loops else None

    def enclosing(self) -> "Grid | None":
        """The tiling being recorded, for a statement that must size over it."""
        return self.grids[-1] if self.grids else None

    def emit(
        self, kind: str, grid=None, names=(), writes=None, reads=(), top=False, **args
    ) -> Stmt:
        """Record one operation into whatever body is open.

        With no stage open the statement carries its own `grid` and goes to the
        top level. `top` puts it there ANYWAY, after the tiling being recorded
        rather than inside it -- for work a statement implies but that must run
        once over the whole result, not once per tile. Raises
        :class:`RecordError` if it has neither a stage nor a grid.
        """
        stmt = Stmt(kind, args, grid, tuple(names), writes, tuple(reads))
        if self.stack and not top:
            self.stack[-1].append(stmt)
            return stmt
        if top and grid is None:
            raise RecordError(f"{kind} runs after a tiling and states no grid")
        if grid is None:
            raise RecordError(
                f"{kind} outside a stage and with no grid of its own; either open "
                f"`with units(...)` or use a form that knows its own extent"
            )
        self.outer[-1].append(stmt)
        return stmt

    def fresh(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}{self._ids}"

    def pretty(self) -> str:
        lines = [f"kernel {self.name}"]
        if not self.top:
            return lines[0] + "\n  (no stage)"
        _render_top(self.top, lines, 1)
        return "\n".join(lines)


def _walk(items):
    """Every Grid in a top-level body, loops included, in source order."""
    for item in items:
        if isinstance(item, Loop):
            yield from _walk(item.body)
        elif isinstance(item, Grid):
            yield item


def _render_top(items, lines, depth) -> None:
    pad = "  " * depth
    for item in items:
        if isinstance(item, Loop):
            lines.append(f"{pad}{item}   (repeats the stages below)")
            _render_top(item.body, lines, depth + 1)
        elif isinstance(item, Stmt):
            axes = ", ".join(str(a) for a in item.grid)
            lines.append(f"{pad}{item}   [over {axes}, staged by the compiler]")
        else:
            axes = ", ".join(str(a) for a in item.axes)
            names = tuple(str(i) for i in item.index)
            lines.append(f"{pad}units ({axes}) as {names}")
            _render(item.body, lines, depth + 1)


def _render(body, lines, depth) -> None:
    pad = "  " * depth
    for item in body:
        if isinstance(item, Loop):
            lines.append(f"{pad}{item}")
            _render(item.body, lines, depth + 1)
        else:
            lines.append(f"{pad}{item}")


def active() -> Trace:
    """The trace being recorded. Raises outside a kernel."""
    if not _active:
        raise RecordError(
            "this is kernel syntax and there is no kernel being traced; call it "
            "inside a function decorated with @kernel"
        )
    return _active[-1]


def begin(name: str, knobs: dict | None = None) -> Trace:
    t = Trace(name, knobs)
    _active.append(t)
    return t


def end() -> None:
    _active.pop()


def _names(given, axes: int) -> list:
    """Index names for a tiling `axes` wide, unique against the ones open.

    Defaulting to `i, j, ...` from zero made a one-axis tiling inside another
    name its index `i` again, shadowing the outer one: the inner slice read the
    outer tile and the write landed at `y[i, i]`, silently.
    """
    if given:
        return list(given)
    at = sum(len(g.index) for g in active().grids)
    return [chr(ord("i") + at + n) for n in range(axes)]


class grid:
    """Instance the body over `axes`, without demanding a barrier after it.

    `for i, j in grid(gx, gy)` states how the work is split; whether it gets a
    stage of its own is the compiler's. Use :class:`stage` to demand a barrier.
    """

    def __init__(self, *axes, names=None) -> None:
        self.axes = axes
        self.names = names

    def __iter__(self) -> Iterator[Any]:
        """One pass, yielding the index or the tuple of them."""
        index = tuple(Index(n) for n in _names(self.names, len(self.axes)))
        with _opened(self.axes, index, barrier=False):
            yield index if len(index) > 1 else index[0]


class _opened:
    """The body of a grid or a stage, however it was spelled."""

    def __init__(self, axes, index, barrier: bool) -> None:
        self.axes, self.index, self.barrier = axes, index, barrier

    def __enter__(self):
        t = active()
        made = Grid(self.axes, self.index, barrier=self.barrier)
        # A tiling INSIDE a tiling is a VIEW of the enclosing tile: it repeats
        # the statements with its index bound and opens no stage of its own.
        (t.stack[-1] if t.stack else t.outer[-1]).append(made)
        t.stack.append(made.body)
        t.grids.append(made)
        return self.index

    def __exit__(self, *exc) -> None:
        t = active()
        t.stack.pop()
        t.grids.pop()


class stage:
    """A grid that IS a barrier: nothing after it starts until it finishes."""

    def __init__(self, *axes, names=None) -> None:
        self.axes = axes
        self.names = names

    def __enter__(self) -> Any:
        index = tuple(Index(n) for n in _names(self.names, len(self.axes)))
        self._body = _opened(self.axes, index, barrier=True)
        self._body.__enter__()
        return index if len(index) > 1 else index[0]

    def __exit__(self, *exc) -> None:
        self._body.__exit__(*exc)


#: The older spelling of :class:`stage`.
units = stage


class loop:
    """A counted loop, recorded once and unrolled when the bound is a number.

    Inside a stage it repeats STATEMENTS; outside one it repeats whole STAGES.
    """

    def __init__(self, count, name: str | None = None) -> None:
        self.count = count
        self.name = name

    def __iter__(self):
        t = active()
        var = Index(self.name or t.fresh("k"))
        node = Loop(var, self.count)
        where = t.stack if t.stack else t.outer
        where[-1].append(node)
        where.append(node.body)
        t.loops.append(var)
        yield var
        t.loops.pop()
        where.pop()


#: A compile-time repetition, whether of statements or of whole grids.
sweep = loop


def value_of(v, env: dict):
    """`v` as a number, resolving indices, extents and index arithmetic."""
    return _value(v, env)


def unroll(body, env: dict) -> list:
    """Flatten every loop and nested tiling in `body`, bound against `env`."""
    out: list = []
    for item in body:
        if isinstance(item, Loop):
            n = _value(item.count, env)
            for value in range(n):
                out += unroll(item.body, {**env, item.var.name: value})
        elif isinstance(item, Grid):
            for point in _grid_points([_value(a, env) for a in item.axes]):
                inner = {**env, **{i.name: v for i, v in zip(item.index, point)}}
                out += unroll(item.body, inner)
        else:
            out.append(_bind(item, env))
    return out


def _grid_points(extents: list):
    """Every coordinate of `extents`, last axis fastest."""
    out = [()]
    for extent in extents:
        out = [p + (i,) for p in out for i in range(extent)]
    return out


#: Arithmetic an :class:`Index` can record, resolved when the loop unrolls.
_INDEX_OPS = {
    "mul": lambda a, b: a * b,
    "add": lambda a, b: a + b,
    "sub": lambda a, b: a - b,
}


def _bind(stmt: Stmt, env: dict) -> Stmt:
    """A statement with every index and extent replaced by its value.

    Unrolling resolves the ARGUMENTS; it does not change what a statement reads
    or writes. Dropping those made `check_hazards` unable to fire for any kind.
    """
    return Stmt(
        stmt.kind,
        {k: _value(v, env) for k, v in stmt.args.items()},
        stmt.grid,
        stmt.names,
        stmt.writes,
        stmt.reads,
    )


def _value(v, env: dict):
    if isinstance(v, Index):
        if v.name not in env:
            raise RecordError(f"index {v.name} is not bound at this point")
        return env[v.name]
    if isinstance(v, Dim):
        return v.resolve(env)
    if isinstance(v, Affine):
        return _INDEX_OPS[v.op](_value(v.lhs, env), _value(v.rhs, env))
    # A project's own leaf may hold an index of its own. It knows its shape and
    # the framework does not, so it rebinds itself rather than being walked.
    if hasattr(v, "rebind"):
        return v.rebind(lambda inner: _value(inner, env))
    if isinstance(v, (list, tuple)):
        return type(v)(_value(x, env) for x in v)
    return v
