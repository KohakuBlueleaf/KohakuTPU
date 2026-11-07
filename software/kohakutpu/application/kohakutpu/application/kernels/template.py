"""Generator descriptors retained by standalone Python kernel factories.

The Python body remains available for inlining into an explicitly authored DSL
kernel. Device specialisation of the standalone result uses its recorded call.
"""

from dataclasses import asdict, dataclass, is_dataclass
from functools import wraps
from inspect import signature

from kohakutpu.application.plan.emit import expr
from kohakutpu.language import dsl as kt
from kohakutpu.language.dsl import TensorSpec, Traced


@dataclass(frozen=True)
class TemplateCall:
    generator: str
    arguments: dict


def argument(name, value):
    if name == "e":
        return expr(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (tuple, list)):
        return [argument("", item) for item in value]
    return value


def template(generator, fields):
    """Keep the logical generator invocation on a factory's returned kernel."""

    def decorate(factory):
        parameters = signature(factory)

        @wraps(factory)
        def build(*args, **kwargs):
            result = factory(*args, **kwargs)
            kernel = result[0]
            if not hasattr(kernel, "template_call"):
                values = parameters.bind(*args, **kwargs)
                values.apply_defaults()
                kernel.template_call = TemplateCall(
                    generator,
                    {
                        name: argument(name, values.arguments[name])
                        for name in fields
                        if values.arguments[name] is not None
                    },
                )
            return result

        return build

    return decorate


class GraphKernel(kt.Kernel):
    """Existing custom-call protocol over a generated graph, without a second ABI."""

    def __init__(self, graph, *, selection=None, template_call=None):
        self.generated = graph
        self.selection = dict(selection or {})
        self.name = graph["name"]
        self.consts = {}
        self.template_call = template_call
        self.ports = {
            p["name"]: (kt.In if p["role"] == "in" else kt.Out)(
                *p["spec"]["shape"],
                dtype=p["spec"]["dtype"],
                layout={k: next(iter(v.values())) for k, v in p["spec"]["layout"]},
                space=p["spec"]["space"],
            )
            for p in graph["ports"]
        }

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
            for p in self.generated["ports"]
        )
        result = Traced(
            self.name,
            "",
            params,
            {p["name"]: p["role"] for p in self.generated["ports"]},
            {},
            machine if machine is not None else kt.machine(),
            graph=self.generated,
        )
        if check is not None:
            check(result)
        return result
