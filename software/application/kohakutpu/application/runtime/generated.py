"""Machine-specific Rust kernel generation and the shared driver binding path."""

import json
import os
from functools import lru_cache

from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.plan.emit import expr
from kohakutpu.language.dsl import TensorSpec, Traced
from kohakutpu.language.text import module as M
from kohakutpu.stack import rs
from kohakutpu.stack.kernel import Compiled, dsl_machine


@lru_cache(maxsize=4096)
def graph(generator, arguments, machine, mutant):
    return rs._rs.generate(generator, arguments, machine, mutant)


def launch(dev, generator, arguments, bindings, kind="dsl", info=None):
    machine = rs.machine_json(dev.machine)
    if generator == "matmul.quant_kernel":
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
    source = graph(
        generator, json.dumps(arguments, sort_keys=True), machine, rs._MUTANT.get()
    )
    compiled = dev.kernel(source)
    try:
        run = dev.run(compiled, bindings, kind=kind, info=info)
    except Exception as error:
        error.add_note(f"{generator}: {arguments}")
        raise
    run.info.setdefault("compiled", compiled)
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


def walk(dev, src_bytes, levels, at, dst_bytes, dlevels, dat, bindings, kind, info=None):
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
    return launch(dev, "convert.fill_kernel", arguments, bindings, kind="move", info=info)


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


def flat(dev, expression, dtypes, n, bindings, info=None, scalars=()):
    return launch(
        dev,
        "vector.flat_kernel",
        {
            "e": expr(expression),
            "dtypes": dtypes,
            "n": n,
            "out_dtype": "f16",
            "scalars": scalars,
        },
        bindings,
        info=info,
    )
