"""Building a package step by step.

The builder owns the unit, buffer and payload tables and the step list; an
address field inside a live span becomes a relocation (package-format.md §2).

    b = PackageBuilder(types={(1, 1): "MG"}, credit=512)
    b.spans_from({0x40_0000: 4096})              # live spans: base -> size
    b.dispatch(b.unit((1, 1)), words, addresses=project.addresses)
    b.await_(0, len(words))
    pkg = b.build()
"""

from collections.abc import Callable, Iterable

from kohakuaccel.compiler.package.format import (
    F_POSTED,
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

#: A project's report on one payload of one unit type: every address field
#: and its value.
AddressFn = Callable[[int, str], Iterable[tuple[Segments, int]]]


def _field(word: int, bit: int, width: int) -> int:
    return (word >> bit) & ((1 << width) - 1)


#: The longest template :func:`_periodic` tries: a K step is 3 words, a vector
#: RUN a handful, and the search is quadratic in the kick's length.
MAX_PERIOD = 16


def _periodic(words: list[int], fields: list) -> tuple | None:
    """``(head, period, repeats, increments)`` of the run saving the most
    payload slots, or None when no run saves any.

    A run repeats `words[head:head+period]` with every bit outside `fields`
    equal and each field stepping by one constant per repetition.
    """
    rest = (1 << 256) - 1
    for bit, width in fields:
        rest &= ~(((1 << width) - 1) << bit)
    best, saved = None, 0
    n = len(words)
    for period in range(1, min(MAX_PERIOD, n // 2) + 1):
        for head in range(n - 2 * period + 1):
            if (words[head] ^ words[head + period]) & rest:
                continue
            incs = []
            for j in range(period):
                a, b = words[head + j], words[head + period + j]
                if (a ^ b) & rest:
                    break
                for bit, width in fields:
                    d = _field(b, bit, width) - _field(a, bit, width)
                    if d < 0:
                        break
                    if d:
                        incs.append((j, bit, width, d))
                else:
                    continue
                break
            else:
                repeats = 2
                while head + (repeats + 1) * period <= n:
                    base = head + repeats * period
                    ok = all(
                        not (words[base + j] ^ words[head + j]) & rest
                        for j in range(period)
                    )
                    for j in range(period):
                        for bit, width in fields:
                            want = _field(words[head + j], bit, width) + repeats * next(
                                (d for jj, bb, _, d in incs if jj == j and bb == bit), 0
                            )
                            ok = ok and _field(words[base + j], bit, width) == want
                    if not ok:
                        break
                    repeats += 1
                gain = (repeats - 1) * period - (len(incs) + 1) // 2
                if gain > saved:
                    best, saved = (head, period, repeats, incs), gain
    return best


def address_fields(words, addresses: AddressFn | None, unit_type: str) -> list:
    """Every ``(bit, width)`` address segment `addresses` reports in `words`."""
    if addresses is None:
        return []
    out = set()
    for w in words:
        for segments, _ in addresses(w & ((1 << 256) - 1), unit_type):
            out.update((bit, width) for bit, width, _ in segments)
    return sorted(out)


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
        fetch: tuple | None = None,
    ) -> None:
        self.types = dict(types or {})
        self.credit = credit
        #: The memory port every unit's DISPATCH words stream from, or None.
        self.fetch = fetch
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
        port = 0 if self.fetch is None else 1 << 16 | self.fetch[1] << 8 | self.fetch[0]
        self.units.append(
            Unit(type_code(name), coord[0], coord[1], m, self.credit, port)
        )
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
        """Append one 256-bit word; relocate every address field `addresses`
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

    def repeat(
        self,
        unit: int,
        template,
        repeats: int,
        increments,
        addresses: AddressFn | None = None,
    ) -> None:
        """`template` sent `repeats` times; repetition r adds r * delta to each
        ``(index, bit, width, delta)`` field (package-format.md §4.6).

        Raises :class:`PackageError` for an empty template, a field outside the
        word, or a field the last repetition would wrap.
        """
        template, incs = list(template), sorted(increments)
        if not template or not 1 <= repeats <= 0xFFFF or len(incs) > 0xFFFF:
            raise PackageError(f"a REPEAT of {len(template)} words x {repeats}")
        for index, bit, width, delta in incs:
            field = (template[index] >> bit) & ((1 << width) - 1)
            if not 0 < width <= 64 or bit + width > 256:
                raise PackageError(f"increment field [{bit}, +{width}) leaves the word")
            if field + (repeats - 1) * delta >= 1 << width or delta < 0:
                raise PackageError(
                    f"word {index}'s field [{bit}, +{width}) wraps within {repeats} "
                    f"repetitions of {delta:+d}"
                )
        first = len(self.payloads)
        kind = type_name(self.units[unit].type)
        for w in template:
            self.payload(w, addresses, kind)
        for k in range(0, len(incs), 2):
            word = 0
            for half, (index, bit, width, delta) in enumerate(incs[k : k + 2]):
                word |= (index | bit << 32 | width << 40) << (128 * half)
                word |= delta << (128 * half + 64)
            self.payloads.append(word)
        arg = first | repeats << 32 | len(incs) << 48
        self.steps.append(Step(Op.REPEAT, unit, len(template), arg))

    def dispatch_periodic(
        self, unit: int, words, fields, addresses: AddressFn | None = None
    ) -> None:
        """`words` as DISPATCH, REPEAT, DISPATCH: the longest run that repeats
        with only `fields` (``(bit, width)`` spans) stepping by a constant
        becomes one REPEAT, so a periodic program's size does not grow with its
        length. Words the run does not cover go out as plain DISPATCH."""
        words = [w & ((1 << 256) - 1) for w in words]
        best = _periodic(words, list(fields))
        if best is None:
            self.dispatch(unit, words, addresses)
            return
        head, period, repeats, incs = best
        self.dispatch(unit, words[:head], addresses)
        self.repeat(unit, words[head : head + period], repeats, incs, addresses)
        self.dispatch(unit, words[head + period * repeats :], addresses)

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

    def mover(
        self, writes, addresses: AddressFn | None = None, posted: bool = False
    ) -> None:
        """Mover register writes ``[(reg, value), ...]``, issued in order; the
        step waits for every move a GO in them starts -- unless `posted`, when
        an `mwait`, a barrier or the end does. Two pairs per payload."""
        pairs = list(writes)
        if len(pairs) % 2:
            pairs.append((MOVER_SKIP, 0))
        first = len(self.payloads)
        for k in range(0, len(pairs), 2):
            (r0, v0), (r1, v1) = pairs[k], pairs[k + 1]
            self.payload(r0 | v0 << 64 | r1 << 128 | v1 << 192, addresses, "mover")
        self.steps.append(
            Step(
                Op.MOVER,
                0,
                len(self.payloads) - first,
                first,
                F_POSTED if posted else 0,
            )
        )

    def fetch_from(self, unit: int, addr: int, count: int) -> None:
        """`count` words for `unit` read by its fetch port from `addr`, an
        absolute address; they count as words sent to it."""
        if not self.units[unit].fetch >> 16:
            raise PackageError(f"unit {unit} has no fetch port to stream from")
        if count:
            self.steps.append(Step(Op.FETCH, unit, count, addr))

    def mwait(self, moves: int) -> None:
        """Wait until `moves` of the package's moves (its GOs, counted from its
        start) are done."""
        self.steps.append(Step(Op.MWAIT, 0, moves))

    def reserve_acks(self, count: int) -> None:
        """Keep `count` mailbox entries free for acknowledgements a round
        awaits beyond the words it dispatched (a peer's transfer acks)."""
        self.ack_reserve = max(self.ack_reserve, count)

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
