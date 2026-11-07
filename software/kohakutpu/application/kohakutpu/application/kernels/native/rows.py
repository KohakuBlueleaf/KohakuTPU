"""Single-launch row reductions, and normalization as a linear input stage."""

from dataclasses import asdict

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.kernels.native import linear as L
from kohakutpu.application.kernels.template import TemplateCall
from kohakutpu.application.plan.emit import expr
from kohakutpu.language import dsl as kt


def expression(op, width, eps=1e-5):
    x = X.Load(0)
    if op == "softmax":
        p = X.op("exp2", X.op("mul", kt.LOG2E, X.op("sub", x, X.Reduce("max", x))))
        return X.op("mul", p, X.op("recip", X.Reduce("sum", p)))
    if op == "layernorm":
        centered = X.op("sub", x, X.op("mul", 1.0 / width, X.Reduce("sum", x)))
        variance = X.op(
            "mul", 1.0 / width, X.Reduce("sum", X.op("mul", centered, centered))
        )
        normalized = X.op("mul", centered, X.op("rsqrt", X.op("add", variance, eps)))
        return X.op("add", X.op("mul", normalized, X.Load(1)), X.Load(2))
    raise ValueError("row op must be layernorm or softmax")


def kernel(op, rows, width, cores, eps=1e-5):
    operands = (V.Operand("full", pitch=width, rows=rows),)
    if op == "layernorm":
        operands += (V.Operand("col"), V.Operand("col"))
    result, _, _ = V.rows_kernel(
        expression(op, width, eps),
        operands,
        rows,
        width,
        V.Operand("full", pitch=width, rows=rows),
        cores,
        shape_class=ShapeClass(32, 256),
    )
    return result


def linear_kernel(
    rows, width, columns, clusters, cores, eps=1e-5, activation="none", *, machine_json
):
    norm = kernel("layernorm", rows, width, cores, eps)
    product = L.kernel(
        rows, width, columns, clusters, cores, activation, machine_json=machine_json
    )
    ports = {
        "x": kt.In(rows, width),
        "scale": kt.In(width),
        "shift": kt.In(width),
        "w": product.ports["w"],
        "bias": kt.In(columns),
        "out": kt.Out(rows, columns),
    }

    def body(x, scale, shift, w, bias, out):
        h = kt.buffer((rows, width), "f16")
        norm.fn(in0=x, in1=scale, in2=shift, out=h)
        product.fn(x=h, w=w, bias=bias, out=out)

    return kt.Kernel.build("native_norm_linear", body, ports)


def wide_norm_call(rows, width, eps, *, balance_norm=True):
    """Mean, reciprocal standard deviation and affine rows in private staging."""
    full, stat, col = (
        V.Operand("full", pitch=width),
        V.Operand("row"),
        V.Operand("col"),
    )
    x = X.Load(0)
    mean = X.op("mul", X.Reduce("sum", x), 1.0 / width)
    d = X.op("sub", x, X.Load(1))
    rstd = X.op(
        "rsqrt",
        X.op("add", X.op("mul", X.Reduce("sum", X.op("mul", d, d)), 1.0 / width), eps),
    )
    affine = X.op(
        "add",
        X.op("mul", X.op("mul", d, X.Load(2)), X.Load(3)),
        X.Load(4),
    )
    steps = [
        (mean, (full,), ("in0",), stat, "mean", True),
        (rstd, (full, stat), ("in0", "mean"), stat, "rstd", True),
        (
            affine,
            (full, stat, stat, col, col),
            ("in0", "mean", "rstd", "in1", "in2"),
            full,
            "out",
            False,
        ),
    ]
    return TemplateCall(
        "fused.kernel",
        {
            "ports": [
                {
                    "name": "in0",
                    "shape": [rows, width],
                    "dtype": "f16",
                    "written": False,
                },
                {"name": "in1", "shape": [width], "dtype": "f16", "written": False},
                {"name": "in2", "shape": [width], "dtype": "f16", "written": False},
                {
                    "name": "out",
                    "shape": [rows, width],
                    "dtype": "f16",
                    "written": True,
                },
            ],
            "scratch": [
                {
                    "name": name,
                    "shape": [V.stat_words(rows) * V.STAT_ROWS],
                    "dtype": "f16",
                }
                for name in ("mean", "rstd")
            ],
            "scratch_space": "stage",
            "balance_stages": balance_norm,
            "staged": [],
            "steps": [
                {
                    "kind": "rows",
                    "fold": fold,
                    "R": rows,
                    "W": width,
                    "exprs": [expr(e)],
                    "operands": [asdict(o) for o in operands],
                    "ins": list(inputs),
                    "out_operands": [asdict(out)],
                    "outs": [output],
                }
                for e, operands, inputs, out, output, fold in steps
            ],
        },
    )


def norm_capacity_call(
    rows, capacity, product_rows, width, eps, *, classed, balance_norm
):
    """Initialize a private output capacity, normalize it in place, clear its tail."""
    if width > 256:
        result = wide_norm_call(capacity, width, eps, balance_norm=balance_norm)
        arguments = result.arguments
        arguments["ports"][0]["shape"] = [rows, width]
        for step in arguments["steps"]:
            step["ins"] = ["out" if name == "in0" else name for name in step["ins"]]
    else:
        full = V.Operand("full", pitch=width, rows=capacity)
        arguments = {
            "ports": [
                {
                    "name": "in0",
                    "shape": [rows, width],
                    "dtype": "f16",
                    "written": False,
                },
                {"name": "in1", "shape": [width], "dtype": "f16", "written": False},
                {"name": "in2", "shape": [width], "dtype": "f16", "written": False},
                {
                    "name": "out",
                    "shape": [capacity, width],
                    "dtype": "f16",
                    "written": True,
                },
            ],
            "scratch": [],
            "staged": [],
            "steps": [
                {
                    "kind": "rows",
                    "R": capacity,
                    "W": width,
                    "exprs": [expr(expression("layernorm", width, eps))],
                    "operands": [
                        asdict(o) for o in (full, V.Operand("col"), V.Operand("col"))
                    ],
                    "ins": ["out", "in1", "in2"],
                    "out_operands": [asdict(full)],
                    "outs": ["out"],
                }
            ],
        }
        result = TemplateCall("fused.kernel", arguments)
    arguments["steps"].insert(
        0,
        {
            "kind": "prefix",
            "src": "in0",
            "dst": "out",
            "boundary": {
                "src_rows": rows,
                "dst_rows": capacity,
                "width": width,
                "rows": capacity,
                "copied_rows": rows,
                "classed": classed,
                "initialize": True,
            },
        },
    )
    if capacity != product_rows and rows < product_rows:
        arguments["steps"].append(
            {
                "kind": "prefix",
                "src": "in0",
                "dst": "out",
                "boundary": {
                    "src_rows": rows,
                    "dst_rows": capacity,
                    "width": width,
                    "dst_first": rows,
                    "rows": product_rows - rows,
                    "copied_rows": 0,
                    "initialize": True,
                },
            }
        )
    return result
