"""Trace once, compile per shape, and CALL rather than launch.

The decorator records a kernel's STRUCTURE with symbolic extents. Calling it
with device arrays solves the extents from their shapes, compiles for the
machine they live on, materialises each operand in the layout this compilation
asked for, allocates the results, dispatches, and hands the results back.

The trace is cached on the block parameters, which change what is recorded, and
not on the shapes. A `Backend` supplies the vocabulary: what a port looks like
inside the body, what byte order each port needs, and how a statement encodes.
"""

import functools
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from kohakuaccel.lang import iface
from kohakuaccel.lang.dims import free, resolve
from kohakuaccel.lang.record import (
    Grid,
    Index,
    Loop,
    Stmt,
    Trace,
    begin,
    end,
    unroll,
    value_of,
)
from kohakuaccel.lifetime import Footprint, Span, Writes, aliases, lifetimes, pack
from kohakuaccel.memory import Batched, OutOfMemory


class KernelError(ValueError):
    """A kernel that cannot be traced, compiled or called, and why."""


@runtime_checkable
class Backend(Protocol):
    """What a project supplies so its kernels mean something."""

    def unit_of(self, kinds: set) -> str:
        """Which unit type runs statements of these kinds."""

    def handle(self, port: iface.Port, knobs: dict):
        """What the kernel body sees for `port` while tracing."""

    def layouts(self, compiled: "Compiled") -> dict:
        """Buffer name -> the byte order this compilation needs it in."""

    def constants(self, compiled: "Compiled") -> dict:
        """Buffers with fixed contents, as name -> ``(array, layout)``."""

    def encode(self, compiled: "Compiled", stage: "Stage", addrs: dict) -> dict:
        """Grid coordinate -> instruction words for one stage."""


@dataclass
class Instance:
    """One grid point and the statements it runs."""

    at: tuple
    stmts: list


@dataclass
class Stage:
    """One grid of independent instances, all on the same kind of unit.

    `grid` is what runs; `body_grid` is what the kernel WROTE. They differ when a
    port is batched, because the batch becomes a leading grid axis the body never
    mentions -- the body describes one element of it.
    """

    index: int
    unit: str
    grid: tuple
    body_grid: tuple = ()
    batched: bool = False
    #: True when the author opened this with `stage`, not `grid`. A stage that
    #: nobody asked for may be banded into its neighbour.
    wanted_barrier: bool = True
    #: Coordinates this stage must run on, when the project named a peer inside
    #: an instruction and a freer placement would send the program elsewhere.
    nodes: tuple | None = None
    #: Instance -> ``(coord, count)`` completions to wait for on a node this
    #: stage never kicks, because a peer answers the transfer it started.
    acks: dict | None = None
    #: How to build one instance, given its coordinate. Set only for a grid the
    #: COMPILER formed, which is what licenses :func:`_resize` to change it.
    build: object = field(default=None, compare=False, repr=False)
    instances: list = field(default_factory=list)

    @property
    def kinds(self) -> set:
        return {s.kind for i in self.instances for s in i.stmts}

    def element(self, at: tuple) -> int:
        """Which batch element an instance coordinate belongs to."""
        return at[0] if self.batched else 0


