"""FP16 activation packing and class contraction in one eagerly launched kernel."""

from dataclasses import replace

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels.classes import mm as CM
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.language import dsl as kt


def epilogue(activation, residual):
    value = X.op("add", X.Load(0), X.Load(1))
    if activation == "relu":
        value = X.op("max", value, 0)
    elif activation == "gelu":
        cube = X.op("mul", X.op("mul", value, value), value)
        z = X.op("mul", X.op("add", value, X.op("mul", 0.044715, cube)), 0.7978845608)
        tanh = X.op(
            "sub",
            X.op(
                "mul",
                2,
                X.op(
                    "recip", X.op("add", 1, X.op("exp2", X.op("mul", -2 * kt.LOG2E, z)))
                ),
            ),
            1,
        )
        value = X.op("mul", X.op("mul", 0.5, value), X.op("add", 1, tanh))
    elif activation != "none":
        raise ValueError("activation must be none, relu, or gelu")
    return X.op("add", value, X.Load(2)) if residual else value


def stages(
    rows,
    width,
    columns,
    clusters,
    cores,
    activation="none",
    residual=False,
    tile_rows=None,
    *,
    machine_json,
):
    """Activation packing and the existing class contraction generator."""
    if width % 32:
        raise ValueError(
            "native linear input width must contain whole 32-column blocks"
        )
    mm = MM.plan(1, rows, columns, width, machine_json=machine_json)
    if tile_rows is not None:
        if mm.mp % tile_rows:
            raise ValueError("tile_rows must divide the padded activation rows")
        mm = replace(mm, tm=tile_rows)
    shape_class = ShapeClass(32, 256)
    quant, _ = MM.quant_kernel(
        Operand("full", pitch=width, rows=rows),
        rows,
        width,
        mm.mp,
        mm.kp,
        "a",
        mm.tm // 4,
        mm.nk,
        shape_class=shape_class,
    )
    extras = (Operand("col"),)
    if residual:
        extras += (Operand("full", pitch=columns, rows=mm.mp),)
    product, _ = CM.kernel(
        mm,
        epilogue(activation, residual),
        extras,
        Operand("full", pitch=columns, rows=mm.mp),
        clusters,
        cores,
        shape_class,
    )
    return mm, quant, product


def kernel(
    rows,
    width,
    columns,
    clusters,
    cores,
    activation="none",
    residual=False,
    tile_rows=None,
    class_capacity=True,
    *,
    machine_json,
):
    """Logical FP16 rows in/out; packed weights are persistent caller-owned forms.

    class_capacity fixes internal rows to a power-of-two tile class; only logical
    copy walks vary. False keeps exact-row work. Both retain DRAM intermediates.
    """
    mm, quant, product = stages(
        rows,
        width,
        columns,
        clusters,
        cores,
        activation,
        residual,
        tile_rows,
        machine_json=machine_json,
    )
    quant_rows = mm.mp
    if class_capacity:
        quant_rows = mm.tm * max(2, 1 << (mm.mp // mm.tm - 1).bit_length())
        mm, quant, product = stages(
            quant_rows,
            width,
            columns,
            clusters,
            cores,
            activation,
            residual,
            mm.tm,
            machine_json=machine_json,
        )
    ports = {
        "x": kt.In(rows, width),
        "w": product.ports["b"],
        "bias": kt.In(columns),
    }
    if residual:
        ports["residual"] = kt.In(rows, columns)
    ports["out"] = kt.Out(rows, columns)

    def body(**p):
        source = p["x"]
        if class_capacity:
            source = kt.buffer((quant_rows, width), "f16")
            kt.fill_value(source, 0)
            kt.copy(source[:rows, :], p["x"], classed=True, node_loop=True)
        a = kt.buffer((quant_rows, mm.kp), "mx7a", gm=mm.tm // 4, nk=mm.nk)
        quant.fn(x=source, q=a)
        binds = {"a": a, "b": p["w"], "in1": p["bias"]}
        if residual:
            r = p["residual"]
            if class_capacity or rows != mm.mp:
                r = kt.buffer((mm.mp, columns), "f16")
                kt.fill_value(r, 0)
                kt.copy(r[:rows, :], p["residual"], classed=True, node_loop=True)
            binds["in2"] = r
        staged = class_capacity or rows != mm.mp
        out = kt.buffer((mm.mp, columns), "f16") if staged else p["out"]
        product.fn(**binds, out=out)
        if staged:
            kt.copy(p["out"], out[:rows, :], classed=True, node_loop=True)

    result = kt.Kernel.build("native_linear", body, ports)
    result.class_info = {
        "row_capacity": quant_rows,
        "tm": mm.tm,
        "tn": mm.tn,
        "nk": mm.nk,
    }
    return result


def mlp_kernel(
    rows,
    width,
    hidden,
    columns,
    clusters,
    cores,
    activation="gelu",
    residual=False,
    class_capacity=True,
    *,
    machine_json,
):
    """Two linear stages in one package, retaining an FP16 hidden boundary."""
    first = kernel(
        rows,
        width,
        hidden,
        clusters,
        cores,
        activation,
        class_capacity=class_capacity,
        machine_json=machine_json,
    )
    second = kernel(
        rows,
        hidden,
        columns,
        clusters,
        cores,
        "none",
        residual,
        class_capacity=class_capacity,
        machine_json=machine_json,
    )
    ports = {
        "x": first.ports["x"],
        "w1": first.ports["w"],
        "b1": first.ports["bias"],
        "w2": second.ports["w"],
        "b2": second.ports["bias"],
    }
    if residual:
        ports["residual"] = second.ports["residual"]
    ports["out"] = second.ports["out"]

    def body(**p):
        h = kt.buffer((rows, hidden), "f16")
        first.fn(x=p["x"], w=p["w1"], bias=p["b1"], out=h)
        extra = {"residual": p["residual"]} if residual else {}
        second.fn(x=h, w=p["w2"], bias=p["b2"], out=p["out"], **extra)

    return kt.Kernel.build("native_mlp", body, ports)
