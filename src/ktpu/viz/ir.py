"""Render the three IR levels as HTML, and compile an arbitrary kernel.

`ktpu.interp` answers "what does it compute". This answers "what did the
compiler decide", which is the question the printed forms were always for --
`.plan/measurements/dispatch-n-only.md` exists because a scheduling decision
nobody could see survived review.

`compile_kernel` EXECUTES the source it is given. It is a local development
tool and `scripts/py/ir_server.py` binds to loopback for that reason; do not put
it on a network.
"""

import html
import io
import traceback

import numpy as np

from ktpu.codegen import codegen, cost_of
from ktpu.interp.timing import MACS_PER_CLUSTER, time_of
from ktpu.ir import FP16
from ktpu.ir.graph import OpKind
from ktpu.ir.sched import Engine
from ktpu.passes import lower
from ktpu.target import VU13P_8CU

#: Op kind -> CSS class, so a reader can find the matmuls and the reductions
#: without reading every line.
KIND_CLASS = {
    OpKind.MATMUL: "k-mm",
    OpKind.INPUT: "k-in",
    OpKind.CONST: "k-const",
}


def _esc(x) -> str:
    return html.escape(str(x))


def _kind_class(kind: OpKind) -> str:
    from ktpu.ir.graph import REDUCTION, VIEW

    if kind in KIND_CLASS:
        return KIND_CLASS[kind]
    if kind in REDUCTION:
        return "k-red"
    if kind in VIEW:
        return "k-view"
    return "k-elem"