@dataclass
class Compiled:
    """A kernel resolved for one machine, one set of extents and one tiling."""

    name: str
    machine: object
    signature: iface.Signature
    extents: dict
    knobs: dict
    #: Whether this compilation's grids were sized from the layouts. Part of
    #: :attr:`tag`: the two grids fold their constants to different lengths.
    regrid: bool = False
    stages: list = field(default_factory=list)
    layouts: dict = field(default_factory=dict)
    #: Internal buffers the body declared, as name -> resolved shape.
    temps: dict = field(default_factory=dict)
    #: Relayouts the stages imply, as ``(before stage, name, from, to)``.
    conversions: list = field(default_factory=list)
    #: Internal buffers whose contents the compiler already knows.
    tables: dict = field(default_factory=dict)
    #: The project's backend, so a compilation can price its own folded
    #: constants without a caller passing one back in.
    backend: object = field(default=None, compare=False, repr=False)
    _touch: list | None = field(default=None, init=False, compare=False, repr=False)
    _consts: dict | None = field(default=None, init=False, compare=False, repr=False)
    _placed: dict = field(default_factory=dict, init=False, compare=False, repr=False)

    def before(self, stage: int) -> list:
        """The conversions due before `stage` runs."""
        return [c for c in self.conversions if c[0] == stage]

    def orders(self, name: str) -> list:
        """Every byte order `name` is held in over this kernel's life."""
        seen = [self.layouts[name]]
        for _, who, _, after in self.conversions:
            if who == name:
                seen.append(after)
        return seen

    def widest(self, name: str):
        """The order `name` is held in that costs the most bytes.

        WHAT A SPAN IS SIZED FOR, and not always the order it ends in: a
        conversion rewrites a buffer where it lies, so a span sized for the
        order the kernel writes it in is overrun by any wider one after it.
        """
        shape = self.sized(name)
        return max(self.orders(name), key=lambda lay: lay.nbytes(shape))

    def final(self, name: str):
        """The order `name` holds its bytes in once the call has run.

        WHAT A RESULT IS LABELLED WITH. The first order is what the caller gets
        back only when nothing rewrote the buffer.
        """
        return self.orders(name)[-1]

    def sized(self, name: str) -> tuple:
        """The shape a layout prices `name` at.

        A table is sized for ONE batch element, because that is what it holds --
        every element reads the same copy, which is why `compile` is the one
        thing it does not wrap in :class:`~kohakuaccel.memory.Batched`.
        """
        return self.block(name) if name in self.tables else self.shape(name)

    @property
    def batch(self) -> int:
        """Batch elements this call covers. 1 when nothing is batched."""
        return int(self.extents.get(iface.BATCH, 1))

    def shape(self, name: str) -> tuple:
        """The full resolved shape of a port or an internal buffer."""
        if name in self.temps:
            lead = tuple(self.extents.get(iface.LEAD, ())) if self.batch > 1 else ()
            return lead + self.temps[name]
        return self.signature.port(name).resolve(self.extents)

    def block(self, name: str) -> tuple:
        """One batch element's shape, which is what a layout is chosen for."""
        if name in self.temps:
            return self.temps[name]
        return self.signature.port(name).resolve_trailing(self.extents)

    @property
    def grid(self) -> tuple:
        """The only stage's grid, for a kernel that has one stage."""
        return self.stages[0].grid

    @property
    def instances(self) -> list:
        return self.stages[0].instances

    def shape_of(self, port: str) -> tuple:
        """The resolved shape of one port."""
        return self.signature.port(port).resolve(self.extents)

    @property
    def statements(self) -> int:
        return sum(len(i.stmts) for s in self.stages for i in s.instances)

    # ------------------------------------------------------------------ memory
    def sizes(self) -> dict:
        """Every buffer this call places, and the bytes each needs.

        The WIDEST byte order it is held in, at the shape that order prices it
        at: see :meth:`widest` and :meth:`sized`. THIS IS THE ONE AUTHORITY ON
        WHAT A BUFFER COSTS -- the footprint, the affordability check, the
        aliasing guard and the allocation itself all read it, so a span the
        allocator hands out cannot disagree with the span the guard checks.
        """
        return {
            name: self.widest(name).nbytes(self.sized(name)) for name in self.layouts
        }

    def roles(self) -> dict:
        """Buffer name -> ``in``, ``out``, ``temp`` or ``table``."""
        out = {p.name: p.role for p in self.signature.ports if p.name in self.layouts}
        for name in self.temps:
            out[name] = "table" if name in self.tables else "temp"
        return out

    @property
    def tag(self) -> str:
        """This compilation's identity, for a cache a whole session shares.

        A FOLDED CONSTANT'S NAME NEED NOT IDENTIFY ITS VALUE: an extent folded
        to a scalar is named for the symbol, so `const<N>` is one name for every
        N while the array behind it is resolved per compilation. Measured on the
        shipped `layernorm`: 64x128 after 64x64 on one device scored 0.334
        against 8.8e-4, silently, reading the earlier shape's array.

        NOR ITS LENGTH: a folded array is one instance's STRIDE long, and the
        two grids stride differently. `linear_scale_separated` at M=2 N=6144 on
        the traced grid, after the same call on the layout-sized one, read that
        call's 8,192-long array over a 98,304-element pass: 1.00 error, silent.
        Appended only when set, so every tag already in a cache still names the
        compilation it named before.
        """
        parts = [f"{k}={v}" for k, v in sorted(self.extents.items())]
        parts += [f"{k}:{v}" for k, v in sorted(self.knobs.items())]
        if self.regrid:
            parts.append("regrid")
        return ",".join(parts)

    def constants(self) -> dict:
        """The backend's folded buffers, as name -> ``(array, layout)``.

        Empty when this compilation was given no backend, which is the only
        thing it needs one for. Cached: it is a sweep over every statement.
        """
        if self._consts is None:
            ask = getattr(self.backend, "constants", None)
            self._consts = dict(ask(self) or {}) if ask is not None else {}
        return self._consts

    def demand(self, align: int = 1) -> int:
        """Bytes this call needs with every buffer on a span of its own.

        The cheap upper bound :meth:`footprint` refines: one pass over the
        layouts, no lifetimes and no roles.
        """
        total = sum(_up(v, align) for v in self.sizes().values())
        for array, layout in self.constants().values():
            total += _up(layout.nbytes(getattr(array, "shape", ())), align)
        return total

    def touches(self) -> list:
        """Per stage, the buffers it reads from BEFORE it, and the ones it writes.

        Only names this kernel places; a statement naming anything else is
        ignored. A conversion counts as both, since it rewrites a buffer where
        it lies, and lands on the stage it runs before.

        A read of something the stage itself already wrote is NOT an incoming
        read -- a fused drain and its epilogue are one stage writing a temp and
        reading it straight back, and counting that as incoming makes every
        lifetime start one stage early. Cached: it is a sweep over every
        statement of every instance.
        """
        if self._touch is None:
            known = set(self.layouts)
            out = [(set(), set()) for _ in self.stages]
            for at, name, _, _ in self.conversions:
                if name in known and 0 <= at < len(out):
                    out[at][0].add(name)
                    out[at][1].add(name)
            for at, stage in enumerate(self.stages):
                reads, writes = out[at]
                for inst in stage.instances:
                    made: set = set()
                    for s in inst.stmts:
                        reads |= {r for r in s.reads if r in known and r not in made}
                        if s.writes in known:
                            writes.add(s.writes)
                            made.add(s.writes)
            self._touch = out
        return self._touch

    def bands(self) -> list[tuple]:
        """Temps the project wants placed ADJACENTLY, in order.

        Adjacency costs reuse -- a group is one allocation over every member's
        life -- so it is asked for, never assumed. A temp named twice keeps the
        first group: one buffer cannot sit in two places.
        """
        ask = getattr(self.backend, "bands", None)
        if ask is None:
            return []
        out: list[tuple] = []
        taken: set = set()
        for group in ask(self) or ():
            fresh = tuple(
                n
                for n in group
                if n in self.temps and n not in self.tables and n not in taken
            )
            if len(fresh) > 1:
                taken.update(fresh)
                out.append(fresh)
        return out

    def placement(self, align: int = 1) -> tuple:
        """Where each temp goes inside one block, and how big that block is.

        Temps whose lifetimes do not overlap share bytes. Everything else the
        call places is live throughout -- the caller owns the operands and the
        results, and a table or a folded constant outlives the call in the
        runtime's cache -- so only temps are ever put together.

        Returns ``(name -> offset, bytes)``, and ``({}, 0)`` with no temps.
        Cached per alignment: a call asks every time.
        """
        got = self._placed.get(align)
        if got is None:
            temps = [n for n in self.temps if n not in self.tables]
            if not temps:
                got = ({}, 0)
            else:
                sizes = {n: _up(v, align) for n, v in self.sizes().items()}
                keep = [n for n in sizes if n not in temps]
                life = lifetimes(self.touches(), sizes, keep=keep)
                got = pack(temps, sizes, life, align=align, groups=self.bands())
            self._placed[align] = got
        return got

    def footprint(self, align: int = 1, reuse: bool = False) -> Footprint:
        """What this call would need resident, without running anything.

        `align` is the allocator's granularity, which every span is rounded up
        to; `reuse` packs the temps by :meth:`placement` instead of giving each
        its own span, so what this predicts is what a call really does.

        Returns a :class:`~kohakuaccel.lifetime.Footprint`.
        """
        sizes = {n: _up(v, align) for n, v in self.sizes().items()}
        roles = self.roles()
        for name, (array, layout) in self.constants().items():
            sizes[name] = _up(layout.nbytes(getattr(array, "shape", ())), align)
            roles[name] = "const"

        temps = [n for n in self.temps if n not in self.tables]
        life = lifetimes(
            self.touches(), sizes, keep=[n for n in sizes if n not in temps]
        )
        offsets, scratch = self.placement(align) if reuse else ({}, 0)
        return Footprint(
            self.name,
            align,
            sizes,
            roles,
            life,
            offsets,
            scratch,
            bool(reuse),
            mesh=getattr(self.machine, "default", None),
        )

    def summary(self) -> str:
        shape = " ".join(
            f"{k}={v}"
            for k, v in sorted(self.extents.items())
            if not k.startswith("__")
        )
        runs: list = []
        for s in self.stages:
            if runs and runs[-1][0] == s.unit:
                runs[-1][1] += 1
            else:
                runs.append([s.unit, 1])
        chain = " ".join(f"{u}x{n}" if n > 1 else u for u, n in runs)
        return (
            f"{self.name}[{shape}] {len(self.stages)} stages ({chain}), "
            f"{self.statements} statements, {len(self.conversions)} relayouts"
        )

    def pretty(self) -> str:
        """The compiled IR: every stage, what it runs on, and what it costs."""
        lines = [self.summary()]
        for stage in self.stages:
            due = self.before(stage.index)
            for _, name, before, after in due:
                lines.append(f"  relayout {name}: {before.key} -> {after.key}")
            per = len(stage.instances[0].stmts) if stage.instances else 0
            lines.append(
                f"  stage {stage.index:>2} {stage.unit} grid{stage.grid} "
                f"{len(stage.instances)} instances x {per} instructions"
            )
        widest = max((len(n) for n in self.layouts), default=1)
        lines.append("  buffers:")
        for name, layout in sorted(self.layouts.items()):
            kind = "temp" if name in self.temps else "port"
            lines.append(
                f"    {name:<{widest}}  {kind}  {self.shape(name)}  {layout.key}"
            )
        return "\n".join(lines)


