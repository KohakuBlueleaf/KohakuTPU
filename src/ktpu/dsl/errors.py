"""What the DSL refuses, and how it says where the refusal happened.

`TracerControlFlowError` is the one that matters. A tracer reaching `__bool__`
must raise rather than pick a branch: picking one produces a graph that is right
for the input that happened to be traced and wrong for the next, silently, with
no artefact anywhere recording that a choice was made. That is the failure mode
which makes naive tracers untrustworthy, and refusing it is why
docs/driver/dsl.md s2 is a rule rather than a caveat.

An error here is only useful if it names the line that caused it, and no line
inside this package ever did -- the guilty line is in the kernel. `at_caller`
therefore walks out of `ktpu/dsl/` before reporting, the same way
`warnings.warn` uses `stacklevel`.
"""

import inspect
import linecache
import os
from types import FrameType

from ktpu.ir import DTypeError, ShapeError

_PACKAGE_DIR = os.path.normcase(os.path.dirname(os.path.abspath(__file__)))


class DSLError(Exception):
    """Something the tracing frontend will not do.

    Deliberately NOT a subclass of `TypeError`: numpy and several stdlib
    protocols catch `TypeError` to mean "try something else", and a refusal that
    gets swallowed into a fallback path is a refusal that did not happen.
    """


class TracerControlFlowError(DSLError):
    """A traced value was asked for something only its contents could answer.

    `if x > 0`, `len(x)`, `for v in x`, `lst[x]`, `float(x)` -- each needs the
    numbers, and the numbers do not exist until the machine runs. The fix is
    always the same shape: make the choice a value (`where`, `maximum`) or make
    it compile-time (a plain Python int, which is free and unrolls).
    """


class TracerShapeError(DSLError, ShapeError):
    """A level-1 shape disagreement, with the kernel line that caused it.

    Inherits `ShapeError` so the graph's own callers keep catching it, and
    `DSLError` so `except DSLError` covers the whole frontend. The message is the
    graph's, with a location appended -- raising it where it happened is the
    single largest usability difference between the DSL and building IR by hand
    (docs/driver/dsl.md s4).
    """


class TracerDTypeError(DSLError, DTypeError):
    """Operand formats with no common form, at the kernel line that mixed them.

    Separate from `TracerShapeError` only because the IR's own two errors are
    separate -- `ShapeError` is a `ValueError` and `DTypeError` a `TypeError`, and
    collapsing them here would change what a caller can catch.
    """


def user_frame() -> FrameType | None:
    """The innermost frame outside this package.

    Everything in `ktpu/dsl/` is machinery; the line worth printing belongs to
    whoever called it. Kernels shipped in `library.py` live here too, so an error
    raised inside `softmax` reports the call site -- which is right, because from
    the caller's side `softmax` is part of the DSL.
    """
    frame = inspect.currentframe()
    while frame is not None and _is_dsl(frame.f_code.co_filename):
        frame = frame.f_back
    return frame


def at_caller() -> str:
    """`path:line` and the source text of the kernel line now executing."""
    frame = user_frame()
    if frame is None:
        return "<unknown location>"
    path, lineno = frame.f_code.co_filename, frame.f_lineno
    text = linecache.getline(path, lineno).strip()
    head = f"{path}:{lineno}"
    return f"{head}\n    {text}" if text else head


def _is_dsl(path: str) -> bool:
    # Frames from `exec` and the REPL have names like "<string>", which resolve
    # to the cwd and so count as user code -- which is what they are.
    return os.path.normcase(os.path.dirname(os.path.abspath(path))) == _PACKAGE_DIR
