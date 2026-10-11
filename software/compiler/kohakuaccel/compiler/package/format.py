"""The work package's binary form (docs/spec/package-format.md).

A package is what a compile STORES and a node's dispatcher RUNS: the 256-bit
unit words, the units they go to, the buffers they name, where each buffer's
address sits inside the words (relocations), and the steps. Addresses are
bound at submission, so one package serves every call of a kernel whatever
its operands' addresses are.

The firmware's reader is
``software/firmware/kohakuaccel/include/ka/package/format.h``; the offsets and
codes here are that table.
"""

import enum
import struct
from dataclasses import dataclass, field

MAGIC = 0x4B50414B  # "KAPK"
VERSION = 1
HEADER_BYTES = 96
ALIGN = 32

UNIT_BYTES = 16
BUFFER_BYTES = 32
STEP_BYTES = 16
RELOC_BYTES = 16
PAYLOAD_BYTES = 32

FLAG_CHECKSUM = 1
#: Header word holding the checksum; it reads as zero while the sum is taken.
CHECK_WORD = 10

FNV_BASIS = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3
MASK64 = (1 << 64) - 1

#: A MOVER pair whose register is this is skipped.
MOVER_SKIP = MASK64


class Op(enum.IntEnum):
    """Step opcodes."""

    END = 0
    DISPATCH = 1
    AWAIT = 2
    BARRIER = 3
    MOVER = 4
    RING = 5
    WAIT_BELL = 6
    SIGNAL = 7
    SETTLE = 8
    REPEAT = 9
    MWAIT = 10
    #: Dispatch-engine entries, three a payload (package/engine.py).
    ENGINE = 11
    #: `count` words for `unit`, streamed by its fetch port from the absolute
    #: address `arg` -- words a unit wrote to memory while the package ran.
    FETCH = 12


#: Step flags. A MOVER step POSTED starts its moves and goes on; an MWAIT, a
#: BARRIER or the package's end waits for them.
F_POSTED = 1


class Kind(enum.IntEnum):
    """What a buffer is to the package's caller."""

    INPUT = 0
    OUTPUT = 1
    INOUT = 2
    TEMP = 3
    CONST = 4


class PackageError(ValueError):
    """Bytes that are not a package this reader understands, and why."""


def fnv1a(data: bytes, h: int = FNV_BASIS) -> int:
    """FNV-1a 64 over `data`."""
    for b in data:
        h = ((h ^ b) * FNV_PRIME) & MASK64
    return h


def unit_word(type_code: int, x: int, y: int, mesh: int = 0) -> int:
    """A unit as one word: ``type << 32 | mesh << 16 | y << 8 | x``."""
    return (type_code & 0xFFFF) << 32 | (mesh & 0xFF) << 16 | (y & 0xFF) << 8 | x & 0xFF


def type_code(name: str) -> int:
    """Two ASCII characters -> the 16-bit CU_TYPE (``'MG'`` is 0x4D47)."""
    raw = name.encode("ascii")
    if len(raw) != 2:
        raise ValueError(f"a unit type is exactly two ASCII characters, got {name!r}")
    return raw[0] << 8 | raw[1]


def signature(unit_words) -> int:
    """The machine signature: FNV-1a 64 over the SORTED unit words, 8 bytes each.

    The firmware computes the same over its boot unit table; a package carrying
    a different nonzero signature is refused.
    """
    return fnv1a(b"".join(struct.pack("<Q", w) for w in sorted(unit_words)))


@dataclass(frozen=True)
class Unit:
    """A unit the package dispatches to. `credit` bounds its in-flight count.

    `fetch` names the memory port that streams this unit's DISPATCH words from
    the package itself, as ``1 << 16 | y << 8 | x``; 0 sends them through the
    node's mailbox.
    """

    type: int
    x: int
    y: int
    mesh: int = 0
    credit: int = 0
    fetch: int = 0

    @property
    def word(self) -> int:
        return unit_word(self.type, self.x, self.y, self.mesh)

    @property
    def coord(self) -> tuple[int, int]:
        return (self.x, self.y)