class Kernel:
    """A traced kernel: one structure per tiling, one compilation per shape."""

    def __init__(self, fn, backend: Backend) -> None:
        self._fn = fn
        self._backend = backend
        self._traces: dict = {}
        self._compiled: dict = {}
        self.signature = iface.read(fn)
        functools.update_wrapper(self, fn)

    @property
    def backend(self) -> Backend:
        return self._backend

    @property
    def traces(self) -> int:
        """Distinct structures recorded so far -- one per tiling, not per shape."""
        return len(self._traces)

    def trace(self, **knobs) -> Trace:
        """The recorded structure for this tiling, from cache.

        Raises :class:`KernelError` if the body opened no grid: a kernel says
        what ONE instance does, so it needs `with units(...)` around it.
        """
        settings = {**self.signature.knobs, **knobs}
        key = tuple(sorted(settings.items()))
        got = self._traces.get(key)
        if got is not None:
            return got

        handles = {
            p.name: self._backend.handle(p, settings) for p in self.signature.ports
        }
        # The trace sees every knob; the BODY sees only the ones it declared. A
        # knob the compiler turns is not part of the kernel's signature.
        declared = {k: v for k, v in settings.items() if k in self.signature.knobs}
        t = begin(getattr(self._fn, "__name__", "kernel"), settings)
        try:
            self._fn(**handles, **declared)
        finally:
            end()
        if not t.top:
            raise KernelError(
                f"{t.name} recorded no stage; a kernel describes what ONE instance "
                "does, so it needs `with units(...)` around its body"
            )
        self._traces[key] = t
        return t

    def compile(
        self, machine, extents: dict, regrid: bool = False, **knobs
    ) -> Compiled:
        """Resolve this kernel for `machine`, these extents and this tiling.

        `regrid` sizes every compiler-formed grid from the layouts instead of
        from the traced extent, which fills units the traced extent left idle
        and lengthens the instruction stream to do it. It is part of this
        compilation's IDENTITY, so a cache holding one cannot answer for the
        other -- the two emit different programs.
        """
        settings = {**self.signature.knobs, **knobs}
        key = (
            id(machine),
            tuple(sorted(extents.items())),
            tuple(sorted(settings.items())),
            bool(regrid),
        )
        got = self._compiled.get(key)
        if got is not None:
            return got
        try:
            out = self._build(machine, extents, regrid, settings)
        except Exception as exc:
            relax = getattr(self._backend, "relax", None)
            eased = relax(exc, settings) if relax is not None else None
            if eased is None:
                raise
            out = self._build(machine, extents, regrid, eased)
        self._compiled[key] = out
        return out

    def _build(self, machine, extents: dict, regrid: bool, settings: dict) -> Compiled:
        """One compilation attempt at one set of knobs."""
        t = self.trace(**settings)
        out = Compiled(
            t.name,
            machine,
            self.signature,
            dict(extents),
            settings,
            bool(regrid),
            backend=self._backend,
        )
        out.temps = {
            name: tuple(resolve(a, extents) for a in shape)
            for name, shape in t.buffers.items()
        }
        out.tables = dict(t.tables)
        for n, (stage, outer) in enumerate(_sequence(t.top, extents, self._backend)):
            missing: set = set()
            for axis in stage.axes:
                missing |= free(axis) - set(outer)
            if missing:
                raise KernelError(
                    f"{t.name} stage {n}: the grid needs {sorted(missing)}, which "
                    f"no operand supplied and no argument gave"
                )
            body_grid = tuple(resolve(a, outer) for a in stage.axes)
            names = [i.name for i in stage.index]
            batch = out.batch
            grid = (batch, *body_grid) if batch > 1 else body_grid
            made = Stage(
                n, "", grid, body_grid, batch > 1, getattr(stage, "barrier", True)
            )
            build = _builder(self._backend, out, made, stage.body, outer, names)
            made.instances = [build(point) for point in _points(grid)]
            # Kept only where it may be used again: a grid the COMPILER sized,
            # and only when this call asked for the layouts to resize it.
            if regrid and getattr(stage, "formed", False):
                made.build = build
            out.stages += _by_unit(self._backend, made)
        out.stages = _band(out.stages)
        for at, made in enumerate(out.stages):
            made.index = at

        out.layouts = self._backend.layouts(out)
        # AFTER the layouts: a region is an offset AND an extent, and an extent
        # is a layout fact. Before them the guard can only compare NAMES.
        for made in out.stages:
            check_hazards(t.name, made, self._backend, out)
        settle = getattr(self._backend, "validate", None)
        if settle is not None:
            settle(out)
        arrives = {p.name for p in self.signature.inputs}
        rewritten = sorted({n for _, n, _, _ in out.conversions if n in arrives})
        if rewritten:
            raise KernelError(
                f"{t.name}: operand {rewritten} is rewritten into another byte "
                f"order mid-kernel. The CALLER owns those bytes and its span was "
                f"sized for the order the operand arrived in, so a wider one runs "
                f"off the end of somebody else's array and this level cannot grow "
                f"it. Give the kernel a temp of its own and copy into that."
            )
        # A LEADING AXIS OF ONE is still a leading axis: unwrapped, a `(1, L, D)`
        # operand reaches a `(rows, k)` layout with three axes and cannot unpack.
        if _leads(self.signature, extents):
            # A port declared without `...` is SHARED by every batch element.
            follows = {p.name for p in self.signature.ports if p.batched}
            # A table is ONE copy every batch element reads, so it stays flat.
            follows |= set(out.temps) - set(out.tables)
            # Rounded to whatever granularity the project's passes WRITE at: a
            # stride below it and element `i` is overwritten by element `i+1`.
            grain = getattr(self._backend, "batch_grain", 1)
            room = {
                name: -(
                    -max(
                        (lay.nbytes(out.block(name)) for lay in out.orders(name)),
                        default=0,
                    )
                    // grain
                )
                * grain
                for name in follows
            }

            def wrap(name, inner):
                if name not in follows:
                    return inner
                return Batched(inner, out.batch, out.block(name), room[name])

            out.layouts = {n: wrap(n, lay) for n, lay in out.layouts.items()}
            out.conversions = [
                (at, n, wrap(n, before), wrap(n, after))
                for at, n, before, after in out.conversions
            ]
        # LAST, so a grid sized off the layouts is sized off the ones that will
        # be encoded -- the batch wrapper above is one of them.
        if regrid:
            _resize(self._backend, out)
        return out

    def plan(self, *args, **kwargs) -> Compiled:
        """What calling with these arguments WOULD compile to, without running.

        Same binding and solving as :meth:`__call__`, so the grid, the layouts
        and the stages it reports are the ones that would execute.
        """
        knobs = {k: kwargs.pop(k) for k in list(kwargs) if k in self.signature.knobs}
        bound = iface.bind(self.signature, args, kwargs)
        rt = _runtime_of(bound, self.name)
        return self.compile(
            rt.machine, iface.solve(self.signature, bound), _regrids(rt), **knobs
        )

    def footprint(self, *args, **kwargs) -> Footprint:
        """What calling with these arguments would need resident, without running.

        Same binding and solving as :meth:`__call__`, priced at the runtime's
        OWN allocator granularity and reuse setting -- which
        :meth:`Compiled.footprint` cannot know, since one compilation is cached
        across every runtime on that machine.

        Returns a :class:`~kohakuaccel.lifetime.Footprint`.
        """
        knobs = {k: kwargs.pop(k) for k in list(kwargs) if k in self.signature.knobs}
        bound = iface.bind(self.signature, args, kwargs)
        rt = _runtime_of(bound, self.name)
        compiled = self.compile(
            rt.machine, iface.solve(self.signature, bound), _regrids(rt), **knobs
        )
        return compiled.footprint(*_allocation(rt))

    def __call__(self, *args, **kwargs) -> Any:
        """Run this kernel on device arrays and return its results.

        Extents come from the arguments' shapes, the machine from the runtime
        they live on, and the results are allocated here. Keyword arguments
        matching a block parameter retune the tiling for this call only.
        """
        knobs = {k: kwargs.pop(k) for k in list(kwargs) if k in self.signature.knobs}
        bound = iface.bind(self.signature, args, kwargs)
        rt = _runtime_of(bound, self.name)
        extents = iface.solve(self.signature, bound)
        compiled = self.compile(rt.machine, extents, _regrids(rt), **knobs)
        align, reuse = _allocation(rt)
        _afford(rt, compiled, align, reuse)

        addrs: dict = {}
        for port in self.signature.inputs:
            addrs[port.name] = bound[port.name].address(compiled.layouts[port.name])

        sizes = compiled.sizes()
        results: list = []
        for port in self.signature.outputs:
            # A conversion rewrites a buffer where it lies, so the span fits the
            # WIDEST order and the bytes handed back are in the LAST one.
            order = compiled.final(port.name)
            value = rt.empty(compiled.shape_of(port.name), order, sizes[port.name])
            addrs[port.name] = value.address(order)
            results.append(value)

        for name, value in compiled.tables.items():
            addrs[name] = rt.constant(
                f"{compiled.name}:{compiled.tag}:{name}", value, compiled.layouts[name]
            )
        offsets, span = compiled.placement(align) if reuse else ({}, 0)
        # One span for every temp that shares, so nothing frees a piece of it.
        # SCRATCH: these are dead on return, so the next call reuses the bytes.
        block = rt.scratch(span) if span else None
        scratch: list = []
        for name in compiled.temps:
            if name in compiled.tables:
                continue
            if name in offsets:
                addrs[name] = block + offsets[name]
                continue
            order = compiled.final(name)
            held = rt.empty(compiled.shape(name), order, sizes[name])
            scratch.append(held)
            addrs[name] = held.address(order)
        consts = compiled.constants()
        for name, (array, layout) in consts.items():
            addrs[name] = rt.constant(
                f"{compiled.name}:{compiled.tag}:{name}", array, layout
            )

        arrived = [p.name for p in self.signature.inputs]
        guard = _guard(compiled, addrs, consts, [*arrived, *compiled.tables, *consts])
        # `block` is the runtime's SCRATCH and outlives this call deliberately:
        # the temps in it are dead on return and `empty_cache` returns the span.
        for at, stage in enumerate(compiled.stages):
            reads, writes = compiled.touches()[at]
            guard.check(f"{compiled.name} stage {at}", *reads)
            for _, name, before, after in compiled.before(stage.index):
                rt.convert(addrs[name], compiled.shape(name), before, after)
            guard.wrote(*writes)
            rt.dispatch(
                self._backend.encode(compiled, stage, addrs),
                stage.unit,
                f"{compiled.name}:{stage.index}",
                nodes=stage.nodes,
                acks=stage.acks,
            )
        return results[0] if len(results) == 1 else tuple(results)

    @property
    def name(self) -> str:
        return getattr(self._fn, "__name__", "kernel")

    def __repr__(self) -> str:
        ports = ", ".join(f"{p.name}: {p.role}" for p in self.signature.ports)
        return f"<kernel {self.name}({ports})>"