def render_graph(graph) -> str:
    """Level 1 as a table: value, op, operands, shape. No machine in sight."""
    rows = []
    for op in graph.ops:
        ins = ", ".join(f'<span class="vid">%{v.vid}</span>' for v in op.inputs)
        attrs = " ".join(
            f'<span class="attr">{_esc(k)}={_esc(v)}</span>'
            for k, v in sorted(op.attrs.items())
            if k != "name"
        )
        name = op.attrs.get("name")
        shape = "x".join(str(d) for d in op.out.shape) or "scalar"
        rows.append(
            f'<tr><td class="vid">%{op.out.vid}</td>'
            f'<td><span class="kind {_kind_class(op.kind)}">{_esc(op.kind.value)}'
            f"</span>{f' <b>{_esc(name)}</b>' if name else ''}</td>"
            f"<td>{ins}</td><td class='shape'>{_esc(shape)}</td>"
            f"<td class='dim'>{attrs}</td></tr>"
        )
    outs = ", ".join(f"%{v.vid}" for v in graph.outputs)
    return (
        f'<div class="meta">{len(graph.ops)} ops &middot; '
        f"inputs {len(graph.inputs)} &middot; returns {_esc(outs)}</div>"
        '<table class="ir"><thead><tr><th>value</th><th>op</th><th>operands</th>'
        "<th>shape</th><th></th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def render_schedule(sched) -> str:
    """Level 2 as one card per band: engine, grid, tile, and the ops on it."""
    cards = []
    for b in sched.bands:
        eng = b.engine.value
        tags = []
        if b.consumes:
            tags.append(f'<span class="tag t-fold">consumes {_esc(b.consumes)}</span>')
        if b.reduces:
            tags.append(f'<span class="tag t-red">reduces {_esc(b.reduces)}</span>')
        if b.grid.sk > 1:
            tags.append(f'<span class="tag t-sk">split-K {b.grid.sk}</span>')
        if b.engine is Engine.MATMUL:
            a = b.ops[0].attrs
            body = (
                f"<code>C[{b.tile.m},{b.tile.n}] += A[{b.tile.m},{b.tile.k}] "
                f"@ B[{b.tile.k},{b.tile.n}]</code>"
                f'<div class="dim">whole problem {a.get("m")} x {a.get("k")} x '
                f'{a.get("n")} &middot; accumulates in acc24</div>'
            )
        else:
            ops = " ".join(
                f'<span class="kind {_kind_class(o.kind)}">{_esc(o.kind.value)}</span>'
                for o in b.ops
            )
            elems = b.tile.m * b.tile.n
            body = (
                f"{ops}"
                f'<div class="dim">{len(b.ops)} ops on {b.tile.m}x{b.tile.n} '
                f"= {elems:,} elements per instance</div>"
            )
        cards.append(
            f'<div class="band e-{eng}"><div class="bhead">'
            f'<span class="bname">{_esc(b.name)}</span>'
            f'<span class="eng">{_esc(eng)}</span>'
            f'<span class="grid">grid{_esc(b.grid)} = {b.grid.size} instances</span>'
            f'<span class="tile">tile {_esc(b.tile)}</span>'
            f'{"".join(tags)}</div>{body}</div>'
        )
    return (
        f'<div class="meta">{len(sched.bands)} bands</div>' + "".join(cards)
        if cards
        else '<div class="meta">no bands</div>'
    )


def render_program(prog, limit: int = 400) -> str:
    """Level 3: the memory map, then the instruction stream grouped by node."""
    regions = "".join(
        f"<tr><td><b>{_esc(r.name)}</b></td>"
        f'<td class="num">{r.word:,}</td><td class="num">{r.words:,}</td>'
        f'<td class="num">{r.elems:,}</td><td class="dim">{_esc(r.note)}</td></tr>'
        for r in prog.memory.regions
    )
    alias = ", ".join(f"{a}&rarr;{t}" for a, t in sorted(prog.memory.aliases.items()))

    rows = []
    for i in prog.insts[:limit]:
        args = " ".join(
            f'<span class="attr">{_esc(k)}={_esc(v)}</span>'
            for k, v in i.fields.items()
            if k != "srcs"
        )
        srcs = i.fields.get("srcs")
        if srcs:
            args = f'<span class="srcs">{_esc(", ".join(srcs))}</span> ' + args
        rows.append(
            f'<tr><td class="node">{i.node[0]},{i.node[1]}</td>'
            f'<td><span class="op o-{_esc(i.engine.value)}">'
            f"{_esc(i.op.value)}</span></td><td>{args}</td></tr>"
        )
    more = (
        f'<tr><td colspan="3" class="dim">... {len(prog.insts) - limit} more</td></tr>'
        if len(prog.insts) > limit
        else ""
    )
    return (
        f'<div class="meta">{len(prog.insts):,} instructions &middot; '
        f"{len(prog.rounds)} rounds &middot; image {prog.memory.total:,} words "
        f"({prog.memory.total * 4 / 1024:,.0f} KiB)</div>"
        '<table class="ir"><thead><tr><th>region</th><th>word</th><th>words</th>'
        "<th>elems</th><th></th></tr></thead><tbody>"
        + regions
        + "</tbody></table>"
        + (f'<div class="dim">aliases: {alias}</div>' if alias else "")
        + '<table class="ir asm"><thead><tr><th>node</th><th>op</th><th></th>'
        "</tr></thead><tbody>" + "".join(rows) + more + "</tbody></table>"
    )


def render_cost(cost, timing=None, target=None) -> str:
    """The counted figures, `overhead` first because it is the one to watch.

    With `timing`, the cycle figures come first instead: they are a BRACKET,
    because the model charges every FILL while the machine hides most of them
    behind the GEMM before them.
    """
    per = " ".join(
        f'<span class="attr">{_esc(k)} {v}</span>'
        for k, v in sorted(cost.insts.items())
    )
    cells = []
    if timing is not None and target is not None and timing.cycles:
        hz = target.mhz * 1e6
        # Both engines, because `flops` counts both: 2mkn per matmul band plus
        # one per elementwise op per element.
        mm_peak = MACS_PER_CLUSTER * 2 * target.clusters
        vec_peak = target.vector_cores * target.vector_lanes
        peak = max(1, mm_peak + vec_peak) * hz / 1e9
        lo = cost.flops / (timing.cycles / hz) / 1e9
        hi = cost.flops / (max(1, timing.overlapped) / hz) / 1e9
        mm = timing.utilisation("matmul", target.clusters) * 100
        vec = timing.utilisation("vector", target.vector_cores) * 100
        ops = " ".join(
            f'<span class="attr">{_esc(k)} {v:,}</span>'
            for k, v in sorted(timing.by_op.items())
        )
        fast_ms = timing.overlapped / hz * 1e3
        when = (
            f"{fast_ms:.3f} to {timing.seconds * 1e3:.3f} ms at "
            f"{target.mhz:g} MHz &mdash; the low end assumes every FILL hides "
            "behind the GEMM before it, the high end charges all of them"
        )
        rate = (
            f"{100 * lo / peak:.1f}% to {100 * hi / peak:.1f}% of the "
            f"{peak:,.0f} GF/s peak &middot; matmul and vector flops both counted"
        )
        cells += [
            ("cycles", f"{timing.overlapped:,}–{timing.cycles:,}", when),
            ("throughput", f"{lo:,.0f}–{hi:,.0f} GF/s", rate),
            ("busy", f"{mm:.0f}% / {vec:.0f}%", f"matmul / vector &middot; {ops}"),
        ]
    cells += [
        ("overhead", f"{cost.overhead:.3f}", "dram bytes per useful flop"),
        ("instructions", f"{cost.total_insts:,}", per),
        (
            "device image",
            f"{cost.image_words:,} w",
            f"{cost.image_words * 4 / 1024:,.0f} KiB",
        ),
        ("dram read", f"{cost.dram_read:,} w", ""),
        ("dram write", f"{cost.dram_write:,} w", ""),
        ("useful flops", f"{cost.flops:,}", "what the KERNEL asked for"),
        ("vector body", f"{cost.vector_body:,}", "instructions inside the loop"),
    ]
    return (
        '<table class="ir cost"><tbody>'
        + "".join(
            f'<tr><td class="ck">{_esc(k)}</td><td class="cv">{_esc(v)}</td>'
            f'<td class="dim">{n}</td></tr>'
            for k, v, n in cells
        )
        + "</tbody></table>"
    )


def compile_kernel(source: str, target=VU13P_8CU, seed: int = 0) -> dict:
    """Execute `source`, compile the kernel it defines, and run it.

    The source must define `kernel(...)` and `INPUTS`, a dict of parameter name
    to shape. It may define `CONSTS`, name -> array, for operands that are
    structure rather than data -- a causal mask is a triangular constant, and
    filling it with noise would compile but mean nothing. Returns a dict of HTML
    fragments plus `error` (None on success) and `check`, the relative error
    against the level-1 reference.

    THIS RUNS ARBITRARY PYTHON. Local tool only -- see the module docstring.
    """
    out = {"error": None, "graph": "", "sched": "", "prog": "", "cost": "", "check": ""}
    try:
        import ktpu.dsl as D
        from ktpu.dsl import nn
        from ktpu.interp import run
        from ktpu.interp.mesh import Mesh

        ns: dict = {"D": D, "np": np, "nn": nn, "FP16": FP16}
        exec(compile(source, "<kernel>", "exec"), ns)  # noqa: S102
        fn, shapes = ns.get("kernel"), ns.get("INPUTS")
        if fn is None or shapes is None:
            raise ValueError("source must define kernel(...) and INPUTS")

        import inspect

        def spec_of(shape):
            """A shape, or a LIST of shapes for a variadic operand like MoE's
            per-expert weights, which the tracer walks at trace time."""
            if isinstance(shape, list):
                return [D.TensorSpec(tuple(s), FP16) for s in shape]
            return D.TensorSpec(tuple(shape), FP16)

        # BY NAME: with optional operands, passing positionally slides the next
        # one into the gap. A parameter absent from INPUTS keeps its default.
        names = list(inspect.signature(fn).parameters)
        extra = {n: shapes[n] for n in names if n in shapes}
        missing = [n for n in shapes if n not in names]
        if missing:
            raise ValueError(
                f"INPUTS names {missing} that kernel() does not take; "
                f"its parameters are {names}"
            )
        graph = D.trace(fn, **{n: spec_of(s) for n, s in extra.items()})
        sched = lower(graph, target)

        # From the RESULT, not the first matmul: MoE's gate is T x C x E, so
        # that n is the expert count and lays the image out for a 64x4 result.
        shp = graph.outputs[0].shape
        m, n = shp[0], (shp[1] if len(shp) > 1 else 1)

        # `k` from the band codegen tiles for -- the last matmul, or what a
        # folded epilogue consumes. Taking the first indexes off the end.
        by_name = {b.name: b for b in sched.bands}
        mm = by_name.get(sched.bands[-1].consumes or "") or next(
            (b for b in reversed(sched.bands) if b.engine is Engine.MATMUL), None
        )
        k = mm.ops[0].attrs["k"] if mm is not None else 1

        prog = codegen(sched, m, k, n, target)

        out["graph"] = render_graph(graph)
        out["sched"] = render_schedule(sched)
        out["prog"] = render_program(prog)
        counted = cost_of(prog, sched, target)
        out["cost"] = render_cost(counted, time_of(prog, target, counted.flops), target)

        rng = np.random.default_rng(seed)
        consts = ns.get("CONSTS") or {}
        arrays = []
        for name, s in extra.items():
            group = s if isinstance(s, list) else [s]
            for g in group:
                given = consts.get(name)
                arrays.append(
                    np.asarray(given, dtype=np.float64)
                    if given is not None
                    else rng.standard_normal(tuple(g)) * 0.3
                )
        mesh = Mesh(prog, target)
        if mm is not None and len(arrays) >= 2:
            mesh.upload(sched.bands[0], arrays[0], arrays[1])
            bound = 2
        else:
            bound = 0
        for value, arr in list(zip(graph.inputs, arrays, strict=False))[bound:]:
            mesh.bind(graph.producer(value).attrs["name"], arr)
        mesh.run()

        # A kernel may return several values -- causal attention keeps softmax
        # state per query block -- so compare the whole set, in order.
        names = [graph.producer(v).attrs["name"] for v in graph.inputs]
        want = np.concatenate(
            [np.asarray(x).reshape(-1) for x in run(graph, dict(zip(names, arrays)))]
        )
        got = np.concatenate(
            [mesh.result(f"v{v.vid}").reshape(-1) for v in graph.outputs]
        )
        rel = float(np.abs(got - want).max() / max(1e-30, np.abs(want).max()))
        ok = rel < 5e-3
        out["check"] = (
            f'<div class="check {"ok" if ok else "bad"}">'
            f'{"MATCHES" if ok else "DIVERGES"} the float64 reference &middot; '
            f"relative error {rel:.2e}"
            + (f" &middot; {mesh.saturated:,} clamped" if mesh.saturated else "")
            + "</div>"
        )
    except Exception:  # noqa: BLE001 -- a playground must never 500
        buf = io.StringIO()
        traceback.print_exc(file=buf)
        out["error"] = buf.getvalue()
    return out
