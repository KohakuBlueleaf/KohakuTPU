"""Single-launch row reductions, and normalization as a linear input stage."""

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.kernels.native import linear as L
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
