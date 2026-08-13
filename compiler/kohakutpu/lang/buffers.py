"""What a kernel reads and writes: ports, and the temps between stages.

Indexing picks the vocabulary. TWO indices are a cluster's view -- a tile and a
K-chunk of an operand, or an instance's corner of a result. ONE index is a
vector core's view: a part of the elements, in whatever order they are in.
"""

from typing import Any, Self

from kohakuaccel.lang import Index, Name, PortSpec, active, ceildiv, iface
from kohakutpu.lang.cluster import Slice
from kohakutpu.lang.errors import LangError
from kohakutpu.lang.vector import Part

from kohakutpu import layout as LO

LANES = LO.LANES
KBLOCK = LO.KBLOCK

#: Elements one vector instance handles when nobody said otherwise. One RUN
#: stores a whole batch, so a part is whole batches or the tail is rewritten.
PART = 8192
#: The index a whole-buffer elementwise write is instanced over.
ELEM = Index("_e")


def _positive(per: int, call: str, name: str) -> int:
    """`per` if it is at least one, else a refusal naming the knob.

    Without this a zero tiling knob reached `ceildiv` and the author got
    `ZeroDivisionError` from inside the compiler, which names nothing.
    """
    if not isinstance(per, int) or per < 1:
        raise LangError(
            f"{name or 'a buffer'}.{call}({per!r}): a tiling knob is how many "
            f"units of work ONE instance takes, so it has to be a positive "
            f"integer. Zero would mean an instance does nothing and the grid is "
            f"infinite"
        )
    return per


class Buffer:
    """Something a kernel reads or writes: a port, or a stage-to-stage temp."""

    #: Set by the compiler for a port, by :func:`temp` for an internal buffer.
    name = ""

    def __init__(self, *shape) -> None:
        self.port_shape = tuple(shape)

    @property
    def trailing(self) -> tuple:
        """The axes this kernel tiles. A leading `...` is not one of them."""
        if self.port_shape[:1] == (Ellipsis,):
            return self.port_shape[1:]
        return self.port_shape

    @property
    def rows(self):
        return self.trailing[0]

    @property
    def cols(self):
        return self.trailing[-1]

    @property
    def groups(self):
        """Lane groups, which is what a tile is counted in."""
        return ceildiv(self.rows, LANES)

    @property
    def kblocks(self):
        """K-blocks, which is what a chunk is counted in."""
        return ceildiv(self.cols, KBLOCK)

    @property
    def elements(self):
        """Elements in ONE batch element.

        A whole-array `...` has no trailing axes, so the count stays symbolic
        until a shape arrives.
        """
        trailing = self.trailing
        if not trailing:
            return Name(iface.FLAT)
        n = trailing[0]
        for axis in trailing[1:]:
            n = n * axis
        return n

    def tiles(self, per: int):
        """Grid extent when each instance owns `per` lane groups.

        Raises :class:`LangError` for `per` below one.
        """
        return ceildiv(self.groups, _positive(per, "tiles", self.name))

    def chunks32(self, per: int):
        """Sweep length when each pass contracts `per` K-blocks of 32.

        The 32 is in the NAME because it is the only granule the ISA has -- a
        GEMM's `nk` counts K-blocks, not elements -- and `chunks(1)` read as
        "chunks of one element" to everyone who met it. Raises
        :class:`LangError` for `per` below one.
        """
        return ceildiv(self.kblocks, _positive(per, "chunks32", self.name))

    def parts(self, per: int):
        """Grid extent when each instance handles `per` elements.

        Raises :class:`LangError` for `per` below one.
        """
        return ceildiv(self.elements, _positive(per, "parts", self.name))

    def rows_from(self, start) -> "View":
        """This buffer seen from row `start` on."""
        return View(self, start * self.cols)

    def as_rows(self, width: int) -> "Reshaped":
        """This buffer walked as rows of `width`. Same bytes, different fold."""
        return Reshaped(self, width)

    def repeated(self) -> "Spread":
        """This whole buffer read over and over, against a longer operand.

        A per-channel bias is `N` values against an `M*N` result. Stride 0 in
        the AGU: one descriptor dimension and no pass. Materialising the
        broadcast instead costs `M` times the DRAM and the bandwidth to match.

        The reader's `part` must be a whole multiple of these elements, or an
        instance starts part way through and every later repeat is misaligned.
        """
        return Spread(self, self.elements, self.elements)

    def per_group(self, rows):
        """This buffer seen as row 0 of every `rows`-row group, repeated over it.

        The read side of a group reduction, and the one thing a shifted pass
        cannot express: the value at `i` depends on `i mod rows`. At `rows == 1`
        it is the buffer itself, so a kernel that spreads nothing emits nothing.
        """
        if rows == 1:
            return self
        return Spread(self, rows * self.cols, self.cols)

    def __getitem__(self, idx) -> Any:
        """Two indices are a cluster's slice; one is a vector core's part.

        A 1-D buffer indexed by a TILE is per-channel -- the same values again
        for every row -- so it carries its period from here. That is what lets
        a staged epilogue rewrite it as a spread once the tile index is gone.
        """
        if isinstance(idx, tuple):
            return Slice(self, *idx)
        if idx is not ELEM and len(self.trailing) == 1:
            return Part(self, idx, period=self.elements, take=self.elements)
        return Part(self, idx)

    def __setitem__(self, idx, value) -> None:
        """Absorb the store half of `y[...] <<= ...`.

        Python rewrites an augmented store as get, operate, set; the operation
        already emitted the statement. Raises :class:`LangError` otherwise.
        """
        if not isinstance(value, (Slice, Part)):
            raise LangError(f"{self.name!r} is written with `<<=`")

    def whole(self) -> Part:
        """This buffer as one elementwise value, instanced over its elements.

        The part size is this kernel's `part=` if it declared one.
        """
        per = active().knobs.get("part", PART)
        return Part(self, ELEM, grid=(self.parts(per),), names=(ELEM.name,))

    def _op(self, kind, other=None, flip: bool = False):
        """Arithmetic on a bare buffer means arithmetic on all of it."""
        return self.whole()._op(kind, other, flip)

    __mul__ = Part.__mul__
    __rmul__ = Part.__rmul__
    __add__ = Part.__add__
    __radd__ = Part.__radd__
    __sub__ = Part.__sub__
    __rsub__ = Part.__rsub__
    __truediv__ = Part.__truediv__
    __rtruediv__ = Part.__rtruediv__
    __neg__ = Part.__neg__

    def __matmul__(self, other: "Buffer"):
        """Always raises :class:`LangError`: this is the DSL's upper bound.

        A contraction between whole buffers states no tiling, so it says no
        more than the tensor level does.
        """
        raise LangError(
            f"{self.name!r} @ {getattr(other, 'name', other)!r} states no tiling, "
            f"so it says no more than the tensor level does. Write it as "
            f"`acc += a[i, k] @ b[j, k]` inside a `grid(...)`, or call "
            f"`ktpu.matmul` and stay one level up."
        )

    def __ilshift__(self, value) -> Self:
        """`y <<= ...` over every element, for work with no tile shape to pick."""
        self.whole().write(value)
        return self

    def __repr__(self) -> str:
        return f"{type(self).__name__}{self.port_shape} {self.name!r}"