def _builder(backend, compiled, stage: Stage, body: list, outer: dict, names: list):
    """A function building one instance of `stage` from its grid coordinate.

    Kept on the stage so a grid the layouts turn out to have undersized can be
    materialised AGAIN from the body it was recorded as, rather than from the
    instances that body already produced.
    """

    def build(at: tuple) -> Instance:
        inner = at[1:] if stage.batched else at
        env = {**outer, **dict(zip(names, inner))}
        return Instance(at, _expand(backend, compiled, unroll(body, env)))

    return build


def _resize(backend, compiled) -> None:
    """Give every compiler-formed grid the extent its LAYOUT asks for.

    A grid the compiler formed took its extent from a buffer's logical element
    count, and the pass walks whatever byte order the buffer really ended up in
    -- so a padded one leaves the grid short by whole instances, and the work
    left over piles onto the instances there are rather than onto units that
    are idle. Only the extent moves: the same statements are rebuilt at more
    coordinates, so nothing decided earlier is invalidated.

    ASKED FOR, never assumed: more instances is more program images, and a
    caller who did not choose that gets the stream it always got. A project
    that offers no `regrid` keeps the grid it traced whatever the caller said.
    """
    ask = getattr(backend, "regrid", None)
    if ask is None:
        return
    for made in compiled.stages:
        if made.build is None:
            continue
        want = ask(compiled, made)
        if want is None or tuple(want) == made.body_grid:
            continue
        made.body_grid = tuple(want)
        made.grid = (compiled.batch, *want) if made.batched else tuple(want)
        made.instances = [made.build(at) for at in _points(made.grid)]


