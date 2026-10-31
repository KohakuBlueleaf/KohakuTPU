"""Machine-specific Rust kernel generation and the shared driver binding path."""

import json
import os
from dataclasses import asdict
from functools import lru_cache

import numpy as np

from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.plan.emit import expr
from kohakutpu.language.dsl import TensorSpec, Traced
from kohakutpu.language.numerics import fp16
from kohakutpu.language.text import module as M
from kohakutpu.stack import rs
from kohakutpu.stack.kernel import Compiled, dsl_machine


@lru_cache(maxsize=4096)
def graph(generator, arguments, machine, mutant):
    return rs._rs.generate(generator, arguments, machine, mutant)


def source(dev, generator, arguments):
    """The graph of a generator call under the process's kernel knobs: what
    `launch` compiles, and what a trace-only capture records."""
    if generator in ("matmul.quant_kernel", "matmul.quant_walk_kernel"):
        arguments = dict(arguments)
        arguments["schedule"] = {
            name: int(os.environ.get(env, default))
            for name, env, default in (
                ("finite_batch", "KTPU_PIPE_FINITE_BATCH", "4"),
                ("overlap", "KTPU_PIPE_OVERLAP", "1"),
                ("classifier_overlap", "KTPU_QUANT_CLASSIFIER_OVERLAP", "0"),
                ("pipeline", "KTPU_QUANT_PIPELINE", "1"),
                ("zero_classifier", "KTPU_QUANT_ZERO_CLASSIFIER", "1"),
                ("stream_axes", "KTPU_QUANT_STREAM_AXES", "1"),
                ("fill_overlap", "KTPU_QUANT_FILL_OVERLAP", "1"),
                ("zero_reuse", "KTPU_QUANT_ZERO_REUSE", "1"),
            )
        }
    if generator == "matmul.mm_kernel":
        arguments = {**MM.kernel_knobs(), **arguments}
    return graph(*_call(dev, generator, _scheduled(arguments)))


def _scheduled(arguments: dict) -> dict:
    """`arguments`, a class call's with the `KTPU_CLASS_*` knobs."""
    if "shape_class" not in arguments:
        return arguments
    width = os.environ.get("KTPU_CLASS_WIDE_WIDTH")
    return dict(
        arguments,
        class_schedule={
            "band": int(os.environ.get("KTPU_CLASS_BAND", "4")),
            "wide_width": int(width) if width else None,
            "rows_direct": os.environ.get("KTPU_CLASS_ROWS_DIRECT", "0") == "1",
            "wide_direct": os.environ.get("KTPU_CLASS_WIDE_DIRECT", "0") == "1",
        },
    )


def _call(dev, generator, arguments) -> tuple:
    return (
        generator,
        json.dumps(arguments, sort_keys=True),
        rs.machine_json(dev.machine),
        rs._MUTANT.get(),
    )


def rows_arguments(e, operands, R, W, out) -> dict:
    """A `vector.rows_kernel` call of `e` over [R, W] rows into `out`."""
    return {
        "e": expr(e),
        "operands": [vars(o) for o in operands],
        "R": R,
        "W": W,
        "out": vars(out),
    }


def in_class(arguments: dict, shape_class) -> dict:
    """A generator call's arguments in `shape_class`."""
    return dict(arguments, shape_class=asdict(shape_class))


def class_info(dev, generator, arguments) -> dict:
    """What a class call decided (`_rs.class_info`)."""
    return json.loads(rs._rs.class_info(*_call(dev, generator, _scheduled(arguments))))


def classed(dev, generator, arguments, shape_class=None) -> tuple:
    """A generator call in `shape_class` (`lanes.class_kernel` none):
    ``(graph source, what it decided)``."""
    if shape_class is not None:
        arguments = in_class(arguments, shape_class)
    return source(dev, generator, arguments), class_info(dev, generator, arguments)


def class_fits(dev, arguments) -> bool:
    """Whether a `vector.rows_kernel` class call's trip fits a core's L1."""
    return rs._rs.class_fits(
        json.dumps(_scheduled(arguments)), rs.machine_json(dev.machine)
    )


def lanes_tables(dev, made) -> dict:
    """A `lanes.plan` call's classes with their padded tables: ``classes``
    of ``{class, index (fp32), masks (f16 bits, [taps, vlmax])}``,
    ``src_words``, ``dst_words``."""
    got = json.loads(
        rs._rs.lanes_class_tables(
            json.dumps(made["made_by"]["args"]), rs.machine_json(dev.machine)
        )
    )
    for c in got["classes"]:
        c["index"] = np.array(c["index"], np.float32)
        c["masks"] = np.array(c["masks"], np.uint16).reshape(
            len(c["class"]["taps"]), -1
        )
    return got


def launch(dev, generator, arguments, bindings, kind="dsl", info=None):
    compiled = dev.kernel(source(dev, generator, arguments))
    try:
        run = dev.run(compiled, bindings, kind=kind, info=info)
    except Exception as error:
        error.add_note(f"{generator}: {arguments}")
        raise
    run.info.setdefault("compiled", compiled)
    if arguments.get("composed"):
        run.info["composed"] = arguments["composed"]
    return run


