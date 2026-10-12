"""The DSL's refusals, each naming the source line that caused it."""

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent


class DSLError(ValueError):
    """A kernel that cannot be traced as written, and why."""


class TracerControlFlowError(DSLError):
    """A traced value asked a question only the machine can answer."""


def at_caller() -> str:
    """``file:line`` of the innermost frame outside this package (the
    kernel author's line)."""
    f = sys._getframe(1)
    while f is not None:
        path = pathlib.Path(f.f_code.co_filename)
        try:
            inside = path.resolve().is_relative_to(_HERE)
        except OSError:
            inside = False
        if not inside:
            return f"{path.name}:{f.f_lineno}"
        f = f.f_back
    return "<unknown>"


def fail(message: str) -> DSLError:
    """A `DSLError` carrying the author's line."""
    return DSLError(f"{message}, at {at_caller()}")


__all__ = ["DSLError", "TracerControlFlowError", "at_caller", "fail"]
