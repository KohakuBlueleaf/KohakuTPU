"""A node's log area (include/kos/abi.h KOS_LOG_*): the 64-byte header (byte
count, what the node is doing, a heartbeat), then the bytes, wrapping."""

from image import abi_const

LAND, LOG = abi_const("KOS_LAND_SIZE"), abi_const("KOS_LOG_SIZE")
COUNT, WHERE, A, B, SWITCHES = (
    abi_const(f"KOS_LOG_{k}") for k in ("COUNT", "WHERE", "A", "B", "SWITCHES")
)
NAMES = {
    abi_const(f"KOS_W_{k}"): k.lower()
    for k in ("BOOT", "START", "RING", "SEND", "IDLE", "USER", "PROBE", "DONE", "BAR")
}


def offset(span_size: int) -> int:
    """The log area's offset in a private span (KOS_PRIV_LOG_OFF)."""
    return span_size - LAND - LOG


def parse(area: bytes) -> dict:
    """The header's words and the log text from a whole log area."""
    w = [int.from_bytes(area[8 * k : 8 * k + 8], "little") for k in range(8)]
    cap = LOG - 64
    ring = area[64:LOG]
    count = w[COUNT]
    if count <= cap:
        text = ring[:count]
    else:
        k = count % cap
        text = ring[k:] + ring[:k]
    return {
        "log": text.decode("utf-8", "replace"),
        "log_bytes": count,
        "where": NAMES.get(w[WHERE], str(w[WHERE])),
        "where_a": w[A],
        "where_b": w[B],
        "switches": w[SWITCHES],
    }


def describe(nd: dict) -> str:
    return (
        f"where {nd['where']} a {nd['where_a']:#x} b {nd['where_b']:#x}"
        f" switches {nd['switches']} log_bytes {nd['log_bytes']}"
    )
