"""The V2 core's timing of an image: the cycles a vector lowering compares
its candidates by (the band shapes, the modulo schedule).

The rules are the replay bench's (vector-core-v2.md s2, s4, s8), its memory
answering a word a cycle (memwait 0):

- the front end dispatches a word a cycle in program order -- an op's
  configuration words (the encoder's, `emit.image.Encoder`) before it; a
  front-end instruction that evaluates a condition (`vsync`, `seti`) leaves
  on its second cycle; the math, unpack and pack queues hold QUEUE entries,
  the fill and drain queues DMA_QUEUE, and dispatch waits for a free one;
- each engine runs its queue in order, an op's beats back to back (a math op
  a beat a 16-lane chunk, held a beat where a two-copy register file reads
  three words of one bank, `modulo.parse`; an unpack or pack a word a cycle;
  a fill or drain a word a cycle of the one memory port, a fill landing
  MEM_LATENCY after its last word leaves memory);
- a math result is read `modulo.LAT_MM` cycles after the producer's last beat
  (`LAT_XB` more through the crossbar; a pack's `LAT_MP`), an unpack's
  `LAT_UM` after its last word; a chunk-selected write waits on no earlier
  math write; a math write waits for the packs reading and the unpacks
  writing that register, an unpack write for the math ops reading it;
- a `vsync W on=K slack=S` holds W (its queue, or the front end) until all
  but S of the K instructions issued before it have finished; a `vbar` holds
  the unpack queue until every fill before it has landed.

A loop runs LOOP_PASSES passes; the rest are the last pass's cycles each.

Against the replay bench (128 rows a core, every band and modulo image of
softmax, layernorm and leaky): math-bound images within -2% / +6%; the
memory-bound leaky reads 17-26% low (the bench's memory is not one port a
word a cycle, and no single latency or per-transfer cost fits both shapes),
its band and modulo images still ordered right.
"""

import math
import re
from dataclasses import replace

from kohakutpu.compiler.emit import image as EI
from kohakutpu.compiler.l2 import banks
from kohakutpu.compiler.l2 import modulo as MO
from kohakutpu.language.text import module as TM
from kohakutpu.language.text.meta import Expander
from kohakutpu.language.text.syntax import Assign, Int, emit, fmt

QUEUE = 8
DMA_QUEUE = 4
#: cycles from a fill's last word leaving memory to its landing in L1
MEM_LATENCY = 16
#: dispatch cycles of a front-end instruction that evaluates a condition
COND = 2
LOOP_PASSES = 4
_COND_OPS = ("vsync", "seti", "setvl", "halt")
#: VSYNC waiter / kind names -> engine
_ENGINE = {
    "unpack": "U",
    "math": "M",
    "pack": "P",
    "fill": "F",
    "drain": "D",
    "fe": "fe",
}
_DEPTH = {"M": QUEUE, "U": QUEUE, "P": QUEUE, "F": DMA_QUEUE, "D": DMA_QUEUE}


class _Core:
    def __init__(self, copies: int) -> None:
        self.copies = copies
        self.fe = 0
        self.mem = 0  # the memory port's next free cycle
        self.free = dict.fromkeys(_DEPTH, 0)
        self.starts: dict = {e: [] for e in _DEPTH}  # each queue entry's leave
        self.done: dict = {e: [] for e in _DEPTH}  # each instruction's finish
        self.ready: dict = {}  # register -> cycle a math op may read it
        self.ready_p: dict = {}  # register -> cycle a pack may read it
        self.read_end: dict = {}  # register -> last math read's end
        self.pack_end: dict = {}  # register -> last pack read's end
        self.unpack_end: dict = {}  # register -> last unpack write's end
        self.by_math: set = set()  # registers a math op wrote last
        self.parsed: dict = {}

    def shift(self, d: int) -> None:
        """Every time in the state `d` cycles later."""
        self.fe += d
        self.mem += d
        for e in self.free:
            self.free[e] += d
            self.starts[e] = [s + d for s in self.starts[e][-QUEUE:]]
            self.done[e] = [t + d for t in self.done[e]]
        for m in (
            self.ready,
            self.ready_p,
            self.read_end,
            self.pack_end,
            self.unpack_end,
        ):
            for k in m:
                m[k] += d

    def _enter(self, eng: str, words: int) -> int:
        """Dispatch `words` into `eng`'s queue: the cycle the entry is in."""
        self.fe += words
        q = self.starts[eng]
        if len(q) >= _DEPTH[eng]:
            self.fe = max(self.fe, q[-_DEPTH[eng]])
        return self.fe

    def op(self, text: str, words: int) -> None:
        o = self.parsed.get(text)
        if o is None:
            o = self.parsed[text] = MO.parse(*banks.symbolic(text), self.copies)
        start = max(self._enter(o.eng, words), self.free[o.eng])
        if o.eng == "M":
            start = max(
                start,
                *(
                    self.ready.get(r, 0)
                    + (MO.LAT_XB if r in self.by_math and r in o.xreads else 0)
                    for r in o.reads
                ),
                *(
                    max(self.pack_end.get(w, 0), self.unpack_end.get(w, 0))
                    for w in o.writes
                ),
            )
            end = start + o.occ
            for r in o.reads:
                self.read_end[r] = max(self.read_end.get(r, 0), end)
            for w in o.writes:
                self.ready[w] = end + MO.LAT_MM
                self.ready_p[w] = end + MO.LAT_MP
                self.by_math.add(w)
        elif o.eng == "U":
            start = max(start, *(self.read_end.get(w, 0) for w in o.writes))
            end = start + o.occ
            for w in o.writes:
                self.ready[w] = self.ready_p[w] = end + MO.LAT_UM
                self.unpack_end[w] = end
                self.by_math.discard(w)
        else:
            start = max(start, *(self.ready_p.get(r, 0) for r in o.reads))
            end = start + o.occ
            for r in o.reads:
                self.pack_end[r] = max(self.pack_end.get(r, 0), end)
        self.free[o.eng] = end
        self.starts[o.eng].append(start)
        self.done[o.eng].append(end)

    def dma(self, eng: str, words: int) -> None:
        """A fill (F) or drain (D) of `words` memory words."""
        start = max(self._enter(eng, 1), self.free[eng], self.mem)
        self.mem = self.free[eng] = start + words
        self.starts[eng].append(start)
        self.done[eng].append(start + words + (MEM_LATENCY if eng == "F" else 0))

    def sync(self, waiter: str, kind: str, slack: int) -> None:
        n = len(self.done[kind]) - slack
        at = self.done[kind][n - 1] if n > 0 else 0
        if waiter == "fe":
            self.fe = max(self.fe + COND, at)
            return
        start = max(self._enter(waiter, 1), self.free[waiter], at)
        self.free[waiter] = start
        self.starts[waiter].append(start)

    def bar(self) -> None:
        """The unpack queue waits for every fill issued before."""
        at = max(self.done["F"], default=0)
        start = max(self._enter("U", 1), self.free["U"], at)
        self.free["U"] = start
        self.starts["U"].append(start)

    def other(self, words: int) -> None:
        self.fe += words

    def end(self) -> int:
        return max(
            self.fe,
            *self.free.values(),
            *(max(d, default=0) for d in self.done.values()),
        )


