"""Generator descriptors retained by standalone Python kernel factories.

The Python body remains available for inlining into an explicitly authored DSL
kernel. Device specialisation of the standalone result uses its recorded call.
"""

from dataclasses import asdict, dataclass, is_dataclass
from functools import wraps
from inspect import signature

from kohakutpu.application.plan.emit import expr


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
