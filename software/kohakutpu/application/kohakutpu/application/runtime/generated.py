"""Machine-specific Rust kernel generation and the shared driver binding path."""

import json
import os
from functools import lru_cache

import numpy as np

from kohakutpu.application.kernels import matmul as MM
from kohakutpu.language.dsl import TensorSpec, Traced
from kohakutpu.language.numerics import fp16
from kohakutpu.language.text import module as M
from kohakutpu.stack import rs
from kohakutpu.stack.kernel import Compiled, dsl_machine


@lru_cache(maxsize=4096)
def graph(generator, arguments, machine, mutant):
    ring = (
        json.loads(arguments).get("ring_depth", 0)
        if generator == "matmul.mm_kernel"
        else 0
    )
    if ring not in (0, 1, 2):
        raise ValueError("matmul ring depth must be 0, 1 or 2")
    source = rs._rs.generate(generator, arguments, machine, mutant)
    if ring:
        # Qualify the generator at its cached production boundary, not on hits.
        scratch = json.loads(source)["scratch"]
        if not scratch or any(p["spec"]["space"] != "stage" for p in scratch):
            raise ValueError("ring lowering requires the MMEFF ring-capable compiler")
    return source


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
