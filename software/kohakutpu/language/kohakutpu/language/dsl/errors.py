"""The DSL's refusals, each naming the source line that caused it."""

import contextlib
import functools
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
#: Files whose frames stand between the DSL and the author: the
#: `contextlib.contextmanager` wrapper of `kt.when` / `kt.sticky`.
FOREIGN = (contextlib.__file__,)


class DSLError(ValueError):
    """A kernel that cannot be traced as written, and why."""


class TracerControlFlowError(DSLError):
    """A traced value asked a question only the machine can answer."""


@functools.cache
def _inside(filename: str) -> bool:
    """Whether a code object's file is in this package or a `FOREIGN` file
    (resolved once a file: a trace asks for every op it records)."""
    try:
        path = pathlib.Path(filename).resolve()
        return path.is_relative_to(_HERE) or any(
            path == pathlib.Path(f).resolve() for f in FOREIGN
        )
    except OSError:
        return False


@functools.cache
def _basename(filename: str) -> str:
    return pathlib.Path(filename).name


def at_caller() -> str:
    """``file:line`` of the innermost frame outside this package (the
    kernel author's line)."""
    f = sys._getframe(1)
    while f is not None:
        if not _inside(f.f_code.co_filename):
            return f"{_basename(f.f_code.co_filename)}:{f.f_lineno}"
        f = f.f_back
    return "<unknown>"


def fail(message: str) -> DSLError:
    """A `DSLError` carrying the author's line."""
    return DSLError(f"{message}, at {at_caller()}")


__all__ = ["DSLError", "TracerControlFlowError", "at_caller", "fail"]
