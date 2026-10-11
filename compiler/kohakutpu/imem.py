"""Vector programs and descriptors kept resident in each vector core.

A vector kernel arrives as IMEM words, DESC words and a RUN. The program part
does not change between calls and the core keeps it across RUNs, so a
program already loaded is sent as its DESC words and a RUN at its slot. VLOOP
is pc-relative, so a program runs at any base and several share the memory.
A program placed over words the core already holds (a variant differing in a
few immediates) is sent as the words that differ: the CU loads IMEM only
between RUNs (`vec_cu.v` C_IDLE), so no running program sees a partial write.

Descriptors are core state too: a DESC word writing the value its field already
holds is dropped. Every DESC word is one the node dispatches on its own (~600
cycles, tiling.md), so a program may write every field it relies on and pay
for it only when something else changed it.
"""

import hashlib
from collections import OrderedDict

from kohakutpu.isa.vector import ISA as VEC
from kohakutpu.isa.vector import OP_DESC, OP_IMEM, OP_RUN

#: Words of one v1 core's instruction memory (vec_core.v IMEM_DEPTH).
IMEM_WORDS = 1 << VEC.cfg.addr_bits
#: A V2 core's (`hw/vector2.py` IMEM_WORDS).
IMEM_WORDS_V2 = 1 << (VEC.cfg.addr_bits + VEC.cfg.addr_hi_bits)
#: Where a RUN can start: its pc is `addr_bits` wide on either core.
RUN_REACH = 1 << VEC.cfg.addr_bits

OP_SHIFT = VEC.cfg.payload_bits - VEC.cfg.op_bits
#: A dispatched word may carry flit header bits above its payload.
PAYLOAD = (1 << VEC.cfg.payload_bits) - 1


def op(word: int) -> int:
    return (word >> OP_SHIFT) & ((1 << VEC.cfg.op_bits) - 1)


def keep_header(word: int, payload: int) -> int:
    """`payload` under `word`'s own header bits."""
    return (word & ~PAYLOAD) | payload


class Resident:
    """One core's instruction memory: which program occupies which words."""

    def __init__(self, words: int = IMEM_WORDS) -> None:
        self.words = words
        #: program key -> (base, length), oldest use first.
        self.slots: OrderedDict = OrderedDict()
        #: Where the last program placed here starts: a RUN with no IMEM words
        #: before it runs the program loaded by an earlier dispatch.
        self.last = 0
        #: (descriptor, field) -> the (value, value_hi) the core holds.
        self.desc: dict = {}
        #: IMEM address -> the word the core holds there.
        self.mem: dict = {}

    def forget(self) -> None:
        """Assume nothing is resident: after a fault the core's state is unknown."""
        self.slots.clear()
        self.desc.clear()
        self.mem.clear()
        self.last = 0

    def _free(self, length: int) -> int | None:
        """The lowest base with `length` free words a RUN can start at, or None."""
        taken = sorted(self.slots.values())
        at = 0
        for base, size in taken:
            if at >= RUN_REACH:
                return None
            if base - at >= length:
                return at
            at = max(at, base + size)
        return at if self.words - at >= length and at < RUN_REACH else None

    def place(self, key: bytes, length: int) -> tuple:
        """`(base, loaded)`: where the program runs, and whether it is there already."""
        got = self.slots.get(key)
        if got is not None:
            self.slots.move_to_end(key)
            return got[0], True
        base = self._free(length)
        while base is None and self.slots:
            self.slots.popitem(last=False)
            base = self._free(length)
        if base is None:
            raise ValueError(
                f"a {length}-word program exceeds the {self.words}-word IMEM"
            )
        self.slots[key] = (base, length)
        return base, False


class Cores(dict):
    """Every vector core's `Resident`, made on first use for cores of `words`
    instruction words: the `resident` state `Program.build` takes."""

    def __init__(self, words: int = IMEM_WORDS) -> None:
        super().__init__()
        self.words = words

    def __missing__(self, coord) -> Resident:
        self[coord] = got = Resident(self.words)
        return got


def rewrite(words: list, memory: Resident) -> list:
    """`words` for one RUN-terminated vector kernel, against what `memory` holds.

    Each program (a run of IMEM words) is placed in a slot: its words are
    dropped when that slot already holds it, or rebased into it otherwise
    (each word the address already holds dropped), and the RUN that follows
    starts at the slot. A DESC word is dropped when its field already holds
    that value.
    """
    out: list = []
    prog: list = []
    base = memory.last
    for w in words:
        code = op(w)
        if code == OP_IMEM:
            prog.append((w, VEC.IMEM.decode(w & PAYLOAD)))
            continue
        if code == OP_DESC:
            f = VEC.DESC.decode(w & PAYLOAD)
            key, value = (f["ad"], f["fld"]), (f["value"], f["value_hi"])
            if memory.desc.get(key) == value:
                continue
            memory.desc[key] = value
        if prog:
            body = [(VEC.imem_addr(f), f["word"]) for _, f in prog]
            key = hashlib.sha1(repr(body).encode()).digest()
            base, loaded = memory.place(key, max(a for a, _ in body) + 1)
            memory.last = base
            if not loaded:
                for raw, f in prog:
                    at = base + VEC.imem_addr(f)
                    if memory.mem.get(at) != f["word"]:
                        memory.mem[at] = f["word"]
                        out.append(keep_header(raw, VEC.imem(at, f["word"])))
            prog = []
        if code == OP_RUN:
            out.append(
                keep_header(w, VEC.run(base + VEC.RUN.decode(w & PAYLOAD)["pc"]))
            )
        else:
            out.append(w)
    if prog:
        raise ValueError("IMEM words with no RUN after them")
    return out
