"""Level 3 -> machine code: a cluster instruction becomes a flit payload.

The bit layout is `docs/isa/cluster.md` §2, which is checked against
`mx_cluster_cu.v`'s decode block and `driver/src/kohakutpu/matmul.py`'s `_flit`.
Fields do NOT overlap by opcode: the payload has spare bits and a field whose
meaning depends on the opcode is how a decode bug survives review.

Addresses in a `Program` are 32-BIT WORDS; the ISA's `addr` is a BYTE address.
That conversion happens here and nowhere else.
"""

from ktpu.ir.program import Inst, Opcode

#: (high, low) inclusive bit positions, from isa/cluster.md §2.
FIELDS = {
    "op": (255, 252),
    "addr": (251, 218),
    "n": (217, 202),
    "sel": (201, 201),
    "acc": (200, 200),
    "gm": (199, 192),
    "gn": (191, 184),
    "nk": (183, 176),
    "anchor": (175, 168),
    "peers": (167, 144),
    "npeer": (143, 142),
    "preq": (141, 141),
    "eoff": (140, 133),
    "aoff": (132, 125),
    "boff": (124, 117),
    "emit": (116, 116),
    "fuse": (115, 115),
}

#: A flit is 288 bits: a 32-bit header above the 256-bit payload. `last` is a
#: HEADER bit, not a payload field -- the same bit the memory protocol uses for
#: the final beat of a burst (isa/cluster.md §6).
FLIT_BITS = 288
LAST_BIT = FLIT_BITS - 28 - 1
TYPE_SHIFT = FLIT_BITS - 16 - 4
TXN_SHIFT = FLIT_BITS - 20 - 8

#: `CU_INST`, and the txn the driver stamps on it (isa/cluster.md §2).
CU_INST = 0x5
CU_TXN = 0x40

OPCODE = {Opcode.FILL: 1, Opcode.GEMM: 2, Opcode.DRAIN: 3}

#: `2 * SBIAS`, cancelling both stored block-scale biases (isa/cluster.md §4.3).
#: Written on EVERY opcode, matching the driver: DRAIN's copy is decoded and
#: discarded (§5) but the CU still latches it, so the bytes must agree.
ANCHOR = 40

#: A FILL of 256 entries wraps the memory request's 8-bit streaming count to 0,
#: which memory coerces to 1 -- so the CU waits forever for entries nobody asked
#: for, the result is zeros, and nothing is reported.
MAX_FILL = 255


class EncodeError(ValueError):
    """An instruction that does not fit its ISA fields."""


def place(name: str, value: int) -> int:
    """Shift `value` into `name`'s bit position, checking it fits.

    Raises EncodeError on a negative value or one too wide for the field. That
    check is the point: an unsized value written into a concatenation shifts
    every field below it and still elaborates -- the payload comes out short,
    the CU reads nonsense and executes nothing, with no error anywhere
    (isa/cluster.md §2).
    """
    hi, lo = FIELDS[name]
    width = hi - lo + 1
    if value < 0 or value >> width:
        raise EncodeError(
            f"{name} = {value} does not fit {width} bits [{hi}:{lo}]; "
            "a value too wide here shifts every field below it"
        )
    return value << lo


def flit(inst: Inst, last: bool = False) -> int:
    """Encode one cluster instruction as its 260-bit flit payload.

    `last` sets the header bit that makes the CU report SIG_BATCH_COMPLETE
    instead of SIG_INST_COMPLETE; the driver sets it on the DRAIN that ends a
    pass and nowhere else.

    Returns the integer payload. Raises EncodeError if the opcode is not a
    cluster instruction or a field does not fit.
    """
    if inst.op not in OPCODE:
        raise EncodeError(f"{inst.op.value} is not a cluster instruction")

    f = inst.fields
    out = place("op", OPCODE[inst.op])
    out |= CU_INST << TYPE_SHIFT
    out |= CU_TXN << TXN_SHIFT
    if last:
        out |= 1 << LAST_BIT

    out |= place("nk", f.get("nk", 1))
    out |= place("anchor", f.get("anchor", ANCHOR))

    match inst.op:
        case Opcode.FILL:
            if f["n"] > MAX_FILL:
                raise EncodeError(
                    f"FILL of {f['n']} entries exceeds the 8-bit streaming "
                    "count; the CU would wait forever for entries nobody "
                    "asked for and report nothing"
                )
            out |= place("addr", f["word"] * 4)
            out |= place("n", f["n"])
            out |= place("sel", 1 if f["sel"] == "b" else 0)
            out |= place("eoff", f.get("eoff", 0))
            out |= place("preq", int(f.get("preq", 0)))
        case Opcode.GEMM:
            out |= place("gm", f["gm"])
            out |= place("gn", f["gn"])
            out |= place("acc", f["acc"])
            if "word" in f:
                out |= place("addr", f["word"] * 4)
            out |= place("aoff", f.get("aoff", 0))
            out |= place("boff", f.get("boff", 0))
            out |= place("emit", int(f.get("emit", 0)))
        case Opcode.DRAIN:
            out |= place("addr", f["word"] * 4)
            out |= place("n", f["n"])
            out |= place("fuse", int(f.get("fuse", 0)))
    return out


def decode(payload: int) -> dict:
    """Pull every field back out of a payload. The inverse of `flit`, for
    checking an encoder against a decoder rather than against itself."""
    out = {
        n: (payload >> lo) & ((1 << (hi - lo + 1)) - 1)
        for n, (hi, lo) in FIELDS.items()
    }
    out["last"] = (payload >> LAST_BIT) & 1
    return out


def cluster_words(prog) -> list[tuple[tuple[int, int], int]]:
    """Every cluster instruction of `prog` as `(node, payload)`.

    `last` is set on each node's final DRAIN, which is what ends that node's
    batch. Vector instructions are skipped: their encoding is isa/vector.md's
    32-bit word and is not this format.
    """
    drains = {}
    for i, inst in enumerate(prog.insts):
        if inst.op is Opcode.DRAIN:
            drains[inst.node] = i
    return [
        (inst.node, flit(inst, last=drains.get(inst.node) == i))
        for i, inst in enumerate(prog.insts)
        if inst.op in OPCODE
    ]
