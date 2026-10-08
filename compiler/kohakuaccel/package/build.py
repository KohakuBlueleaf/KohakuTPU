"""Building a package: from a compile's artifacts, or step by step.

The builder owns the unit, buffer and payload tables and the step list; an
address field inside a live span becomes a relocation (package-format.md §2).

    b = PackageBuilder(types={(1, 1): "MG"}, credit=512)
    b.spans_from({0x40_0000: 4096})              # live spans: base -> size
    b.artifact(result.artifact, addresses=backend.addresses)
    pkg = b.build()
"""

from collections.abc import Callable, Iterable

from kohakuaccel.artifact import Artifact, Await, Barrier, Kick, SeedCredits
from kohakuaccel.package.format import (
    MOVER_SKIP,
    Buffer,
    Kind,
    Op,
    Package,
    PackageError,
    Reloc,
    Step,
    Unit,
    type_code,
)

#: ``((bit, width, shift), ...)``: payload bits ``[bit, bit+width)`` hold
#: ``address >> shift``. A split field is two segments.
Segments = tuple[tuple[int, int, int], ...]

#: A backend's report on one payload of one unit type: every address field and
#: its value (`Backend.addresses`).
AddressFn = Callable[[int, str], Iterable[tuple[Segments, int]]]


def type_name(code: int) -> str:
    """0x4D47 -> 'MG'."""
    return bytes(((code >> 8) & 0xFF, code & 0xFF)).decode("ascii", "replace")


