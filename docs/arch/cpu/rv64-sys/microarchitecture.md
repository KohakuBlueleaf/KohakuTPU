---
title: RV64 system core microarchitecture
summary: Every stage opened up — the sub-pipeline inside each, the three-source forwarding network a read-first register file forces, every stall and every flush, the predictor, and the multi-cycle units.
tags:
  - architecture
  - cpu
  - rv64
  - microarchitecture
---

# RV64 system core microarchitecture

How `rv64_core` is built, cycle by cycle. The [architecture](architecture.md)
page is the contract; this page is the machine that keeps it, and everything on
it is free to change.

The reader this page assumes knows what a pipeline stage, a forwarding path and
a saturating counter are, and knows nothing about this core. Read
[README](README.md) first if you have not: the core exists to host a runtime,
which is what makes a return-address stack and a divider worth their area and
makes a floating-point unit not.

Resource and frequency figures on this page are **out-of-context synthesis, not
routed**, on `xcvu13p-fhgb2104-2L-e` under Vivado 2024.2, produced by
`scripts/tcl/ooc_syscore.tcl` unless another source is named. Synthesis slack is
optimistic; [performance](performance.md) states the request each figure was
taken at and the measurement context each one belongs to, and
[measurement](../../physical/measurement.md) states what a figure from this tree
means in general.

**"Five-stage" is the wrong summary.** Five is the logical decomposition. An
instruction crosses **six** register boundaries, three of the stages contain
their own sub-pipelines, and the deepest path through execute is **66 cycles**.
Anything that models this as a uniform five-stage machine will mispredict both
its frequency and its cycle count.

### The other core is the useful contrast

The [RV32 controller PE](../rv32-pe/microarchitecture.md) is the same framework,
the same objectives — LUT first, frequency second, latency last — and a
different job. Where the two diverge, the divergence is almost always the
lifecycle rather than the word width, and this page draws on the comparison
throughout rather than describing this core in isolation.

