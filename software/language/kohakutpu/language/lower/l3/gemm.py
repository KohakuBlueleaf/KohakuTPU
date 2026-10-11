"""L3 -> L2 for chains of cluster gemms with vector epilogues (MLP, SwiGLU).

Parameters used only through `quantise` become MXFP7 memory: `mx7a` where a
gemm reads them as A, `mx7b` as B. Each gemm C = A B^T runs in 256 x 256
output tiles: column tile u on cluster u % 4, row tiles in order. Gemms of
one A read by one epilogue share a phase, so the epilogue sees all its tiles
in one instance. An epilogue (the elementwise ops from gemm results to a
`quantise`) runs on core u % 2 and writes its tile of an `mx7a` scratch
buffer; the result gemm drains straight to the output.
"""

from kohakutpu.language.lower.l3.common import (
    CLUSTERS,
    CORES,
    Names,
    PlanError,
    lets,
    returned,
    size,
    ty,
)
from kohakutpu.language.text.writer import Text

TILE = 256


def plan(fn, dims: dict) -> str:
    body = lets(fn)
    res = returned(fn)
    by = {s.name: s for s in body}
    params = {p: size(t.shape, dims) for p, t in fn.params}
    # MXFP7 sources: quantised parameter -> its role.
    mx7 = {}
    for s in body:
        if s.op == "mmt":
            for role, a in zip("ab", s.args, strict=True):
                src = by.get(a.name)
                if src is None or src.op != "quantise":
                    raise PlanError(f"gemm operand {a.name} is not quantised")
                mx7[a.name] = role
    readers: dict = {}
    for s in body:
        for a in s.args:
            if hasattr(a, "name"):
                readers.setdefault(a.name, []).append(s)
    mem_params = {}
    for s in body:
        if s.op == "quantise" and s.args[0].name in params:
            p = s.args[0].name
            if s.name not in mx7 or len(readers.get(p, [])) != 1:
                raise PlanError(f"{p} is used other than through one quantise")
            mem_params[p] = ("mx7" + mx7[s.name], s.name)
    # Phases: gemms grouped by the epilogue (or output) consuming them.
    gemms = [s for s in body if s.op == "mmt"]
    epi_of = {}
    for g in gemms:
        epi_of[g.name] = None if g.name == res else _epilogue(g.name, by, readers)
    phases: list = []
    for g in gemms:
        key = epi_of[g.name]
        if phases and key is not None and phases[-1][0] == key:
            phases[-1][1].append(g)
        else:
            phases.append((key, [g]))
    scratch = {}
    for key, _ in phases:
        if key is not None:
            q = by[key]
            scratch[key] = size(q.type.shape, dims)
    names = Names(list(params) + [res] + list(scratch) + ["u", "i", "c"])
    m_out = size(by[res].type.shape, dims)
    out = Text()
    sig = []
    for p, shape in params.items():
        dtype = mem_params[p][0] if p in mem_params else "f16"
        sig.append(f"{p}: {ty(dtype, shape, 'dram')}")
    sig.append(f"{res}: {ty('f16', m_out, 'dram')}")
    out(f"fn {fn.name}.l2({', '.join(sig)})")
    mem_of = {q: p for p, (_, q) in mem_params.items()}
    mem_of.update({k: k for k in scratch})
    with out.block():
        for k, shape in scratch.items():
            out(f"{k} = buffer : {ty('mx7a', shape, 'dram')}")
        for key, group in phases:
            _phase(out, group, key, by, readers, mem_of, names, dims, res)
    return out.text()


def _epilogue(g: str, by: dict, readers: dict) -> str:
    """The quantise ending the elementwise ops fed by gemm `g`."""
    seen, todo = set(), [g]
    while todo:
        v = todo.pop()
        for r in readers.get(v, []):
            if r.name in seen:
                continue
            seen.add(r.name)
            if r.op == "quantise":
                return r.name
            if r.op == "mmt":
                raise PlanError(f"{g} reaches a gemm without a quantise")
            todo.append(r.name)
    raise PlanError(f"{g} reaches no quantise and is not the result")


def _phase(out, group, key, by, readers, mem_of, names, dims, res) -> None:
    a = group[0].args[0].name
    if any(g.args[0].name != a for g in group):
        raise PlanError("gemms of one phase read one A")
    am = mem_of[a]
    m, k = size(by[a].type.shape, dims)
    n = size(group[0].type.shape, dims)[1]
    if m % TILE or n % TILE:
        raise PlanError(f"gemm {m} x {n} is not whole {TILE} tiles")
    out(f"par u in 0..{n // TILE} on mg[u % {CLUSTERS}]")
    with out.block():
        out(f"for i in 0..{m // TILE}")
        with out.block():
            for g in group:
                b = mem_of[g.args[1].name]
                acc = names(g.name + "_acc")
                out(
                    f"{acc} = gemm {am}[i*{TILE} : +{TILE}, :], {b}[u*{TILE} : +{TILE}, :] "
                    f"k={k} : {ty('f32', (TILE, TILE), 'acc')}"
                )
                if g.name == res:
                    out(
                        f"store {res}[i*{TILE} : +{TILE}, u*{TILE} : +{TILE}] <- drain {acc}"
                    )
                else:
                    out(
                        f"{names(g.name)} = drain {acc} : {ty('f16', (TILE, TILE), 'dram')}"
                    )
            if key is None:
                return
            out(f"par c in 0..1 on vc[u % {CORES}]")
            with out.block():
                loc = Names(
                    list(names.map.values()) + list(mem_of.values()) + ["u", "i", "c"]
                )
                for g in group:
                    loc.map[g.name] = names(g.name) + "t"
                    out(
                        f"{loc(g.name)} = load {names(g.name)} : {ty('f16', (TILE, TILE), 'l1')}"
                    )
                for s in _chain(group, key, by, readers):
                    args = ", ".join(loc.operand(x) for x in s.args)
                    out(
                        f"{loc(s.name)} = {s.op} {args} : {ty(s.type.dtype, (TILE, TILE))}"
                    )
                q = by[key]
                out(
                    f"store {key}[i*{TILE} : +{TILE}, u*{TILE} : +{TILE}] <- "
                    f"quantise {loc(q.args[0].name)}"
                )


def _chain(group, key, by, readers) -> list:
    """The elementwise lets from the group's gemms to the quantise `key`, in
    body order."""
    want, todo = set(), [by[key].args[0].name]
    starts = {g.name for g in group}
    while todo:
        v = todo.pop()
        if v in starts or v in want:
            continue
        s = by.get(v)
        if s is None:
            raise PlanError(f"the epilogue reads {v}, not a gemm result")
        want.add(v)
        todo += [x.name for x in s.args if hasattr(x, "name")]
    return [s for s in by.values() if s.name in want]


__all__ = ["plan"]
