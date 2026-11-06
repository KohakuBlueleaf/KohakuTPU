"""Each die's interlink counters around a run: AUX_STATW[0..15] of its memory agent
(docs/spec/control-registers.md §4), split into the fields `mag_ilink.v` assembles.

    got, deltas = interlink.around(card.t, board_map(card.board)["ctrl"], run)
"""

from collections.abc import Callable

#: The agent's AUX_STATW window and the interlink's control register (AUX_CFG 0x800
#: forwards client offset 0x80): [0] enable, [2] clear the sticky faults.
STATW, CONTROL = 0x80, 0x880
ENABLE, CLEAR_FAULTS = 1, 4
WORDS = 16

#: (name, word, low bit, width, counter). A counter reads as its delta modulo its
#: width, any other field as its value after the run (mag_ilink.v `stat_q`).
FIELDS = (
    ("caps", 0, 0, 64, False),
    ("faults", 1, 0, 8, False),
    *((f"bells_from{m}", 2 + m, 0, 32, True) for m in range(4)),
    *((f"tag_from{m}", 2 + m, 32, 16, False) for m in range(4)),
    *(
        (f"{d}{k}_{part}", w, lo, 32, True)
        for k, (tx, rx) in enumerate(((6, 7), (8, 9)))
        for d, w in (("tx", tx), ("rx", rx))
        for part, lo in (("packets", 0), ("beats", 32))
    ),
    *(
        (f"{part}{k}", 10 + k, lo, 32, True)
        for k in range(2)
        for part, lo in (("stall", 0), ("idle", 32))
    ),
    ("forwarded", 12, 0, 32, True),
    ("forward_blocked", 12, 32, 32, True),
    ("credits0", 13, 0, 32, False),
    ("credits1", 13, 32, 32, False),
    ("bells_sent", 14, 0, 32, True),
    ("egress_blocked", 15, 0, 32, True),
)

#: The sticky fault register's bits, low first.
FAULTS = ("RD_REMOTE", "ACK0", "SWITCH", "AXI", "INJ", "RING", "CW_OFF")


def read(t, ctrls: list[int]) -> dict[int, list[int]]:
    """Every die's sixteen words, die by die."""
    return {
        d: [t.read64(c + STATW + 8 * i) for i in range(WORDS)]
        for d, c in enumerate(ctrls)
    }


def clear_faults(t, ctrls: list[int], words: dict[int, list[int]]) -> None:
    """Each die's sticky faults cleared, its enable kept (word 0 is zero while
    disabled). A 32-bit control path writes the low half alone: a high half
    would land on the same register as a second write."""
    narrow = getattr(t, "control_bytes", 8) == 4
    for d, c in enumerate(ctrls):
        value = (ENABLE if words[d][0] else 0) | CLEAR_FAULTS
        (t.write32 if narrow else t.write64)(c + CONTROL, value)


def failures(word: int, *, f015_present: bool, long_remote: bool) -> int:
    """ACK0 is evidence only outside an F-015 long-burst sending die."""
    return word & ~2 if f015_present and long_remote else word


def delta(
    before: dict,
    after: dict,
    *,
    f015_present: bool = False,
    long_remote: frozenset[int] = frozenset(),
) -> dict[int, dict]:
    """Each die's fields and scoped faults; `raw_faults` retains every bit."""
    out = {}
    for d, words in after.items():
        got = {}
        for name, w, lo, width, counter in FIELDS:
            mask = (1 << width) - 1
            value = words[w] >> lo & mask
            got[name] = (
                (value - (before[d][w] >> lo & mask)) & mask if counter else value
            )
        got["raw_faults"] = got["faults"]
        got["faults"] = failures(
            got["raw_faults"], f015_present=f015_present, long_remote=d in long_remote
        )
        got["fault_names"] = [f for i, f in enumerate(FAULTS) if got["faults"] >> i & 1]
        out[d] = got
    return out


def around[T](
    t,
    ctrls: list[int],
    run: Callable[[], T],
    *,
    f015_present: bool = False,
    long_remote: frozenset[int] = frozenset(),
) -> tuple[T, dict[int, dict]]:
    """`run()` between two reads of every die's counters (`ctrls`: each die's
    control window), the sticky faults cleared before it: its result and `delta`."""
    before = read(t, ctrls)
    clear_faults(t, ctrls, before)
    got = run()
    return got, delta(
        before, read(t, ctrls), f015_present=f015_present, long_remote=long_remote
    )
