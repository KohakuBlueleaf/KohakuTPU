"""Declare an instruction layout once; get encode, decode and disassembly.

Fields are laid MSB-FIRST, counting down from the top of the container, which is
how the RTL reads a flit (``payload[255 -: 32]``).
"""

from dataclasses import dataclass
from dataclasses import field as _field


class ISAError(Exception):
    """A layout is malformed, or a value does not fit the field it was given."""


@dataclass(frozen=True)
class Field:
    """One field of an instruction.

    `const` fixes the value and removes it from :meth:`InstFormat.encode`'s
    arguments; decoding checks it. `signed` stores two's complement. `default`
    supplies a value when the caller omits one.
    """

    name: str
    width: int
    const: int | None = None
    signed: bool = False
    default: int | None = None
    doc: str = ""

    def __post_init__(self) -> None:
        if self.width < 1:
            raise ISAError(f"field {self.name!r} has width {self.width}")
        if self.const is not None:
            self.fit(self.const)

    def limits(self) -> tuple[int, int]:
        """The inclusive range this field can hold."""
        if self.signed:
            half = 1 << (self.width - 1)
            return -half, half - 1
        return 0, (1 << self.width) - 1

    def fit(self, value: int) -> int:
        """`value` as raw bits, or raise :class:`ISAError` saying what fits."""
        if not isinstance(value, int):
            raise ISAError(f"field {self.name!r} takes an int, got {value!r}")
        low, high = self.limits()
        if not low <= value <= high:
            raise ISAError(
                f"field {self.name!r} is {self.width} bits "
                f"({'signed' if self.signed else 'unsigned'}), so it holds "
                f"{low}..{high}; got {value}"
            )
        return value & ((1 << self.width) - 1)

    def unfit(self, raw: int) -> int:
        """Raw bits back to a value, sign-extending a signed field."""
        if self.signed and raw >> (self.width - 1):
            return raw - (1 << self.width)
        return raw


@dataclass
class InstFormat:
    """A named instruction layout inside a `width`-bit container.

    Fields are given MSB-first. Any bits left over are low-order padding and
    decode to nothing, so a format need not describe the whole container.
    """

    name: str
    fields: tuple[Field, ...]
    width: int = 256
    doc: str = ""
    _pos: dict[str, int] = _field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.fields = tuple(self.fields)
        seen: set[str] = set()
        used = 0
        for f in self.fields:
            if f.name in seen:
                raise ISAError(f"{self.name}: field {f.name!r} appears twice")
            seen.add(f.name)
            used += f.width
        if used > self.width:
            raise ISAError(
                f"{self.name}: fields total {used} bits, container is {self.width}"
            )
        at = self.width
        for f in self.fields:
            at -= f.width
            self._pos[f.name] = at
        self.padding = self.width - used

    def by_name(self, name: str) -> Field:
        for f in self.fields:
            if f.name == name:
                return f
        raise ISAError(f"{self.name} has no field {name!r}; it has {self.names()}")

    def names(self) -> list[str]:
        """Every field name, in layout order."""
        return [f.name for f in self.fields]

    def settable(self) -> list[str]:
        """Field names :meth:`encode` accepts: everything without a `const`."""
        return [f.name for f in self.fields if f.const is None]

    def encode(self, **values: int) -> int:
        """Pack `values` into one word.

        Raises :class:`ISAError` on an unknown name, a missing field with no
        default, or a value outside its field.
        """
        unknown = set(values) - set(self.names())
        if unknown:
            raise ISAError(
                f"{self.name}: no field(s) {sorted(unknown)}; it takes "
                f"{self.settable()}"
            )
        word = 0
        for f in self.fields:
            if f.const is not None:
                if f.name in values and values[f.name] != f.const:
                    raise ISAError(
                        f"{self.name}: {f.name!r} is fixed at {f.const}, "
                        f"got {values[f.name]}"
                    )
                raw = f.fit(f.const)
            elif f.name in values:
                raw = f.fit(values[f.name])
            elif f.default is not None:
                raw = f.fit(f.default)
            else:
                raise ISAError(f"{self.name}: field {f.name!r} has no value")
            word |= raw << self._pos[f.name]
        return word

    def decode(self, word: int) -> dict[str, int]:
        """Unpack `word`. Raises if a `const` field disagrees with the layout."""
        out: dict[str, int] = {}
        for f in self.fields:
            raw = (word >> self._pos[f.name]) & ((1 << f.width) - 1)
            if f.const is not None and raw != (f.const & ((1 << f.width) - 1)):
                raise ISAError(
                    f"{self.name}: field {f.name!r} should be {f.const:#x}, "
                    f"word has {raw:#x}"
                )
            out[f.name] = f.unfit(raw)
        return out

    def matches(self, word: int) -> bool:
        """True if every `const` field in `word` agrees with this layout."""
        for f in self.fields:
            if f.const is None:
                continue
            raw = (word >> self._pos[f.name]) & ((1 << f.width) - 1)
            if raw != (f.const & ((1 << f.width) - 1)):
                return False
        return True

    def disasm(self, word: int) -> str:
        """One readable line for `word`."""
        parts = [
            f"{k}={v:#x}" if v >= 0 else f"{k}={v}"
            for k, v in self.decode(word).items()
            if self.by_name(k).const is None
        ]
        return f"{self.name} " + " ".join(parts)

    def layout(self) -> str:
        """A bit map of the format."""
        lines = [f"{self.name}  [{self.width} bits]"]
        for f in self.fields:
            lo = self._pos[f.name]
            hi = lo + f.width - 1
            tag = f" = {f.const:#x}" if f.const is not None else ""
            note = f"  {f.doc}" if f.doc else ""
            lines.append(f"  [{hi:3}:{lo:3}] {f.name}{tag}{note}")
        if self.padding:
            lines.append(f"  [{self.padding - 1:3}:  0] (padding)")
        return "\n".join(lines)


