# L2 schedule: buffers, work items, placement, packages

**Status: frozen.** A change to anything below is an owner-reviewed spec change.

L2 is what every hand-written L1 kernel decided before it emitted a word: which
buffers exist and how their bytes are laid out, what units of work there are,
which unit runs each and in what order, what depends on what, and which work
shares a package. It touches L1 ([l1-program.md](l1-program.md)) and nothing
below: the L2 -> L1 compiler (§4) is the only code that reads both. A project
supplies the layouts, the work-item kinds and one lowerer per unit type;
KohakuTPU's are in [../projects/kohakutpu/ir/l2.md](../projects/kohakutpu/ir/l2.md).

Code: `compiler/kohakuaccel/ir/l2/schedule.py` (the IR and its checker),
`compiler/kohakuaccel/ir/l2/lower.py` (the compiler). The pass pipeline's old
L2 (`ScheduleIR`: encoded payloads, rounds) is `kohakuaccel.ir.l2.legacy`,
kept until its callers are gone.

## 1. Contents

| object | what it is |
|---|---|
| `Buffer(name, nbytes, layout, space)` | bytes with a project LAYOUT tag (opaque here); `space` is `"mem"` (an address, given or allocated) or a unit's local storage `("local", coord)` |
| `View(buffer, offset, nbytes)` | a byte range of a buffer: what a work item reads or writes |
| `Item(kind, unit, params, reads, writes, at, package)` | one unit of work of a project KIND on one unit TYPE, its views, its placement `at` (a coordinate, or None: the compiler places it) and its package; a parameter is an int, a float, a string, a bool, None or a tuple of those (a list is kept as a tuple) |
| `Schedule` | the buffers and the items IN SEQUENCE ORDER, and the `machine` its placement names when known (a schedule read from text knows it) |

Its text is [ir-text.md](ir-text.md) §4.

The `"mover"` unit type is the node's mover: its items lower to mover steps.

## 2. Semantics

- **Sequence order is program order.** The items' list order is the order a
  single-threaded execution would run them; every result must equal that.
- **Dependences are derived, never declared.** Item `b` depends on every earlier
  item `a` whose writes overlap `b`'s reads or writes, or whose reads overlap
  `b`'s writes (RAW, WAW, WAR on byte ranges of one buffer). Two views of
  different buffers never overlap; two `"mem"` buffers never alias.
- **Per-unit order** is the sequence order of the items placed on that unit.
- **A package** is a set of items run as one L0 package; an item depends only
  on items in its own or an earlier package.

## 3. What the checker refuses (`Schedule.check`)

- a view outside its buffer, or an item whose views name a buffer the schedule
  does not hold;
- an item with no placement once placement has run, or placed on a coordinate
  that is not a unit of its type;
- an item reading or writing a `("local", coord)` buffer while placed elsewhere;
- a dependence on an item in a LATER package;
- two `"mem"` buffers whose addresses overlap.

## 4. The L2 -> L1 compiler (`lower.compile`)

One L1 `Program` per package:

1. **Per-unit lowering.** Each unit's items go, in per-unit order, through that
   unit type's project LOWERER, which keeps the unit's own state across items
   (what its local storage holds, which bank is free, a result whose write-out
   it holds back). It returns CHUNKS: L1 ops, the items whose results are
   complete once the chunk has run, and a cost estimate in cycles.
2. **Sync from dependences.** A dependence between two items on one unit needs
   nothing (program order). Across units: a `mark` after the producer's
   completing chunk, a `wait(unit, token)` before the consumer's first chunk.
   A mover item waits for its producers and is a barrier after (a mover step
   is a barrier on both sides).
3. **Node order.** The node is single-threaded: a wait blocks every step behind
   it. Chunks are sent as early as their waits allow; when nothing can be sent,
   the wait emitted next is the one whose producer the cost estimates finish
   first. This is what the hand-written overlapped kernels do: every producer
   holds its next work before the node blocks.

The compiler emits correct, near-optimal L1. Optimality inside a unit's stream
beyond what its lowerer knows is the L1 -> L1 optimizer's, a separate pass
(planned, not built).
