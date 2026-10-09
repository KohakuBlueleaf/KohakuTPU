# L1 program: per-unit streams and sync points

**Status: frozen.** A change to anything below is an owner-reviewed spec change.

L1 is the lowest level a kernel is written at, and the level a sysnode
dispatches: one instruction stream per unit plus the node's sync points between
them. It sits directly on L0, the package bytes the node firmware runs
([package-format.md](package-format.md)), and touches nothing above. A project
supplies the unit op types and an optional per-unit-type lowering;
KohakuTPU's are in [../projects/kohakutpu/ir/l1.md](../projects/kohakutpu/ir/l1.md).

Code: `compiler/kohakuaccel/ir/l1/program.py` (`Program`); the pass pipeline's
staged-flit form is `kohakuaccel.ir.l1.ProgramIR` (`staged.py`). A program keeps
the ops it was given beside their words, so it prints as text
([ir-text.md](ir-text.md) §3).

## 1. A program

| call | meaning |
|---|---|
| `send(unit, *ops)` | append the ops' words (`op.flits()`) to `unit`'s stream |
| `wait(unit)` | hold the node until `unit` has completed every word sent to it since its last wait |
| `mark(unit)` / `wait(unit, token)` | a point in `unit`'s stream; hold the node until everything sent to `unit` up to the mark has completed, whatever was sent after it (revision 2: a cross-unit dependence per tile, with the producer already holding its next work) |
| `barrier()` | hold the node until every unit has |
| `move(*ops)` | one mover step (`kohakuaccel.package.mover`): ops with `writes()` (the project's mover ops), or one list of raw `(register, value)` writes; a barrier on both sides |
| `units(kind)` | the coordinates of every unit of `kind` |

## 2. Semantics

- **Within one unit, order is program order.**
- **Between units, sends inside one sync interval are unordered.** What one unit
  must see from another is ordered by a `wait`/`barrier` between them, never by
  send order.
- **Every word sent returns one completion.** When a word completes is the
  unit's definition (a project's L1 spec names it per op).

## 3. Lowering to L0 (`Program.build(fetch, resident)`)

1. Each unit's sends in one sync interval become ONE `DISPATCH` step. MEASURED
   (card_v9_1n): a fetch-port request costs ~1k node cycles whatever its length;
   four clusters sent one instruction a dispatch ran at 73% of sweep rate,
   coalesced 99%.
2. A unit type named in `Program.lowerings` has its interval's words rewritten
   by ``f(words, resident, coord)`` against `resident`, the caller's state kept
   across packages (what the unit already holds). `resident=None` runs no hook.
3. `wait`/`barrier` become `AWAIT` steps counting exactly the words dispatched
   since that unit's last await, then `BARRIER`; a `move` is a `MOVER` step and
   a `BARRIER`. The program ends with every unit awaited and a barrier.
4. A `mark` dispatches that unit's pending words at once and records how many
   it has been sent; `wait(unit, token)` awaits up to that count. An AWAIT is
   cumulative (it raises the unit's expected count), so a partial wait is the
   difference, and the words after the mark stay queued in the unit.

Units resolve through the package unit table (`kohakuaccel.package.units`).

## 4. What the node costs (L0 executor, card_v9_1n)

Per package: header ~430 node cycles, ~215 per unit to bind, ~150 to barrier.
Per completion ~50 cycles of firmware, serial across units: a unit whose words
retire faster than that waits on the node (`firmware/kohakuaccel/dispatch/
engine.c`, measured with RV_PC_PROF). A MOVER step's wait is bounded by
progress (a move finishing), not by the step.
