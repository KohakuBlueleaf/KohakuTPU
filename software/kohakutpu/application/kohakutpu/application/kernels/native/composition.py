"""Typed-graph composition, using GFUSE's hygienic private-edge substitution."""

from kohakutpu.language import dsl as kt
from kohakutpu.language.dsl import TensorSpec, Traced
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


def renamed(value, mapping, field=""):
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


def compose(left, right, output, inputs, space="stage"):
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


def var_names(body, prefix):
    result = {}
    for statement in statements(body):
        for field in ("var", "target"):
            if field in statement:
                result[statement[field]] = prefix + statement[field]
    return result


def sequence(graphs, space="stage"):
    """Execute chunk graphs sequentially while reusing scratch buffers."""
    first = graphs[0]
    shared = {s["name"] for s in first["scratch"] if _spec_bytes(s["spec"]) >= 256}
    scratch = [
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


def trim_in_tail(
    graph, rows, capacity, in_name, h1_name, machine, *, r0=0, total_rows=None
):
    """Quantize whole 4-row groups directly from in_name and stage only the tail group."""
    total_rows = rows if total_rows is None else total_rows
    width = next(p["spec"]["shape"][1] for p in graph["ports"] if p["name"] == in_name)
    main_rows = (rows // 4) * 4
    rem_rows = rows - main_rows
    tail_cap = capacity - main_rows
    for p in graph["ports"]:
        if p["name"] == in_name:
            p["spec"]["shape"] = [total_rows, width]
    orig_q = graph["body"][0]
    kw = orig_q.get("kw", [])
    flags = orig_q.get("flags", [])
    graph["scratch"].append(
        {
            "name": "tail_in",
            "spec": {
                "shape": [tail_cap, width],
                "dtype": "f16",
                "layout": [],
                "space": "stage",
            },
        }
    )

    def copy_tail_in(x, tail):
        kt.fill_value(tail[rem_rows:tail_cap, :], 0)
        if rem_rows > 0:
            kt.copy(
                tail,
                x,
                at=(r0 + main_rows) * width * 2,
                walk=((rem_rows * width // 16, 32),),
                dwalk=((rem_rows * width // 16, 32),),
            )

    cp = (
        kt.Kernel.build(
            "tail_in_cp",
            copy_tail_in,
            {"x": kt.In(total_rows, width), "tail": kt.Out(tail_cap, width)},
        )
        .trace({"x": (total_rows, width)}, machine=dsl_machine(machine))
        .graph
    )
    new_q = list(renamed(cp["body"], {"x": in_name, "tail": "tail_in"}))
    if main_rows > 0:
        new_q.append(
            {
                "kind": "mover",
                "op": "quant",
                "dst": {
                    "buffer": h1_name,
                    "axes": [{"run": [0, main_rows]}, {"run": [0, width]}],
                },
                "src": {
                    "buffer": in_name,
                    "axes": [{"run": [r0, main_rows]}, {"run": [0, width]}],
                },
                "flags": list(flags),
                "kw": list(kw),
                "where": orig_q.get("where", "<native>"),
            }
        )
    new_q.append(
        {
            "kind": "mover",
            "op": "quant",
            "dst": {
                "buffer": h1_name,
                "axes": [{"run": [main_rows, tail_cap]}, {"run": [0, width]}],
            },
            "src": {"buffer": "tail_in", "axes": ["all", "all"]},
            "flags": list(flags),
            "kw": list(kw),
            "where": orig_q.get("where", "<native>"),
        }
    )
    graph["body"] = new_q + graph["body"][1:]
    return graph


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
    has_when = vc_par["body"][-1].get("kind") == "when"
    when_stmt = vc_par["body"][-1] if has_when else None
    t_loop = when_stmt["body"][-1] if has_when else vc_par["body"][-1]
    height = t_loop["body"][-2]["type"]["shape"][0]
    n_main = rows // height
    main_rows = n_main * height
    tail_rows = capacity - main_rows
    rem_rows = rows - main_rows
    columns = next(
        p["spec"]["shape"][1] for p in graph["ports"] if p["name"] == out_name
    )
    t_main = dict(t_loop, hi=n_main)
    vc_main = dict(
        vc_par,
        body=[
            *vc_par["body"][:-1],
            (
                dict(when_stmt, body=[*when_stmt["body"][:-1], t_main])
                if has_when
                else t_main
            ),
        ],
    )
    vc_tail = renamed(vc_par, var_names([vc_par], "tl_"))
    tail_holder = vc_tail["body"][-1] if has_when else vc_tail
    t_tail = dict(tail_holder["body"][-1], lo=n_main, hi=n_main + 1)
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
    if has_when:
        when_tail = dict(tail_holder, body=[*tail_holder["body"][:-1], t_tail])
        vc_tail = dict(vc_tail, body=[*vc_tail["body"][:-1], when_tail])
    else:
        vc_tail = dict(vc_tail, body=[*vc_tail["body"][:-1], t_tail])
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


def _spec_bytes(spec):
    items = 1
    for d in spec["shape"]:
        items *= d
    return items * (1 if str(spec["dtype"]).startswith("mx7") else 2)


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


class GraphKernel:
    """Existing custom-call protocol over a generated graph, without a second ABI."""

    def __init__(self, graph):
        self.graph = graph
        self.name = graph["name"]
        self.consts = {}
        self.inputs = [p["name"] for p in graph["ports"] if p["role"] == "in"]
        self.outputs = [p["name"] for p in graph["ports"] if p["role"] == "out"]

    def trace(self, given=None, *, machine=None, check=None, **consts):
        params = tuple(
            (
                p["name"],
                TensorSpec.make(
                    p["spec"]["shape"],
                    p["spec"]["dtype"],
                    {k: next(iter(v.values())) for k, v in p["spec"]["layout"]},
                    p["spec"]["space"],
                ),
            )
            for p in self.graph["ports"]
        )
        result = Traced(
            self.name,
            "",
            params,
            {p["name"]: p["role"] for p in self.graph["ports"]},
            {},
            machine,
            graph=self.graph,
        )
        if check is not None:
            check(result)
        return result
