"""L3 -> L2 for vector-core kernels.

- Elementwise over [N]: each core takes N / 2 in tiles of `elems` (2048,
  128 L1 words), two in flight.
- Rows over [R, C]: each core takes R / 2 rows in tiles of `rows` (16), two in
  flight; [C] parameters load once before the loop.
"""

from kohakutpu.language.lower.l2.emit import L1_WORDS
from kohakutpu.language.lower.l3.common import (
    CORES,
    Names,
    PlanError,
    lets,
    returned,
    size,
    ty,
)
from kohakutpu.language.text.writer import Text


def stream(fn, dims: dict, elems: int | None = None) -> str:
    params = [(p, size(t.shape, dims)) for p, t in fn.params]
    (n,) = params[0][1]
    if any(s != (n,) for _, s in params):
        raise PlanError("an elementwise body's parameters are all [N]")
    if elems is None:
        # The largest tile whose inputs and output, double-buffered, fit L1.
        elems = 2048
        while elems > 512 and 2 * (elems // 16) * (len(params) + 1) > L1_WORDS:
            elems //= 2
    res = returned(fn)
    names = Names([p for p, _ in params] + [res, "c", "k", "t"])
    per = n // CORES
    if per % (2 * elems):
        raise PlanError(f"{n} values are not whole tile pairs of {elems} a core")
    out = Text()
    sig = ", ".join(f"{p}: {ty('f16', s, 'dram')}" for p, s in params)
    out(f"fn {fn.name}.l2({sig}, {res}: {ty('f16', (n,), 'dram')})")
    with out.block():
        out(f"par c in 0..{CORES} on vc[c]")
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


def rows(fn, dims: dict, rows: int = 16) -> str:
    params = [(p, size(t.shape, dims)) for p, t in fn.params]
    main = [s for _, s in params if len(s) == 2]
    if not main or any(s != main[0] for s in main):
        raise PlanError("a row body has one [R, C] shape")
    r, c = main[0]
    res = returned(fn)
    names = Names([p for p, _ in params] + [res, "c", "k", "t"])
    per = r // CORES
    if per % (2 * rows):
        raise PlanError(f"{r} rows are not whole tile pairs of {rows} a core")
    out = Text()
    sig = ", ".join(f"{p}: {ty('f16', s, 'dram')}" for p, s in params)
    out(f"fn {fn.name}.l2({sig}, {res}: {ty('f16', (r, c), 'dram')})")
    with out.block():
        out(f"par k in 0..{CORES} on vc[k]")
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