@dataclass(frozen=True)
class Buffer:
    """A memory range the words address. `default` is used when unbound."""

    name: str
    size: int
    default: int = 0
    kind: Kind = Kind.TEMP
    mesh: int = 0


@dataclass(frozen=True)
class Reloc:
    """Bits ``[bit, bit+width)`` of payload `payload` := ``(bind + addend) >> shift``."""

    payload: int
    bit: int
    width: int
    buffer: int
    addend: int
    shift: int = 0

    def check(self) -> None:
        if not 0 < self.width <= 64 or self.bit + self.width > 256:
            raise PackageError(
                f"relocation field [{self.bit}+{self.width}] leaves 256 bits"
            )
        if not 0 <= self.shift < 64:
            raise PackageError(f"relocation shift {self.shift} is not 0..63")
        if not 0 <= self.buffer < 256:
            raise PackageError(f"relocation buffer {self.buffer} is not 0..255")


@dataclass(frozen=True)
class Step:
    """One step: an opcode, a unit (or mesh), a count, a 64-bit argument."""

    op: Op
    unit: int = 0
    count: int = 0
    arg: int = 0
    flags: int = 0


@dataclass
class Package:
    """A package in memory. :meth:`to_bytes` and :meth:`from_bytes` are the wire form."""

    units: list[Unit] = field(default_factory=list)
    buffers: list[Buffer] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    relocs: list[Reloc] = field(default_factory=list)
    payloads: list[int] = field(default_factory=list)
    signature: int = 0
    #: Completions that may arrive from units this package never sent to.
    ack_reserve: int = 0
    checksum: bool = False
    #: Buffer names, kept for the host; the wire form carries only their hashes.
    meta: dict = field(default_factory=dict)

    # ---------------------------------------------------------------- write
    def to_bytes(self) -> bytes:
        """The wire form, a multiple of 32 bytes. Raises :class:`PackageError`."""
        self._check()
        sections = [
            b"".join(
                struct.pack(
                    "<QQ",
                    u.word,
                    u.credit & 0xFFFF_FFFF | (u.fetch & 0xFFFF_FFFF) << 32,
                )
                for u in self.units
            ),
            b"".join(
                struct.pack(
                    "<QQQQ",
                    fnv1a(b.name.encode()),
                    b.size,
                    b.default,
                    int(b.kind) | (b.mesh & 0xFF) << 8,
                )
                for b in self.buffers
            ),
            b"".join(
                struct.pack(
                    "<QQ",
                    (s.op & 0xFF)
                    | (s.flags & 0xFF) << 8
                    | (s.unit & 0xFFFF) << 16
                    | (s.count & 0xFFFF_FFFF) << 32,
                    s.arg & MASK64,
                )
                for s in self.steps
            ),
            b"".join(
                struct.pack(
                    "<QQ",
                    r.payload
                    | r.bit << 32
                    | r.width << 40
                    | r.shift << 48
                    | r.buffer << 56,
                    r.addend & MASK64,
                )
                for r in sorted(self.relocs, key=lambda r: (r.payload, r.bit))
            ),
            b"".join(
                (p & ((1 << 256) - 1)).to_bytes(32, "little") for p in self.payloads
            ),
        ]
        offs, at = [], HEADER_BYTES
        for s in sections:
            offs.append(at)
            at += -(-len(s) // ALIGN) * ALIGN
        total = at
        flags = FLAG_CHECKSUM if self.checksum else 0
        head = struct.pack(
            "<12Q",
            MAGIC | VERSION << 32 | HEADER_BYTES << 48,
            self.signature & MASK64,
            total | flags << 32,
            len(self.payloads) | len(self.relocs) << 32,
            len(self.buffers)
            | len(self.steps) << 16
            | len(self.units) << 32
            | (self.ack_reserve & 0xFFFF) << 48,
            *offs,
            0,
            0,
        )
        out = bytearray(head)
        for s, o in zip(sections, offs, strict=True):
            out += bytes(o - len(out))
            out += s
        out += bytes(total - len(out))
        if self.checksum:
            out[CHECK_WORD * 8 : CHECK_WORD * 8 + 8] = struct.pack("<Q", checksum(out))
        return bytes(out)

    def _check(self) -> None:
        if (
            len(self.units) > 0xFFFF
            or len(self.buffers) > 0xFFFF
            or len(self.steps) > 0xFFFF
        ):
            raise PackageError("a count does not fit its 16-bit header field")
        for r in self.relocs:
            r.check()
            if not 0 <= r.payload < len(self.payloads):
                raise PackageError(
                    f"relocation names payload {r.payload} of {len(self.payloads)}"
                )
            if r.buffer >= len(self.buffers):
                raise PackageError(
                    f"relocation names buffer {r.buffer} of {len(self.buffers)}"
                )
        for i, s in enumerate(self.steps):
            if s.op in (Op.DISPATCH, Op.MOVER) and s.arg + s.count > len(self.payloads):
                raise PackageError(f"step {i} reads payloads past {len(self.payloads)}")
            if s.op == Op.ENGINE and s.arg + -(-s.count // 3) > len(self.payloads):
                raise PackageError(f"step {i} reads payloads past {len(self.payloads)}")
            if s.op == Op.REPEAT:
                first, incs = s.arg & 0xFFFF_FFFF, s.arg >> 48
                if first + s.count + (incs + 1) // 2 > len(self.payloads):
                    raise PackageError(
                        f"step {i} reads payloads past {len(self.payloads)}"
                    )
            if s.op in (Op.DISPATCH, Op.REPEAT, Op.AWAIT, Op.FETCH) and s.unit >= len(
                self.units
            ):
                raise PackageError(f"step {i} names unit {s.unit} of {len(self.units)}")
            if s.op == Op.FETCH and not self.units[s.unit].fetch >> 16:
                raise PackageError(
                    f"step {i} fetches for unit {s.unit}: it has no port"
                )

    # ----------------------------------------------------------------- read
    @classmethod
    def from_bytes(cls, data: bytes) -> "Package":
        """Parse the wire form. Raises :class:`PackageError` on anything malformed."""
        if len(data) < HEADER_BYTES:
            raise PackageError(f"{len(data)} bytes is shorter than the header")
        h = struct.unpack_from("<12Q", data)
        if h[0] & 0xFFFF_FFFF != MAGIC:
            raise PackageError(f"magic {h[0] & 0xFFFF_FFFF:#x} is not KAPK")
        if (h[0] >> 32) & 0xFFFF != VERSION or h[0] >> 48 != HEADER_BYTES:
            raise PackageError(f"version/header {h[0] >> 32:#x} is not this reader's")
        total, flags = h[2] & 0xFFFF_FFFF, h[2] >> 32
        if total > len(data):
            raise PackageError(f"header says {total} bytes, {len(data)} present")
        npay, nrel = h[3] & 0xFFFF_FFFF, h[3] >> 32
        nbuf, nstep, nunit = h[4] & 0xFFFF, (h[4] >> 16) & 0xFFFF, (h[4] >> 32) & 0xFFFF
        ou, ob, os_, orl, op = h[5:10]
        for off, n, size in (
            (ou, nunit, UNIT_BYTES),
            (ob, nbuf, BUFFER_BYTES),
            (os_, nstep, STEP_BYTES),
            (orl, nrel, RELOC_BYTES),
            (op, npay, PAYLOAD_BYTES),
        ):
            if off + n * size > total:
                raise PackageError(f"a section at {off:#x} runs past {total} bytes")
        if flags & FLAG_CHECKSUM and checksum(data[:total]) != h[CHECK_WORD]:
            raise PackageError("checksum mismatch")
        units = []
        for i in range(nunit):
            w, credit = struct.unpack_from("<QQ", data, ou + i * UNIT_BYTES)
            units.append(
                Unit(
                    (w >> 32) & 0xFFFF,
                    w & 0xFF,
                    (w >> 8) & 0xFF,
                    (w >> 16) & 0xFF,
                    credit & 0xFFFF_FFFF,
                    credit >> 32,
                )
            )
        buffers = []
        for i in range(nbuf):
            name, size, default, kind = struct.unpack_from(
                "<QQQQ", data, ob + i * BUFFER_BYTES
            )
            buffers.append(
                Buffer(
                    f"#{name:016x}",
                    size,
                    default,
                    Kind(kind & 0xFF),
                    (kind >> 8) & 0xFF,
                )
            )
        steps = []
        for i in range(nstep):
            w, arg = struct.unpack_from("<QQ", data, os_ + i * STEP_BYTES)
            steps.append(
                Step(Op(w & 0xFF), (w >> 16) & 0xFFFF, w >> 32, arg, (w >> 8) & 0xFF)
            )
        relocs = []
        for i in range(nrel):
            w, add = struct.unpack_from("<QQ", data, orl + i * RELOC_BYTES)
            relocs.append(
                Reloc(
                    w & 0xFFFF_FFFF,
                    (w >> 32) & 0xFF,
                    (w >> 40) & 0xFF,
                    w >> 56,
                    add - (1 << 64) if add >> 63 else add,
                    (w >> 48) & 0xFF,
                )
            )
        payloads = [
            int.from_bytes(
                data[op + i * PAYLOAD_BYTES : op + (i + 1) * PAYLOAD_BYTES], "little"
            )
            for i in range(npay)
        ]
        return cls(
            units=units,
            buffers=buffers,
            steps=steps,
            relocs=relocs,
            payloads=payloads,
            signature=h[1],
            ack_reserve=h[4] >> 48,
            checksum=bool(flags & FLAG_CHECKSUM),
        )

    # ---------------------------------------------------------------- views
    def bound(self, bindings=None) -> list[int]:
        """Payloads with every relocation applied; unbound buffers take their default."""
        addrs = bind_addresses(self.buffers, bindings)
        out = list(self.payloads)
        for r in self.relocs:
            out[r.payload] = apply_reloc(out[r.payload], r, addrs[r.buffer])
        return out

    def summary(self) -> str:
        ops = {}
        for s in self.steps:
            ops[s.op.name] = ops.get(s.op.name, 0) + 1
        mix = " ".join(f"{k}={v}" for k, v in sorted(ops.items()))
        return (
            f"package: {len(self.units)} units, {len(self.payloads)} payloads, "
            f"{len(self.relocs)} relocations over {len(self.buffers)} buffers, "
            f"{len(self.steps)} steps ({mix})"
        )


def checksum(data: bytes) -> int:
    """FNV-1a 64 over `data` with the checksum word read as zero."""
    lo, hi = CHECK_WORD * 8, CHECK_WORD * 8 + 8
    return fnv1a(bytes(data[:lo]) + bytes(8) + bytes(data[hi:]))


def bind_addresses(buffers, bindings=None) -> list[int]:
    """Each buffer's address: its binding when one is given and nonzero, else its default."""
    got = list(bindings or [])
    return [
        (got[i] if i < len(got) and got[i] else b.default)
        for i, b in enumerate(buffers)
    ]


def apply_reloc(payload: int, r: Reloc, base: int) -> int:
    """`payload` with `r`'s field set from `base`."""
    mask = (1 << r.width) - 1
    v = ((base + r.addend) >> r.shift) & mask
    return (payload & ~(mask << r.bit)) | (v << r.bit)
