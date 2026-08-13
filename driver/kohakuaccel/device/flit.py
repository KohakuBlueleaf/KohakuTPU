"""Building and decoding flits.

Header fields count DOWN from the flit's most significant bit --
``{DX, DY, SX, SY, TYPE, ID, LAST, ...}`` -- so they are assembled that way here
rather than from a struct that happens to match.
"""

from kohakuaccel.device.registers import (
    CUD_MAX_WORDS,
    FLIT_BITS,
    POS_BITS,
    T_CU_CTRL,
    T_CU_DATA,
)
from kohakuaccel.transport.base import MASK32


def field(flit: int, hi: int, width: int) -> int:
    """Verilog's ``flit[hi -: width]``, counting from the MSB as the RTL does."""
    return (flit >> (hi - width + 1)) & ((1 << width) - 1)


def _pack(parts) -> int:
    """Lay ``(value, width)`` pairs down from the flit's most significant bit."""
    flit, at = 0, FLIT_BITS
    for value, width in parts:
        at -= width
        flit |= (value & ((1 << width) - 1)) << at
    return flit


def header(ty: int, txn: int, last: bool, dst=None, src=None) -> int:
    """The 32-bit routing header.

    `dst` and `src` default to zero because the DISPATCHER stamps both: it
    overwrites destination from PROG_DST and source with its own coordinates,
    and leaves type, txn, last and payload exactly as staged. Pass them only for
    the raw mailbox path, which stamps nothing.
    """
    dst = dst or (0, 0)
    src = src or (0, 0)
    return _pack(
        [
            (dst[0], POS_BITS),
            (dst[1], POS_BITS),
            (src[0], POS_BITS),
            (src[1], POS_BITS),
            (ty, 4),
            (txn, 8),
            (1 if last else 0, 1),
            (0, 3),
        ]
    )


def ctrl_request(dst, src, idx: int, txn: int = 1) -> int:
    """A CU_CTRL read flit: ask node `dst` for control register `idx`.

    `src` is where the reply goes, which for a host-driven read is the agent.
    """
    return _pack(
        [
            (dst[0], POS_BITS),
            (dst[1], POS_BITS),
            (src[0], POS_BITS),
            (src[1], POS_BITS),
            (T_CU_CTRL, 4),
            (txn, 8),
            (1, 1),  # LAST
            (0, 3),
            (0, 8),  # opcode; the unit reads the index and answers regardless
            (idx, 8),
        ]
    )


def ctrl_reply(flit: int) -> dict:
    """Decode a CU_CTRL read response: who answered, which register, what value."""
    return {
        "src": (field(flit, 279, POS_BITS), field(flit, 275, POS_BITS)),
        "type": field(flit, 271, 4),
        "txn": field(flit, 267, 8),
        "op": field(flit, 255, 8),
        "idx": field(flit, 247, 8),
        "value": field(flit, 239, 64),
    }


