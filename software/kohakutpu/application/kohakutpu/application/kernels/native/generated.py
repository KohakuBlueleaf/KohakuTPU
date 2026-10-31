"""Native eager ops built from Rust FP16-input contraction stages
(`matmul.mm_kernel` under `activation_fp16` and `ring_depth`).

Graph composition is before lowering; compiled output uses the normal library.
"""

import json
from dataclasses import asdict

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels.native import layout as Y
from kohakutpu.application.kernels.native import linear as L
from kohakutpu.application.kernels.native import rows as R
from kohakutpu.application.kernels.native.composition import (
    GraphKernel,
    apply_quant_backend,
    compose,
    logical_rows,
    names,
    renamed,
    sequence,
    statements,
    trim_out_tail,
    var_names,
)
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.plan.emit import expr
from kohakutpu.application.runtime import generated as G
from kohakutpu.application.runtime import launch_reductions as LR
from kohakutpu.language import dsl as kt
from kohakutpu.stack import rs
from kohakutpu.stack.kernel import dsl_machine


def graph(generator, arguments, machine):
    return json.loads(
        G.graph(
            generator,
            json.dumps(arguments, sort_keys=True),
            rs.machine_json(machine),
            rs._MUTANT.get(),
        )
    )


def mentions(term, var):
    """Whether an index term reads loop variable `var`."""
    return {"var": var} == term or any(
        mentions(part, var)
        for part in (term.values() if isinstance(term, dict) else term)
        if isinstance(part, (dict, list))
    )


