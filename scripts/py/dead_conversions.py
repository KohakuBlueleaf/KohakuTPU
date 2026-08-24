"""Price a kernel's layout conversions, each from its OWN layouts.

DO NOT ZIP `compiled.conversions` AGAINST `cost.relayouts`. They are different
lengths -- the list still names conversions the runtime no longer performs, 21
against 12 for `flash_attention` at 4x256 -- so a positional pairing misattributes
every cost after the first drop. That bug labelled a `tile -> flat` at 152 cycles
next to another at 19,359 for the same shape. Everything here recomputes from
`RL.for_conversion(before, after, compiled.shape(name))`.

The expense is not a conversion, it is one that touches `Tile`: over `(1024, 64)`
`flat <-> entry` is 4096 of 4096 words whole at 280 cycles, and both `Tile` pairs
are 0 of 4096 at 38,511.
"""

import numpy as np
from kohakutpu.isa import relayout as RL
from kohakutpu.model import SimDevice

from kohakutpu import cost
from kohakutpu import kernels as K

FP16 = np.float16

#: `(heads, Lq, Lkv, Dv)`.
SHAPES = [
    (4, 128, 128, 64),
    (4, 128, 256, 64),
    (4, 256, 256, 64),
    (20, 256, 256, 64),
    (10, 512, 512, 64),
]


def written(stage) -> set:
    """Buffers any statement of `stage` writes."""
    out = set()
    for inst in stage.instances:
        for s in inst.stmts:
            name = s.args.get("writes") or s.args.get("result")
            if name:
                out.add(name)
    return out


def read(stage) -> set:
    """Buffers any statement of `stage` reads, by every route one can."""
    out = set()
    for inst in stage.instances:
        for s in inst.stmts:
            out |= set(s.args.get("reads") or ())
            out |= {
                leaf.name
                for leaf in (s.args.get("leaves") or ())
                if hasattr(leaf, "name")
            }
            for key in ("operand", "source"):
                if s.args.get(key):
                    out.add(s.args[key])
    return out


def priced(compiled) -> list:
    """`(at, name, before, after, cycles, overwritten)` for every LISTED conversion.

    `overwritten` is the cheap SCREEN for a conversion nobody reads: the stage it
    runs before writes the buffer and does not read it. It is only sound because
    each of these drains covers its whole grid -- a PARTIAL write would leave the
    old order live, so the rule wants to be "is any element still read".
    """
    out = []
    for at, name, before, after in compiled.conversions:
        made = RL.for_conversion(before, after, compiled.shape(name))
        cycles = 0 if made is None else RL.cycles_for(made[0], made[3])
        stage = compiled.stages[at] if at < len(compiled.stages) else None
        gone = stage is not None and name in written(stage) and name not in read(stage)
        out.append((at, name, before.key, after.key, cycles, gone))
    return out


def report(heads: int, lq: int, lkv: int, dv: int, block: int = 64, show=False):
    dev = SimDevice(size=8192 << 20)
    compiled = K.flash_attention.plan(
        dev.tensor(np.zeros((heads, lq, dv), FP16)),
        dev.tensor(np.zeros((heads, lkv, dv), FP16)),
        dev.tensor(np.zeros((heads, dv, lkv), FP16)),
        block=block,
    )
    rows = priced(compiled)
    charged = cost.relayouts(compiled)
    total = cost.time(compiled, dev.machine).cycles
    live = sum(c for *_, c, gone in rows if not gone)
    print(
        f"h={heads:<3} Lq={lq:<4} Lkv={lkv:<4} blocks {lkv // block:<2} "
        f"cycles {total:>11,}   listed {len(rows):>3}  run {len(charged):>3}  "
        f"charged {sum(s.cycles for s in charged):>10,}  live-sum {live:>10,} "
        f"({100 * live / total:4.1f}% of the call)"
    )
    if show:
        for at, name, before, after, cycles, gone in rows:
            tag = "  overwritten, not run" if gone else ""
            print(
                f"    stage {at:>2}  {name:<4} {before:<22} -> {after:<22} "
                f"{cycles:>9,}{tag}"
            )


if __name__ == "__main__":
    for n, shape in enumerate(SHAPES):
        report(*shape, show=n == 2)