class Reshaped(Buffer):
    """The same bytes, walked as rows of `width`.

    Row-major `(M, N)` and `(M*N/W, W)` are ONE linear memory, so this changes
    only what a row reduction folds over -- `VRED` takes `cols` lanes. It is
    what `kernels/wide.py` makes the caller do by hand before the call.
    """

    def __init__(self, buffer: Buffer, width: int) -> None:
        lead = buffer.port_shape[:1] if buffer.port_shape[:1] == (Ellipsis,) else ()
        super().__init__(*lead, ceildiv(buffer.elements, width), width)
        self.buffer = buffer

    @property
    def name(self) -> str:
        return self.buffer.name


class View:
    """A buffer offset by a whole number of elements. Indexes like the buffer."""

    def __init__(self, buffer: Buffer, off) -> None:
        self.buffer, self.off = buffer, off

    @property
    def name(self) -> str:
        return self.buffer.name

    def parts(self, per: int):
        return self.buffer.parts(per)

    def __getitem__(self, idx) -> Any:
        if isinstance(idx, tuple):
            raise LangError(
                f"{self.name!r} is a row-offset view, which a cluster cannot "
                f"address; a fill already takes its tile index directly"
            )
        return Part(self.buffer, idx, self.off)

    def __setitem__(self, idx, value) -> None:
        if not isinstance(value, Part):
            raise LangError(f"{self.name!r} is written with `<<=`")


class Spread:
    """A buffer read periodically: `take` elements of every `period`, repeated.

    An ADDRESS-DEPENDENT operand, and the only one this DSL has. `vec_agu`
    calls it a broadcast and spells it stride 0, so it costs a descriptor
    dimension and no pass at all -- `.plan/measurements/wide-rows.md` §2.
    """

    def __init__(self, buffer: Buffer, period, take) -> None:
        self.buffer, self.period, self.take = buffer, period, take

    @property
    def name(self) -> str:
        return self.buffer.name

    def parts(self, per: int):
        return self.buffer.parts(per)

    def whole(self) -> Part:
        """This view as one elementwise value, for arithmetic on a bare name."""
        return self[ELEM]

    def __getitem__(self, idx) -> Any:
        """One index only: a period is a vector core's view of its own operand."""
        if isinstance(idx, tuple):
            raise LangError(
                f"{self.name!r} is a periodic view, which a cluster cannot "
                f"address; a fill reaches a tile by index, not by period"
            )
        return Part(self.buffer, idx, 0, period=self.period, take=self.take)

    def __setitem__(self, idx, value) -> None:
        """Always raises :class:`LangError`: a spread is read, never written.

        Writing one would have every group's rows land on the same addresses,
        so the last row written would win and the rest would vanish.
        """
        raise LangError(
            f"{self.name!r} is a periodic READ; writing it would land every row "
            f"of a group on one address and keep only the last. Write the "
            f"compact buffer and spread it where it is read"
        )


