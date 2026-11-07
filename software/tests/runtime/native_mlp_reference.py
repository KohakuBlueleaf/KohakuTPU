"""Independent materialized native schedules for byte and timing comparisons.

Test support constructs references without eager descriptors or production
composition helpers. Runtime witnesses own execution and timing assertions;
this module contains no collected test or simulator invocation.
"""

import json
from dataclasses import asdict, replace
from typing import Any

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import transpose as T
from kohakutpu.application.kernels.native import attention as A
from kohakutpu.application.kernels.native import linear as L
from kohakutpu.application.kernels.native import rows as R
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.plan.emit import expr
from kohakutpu.application.runtime import generated as G
from kohakutpu.application.runtime import launch_reductions as LR
from kohakutpu.language import dsl as kt
from kohakutpu.language.examples.dsl.cluster.sweep import core_blocks, sweep
from kohakutpu.stack import rs
from kohakutpu.stack.kernel import dsl_machine


def statements(body):
    for statement in body:
        yield statement
        yield from statements(statement.get("body", ()))


def names(graph, prefix):
    result = {
        p["name"]: prefix + p["name"] for p in (*graph["ports"], *graph["scratch"])
    }
    for statement in statements(graph["body"]):
        for field in ("var", "target"):
            if field in statement:
                result[statement[field]] = prefix + statement[field]
    return result


def renamed(value, mapping, field="") -> Any:
    if isinstance(value, dict):
        return {k: renamed(v, mapping, k) for k, v in value.items()}
    if isinstance(value, list):
        return [renamed(v, mapping, field) for v in value]
    if isinstance(value, str) and field in (
        "buffer",
        "var",
        "target",
        "value",
        "bcast",
        "src",
        "name",
    ):
        return mapping.get(value, value)
    return value


def compose(left, right, output, inputs, space="stage") -> dict:
    """The op builder owns the private, identically typed producer/consumer edge."""
    lm, rm = names(left, "p_"), names(right, "c_")
    internal = lm[output]
    rm.update(dict.fromkeys(inputs, internal))
    a, b = renamed(left, lm), renamed(right, rm)
    intermediate = next(p for p in a["ports"] if p["name"] == internal)
    return dict(
        a,
        name="native_composition",
        consts={},
        ports=[p for p in (*a["ports"], *b["ports"]) if p["name"] != internal],
        scratch=[
            *a["scratch"],
            {"name": internal, "spec": dict(intermediate["spec"], space=space)},
            *b["scratch"],
        ],
        body=a["body"] + b["body"],
    )


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


