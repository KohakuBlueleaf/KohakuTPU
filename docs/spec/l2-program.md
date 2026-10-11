---
title: L2 program
summary: L2, the level above L1 -- batches of work on named units, every value hardware-sized and placed in a named space -- what it may leave open, and what the L2 -> L2 passes and the L2 -> L1 compiler decide.
tags:
  - spec
  - ir
  - compiler
---

# L2 program: batches of work on named units

**Status: draft.** L2 is the level above L1 ([l1-program.md](l1-program.md))
and the top of the IR: there is no level above it inside the IR. A kernel
written at L2 states what each unit does in batches -- fill a whole operand
tile, run a sweep sequence into an accumulator tile, load a block into a
core's local memory, apply hardware ops to register-shaped values, store or
send the result -- and leaves out only what a compiler can decide without
changing what is computed.

Code: `software/compiler/kohakutpu/compiler/l2/` -- `body.py` (the reader:
scopes, views, instances), `memory.py` (a view as runs of its buffer's
layout), `cluster.py` / `vector.py` (each unit's lowering), `lower.py` (the
stages, interleaving and node sync), `passes.py` (L2 -> L2). KohakuTPU's
words: [../projects/kohakutpu/ir/ktpu.md](../projects/kohakutpu/ir/ktpu.md) §9.

## 1. The rule

- **Every value has a hardware-sized shape and a named space.** A space is a
  memory the hardware has: a register file, a unit's local memory, an
  accumulator, a staging store, DRAM. The verifier refuses a value its space
  cannot hold.
- **Every op is one hardware feature, or a fixed instruction pattern whose
  cost is known** (a row reduction is the crossbar merge tree, nothing else).
  An op the hardware does not have is refused, never synthesised.
- **Nothing is above the hardware.** Block sizes, which tiles are resident,
  layouts and the arithmetic are written; a compiler never decides how to tile
  or what an op means.

## 2. What L2 may leave open

| left open | decided by |
|---|---|
| exact addresses, banks, registers | L2 -> L1 |
| issue order, the overlap of transfers with compute, double buffering | L2 -> L1 |
| marks and waits between units; rings and slots between nodes | L2 -> L1 |
| packaging into node steps | L2 -> L1 (then L1 -> L0) |
| which unit (`mg[any]`, `vc[any]`) | the `place` pass |
| which memory (`@mem`: DRAM or staging; `@any` where a value may go to a unit's local memory directly) | the `space` pass |
| a gemm larger than a bank or the accumulator (`gemm ... split`) | the `split` pass |

A pass narrows a choice and prints L2 text with it written in; it never
changes what is computed. Every pass's output is read back and verified.

## 3. A body

```
fn NAME.l2(PARAM: TYPE @SPACE(LAYOUT), ...)
    NAME = buffer : TYPE @SPACE(LAYOUT)       # scratch memory, at the top
    par VAR in LO..HI on UNIT[EXPR]           # instances, one per VAR value
        for VAR in LO..HI                     # ordered iterations on the unit
            NAME = OP ARGS : TYPE @SPACE      # a value
            OP ARGS                           # an effect (a store, a drain)
```

- `par` makes instances that run concurrently, instance `VAR` on the unit
  `UNIT[EXPR]`; `UNIT` is `node` (a node and its mesh), a unit kind of the
  target, or a kind with `[any]`. Instances of one `par` share nothing but
  memory.
- `for` iterations run in order on the enclosing instance. A name a `for` body
  rebinds from outside the loop is loop-carried. Outside every unit's `par`,
  `for ... pipe=K` lets later iterations start before earlier ones finish,
  where they touch nothing the earlier ones still use (§4).
- `when COND` keeps or drops a block; `expand` and the read-time expansion of
  [ir-text.md](ir-text.md) §2 apply before the reader. `if COND` is a
  run-time condition a unit evaluates (a target names which it has); a value
  the block rebinds keeps its old contents when the block is skipped.
- `recv UNIT` is a value another unit sent into this unit's memory; the
  k-th `recv` from a unit pairs with that unit's k-th transfer to this one.
- Index expressions are integer arithmetic over loop and `par` variables.
- A view is `NAME[axis, ...]`, each axis `:` (whole), `LO : +SIZE` (a run),
  `I` (one index, the axis dropped) or `*` (a new broadcast axis).

## 4. Order

- **Within one instance, order is program order.** A value is ready when the
  op that makes it has completed.
- **Between instances, only memory orders.** A read of a region another
  instance (of any `par`, on any node) wrote is ordered after that write by
  program order of the enclosing scopes: statements after a `par` see
  everything its instances did. The L2 -> L1 compiler inserts the L1 marks,
  waits, barriers, rings and slot waits that make that hold, and no more: a
  region is a memory range, a unit's local memory, or a receive port the
  hardware cannot share, and two stages that touch no region in common (one
  of them writing) are not ordered.

## 5. Lowering to L1

The compiler's whole job, in this order:

1. **Allocate:** registers, local-memory addresses, banks; a value read in the
   next iteration of a loop gets a second buffer (double buffering) when the
   space holds both.
2. **Order:** each unit's stream in program order; a transfer for iteration
   `i + 1` placed before the compute of `i` when its buffer is free; a
   project's unit rules (a fused drain placed late, a fill never into the bank
   being read).
3. **Sync:** from the regions each statement reads and writes, the L1 `mark` /
   `wait` / `barrier` between units of a node and `ring` / `wait slot` between
   nodes.
4. **Package:** L1 text (`fn NAME.l1` with the images it runs), read back by
   the L1 reader ([l1-program.md](l1-program.md)).

The output of every stage is `.ktpu` text. The gate is cycle parity with the
kernel's hand-written L1 on the card (near, not word for word), and the L1
output computing what the L2 body's interpreter computes.
