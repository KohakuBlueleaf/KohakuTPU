"""Machine-specific Rust kernel generation and the shared driver binding path."""

import json
from functools import lru_cache

from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.plan.emit import expr
from kohakutpu.application.runtime import packing
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
        arguments["finite"] = packing.finite_contract(
            dev,
            bindings["x"],
            Operand(**arguments["src"]),
            arguments["rows"],
            arguments["cols"],
            arguments.get("batches", 1),
        )
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
