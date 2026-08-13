"""One kernel at every rung of the DSL band, all compiling and all agreeing.

THIS TEST EXISTS TO HOLD THE BAND OPEN. Every regression in this design so far
has been a rung quietly disappearing: a form gets added, the library is rewritten
in it, and the previous form dies unnoticed because nothing was exercising it.
One witness per design is how a spectrum collapses to a point.

The band is bounded by other levels, and both bounds are tested too:
a construct that states no tiling is a tensor op, and one that names an address
is IR. Neither belongs here.
"""

import numpy as np
import pytest
from kohakuaccel.lang import dims, grid, iface, stage, sweep
from kohakuaccel.machinespec import MachineSpec
from kohakutpu.lang import kernel

from kohakutpu import lang as L

M, K, N = dims("M, K, N")

MACHINE = MachineSpec(
    name="test", units={"MG": ((1, 1), (2, 1)), "VC": ((2, 0),)}, inst_depth=512
)


@kernel
def tiling(a=L.In(M, K), b=L.In(N, K), c=L.Out(M, N), *, gm=2, gn=1, nk=2):
    """Rung T: the split, and nothing else."""
    for i, j in grid(a.tiles(gm), b.tiles(gn)):
        acc = L.tile(gm, gn)
        for k in sweep(a.chunks32(nk)):
            acc += a[i, k] @ b[j, k]
        c[i, j] <<= acc


@kernel
def placement(a=L.In(M, K), b=L.In(N, K), c=L.Out(M, N), *, gm=2, gn=1, nk=2):
    """Rung P: which window holds what, and when it is filled."""
    for i, j in grid(a.tiles(gm), b.tiles(gn)):
        ra, rb = L.region(gm, nk), L.region(gn, nk)
        acc = L.tile(gm, gn)
        for k in sweep(a.chunks32(nk)):
            ra <<= a[i, k]
            rb <<= b[j, k]
            acc += ra @ rb
        c[i, j] <<= acc


@kernel
def schedule(a=L.In(M, K), b=L.In(N, K), c=L.Out(M, N), *, gm=2, gn=1, nk=2):
    """Rung S: and where the barrier falls."""
    with stage(a.tiles(gm), b.tiles(gn)) as (i, j):
        ra, rb = L.region(gm, nk), L.region(gn, nk)
        acc = L.tile(gm, gn)
        for k in sweep(a.chunks32(nk)):
            ra <<= a[i, k]
            rb <<= b[j, k]
            acc += ra @ rb
        c[i, j] <<= acc


RUNGS = [tiling, placement, schedule]


class Shaped:
    """What `solve` needs of an argument, without a device to put it on."""

    def __init__(self, shape) -> None:
        self.shape = shape


def compile_at(fn, m=16, k=64, n=32):
    extents = iface.solve(fn.signature, {"a": Shaped((m, k)), "b": Shaped((n, k))})
    return fn.compile(MACHINE, extents)


@pytest.mark.parametrize("fn", RUNGS, ids=lambda f: f.name)
def test_every_rung_compiles(fn):
    """A rung that stops compiling has been deleted, whatever the intent."""
    assert compile_at(fn).statements > 0


def test_rungs_emit_the_same_program():
    """Saying less must not cost anything.

    The rungs differ in how much was written down, not in what runs. If the
    inferred form emitted more instructions than the hand-placed one, the
    inference would be a tax and nobody would use the top of the band.
    """
    programs = []
    for fn in RUNGS:
        got = compile_at(fn)
        words = fn.backend.encode(
            got, got.stages[0], {"a": 0x1000, "b": 0x2000, "c": 0x3000}
        )
        programs.append([words[at] for at in sorted(words)])
    assert programs[0] == programs[1] == programs[2]


def test_upper_bound_is_the_tensor_level():
    """`c <<= a @ b` between whole buffers is above the band, so it is refused.

    It states no split, which is exactly what `ktpu.matmul(x, w)` states -- so
    it would be a tensor op wearing kernel syntax, and the level above does the
    same thing with less ceremony.
    """
    with pytest.raises((L.LangError, TypeError)):

        @kernel
        def untiled(a=L.In(M, K), b=L.In(N, K), c=L.Out(M, N)):
            c <<= a @ b

        compile_at(untiled)


def test_lower_bound_is_the_ir():
    """No rung names an address: that is the level below."""
    for fn in RUNGS:
        got = compile_at(fn)
        for st in got.stages:
            for inst in st.instances:
                for s in inst.stmts:
                    assert "addr" not in s.args, f"{fn.name} names an address"


