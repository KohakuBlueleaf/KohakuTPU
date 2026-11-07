"""Native eager ops built from Rust FP16-input contraction stages
(`matmul.mm_kernel` under `activation_fp16` and `ring_depth`).

Graph composition is before lowering; compiled output uses the normal library.
"""

from dataclasses import asdict

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels.native import layout as Y
from kohakutpu.application.kernels.native import linear as L
from kohakutpu.application.kernels.native import rows as R
from kohakutpu.application.kernels.template import GraphKernel, TemplateCall
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.plan.emit import expr
from kohakutpu.application.runtime import generated as G
from kohakutpu.application.runtime.generated import template_graph as graph


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


def linear_call(
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
    return TemplateCall(
        "matmul.mm_kernel",
        {
            "activation_any": True,
            "activation_fp16": False,
            "batch_tiles": False,
            "split_vc": False,
            "tail_bands": False,
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
    call = linear_call(
        rows,
        width,
        columns,
        machine,
        activation,
        residual,
        ring_depth,
        out_offset=out_offset,
        out_rows=out_rows,
        n_tiles_override=n_tiles_override,
    )
    return graph(call.generator, call.arguments, machine)


def row_boundary_call(src_rows, dst_rows, width, *, classed, initialize):
    return TemplateCall(
        "convert.rows_kernel",
        {
            "src_rows": src_rows,
            "dst_rows": dst_rows,
            "width": width,
            "classed": classed,
            "initialize": initialize,
        },
    )


def chain_call(first, second, source, destination, interface):
    """A private typed edge and the public ports in caller order."""
    return TemplateCall(
        "kernel.chain",
        {
            "first": {"gen": first.generator, "args": first.arguments},
            "second": {"gen": second.generator, "args": second.arguments},
            "from": source,
            "to": destination,
            "keep": False,
            "interface": [
                {"side": side, "port": port, "name": name}
                for side, port, name in interface
            ],
        },
    )


def linear_capacity_call(
    product, rows, capacity, width, columns, residual, classed, output_classed
):
    """Input boundaries, capacity product and logical output in one typed chain."""
    inputs = ["a", "b", "in1"] + (["in2"] if residual else [])
    product = chain_call(
        product,
        row_boundary_call(
            capacity, rows, columns, classed=output_classed, initialize=False
        ),
        "src",
        "out",
        [("first", port, port) for port in inputs] + [("second", "dst", "out")],
    )
    if residual:
        product = chain_call(
            row_boundary_call(
                rows, capacity, columns, classed=classed, initialize=True
            ),
            product,
            "in2",
            "dst",
            [
                ("second", "a", "a"),
                ("second", "b", "b"),
                ("second", "in1", "in1"),
                ("first", "src", "in2"),
                ("second", "out", "out"),
            ],
        )
    product = chain_call(
        row_boundary_call(rows, capacity, width, classed=classed, initialize=True),
        product,
        "a",
        "dst",
        [("first", "src", "a")]
        + [("second", port, port) for port in inputs[1:] + ["out"]],
    )
    product.arguments["stage_budget"] = 983040
    return product


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
    call = linear_call(
        capacity, width, columns, machine, activation, residual, ring_depth
    )
    if row_capacity is not None or capacity != rows:
        classed = row_capacity is not None or not mover_overlap
        call = linear_capacity_call(
            call,
            rows,
            capacity,
            width,
            columns,
            residual,
            classed,
            classed and (row_capacity is not None or ring_depth == 0),
        )
    call.arguments["quant_backend"] = quant_backend
    return GraphKernel(
        graph(call.generator, call.arguments, machine), template_call=call
    )


def mlp_products_call(
    capacity,
    width,
    hidden,
    columns,
    machine,
    activation,
    residual,
    ring_depth,
    edge_space,
    *,
    first_tiles=None,
    second_tiles=None,
    output_boundary=None,
):
    """Typed product stages, with output-boundary fusion before composition."""
    first = linear_call(
        capacity,
        width,
        hidden,
        machine,
        "none" if activation == "gelu" else activation,
        False,
        ring_depth,
        n_tiles_override=first_tiles,
    )
    if activation == "gelu":
        full = Operand("full", pitch=hidden, rows=capacity)
        pointwise = TemplateCall(
            "vector.rows_kernel",
            {
                "e": expr(X.gelu(X.Load(0))),
                "expr_cse": True,
                "operands": [asdict(full)],
                "R": capacity,
                "W": hidden,
                "out": asdict(full),
            },
        )
        first = chain_call(
            first,
            pointwise,
            "in0",
            "out",
            [("first", port, port) for port in ("a", "b", "in1")]
            + [("second", "out", "out")],
        )
        first.arguments["materialized_pointwise"] = True
        first.arguments["link_space"] = edge_space
    second = linear_call(
        capacity,
        hidden,
        columns,
        machine,
        "none",
        residual,
        ring_depth,
        n_tiles_override=second_tiles,
    )
    if output_boundary is not None:
        second_inputs = ["a", "b", "in1"] + (["in2"] if residual else [])
        second = chain_call(
            second,
            output_boundary,
            "src",
            "out",
            [("first", port, port) for port in second_inputs]
            + [("second", "dst", "out")],
        )
    interface = [
        ("first", "a", "p_a"),
        ("first", "b", "p_b"),
        ("first", "in1", "p_in1"),
        ("second", "b", "c_b"),
        ("second", "in1", "c_in1"),
    ]
    if residual:
        interface.append(("second", "in2", "c_in2"))
    interface.append(("second", "out", "c_out"))
    result = chain_call(first, second, "a", "out", interface)
    result.arguments["link_space"] = edge_space
    return result


def mlp_call(
    rows,
    width,
    hidden,
    columns,
    machine,
    activation="gelu",
    residual=False,
    ring_depth=2,
    edge_space="stage",
    quant_backend="mover_quant",
):
    """Two typed contractions with a materialized FP16 activation stage."""
    capacity = -(-rows // 4) * 4
    result = mlp_products_call(
        capacity,
        width,
        hidden,
        columns,
        machine,
        activation,
        residual,
        ring_depth,
        edge_space,
    )
    input_names = ["p_a", "p_b", "p_in1", "c_b", "c_in1"] + (
        ["c_in2"] if residual else []
    )
    if rows != capacity:
        result = chain_call(
            result,
            row_boundary_call(
                capacity,
                rows,
                columns,
                classed=ring_depth == 0 or residual,
                initialize=False,
            ),
            "src",
            "c_out",
            [("first", port, port) for port in input_names]
            + [("second", "dst", "c_out")],
        )
        boundaries = [("p_a", width)]
        if residual:
            boundaries.append(("c_in2", columns))
        for port, pitch in reversed(boundaries):
            result = chain_call(
                row_boundary_call(rows, capacity, pitch, classed=True, initialize=True),
                result,
                port,
                "dst",
                [
                    ("first", "src", name) if name == port else ("second", name, name)
                    for name in input_names + ["c_out"]
                ],
            )
        result.arguments["stage_budget"] = 983040
    result.arguments["quant_backend"] = quant_backend
    return result


def mlp_chunks_call(
    logical,
    chunk_rows,
    width,
    hidden,
    columns,
    machine,
    activation,
    ring_depth,
    *,
    compact_mlp=True,
    mover_overlap=True,
    quant_backend="mover_quant",
):
    """An ordered typed row-chunk schedule with global public interfaces."""
    clusters = machine.count("MG")
    second_tiles = _cluster_n_tiles(chunk_rows, columns, clusters)
    tile_columns = columns // second_tiles
    aligned = (
        compact_mlp and clusters > 1 and ring_depth > 0 and hidden % tile_columns == 0
    )
    effective_ring = 1 if aligned else ring_depth
    ports = ["p_a", "p_b", "p_in1", "c_b", "c_in1", "c_out"]
    members = []
    for first in range(0, logical, chunk_rows):
        valid = min(chunk_rows, logical - first)
        output = TemplateCall(
            "convert.prefix_kernel",
            {
                "src_rows": chunk_rows,
                "dst_rows": logical,
                "width": columns,
                "rows": valid,
                "dst_first": first,
                "initialize": False,
            },
        )
        chunk = mlp_products_call(
            chunk_rows,
            width,
            hidden,
            columns,
            machine,
            activation,
            False,
            effective_ring,
            "stage",
            first_tiles=hidden // tile_columns if aligned else None,
            second_tiles=second_tiles if aligned else None,
            output_boundary=output,
        )
        boundary = TemplateCall(
            "convert.prefix_kernel",
            {
                "src_rows": logical,
                "dst_rows": chunk_rows,
                "src_pitch": width,
                "dst_pitch": max(width, hidden) if valid < chunk_rows else width,
                "width": width,
                "src_first": first,
                "rows": chunk_rows,
                "copied_rows": valid,
                "initialize": valid < chunk_rows,
            },
        )
        chunk = chain_call(
            boundary,
            chunk,
            "p_a",
            "dst",
            [
                ("first", "src", port) if port == "p_a" else ("second", port, port)
                for port in ports
            ],
        )
        if valid < chunk_rows:
            chunk.arguments["link_view"] = "prefix"
        chunk.arguments["link_space"] = "stage"
        members.append({"gen": chunk.generator, "args": chunk.arguments})
    return TemplateCall(
        "kernel.sequence",
        {
            "members": members,
            "quant_backend": quant_backend,
            "pipeline_lookahead": mover_overlap,
        },
    )


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
        call = mlp_chunks_call(
            logical,
            cr,
            width,
            hidden,
            columns,
            machine,
            activation,
            ring_depth,
            compact_mlp=compact_mlp,
            mover_overlap=mover_overlap,
            quant_backend=quant_backend,
        )
        return GraphKernel(
            graph(call.generator, call.arguments, machine), template_call=call
        )
    call = mlp_call(
        logical,
        width,
        hidden,
        columns,
        machine,
        activation,
        residual,
        ring_depth,
        edge_space,
        quant_backend,
    )
    return GraphKernel(
        graph(call.generator, call.arguments, machine), template_call=call
    )


def rows_call(op, rows, width, eps=1e-5, *, balance_norm=True):
    """Logical row stages, including complete FP16 wide folds."""
    if op == "layernorm" and width > 256:
        return R.wide_norm_call(rows, width, eps, balance_norm=balance_norm)
    operands = [Operand("full", pitch=width, rows=rows)]
    if op == "layernorm":
        operands += [Operand("col"), Operand("col")]
    return TemplateCall(
        "vector.rows_kernel",
        G.rows_arguments(
            R.expression(op, width, eps),
            operands,
            rows,
            width,
            Operand("full", pitch=width, rows=rows),
        ),
    )


def rows_graph(op, rows, width, machine, eps=1e-5, *, balance_norm=True):
    call = rows_call(op, rows, width, eps, balance_norm=balance_norm)
    return graph(call.generator, call.arguments, machine)


def rows_kernel(op, rows, width, machine, eps=1e-5, *, balance_norm=True):
    call = rows_call(op, rows, width, eps, balance_norm=balance_norm)
    return GraphKernel(
        graph(call.generator, call.arguments, machine), template_call=call
    )


def norm_linear_call(
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
    in_place = rows < norm_cap and ring_depth > 0 and edge == "stage"
    if in_place:
        norm = R.norm_capacity_call(
            rows,
            norm_cap,
            capacity,
            width,
            eps,
            classed=not mover_overlap,
            balance_norm=balance_norm,
        )
    else:
        norm = rows_call("layernorm", norm_cap, width, eps, balance_norm=balance_norm)
    product = linear_call(
        capacity, width, columns, machine, activation, False, ring_depth
    )
    if rows < capacity:
        product = chain_call(
            product,
            row_boundary_call(
                capacity,
                rows,
                columns,
                classed=not in_place and not mover_overlap,
                initialize=False,
            ),
            "src",
            "out",
            [("first", port, port) for port in ("a", "b", "in1")]
            + [("second", "dst", "out")],
        )
    call = chain_call(
        norm,
        product,
        "a",
        "out",
        [("first", port, "p_" + port) for port in ("in0", "in1", "in2")]
        + [("second", port, "c_" + port) for port in ("b", "in1", "out")],
    )
    if norm_cap != capacity:
        call.arguments["link_view"] = "prefix"
    call.arguments["link_space"] = edge
    if not in_place and rows < norm_cap:
        call = chain_call(
            row_boundary_call(
                rows, norm_cap, width, classed=not mover_overlap, initialize=True
            ),
            call,
            "p_in0",
            "dst",
            [("first", "src", "p_in0")]
            + [
                ("second", port, port)
                for port in ("p_in1", "p_in2", "c_b", "c_in1", "c_out")
            ],
        )
        call.arguments["stage_budget"] = 983040
    call.arguments["quant_backend"] = quant_backend
    return call


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
    call = norm_linear_call(
        rows,
        width,
        columns,
        machine,
        eps,
        activation,
        ring_depth,
        balance_norm=balance_norm,
        mover_overlap=mover_overlap,
        quant_backend=quant_backend,
    )
    return GraphKernel(
        graph(call.generator, call.arguments, machine), template_call=call
    )


def qkv_projection(
    batch, tokens, width, machine, *, eps=None, ring_depth=2, heads=None, dim=64
):
    """QKV linear (or norm+linear) writing planar Q, K and transposed V directly."""
    rows = batch * tokens
    heads = width // dim if heads is None else heads
    projection = (
        linear_call(rows, width, 3 * width, machine, "none", False, ring_depth)
        if eps is None
        else norm_linear_call(rows, width, 3 * width, machine, eps, "none", ring_depth)
    )
    inputs = (
        ("a", "b", "in1")
        if eps is None
        else ("p_in0", "p_in1", "p_in2", "c_b", "c_in1")
    )
    output = "out" if eps is None else "c_out"
    extraction = Y.split_call(
        batch,
        tokens,
        heads,
        dim,
        space="stage" if rows * width * 8 <= 600000 else "dram",
    )
    call = chain_call(
        projection,
        extraction,
        "x",
        output,
        [("first", port, "p_" + port) for port in inputs]
        + [("second", port, "c_" + port) for port in ("mask", "q", "k", "vt")],
    )
    call.arguments["link_space"] = "stage"
    return GraphKernel(
        graph(call.generator, call.arguments, machine), template_call=call
    )