def _walk_words(walk) -> int:
    """The words a descriptor's walk moves: the product of its counts."""
    counts = [int(c) for c in re.findall(r"\(\s*-?\d+\s*,\s*(\d+)\s*\)", fmt(walk))]
    return math.prod(counts) if counts else 1


def cycles(text: str, name: str, vregs: int, copies: int) -> int:
    """Cycles image `name` of `text` takes on a core of `vregs` registers and
    `copies` register-file read copies, its parameters at 0."""
    mod = replace(TM.read(text, f"<{name}>"), vregs=vregs)
    r = mod.reader()
    img = mod.images[name]
    macros = {m.name: (m.params, m.stmts) for m in mod.macros.values()}
    stmts = Expander(macros, r.fail, unroll_all=True).stmts(
        img.stmts, dict.fromkeys(img.params, Int(0))
    )
    enc = EI.Encoder(r.fail, regs=vregs)
    core = _Core(copies)
    state = EI.UNKNOWN
    scalars: dict = {}
    words_of: dict = {}

    def run(sts) -> None:
        nonlocal state
        for st in sts:
            op = st.op
            if op == "loop":
                n = scalars.get(st.positional()[0].text, 1)
                marks = []
                for _ in range(min(n, LOOP_PASSES)):
                    run(st.body)
                    marks.append(core.end())
                if n > LOOP_PASSES:
                    core.shift((marks[-1] - marks[-2]) * (n - LOOP_PASSES))
                continue
            if op == "desc":
                bind = next((x for x in st.args if isinstance(x, Assign)), None)
                walk = st.kwargs().get("walk")
                if bind is not None and walk is not None:
                    words_of[bind.lhs.text] = _walk_words(walk)
                continue
            if op == "seti":
                (a,) = st.args
                if isinstance(a, Assign) and isinstance(a.rhs, Int):
                    scalars[a.lhs.text] = a.rhs.value
            if op in ("vunpk", "vpack") or op in EI.MATH:
                words, state = enc.stmt(st, state, 1)
                core.op(emit([st], 0).strip(), len(words))
            elif op in (*EI.EXPLICIT, "vcfg", "setvl", "word"):
                words, state = enc.stmt(st, state, 1)
                core.other(len(words))
            elif op in ("vfill", "vdrain"):
                ad = st.positional()[0]
                core.dma(
                    "F" if op == "vfill" else "D",
                    words_of.get(getattr(ad, "text", ""), 8),
                )
            elif op == "vsync" and "on" in st.kwargs():
                waiter = st.positional()[0].text
                on = st.kwargs()["on"]
                slack = st.kwargs().get("slack")
                kind = _ENGINE.get(getattr(on, "text", ""))
                if kind in _DEPTH and waiter in _ENGINE:
                    core.sync(
                        _ENGINE[waiter],
                        kind,
                        slack.value if isinstance(slack, Int) else 0,
                    )
                else:
                    core.other(COND)
            elif op == "vbar":
                core.bar()
            else:
                # a scalar op, a mark or a wait on a count: its words
                core.other(COND if op in _COND_OPS else 2 if op == "seti" else 1)

    run(stmts)
    return core.end()


__all__ = ["COND", "DMA_QUEUE", "LOOP_PASSES", "MEM_LATENCY", "QUEUE", "cycles"]