def walks(levels) -> list:
    return [[int(count), int(stride)] for count, stride in levels]


def copy(dev, nbytes, bindings, info=None):
    return launch(
        dev,
        "convert.copy_kernel",
        {"nbytes": int(nbytes)},
        bindings,
        kind="copy",
        info=info,
    )


def walk(
    dev, src_bytes, levels, at, dst_bytes, dlevels, dat, bindings, kind, info=None
):
    arguments = {
        "src_bytes": int(src_bytes),
        "levels": walks(levels),
        "at": int(at),
        "dst_bytes": int(dst_bytes),
        "dlevels": walks(dlevels),
        "dat": int(dat),
    }
    return launch(dev, "convert.walk_kernel", arguments, bindings, kind=kind, info=info)


def fill(dev, dst_bytes, dlevels, dat, value, ew, bindings, info=None):
    arguments = {
        "dst_bytes": int(dst_bytes),
        "dlevels": walks(dlevels),
        "dat": int(dat),
        "value": int(value),
        "ew": int(ew),
    }
    return launch(
        dev, "convert.fill_kernel", arguments, bindings, kind="move", info=info
    )


def relayout_plan(gen, n, walk_, at, dwalk, dat, m, keep, z0=0) -> dict:
    """The generator's plan of one `relayout.plan` or `lanes.plan` call."""
    plan = {
        "src_n": int(n),
        "walk": walks(walk_),
        "at": int(at),
        "dwalk": walks(dwalk),
        "dat": int(dat),
        "m": int(m),
        "keep": bool(keep),
    }
    if z0:
        plan["z0"] = int(z0)
    return {"made_by": {"gen": gen, "args": plan}}


@lru_cache(maxsize=1024)
def _planned(gen, arguments, machine, mutant):
    plan = rs._rs.relayout_plan if gen == "relayout.plan" else rs._rs.lanes_plan
    got = json.loads(plan(arguments, machine, mutant))
    if "index" in got:
        got["index"] = np.array(got["index"], np.float32)
        if gen == "relayout.plan":
            got["masks"] = fp16.write(got["masks"]).view(np.uint16)
        else:
            got["masks"] = np.array(got["masks"], np.uint16)
    return got


def planned(dev, made) -> dict:
    """The plan of a `relayout_plan` call on `dev`: its tables ``index``
    (fp32) and ``masks`` (f16 bits) and, of a `relayout.plan` one, ``direct``
    (it writes its destination in words, not row indices); ``unplanned`` for
    a walk pair no form takes."""
    call = made["made_by"]
    return _planned(
        call["gen"],
        json.dumps(call["args"], sort_keys=True),
        rs.machine_json(dev.machine),
        rs._MUTANT.get(),
    )


def transpose_masks(dev):
    """The `transpose.transpose_kernel` mask table, f16 bits."""
    return np.array(rs._rs.transpose_masks(rs.machine_json(dev.machine)), np.uint16)


def rows_shape(dev, e, operands, R, W, out) -> dict:
    """``nblk``, the column blocks of `e` over [R, W] rows into `out`, and
    ``out``, its extents (`_rs.rows_shape`)."""
    arguments = rows_arguments(e, operands, R, W, out)
    return json.loads(
        rs._rs.rows_shape(
            json.dumps(arguments), rs.machine_json(dev.machine), rs._MUTANT.get()
        )
    )


def port_shape(dev, operand, R, W) -> tuple:
    """The extents of a buffer read as `operand` over [R, W] rows."""
    arguments = {"operand": vars(operand), "R": R, "W": W}
    return tuple(
        rs._rs.port_shape(
            json.dumps(arguments), rs.machine_json(dev.machine), rs._MUTANT.get()
        )
    )


def from_output(dev, graph_data, output, traced=None):
    name = output["name"]
    return Compiled(
        name,
        output["l2"],
        M.Text(output["l1"], f"<{name}.l1>"),
        dev.machine,
        verdict=output["verdict"],
        packages=output["packages"],
        stack="rust",
        traced=traced
        or Traced(
            name,
            output["l2"],
            tuple(
                (
                    p["name"],
                    TensorSpec.make(
                        p["spec"]["shape"],
                        p["spec"]["dtype"],
                        dict(p["spec"].get("layout", [])),
                        p["spec"].get("space", "dram"),
                    ),
                )
                for p in graph_data["ports"]
            ),
            {},
            {},
            dsl_machine(dev.machine),
            graph=graph_data,
        ),
        scratch=output.get("scratch"),
        bound=output["bound"],
        recipe_scope=(dev.recipe_epoch, dev.recipe_epoch.generation),
        native_recipe=dev.native_recipe,
        wire_recipe=dev.wire_recipe,
    )


def flat_arguments(expression, dtypes, n, scalars=()) -> dict:
    """A `vector.flat_kernel` call of `expression` over `n` elements."""
    return {
        "e": expr(expression),
        "dtypes": dtypes,
        "n": n,
        "out_dtype": "f16",
        "scalars": scalars,
    }


def flat(dev, expression, dtypes, n, bindings, info=None, scalars=()):
    return launch(
        dev,
        "vector.flat_kernel",
        flat_arguments(expression, dtypes, n, scalars),
        bindings,
        info=info,
    )
