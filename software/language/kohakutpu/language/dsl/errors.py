"""The DSL's refusals, each naming the source line that caused it."""

import functools
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent


class DSLError(ValueError):
    """A kernel that cannot be traced as written, and why."""


class TracerControlFlowError(DSLError):
    """A traced value asked a question only the machine can answer."""


@functools.cache
def _inside(filename: str) -> bool:
    """Whether a code object's file is in this package (resolved once a file:
    a trace asks for every op it records)."""
    try:
        return pathlib.Path(filename).resolve().is_relative_to(_HERE)
    except OSError:
        return False


def at_caller() -> str:
    """``file:line`` of the innermost frame outside this package (the
    kernel author's line)."""
    f = sys._getframe(1)
    while f is not None:
        if not _inside(f.f_code.co_filename):
            return f"{pathlib.Path(f.f_code.co_filename).name}:{f.f_lineno}"
        f = f.f_back
    return "<unknown>"


def fail(message: str) -> DSLError:
    """A `DSLError` carrying the author's line."""
    return DSLError(f"{message}, at {at_caller()}")


__all__ = ["DSLError", "TracerControlFlowError", "at_caller", "fail"]
