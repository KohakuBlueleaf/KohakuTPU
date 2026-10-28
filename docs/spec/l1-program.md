---
title: L1 program
summary: L1, the level a sysnode dispatches -- one instruction stream per unit plus the node's sync points -- and how it lowers to a package.
tags:
  - spec
  - ir
  - compiler
---

# L1 program: per-unit streams and sync points

**Status: frozen** (revision 3, 2026-10-11: every L0 v2 step reachable,
fields set from registers, labels, several nodes). A change to anything below
is an owner-reviewed spec change.

L1 is the lowest level a kernel is written at, and the level a sysnode
dispatches: one instruction stream per unit plus the node's sync points between
them. It sits directly on L0, the package bytes the node firmware runs
([package-format.md](package-format.md)), and touches nothing above. A project
supplies the unit op types and an optional per-unit-type lowering;
KohakuTPU's L1 body text is in
[../projects/kohakutpu/ir/ktpu.md](../projects/kohakutpu/ir/ktpu.md), its ops in
`ktpu-isa` and its lowering in `ktpu-l1::lower`.

Code: `software/rust/crates/ktpu-l0/src/program.rs` (`Program`).

## 1. A program

| call | meaning |
|---|---|
| `send(unit, *ops)` | append the ops' words (`op.flits()`) to `unit`'s stream |
| `wait(unit)` | hold the node until `unit` has completed every word sent to it since its last wait |
| `mark(unit)` / `wait(unit, token)` | a point in `unit`'s stream; hold the node until everything sent to `unit` up to the mark has completed, whatever was sent after it (revision 2: a cross-unit dependence per tile, with the producer already holding its next work) |
| `barrier()` | hold the node until every unit has |
| `move(*ops)` | one mover step (`ktpu-l0::mover`): ops with `writes()` (the project's mover ops), or one list of raw `(register, value)` writes; a barrier on both sides |
| `post(*ops) -> token` | a mover step the node does not wait for: it goes on while the moves run |
| `wait_moves(token)` | hold the node until the moves of that `post`, and every move before them, are done |
| `fetch(unit, addr, count)` | `count` words for `unit` that its fetch port reads from `addr` when the node gets here: words a unit wrote while the package ran. They count as sent to `unit`; no lowering hook sees them |
| `ring(mesh, slot, amount, fence)` / `wait_bell(mesh, n)` / `wait_slot(slot, n, consume)` | the interlink's doorbells and signal slots |
| `store(pairs, from_regs)` / `ctrl(pairs, from_regs)` | 64-bit stores (another mesh's address is a posted remote write) / control-region writes |
| `load(reg, addr, index, ctrl)` / `alu(rd, op, ra, b, imm)` | a package register from memory or a control register / from arithmetic |
| `label(name)` / `jump(name, cond, ra, b, imm)` | a jump target / a jump to it when `ra cond b` |
| `signal(v32, v64)` / `settle(cycles)` | a progress completion to the host / a fixed wait |
| `send_raw(unit, type, words, ack)` / `push(unit, desc, addr, n, ack)` / `recv(addr, n, unit)` | flits of any type from the node / a `CU_DATA` burst from memory / RX flits into memory |
| `units(kind)` | the coordinates of every unit of `kind` |

An op passed to `send` or `move` may carry `patches`: `Patch(word, segments,
reg, const)` sets each `(bit, width, shift)` segment of its word to
`(reg + const) >> shift` when the node reaches it, so any field of a unit word
or a mover register may take a run-time value.

## 2. Semantics

- **Within one unit, order is program order.**
- **Between units, sends inside one sync interval are unordered.** What one unit
  must see from another is ordered by a `wait`/`barrier` between them, never by
  send order.
- **Every word sent returns one completion.** When a word completes is the
  unit's definition (a project's L1 spec names it per op).

## 3. Lowering to L0 (`Program.build(fetch, resident)`)

1. Each unit's sends in one sync interval become one `DISPATCH` step, or --
   past the unit's credit (`inst_depth`) -- steps of the credit's size, the
   units' steps interleaved: A's first 512, B's first 512, A's next, ...
   MEASURED (card_v9_1n): a fetch-port request costs ~1k node cycles whatever
   its length (four clusters sent one instruction a dispatch ran at 73% of sweep
   rate, coalesced 99%), and the node sends a fetched step whole, waiting on
   that unit's credit, before the next: two vector cores each sent 973 words as
   one step ran 16 epilogues in 411k cycles, split by package in 310k. A stream
   within the credit cut smaller costs: add 262144 cut at 255 ran 93.9k ->
   107.8k.
2. A unit type named in `Program.lowerings` has its interval's words rewritten
   by ``f(words, resident, coord)`` against `resident`, the caller's state kept
   across packages (what the unit already holds). `resident=None` runs no hook.
3. `wait`/`barrier` become `AWAIT` steps counting exactly the words dispatched
   since that unit's last await, then `BARRIER`; a `move` is a `MOVER` step and
   a `BARRIER`; a `post` is a `MOVER` step flagged `POSTED`, and `wait_moves`
   an `MWAIT` counting the package's moves up to that post's last; a `fetch` is
   a `FETCH` step after the unit's queued words. The program ends with every
   unit awaited and a barrier.
4. A `mark` dispatches that unit's pending words at once and records how many
   it has been sent; `wait(unit, token)` awaits up to that count. An AWAIT is
   cumulative (it raises the unit's expected count), so a partial wait is the
   difference, and the words after the mark stay queued in the unit.
5. Every node step (`ring`, `store`, `load`, `alu`, `label`, `jump`, ...)
   dispatches every unit's pending words first, so a register a patch reads
   holds the value it had where the op was written. A patch is an `ALU` into
   the scratch register r15 (when `const` is nonzero) and a `PATCH` a segment,
   right before the dispatch that sends its word; a word's lowering hook does
   not run on a dispatch that carries patches. Past a label a patched unit is
   awaited before its words are patched again (a fetch port may still be
   reading the last pass's).
6. A `label` is the index of the next step; a `jump` resolves to it at the
   end. A `wait_moves` past a label is refused: an `MWAIT`'s count is absolute.

Several nodes are several programs, one per node, each on its node's mesh
(`MachineSpec.for_mesh`); they share memory and meet through `ring` and
`wait_slot`.

Units resolve through the package unit table (`ktpu-l0::units`).

## 4. What the node costs (L0 executor, card_v9_1n)

Per package: header ~430 node cycles, ~215 per unit to bind, ~150 to barrier.
Per completion ~50 cycles of firmware, serial across units: a unit whose words
retire faster than that waits on the node (`software/firmware/kohakuaccel/
dispatch/engine.c`, measured with RV_PC_PROF). A MOVER step's wait is bounded by
progress (a move finishing), not by the step.
