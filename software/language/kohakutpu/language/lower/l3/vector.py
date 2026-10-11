"""L3 -> L2 for vector-core kernels, on every vector core of the target.

- Elementwise over [N]: each core takes N / cores in tiles of `elems`, two in
  flight: the largest of 2048, 1024, 512 whose inputs and output,
  double-buffered, fit L1 and whose pairs make up the core's share.
- Rows over [R, C]: each core takes R / cores rows in tiles of `rows` (16,
  else 8), two in flight; [C] parameters load once before the loop.
"""

from kohakutpu.language.lower.l3.common import (
    Names,
    PlanError,
    cores,
    lets,
    returned,
    size,
    ty,
)
from kohakutpu.language.text.writer import Text

#: Stream tiles, largest first; the lowering takes whole 512-element groups.
ELEMS = (2048, 1024, 512)
#: Row tiles, largest first: the lowering's lockstep (16) and pipelined (8).
ROWS = (16, 8)
#: f16 values in an L1 word.
PER_WORD = 16


def _share(extent: int, n_cores: int, what: str) -> int:
    if extent % n_cores:
        raise PlanError(f"{extent} {what} do not split over {n_cores} cores")
    return extent // n_cores


def stream(fn, dims: dict, target, elems: int | None = None) -> str:
    params = [(p, size(t.shape, dims)) for p, t in fn.params]
    (n,) = params[0][1]
    if any(s != (n,) for _, s in params):
        raise PlanError("an elementwise body's parameters are all [N]")
    n_cores = cores(target)
    per = _share(n, n_cores, "values")
    if elems is None:
        fits = [
            e
            for e in ELEMS
            if 2 * (e // PER_WORD) * (len(params) + 1) <= target.l1_words
            and per % (2 * e) == 0
        ]
        if not fits:
            raise PlanError(
                f"{per} values a core: no tile of {list(ELEMS)} fits L1 "
                f"double-buffered and comes in pairs"
            )
        elems = fits[0]
    res = returned(fn)
    names = Names([p for p, _ in params] + [res, "c", "k", "t"])
    if per % (2 * elems):
        raise PlanError(f"{n} values are not whole tile pairs of {elems} a core")
    out = Text()
    sig = ", ".join(f"{p}: {ty('f16', s, 'dram')}" for p, s in params)
    out(f"fn {fn.name}.l2({sig}, {res}: {ty('f16', (n,), 'dram')})")
    with out.block():
        out(f"par c in 0..{n_cores} on vc[c]")
        with out.block():
            out(f"for t in 0..{per // elems} pipe=2")
            with out.block():
                at = f"c*{per} + t*{elems} : +{elems}"
                for p, _ in params:
                    out(f"{names(p)} = load {p}[{at}] : {ty('f16', (elems,), 'l1')}")
                for s in lets(fn):
                    args = ", ".join(names.operand(a) for a in s.args)
                    out(
                        f"{names(s.name)} = {s.op} {args} : {ty(s.type.dtype, (elems,))}"
                    )
                out(f"store {res}[{at}] <- {names(res)}")
    return out.text()


def rows(fn, dims: dict, target, rows: int | None = None) -> str:
    params = [(p, size(t.shape, dims)) for p, t in fn.params]
    main = [s for _, s in params if len(s) == 2]
    if not main or any(s != main[0] for s in main):
        raise PlanError("a row body has one [R, C] shape")
    r, c = main[0]
    res = returned(fn)
    names = Names([p for p, _ in params] + [res, "c", "k", "t"])
    n_cores = cores(target)
    per = _share(r, n_cores, "rows")
    if rows is None:
        fits = [k for k in ROWS if per % (2 * k) == 0]
        if not fits:
            raise PlanError(f"{per} rows a core are not tile pairs of {list(ROWS)}")
        rows = fits[0]
    if per % (2 * rows):
        raise PlanError(f"{r} rows are not whole tile pairs of {rows} a core")
    out = Text()
    sig = ", ".join(f"{p}: {ty('f16', s, 'dram')}" for p, s in params)
    out(f"fn {fn.name}.l2({sig}, {res}: {ty('f16', (r, c), 'dram')})")
    with out.block():
        out(f"par k in 0..{n_cores} on vc[k]")
        with out.block():
            for p, s in params:
                if s == (c,):
                    out(f"{names(p)} = load {p} : {ty('f16', s, 'l1')}")
                elif len(s) != 2:
                    raise PlanError(f"{p}: a row body takes [R, C] and [C]")
            out(f"for t in 0..{per // rows} pipe=2")
            with out.block():
                at = f"k*{per} + t*{rows} : +{rows}, :"
                for p, s in params:
                    if len(s) == 2:
                        out(
                            f"{names(p)} = load {p}[{at}] : {ty('f16', (rows, c), 'l1')}"
                        )
                for s in lets(fn):
                    shape = size(s.type.shape, dims)
                    shape = (rows, *shape[1:]) if shape and shape[0] == r else shape
                    args = ", ".join(names.operand(a) for a in s.args)
                    out(f"{names(s.name)} = {s.op} {args} : {ty(s.type.dtype, shape)}")
                out(f"store {res}[{at}] <- {names(res)}")
    return out.text()


__all__ = ["rows", "stream"]
