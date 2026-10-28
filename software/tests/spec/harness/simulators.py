"""One active tier-2 model, prepared before poison and mutation scopes."""

import os
from contextlib import ExitStack

from runners.target import l0, l2

from harness.fresh import CARDS

_ACTIVE = None


def prepare(v, token=None):
    """Switch targets serially and expose allocations to the fresh-run harness."""
    global _ACTIVE
    wanted = None
    if v is not None and v["tier"] == 2 and os.environ.get("KTPU_SPEC_VERILATOR"):
        if v["runner"] == "l0.bench" and "test" in v["input"]:
            wanted = ("l0", v["input"].get("nodes", 1), token)
        elif v["runner"] == "l2.verilated":
            wanted = ("l2", v.get("machine"), v["input"].get("nodes", 1), token)
    if wanted != _ACTIVE:
        release()
    if wanted is not None:
        if wanted[0] == "l0":
            l0.verilated(wanted[1])
        else:
            l2.verilated(v["input"], {"vector": v})
    _ACTIVE = wanted


def release():
    """Stop simulator transports directly, even after a corrupt mutant package."""
    global _ACTIVE
    _ACTIVE = None
    cards = list(l0._CARDS.values())
    devices = list(l2._VERILATED.values())
    l0._CARDS.clear()
    l2._VERILATED.clear()
    retired = cards + [dev.card for dev in devices]
    CARDS[:] = [card for card in CARDS if all(card is not old for old in retired)]
    with ExitStack() as stack:
        for card in retired:
            stack.callback(card.t.close)