def cu_data_flits(
    words, buf_id=0, offset=0, signal=True, txn=0, ack=None, dst=None, src=None
):
    """A whole CU_DATA burst: the descriptor flit, then one flit per word.

    `words` is a sequence of 256-bit payloads, at most :data:`CUD_MAX_WORDS`.
    `offset` is the destination start in 32-BYTE GRANULES; `buf_id` names the
    destination buffer, which is unit-defined; `signal` sets
    ``signal_on_complete``, which is what makes the transfer observable.
    `ack` routes that completion to an ``(x, y)``; None sends it to the
    descriptor's source, which is useless when the sender is another unit.

    Returns the flits in order, `last` set on the final one. Raises
    :class:`ValueError` if the burst is empty, too long for the 8-bit length
    field, or if a field does not fit -- `ack` included, since ``(0, 0)`` is the
    "reply to the sender" sentinel and no endpoint can live on a mesh corner.
    """
    words = list(words)
    if not words:
        raise ValueError("a CU_DATA burst carries at least one data flit")
    if len(words) > CUD_MAX_WORDS:
        raise ValueError(
            f"{len(words)} words: `len` is 8 bits holding count-1, so a burst "
            f"carries at most {CUD_MAX_WORDS}. Split it."
        )
    for name, value, width in (
        ("buf_id", buf_id, 8),
        ("offset", offset, 16),
        ("txn", txn, 8),
    ):
        if not isinstance(value, int) or value < 0 or value >> width:
            raise ValueError(f"{name} = {value!r} does not fit {width} bits")

    ack_field = 0
    if ack is not None:
        ax, ay = ack
        for name, value in (("ack x", ax), ("ack y", ay)):
            if not isinstance(value, int) or value < 0 or value >> POS_BITS:
                raise ValueError(f"{name} = {value!r} does not fit {POS_BITS} bits")
        if (ax, ay) == (0, 0):
            raise ValueError(
                "ack (0,0) is the 'reply to the sender' sentinel, and no endpoint "
                "can live there -- it is a mesh corner. Pass ack=None for that."
            )
        ack_field = (ay << 4) | ax

    desc = (
        (buf_id << 248)
        | (offset << 232)
        | ((len(words) - 1) << 224)
        | ((1 if signal else 0) << 216)
        | (ack_field << 208)
    )
    out = [header(T_CU_DATA, txn, False, dst, src) | desc]
    for i, w in enumerate(words):
        out.append(
            header(T_CU_DATA, txn, i == len(words) - 1, dst, src)
            | (w & ((1 << 256) - 1))
        )
    return out


def decode_caps(word: int) -> dict:
    """CU_CAPS: what an endpoint says it is, before anything is dispatched to it.

    ``{CU_TYPE[16], CU_VERSION[8], N_BUFFERS[4], INST_DEPTH[16], 20'd0}``.
    """
    kind = (word >> 48) & 0xFFFF
    return {
        "type": kind,
        "name": bytes([(kind >> 8) & 0xFF, kind & 0xFF]).decode("ascii", "replace"),
        "version": (word >> 40) & 0xFF,
        "buffers": (word >> 36) & 0xF,
        "inst_depth": (word >> 20) & 0xFFFF,
    }


def encode_caps(type_code: int, version=0, buffers=1, inst_depth=32) -> int:
    """The CU_CAPS word an endpoint of this type answers with.

    The inverse of :func:`decode_caps`, needed by anything standing in for a
    unit -- a simulated one, or a bench that answers a discovery read.
    """
    return (
        ((type_code & 0xFFFF) << 48)
        | ((version & 0xFF) << 40)
        | ((buffers & 0xF) << 36)
        | ((inst_depth & 0xFFFF) << 20)
    )


def decode_status(word: int) -> dict:
    """CU_STATUS: ``{busy, error, 14'd0, inst_space[16], 32'd0}``.

    `inst_space` is the instruction FIFO's FREE entries. At zero the dispatcher
    is being held off, which is a different problem from a slow datapath and
    looks identical in wall clock.

    The unit's own error flag is `cu_error`, not `error`: a bare `error` key is
    this package's sentinel for a node that did not answer at all, and the two
    colliding made every reachable node look like a failed read.
    """
    return {
        "busy": (word >> 63) & 1,
        "cu_error": (word >> 62) & 1,
        "inst_space": (word >> 32) & 0xFFFF,
    }


def decode_counters(word: int) -> dict:
    """CU_CTRL index 2: ``{instructions_retired[32], busy_cycles[32]}``.

    Counted in ``noc_cu_base``, so every endpoint reports these identically
    whatever it computes -- that is the point of them living there.

    BOTH ACCUMULATE SINCE RESET AND NEITHER CAN BE CLEARED, so a measurement is
    the difference between two reads. They are 32 bits at a few hundred MHz,
    which wraps every ten seconds or so, and differences must be taken modulo
    2**32.
    """
    return {"retired": (word >> 32) & MASK32, "busy_cycles": word & MASK32}
