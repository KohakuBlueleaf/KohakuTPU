"""L3, the graph: what the work is, before anything decides where it happens.

An op has a domain, reads, writes and a `kind` the framework never interprets.
This is the level a frontend produces.
"""

from dataclasses import dataclass, field

from kohakuaccel.ir.base import Level, Node


@dataclass(frozen=True)
class Domain:
    """An index space, as one extent per axis."""

    extents: tuple[int, ...] = ()

    def size(self) -> int:
        out = 1
        for e in self.extents:
            out *= e
        return out

    def rank(self) -> int:
        return len(self.extents)

    def split(self, axis: int, pieces: int) -> list["Domain"]:
        """Cut `axis` into `pieces` near-equal parts, remainder to the earlier.

        Raises :class:`ValueError` on a bad axis number or fewer than one piece.
        """
        if not 0 <= axis < self.rank():
            raise ValueError(f"axis {axis} outside a rank-{self.rank()} domain")
        if pieces < 1:
            raise ValueError(f"cannot split into {pieces} pieces")
        base, extra = divmod(self.extents[axis], pieces)
        out = []
        for i in range(pieces):
            n = base + (1 if i < extra else 0)
            if n:
                out.append(
                    Domain(self.extents[:axis] + (n,) + self.extents[axis + 1 :])
                )
        return out


@dataclass
class Buffer:
    """A named region of machine memory an op reads or writes.

    `base` is None until allocation runs; :meth:`GraphIR.check` reports any that
    are still unresolved.
    """

    name: str
    nbytes: int
    base: int | None = None

    @property
    def addr(self) -> int:
        """The assigned address.

        Raises :class:`ValueError` if allocation has not run.
        """
        if self.base is None:
            raise ValueError(
                f"buffer {self.name!r} has no address yet; run the allocator"
            )
        return self.base


@dataclass
class Op(Node):
    """One operation over a domain."""

    domain: Domain = field(default_factory=Domain)
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()


class GraphIR(Level):
    """A graph of operations and the buffers they move between."""

    name = "L3 graph"

    def __init__(self) -> None:
        super().__init__()
        self.buffers: dict[str, Buffer] = {}

    def buffer(self, name: str, nbytes: int, base: int | None = None) -> Buffer:
        """Declare a buffer. Raises if the name is taken with a different size."""
        held = self.buffers.get(name)
        if held is not None and held.nbytes != nbytes:
            raise ValueError(
                f"buffer {name!r} is already {held.nbytes} bytes, not {nbytes}"
            )
        buf = self.buffers.setdefault(name, Buffer(name, nbytes, base))
        if base is not None:
            buf.base = base
        return buf

    def op(self, kind: str, domain=None, reads=(), writes=(), label="", **attrs) -> int:
        """Append an operation and return its index."""
        return self.add(
            Op(
                kind=kind,
                attrs=attrs,
                label=label,
                domain=domain or Domain(),
                reads=tuple(reads),
                writes=tuple(writes),
            )
        )

    def infer_edges(self) -> int:
        """Add the edges implied by one op writing a buffer a later op reads.

        Emission order is taken as program order. Returns how many were added.
        """
        before = len(self.edges)
        for i, producer in enumerate(self.nodes):
            for j in range(i + 1, len(self.nodes)):
                consumer = self.nodes[j]
                touched = set(consumer.reads) | set(consumer.writes)
                if set(producer.writes) & touched:
                    self.depends(i, j)
        return len(self.edges) - before

    def check(self) -> list[str]:
        problems = []
        for i, op in enumerate(self.nodes):
            for name in op.reads + op.writes:
                if name not in self.buffers:
                    problems.append(f"op {op.described(i)} uses undeclared {name!r}")
        for name, buf in sorted(self.buffers.items()):
            if buf.base is None:
                problems.append(f"buffer {name!r} was never given an address")
        return problems

    def describe(self, index: int, node: Op) -> str:
        rw = f" <-{list(node.reads)}" if node.reads else ""
        w = f" ->{list(node.writes)}" if node.writes else ""
        return f"{node.described(index)} {node.domain.extents}{rw}{w}"