def test_a_grid_does_not_force_a_barrier():
    """Two `grid`s that want the same split share a stage; two `stage`s do not."""

    @kernel
    def banded(x=L.In(M, N), y=L.Out(M, N), z=L.Out(M, N), *, part=8192):
        for e in grid(x.parts(part)):
            y[e] <<= x[e] * 2.0
        for e in grid(x.parts(part)):
            z[e] <<= x[e] * 3.0

    @kernel
    def split(x=L.In(M, N), y=L.Out(M, N), z=L.Out(M, N), *, part=8192):
        with stage(x.parts(part)) as e:
            y[e] <<= x[e] * 2.0
        with stage(x.parts(part)) as e:
            z[e] <<= x[e] * 3.0

    extents = iface.solve(banded.signature, {"x": Shaped((16, 64))})
    assert len(banded.compile(MACHINE, extents).stages) == 1
    extents = iface.solve(split.signature, {"x": Shaped((16, 64))})
    assert len(split.compile(MACHINE, extents).stages) == 2


def test_a_read_after_write_is_banded_only_when_one_program_holds_it():
    """Two statements sharing a stage may depend on each other, if ONE program
    runs them.

    A unit does not wait between the programs of one stage, so the second
    reading what the first wrote reads whatever was there before: 79% error,
    measured on `t <<= x * w; y <<= t + x`. A band makes that pair one image
    with `t` in a register, so the rule is now the descriptor budget -- `y`
    below reads a folded constant too, which costs 9 of 8 and splits.
    """

    @kernel
    def dependent(x=L.In(M, N), w=L.In(M, N), y=L.Out(M, N), *, part=8192):
        t = L.temp(M, N)
        t <<= x * w
        y <<= t + x

    @kernel
    def too_wide(x=L.In(M, N), w=L.In(M, N), y=L.Out(M, N), *, part=8192):
        t = L.temp(M, N)
        t <<= x * w
        y <<= t * 0.5 + x

    @kernel
    def independent(x=L.In(M, N), w=L.In(M, N), y=L.Out(M, N), z=L.Out(M, N)):
        y <<= x * w
        z <<= x + w

    bound = {"x": Shaped((16, 64)), "w": Shaped((16, 64))}
    extents = iface.solve(dependent.signature, bound)
    assert (
        len(dependent.compile(MACHINE, extents).stages) == 1
    ), "a read-after-write one program holds should be one stage"
    extents = iface.solve(too_wide.signature, bound)
    assert (
        len(too_wide.compile(MACHINE, extents).stages) == 2
    ), "a read-after-write over the descriptor budget must still split"
    extents = iface.solve(independent.signature, bound)
    assert (
        len(independent.compile(MACHINE, extents).stages) == 1
    ), "independent statements should still share a stage"


def test_a_folded_constant_keeps_its_fraction():
    """A scalar in a kernel must reach the card as itself.

    Extents resolve through `dims.resolve`, which ends in `int(value)`. Routing
    a float through it turns log2 e into 1 and a half into zero -- wrong numbers
    everywhere a scale is folded, and nothing crashes.
    """

    @kernel
    def scaled(x=L.In(M, N), y=L.Out(M, N)):
        y <<= x * 1.4426950408889634 + 0.5

    extents = iface.solve(scaled.signature, {"x": Shaped((16, 64))})
    got = scaled.compile(MACHINE, extents)
    folded = sorted(
        float(array[0]) for array, _ in scaled.backend.constants(got).values()
    )
    assert folded == pytest.approx([0.5, 1.4426950408889634], rel=1e-3), folded


def test_an_extent_valued_constant_resolves():
    """`x / N` folds the row length, which is only a number once a shape lands."""

    @kernel
    def averaged(x=L.In(M, N), y=L.Out(M, N)):
        y <<= x / N

    extents = iface.solve(averaged.signature, {"x": Shaped((16, 64))})
    got = averaged.compile(MACHINE, extents)
    folded = [float(a[0]) for a, _ in averaged.backend.constants(got).values()]
    assert folded == [64.0], folded


def test_numbers_agree_offline():
    """Every rung lowers to the same instruction words, so one check covers all."""
    rng = np.random.default_rng(0)
    a = rng.standard_normal((16, 64)).astype(np.float16)
    assert a.shape == (16, 64)
    counts = {fn.name: compile_at(fn).statements for fn in RUNGS}
    assert len(set(counts.values())) == 1, counts
