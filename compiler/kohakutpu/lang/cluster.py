"""What a matrix cluster's statements mean: fill, sweep, drain.

`<<=` into an L1 region is a FILL, `@` between two regions accumulating into a
tile is a GEMM, and `<<=` into a result slice is a DRAIN.

A region counts entries of `lanes x kblock` and a tile counts `lanes x lanes`
sub-tiles. Which L1 side a region occupies is decided by where it stands in
`ra @ rb`, not by which tensor filled it.
"""

from dataclasses import dataclass
from typing import Self

from kohakuaccel.lang import active
from kohakutpu.lang.errors import LangError
from kohakutpu.lang.vector import (
    TILES,
    Fold,
    Node,
    Ref,
    Resident,
    Value,
    chain_of,
    leaves_of,
)

from kohakutpu import layout as LO

#: Statement kinds this unit runs.
KINDS = {"fill", "gemm", "drain"}


def step_of(trace) -> object:
    """Where in an accumulation this statement stands: the innermost index.

    The L1 bank alternates per SWEEP STEP, not per operand chunk, and the two
    part company as soon as a chunk index is anything but the loop counter.
    """
    at = trace.innermost()
    return at if at is not None else 0


@dataclass
class Slice:
    """Two indices of a buffer: one FILL's worth, or one instance's corner.

    A third index offsets the fill by that many LANES of the entry stream -- 64
    bytes, a quarter of an entry. Nothing but a convolution's taps needs it, and
    zero is the address every other kernel has always had.
    """

    buffer: object
    i: object
    j: object
    lane: object = 0

    def __matmul__(self, other: "Slice") -> "Sweep":
        """`a[i, k] @ b[j, k]` -- a sweep over two GLOBAL tiles.

        Raises :class:`LangError` unless both sides are operand slices.
        """
        if not isinstance(other, Slice):
            raise LangError("a sweep multiplies two operand slices")
        return Sweep(self, other)

    def __ilshift__(self, acc) -> Self:
        """`c[i, j] <<= acc` to memory, or `<<= f(acc)` over the NoC.

        An expression ON the accumulator drains into a vector core's L1 instead
        and runs the epilogue there, so the tile never returns through MAG. Both are
        one `DRAIN`; only its destination fields differ.
        """
        if isinstance(acc, Value):
            return self._fused(acc)
        if not isinstance(acc, Tile):
            raise LangError("a result slice is drained from an accumulator tile")
        active().emit(
            "drain",
            writes=self.buffer.name,
            result=self.buffer.name,
            i=self.i,
            j=self.j,
            gm=acc.gm,
            gn=acc.gn,
        )
        return self

    def _staged(self, value: Value) -> Self:
        """The tile LANDS, then the epilogue reads it back as an ordinary pass.

        What `_fused` does when the machine cannot carry the tile over the NoC.
        The accumulator's leaf is rewritten to the landed buffer, so the chain
        is the author's unchanged and only its operand moved.
        """
        from kohakutpu.lang.buffers import ELEM, PART
        from kohakutpu.lang.vector import Part

        every = leaves_of(value.expr)
        held = [le for le in every if isinstance(le, Resident)]
        if len(held) != 1:
            raise LangError(
                f"this epilogue reads {len(held)} accumulators; one drain hands "
                f"over one tile, so lift the rest into their own statement"
            )
        tile = TILES.get(held[0].at)
        if tile is None:
            raise LangError("this epilogue reads an accumulator that cannot land")
        landed = tile.landed()
        per = active().knobs.get("part", PART)
        grid, names = (landed.parts(per),), (ELEM.name,)
        tiled = [le for le in every if isinstance(le, Ref) and le.part is not ELEM]
        if tiled:
            # Reseating it to a period-N spread gives rel err 1.0, and pinning
            # the pass to row order as well gives 1.15. Measured, twice.
            raise LangError(
                f"this epilogue could not fuse, so it runs AFTER the tiling, "
                f"where {tiled[0].name!r}'s tile index does not exist. Raise the "
                f"tiling until the grid fits the vector cores, or add the "
                f"per-channel operand in its own pass"
            )
        fresh = _reseat(value.expr, held[0], Ref(landed.name, ELEM))
        Part(self.buffer, ELEM, grid=grid, names=names, top=True).write(
            Value(fresh, value.shape_rc)
        )
        return self

    def _fused(self, value: Value) -> Self:
        """The drain and the epilogue it feeds, as two statements in one grid.

        With `fuse=False` the tile LANDS and the epilogue reads it back: the
        same arithmetic, one buffer and one extra pass. `compile` turns the knob
        when the machine refuses the transfer, so the author writes one kernel.
        """
        if not active().knobs.get("fuse", True):
            return self._staged(value)
        leaves = leaves_of(value.expr)
        held = [le for le in leaves if isinstance(le, Resident)]
        if not held:
            raise LangError(
                f"{self.buffer.name!r} is written by tile, but this expression "
                f"reads no accumulator -- something in it landed the tile, so "
                f"the work is now a vector pass over rows. Give it its own "
                f"index space over the landed buffer and write a part of it"
            )
        if len(held) != 1:
            raise LangError(
                f"this epilogue reads {len(held)} accumulators; one drain hands "
                f"over one tile, so lift the rest into their own statement"
            )
        acc = held[0]
        trace = active()
        trace.emit(
            "drain",
            writes=self.buffer.name,
            result=self.buffer.name,
            i=self.i,
            j=self.j,
            gm=acc.gm,
            gn=acc.gn,
            node=True,
        )
        trace.emit(
            "apply",
            writes=self.buffer.name,
            reads=tuple(le.name for le in leaves if isinstance(le, Ref)),
            result=self.buffer.name,
            resident=acc,
            i=self.i,
            j=self.j,
            gm=acc.gm,
            gn=acc.gn,
            part=0,
            off=0,
            chain=tuple(chain_of(value.expr, leaves)),
            leaves=tuple(leaves),
        )
        return self