def _leads(signature: iface.Signature, extents: dict) -> bool:
    """Whether a per-element `...` absorbed any axes at all.

    NOT `batch > 1`: one batch element is still one leading axis, and a shape
    carrying it needs the same wrapper a longer batch does. A whole-array `...`
    absorbs axes too and is NOT one of these -- it is one contiguous block.
    """
    per = any(p.batched and p.trailing for p in signature.ports)
    return per and bool(extents.get(iface.LEAD, ()))


def _allocation(rt) -> tuple:
    """`rt`'s allocator granularity and whether it reuses temp spans."""
    arena = getattr(rt, "arena", None)
    return getattr(arena, "align", 1), bool(getattr(rt, "reuse_temps", False))


def _regrids(rt) -> bool:
    """Whether `rt` asks for grids sized to the work rather than to the trace."""
    return bool(getattr(rt, "regrid", False))


def _afford(rt, compiled, align: int, reuse: bool) -> None:
    """Refuse a call no arena of this size could ever hold.

    Against the arena's SIZE and not its free bytes: what is resident now is
    somebody's to release, and refusing a call a `del` would have made room for
    is worse than the message it saves. So this fires only where every ordering
    fails, and it names the mesh and every buffer -- which is the diagnosis a
    bare allocation failure cannot give.

    Raises :class:`~kohakuaccel.memory.OutOfMemory`.
    """
    arena = getattr(rt, "arena", None)
    if arena is None or compiled.demand(align) <= arena.size:
        return
    got = compiled.footprint(align, reuse=reuse)
    if got.peak <= arena.size:
        return
    raise OutOfMemory(
        f"{compiled.name} needs {got.peak:,} bytes at once and this arena holds "
        f"{arena.size:,}, so no allocation order fits it:\n{got.report()}"
    )


