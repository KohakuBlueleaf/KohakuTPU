"""A fn's token ledger: every ring it issues, as (source, dest, slot, amount,
packets), checked against each die's AUX_STATW deltas and the signal slots each
sysnode reads back at its end.

Counted: words 2-5 low 32 bits, rings received by source (a packet each, whatever
its amount), and word 14 low 32 bits, rings sent. The last transaction (words 2-5
bits 47:32) and the credit state (word 13) are values, never differenced. A slot's
residual is its signal word's low 32 bits; bits 53:50 name the slot read."""

import dataclasses

import numpy as np

DIES = 4
SLOT_SHIFT, SLOT_MASK, COUNT_MASK = 50, 0xF, 0xFFFF_FFFF
#: Fault bit 0, RD_REMOTE: every unit access to a canonical shared address (mesh
#: bits zero) from dies 1-3 raises it (multi-mesh.md "IL_FAULT is not a health
#: signal on any mesh but mesh_0"); it stays in the record, not in the verdict.
RD_REMOTE = 1


@dataclasses.dataclass(frozen=True)
class Ledger:
    """`rings`: (source, dest, slot, amount, packets) totals; `slots`: the
    (sysnode, slot) of each word of the fn's `slots` output, in order."""

    rings: tuple[tuple[int, int, int, int, int], ...]
    slots: tuple[tuple[int, int], ...]


def expected(ledger: Ledger, dies: int = DIES) -> dict[int, dict]:
    """Each die's counted AUX_STATW deltas, and a clear fault register."""
    out = {d: {f"bells_from{m}": 0 for m in range(dies)} for d in range(dies)}
    for d in out:
        out[d] |= dict(bells_sent=0, faults=0)
    for s, d, _, _, packets in ledger.rings:
        out[d][f"bells_from{s}"] += packets
        out[s]["bells_sent"] += packets
    return out


def settled(ledger: Ledger) -> np.ndarray:
    """The `slots` words of a fn that took every amount rung."""
    return np.array([slot << SLOT_SHIFT for _, slot in ledger.slots], np.uint64)


def check(ledger: Ledger, deltas: dict, words: np.ndarray) -> list[str]:
    """Mismatches of the run's die deltas (`interlink.delta`) and `slots` words
    against the ledger; empty when they agree."""
    bad = []
    for d, fields in expected(ledger, len(deltas)).items():
        for k, want in fields.items():
            got = deltas[d][k]
            if k == "faults" and d:
                got &= ~RD_REMOTE
            if got != want:
                bad.append(f"die {d} {k}: {got}, ledger {want}")
    for (n, slot), w in zip(ledger.slots, words.tolist(), strict=True):
        read = w >> SLOT_SHIFT & SLOT_MASK
        if read != slot or w & COUNT_MASK:
            bad.append(
                f"sysnode {n} slot {slot}: read slot {read}, left {w & COUNT_MASK}"
            )
    return bad