class Region:
    """An L1 region, sized in entries. `<<=` streams a buffer slice into it."""

    def __init__(self, groups: int, blocks: int) -> None:
        self.groups, self.blocks = groups, blocks
        self.sel: int | None = None
        self.fills: list = []

    @property
    def entries(self) -> int:
        return self.groups * self.blocks

    def __ilshift__(self, src: Slice) -> Self:
        if not isinstance(src, Slice):
            raise LangError("an L1 region is filled from a tile-and-chunk slice")
        trace = active()
        self.fills.append(
            trace.emit(
                "fill",
                reads=(src.buffer.name,),
                operand=src.buffer.name,
                sel=self.sel,
                tile=src.i,
                chunk=src.j,
                lane=src.lane,
                order=getattr(src.buffer, "order", None),
                step=step_of(trace),
                groups=self.groups,
                blocks=self.blocks,
                chunks=src.buffer.chunks32(self.blocks),
            )
        )
        return self

    def side(self, sel: int) -> None:
        """Fix which L1 side this region occupies, and stamp its fills."""
        if self.sel is not None and self.sel != sel:
            raise LangError(
                f"this region is read as L1 {'AB'[self.sel]} and as {'AB'[sel]}; "
                f"one region occupies one side"
            )
        self.sel = sel
        for stmt in self.fills:
            stmt.args["sel"] = sel

    def __matmul__(self, other: "Region") -> "Product":
        return Product(self, other)


class Product:
    """`ra @ rb`, which is also what assigns the two regions their L1 sides."""

    def __init__(self, a: Region, b: Region) -> None:
        if not isinstance(b, Region):
            raise LangError("a product multiplies two L1 regions")
        if a.blocks != b.blocks:
            raise LangError(
                f"the left region holds {a.blocks} K-blocks and the right holds "
                f"{b.blocks}; a sweep contracts over the same K"
            )
        a.side(0)
        b.side(1)
        self.a, self.b = a, b


@dataclass
class Sweep:
    """`a[i, k] @ b[j, k]` before anywhere to put it."""

    a: Slice
    b: Slice