def _guard(compiled, addrs: dict, consts: dict, arrived) -> Writes:
    """A read guard over where this call's buffers REALLY landed.

    Built from the addresses handed out, not from the placement that asked for
    them, so it also sees an overlap nobody intended -- a table on a temp, an
    operand the allocator handed out twice. `arrived` names the buffers that
    come with their contents already in them.

    Returns a :class:`~kohakuaccel.lifetime.Writes`. Two buffers on one span
    that nothing writes are left alone: a caller passing one tensor as two
    operands is ordinary, and only a WRITE can make a read wrong.
    """
    sizes = compiled.sizes()
    for name, (array, layout) in consts.items():
        sizes[name] = layout.nbytes(getattr(array, "shape", ()))
    spans = [Span(n, addrs[n], sizes[n]) for n in addrs if n in sizes]
    return Writes(aliases(spans), arrived)


def _up(v: int, q: int) -> int:
    return -(-v // q) * q if q > 1 else v


def _runtime_of(bound: dict, name: str):
    """The runtime every operand lives on. Raises if they disagree."""
    seen: dict = {}
    for key, value in bound.items():
        rt = getattr(value, "runtime", None)
        if rt is None:
            raise KernelError(
                f"{name}: operand {key!r} is not on a device; put it there with "
                f"dev.tensor(...) first"
            )
        seen.setdefault(id(rt), (rt, key))
    if len(seen) > 1:
        which = ", ".join(f"{k} on {r}" for r, k in seen.values())
        raise KernelError(f"{name}: operands are on different devices ({which})")
    return next(iter(seen.values()))[0]


def _sequence(top: list, extents: dict, backend=None):
    """Every stage to run, in order, with the loop indices around it bound.

    Statements written without a stage are BANDED: neighbours that need the same
    grid become one stage, and a boundary falls where the grid changes or where
    one would read what another wrote without the unit sequencing them.
    """
    free: list = []
    shape: tuple | None = None

    def flush():
        nonlocal free, shape
        if free:
            yield Grid(
                shape,
                tuple(Index(n) for n in free[0].names),
                list(free),
                formed=True,
            ), extents
        free, shape = [], None

    for item in top:
        if isinstance(item, Stmt):
            want = tuple(value_of(a, extents) for a in item.grid)
            same = (
                free
                and want == shape
                and item.names == free[0].names
                and backend_band(backend, item) == backend_band(backend, free[-1])
                and joins(backend, free, item, extents)
            )
            if free and not same:
                yield from flush()
            shape = want
            free.append(item)
            continue
        yield from flush()
        if isinstance(item, Loop):
            for value in range(value_of(item.count, extents)):
                inner = {**extents, item.var.name: value}
                yield from _sequence(item.body, inner, backend)
        else:
            yield item, extents
    yield from flush()


def _by_unit(backend, made: Stage) -> list:
    """`made` as one stage per RUN of statements on the same kind of unit."""
    if not made.instances:
        made.unit = backend.unit_of(made.kinds)
        return [made]
    runs: list = []
    for at, stmt in enumerate(made.instances[0].stmts):
        unit = backend.unit_of({stmt.kind})
        if runs and runs[-1][0] == unit:
            runs[-1][1].append(at)
        else:
            runs.append((unit, [at]))
    if len(runs) == 1:
        made.unit = runs[0][0]
        return [made]
    out = []
    for unit, positions in runs:
        part = replace(made, unit=unit, wanted_barrier=True, instances=[])
        part.instances = [
            Instance(i.at, [i.stmts[p] for p in positions]) for i in made.instances
        ]
        out.append(part)
    return out


def _band(stages: list) -> list:
    """Merge neighbouring grids that asked for no barrier and agree on a split.

    A grid opened with `stage` keeps its boundary; one opened with `grid` gives
    it up when the neighbour matches.
    """
    out: list = []
    for made in stages:
        last = out[-1] if out else None
        joins = (
            last is not None
            and not made.wanted_barrier
            and not last.wanted_barrier
            and last.grid == made.grid
            and last.unit == made.unit
        )
        if joins:
            for held, fresh in zip(last.instances, made.instances):
                held.stmts += fresh.stmts
            continue
        out.append(made)
    return out


def check_hazards(name: str, stage, backend=None, compiled=None) -> None:
    """Refuse a stage whose statements RACE each other.

    A unit runs a stage's statements as separate programs and does not wait
    between them, so a read-after-write inside one reads stale bytes: 79% error,
    measured. The project may say two of its own statements are ONE program, in
    which case the second reads a register. Raises :class:`KernelError` naming
    both statements.
    """
    if not stage.instances:
        return
    written: dict = {}
    for at, stmt in enumerate(stage.instances[0].stmts):
        clash = [
            r
            for r in stmt.reads
            if r in written
            and not apart(backend, compiled, stage, written[r][1], stmt)
            and not sequenced(backend, written[r][1], stmt)
        ]
        if clash:
            raise KernelError(
                f"{name} stage {stage.index}: statement {at} reads {clash[0]!r}, "
                f"which statement {written[clash[0]][0]} in the same stage wrote. "
                f"A unit does not wait between the programs of one stage, so "
                f"this reads stale bytes. Put them in separate stages."
            )
        if stmt.writes is not None:
            written[stmt.writes] = (at, stmt)


def apart(backend, compiled, stage, before: Stmt, after: Stmt) -> bool:
    """Whether the project proves these two touch no common byte.

    A buffer name is not a region. Only asked once the layouts are known, since
    a region needs its extent; a project that does not answer keeps the name
    test, which is what this guard did before regions existed.
    """
    ask = getattr(backend, "apart", None)
    if ask is None or compiled is None:
        return False
    return bool(ask(compiled, stage, before, after))


def sequenced(backend, before: Stmt, after: Stmt) -> bool:
    """Whether the project runs these two as ONE program, in order.

    A project that does not answer has every dependency treated as a race,
    which is what this guard assumed before any unit could sequence one.
    """
    ask = getattr(backend, "sequences", None)
    return bool(ask and ask(before, after))


def joins(backend, held: list, stmt: Stmt, extents: dict | None = None) -> bool:
    """Whether `stmt` may share a compiler-formed stage with `held`.

    Independent statements always may. A read-after-write may only when the
    project says ONE program holds them all -- a stage the compiler formed and
    the emitter then cannot run is a refusal the author never asked for.
    `extents` are this compilation's dims, which a project needs to answer for
    a statement whose shape is still symbolic.
    """
    if not any(s.writes is not None and s.writes in stmt.reads for s in held):
        return True
    ask = getattr(backend, "may_join", None)
    return bool(ask and ask(held, stmt, extents or {}))


def backend_band(backend, stmt: Stmt) -> str:
    """What may share a stage with this statement.

    The project's answer, since which KINDS one of its units runs as a single
    program is its own business; the statement's kind where it does not answer.
    """
    ask = getattr(backend, "band", None)
    return ask(stmt) if ask else stmt.kind


def _expand(backend, compiled, stmts: list) -> list:
    """Let the project turn a whole-buffer statement into placed ones.

    `expand` sees ONE statement, so redundancy between two of them is invisible
    to it; `prune` then sees the whole instance and may drop what is already
    resident. Both are optional.
    """
    expand = getattr(backend, "expand", None)
    if expand is None:
        return stmts
    out: list = []
    for stmt in stmts:
        out += expand(compiled, stmt)
    prune = getattr(backend, "prune", None)
    return prune(compiled, out) if prune else out


def _points(grid: tuple):
    """Every grid coordinate, last axis fastest."""
    out = [()]
    for extent in grid:
        out = [p + (i,) for p in out for i in range(extent)]
    return out


def kernel(fn=None, *, backend: Backend):
    """Decorator: record this function's structure once, call it per shape.

    A project binds its own backend, so its users write a plain `@kernel`.
    """

    def wrap(f) -> Kernel:
        return Kernel(f, backend)

    return wrap if fn is None else wrap(fn)
