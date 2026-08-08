"""One tracing session: the graph being built, and where its errors are reported.

Split from `tracer.py` because it answers a different question. A `Tracer` is a
value; a `TraceContext` is the session that value belongs to -- which graph it
appends to, which constants it shares, and whether the session is still open.

There is no global "current trace" and no thread-local. Every tracer carries its
context, so an op finds its graph through its operands. That is what makes
mixing two traces a detectable error rather than a graph with an edge into a
value it does not contain, and it is why nesting or threading a trace needs no
special support.
"""

from ktpu.dsl.errors import DSLError, TracerDTypeError, TracerShapeError, at_caller
from ktpu.ir import DType, DTypeError, Graph, ShapeError, Value


class TraceContext:
    """The graph under construction, plus its constant pool."""

    def __init__(self, name: str = "graph") -> None:
        self.graph = Graph(name)
        self.closed = False
        self._consts: dict[tuple[str, float], Value] = {}

    def const(self, x: float, dtype: DType) -> Value:
        """One value per (number, dtype), reused.

        Without this a constant inside an unrolled loop appears once per
        iteration, so the op count depends on how the kernel was written rather
        than on what it computes -- and two graphs that should be identical no
        longer diff.
        """
        key = (dtype.name, float(x))
        value = self._consts.get(key)
        if value is None:
            value = self._consts[key] = self.emit(self.graph.const, float(x), dtype)
        return value

    def emit(self, build, *args, **kwargs) -> Value:
        """Call a `Graph` builder, reporting its complaints at the kernel's line.

        The graph's own messages are already good; what they cannot know is which
        line of which kernel produced the operands. Appending that is the single
        largest usability difference between tracing and building IR by hand
        (docs/driver/dsl.md s4), and it is the only reason this wrapper exists.
        """
        if self.closed:
            raise DSLError(
                "this tracer escaped the function it was traced in, at "
                f"{at_caller()}\n"
                "  A Tracer is only valid while its kernel is being traced. "
                "Stashing one in a global or a closure and using it later would "
                "append to a graph that has already been returned."
            )
        try:
            return build(*args, **kwargs)
        except ShapeError as exc:
            raise TracerShapeError(f"{exc}\n  at {at_caller()}") from exc
        except DTypeError as exc:
            raise TracerDTypeError(f"{exc}\n  at {at_caller()}") from exc