class Tile:
    """The accumulator's resident tile, in sub-tiles. `+=` sweeps into it.

    `nk` is how many K-blocks one sweep contracts.
    """

    def __init__(self, gm: int, gn: int, nk: int | None = None) -> None:
        self.gm, self.gn = gm, gn
        self.nk = active().knobs.get("nk", 2) if nk is None else nk
        self.at = active().fresh("acc")
        #: Loops already open when this tile was made. A sweep is chained by the
        #: INNERMOST counter, so only loops opened after this one can carry it.
        self.born = len(active().loops)
        self._landed = None
        TILES[self.at] = self

    @property
    def expr(self) -> Resident:
        """This tile as an expression leaf, for `sigmoid(acc) * acc`."""
        return Resident(self.gm, self.gn, self.at)

    def landed(self):
        """This tile as a BUFFER, draining it on first ask.

        For the operations sub-tile order cannot serve -- a row reduction, and
        being a sweep's operand. Emitted once however many read it; the layout
        pass then inserts the sub-tile to row order conversion, which is the
        one link `scratch/sdxl-fwd/planned_btb.py` also pays as `sc_link`.
        """
        if self._landed is not None:
            return self._landed
        from kohakutpu.lang.buffers import Buffer

        trace = active()
        grid = trace.enclosing()
        if grid is None or len(grid.axes) != 2:
            raise LangError(
                "an accumulator lands over the tiling that produced it, so it "
                "needs a two-dimensional `with units(...)` around it"
            )
        # Sized over the WHOLE tiling: one buffer, one tile per instance. Sized
        # to one tile, every instance would drain onto the same addresses.
        rows, cols = self.gm * LO.LANES, self.gn * LO.LANES
        made = Buffer(grid.axes[0] * rows, grid.axes[1] * cols)
        made.name = trace.declare("land", (grid.axes[0] * rows, grid.axes[1] * cols))
        trace.emit(
            "drain",
            writes=made.name,
            result=made.name,
            i=grid.index[0],
            j=grid.index[1],
            gm=self.gm,
            gn=self.gn,
        )
        self._landed = made
        return made

    def _op(self, kind, other=None, flip: bool = False) -> Value:
        """Arithmetic on the accumulator: an EPILOGUE, not a sweep.

        Returns a vector expression whose leaf is this tile.
        """
        return Value(self.expr)._op(kind, other, flip)

    __mul__ = Value.__mul__
    __rmul__ = Value.__rmul__
    __add__ = Value.__add__
    __radd__ = Value.__radd__
    __sub__ = Value.__sub__
    __rsub__ = Value.__rsub__
    __truediv__ = Value.__truediv__
    __rtruediv__ = Value.__rtruediv__
    __neg__ = Value.__neg__

    def __matmul__(self, other) -> "Sweep":
        """Always raises :class:`LangError`: an accumulator is not an operand.

        `acc += sw @ v[j]` reads as "multiply the tile I just computed", and a
        GEMM contracts two L1 REGIONS -- the accumulator is neither, and there
        is no instruction that feeds it back in without a drain.
        """
        raise LangError(
            "an accumulator tile cannot be multiplied: a sweep contracts two L1 "
            "regions, and a resident tile is neither. Drain it to a temp and "
            "fill from there, or send it to a vector core with `y[i, j] <<= f(acc)`"
        )

    def __iadd__(self, product) -> Self:
        self._chainable()
        if isinstance(product, Sweep):
            return self._sweep(product)
        return self._explicit(product)

    def _chainable(self) -> None:
        """Refuse a sweep the GEMM's chain flag cannot express.

        The flag is the innermost loop counter, and zero CLEARS the tile, so a
        sweep two loops deep restarts the accumulation on every outer iteration
        and silently keeps only the last. Raises :class:`LangError` naming the
        nest. Measured: a doubly-nested sweep returned 1.001x one pass, not 3x.
        """
        nest = len(active().loops) - self.born
        if nest > 1:
            raise LangError(
                f"this accumulator is swept inside {nest} nested loops, and a "
                f"GEMM chains on the INNERMOST counter -- which restarts at 0 "
                f"every outer iteration, and 0 clears the tile. Every pass but "
                f"the last would be discarded, and nothing would report it. "
                f"Use one loop over the whole sweep, or move the accumulator "
                f"inside the outer loop and combine the results yourself"
            )

    def _sweep(self, sweep: Sweep) -> Self:
        """Fill both windows and contract them, sized from this accumulator."""
        trace = active()
        nk = self.nk
        for src, sel, groups in ((sweep.a, 0, self.gm), (sweep.b, 1, self.gn)):
            trace.emit(
                "fill",
                reads=(src.buffer.name,),
                operand=src.buffer.name,
                sel=sel,
                tile=src.i,
                chunk=src.j,
                lane=src.lane,
                order=getattr(src.buffer, "order", None),
                step=step_of(trace),
                groups=groups,
                blocks=nk,
                chunks=src.buffer.chunks32(nk),
            )
        after = trace.innermost()
        trace.emit(
            "gemm",
            gm=self.gm,
            gn=self.gn,
            nk=nk,
            acc=after if after is not None else 0,
        )
        return self

    def _explicit(self, product: Product) -> Self:
        """Sweep `product` into this tile, chaining after the first iteration.

        `acc` names the enclosing loop index, which is only a number once the
        loop unrolls. Raises :class:`LangError` for anything but a product.
        """
        if not isinstance(product, Product):
            raise LangError("an accumulator takes a product of two L1 regions")
        trace = active()
        after = trace.innermost()
        trace.emit(
            "gemm",
            gm=self.gm,
            gn=self.gn,
            nk=product.a.blocks,
            acc=after if after is not None else 0,
        )
        return self


def _reseat(expr, leaf, fresh):
    """`expr` with `leaf` replaced by `fresh`, everything else untouched.

    The author's chain is kept exactly; only where its accumulator comes from
    changes, so a staged epilogue computes what the fused one would have.
    """
    if expr == leaf:
        return fresh
    if isinstance(expr, Node):
        args = tuple(_reseat(a, leaf, fresh) for a in expr.args)
        return type(expr)(expr.kind, args, *_extra(expr))
    return expr


def _extra(node) -> tuple:
    """A `Fold`'s row shape, which its constructor takes after the args."""
    return (node.rows, node.cols) if isinstance(node, Fold) else ()


def region(groups: int, blocks: int) -> Region:
    """An L1 region of `groups x blocks` entries."""
    return Region(groups, blocks)


def tile(gm: int, gn: int, nk: int | None = None) -> Tile:
    """An accumulator tile of `gm x gn` sub-tiles.

    `nk` is K-blocks per sweep; omitted, it is this kernel's `nk=` knob.
    """
    return Tile(gm, gn, nk)