class InstSet:
    """Several formats sharing a container, dispatched by their `const` fields.

    Decoding tries each format's constants in registration order, so a format
    with more fixed bits should be registered before a more general one.
    """

    def __init__(self, name: str, formats=()) -> None:
        self.name = name
        self.formats: list[InstFormat] = []
        for f in formats:
            self.add(f)

    def add(self, fmt: InstFormat) -> InstFormat:
        """Register `fmt`. Raises if the name is taken or the width disagrees."""
        if any(f.name == fmt.name for f in self.formats):
            raise ISAError(f"{self.name}: format {fmt.name!r} is already registered")
        if self.formats and fmt.width != self.formats[0].width:
            raise ISAError(
                f"{self.name}: {fmt.name!r} is {fmt.width} bits, "
                f"{self.formats[0].name!r} is {self.formats[0].width}"
            )
        if not any(f.const is not None for f in fmt.fields):
            raise ISAError(
                f"{self.name}: {fmt.name!r} has no const field, so nothing can "
                f"identify it when decoding"
            )
        self.formats.append(fmt)
        return fmt

    def find(self, word: int) -> InstFormat | None:
        """The first format whose constants match, or None."""
        for fmt in self.formats:
            if fmt.matches(word):
                return fmt
        return None

    def decode(self, word: int) -> tuple[str, dict[str, int]]:
        """``(format name, fields)``. Raises if nothing matches."""
        fmt = self.find(word)
        if fmt is None:
            raise ISAError(f"{self.name}: no format matches {word:#x}")
        return fmt.name, fmt.decode(word)

    def disasm(self, word: int) -> str:
        """One readable line, or a marker if nothing claims the word."""
        fmt = self.find(word)
        return fmt.disasm(word) if fmt else f"<unknown {word:#x}>"

    def layout(self) -> str:
        return "\n\n".join(f.layout() for f in self.formats)

    def __len__(self) -> int:
        return len(self.formats)

    def __iter__(self):
        return iter(self.formats)


def roundtrip(fmt: InstFormat, **values: int) -> dict[str, int]:
    """Encode then decode `values`, returning the decoded fields.

    Raises :class:`ISAError` if a value does not survive the round trip.
    """
    got = fmt.decode(fmt.encode(**values))
    for name, want in values.items():
        if got[name] != want:
            raise ISAError(
                f"{fmt.name}: {name} went in as {want} and came out {got[name]}"
            )
    return got