class PackageBuilder:
    """Accumulates units, buffers, payloads and steps into one :class:`Package`.

    `types` maps a coordinate to its unit type name ("MG"), so a completion
    from a unit the package only AWAITs -- a peer acknowledging a transfer --
    is still attributed. `credit` is the in-flight bound a unit is given (its
    instruction FIFO); the firmware further bounds it by the mailbox's depth.
    """

    def __init__(
        self,
        types: dict | None = None,
        credit: int = 32,
        signature: int = 0,
        mesh: int = 0,
    ) -> None:
        self.types = dict(types or {})
        self.credit = credit
        self.signature = signature
        self.mesh = mesh
        self.units: list[Unit] = []
        self.buffers: list[Buffer] = []
        self.payloads: list[int] = []
        self.relocs: list[Reloc] = []
        self.steps: list[Step] = []
        self.ack_reserve = 0
        #: Live spans the address fields are matched against, sorted by base.
        self.spans: list[tuple[int, int]] = []
        self._buf: dict[str, int] = {}
        self._span_buf: dict[int, int] = {}
        self._unit: dict[tuple[int, int, int], int] = {}

    # ---------------------------------------------------------------- tables
    def unit(self, coord, type_name: str | None = None, mesh: int | None = None) -> int:
        """The index of the unit at `coord`, adding it on first use.

        Raises :class:`PackageError` for a coordinate with no known type.
        """
        m = self.mesh if mesh is None else mesh
        key = (m, coord[0], coord[1])
        got = self._unit.get(key)
        if got is not None:
            return got
        name = type_name or self.types.get(tuple(coord))
        if name is None:
            raise PackageError(f"no unit type is known for {tuple(coord)}")
        self.units.append(Unit(type_code(name), coord[0], coord[1], m, self.credit))
        self._unit[key] = len(self.units) - 1
        return self._unit[key]

    def buffer(
        self, name: str, size: int, default: int = 0, kind: Kind = Kind.TEMP
    ) -> int:
        """The index of buffer `name`, adding it on first use."""
        got = self._buf.get(name)
        if got is not None:
            return got
        self.buffers.append(Buffer(name, size, default, kind, self.mesh))
        self._buf[name] = len(self.buffers) - 1
        return self._buf[name]

    def spans_from(self, live: dict) -> None:
        """Make every ``{base: size}`` span a candidate buffer for address
        fields; a span becomes a buffer, in first-reached order, when one does."""
        self.spans = sorted(live.items())

    def _span_of(self, value: int) -> tuple[int, int] | None:
        """(base, size) of the live span holding `value`, or None."""
        lo, hi = 0, len(self.spans)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.spans[mid][0] <= value:
                lo = mid + 1
            else:
                hi = mid
        if lo:
            base, size = self.spans[lo - 1]
            if value < base + size:
                return base, size
        return None

    # -------------------------------------------------------------- payloads
    def payload(
        self, word: int, addresses: AddressFn | None = None, unit_type: str = ""
    ) -> int:
        """Append one 256-bit word; relocate every address field the backend
        reports that lands in a known span. Returns the payload's index."""
        idx = len(self.payloads)
        word &= (1 << 256) - 1
        for segments, value in (addresses(word, unit_type) if addresses else ()):
            span = self._span_of(value)
            if span is None:
                continue
            base, size = span
            b = self._span_buf.get(base)
            if b is None:
                b = self.buffer(f"buf{len(self.buffers)}", size, base)
                self._span_buf[base] = b
            for bit, width, shift in segments:
                self.relocs.append(Reloc(idx, bit, width, b, value - base, shift))
                word &= ~(((1 << width) - 1) << bit)
        self.payloads.append(word)
        return idx

    # ------------------------------------------------------------------ steps
    def dispatch(self, unit: int, words, addresses: AddressFn | None = None) -> None:
        first = len(self.payloads)
        kind = type_name(self.units[unit].type)
        for w in words:
            self.payload(w, addresses, kind)
        if len(self.payloads) > first:
            self.steps.append(
                Step(Op.DISPATCH, unit, len(self.payloads) - first, first)
            )

    def await_(self, unit: int, count: int) -> None:
        if count:
            self.steps.append(Step(Op.AWAIT, unit, count))

    def barrier(self) -> None:
        if self.steps and self.steps[-1].op != Op.BARRIER:
            self.steps.append(Step(Op.BARRIER))

    def signal(self, value32: int = 0, value64: int = 0) -> None:
        self.steps.append(Step(Op.SIGNAL, 0, value32, value64))

    def settle(self, cycles: int) -> None:
        self.steps.append(Step(Op.SETTLE, 0, cycles))

    def ring(self, mesh: int, tag: int = 0) -> None:
        self.steps.append(Step(Op.RING, mesh, tag & 0xFF))

    def wait_bell(self, mesh: int, count: int = 1) -> None:
        self.steps.append(Step(Op.WAIT_BELL, mesh, count))

    def mover(self, writes, addresses: AddressFn | None = None) -> None:
        """Mover register writes ``[(reg, value), ...]``, issued in order; the
        step waits for every move a GO in them starts. Two pairs per payload."""
        pairs = list(writes)
        if len(pairs) % 2:
            pairs.append((MOVER_SKIP, 0))
        first = len(self.payloads)
        for k in range(0, len(pairs), 2):
            (r0, v0), (r1, v1) = pairs[k], pairs[k + 1]
            self.payload(r0 | v0 << 64 | r1 << 128 | v1 << 192, addresses, "mover")
        self.steps.append(Step(Op.MOVER, 0, len(self.payloads) - first, first))

    # ---------------------------------------------------------------- artifacts
    def artifact(self, art: Artifact, addresses: AddressFn | None = None) -> None:
        """One compile's artifact: each kick a DISPATCH of its slots, each await
        an AWAIT, each barrier a BARRIER. Seeds are dropped: credit is the
        firmware's, per unit. A round awaiting more than it kicked on some unit
        reserves that much mailbox room for acknowledgements."""
        kicked: dict[int, int] = {}
        owed: dict[int, int] = {}
        for s in art.steps:
            if isinstance(s, SeedCredits):
                continue
            if isinstance(s, Kick):
                u = self.unit(s.coord)
                self.dispatch(u, art.flits[s.base : s.base + s.nflits], addresses)
                kicked[u] = kicked.get(u, 0) + s.nflits
            elif isinstance(s, Await):
                u = self.unit(s.coord)
                self.await_(u, s.count)
                owed[u] = owed.get(u, 0) + s.count
            elif isinstance(s, Barrier):
                self._reserve(kicked, owed)
                kicked, owed = {}, {}
                self.barrier()
            else:
                raise PackageError(f"an artifact step this builder cannot lower: {s!r}")
        self._reserve(kicked, owed)

    def _reserve(self, kicked: dict, owed: dict) -> None:
        extra = sum(max(0, n - kicked.get(u, 0)) for u, n in owed.items())
        self.ack_reserve = max(self.ack_reserve, extra)

    # ------------------------------------------------------------------ build
    def build(self, defaults: bool = True, checksum: bool = False) -> Package:
        """The package. `defaults=False` clears every buffer's default address,
        so it runs only with bindings and its bytes name no address at all."""
        bufs = self.buffers
        if not defaults:
            bufs = [Buffer(b.name, b.size, 0, b.kind, b.mesh) for b in bufs]
        return Package(
            units=list(self.units),
            buffers=list(bufs),
            steps=list(self.steps),
            relocs=list(self.relocs),
            payloads=list(self.payloads),
            signature=self.signature,
            ack_reserve=self.ack_reserve,
            checksum=checksum,
            meta={"buffers": [b.name for b in self.buffers]},
        )

    def bindings(self) -> list[int]:
        """Each buffer's address as recorded: what to bind a no-defaults build to."""
        return [b.default for b in self.buffers]

    def __len__(self) -> int:
        return len(self.steps)