| | RV32 controller PE | RV64 system core |
|---|---|---|
| register boundaries | six | six |
| where the address is computed | EX, consumed in MEM — **it has an address-generation stage** | E, consumed in M — [it does not](#why-there-is-no-address-generation-stage) |
| load forwarding | distance 3 only, so **two** stall cycles back to back | distance 2 after one bubble, so **one** |
| predictor | 32-entry BTB, 2-bit counters | [BTB + gshare + a return stack](#why-the-predictor-is-bigger-than-the-rv32-pes-and-differently-shaped) |
| divide | [refused, and costed](../rv32-pe/microarchitecture.md#why-div-and-rem-are-a-different-answer) | built, 66 cycles |
| a fault is | the unit's completion — it halts and says why | a trap, or a halt only if no handler is installed |
| externally written memory | a scratchpad with a **cross-port bypass**, because a doorbell lands in the word a poll loop is reading | none — the doorbell arrives as an interrupt line, not as memory |

The last row is the shape of the whole comparison: a batch compute unit is
written *to* by its peers and must make that correct in the array; a runtime host
is signalled instead, and pays for a trap model rather than a bypass.

## Five stages, six register boundaries

```
        F          D           E           M           W         W-1
      ┌────┐    ┌──────┐    ┌──────┐    ┌──────┐    ┌──────┐
      │ pc │───▶│ d_*  │───▶│ e_*  │───▶│ m_*  │───▶│ wb_* │───▶ w_*_q
      └────┘    └───┬──┘    └───┬──┘    └──────┘    └──────┘
         ▲          │           │
         └──────────┴───────────┘   redirect_pc: a trap or a mispredict
                                    resolved in E, or a prediction made in D

   F     next-PC select. The instruction memory and the predictor are addressed
         and both answer next cycle
   D     decode, combinational. The register file's address leaves, and the
         forward SELECT is computed here
   E     the forward mux, then ALU (1 cycle) | muldiv (8 or 66) | AMO (3-4) |
         CSR (2). The branch resolves and the effective address is computed
   M     the data memory answers; load align and sign extend    1st forward source
   W     the register file's write port                         2nd forward source
   W-1   the write that landed last cycle                       3rd forward source
```

| boundary | register | what it holds |
|---|---|---|
| F | `pc`, and `d_instr_hold`/`d_hold_v` on a hold | the fetch address |
| D | `d_valid`, `d_pc` | the instruction's PC; its bits arrive on `imem_data` |
| E | `e_valid`, `e_pc`, `e_imm`, `e_rs1/2`, `e_rd`, the control bits, `e_s1_*`/`e_s2_*` | the decoded instruction and its **forward selects** |
| M | `m_wr`, `m_ld`, `m_rd`, `m_f3`, `m_off`, `m_val` | the result, or the alignment control for a load |
| W | `wb_we`, `wb_rd`, `wb_val` | the value being written to the register file |
| W−1 | `w_wr_q`, `w_rd_q`, `w_val_q` | the value written **last** cycle |

The sixth boundary, W−1, is not architectural — nothing reads it but the
forwarding network. It exists because of what a synchronous array does with a
write and a read on the same edge, and
[the forwarding network](#why-a-read-first-register-file-needs-three-forward-sources)
is about exactly that.

### Occupancy and latency

**Occupancy** is how many cycles an instruction holds E, which is what blocks
everything behind it. **Latency** is how many cycles until a consumer can use
the result.

| class | occupancy | latency | why |
|---|---|---|---|
| ALU, shift, `LUI`, `AUIPC` | 1 | 1 | forwarded from M |
| load | 1 | **2** | the data does not exist until M has run — one bubble if consumed immediately |
| store | 1 | — | no result |
| branch, correctly predicted | 1 | — | **no penalty** |
| branch or jump, predicted taken in D | 1 | — | 1 instruction killed |
| branch or jump, mispredicted | 1 | — | **2 killed** — the resolve is in E |
| `jalr` whose target the BTB missed | 1 | — | 2 killed |
| `mul`, `mulh`, `mulhsu`, `mulhu`, `mulw` | **8** | 8 | one 32×32 DSP reused four times, two-deep |
| `div`, `divu`, `rem`, `remu` and the `W` forms | **66** | 66 | restoring, one bit per cycle |
| `amo*`, `sc` | **4** | 4 | read, modify, write |
| `lr` | **3** | 3 | read only |
| any CSR instruction | **2** | 2 | the write data is registered — [why a CSR instruction costs two cycles](#why-a-csr-instruction-costs-two-cycles) |
| any access the wrapper stalls | 1 + the stall | | the request is issued in E, so **E is what holds** |

## F — one mux, and two arrays that answer next cycle

```
        trap_redir ─┐
        e_redir ────┼─▶ [priority] ──▶ redirect, redirect_pc
        d_redir ────┘                       │
                                            ▼
              pc ──▶ (+4) ──────────────▶ [mux] ──▶ ┌────┐
                                                    │ pc │
                                                    └──┬─┘
                                                       │
                              ┌────────────────────────┴───────────────────┐
                              ▼                                            ▼
                    imem_addr ─▶ instruction memory            u_bp.q_addr ─▶ BTB, PHT
                                 READ_LAT 1                                  READ_LAT 1
                                     │                                            │
                                     ▼ (in D)                                     ▼ (in D)
                                 imem_data                                pr_taken, pr_target
```

The whole address path in fetch is `pc → 2:1 mux → the arrays' address pins`,
and nothing else. That is deliberate: the arrays register their own address
input, so any logic in front of the mux is logic in front of a memory, and the
fetch loop closes only because there is none.

The predictor's lookup goes out with the **fetch** address and its answer
arrives with the instruction it describes. Checking that answer against `q_pc`
— the D-stage PC — is what makes the tag compare meaningful.

**`imem_addr` is a virtual address, and one stage of the fetch path is outside
the core.** In the node configuration it passes through a single registered
page mapping in the wrapper before it reaches the array, and the wrapper raises
`imem_stall` while that mapping is being resolved
([memory-system](memory-system.md#fetch-is-translated-through-one-page-register)).
A faulting fetch comes back as a word flagged *faulted*, which decode turns into
a `NOP` and E turns into a trap. In the mesh compute-unit configuration both
inputs are constants and the whole path constant-propagates away.

### The instruction word has to be captured when fetch stops

The array is addressed by `pc`, and D holds the instruction fetched from the
*previous* `pc`. Freezing `pc` therefore does **not** freeze the word D is
looking at: one cycle later the array is answering the frozen `pc`, and D's own
word is gone.

A holding register solves it: on the **first** cycle of a hold — and only the
first, because a flag marks it taken — the instruction word is copied out of the
array's output into `d_instr_hold`, and decode reads that copy for the rest of
the hold. Both clear when fetch moves again.

**That is the general shape of holding a stage whose input is a synchronous
array, and it is why a hold is never simply a clock enable.** A flop-input stage
holds by not clocking; an array-input stage keeps being handed new data whether
it wants it or not, so it has to capture. The same trap is one register-file
read away in any design that stalls a stage fed by BRAM.

## Why decode also computes the forward select

```
   imem_data ────┐
                 ├──▶ [mux] ──▶ d_instr ──▶ rv64_decode ──┬──▶ rs1_a, rs2_a ──▶ regfile
   d_instr_hold ─┘      ▲                  (combinational)│        address pins, READ_LAT 1
                     d_hold_v                             │
                                                          ├──▶ imm_d, alu_op_d, e_* control
                                                          │
                                                          ├──▶ d_call / d_ret ──▶ RAS push/pop
                                                          │
                                                          └──▶ d_predict ──▶ d_redir

   rs1_a, rs2_a ──▶ sel_m()  against E as it is NOW ──▶ e_s1_m, e_s2_m
                ──▶ sel_w()  against M as it is NOW ──▶ e_s1_w, e_s2_w
                ──▶ sel_q()  against W as it is NOW ──▶ e_s1_q, e_s2_q
```

Three things happen here that a textbook diagram puts elsewhere.

**Decode is combinational on the fetched word.** The register-file address
leaves at the same edge as the control bits, which buys the operand-fetch cycle
instead of costing a seventh boundary.

**The return-address stack moves in fetch order.** `d_call` is an issued `jal`
or `jalr` writing `x1` or `x5`; `d_ret` is a `jalr` writing `x0` whose source is
`x1` or `x5`. Both are qualified by `d_issue = d_valid && fd_go`, so an
instruction killed by a redirect never touches the stack and a held instruction
never touches it twice.

**The forward select is computed here, not in E** —
[the select is computed in D](#the-select-is-computed-in-d-and-it-is-exact).

## Why a read-first register file needs three forward sources

In-order, single issue, one write port. That settles most of the hazard
question before it is asked:

- **WAW and WAR cannot occur.** One write port, and instructions retire in issue
  order.
- **Structural hazards are designed out rather than arbitrated.** The register
  file is 2R1W as two mirrored single-read arrays, so the two operand reads
  never contend; instruction and data memories are separate ports, so F and M
  never collide.

What is left is **RAW**, **control**, and **multi-cycle occupancy**.

### Three sources, and the third is the memory primitive's fault

```
   distance   producer is in   source        why the array cannot answer

      1            M           m_val         the result has not been written at all
      2            W           w_data        the write is being presented THIS cycle
      3           W-1          w_val_q       the write landed on the SAME EDGE as the read
      4+        already in      —            the array returns it
```

Work the third one through, because it is the one that is not in a textbook
diagram. Instruction *I* is in E at cycle *T*. Its operand address left D during
*T−1*, and `kohaku_sdpram` at `READ_LAT = 1` returns the data at *T*.

- *I−3* wrote the register file at the edge ending *T−1* — **the same edge that
  captured I's read**. The array is **read-first**: a write and a read of one
  address on one edge return the *old* value. So *I−3*'s result is not in what
  the array handed back, even though it is architecturally a whole instruction
  older than the boundary a flop-based file would need.
- *I−4* wrote at the edge ending *T−2*, one edge earlier, so the array does
  return it. No fourth source is needed.

That is the price of putting the register file in a memory primitive rather than
in flops, and it is paid **once in a mux** rather than continuously in LUTRAM.
It is also why the core cannot be read as a textbook five-stage machine: those
diagrams assume a flop-based file with a write-through read port and show two
forward sources.

**The rule generalises, and it is the one to carry away from this section:
count your forward sources from the memory primitive's read-during-write
behaviour, not from the stage diagram.** A write-first array needs two, a
read-first array needs three, and a flop file with a write-through port needs
two — and nothing in the pipeline drawing tells you which you are looking at.
Get it wrong in the cheap direction and the core is incorrect for exactly one
producer-to-consumer spacing, which is the kind of bug a casual test suite does
not reach. The co-simulation behind this core covers every spacing by
construction for that reason.

The three sources are selected per operand and are mutually exclusive by
priority — M first, then W, then W−1, then the array's own output.

**A load is excluded from the distance-1 source only.** A load's data does not
exist at M's *input*; by W it has been aligned and sign-extended and is an
ordinary registered value. That single exclusion is what the load-use interlock
in [the one data stall](#the-one-data-stall-load-use) pays for, and it is why
this core's load-use penalty is one cycle where the RV32 PE's is two — that core
forwards a load only at distance 3, so it stalls at both 1 and 2.

### The select is computed in D, and it is exact

The three selects compare register numbers **in decode**, against E, M and W
**as they are in that cycle** — not against where the instruction's producers
will be. Each is one equality on a register number, qualified by the producing
stage's own valid and write-enable bits, with `x0` and a load in M excluded.

This is exact rather than approximate, and the reason is that **the pipeline
shifts exactly one stage per cycle**. An instruction sitting in D this cycle
will be in E next cycle, and whatever is in E now will be in M then, W now will
be W−1 then. So "compare against E, M and W now" and "compare against M, W and
W−1 when I get to E" are the same comparison, one cycle apart.

The motivation is timing, not elegance. Comparing in E put the comparator, the
4:1 mux and the ALU in one cycle and made the comparator itself the binding
path. Moving it to D leaves E with a mux and measured **−156 LUT and
+12.1 MHz**, with byte-identical cycle counts on all three test programs — which
is the correctness argument a pure timing transform owes.

Correctness across a hold follows from where the capture is gated: the selects
are registered under `go`, the same enable as the E stage itself. If E is
stalled, both freeze together and the instruction in E is unchanged. If D is
held by a `bubble`, `go` is still high, so the comparison re-evaluates against
the shifted pipeline every cycle and the value captured on the cycle the
instruction finally moves is the one that is used.

### Why the operands have to be frozen, and not just the stage

**The forwarding network describes the pipeline in the cycle an instruction
enters E, and after that it describes other instructions.** M and W keep
draining while E is held ([the drain rule](#why-m-and-w-drain-through-a-stall)),
so `m_val`, `w_data` and `w_val_q` all move on
underneath frozen selects. Reading the mux on a later cycle returns another
instruction's result.

The answer is one latch pair, `op_held`, in front of everything E does with its
operands. On the **first** stalled cycle the forward mux is still correct — the
drain happens at the *end* of that cycle — so that is the cycle to capture both
operands; every later cycle of the stall reads the capture instead of the mux,
and it releases when the stall does.

Everything downstream of the operands inherits the fix at once: the
effective address `ea`, the branch comparison, the store data, the AMO's address
and source operand, the CSR write data, and the multiplier's operands. That is
the point of doing it here. The AMO sequencer latches its own copies as well
(`a_addr`, `a_srcq`) because it needs them across state transitions, but it no
longer *has* to — and a plain load or store, which became multi-cycle only
because the wrapper can stall it, never had a latch of its own and now does not
need one.

Without it, a stalled load recomputes its own address from a network that has
moved, and the symptom is a spurious misalignment fault on an access that was
aligned when it was issued.

**The portable statement: a forwarding network describes the pipeline for
exactly one cycle.** Any structure that holds an instruction longer than that —
a multi-cycle unit, a stallable memory, a CSR write — must take a copy on entry,
and doing it once structurally is cheaper and safer than each unit remembering
to. This core learned it per-unit first, with the atomics sequencer latching its
own address and source operand, and the general form arrived when memory became
able to stall: at that point *every* memory instruction was multi-cycle, and
there is no per-unit place to put the latch.

## Two stalls, three flushes, and what each one freezes

They differ in what they freeze, and the difference is the whole of the hazard
design.

| | raised by | freezes | keeps moving |
|---|---|---|---|
| **`stall`** | `e_md && !md_done` · AMO not in `A_FIN` · `csr_wait` · `mem_wait` (`dmem_stall` from the wrapper) | `go` low: F, D and E all hold; the forward selects freeze; `op_held` freezes the operands; the E→M register inserts a bubble | **M and W drain.** `mcycle`, `mtime` keep counting |
| **`bubble`** | a load in E whose `rd` is a source of the instruction in D — **or `imem_stall`**, the wrapper resolving a fetch translation | `fd_go` low: F and D hold | **E drains into M** — which is the point: the load's data does not exist until M has run |
| `d_redir` | a taken prediction in D | — | kills the one instruction already fetched behind the branch; the branch itself continues into E to be checked |
| `e_redir` | a mispredict resolved in E | — | kills the two behind it |
| `trap_redir` | a trap, `mret` or `sret` at an instruction boundary | — | kills the two behind it, and suppresses the trapping instruction's own writeback and CSR write. [Only the PC moves this cycle](#the-trap-redirects-everything-else-lands-a-cycle-later) |
| `halted` | a fault with no handler, or the external halt input | **everything, W included** | nothing |

Two enables carry all of it. One gates the whole pipeline and is low when the
core is halted or stalled; the other gates fetch and decode alone and is
additionally low on a bubble. Every register in the core takes one of the two.

### A fetch stall is a bubble, not a stall

`imem_stall` — the wrapper saying it cannot name a physical fetch address yet —
joins `bubble` rather than `stall`, and the choice is not cosmetic. **It is the
same shape as a load-use bubble:** F and D hold while E drains, so no
instruction enters E until fetch can name one, and nothing already in flight is
delayed by a translation it does not need. Put in `stall` it would hold the
instruction in E as well, stretching a multiply or an in-flight memory access
for a reason that has nothing to do with it.

It is asserted while the fetch page register is being refilled, for one cycle
after the refill lands, for one cycle after an `SFENCE.VMA` retires, and for the
one cycle a trap's privilege change takes to land — the four cases in
[memory-system](memory-system.md#fetch-is-translated-through-one-page-register).

**The instruction-word capture has to be suppressed while it is asserted.** The
hold register exists because the array keeps answering a frozen `pc`
([above](#the-instruction-word-has-to-be-captured-when-fetch-stops)) — but under
a fetch stall the address on the array is not yet a physical one, so the word on
the bus belongs to nowhere, and capturing it pins that word in D for the rest of
the stall.

Priority is **trap, then mispredict, then prediction**:

a trap redirect wins over a mispredict, which wins over a prediction, and the
next-PC mux picks its source in that order.

### The trap redirects; everything else lands a cycle later

The trap decision is the deepest thing in E. It carries the effective-address
adder — through `misalign` — and it selects a cause, a delegation target and a
vector. Driving roughly two hundred CSR flip-flops' clock enables from it made it
the whole node's critical path.

So the cycle is split:

```
   cycle T      trap_take ──▶ redirect_pc ──▶ pc          the PC moves
                trap, cause, tval, deleg, priv ──▶ registered copies

   cycle T+1    xepc, xcause, xtval ──▶ the CSR file      the state lands
                mstatus stack bits, priv                  from the copies
                                                          fetch is HELD
   cycle T+2    the handler's first instruction is fetched
```

Three properties make that safe, and they are the argument rather than the
description:

- **the handler's first instruction is two cycles behind the redirect**, so no
  instruction exists that could read the intermediate state;
- **fetch is held for the intervening cycle**, because whether the new PC is
  translated depends on `priv`, and `priv` is the one consumer that would
  otherwise see the stale value;
- **the trapping instruction's own writeback and CSR write are suppressed in
  cycle T**, by the combinational `trap_take` — which is a single-bit term into
  a handful of enables, not into the file.

`retire` is registered for the same reason and is one cycle late, so `minstret`
counts correctly and late rather than promptly and expensively.

The same trap decision drives `SFENCE.VMA`'s invalidate, and there the
qualification is deliberately *missing*: the invalidate is **not** gated on
"and this instruction did not trap". **An invalidation may be spurious; it may
never be missed.** Qualifying it put the whole trap cone — the cause chain, the
delegation mux, the vector select — into the fetch page register's clock enable,
at 21 logic levels. A fence that fires on an `SFENCE.VMA` that then traps costs
one re-walk.

**The kill and the redirect are deliberately different signals.** The E stage's
own valid bit is cleared by the trap-or-mispredict pair only, *not* by the
redirect, and that asymmetry is what lets a D-stage prediction steer fetch while
the branch that caused it still travels into E to be checked against what the
predictor said. Gate the valid bit on the redirect instead and a correctly
predicted branch kills itself.

### Why M and W drain through a stall

**A stall belongs to whatever owns E. The bubble is inserted at the E→M
boundary and never by holding W.**

| stage | its register's enable | what a stall does to it |
|---|---|---|
| E | the whole-pipeline enable | holds — this is the instruction being stalled |
| M | the whole-pipeline enable, **plus an explicit clear on stall** | takes a bubble: its write-enable and load bits are forced low |
| W | **not gated on the pipeline enable at all** — only on halt | keeps advancing, every cycle |
| W−1 | the same as W | keeps advancing, every cycle |

The instruction already in M is not the one being held. Gating W on the pipeline
enable does not *delay* its writeback — it **throws the writeback away**,
because M has already advanced past it and there is nothing left to re-present.
The same mistake one stage further down, on the W−1 copy, is equally invisible
and moves nothing.

**The general rule: a stall belongs to the stage that raised it, and every stage
downstream of that one keeps draining.** A pipeline where a stall freezes
everything is only correct if nothing downstream holds state the frozen stage
will not re-present, and a registered writeback is exactly such state.

The debugging corollary is worth as much as the rule: **if a value goes missing,
check the drain before checking the forwarding.** The forwarding network is what
makes a wrong value appear; the drain is what makes a right value disappear.

### The one data stall: load-use

The interlock is narrow by construction — it fires only when the instruction in
E is a load writing a real register, and the instruction in D reads that
register as either source.

A load's data arrives in M. Forwarding it from there means putting the byte
align, the sign extension and the forward mux in front of the ALU in one cycle —
measured at **121 failing paths at 20 logic levels**. It is forwardable one
cycle later from W, where it is an ordinary registered value, so the interlock
buys exactly one cycle and the writeback register is what makes every forward
source a register rather than the tail of the load-align chain.

**Cost: +4.6 % cycles on Dhrystone**, because a scheduling compiler fills most
load delay slots. One cycle of IPC to keep the memory alignment network off the
ALU path.

### Nothing address-derived may reach `stall`

`stall` gates every pipeline register's enable and fans out across the whole
front end, including the predictor's stack pointer. The 64-bit effective-address
adder is roughly eight logic levels on its own, so anything that runs the adder
into `stall` starts two thirds of the way through the budget before it does
anything.

The core therefore exports a **decode-only** memory request: a single bit
meaning *the instruction in E is a load, a store or an atomic*, derived from the
E-stage control bits and nothing else. No address, no misalignment test, no
range decode — so a wrapper can decide to stall without the adder in the path,
and `stall` is register-derived throughout. The
wrapper registers its own range decode on the first cycle and steers later
cycles from registers ([memory-system](memory-system.md#one-handshake-for-every-access)).

The visible consequence: **a misaligned access issues its transaction and then
traps.** That is harmless here — a misaligned store already emits no byte
strobes, and a read has no side effect on this fabric — and it is why `mem_wait`
is safe in the trap boundary, which makes a misaligned access trap *once*, after
the transaction retires, rather than on every cycle it is held.

## What execute has to fit into one cycle

```
   the four operand sources, and the select captured in D:

       m_val    (E+1)  ─┐
       w_data   (E+2)  ─┤
       w_val_q  (E+3)  ─┼──▶ [4:1 mux] ──▶ op_rs1_raw ──▶ [op_held] ──▶ op_rs1
       rf_rs1          ─┘    e_s1_m / e_s1_w / e_s1_q

       (the same four sources, the same shape, for op_rs2)

   op_rs1 and op_rs2 then feed, in parallel:

       rv64_alu           1 cycle                     ─┐
       rv64_muldiv        8 cycles, or 66             ─┤
       the AMO sequencer  3 cycles, or 4              ─┼──▶ e_result ──▶ m_val
       rv64_csr           2 cycles                    ─┤
       e_pc + 4           the jal / jalr link value   ─┘

       op_rs1 + e_imm ──▶ ea ──▶ eff ──┬──▶ dmem_addr = {eff[63:3], 3'd0}
                                       ├──▶ misalign ──▶ the trap
                                       ├──▶ strb     ──▶ dmem_wstrb
                                       └──▶ eff_q    ──▶ mtval

       (eff is the AMO's latched a_addr instead, once one is past its first cycle)

       beq / blt / bltu ──▶ br_take ──▶ taken ──▶ mispred ──▶ e_redir
                                        target ──▶ redirect_pc

   [op_held] freezes both operands for the duration of any stall
```

### The ALU is one adder and one shifter

Not three of each, and both economies are worth naming because getting either
wrong is invisible in simulation and expensive in synthesis.

**Subtract is add-with-inverted-operand, and the carry out of that same adder
*is* the unsigned compare.** With `sub` asserted the adder computes `a - b`, and
its carry is set exactly when `a >= b`, so `SLTU` is the inverse of a wire
rather than a second 64-bit comparator. `SLT` differs from it only by the sign
correction: when the operands' signs differ, the negative one is smaller.

**One arithmetic right shifter covers `SLL`, `SRL`, `SRA` and all three `W`
forms.** A left shift is a right shift between two bit reversals, and a reversal
is wiring. Written as three separate expressions — `<<`, `>>`, `>>>` — synthesis
builds **three 64-bit barrel shifters**, which is what the module costs when
nobody checks: the shared form measured 539 LUT against 1,038 for the three, on
the same exhaustively verified behaviour.

The `W` forms are the same hardware: the shifter is fed a 32-bit operand and a
5-bit amount, and **every** `W` result is sign-extended from bit 31 once, at the
output, including `SLTU`'s — which is zero or one and extends to itself. Stating
it once is cheaper and safer than deciding per operation.

### The effective address leaves E combinationally

`ea = op_rs1 + e_imm`, and `dmem_addr` is `eff` with its low three bits cleared.
It is not registered, because the data arrays register their own address input
and a read has to be issued in the first cycle to be answered in the second.
**Everything else derived from the address is registered** — write data, byte
enables, range decodes, control-region decodes — and the reason is
[nothing address-derived may reach `stall`](#nothing-address-derived-may-reach-stall):
the address adder must not reach a global signal. That rule is the wrapper's to
apply and [memory-system](memory-system.md#registering-every-address-consumer)
carries it.

`eff` is `ea` normally and the AMO's latched `a_addr` once an atomic is past its
first cycle, so the address the write phase uses is the address the read phase
used.

### The store path replicates, it does not shift

`strb` already selects which byte lanes are written, so a sub-word datum only
has to be **present** in the lane it lands in — it does not have to be moved
there.

```verilog
2'b00:   st_data = {8{st_src[7:0]}};    // sb
2'b01:   st_data = {4{st_src[15:0]}};   // sh
2'b10:   st_data = {2{st_src[31:0]}};   // sw
default: st_data = st_src;              // sd
```

This is the one place on the page where the code is the argument. The obvious
alternative — shift the datum left by the address's low bits — is a **64-bit
barrel shifter fed by the forward mux**, sitting on the path from the writeback
register to a memory's data pins. The four lines above are **wiring**: a
replication is a fan-out, not a mux.

It measured **−235 LUT and +25.8 MHz** on the mesh compute unit and **−107 LUT
and +15.9 MHz** on the node processor — one expression, both units, area *and*
frequency. Every consumer applies the strobes per byte, so nothing above the
port can tell the difference.

**The general move: when a datum has to reach a position, ask whether anything
downstream already selects position.** If it does, put the datum everywhere and
let the existing selector do the work. It pays wherever the selector was going
to exist regardless — and it is worth nothing where you would have to build the
selector to use it.

## Three sequencers, and the two rules they all obey

Multiply-divide, atomics and the CSR write all take more than a cycle, all live
inside E, and all obey the same two rules: **latch the operands on entry**, and
**start exactly once**. Both rules exist for the same underlying reason — an
instruction that holds E sees a pipeline that keeps moving underneath it, so
neither its inputs nor its start condition can be re-read.

### Why the multiplier is 4 DSP and not 9 to 16

A flat 64×64 product wants 9 to 16 DSP48s. This issues **four 32×32 partial
products through one multiplier over four cycles** and accumulates them by
range, which measures 4 DSP — the number that keeps the whole system node inside
its 48-DSP budget, with 43 spent elsewhere in the node (32 in the transform
bank, 8 in the mover's random generator, 3 in the mover) as measured in
[performance](performance.md#in-context-inside-the-system-node).

```
   S_IDLE ──start──▶ S_MUL  cnt 0 1 2 3 4 5 ──▶ S_FIN ──▶ S_IDLE     8 cycles
                                └─────┘ └─┘
                                   │      └── the DSP pipeline draining
                                   └───────── four operand pairs issued

   S_IDLE ──start──▶ S_DIV  cnt 0 … 63 ──────▶ S_FIN ──▶ S_IDLE    66 cycles
                            restoring, one bit per cycle
```

**A DSP48E2 is a pipelined primitive, and using it combinationally forfeits
frequency for no area gain.** It carries A/B, M and P registers internally.
Driven combinationally, the path `cnt → operand mux → DSP → accumulator add` was
**23 logic levels and 11 CARRY8 in one cycle** and held the whole core to
216.5 MHz. Registering it — two stages, which is the shape the primitive wants —
bought **+47 MHz, and the LUT count fell**.

Registering it means one stage on the operands and one on the product, and those
two stages are why the multiply state runs to a count of five while only counts
zero through three issue operand pairs: the last two cycles drain the DSP. Each
product's **range and validity travel alongside it, two deep**, so the
accumulator knows which quarter of the 128-bit result an arriving product
belongs to without recomputing it from a counter that has already moved on.

**The portable form: a hard multiplier is a pipelined primitive, and the
registers you put around it are the ones already inside it.** Vivado absorbs
them; you pay flip-flops the device has in abundance and get the primitive's
rated frequency. Use it combinationally and you pay LUT for a carry network,
lose the frequency, and get nothing back. The same is true of a block RAM's
output register.

Two economies inside the accumulate:

- **Each partial lands in its own range**, so the adder is as wide as the range
  rather than as wide as the product — step 0 is a plain assignment, steps 1 and
  2 are 96 bits, step 3 is 64.
- **The signed correction is 64 bits wide, not 128.** Both subtrahends are
  shifted left by 64 and cannot borrow into the low half, so the low half is the
  accumulator untouched and only the high half needs an adder.

A one-bit *already fired* flag is what stops the unit being relaunched on every
stalled cycle — which, for a divide, would restart it forever. **Start exactly
once** is the second of the two rules every sequencer in E obeys, and it is only
needed because the start condition is a level rather than an edge: the
instruction sits in E for the whole operation, so the condition that launched it
is still true on every cycle of it.

Divide is restoring, one bit per cycle, on magnitudes with the signs reapplied
at the end, and it handles the two cases the specification mandates: divide by
zero, and `−2⁶³ ÷ −1`. `DIVW` sign-extends its operands where `DIVUW`
zero-extends them; reversing that is the classic RV64M bug and it shows only on
negative inputs.

#### Why the divider is built here and refused on the RV32 PE

The RV32 PE [costs a divider at 200–300 LUT and turns it
down](../rv32-pe/microarchitecture.md#why-div-and-rem-are-a-different-answer):
35 cycles against libgcc's 60–80 is a 2× on an instruction a controller issues
approximately never, and its cost is a fixed structure rather than a marginal
one. The reasoning is sound and this core reaches the opposite answer, which is
worth understanding because **neither answer is about the divider**.

| | RV32 controller PE | RV64 system core |
|---|---|---|
| what runs on it | one kernel, chosen and scheduled ahead of time | a runtime, executing whatever it is given |
| where a divide appears | in a kernel the author can restructure | in allocation, time conversion, and code the author of this core never sees |
| the option to not have it | real — strength-reduce it away at compile time | **absent.** A runtime cannot decline to execute an instruction its compiler emitted |
| the marginal cost | its own subtractor, its own remainder and quotient registers, its own sign fixups | **near zero** — it shares the multiplier's sequencer, its counter, its operand latches and its finish state |

The second row is the design pressure and the fourth is the arithmetic. Once a
core has committed to a multi-cycle unit in E with a sequencer, an operand
latch and a start-once flag, **the divider is an extra state and a subtractor
rather than a new structure** — which is why RV64M arrives whole here and why
`M` on the RV32 PE stopped at the multiply.

The general form is the same one the RV32 page reaches from the other side: **a
multi-cycle unit is priced by what the machine already has, not by what the unit
does.** There it was "cheap in a machine that already has a way to park an
instruction"; here it is "cheap in a machine that already has a sequencer in the
stage you would put it in".

### Why an atomic holds execute for three or four cycles

```
   A_IDLE ──▶ A_RD ──┬──▶ A_WR ──▶ A_FIN        amoadd, amoswap, … , sc that succeeds
                     └──▶ A_FIN                 lr, and sc that fails
```

- `A_IDLE` issues the read and **latches `a_addr` from `ea` and `a_srcq` from
  `op_rs2`**. Both come through the forwarding network, which is valid only in
  this cycle.
- `A_RD` captures the loaded word, shifted down by the address's low bits, and
  decides: `LR` takes the reservation and finishes; an `SC` whose reservation
  does not match returns 1 and finishes; everything else proceeds to the write.
- `A_WR` computes the new value and emits it on the ordinary store path, with
  the modified datum in place of `op_rs2`.
- `A_FIN` releases the stall and presents the result — the old value for an
  `AMO`, the loaded word for `LR`, 0 or 1 for `SC`.

`LR`/`SC` carry a **single reservation**: this is one hart, so the only ways to
lose it are another `LR` or any `SC`, which is exactly what the specification
allows.

The sequencer advances only on `!dmem_stall`, and the state is reset **only when
the AMO is gone**, never merely because memory is slow. Resetting it on a
stalled cycle restarts the sequence and re-issues every phase it had already
completed — a single `amoadd` becoming thousands of writes. The general form of
that trap is worth carrying away: **narrowing an `if` changes its `else`.**

### Why a CSR instruction costs two cycles

Fan-out, and nothing else. Driven combinationally, the write data would run
`writeback value → forward mux → operand → the read-modify-write → every CSR
register's data pins` — one path into nineteen 64-bit registers. Registering it
costs a cycle on an instruction a runtime executes rarely, which is the cheapest
thing in the core to spend.

So a CSR instruction stalls E for exactly one cycle while its write data is
captured, and the write lands at the end of that cycle. **The read is
unaffected**: the read data is combinational on the registered address, so a CSR
read still returns the pre-write value, which is what the instruction owes.

The write enable is deliberately **narrower** than "not stalled and not
trapping". Both of those terms carry the misalignment test, which carries the
forward mux and the address adder — and putting the adder on a CSR register's
clock enable measured 17 logic levels. A CSR instruction is never a load, a
store or an atomic, so it cannot stall on memory and a misalignment trap cannot
coincide with it. **The only things that can legitimately kill its write are an
illegal instruction and a pending interrupt**, and those are the only two the
enable tests. *Illegal* here covers a bad encoding, a CSR address that does not
exist or is above the current level, a write to a read-only CSR, and a
privileged instruction below its level — all of which the CSR file resolves from
the encoding rather than from a table
([architecture](architecture.md#what-a-level-is-checked-against)).

That narrowing is a general move worth naming: **when a guard is too wide, the
fix is to enumerate what can actually fire rather than to reuse a convenient
aggregate.** The aggregate drags in every term its other users needed.

## Why the predictor is bigger than the RV32 PE's, and differently shaped

Not "the same thing, larger". The two cores' predictors answer different branch
populations, and each of this one's three additions is a response to a specific
property of runtime code that a kernel loop does not have.

| | the RV32 PE's branches | this core's branches |
|---|---|---|
| what runs | one hot loop inside a compute unit | a runtime: schedulers, allocators, drivers |
| the footprint | narrow — a handful of backedges, hit constantly | **wide** — many branches, each hit rarely |
| the directions | positional, and a backedge is taken almost always | **data-dependent**, so position predicts poorly |
| the calls | few, and often inlined | **dense**, and a function is called from many sites |
| what it buys | remove the taken-branch penalty of a loop | remove the taken-branch penalty of code with no loop to speak of |

So the RV32 PE ships a 32-entry BTB with 2-bit counters, and it is right there —
a wider table buys nothing when the working set is four backedges. This core
adds a **larger BTB** for the footprint, **gshare** for the data-dependent
directions, and a **return-address stack** for the call density, because a BTB
predicts returns badly: a function called from N sites has N return targets and
one entry thrashes between them.

**Note which of those three is free.** The BTB and the direction table are block
RAM, so entry count buys depth rather than logic; the stack is the one that
costs real LUT, and it is the one whose contribution is currently unmeasured
([below](#the-return-address-stack-and-what-it-is-currently-connected-to)).

| structure | size | primitive | why |
|---|---|---|---|
| BTB | 256 entries | block RAM | a wide branch footprint, not one loop |
| gshare PHT | 1024 × 2-bit | block RAM | direction depending on data, not position |
| PHT mirror | 1024 × 2-bit | block RAM | so the update reads the old counter without stealing the lookup port |
| global history | 8 bits | flops | XORed into the PHT index |
| RAS | 16 entries | top of stack a flop, the rest LUTRAM | a BTB predicts returns badly |

### The target is 39 bits, and that is a block-RAM decision

`{valid, tag[10:0], target[38:1]}` is **51 bits** and maps to a block RAM. A
full 64-bit target makes the entry **76 bits**, and **a block-RAM port is 72
bits at its widest** — so the array would silently become LUTs, with no warning
from the tool. Sv39 makes 39 bits the real address space, so nothing is lost.

**The rule, and it is a property of the device rather than of this design: a
block-RAM port is 72 bits at its widest, and an array one bit over that becomes
LUTs silently.** No error, no warning, and a structure that should have cost
zero logic costs hundreds. It decides the shape of this entry, it decides the
shape of the TLB entry
([memory-system](memory-system.md#an-entry-is-57-bits-because-the-card-is-40-bit)),
and it is why every array in this core names its primitive instead of being
inferred.

The practical form: **check that
the synthesis report says block RAM where you expected block RAM.** A 74-bit ROM
elsewhere in this tree came back as 2,798 LUT and zero block RAM for exactly
this reason. The same arithmetic decides the shape of the TLB entry
([memory-system](memory-system.md#an-entry-is-57-bits-because-the-card-is-40-bit)).

### Nothing here is architectural

E resolves every branch against the real answer, so a wrong prediction costs the
redirect penalty and never correctness. Three consequences follow, and all three
are why the structure is as cheap as it is:

- the tag can be short and the tables can alias;
- **history updates on the resolve, not on the prediction**, so there is no
  speculative state to repair on a misprediction. The cost is staleness, which
  costs accuracy;
- **the resolve is registered on the way in.** E's comparator driving a
  read-modify-write of a saturating counter is a long path for something
  non-architectural, and a cycle of staleness can only cost a prediction.

Neither array has a reset, so a **power-on sweep** writes every entry before a
prediction is allowed out: the BTB to all-zero, the PHT to `01` — weakly
not-taken. `init_q` matches the array's read latency so the sweep's last write
is visible before `init_busy` drops.

A jump forces its counter to `11` rather than incrementing it; a conditional
branch saturates up or down by one.

> **A combinational loop lives one line away here.** `d_predict` must not read
> `redirect`, because `redirect` is driven by `d_redir` which is driven by
> `d_predict`. Verilator reports that as "Active region did not converge" and
> names the **module**, not the signal. `d_redir` carries the `!e_redir` term
> instead.

### What a misprediction costs

| | killed |
|---|---|
| a branch or jump the predictor called right | **0** |
| a taken prediction made in D | 1 — the instruction already fetched behind it |
| a misprediction resolved in E | 2 |

A misprediction is `(taken != e_pred_t)` **or** a taken branch whose target
differs from the predicted one, so an aliased BTB entry with the right direction
and the wrong target is caught.

### The return-address stack, and what it is currently connected to

The stack itself is complete. `p_call` pushes the link address and moves the old
top into the array; `p_ret` pops it back; the top of stack is a flop precisely so
a prediction costs no array read, and the rest is LUTRAM because a 16:1 mux on
64 bits would be roughly 320 LUT.

**The answer is a two-way mux — the stack, or the BTB — and the two inputs to
that mux describe different instructions.**

| the mux's select and stack data | the BTB's hit test |
|---|---|
| taken from *is this a return*, **registered one cycle** | computed from the decode-stage PC, **not registered** |
| therefore describes the instruction that was in decode **last** cycle | therefore describes the instruction in decode **now** |

Work a return through. It is in decode at cycle *T*: the stack pops at *T*, and
the answer offered at *T* is the BTB's, because the select bit still describes
the instruction before it. The stack's answer — with the correct return address
on it, sampled before the pop — is offered at *T+1*, to whatever is in decode
then. That is the return's fall-through, which is not usually a branch or a
jump, and the prediction path only consumes an answer for a control instruction.

As built, **a return is predicted from its own BTB entry**, which is the case a
stack exists to avoid: a function called from N sites has N return targets and
one BTB entry thrashes between them. This is an accuracy question and not a
correctness one, for the reason above. The measured predictor gain reported in
[performance](performance.md#what-the-predictor-buys) is what the BTB and the
direction table deliver together; **no separate measurement of the stack
exists.**

## Why the writeback stage is a register and nothing else

```
   dmem_rdata ──▶ >> {m_off, 3'b000} ──▶ shifted ──▶ sign / zero extend ──▶ load_ext ─┐
                                                       per m_f3                       │
                                                                                      ├─▶ wb_val
   m_val ─────────────────────────────────────────────────────────────────────────────┘
                                                                            m_ld selects
```

M is one cycle and contains no logic of its own beyond the load alignment: the
data array registered its address in E, so the data arrives here. `m_off` is the
address's low three bits, captured in E, so the shift amount is a register and
not the tail of the adder.

W is a register and nothing else, and that is its whole purpose: it makes the
distance-2 and distance-3 forward sources registered values instead of the tail
of the load-align chain. W−1 follows it for the same reason, and drains with it.

## Why the register file is LUTRAM and not block RAM

31 × 64, two reads and one write, as **two mirrored single-read arrays written
identically** — a simple dual-port RAM has one read port, so two reads means two
copies. The storage doubles; the LUT count does not.

`x0` is not stored. Refusing the write costs one AND gate and removes the case
where a stale `x0` can exist at all, so the read side only has to select. The
zero-select is registered, because the data it qualifies arrives a cycle after
the address that asked for it.

`MEM_PRIM` is a parameter and the choice is a measured trade, not a preference:

| `MEM_PRIM` | LUT | BRAM | Fmax | failing paths |
|---|---|---|---|---|
| `block` | 4,699 | 4 | 264.1 | 67 |
| **`distributed`** (default) | **4,704** | 2 | **323.7** | **0** |

At `block` the binding path is the array's own clock-to-out. **Block-RAM
clock-to-out is slow and no logic restructuring moves it** — it stayed the top
path through two rounds of optimisation, first into the forward mux and then
into the `jalr` adder. `distributed` takes the array out of the path for 5 LUT.

Both rows are out-of-context synthesis of `rv64_core` on
`xcvu13p-fhgb2104-2L-e`, Vivado 2024.2, at a 3.333 ns request, taken at the same
point in the core's history; the absolute figures are superseded by
[performance](performance.md), and it is the *difference* that is the result.

**That 5 LUT is not a stable price.** The same swap cost 89 LUT before the
forward select moved to D. Removing logic from a path changes what the next
change to that path is worth, and the two measurements are not comparable — the
later one is the real price.

### The register file's own cost, and the row that looks like it

The array is **4 Kbit of storage** — 31 usable entries of 64 bits, mirrored. Its
own cost is small and measurable: **147 LUT at `distributed`, or 67 LUT and
2 RAMB18 at `block`**, measured with `rv64_regfile` as its own top.

**A hierarchical row for this instance inside the assembled core reports well
over a thousand LUT — 1,555 in the node's own run — and that number is not the
register file.** It has been the largest single row in the breakdown, larger
than the multiplier and divider together, which makes it the most misread figure
this core produces. The cause is
`-flatten_hierarchy rebuilt`: synthesis dissolves boundaries to optimise across
them and attributes the resulting leaves to whichever boundary survives nearest.
`u_rf` sits between the register file read and the E stage that consumes it,
which is precisely where the forwarding muxes and the operand select live, so
they land on it.

The evidence is in the same report rather than in the argument: **`u_alu` and
`u_dec` have no row at all.** Their logic did not disappear; it was absorbed
into neighbours, and this is one of the neighbours.

**The general rule for reading any hierarchical utilisation report: a row is an
attribution, not a measurement, and under a flattening flow the attribution is
approximate by construction.** When the question is *what does this module
cost*, synthesise it as its own top; when it is *what does this design cost*,
read the total. The two questions do not share an answer —
[performance](performance.md#a-module-does-not-have-a-lut-count) carries the
full form of that rule.

## Why there is no address-generation stage

**The RV32 controller PE has one and this core does not, and the difference
costs one cycle on every memory access — the local scratchpad included.**

On the RV32 PE the effective address is computed in EX and the array access
happens in MEM, so the address arrives at the arrays a whole stage early and
everything downstream of it — the range decode, the write enables, the stall —
has a registered address to work from. This core computes the address in E and
consumes the data in M, with nothing between, so **every consumer of the address
is one adder-delay behind the signal it has to drive.** The 64-bit adder is
roughly eight logic levels on its own against a budget of about eleven for a
whole path.

Two ways out, and the core takes the second:

| | what it costs |
|---|---|
| add the stage | one more register boundary, a seventh; every branch and trap redirect gets a stage deeper to kill; the E→M forwarding distances all shift by one and the whole three-source network is re-derived |
| register each consumer individually | **one extra cycle on every access**, because the first cycle is now decided from decode alone and the range decode only steers cycles two onward |

The second is what is built, and the rule it produced —
[register every consumer of the effective address except a memory *read*
address](memory-system.md#registering-every-address-consumer) — is stated in the
memory system because that is where the consumers live. It measured **−227 LUT
and +13.8 MHz** applied one consumer at a time, which is a real result; the
point here is that **each of those registrations is a local answer to a
structural absence**, and the structural fix is the stage.

The general form is worth having: **a pipeline that computes an address and
consumes it in the very next stage has no slack anywhere downstream of the
adder, and pays for that in either a boundary or a cycle.** Which one is cheaper
depends on how much else the redirect path is already carrying — here the
predictor and the trap logic both kill two instructions, and a seventh boundary
would make that three.

## Why there is no scoreboard

**A multi-cycle unit stalls E, which is why a divide costs 66 cycles of the
whole machine rather than 66 cycles of one instruction.** The alternative is to
let a long operation retire out of order, tracked by a scoreboard or a pending
bit, so the instructions behind it keep issuing.

It is refused, and not on area. **The hazard unit is the whole of this core's
complexity budget** — three forward sources selected by position, two stall
rules, and nothing else — and every one of those three sources is *positional*:
the distance-1 source is "whatever is in M", not "the producer of this
register". Out-of-order retire ends that. A forward source stops being a stage
and starts being a search, `op_held` stops being a single latch pair, and the
select that is precomputed in D stops being computable there at all, because D
would no longer know which stage its producer will be in.

The RV32 PE reached the same conclusion for the same reason, and the SIMT PE is
the contrast that proves the rule: it carries multi-cycle units cheaply because
barrel scheduling had already given it a way to park an instruction — one
pending bit per wave — before any multi-cycle unit was proposed. **A multi-cycle
unit is cheap in a machine that already has a way to park an instruction and
expensive in one whose whole complexity budget is positional forwarding**, and
that is the general statement of why the divider is 66 stalled cycles here and
would not be there.

## Why there is no hit-under-miss

The core stalls in E for the whole of a memory access, and that single property
is load-bearing three modules away: it is what lets the node-port arbiter be a
priority mux rather than a queue, because **at most one client can ever be
active** ([memory-system](memory-system.md#the-node-port-arbiter)).

A non-blocking L1 is the single largest lever on the fabric-latency numbers —
each unit of fabric latency currently costs each access one core cycle, with
nothing overlapped ([performance](performance.md#the-fabric-latency-sweep)). But
it does not arrive alone. It needs a miss-status file, it needs the arbiter to
become real arbitration with per-client response routing, and it needs the core
to be able to park an instruction, which is the section above. **It is one
change that reprices three modules**, which is why it is a decision rather than
an optimisation.

## What this microarchitecture deliberately does not have

Everything else, briefly. The three above have arguments; these are simply
absent.

| Not built | Consequence |
|---|---|
| speculative branch history and any repair for it | the predictor's history is stale by a resolve, which costs accuracy and never correctness — the tables can alias freely |
| dual issue, register renaming, wide fetch | one instruction word per cycle, one instruction in E, and the whole forwarding network assumes it |
| a second write port on the register file | write-after-write and write-after-read hazards cannot be constructed, so nothing checks for them |
| a floating-point unit or a float register file | [architecture](architecture.md#the-instruction-set) |
| an inferred memory primitive anywhere | every array names its primitive, because read latency here is pipeline structure and inference can move it between tool versions |
| a second clock domain | the whole core is on one clock; crossing to the host or to DRAM is the AXI surface's job |