def mlp_stage(
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
    vc_ep, store_stmt, wc = {}, {}, hidden
    if piped:
        vc_ep = first["body"][1]["body"][1]
        store_stmt = next(
            s for s in statements(vc_ep["body"]) if s.get("kind") == "store"
        )
        wc = store_stmt["dst"]["axes"][1]["run"][1]
    if activation == "gelu":
        act_args = {
            "e": expr(X.gelu(X.Load(0))),
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


def var_names(body, prefix):
    result = {}
    for statement in statements(body):
        for field in ("var", "target"):
            if field in statement:
                result[statement[field]] = prefix + statement[field]
    return result


def _spec_bytes(spec):
    items = 1
    for d in spec["shape"]:
        items *= d
    return items * (1 if str(spec["dtype"]).startswith("mx7") else 2)


def sequence(graphs, space="stage"):
    """Execute chunk graphs sequentially while reusing scratch buffers."""
    first = graphs[0]
    shared = {s["name"] for s in first["scratch"] if _spec_bytes(s["spec"]) >= 256}
    scratch: list[dict] = [
        dict(s, spec=dict(s["spec"], space=space))
        for s in first["scratch"]
        if s["name"] in shared
    ]
    body = []
    for i, g in enumerate(graphs):
        mapping = var_names(g["body"], f"s{i}_")
        for s in g["scratch"]:
            if s["name"] not in shared:
                local_name = f"s{i}_{s['name']}"
                mapping[s["name"]] = local_name
                scratch.append(
                    {"name": local_name, "spec": dict(s["spec"], space=space)}
                )
        body.extend(renamed(g["body"], mapping))
    return dict(
        first,
        name="native_sequence",
        consts={},
        scratch=scratch,
        body=body,
    )


def materialized_chunks(rows, chunk_rows, width, hidden, columns, machine):
    """Full row chunks with FP16 GELU and no cross-stage or cross-chunk motion."""
    chunks = []
    for first in range(0, rows, chunk_rows):
        chunk = mlp_stage(
            chunk_rows,
            width,
            hidden,
            columns,
            machine,
            "none",
            False,
            2,
            "stage",
            out_offset=first * columns,
            out_rows=rows,
            share_stage=True,
        )
        full = Operand("full", pitch=hidden, rows=chunk_rows)
        activation = graph(
            "vector.rows_kernel",
            {
                "e": expr(X.gelu(X.Load(0))),
                "expr_cse": True,
                "operands": [asdict(full)],
                "R": chunk_rows,
                "W": hidden,
                "out": asdict(full),
            },
            machine,
        )
        mapping = names(activation, "rounded_")
        mapping.update(in0="p_out", out="p_out")
        activation = renamed(activation, mapping)
        middle_quant = next(
            i
            for i, statement in enumerate(chunk["body"])
            if statement.get("kind") == "mover"
            and statement.get("op") == "quant"
            and statement["src"]["buffer"] == "p_out"
        )
        chunk["body"][middle_quant:middle_quant] = activation["body"]
        chunk["scratch"].extend(activation["scratch"])
        for port in chunk["ports"]:
            if port["name"] == "p_a":
                port["spec"]["shape"] = [rows, width]
        chunk["body"][0]["src"] = {
            "buffer": "p_a",
            "axes": [{"run": [first, chunk_rows]}, "all"],
        }
        chunks.append(chunk)
    result = sequence(chunks, space="stage")
    result["name"] = "materialized_mlp_chunks"
    return result


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


def apply_quant_backend(graph, quant_backend, machine):
    """Switch top-level quant ops to vector_mx7 with deferred stage status when requested."""
    if quant_backend != "vector_mx7":
        return graph
    vc = max(1, machine.count("VC"))
    rec_elems = 64 * vc
    quants = [
        s for s in graph["body"] if s.get("kind") == "mover" and s.get("op") == "quant"
    ]
    if not quants:
        return graph
    sched = [
        ("finite_batch", 4),
        ("overlap", 1),
        ("pipeline", 1),
        ("zero_classifier", 1),
        ("stream_axes", 1),
        ("fill_overlap", 1),
        ("zero_reuse", 1),
    ]
    for idx, q in enumerate(quants):
        existing = {k for k, _ in q.get("kw", ())}
        kw = [[k, v] for k, v in q.get("kw", ()) if k not in ("backend", "status")]
        kw.append(["backend", {"str": "vector_mx7"}])
        kw.append(
            [
                "status",
                {
                    "view": {
                        "buffer": "q_stat",
                        "axes": [{"run": [idx * rec_elems, rec_elems]}],
                    }
                },
            ]
        )
        for sk, sv in sched:
            if sk not in existing:
                kw.append([sk, {"int": sv}])
        q["kw"] = kw
    graph["scratch"].append(
        {
            "name": "q_stat",
            "spec": {
                "shape": [len(quants) * rec_elems],
                "dtype": "f16",
                "layout": [],
                "space": "stage",
            },
        }
    )
    graph["body"].append(
        {
            "kind": "mover",
            "op": "quant_check",
            "dst": {"buffer": "q_stat", "axes": ["all"]},
            "src": None,
            "flags": [],
            "kw": [],
            "where": "<native>",
        }
    )
    return graph


def trim_out_tail(graph, rows, capacity, out_name, machine, *, r0=0, total_rows=None):
    """Write full bands directly to out_name and stage only the final overrun band."""
    total_rows = rows if total_rows is None else total_rows
    k_loop = [s for s in graph["body"] if s.get("kind") == "for" and s.get("pipe")][-1]
    vc_par = k_loop["body"][1]

    def stream(par):
        holder = par
        while holder["body"][-1].get("kind") == "when":
            holder = holder["body"][-1]
        return holder, holder["body"][-1]

    _, t_loop = stream(vc_par)
    height = t_loop["body"][-1]["dst"]["axes"][0]["run"][1]
    n_main = rows // height
    main_rows = n_main * height
    tail_rows = capacity - main_rows
    rem_rows = rows - main_rows
    columns = next(
        p["spec"]["shape"][1] for p in graph["ports"] if p["name"] == out_name
    )
    vc_main = json.loads(json.dumps(vc_par))
    _, t_main = stream(vc_main)
    t_main["hi"] = n_main
    vc_tail = renamed(vc_par, var_names([vc_par], "tl_"))
    tail_holder, t_tail = stream(vc_tail)
    t_tail = dict(t_tail, lo=n_main, hi=n_main + 1)
    orig_run = t_tail["body"][-1]["dst"]["axes"][0]["run"]
    new_ax0 = {
        "run": [
            {"const": -n_main * height, "terms": orig_run[0]["terms"][-1:]},
            height,
        ]
    }
    store_tail = dict(
        t_tail["body"][-1],
        dst=dict(
            t_tail["body"][-1]["dst"],
            buffer="tail_out",
            axes=[new_ax0, t_tail["body"][-1]["dst"]["axes"][1]],
        ),
    )
    t_tail = dict(t_tail, body=[*t_tail["body"][:-1], store_tail])
    tail_holder["body"][-1] = t_tail
    k_loop["body"] = [k_loop["body"][0], vc_main, vc_tail, *k_loop["body"][2:]]
    for p in graph["ports"]:
        if p["name"] == out_name:
            p["spec"]["shape"] = [total_rows, columns]
    graph["scratch"].append(
        {
            "name": "tail_out",
            "spec": {
                "shape": [tail_rows, columns],
                "dtype": "f16",
                "layout": [],
                "space": "stage",
            },
        }
    )

    def copy_tail(x, out):
        kt.copy(
            out,
            x,
            dat=(r0 + main_rows) * columns * 2,
            walk=((rem_rows * columns // 16, 32),),
            dwalk=((rem_rows * columns // 16, 32),),
        )

    cp = (
        kt.Kernel.build(
            "tail_cp",
            copy_tail,
            {"x": kt.In(tail_rows, columns), "out": kt.Out(total_rows, columns)},
        )
        .trace({"x": (tail_rows, columns)}, machine=dsl_machine(machine))
        .graph
    )
    graph["body"].extend(renamed(cp["body"], {"x": "tail_out", "out": out_name}))
    return graph


def logical_rows(
    graph,
    rows,
    capacity,
    inputs,
    machine,
    *,
    pad_outputs=True,
    classed=True,
):
    """Wrap capacity-shaped row operands with zero-pad/trim, keeping port order."""
    outputs = (
        [p["name"] for p in graph["ports"] if p["role"] == "out"] if pad_outputs else []
    )
    affected = set(inputs) | set(outputs)
    mapping = {n: "capacity_" + n for n in affected}
    result = renamed(graph, mapping)
    result["ports"] = []
    used_stage = sum(
        _spec_bytes(s["spec"])
        for s in result["scratch"]
        if s["spec"]["space"] == "stage"
    )
    before, after = [], []
    for port in graph["ports"]:
        name = port["name"]
        if name not in affected:
            result["ports"].append(port)
            continue
        width = port["spec"]["shape"][1]
        result["ports"].append(dict(port, spec=dict(port["spec"], shape=[rows, width])))
        nbytes = capacity * width * 2
        if used_stage + nbytes <= 983040:
            space = "stage"
            used_stage += nbytes
        else:
            space = "dram"
        result["scratch"].append(
            {"name": mapping[name], "spec": dict(port["spec"], space=space)}
        )
        is_input = name in inputs
        shape_in, shape_out = (
            ((rows, width), (capacity, width))
            if is_input
            else ((capacity, width), (rows, width))
        )

        def body(x, out, is_input=is_input, width=width):
            if is_input:
                if classed:
                    kt.fill_value(out, 0)
                elif capacity > rows:
                    kt.fill_value(out[rows:capacity, :], 0)
            kt.copy(
                out,
                x,
                walk=((rows * width // 16, 32),),
                dwalk=((rows * width // 16, 32),),
                classed=classed,
                node_loop=classed,
            )

        copy = (
            kt.Kernel.build(
                "row_boundary", body, {"x": kt.In(*shape_in), "out": kt.Out(*shape_out)}
            )
            .trace({"x": shape_in}, machine=dsl_machine(machine))
            .graph
        )
        operands = (
            {"x": name, "out": mapping[name]}
            if is_input
            else {"x": mapping[name], "out": name}
        )
        (before if is_input else after).extend(renamed(copy["body"], operands))
    result["body"] = before + result["body"] + after
    return result


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


def norm_linear_graph(
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
    return source


def linear_projection_graph(
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
    return source


def split_projection_kernel(
    batch,
    tokens,
    heads,
    dim,
    cores,
    *,
    query_pad=None,
    key_pad=None,
    batch_offset=0,
    total_batch=None,
    head_offset=0,
    total_heads=None,
):
    """Token-major logical rows to zero-padded planar Q/K and transposed V."""
    if dim % 16:
        raise ValueError("projection head width must contain whole 16-element words")
    total_batch = batch if total_batch is None else total_batch
    total_heads = heads if total_heads is None else total_heads
    padded = -(-tokens // 16) * 16 if query_pad is None else query_pad
    k_padded = (-(-tokens // 16) * 16) if key_pad is None else key_pad
    width = total_heads * dim
    planes = batch * heads
    words = planes * k_padded * dim // 16
    ports = {
        "x": kt.In(total_batch * tokens, 3 * width),
        "mask": kt.In(2, 128),
        "q": kt.Out(planes * padded, dim),
        "k": kt.Out(planes * k_padded, dim),
        "vt": kt.Out(planes * dim, k_padded),
    }

    def body(x, mask, q, k, vt):
        v = kt.buffer((words, 16), "f16")
        for i, (dst, rows) in enumerate(((q, padded), (k, k_padded), (v, k_padded))):
            kt.fill_value(dst, 0)
            kt.copy(
                dst,
                x,
                at=(batch_offset * tokens * 3 * width + i * width + head_offset * dim)
                * 2,
                walk=(
                    (batch, tokens * 3 * width * 2),
                    (heads, dim * 2),
                    (tokens, 3 * width * 2),
                    (dim // 16, 32),
                ),
                dwalk=((planes, rows * dim * 2), (tokens * dim // 16, 32)),
            )
        T.emit(v, mask, 0, vt, planes, k_padded, dim, cores)

    return kt.Kernel.build("native_split_projection", body, ports)


def qkv_graph(batch, tokens, width, machine, *, eps=None, heads, dim):
    """Frozen projection, extraction, and transpose with shared stage scratch."""
    rows = batch * tokens
    projection = (
        linear_projection_graph(rows, width, 3 * width, machine)
        if eps is None
        else norm_linear_graph(rows, width, 3 * width, machine, eps)
    )
    split = (
        split_projection_kernel(batch, tokens, heads, dim, machine.count("VC"))
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
    output = next(p["name"] for p in projection["ports"] if p["role"] == "out")
    return compose(projection, split, output, ("x",), "stage")


def referencemerge_kernel(
    batch,
    tokens,
    heads,
    dim,
    *,
    query_pad=None,
    batch_offset=0,
    total_batch=None,
    head_offset=0,
    total_heads=None,
):
    total_batch = batch if total_batch is None else total_batch
    total_heads = heads if total_heads is None else total_heads
    full_width = total_heads * dim
    padded = -(-tokens // 16) * 16 if query_pad is None else query_pad
    words = batch * tokens * heads * dim // 16
    ports = {
        "x": kt.In(batch * heads * padded, dim),
        "out": kt.Out(total_batch * tokens, full_width),
    }

    def body(x, out):
        dwalk = (
            ((words, 32),)
            if heads == total_heads
            else ((batch * tokens, full_width * 2), (heads * dim // 16, 32))
        )
        kt.copy(
            out,
            x,
            dat=(batch_offset * tokens * full_width + head_offset * dim) * 2,
            walk=(
                (batch, heads * padded * dim * 2),
                (tokens, dim * 2),
                (heads, padded * dim * 2),
                (dim // 16, 32),
            ),
            dwalk=dwalk,
        )

    return kt.Kernel.build("native_merge_heads", body, ports)


def reference_chunk_attention(
    batch,
    tokens,
    heads,
    dim,
    cores,
    *,
    query_blocked=False,
    block_rows=None,
    query_tile=16,
    query_pad=None,
    batch_offset=0,
    total_batch=None,
    head_offset=0,
    total_heads=None,
):
    padded = -(-tokens // 16) * 16 if query_pad is None else query_pad
    sk = -(-padded // 64) * 64
    split = split_projection_kernel(
        batch,
        tokens,
        heads,
        dim,
        cores,
        query_pad=padded,
        key_pad=sk,
        batch_offset=batch_offset,
        total_batch=total_batch,
        head_offset=head_offset,
        total_heads=total_heads,
    )
    attn = A.kernel(
        batch * heads,
        padded,
        sk,
        dim,
        cores,
        query_tile=query_tile,
        valid_keys=tokens,
        query_blocked=query_blocked,
        block_rows=block_rows,
    )
    merge = referencemerge_kernel(
        batch,
        tokens,
        heads,
        dim,
        query_pad=padded,
        batch_offset=batch_offset,
        total_batch=total_batch,
        head_offset=head_offset,
        total_heads=total_heads,
    )
    ports = {
        "x": split.ports["x"],
        "mask": split.ports["mask"],
        "out": merge.ports["out"],
    }
    if tokens % 16:
        ports["tail"] = kt.In(16)

    def body(x, mask, out, tail=None):
        q = kt.buffer((batch * heads * padded, dim), "f16")
        k = kt.buffer((batch * heads * sk, dim), "f16")
        vt = kt.buffer((batch * heads * dim, sk), "f16")
        split.fn(x=x, mask=mask, q=q, k=k, vt=vt)
        attn.fn(q=q, k=k, vt=vt, out=q, tail=tail)
        merge.fn(x=q, out=out)

    return kt.Kernel.build("native_projection_attention", body, ports)


def projection_attention_graph(
    batch,
    tokens,
    heads,
    dim,
    cores,
    machine,
    *,
    query_blocked=False,
    block_rows=None,
    quant_backend="mover_quant",
):
    """Frozen general stage policy and materialized schedule, with legacy reuse."""
    given = {"x": (batch * tokens, 3 * heads * dim), "mask": (2, 128)}
    if tokens % 16:
        given["tail"] = (16,)
    q_pad = -(-tokens // (cores * 16)) * cores * 16
    sk = -(-q_pad // 64) * 64
    q_tile = min(q_pad // cores, 256)
    eff_block = (q_pad if query_blocked else None) if block_rows is None else block_rows
    plane_bytes = q_pad * dim * 3 + sk * dim * 12
    attn_bytes = q_pad * (sk * 3 + 128) + q_pad * dim * 2 + sk * 16 + q_pad * 20
    unblocked_cap = 950000 // (plane_bytes + attn_bytes)
    if unblocked_cap >= heads:
        eff_qb = False
        max_b = max(1, unblocked_cap // heads)
        chunk_b = max(b for b in range(1, min(batch, max_b) + 1) if batch % b == 0)
        chunk_h = heads
    else:
        eff_qb = query_blocked
        max_h = max(1, (1040000 - attn_bytes) // plane_bytes)
        chunk_b = 1
        chunk_h = (
            next(h for h in range(min(heads, max_h), 0, -1) if heads % h == 0)
            if heads > max_h
            else heads
        )
    chunks = []
    for b0 in range(0, batch, chunk_b):
        for h0 in range(0, heads, chunk_h):
            k = reference_chunk_attention(
                chunk_b,
                tokens,
                chunk_h,
                dim,
                cores,
                query_blocked=eff_qb,
                block_rows=eff_block,
                query_tile=q_tile,
                query_pad=q_pad,
                batch_offset=b0,
                total_batch=batch,
                head_offset=h0,
                total_heads=heads,
            )
            traced = k.trace(given, machine=dsl_machine(machine)).graph
            traced = dict(
                traced,
                scratch=[s for s in traced["scratch"] if s["name"] != "h7"],
                body=renamed(traced["body"], {"h7": "h4"}),
            )
            chunks.append(traced)
    seq = sequence(chunks, space="stage")
    seq = apply_quant_backend(seq, quant_backend, machine)
    return seq


def frozen_fast_chunk_attention(
    batch,
    tokens,
    heads,
    dim,
    cores,
    *,
    q_pad,
    sk,
    q_tile,
    key_tile,
    step_heads,
    batch_offset=0,
    total_batch=None,
    head_offset=0,
    total_heads=None,
    init_valid=True,
    mover_overlap=True,
):
    total_batch = batch if total_batch is None else total_batch
    total_heads = heads if total_heads is None else total_heads
    width = total_heads * dim
    planes = batch * heads
    words = planes * sk * dim // 16
    srows = step_heads * q_pad
    ports = {
        "x": kt.In(total_batch * tokens, 3 * width),
        "mask": kt.In(2, 128),
        "out": kt.Out(total_batch * tokens, width),
    }
    if tokens % 16:
        ports["tail"] = kt.In(16)

    def body(x, mask, out, tail=None):
        q = kt.buffer((planes * sk, dim), "f16")
        v = kt.buffer((planes * dim, sk), "f16")
        s = kt.buffer((words // 8, 128), "f16")
        y_out = kt.buffer((srows, dim), "f16") if mover_overlap else q
        qp = kt.buffer((planes * q_pad, dim), "mx7a", gm=q_tile // 4, nk=2)
        kp = kt.buffer((planes * sk, dim), "mx7b", gn=sk // 4, nk=2)
        vp = kt.buffer((planes * dim, sk), "mx7b", gn=dim // 4, nk=2)
        mask_row = kt.buffer((1, key_tile), "f16")
        valid = kt.buffer((4, key_tile), "f16", tile=(1, key_tile // 4))
        scores = kt.buffer((srows, sk), "f16", tile=(q_tile // 4, sk // 4))
        probs = kt.buffer((srows, sk), "mx7a", gm=q_tile // 4, nk=2)
        sums = kt.buffer((4, srows), "f16")
        rowmax = kt.buffer((srows,), "f16")
        rowsum = kt.buffer((srows,), "f16")
        product = kt.buffer((srows, dim), "f16", tile=(q_tile // 4, dim // 4))

        P, rg, cw, sub = 2 * dim, sk // 4, dim // 16, dim // 4
        trips = (words // 8) // T.ROWS
        base_at = batch_offset * tokens * 3 * width + head_offset * dim
        output_at = batch_offset * tokens * width + head_offset * dim

        def emit_v_in():
            kt.fill_value(v, 0)
            kt.copy(
                v,
                x,
                at=(base_at + 2 * width) * 2,
                walk=(
                    (batch, tokens * 3 * width * 2),
                    (heads, dim * 2),
                    (tokens, 3 * width * 2),
                    (dim // 16, 32),
                ),
                dwalk=((planes, sk * dim * 2), (tokens * dim // 16, 32)),
            )
            kt.gt4(
                s,
                v,
                walk=[(planes, sk * P), (rg, 4 * P), (cw, 32), (4, P)],
                dwalk=[(planes, rg * cw * 128), (rg, cw * 128), (cw, 128)],
            )

        def emit_v_transpose():
            for c in kt.par(cores, on=kt.vc):
                m1 = kt.unpack(kt.load(mask[0:1, :]))
                m2 = kt.unpack(kt.load(mask[1:2, :]))
                for k_it in kt.range(trips // cores):
                    r = (c * (trips // cores) + k_it) * T.ROWS
                    val = kt.unpack(kt.load(s[r : r + T.ROWS, :]))
                    a = kt.where(m1, kt.xlane(val, 5), val)
                    kt.store(s[r : r + T.ROWS, :], kt.where(m2, kt.xlane(a, 10), a))

        def emit_v_out():
            kt.gt4(
                q,
                s,
                walk=[
                    (planes, rg * sub * 32),
                    (sub, 32),
                    (sk // 16, 4 * sub * 32),
                    (4, sub * 32),
                ],
                dwalk=[
                    (planes, sub * (sk // 16) * 128),
                    (sub, (sk // 16) * 128),
                    (sk // 16, 128),
                ],
            )
            kt.copy(
                v,
                q,
                walk=[
                    (planes, sub * (sk // 16) * 128),
                    (sub, (sk // 16) * 128),
                    (4, 32),
                    (sk // 16, 128),
                ],
                dwalk=[(words, 32)],
            )
            for b_v in range(planes):
                kt.quant(
                    vp[b_v * dim : (b_v + 1) * dim, :],
                    v[b_v * dim : (b_v + 1) * dim, :],
                )

        def emit_qk_in():
            kt.fill_value(q[: planes * q_pad, :], 0)
            kt.copy(
                q,
                x,
                at=base_at * 2,
                walk=(
                    (batch, tokens * 3 * width * 2),
                    (heads, dim * 2),
                    (tokens, 3 * width * 2),
                    (dim // 16, 32),
                ),
                dwalk=((planes, q_pad * dim * 2), (tokens * dim // 16, 32)),
            )
            kt.quant(qp, q[: planes * q_pad, :])
            kt.fill_value(q, 0)
            kt.copy(
                q,
                x,
                at=(base_at + width) * 2,
                walk=(
                    (batch, tokens * 3 * width * 2),
                    (heads, dim * 2),
                    (tokens, 3 * width * 2),
                    (dim // 16, 32),
                ),
                dwalk=((planes, sk * dim * 2), (tokens * dim // 16, 32)),
            )
            kt.quant(kp, q)
            if init_valid:
                rem_keys = max(0, min(key_tile, tokens - key_tile))
                full = (rem_keys // 16) * 16
                kt.fill_value(mask_row, 0)
                if full:
                    kt.fill_value(mask_row[:, :full], 0x3C00, ew=16)
                if tokens % 16:
                    kt.copy(
                        mask_row,
                        tail,
                        dat=full * 2,
                        walk=((1, 32),),
                        dwalk=((1, 32),),
                    )
                kt.gt4(
                    valid,
                    mask_row,
                    walk=((key_tile // 16, 32), (4, 0)),
                    dwalk=((key_tile // 16, 128),),
                )

        emit_v_in()
        emit_v_transpose()
        if mover_overlap:
            emit_qk_in()
        else:
            emit_v_out()
            emit_qk_in()

        scale = dim**-0.5 * kt.LOG2E
        for hp in range(planes // step_heads):
            r_q = hp * srows
            r_k = hp * step_heads * sk
            r_v = hp * step_heads * dim
            sweep(
                A.RowWindow(qp, r_q, srows),
                A.RowWindow(kp, r_k, step_heads * sk),
                scores,
                q_tile,
                sk,
                groups=step_heads,
            )
            if mover_overlap and hp == 0:
                emit_v_out()
            for r0, _, rper in core_blocks(srows, key_tile, key_tile, 16, q_tile):
                for t in kt.range(rper // 16):
                    r = r0 + t * 16
                    x0 = kt.load(scores[r : r + 16, 0:key_tile]) * scale
                    kt.store(rowmax[r : r + 16], kt.rmax(x0))
            for r0, _, rper in core_blocks(srows, key_tile, key_tile, 16, q_tile):
                vmask = kt.unpack(kt.load(valid[0:4, 0:key_tile]))
                for t in kt.range(rper // 16):
                    r = r0 + t * 16
                    x1 = kt.load(scores[r : r + 16, key_tile : 2 * key_tile]) * scale
                    x1 = kt.where(vmask, x1, float("-inf"))
                    kt.store(
                        rowmax[r : r + 16],
                        kt.maximum(kt.load(rowmax[r : r + 16]), kt.rmax(x1)),
                    )
            for r0, _, rper in core_blocks(srows, key_tile, key_tile, 16, q_tile):
                for t in kt.range(rper // 16):
                    r = r0 + t * 16
                    rm = kt.load(rowmax[r : r + 16])
                    x0 = kt.load(scores[r : r + 16, 0:key_tile]) * scale
                    p0 = kt.exp2(x0 - rm)
                    kt.store(probs[r : r + 16, 0:key_tile], p0)
                    kt.store(sums[0, r : r + 16], kt.rsum(p0[:, 0:64]))
                    kt.store(sums[1, r : r + 16], kt.rsum(p0[:, 64:128]))
            for r0, _, rper in core_blocks(srows, key_tile, key_tile, 16, q_tile):
                vmask = kt.unpack(kt.load(valid[0:4, 0:key_tile]))
                for t in kt.range(rper // 16):
                    r = r0 + t * 16
                    rm = kt.load(rowmax[r : r + 16])
                    x1 = kt.load(scores[r : r + 16, key_tile : 2 * key_tile]) * scale
                    x1 = kt.where(vmask, x1, float("-inf"))
                    p1 = kt.exp2(x1 - rm)
                    kt.store(probs[r : r + 16, key_tile : 2 * key_tile], p1)
                    kt.store(sums[2, r : r + 16], kt.rsum(p1[:, 0:64]))
                    kt.store(sums[3, r : r + 16], kt.rsum(p1[:, 64:128]))
            sweep(
                probs,
                A.RowWindow(vp, r_v, step_heads * dim),
                product,
                q_tile,
                dim,
                groups=step_heads,
            )
            for r0, _, rper in core_blocks(srows, 32, 32, 16, q_tile):
                for t in kt.range(rper // 16):
                    r = r0 + t * 16
                    z = kt.unpack(kt.load(sums[0, r : r + 16]))
                    for b in range(1, 4):
                        z = z + kt.load(sums[b, r : r + 16])
                    kt.store(rowsum[r : r + 16], z)
            for r0, _, rper in core_blocks(srows, dim, dim, 16, q_tile):
                for t in kt.range(rper // 16):
                    r = r0 + t * 16
                    value = kt.load(product[r : r + 16, :])
                    dst_slice = (
                        y_out[r : r + 16, :]
                        if mover_overlap
                        else q[r_q + r : r_q + r + 16, :]
                    )
                    kt.store(
                        dst_slice,
                        value * kt.recip(kt.load(rowsum[r : r + 16])),
                    )
            if mover_overlap:
                kt.copy(
                    out,
                    y_out,
                    dat=(output_at + hp * step_heads * dim) * 2,
                    walk=(
                        (batch, step_heads * q_pad * dim * 2),
                        (tokens, dim * 2),
                        (step_heads, q_pad * dim * 2),
                        (dim // 16, 32),
                    ),
                    dwalk=(
                        (batch * tokens, width * 2),
                        (step_heads * dim // 16, 32),
                    ),
                )

        if not mover_overlap:
            dwalk = (
                ((batch * tokens * heads * dim // 16, 32),)
                if heads == total_heads
                else ((batch * tokens, width * 2), (heads * dim // 16, 32))
            )
            kt.copy(
                out,
                q,
                dat=output_at * 2,
                walk=(
                    (batch, heads * q_pad * dim * 2),
                    (tokens, dim * 2),
                    (heads, q_pad * dim * 2),
                    (dim // 16, 32),
                ),
                dwalk=dwalk,
            )

    return kt.Kernel.build("native_fast_projection_attention", body, ports)


def fast_projection_attention_graph(
    tokens, heads, dim, cores, machine, *, batch=1, mover_overlap=True
):
    """Materialized fast chunks with global batch/head output origins.

    Each chunk retains its arithmetic and internal transfer/compute order.
    The trace has one sysnode; the physical machine supplies the unit facts.
    """
    padded = -(-tokens // 16) * 16
    chunk = 4 if cores == 2 and heads % 4 == 0 else min(cores, heads)
    given = {"x": (batch * tokens, 3 * heads * dim), "mask": (2, 128)}
    if tokens % 16:
        given["tail"] = (16,)
    local_machine = replace(dsl_machine(machine), nodes=1)
    graphs = []
    for b0 in range(batch):
        for h0 in range(0, heads, chunk):
            kernel = frozen_fast_chunk_attention(
                1,
                tokens,
                chunk,
                dim,
                cores,
                q_pad=padded,
                sk=256,
                q_tile=padded,
                key_tile=128,
                step_heads=min(cores, heads),
                mover_overlap=mover_overlap,
                batch_offset=b0,
                total_batch=batch,
                head_offset=h0,
                total_heads=heads,
                init_valid=b0 == 0 and h0 == 0,
            )
            graphs.append(kernel.trace(given, machine=local_machine).graph)
    return sequence(graphs, space="stage")


def frozen_unfold_kernel(
    batch,
    height,
    width,
    cin_pad,
    kh,
    kw,
    stride=1,
    padding=0,
    *,
    batch_offset=0,
    total_batch=None,
):
    """Unfold [B, H, W, Cin_pad] (Cin_pad a multiple of 16) into [rows_pad, K_pad]."""
    if cin_pad % 16:
        raise ValueError("channel lane axis must be a multiple of 16")
    total_batch = batch if total_batch is None else total_batch
    hp, wp = height + 2 * padding, width + 2 * padding
    oh, ow = (hp - kh) // stride + 1, (wp - kw) // stride + 1
    rows = batch * oh * ow
    rows_pad = -(-rows // 4) * 4
    k_logical = kh * kw * cin_pad
    k_pad = -(-k_logical // 32) * 32
    words = cin_pad // 16
    ports = {
        "x": kt.In(total_batch * height * width, cin_pad),
        "out": kt.Out(rows_pad, k_pad),
    }
    src_at = batch_offset * height * width * cin_pad * 2

    def body(x, out):
        src = x
        if padding:
            src = kt.buffer((batch * hp * wp, cin_pad), "f16")
            kt.fill_value(src, 0)
            kt.copy(
                src,
                x,
                at=src_at,
                walk=((batch * height * width * words, 32),),
                dwalk=(
                    (batch, hp * wp * cin_pad * 2),
                    (height, wp * cin_pad * 2),
                    (width * words, 32),
                ),
                dat=(padding * wp + padding) * cin_pad * 2,
            )
        kt.fill_value(out, 0)
        kt.copy(
            out,
            src,
            at=0 if padding else src_at,
            walk=(
                (batch, hp * wp * cin_pad * 2),
                (oh, stride * wp * cin_pad * 2),
                (ow, stride * cin_pad * 2),
                (kh, wp * cin_pad * 2),
                (kw * words, 32),
            ),
            dwalk=((rows, k_pad * 2), (kh * kw * words, 32)),
        )

    return kt.Kernel.build("native_conv_unfold", body, ports)


def convolution_graph(
    batch,
    height,
    width,
    cin_pad,
    cout_pad,
    kh,
    kw,
    machine,
    *,
    stride=1,
    padding=0,
    activation="none",
    residual=False,
    ring_depth=2,
    edge_space="stage",
    nodes=None,
):
    """Fused unfold + in-kernel quant + ring contraction + epilogue in one package."""
    if cout_pad % 32:
        raise ValueError("padded output channels must be a multiple of 32")
    hp, wp = height + 2 * padding, width + 2 * padding
    oh, ow = (hp - kh) // stride + 1, (wp - kw) // stride + 1
    rows = batch * oh * ow
    rows_pad = -(-rows // 4) * 4
    k_pad = -(-(kh * kw * cin_pad) // 32) * 32
    assert nodes is None, "reference only covers the local builder"
    per_batch = oh * ow
    k_logical = kh * kw * cin_pad
    if (
        edge_space == "stage"
        and rows_pad * k_pad * 2 > 665600
        and k_logical == k_pad
        and per_batch % 4 == 0
        and per_batch * k_pad * 2 <= 665600
    ):
        chunk_b = max(
            b
            for b in range(1, batch + 1)
            if batch % b == 0 and b * per_batch * k_pad * 2 <= 665600
        )
        chunks = []
        for b0 in range(0, batch, chunk_b):
            u_graph = (
                frozen_unfold_kernel(
                    chunk_b,
                    height,
                    width,
                    cin_pad,
                    kh,
                    kw,
                    stride,
                    padding,
                    batch_offset=b0,
                    total_batch=batch,
                )
                .trace(
                    {"x": (batch * height * width, cin_pad)},
                    machine=dsl_machine(machine),
                )
                .graph
            )
            u_graph = dict(
                u_graph,
                scratch=[
                    dict(s, spec=dict(s["spec"], space="stage"))
                    for s in u_graph["scratch"]
                ],
            )
            p_graph = linear_graph(
                chunk_b * per_batch,
                k_pad,
                cout_pad,
                machine,
                activation,
                residual,
                ring_depth,
                out_offset=b0 * per_batch * cout_pad,
                out_rows=rows_pad,
            )
            chunks.append(compose(u_graph, p_graph, "out", ("a",), "stage"))
        return sequence(chunks, space="stage")
    unfold = (
        frozen_unfold_kernel(batch, height, width, cin_pad, kh, kw, stride, padding)
        .trace({"x": (batch * height * width, cin_pad)}, machine=dsl_machine(machine))
        .graph
    )
    patch_space = (
        "stage" if edge_space == "stage" and rows_pad * k_pad * 2 <= 665600 else "dram"
    )
    unfold = dict(
        unfold,
        scratch=[
            dict(s, spec=dict(s["spec"], space=patch_space)) for s in unfold["scratch"]
        ],
    )
    eff_ring = (
        1
        if patch_space == "stage"
        and rows_pad * k_pad * 3 + ring_depth * machine.count("MG") * 25600 > 1048576
        else ring_depth
    )
    product = linear_graph(
        rows_pad, k_pad, cout_pad, machine, activation, residual, eff_ring
    )
    return compose(unfold, product, "out", ("a",), patch_space)
