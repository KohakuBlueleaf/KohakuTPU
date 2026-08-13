"""Executing a compiled artifact.

The driver and the compiler do not import each other. They meet here, at the
artifact's SERIALISED form: plain data with tagged steps, so this module needs no
class from the compiler and the compiler needs no register from this one.

The artifact carries 256-bit instruction PAYLOADS, not whole flits. The routing
header is added here, because the compiler has no business knowing the flit
layout: the dispatcher stamps destination and source itself, and `last` marks the
final payload of each kick, which is known from the kick's own length.
"""

from kohakuaccel.device import T_CU_INST, Program, header, node_status_addr
from kohakuaccel.transport.base import Transport


def load(
    artifact: dict, transport: Transport, txn: int = 0, stage_flits: int = 128
) -> Program:
    """Build a :class:`Program` from an artifact's serialised form.

    `transport` is read to take a completion baseline before anything is
    dispatched: NODE_STATUS counts are cumulative and cleared by nothing, so a
    wait must target ``baseline + expected`` rather than a number of events.

    Raises :class:`ValueError` on an unknown step tag or a kick whose slots fall
    outside the staged payloads.
    """
    payloads = artifact["flits"]
    prog = Program()
    baseline: dict[tuple[int, int], int] = {}
    expected: dict[tuple[int, int], int] = {}
    staged: set[int] = set()

    for tag, *args in artifact["steps"]:
        if tag == "seed":
            prog.seed_credits(args[0])

        elif tag == "kick":
            x, y, base, nflits = args
            if base + nflits > len(payloads):
                raise ValueError(
                    f"kick at ({x},{y}) wants slots {base}..{base + nflits - 1} "
                    f"but only {len(payloads)} payloads were staged"
                )
            # A slot past the staging RAM is not empty but STALE, and a vector
            # core given the last program's leftovers runs garbage and never halts.
            if base + nflits > stage_flits:
                raise ValueError(
                    f"kick at ({x},{y}) reaches slot {base + nflits - 1}, past a "
                    f"{stage_flits}-slot staging RAM. Split the program into "
                    f"windows that each restage from slot 0."
                )
            for k in range(nflits):
                slot = base + k
                if slot not in staged:
                    flit = header(T_CU_INST, txn, k == nflits - 1) | payloads[slot]
                    prog.stage_flit(slot, flit)
                    staged.add(slot)
            prog.kick(x, y, base, nflits)

        elif tag == "await":
            x, y, count = args
            coord = (x, y)
            if coord not in baseline:
                baseline[coord] = (
                    transport.read64(node_status_addr(x, y)) >> 8
                ) & 0xFFFF
                expected[coord] = 0
            expected[coord] += count
            prog.await_node_at(x, y, (baseline[coord] + expected[coord]) & 0xFFFF)

        elif tag == "barrier":
            continue  # the awaits before it already are the barrier

        else:
            raise ValueError(f"unknown artifact step {tag!r}")

    return prog.done(0)


def execute(artifact: dict, transport: Transport, timeout: float = 30.0) -> int:
    """Load and run an artifact, returning its DONE code."""
    return load(artifact, transport).execute(transport, timeout=timeout)