def _cluster_n_tiles(rows, columns, clusters):
    ntm = -(-rows // min(rows, 256))
    return (
        1
        if ntm % clusters == 0
        else next(
            (
                columns // (x * 32)
                for x in range(min(columns, 256) // 32, 0, -1)
                if columns % (x * 32) == 0 and (columns // (x * 32)) % clusters == 0
            ),
            clusters,
        )
    )


def linear_graph(
    rows,
    width,
    columns,
    machine,
    activation="none",
    residual=False,
    ring_depth=2,
    *,
    out_offset=0,
    out_rows=None,
    n_tiles_override=None,
):
    if rows % 4 or width % 32:
        raise ValueError(
            "FP16 contraction stage requires rows multiple4 and width multiple32"
        )
    rem_rows = rows if out_rows is None else out_rows
    clusters = machine.count("MG")
    n_tiles = (
        _cluster_n_tiles(rows, columns, clusters)
        if n_tiles_override is None
        else n_tiles_override
    )
    extras = [Operand("col")]
    if residual:
        extras.append(Operand("full", offset=out_offset, pitch=columns, rows=rem_rows))
    out = graph(
        "matmul.mm_kernel",
        {
            "activation_any": True,
            "mm": {
                "made_by": {
                    "gen": "matmul.plan",
                    "args": {
                        "bt": 1,
                        "m": rows,
                        "n": columns,
                        "k": width,
                        "pad_m": False,
                        "dense_n": True,
                        "n_tiles": n_tiles,
                    },
                }
            },
            "e": expr(L.epilogue(activation, residual)),
            "expr_cse": True,
            "extras": [asdict(o) for o in extras],
            "out": asdict(
                Operand("full", offset=out_offset, pitch=columns, rows=rem_rows)
            ),
            "ring_depth": (
                1
                if rows <= 256 and n_tiles <= clusters and ring_depth > 1
                else ring_depth
            ),
            "quant_ring": 1 if rows * width > 524288 and ring_depth > 0 else 0,
        },
        machine,
    )
    return out


def linear(
    rows,
    width,
    columns,
    machine,
    activation="none",
    residual=False,
    ring_depth=2,
    row_capacity=None,
    *,
    mover_overlap=True,
    quant_backend="mover_quant",
):
    capacity = -(-rows // 4) * 4 if row_capacity is None else row_capacity
    source = linear_graph(
        capacity, width, columns, machine, activation, residual, ring_depth
    )
    if row_capacity is not None:
        source = logical_rows(
            source, rows, capacity, ("a", "in2") if residual else ("a",), machine
        )
    elif capacity != rows:
        if ring_depth > 0:
            source = trim_out_tail(source, rows, capacity, "out", machine)
            source = logical_rows(
                source,
                rows,
                capacity,
                ("a", "in2") if residual else ("a",),
                machine,
                pad_outputs=False,
                classed=not mover_overlap,
            )
        else:
            source = logical_rows(
                source,
                rows,
                capacity,
                ("a", "in2") if residual else ("a",),
                machine,
                classed=not mover_overlap,
            )
    source = apply_quant_backend(source, quant_backend, machine)
    return GraphKernel(source)


def _mlp_stage(
    rows,
    width,
    hidden,
    columns,
    machine,
    activation="gelu",
    residual=False,
    ring_depth=2,
    edge_space="stage",
    *,
    out_offset=0,
    out_rows=None,
    share_stage=False,
    compact_mlp=True,
    mover_overlap=True,
    quant_backend="mover_quant",
):
    clusters = machine.count("MG")
    sec_nt = _cluster_n_tiles(rows, columns, clusters)
    sec_tn = columns // sec_nt
    unify_h2 = (
        compact_mlp
        and share_stage
        and clusters > 1
        and ring_depth > 0
        and hidden % sec_tn == 0
    )
    first_nt = hidden // sec_tn if unify_h2 else None
    eff_ring = 1 if unify_h2 else ring_depth
    first = linear_graph(
        rows,
        width,
        hidden,
        machine,
        "none" if activation == "gelu" else activation,
        False,
        eff_ring,
        n_tiles_override=first_nt,
    )
    piped = eff_ring > 0 and hidden % 64 == 0
    vc_ep = first["body"][1]["body"][1] if piped else None
    store_stmt = (
        next(s for s in statements(vc_ep["body"]) if s.get("kind") == "store")
        if piped
        else None
    )
    wc = store_stmt["dst"]["axes"][1]["run"][1] if piped else hidden
    if activation == "gelu":
        x = X.Load(0)
        z = X.op(
            "mul",
            X.op("add", x, X.op("mul", X.op("mul", X.op("mul", x, x), x), 0.044715)),
            0.7978845608028654,
        )
        tanh = X.op(
            "sub",
            X.op(
                "mul",
                X.op(
                    "recip",
                    X.op(
                        "add",
                        X.op("exp2", X.op("mul", X.op("mul", z, 2), -kt.LOG2E)),
                        1,
                    ),
                ),
                2,
            ),
            1,
        )
        act_args = {
            "e": expr(X.op("mul", X.op("mul", x, 0.5), X.op("add", tanh, 1))),
            "expr_cse": True,
            "operands": [asdict(Operand("full", pitch=hidden, rows=rows))],
            "R": rows,
            "W": wc,
            "out": asdict(Operand("full", pitch=hidden, rows=rows)),
        }
        if piped and wc < 256 and rows % 8 != 0:
            act_args["bands"] = 1
        activation_graph = graph("vector.rows_kernel", act_args, machine)
        rm = names(activation_graph, "act_")
        rm["in0"] = "out"
        rm["out"] = "out"
        g_act = renamed(activation_graph, rm)
        if piped and len(g_act["body"]) == 1 and vc_ep["body"][-1].get("kind") == "for":
            act_par = json.loads(json.dumps(g_act["body"][0]))
            p_var = act_par["var"]
            col_ax = renamed(
                store_stmt["dst"]["axes"][1],
                {vc_ep["var"]: p_var},
            )
            act_par["hi"] = vc_ep["hi"]
            act_par["unit"] = renamed(vc_ep["unit"], {vc_ep["var"]: p_var})
            t_loop = act_par["body"][-1]
            height = t_loop["body"][-1]["dst"]["axes"][0]["run"][1]
            t_loop["hi"] = rows // height
            for s in statements(act_par["body"]):
                if s.get("op") == "load" and s["args"][0]["view"]["buffer"] == "out":
                    ax0 = s["args"][0]["view"]["axes"][0]["run"][0]
                    ax0["terms"] = [
                        t for t in ax0["terms"] if not mentions(t[0], p_var)
                    ]
                    s["args"][0]["view"]["axes"][1] = col_ax
                elif s.get("kind") == "store" and s["dst"]["buffer"] == "out":
                    ax0 = s["dst"]["axes"][0]["run"][0]
                    ax0["terms"] = [
                        t for t in ax0["terms"] if not mentions(t[0], p_var)
                    ]
                    s["dst"]["axes"][1] = col_ax
            first = json.loads(json.dumps(first))
            first["scratch"].extend(g_act["scratch"])
            first["body"][1]["body"].append(act_par)
        else:
            first = dict(
                first,
                scratch=[*first["scratch"], *g_act["scratch"]],
                body=first["body"] + g_act["body"],
            )
    if share_stage:
        first["scratch"][0]["spec"]["shape"] = [rows, hidden]
        first["body"][0]["dst"]["axes"] = ["all", {"run": [0, width]}]
    second = linear_graph(
        rows,
        hidden,
        columns,
        machine,
        "none",
        residual,
        eff_ring,
        out_offset=out_offset,
        out_rows=out_rows,
        n_tiles_override=sec_nt if unify_h2 else None,
    )
    comp = compose(first, second, "out", ("a",), edge_space)
    if share_stage:
        p_h2 = next(s for s in comp["scratch"] if s["name"] == "p_h2")
        c_h2 = next(s for s in comp["scratch"] if s["name"] == "c_h2")
        same_h2 = p_h2["spec"] == c_h2["spec"]
        drop = ("p_h1", "c_h2") if same_h2 else ("p_h1",)
        remap = {"p_h1": "c_h1", "c_h2": "p_h2"} if same_h2 else {"p_h1": "c_h1"}
        comp = dict(
            comp,
            scratch=[s for s in comp["scratch"] if s["name"] not in drop],
            body=renamed(comp["body"], remap),
        )
    return comp


def mlp(
    rows,
    width,
    hidden,
    columns,
    machine,
    activation="gelu",
    residual=False,
    ring_depth=2,
    edge_space="stage",
    *,
    compact_mlp=True,
    mover_overlap=True,
    quant_backend="mover_quant",
):
    logical = rows
    rows = -(-rows // 4) * 4
    clusters = machine.count("MG")
    sec_nt = _cluster_n_tiles(rows, columns, clusters)
    sec_tn = columns // sec_nt
    unify_h2 = compact_mlp and clusters > 1 and ring_depth > 0 and hidden % sec_tn == 0
    ring_bytes_per_row = (
        clusters * sec_tn * 2
        if unify_h2
        else ring_depth * clusters * (512 if clusters == 1 else 1024)
    )
    row_bytes = hidden * 3 + ring_bytes_per_row
    if (
        edge_space == "stage"
        and rows * hidden * 2 > 450000
        and not residual
        and columns == width
        and ring_depth > 0
    ):
        max_cr = max(4, min(rows, (1030000 // row_bytes) // 4 * 4))
        n_chunks = -(-rows // max_cr)
        cr = -(-rows // (n_chunks * 4)) * 4
        chunks = []
        for r0 in range(0, logical, cr):
            n_rows = min(cr, logical - r0)
            chunk = _mlp_stage(
                cr,
                width,
                hidden,
                columns,
                machine,
                activation,
                False,
                ring_depth,
                "stage",
                out_offset=r0 * columns,
                out_rows=logical,
                share_stage=True,
                compact_mlp=compact_mlp,
                mover_overlap=mover_overlap,
                quant_backend=quant_backend,
            )
            for p in chunk["ports"]:
                if p["name"] in ("p_a", "c_out"):
                    p["spec"]["shape"] = [
                        logical,
                        width if p["name"] == "p_a" else columns,
                    ]
            if n_rows == cr:
                chunk["body"][0]["src"] = {
                    "buffer": "p_a",
                    "axes": [{"run": [r0, cr]}, "all"],
                }
            else:
                chunk["body"][0]["src"] = {
                    "buffer": "p_out",
                    "axes": ["all", {"run": [0, width]}],
                }

                def copy_in(x, out, r0=r0, n_rows=n_rows):
                    kt.fill_value(out[n_rows:cr, :width], 0)
                    kt.copy(
                        out,
                        x,
                        at=r0 * width * 2,
                        walk=((n_rows * width // 16, 32),),
                        dwalk=((n_rows, hidden * 2), (width // 16, 32)),
                    )

                g_in = (
                    kt.Kernel.build(
                        "mlp_chunk_in",
                        copy_in,
                        {"x": kt.In(logical, width), "out": kt.Out(cr, hidden)},
                    )
                    .trace({"x": (logical, width)}, machine=dsl_machine(machine))
                    .graph
                )
                chunk["body"] = (
                    renamed(g_in["body"], {"x": "p_a", "out": "p_out"}) + chunk["body"]
                )
                chunk = trim_out_tail(
                    chunk,
                    n_rows,
                    cr,
                    "c_out",
                    machine,
                    r0=r0,
                    total_rows=logical,
                )
            chunks.append(chunk)
        for c in chunks[:-1]:
            c["scratch"] = chunks[-1]["scratch"]
        if mover_overlap and unify_h2 and len(chunks) == 2:
            for ci, c in enumerate(chunks):
                q_idx = next(
                    i
                    for i, s in enumerate(c["body"])
                    if s.get("kind") == "mover"
                    and s.get("op") == "quant"
                    and s.get("src", {}).get("buffer") == "p_out"
                    and s.get("src", {}).get("axes", [None, None])[1] == "all"
                )
                fc1_stmt = c["body"][q_idx - 1]
                q_stmt = c["body"][q_idx]
                src_rows = q_stmt["src"]["axes"][0]
                dst_rows = q_stmt["dst"]["axes"][0]
                hi1 = fc1_stmt["hi"]
                mid_col = (hi1 - 1) * clusters * sec_tn
                if hi1 > 3 and mid_col > width:
                    fc1_a = dict(fc1_stmt, lo=0, hi=hi1 - 1)
                    fc1_b = renamed(
                        dict(fc1_stmt, lo=hi1 - 1, hi=hi1),
                        var_names([fc1_stmt], f"f1b{ci}_"),
                    )
                    q_mid = dict(
                        q_stmt,
                        dst={
                            "buffer": "c_h1",
                            "axes": [dst_rows, {"run": [width, mid_col - width]}],
                        },
                        src={
                            "buffer": "p_out",
                            "axes": [src_rows, {"run": [width, mid_col - width]}],
                        },
                    )
                    q_lo = dict(
                        q_stmt,
                        dst={
                            "buffer": "c_h1",
                            "axes": [dst_rows, {"run": [0, width]}],
                        },
                        src={
                            "buffer": "p_out",
                            "axes": [src_rows, {"run": [0, width]}],
                        },
                    )
                    q_hi = dict(
                        q_stmt,
                        dst={
                            "buffer": "c_h1",
                            "axes": [dst_rows, {"run": [mid_col, hidden - mid_col]}],
                        },
                        src={
                            "buffer": "p_out",
                            "axes": [src_rows, {"run": [mid_col, hidden - mid_col]}],
                        },
                    )
                    c["body"] = (
                        c["body"][: q_idx - 1]
                        + [fc1_a, q_mid, fc1_b, q_lo, q_hi]
                        + c["body"][q_idx + 1 :]
                    )
            fc2_0 = chunks[0]["body"][-1]
            if fc2_0.get("kind") == "for" and fc2_0.get("hi", 0) >= 2:
                hi2 = fc2_0["hi"]
                fc2_a = dict(fc2_0, lo=0, hi=hi2 - 1)
                fc2_b = renamed(
                    dict(fc2_0, lo=hi2 - 1, hi=hi2),
                    var_names([fc2_0], "f2b0_"),
                )
                chunks[0]["body"] = (
                    chunks[0]["body"][:-1] + [fc2_a] + chunks[1]["body"][:2] + [fc2_b]
                )
                chunks[1]["body"] = chunks[1]["body"][2:]
        seq = sequence(chunks, space="stage")
        seq = apply_quant_backend(seq, quant_backend, machine)
        return GraphKernel(seq)
    source = _mlp_stage(
        rows,
        width,
        hidden,
        columns,
        machine,
        activation,
        residual,
        ring_depth,
        edge_space,
        compact_mlp=compact_mlp,
        mover_overlap=mover_overlap,
        quant_backend=quant_backend,
    )
    if logical != rows:
        if ring_depth > 0 and not residual:
            source = trim_out_tail(source, logical, rows, "c_out", machine)
            source = logical_rows(
                source, logical, rows, ("p_a",), machine, pad_outputs=False
            )
        else:
            source = logical_rows(
                source,
                logical,
                rows,
                ("p_a", "c_in2") if residual else ("p_a",),
                machine,
            )
    source = apply_quant_backend(source, quant_backend, machine)
    return GraphKernel(source)


def rows_graph(op, rows, width, machine, eps=1e-5, *, balance_norm=True):
    if op == "layernorm" and width > 256:
        return wide_norm_graph(rows, width, machine, eps, balance_norm=balance_norm)
    operands = [Operand("full", pitch=width, rows=rows)]
    if op == "layernorm":
        operands += [Operand("col"), Operand("col")]
    return graph(
        "vector.rows_kernel",
        {
            "e": expr(R.expression(op, width, eps)),
            "operands": [asdict(o) for o in operands],
            "R": rows,
            "W": width,
            "out": asdict(Operand("full", pitch=width, rows=rows)),
        },
        machine,
    )


def wide_norm_graph(rows, width, machine, eps, *, balance_norm=True):
    """Exact three-stage wide LayerNorm composition with stage-local statistics."""
    full, stat, col = (
        Operand("full", pitch=width),
        Operand("row"),
        Operand("col"),
    )
    cores = machine.count("VC")
    x = X.Load(0)
    e1 = X.op("mul", X.Reduce("sum", x), 1.0 / width)
    k1, g1, _ = LR.wide_fold_kernel(e1, (full,), rows, width, stat, cores)
    gr1 = k1.trace(g1, machine=dsl_machine(machine)).graph
    d = X.op("sub", X.Load(0), X.Load(1))
    e2 = X.op(
        "rsqrt",
        X.op("add", X.op("mul", X.Reduce("sum", X.op("mul", d, d)), 1.0 / width), eps),
    )
    k2, g2, _ = LR.wide_fold_kernel(e2, (full, stat), rows, width, stat, cores)
    gr2 = k2.trace(g2, machine=dsl_machine(machine)).graph
    e3 = X.op(
        "add",
        X.op("mul", X.op("mul", d, X.Load(2)), X.Load(3)),
        X.Load(4),
    )
    gr3 = graph(
        "vector.rows_kernel",
        {
            "e": expr(e3),
            "operands": [asdict(o) for o in (full, stat, stat, col, col)],
            "R": rows,
            "W": width,
            "out": asdict(full),
        },
        machine,
    )
    stages = [
        (gr1, {"in0": "in0", "out": "mean"}),
        (gr2, {"in0": "in0", "in1": "mean", "out": "rstd"}),
        (
            gr3,
            {
                "in0": "in0",
                "in1": "mean",
                "in2": "rstd",
                "in3": "in1",
                "in4": "in2",
                "out": "out",
            },
        ),
    ]
    graphs = []
    for i, (source, mapping) in enumerate(stages):
        source = dict(
            source,
            scratch=[
                dict(s, spec=dict(s["spec"], space="stage")) for s in source["scratch"]
            ],
        )
        prefix = names(source, f"norm{i}_")
        prefix.update(mapping)
        graphs.append(renamed(source, prefix))
    ports = {}
    scratch = []
    for g in graphs:
        scratch.extend(g["scratch"])
        for p in g["ports"]:
            if p["name"] in ("mean", "rstd"):
                if p["role"] == "out":
                    scratch.append(
                        {"name": p["name"], "spec": dict(p["spec"], space="stage")}
                    )
            else:
                ports[p["name"]] = p
    body = [s for g in graphs for s in g["body"]]
    if balance_norm and cores > 1:
        single_idx = 0
        for st in body:
            if (
                st.get("kind") == "par"
                and st.get("hi") == 1
                and st.get("unit", {}).get("kind") == "vc"
            ):
                st["unit"] = {"kind": "vc", "index": single_idx % cores}
                single_idx += 1
    return dict(
        graphs[0],
        name="native_wide_norm",
        ports=[ports[n] for n in ("in0", "in1", "in2", "out")],
        scratch=scratch,
        body=body,
    )


def norm_linear(
    rows,
    width,
    columns,
    machine,
    eps=1e-5,
    activation="none",
    ring_depth=2,
    *,
    balance_norm=True,
    mover_overlap=True,
    quant_backend="mover_quant",
):
    capacity = -(-rows // 4) * 4
    edge = "stage" if rows * width * 2 <= 307200 else "dram"
    norm_cap = (
        -(-rows // 16) * 16
        if mover_overlap and edge == "stage" and ring_depth > 0 and width > 256
        else capacity
    )
    norm = rows_graph(
        "layernorm", norm_cap, width, machine, eps, balance_norm=balance_norm
    )
    product = linear_graph(
        capacity, width, columns, machine, activation, False, ring_depth
    )
    if norm_cap != capacity:
        product = json.loads(json.dumps(product))
        product["ports"][0]["spec"]["shape"] = [norm_cap, width]
        product["body"][0]["src"]["axes"] = [{"run": [0, capacity]}, "all"]
    source = compose(norm, product, "out", ("a",), edge)
    if capacity != rows:
        if ring_depth > 0 and edge == "stage":
            source = logical_rows(
                source,
                rows,
                norm_cap,
                ("p_in0",),
                machine,
                pad_outputs=False,
                classed=not mover_overlap,
            )
            source = dict(
                source,
                scratch=[s for s in source["scratch"] if s["name"] != "capacity_p_in0"],
                body=renamed(source["body"], {"capacity_p_in0": "p_out"}),
            )
            if norm_cap != capacity:

                def clear_pad(buf):
                    kt.fill_value(buf[rows:capacity, :], 0)

                clr_g = (
                    kt.Kernel.build(
                        "nl_clr", clear_pad, {"buf": kt.Out(norm_cap, width)}
                    )
                    .trace({"buf": (norm_cap, width)}, machine=dsl_machine(machine))
                    .graph
                )
                q_idx = next(
                    i
                    for i, s in enumerate(source["body"])
                    if s.get("kind") == "mover" and s.get("op") == "quant"
                )
                source["body"] = (
                    source["body"][:q_idx]
                    + renamed(clr_g["body"], {"buf": "p_out"})
                    + source["body"][q_idx:]
                )
            source = trim_out_tail(source, rows, capacity, "c_out", machine)
        else:
            source = logical_rows(
                source, rows, capacity, ("p_in0",), machine, classed=not mover_overlap
            )
    source = apply_quant_backend(source, quant_backend, machine)
    return GraphKernel(source)


def qkv_projection(
    batch, tokens, width, machine, *, eps=None, ring_depth=2, heads=None, dim=64
):
    """QKV linear (or norm+linear) writing planar Q, K and transposed V directly."""
    rows = batch * tokens
    heads = width // dim if heads is None else heads
    proj = (
        linear(rows, width, 3 * width, machine, "none", False, ring_depth).graph
        if eps is None
        else norm_linear(rows, width, 3 * width, machine, eps, "none", ring_depth).graph
    )
    split = (
        Y.split_kernel(batch, tokens, heads, dim, machine.count("VC"))
        .trace({"x": (rows, 3 * width), "mask": (2, 128)}, machine=dsl_machine(machine))
        .graph
    )
    if rows * width * 8 <= 600000:
        split = dict(
            split,
            scratch=[
                dict(s, spec=dict(s["spec"], space="stage"))
                for s in split["scratch"]
                if s["name"] != "h4"
            ],
            body=renamed(split["body"], {"h4": "h1"}),
        )
    out_port = next(p["name"] for p in proj["ports"] if p["role"] == "out")
    return GraphKernel(compose(proj, split, out_port, ("x",), "stage"))
