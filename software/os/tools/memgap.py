"""The memory-gap pass over compiler assembly, for code that runs translated.

Under Sv39 this core issues a load or store that enters execute in the cycle
right after another load or store completes with that access's physical
address (docs/arch/cpu/rv64-sys/memory-system.md; software/os/tests/xlat).
One instruction in between is enough. The pass puts a `nop` after every load,
store, LR/SC or AMO whose next instruction in the text is another one; a
memory access reached by a jump has the jump in front of it.
"""

import re

#: RV64IA memory instructions (no F/D, no C on this core)
MEM = re.compile(r"^(l[bhwd]u?|s[bhwd]|lr\.[wd]|sc\.[wd]|amo[a-z]+\.[wd])(\.[a-z]+)*$")


def mnemonic(line: str) -> str | None:
    """The instruction on an assembly line, None for labels, directives, blanks."""
    s = line.split("#", 1)[0].strip()
    if not s or s.startswith(".") or s.endswith(":"):
        return None
    return s.split(None, 1)[0]


def is_mem(m: str | None) -> bool:
    return m is not None and bool(MEM.match(m))


def apply(text: str) -> tuple[str, int]:
    """(the assembly with the gaps, how many nops went in)."""
    lines = text.splitlines()
    insts = [mnemonic(ln) for ln in lines]
    out, added, pending = [], 0, False
    for ln, m in zip(lines, insts):
        # labels and directives do not end the straight line: fall-through
        # crosses them
        if m is not None:
            if pending and is_mem(m):
                out.append("\tnop")
                added += 1
            pending = is_mem(m)
        out.append(ln)
    return "\n".join(out) + "\n", added