class Tap:
    """The lane offset of sweep step `s`, resolved when the loop unrolls.

    `s` runs over (tap, channel block) together, because the accumulator chains
    on ONE loop counter and the L1 bank alternates with it. Splitting it into
    two loops restarts both. `//` and `%` are not index arithmetic, so this
    rebinds itself -- the hook `record._value` provides for exactly this.
    """

    def __init__(self, step, blocks, plane, wp) -> None:
        self.step, self.blocks, self.plane, self.wp = step, blocks, plane, wp

    def rebind(self, value):
        step, blocks = int(value(self.step)), int(value(self.blocks))
        tap, block = divmod(step, blocks)
        dy, dx = divmod(tap, 3)
        return block * int(value(self.plane)) + dy * int(value(self.wp)) + dx


class Plane:
    """A buffer read as a padded spatial plane, `[C/32][plane][32]`.

    `x[i, k, off]` fills tile `i` at `off` LANES -- one pixel's 32-channel
    block, 64 bytes. A 3x3 tap IS that offset, which is the whole of branch C
    (`.plan/CONV2D.md` §4): nine taps, nine constants, one accumulator.

    Padding is fixed at 1 because the layout's tail is sized for a 3x3.
    """

    #: `H+2` and `W+2`, since a 3x3 with pad 1 is what this layout is shaped for.
    PAD = 1

    def __init__(self, buffer: Buffer, gm: int) -> None:
        self.buffer = buffer
        self.order = LO.ConvEntry(self.PAD, gm)

    @property
    def name(self) -> str:
        return self.buffer.name

    @property
    def wp(self):
        return self.buffer.trailing[1] + 2 * self.PAD

    @property
    def plane(self):
        """Positions one channel block occupies, rounded to whole entries."""
        rows = (self.buffer.trailing[0] + 2 * self.PAD) * self.wp
        return ceildiv(rows, LANES) * LANES

    @property
    def blocks(self):
        return ceildiv(self.buffer.trailing[2], KBLOCK)

    @property
    def groups(self):
        """Lane groups worth sweeping: past `(H-1)*wp + W` every output is halo."""
        h, w = self.buffer.trailing[0], self.buffer.trailing[1]
        return ceildiv((h - 1) * self.wp + w, LANES)

    def tiles(self, per: int):
        """Grid extent when each instance owns `per` lane groups of the plane."""
        return ceildiv(self.groups, _positive(per, "tiles", self.name))

    def chunks32(self, per: int) -> int:
        """One. A K-chunk is reached by OFFSET here, not by position in the run.

        The channel block is the outer axis, so entry `(block, tile)` is at
        `block*plane/4 + tile*gm` -- the tile stride is one fill's span and the
        block rides in :class:`Tap`. Anything but 1 strides the tile axis by the
        chunk count and reads the wrong pixels.
        """
        return 1

    def tap(self, step) -> Tap:
        return Tap(step, self.blocks, self.plane, self.wp)

    def __getitem__(self, idx) -> Slice:
        if not isinstance(idx, tuple) or len(idx) != 3:
            raise LangError(
                f"{self.name!r} is a plane; a fill of one names a tile, a chunk "
                f"and a tap offset, as `x[i, 0, x.tap(s)]`"
            )
        return Slice(self, *idx)


def plane(buffer: Buffer, gm: int) -> Plane:
    """`buffer` read as a `[H][W][C]` activation in the convolution layout."""
    return Plane(buffer, gm)


class In(PortSpec, Buffer):
    """A tensor the kernel reads. Written as a parameter default, `a=In(M, K)`."""

    port_role = "in"


class Out(PortSpec, Buffer):
    """A tensor the kernel writes, allocated by the compiler and returned."""

    port_role = "out"


def temp(*shape) -> Buffer:
    """A buffer handed from one stage to the next, placed by the compiler.

    One way across a unit boundary; a fused epilogue is the other. Nothing
    outside the kernel sees it.
    """
    made = Buffer(*shape)
    made.name = active().declare("t", shape)
    return made


def table(array) -> Buffer:
    """A buffer whose contents are known while tracing, uploaded once.

    The kernel builds it and the compiler ships it; no caller passes one in.
    """
    made = Buffer(*array.shape)
    made.name = active().declare_table("k", array.shape, array)
    return made
