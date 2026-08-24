---
title: SIMT PE — microarchitecture
summary: How it is built, in diagrams — the pipeline and its two PCs, the three hold signals and the loop between them, the lane-serialising LSU, the IPDOM pair-stack, and the halt-and-flush.
tags:
  - architecture
  - pe
  - gpu
  - simt
  - microarchitecture
---

# Microarchitecture

`kht_core` is a **rebuild** on the base core's shape, not an extension of it.
The base core's six register boundaries and its hazard style are kept because
they are what close at this clock. What changes is which register file an
ordinary RV32I opcode addresses.

## The whole unit

```
+----------------------------------- kht_pe -------------------------------------+
|                                                                                |
|   fabric port                 +--> [ kht_predec ] --> [ ictl window, 60b ]     |
|      |  CU_DATA  --------------+--> [ imem window, 32b ]   [ scratchpad ]      |
|      |  CU_INST  --> kick -----+          |     |                |             |
|      |  completion <-----------|----+     |     |                |             |
|      |                         v    |     v     v                v             |
|   +--------------------------- kht_core --------------------------------+      |
|   |                                                                     |      |
|   |   [ nxt[] per wave ] --> [ fetch ] --> instr + ctrl ---+             |      |
|   |   [ round-robin    ]      f2_pc/f2_wave    (no decode) |             |      |
|   |                                                        |             |      |
|   |            +-------------------------------------------+             |      |
|   |            |                    |                        |           |      |
|   |            v                    v                        v           |      |
|   |   [ scalar file  ]     [ ------- kht_unit ------- ]   [ LSU ]        |      |
|   |   [ s0..s31/wave ]     [  kht_vregfile  x/lane   ]   [ walks ]       |      |
|   |   [ ONE SALU     ]     [  active mask + IPDOM    ]   [ lanes ]       |      |
|   |   [ + writeback  ]     [  kht_valu   lane array  ]      |            |      |
|   |            ^                       |                    |            |      |
|   |            +--- ballot/redux/------+                    |            |      |
|   |                 vreadfirst                              |            |      |
|   +---------------------------------------------------------|------------+      |
|                                                             v                   |
|                                                     [ rv_l1 ] --> fabric --> MAG |
+--------------------------------------------------------------------------------+
```

## Decode does not happen at fetch — it happens at image load

The base core registers its decoded outputs into EX (`rv_id` has
`output reg x_valid`) and closes at 410 MHz. `kht_core` had **no decode stage** at
all: `wire instr = imem_data`, and then decode, operand read, address generation
and the PC update all in one cycle. That is the whole of why it started at 182.

Adding a decode stage would cost a cycle of branch latency. **Predecoding does
not**, because every decode signal is a pure function of the instruction word:

```
   IMAGE LOAD (a CU_DATA burst, once per shader)
                                   +--> [ imem  ] 32 bits, the instruction
   CU_DATA word --> imem_wr_d -----+
                                   +--> [ kht_predec ] --> [ ictl ] 60 bits
                                          combinational       stored BESIDE it

   FETCH (every cycle)
   imem_addr ---+--> imem  --> instr   ]  both READ_LAT=1, same address,
                +--> ictl  --> ctrl    ]  so they arrive together

   wire is_salu = ictl[C_SALU];        <- a wire off a memory, not a decode
   wire is_vmem = ictl[C_VMEM];
   ...
```

`kht_ctrl.vh` is the bit layout and is included by **both** the producer
(`kht_predec.v`) and the consumer (`kht_core.v`), so the two cannot disagree.
Measured: four of the thirteen levels between the window and the per-wave PC's
clock enable were spent producing `mem_store` alone.

**Cost: one memory the width of the control word** (`KHT_CW = 60` bits ×
`IMEM_WORDS`), which the tool maps to the same BRAM class as the instruction
window. **+55 MHz** on the assembled PE. It was 50 bits before the float tier and
RV32M each claimed some — `kht_core` checks the literal `60` in its ports against
`KHT_CW` at elaboration, because widening the word and leaving a port behind has
already cost one synthesis run to an out-of-range part-select.

## The timing budget is about nine levels, and that is arithmetic

Everything in the front end starts at the instruction window's block RAM, and
that costs 0.909 ns before any logic runs:

```
   3.333 ns  the 300 MHz period
  -0.909     RAMB36E2 clock-to-out                     <- not negotiable
  -0.318     the first net (fanout 373 on x_rs2)
  -0.056     clock skew
  -0.035     clock uncertainty
  -0.050     setup at the destination
  =========
   1.965 ns  for LOGIC LEVELS AND THEIR ROUTE

   measured cost of one level, unplaced:  ~0.04 logic + ~0.20 route = 0.24 ns
   =>  about NINE levels, and route is 63% of every one of them
```

So a cone at ten-plus levels is a defect, a cone at nine is at the line, and
**shortening a level is worth 0.24 ns while shaving a LUT off one is worth
0.04.** That is why the fixes below are all about removing levels or fanout, and
why "add a pipeline stage" has been the last resort rather than the first.

The **register-class rule** is the whole design and it is visible in that
picture: an ordinary RV32I opcode drives `kht_unit`; the scalar file is reached
only through custom-2/-3. See [isa](isa.md).

## Fetch: three cycles, and the third one is worth 0.83 ns

The instruction window is block RAM, so there are three stages: `f1` is the
address issued into it, and `f2` is the instruction **registered in fabric**
beside the PC and the wave it belongs to.

```
                 cycle 0     cycle 1     cycle 2     cycle 3
  imem_addr      0x04        0x08        0x0C        0x10
                    \           \           \           \      one cycle of
                     \           \           \           \     block RAM
  f1_pc          0x04        0x08        0x0C        0x10
  imem_data        --        [0x04]      [0x08]      [0x0C]
                                 \           \           \     ONE FABRIC FLOP
                                  v           v           v
  f2_pc            --          --          0x04        0x08
  instr            --          --         [0x04]      [0x08]
                                           ^^^^^^
                                           f2_pc and instr name the SAME
                                           instruction, and instr is a REGISTER
```

**Why the flop is the single largest timing item in the design.** Executing
straight off the window's output makes every downstream cone start at a
RAMB36E2, and that primitive's clock-to-out is **0.909 ns** — 36 % of a 2.5 ns
period, spent before any logic runs. Off a flip-flop the same cone starts at
**0.077 ns**.

```
   at 400 MHz (2.500 ns):

   off the block RAM        off the fabric register
   -0.909  clock-to-out     -0.077  clock-to-out
   -0.116  skew/unc/setup   -0.116  skew/unc/setup
   =1.475 ns for logic      =2.307 ns for logic
   ~6.7 levels at 0.22      ~10.5 levels at 0.22
```

It has a second effect that is not about the start point at all: **`max_fanout`
works on a register and cannot work on a block RAM.** `rs2` was asked to
replicate and stayed at fanout 359 with 0.413 ns of route, because the tool had
no driver it was allowed to duplicate. Behind the flop it does.

**The price is one more cycle of redirect latency.** Two fetches are in flight,
so a taken branch kills the one in `f1` as well as the one being issued.

### Trap 1 — driving the window from the architectural PC

`pc_q` only advances when an instruction **retires**, which is a cycle after the
fetch should already have moved on:

```
  pc_q           0x04        0x04        0x08        0x08
  imem_addr      0x04        0x04        0x08        0x08
  instr         [0x04]      [0x04]      [0x08]      [0x08]
                             ^^^^^^                  ^^^^^^
                             EVERY INSTRUCTION EXECUTES TWICE
```

### Trap 2 — letting the address advance while held

`f1` is frozen by the same `hold` as `f2`, so an address issued during the stall
fetches a word that the resumed `f2 <= f1` no longer names — the instruction in
flight is silently replaced by a later one:

```
  BROKEN:  imem_addr = nxt[cur]
                 cycle 0     cycle 1     cycle 2     cycle 3
  hold           0           1           1           0
  imem_addr      0x08        0x0C        0x10        0x14
  f1_pc          0x08        0x08        0x08        0x08     <- frozen
  imem_data     [0x04]      [0x08]      [0x0C]      [0x10]
                                                     ^^^^^^
                                         on resume  i2 <= [0x10]  but f2_pc <= 0x08.
                                         0x08 NEVER EXECUTES. No error, no trap.

  FIXED:   imem_addr = hold ? f1_pc : nxt[cur]
  imem_addr      0x08        0x08        0x08        0x0C
  imem_data     [0x04]      [0x08]      [0x08]      [0x08]
                                                     ^^^^^^ i2 <= [0x08], f2_pc <= 0x08
```

This is the same trap the two-stage front end had one stage earlier, where the
window was read combinationally and the *decode* swapped onto the next
instruction while `f2_pc` still named this one.

### One pointer per wave (G7)

`nxt[WAVES]` is the fetch pointer **and** the architectural PC — a separate
committed copy bought nothing, because a redirect rewrites this array directly.
Round-robin picks a live wave every cycle:

```
   cycle    0      1      2      3      4
   cur      w0     w1     w2     w0     w1
   fetch    nxt[0] nxt[1] nxt[2] nxt[0] nxt[1]
              \      \      \      \
   f1       . w0     w1     w2     w0
   f2       .  .     w0     w1     w2
   MEM      .  .  .  w0     w1     w2
                     ^^^^^^^^^^^^^^^^^ EX and MEM hold DIFFERENT waves,
                                       so the hazard between them cannot exist
```

**The kill is two conditions, not a register.** With two fetches in flight, the
one sitting in `f1` and the one being issued are both wrong-path — from the next
cycle `nxt` already holds the target — and each only if it belongs to the
redirecting wave:

```verilog
kill_ev  = go_nh && (br_take || wave_ends);
kill_f1  = kill_ev && (f1_wave == f2_wave);   // the fetch already in flight
kill_new = kill_ev && (cur     == f2_wave);   // the one being issued now
```

**Interleaving is what makes the extra stage cheap.** A single-wave front end
would eat both bubbles on every taken branch; with a live wave set, `f1` and the
issuing fetch usually belong to *other* waves and survive untouched.

A single-wave front end can kill unconditionally. Under interleaving the
instruction behind a branch usually belongs to a *different* wave and must
survive, so the unconditional kill would silently drop it.

### `hold` belongs at the clock enable, not inside the decision

Everything the front end writes is already under `if (!hold)`. Carrying `!hold`
inside `go` therefore put it **in series** with the redirect decision instead of
beside it — three LUTs in front of a clock enable it only needed to reach in one:

```
   BEFORE                                   AFTER
   hold ──> go ──> redir ──┐                br_take ──┐
                           v                 cur   ───┼──> nsel|rsel ──┐
   cur ─────────────> [ mess ] ──> CE        f2_wave ──┘                v
                                                                  hold ─> & ─> CE
   5 levels of hazard                       5 levels of hazard
   + go + redir + 2 more = 9                + ONE = 6
```

`go_nh` / `rdir_nh` / `kill_nh` are the hold-free forms; `nsel` selects the
fetched wave and `rsel` the redirected one, and `rsel` wins because it is the
data mux's select rather than a separate write:

```verilog
assign nsel = 1 << cur;                       // the wave being fetched
assign rsel = one_hot & {WAVES{rdir_nh}};     // the wave being redirected

if (!hold)
    for (wf = 0; wf < WAVES; wf = wf + 1)
        if (nsel[wf] || rsel[wf])
            nxt[wf] <= rsel[wf] ? redir_pc : (nxt[wf] + 32'd4);
```

**Measured: 1,633 failing endpoints → 70.** The whole front-end cone family —
both 512-endpoint PC groups and all four instruction-window write-address
groups — disappeared in one change. The same factoring is applied to the halt
FSM, whose reset pin was reached through `go` and two more levels of next-state.

**A fault kills the unit; an `ecall` retires one wave.** A dispatch is finished
when its last wave is, and the unit flushes and completes then. A fault is a
property of the program rather than of the wave that happened to hit it.

**What G7 does not do is hide memory latency.** One instruction is in flight at
a time, so a miss still holds the whole front end. See
[status](status.md#not-built-yet).

## The hold signals: three destinations, three meanings

This is the most delicate part of the core and it has failed in **both**
directions. They are not interchangeable.

```
   l1_stall  ──┐
   lsu_busy  ──┴──> base_hold ────────────────> kht_unit.x_hold   FREEZE the
                        │                                         MEM register
                        v
   vt_stall  ──────> hold ─────> fetch, nxt[], go, s_wen          DO NOT RETIRE
   warm_stall──────────┤
   lsu_want  ──────────┤
   s_hz      ──────┬───┤
   f_soon    ──┬───┤   │
               │   │   │
   lsu_need ───┴───┴───┴─────> kht_unit.x_defer                   READ it, do
                                                                  NOT COMMIT it
```

| Signal | Registered? | Fed to | Means |
|---|---|---|---|
| `base_hold` | from registers | `x_hold` | **freeze** the MEM register |
| `hold` | mixed | fetch, `nxt[]`, `go`, `s_wen` | do not **retire** this instruction |
| `x_defer` | **combinational** | `kht_unit` | do not **commit** it yet — `lsu_need \|\| s_hz \|\| f_soon` |
| `warm_stall` | **combinational** | `hold`, `x_split`, `lsu_need` | the vector read has not caught up, or a reduction's tree has not drained |
| `s_hz` | from registers | `hold`, `x_defer`, `x_tmc` | the scalar half's distance-1 interlock |
| `f_soon` | from registers | `hold`, `x_defer` | a float or multiply result lands in two cycles and wants the write port |

`hold` is `base_hold || vt_stall || warm_stall || lsu_want || s_hz || f_soon`,
and the walk's gate is deliberately `lsu_want` — `lsu_need`'s shallow half —
because `hold` ORs `warm_stall` and `s_hz` anyway, so `A || (B && !A)` collapses
and the walk's own gate stays off that cone.

### x_defer is not one signal's job any more

`x_defer` began as *"an LSU walk owes this instruction its data"*. Two more
things now need exactly the same treatment, and both were found the hard way:

```
   lsu_need   the walk owes it data
   s_hz       the scalar interlock stands, so a commit would use the stale read
   f_soon     the core is emptying writeback for a float result landing in two
```

> **EVERY CORE-LEVEL HOLD THAT IS NOT `base_hold` MUST DEFER THE COMMIT.**
> `kht_unit`'s internal `go` deliberately excludes this core's `hold`, so a held
> instruction is `go` on **every cycle of the hold**. That is idempotent for an
> integer register write and catastrophic for a float: a held `vfmul` re-launched
> the lane array every cycle, `f_soon` never cleared, and the whole machine
> wedged with sixteen waves runnable. `s2v` and `shflxor` are the same class one
> step milder — they would write from the stale read the stall exists to avoid.

The stack has its own version of the same rule. `kht_unit` gates the mask and the
IPDOM stack on `go_c = go && !x_defer` rather than on `go`, because a stack is not
idempotent: **a `join` sitting under another wave's `f_soon` popped once per
cycle until the stack underflowed and faulted.**

### Trap 3 — feeding a unit its own stall

```
        BROKEN                                  x_hold = base_hold || vt_stall

     +-------------------------------------------------+
     |                                                 |
     v                                                 |
  x_hold=1 --> MEM register FREEZES --> m_rd stays live |
                                              |        |
                                              v        |
                                       hz_raw stays 1 --+--> vt_stall stays 1
```

The stall becomes an input to itself: the MEM register freezes, its destination
stays live, the hazard against it never clears, and the core wedges on the first
back-to-back dependency. `kht_unit`'s header carries the warning; the DSP unit
carries the same one for the same reason.

**`x_hold` must be the rest of the machine's stall, never the unit's own.**

### Trap 4 and 5 — the walk's gate on the wrong side of the line

`lsu_busy` is a **register**, so on the cycle a walk is decided it is still low.
(`hold` takes the shallow half, `lsu_want`; `lsu_need` is that AND the two
interlocks. Which of the two is drawn below does not change either failure.)

```
  OUT OF hold:
                 cycle 0     cycle 1
  per_lane       1           1
  lsu_busy       0  <-- still low!
  hold           0           1
  go             1  <-- RETIRES, and the fetch advances
  lsu_run        0           1  <-- the walk now reads width/scale/base
                                    off the NEXT instruction

     symptom: ea = <garbage>, rgn = BAD, l1_req suppressed,
              the store vanishes and three of four checks still pass
```

```
  IN base_hold:
     lsu_need=1 --> base_hold=1 --> MEM register FREEZES
                                          |
                                          v
                                    hazard on the address operand never clears
                                          |
                                          v
                                    vt_stall=1 --> the walk can never START
                                          |
                                          +--> DEADLOCK
```

`x_defer` is the resolution — it suppresses the MEM **capture** without freezing
the register:

```
  cycle 0:  lsu_need=1  base_hold=0  x_defer=1
            kht_unit: !x_hold, so the MEM register ADVANCES -> bubble
            hazard clears
  cycle 1:  vt_stall=0, walk STARTS, lsu_busy <- 1
  ...walk...
  cycle N:  lsu_done=1 -> lsu_need=0 -> x_defer=0, hold=0
            kht_unit captures with ld_buf complete; the instruction retires
```

### Trap 6 — the vector read is registered, and EX consumers forget it

`kht_vregfile` in `block` mode has a **registered** read port, so `v1_rd` in the
EX stage belongs to the **MEM-stage** instruction. That is correct for the ALU,
which consumes it a stage later — and wrong for everything that consumes it
*in* EX:

```
   consumer                         stage    v1_rd is...
   -----------------------------    -----    ------------------------
   kht_valu (ordinary RV32I)        MEM      correct, by construction
   split's predicate                EX       the PREVIOUS instruction's rs1
   ballot / redux* / vreadfirst     EX       the PREVIOUS instruction's rs1
   the LSU walk                     EX+1     correct: lsu_run is a REGISTER,
                                             which buys exactly the cycle
```

Two cases, and they need different amounts of holding:

```
  NO HAZARD                             x6 written long ago
     cycle 1   split in EX, ra1={w,6}   v1_rd = x[rs1 of the PREVIOUS instr]
     cycle 2                            v1_rd = x6                  <- correct
     -> ONE cycle of hold

  HAZARD (andi x6 ; split x6)
     cycle 1   split in EX, hz=1        write of x6 lands at THIS edge;
                                        the read issued now is read-first
                                        and still returns the OLD x6
     cycle 2   hz cleared               v1_rd = OLD x6              <- WRONG
     cycle 3                            v1_rd = NEW x6              <- correct
     -> TWO cycles of hold
```

One rule covers both — **do not count the cycles the hazard stands**:

```verilog
else if (f2_valid && !vt_stall && (warm_q != 4'hF)) warm_q <= warm_q + 4'd1;
```

`warm_q` is a **counter**, not a bit, because the three classes owe different
numbers of cycles and each is a pure decode of a register — compare first, select
after, so only the 3:1 select is downstream of the instruction:

```
   ordinary EX consumer   1 cycle    the registered vector read
   a per-lane access      2          + ea_all_q behind it      (4 with the banked LDS:
                                                                + the per-lane region
                                                                bits and their reduction)
   a redux                LNW + 2    + the registered tree leaves and LNW pipelined levels
```

The symptom, before the fix, was a divergent shader in which *every* lane took
the else-branch and `kht_unit` reported an **all-zero active mask** twice:
`v1_rd` read as zero, so `t_set` was empty and `f_set` was the whole wave.

```
   split x6, pred = 0000_0000 (stale)
      mask <- t_set = 0000_0000     ---> "issued with an all-zero active mask"
      the TRUE body writes nothing
   join
      mask <- f_set = 1111_1111     ---> the FALSE body writes ALL lanes
```

The assertion `kht_unit` carries for an all-zero mask is what named this in one
line. It was written because *"the scheduler cannot issue such a wave"* was an
argument rather than a check.

## The mask is a write enable, not a datapath input

```
      lane 0      lane 1      lane 2      lane 3
     [ kht_valu ] [ kht_valu ] [ kht_valu ] [ kht_valu ]   ALWAYS computes
          |            |            |            |
          v            v            v            v
     +---------+  +---------+  +---------+  +---------+
     | we = 1  |  | we = 1  |  | we = 0  |  | we = 0  |   <-- w_mask
     +---------+  +---------+  +---------+  +---------+
          |            |            X            X
          v            v         dropped      dropped
      x[rd] bank   x[rd] bank
```

An inactive lane computes whatever it computes and its **write** is dropped.
Masking costs **one enable per bank** and nothing on the arithmetic path — which
is why G2 is +64 LUT and why Fmax does not move. See
[ladder](ladder.md#fmax-never-moves).

## The IPDOM stack is a memory, not an indexed flop array

```
  ONE WORD = ONE PAIR = exactly what ONE split pushes      WIDTH = 2*LANES

      +---------------------+---------------------+
      |     outer mask      |   false-lane mask   |
      +---------------------+---------------------+
       taken by the 2nd join  taken by the 1st join
            (phase = 1)            (phase = 0)

  kohaku_sdpram  MEM_PRIM="distributed"  READ_LAT=0  DEPTH = WAVES*PAIRS

      base = wave * PAIRS   +--------------+
                            |   pair 0     |
                            |   pair 1     |
      rd_a = base + sp-1 -->|   pair N-1   |  combinational read: a join
      wr_a = base + sp   -->|   (free)     |  costs NO cycle
                            +--------------+
```

Two pushes are **one write**, so a single write port suffices. A **phase bit per
wave** says which half the next `join` takes. The depth is entries/2 while *"a
split costs two entries"* stays true.

### A divergent if/else, cycle by cycle

```
  mask = 1111   sp = 0   phase = 0

  split (pred = 1100)
      write pair0 <- { outer=1111 , false=0011 }
      mask <- 1100        sp = 1   phase = 0
      |
      +--> the IF body runs on lanes 3,2
  join
      phase 0: mask <- pair0.false = 0011
                          sp = 1   phase = 1
      |
      +--> the ELSE body runs on lanes 1,0
  join
      phase 1: mask <- pair0.outer = 1111
                          sp = 0   phase = 0
      |
      +--> reconverged
```

Overflow is a **fault** — not a wrap, not a mask merge, not a truncation. A
masked-off lane that silently reactivates is a wrong answer with no witness.

A `split` while a wave is half-unwound would overwrite the pair it is still
reading. Balanced code cannot do it — every split has two joins — but *"cannot
happen"* is what this project has been wrong about before, so it is a simulation
assertion rather than an argument. The flop version had no such window to be
wrong in, and that is the honest cost of the rebuild.

## Hazards: stall at distance 1, no forwarding

```
   EX stage                MEM stage
   x_rs1 ──┐
   x_rs2 ──┼──= m_rd ?──> hz_raw ──> vt_stall ──> hold
           │      ^
           │      |
           └──────+ m_wen && m_rd != 0
```

A dependency **stalls** rather than forwarding. A `32*LANES`-wide bypass mux is
the widest path in the unit; the trade is a cycle on a dependency a shader
compiler can usually schedule around. A stalled cycle inserts a **bubble**
rather than freezing the stage, which is what lets the hazard clear.

**Two distances, not one**, because the lane ALU has a writeback stage: a write
lands two cycles after its read was issued, so an EX read misses both the write
happening at the end of this cycle and the one a cycle behind it.

```
   cycle    t        t+1      t+2
   EX       B        .        .        B reads the array at t
   MEM      M        B        .        M writes at end of t+1
   WB       W        M        B        W writes at END of t  <- B's read misses it

   stall B against M  (distance 1)  AND against W  (distance 2)
```

## The lane ALU starts at a flip-flop, not at the register file

The third place this project needed the same move, and the most expensive one:

```
   BEFORE                                   AFTER
   [ kht_vregfile ]  RAMB18E2               [ kht_vregfile ]
        | 0.845 ns  <- a THIRD of a              | 0.845 ns
        v            2.5 ns period               v
   operand select                           [ w1_q / w2_q ]  fabric register
        |                                        | 0.077 ns
   [ kht_valu ]                                  v
        |                                   operand select
   writeback mux                                 |
        |                                   [ kht_valu ]
        v                                        |
   [ kht_vregfile write ]                   writeback mux
                                                 v
   494 endpoints, 8 levels                  [ kht_vregfile write ]
   binding at -0.815
```

**0.845 ns of a 2.500 ns period is spent before any logic runs.** It is the same
statement as the instruction window's 0.909 and the scalar file's read, and the
same fix: a cone that starts at a block RAM begins a third of its budget in
debt, so put a flip-flop in front of the logic.

**Compare first, select after.** `x_rs1` was `mem_store ? rd : rs1`, which put a
decode term and a mux *in series* ahead of the comparator. The unit now takes
both candidates and the select as three separate ports, so every comparator
starts at a raw instruction field.

## The scalar half: one ALU, and a writeback stage

The scalar file is read and written in the same cycle by an instruction that
also decides the next PC. A census of all 1,833 failing endpoints at 237 MHz
found **1,728 of them in two cones, both hanging off this one read**:

```
   sfile read ──> SALU ──> result mux ──> sfile WRITE      704 endpoints
   sfile read ──> sv1 == 0 ──> redirect ──> per-wave PC  1,024 endpoints
```

That is read-modify-write in one cycle — exactly what the base core avoids by
having `rv_ex` / `rv_mem` / `rv_wb`. The split was made the same way and was at
first **forwarded**; it is now **interlocked, and the forwarding is deleted**.

```
   THE REGISTER IS IN FRONT OF THE ALU, NOT BEHIND IT

   cycle N        sfile[sa1] ──> operands ──> [ a1_q | a2_q | aop_q | aad_q ]
   cycle N+1      ALU on REGISTERS ──> sres ──> sfile[aad_q] <= {zero, sres}
```

Behind the ALU, the same two stages leave the read **and** the ALU in one cone
and only move the write; reading one 512-entry distributed RAM out of a block
RAM's output already costs 2.15 ns of a 3.34 ns period before the ALU starts. In
front, each cone starts at a register and neither is close to the limit.

**Forwarding is gone, and that is the point rather than the cost.** At distance 1
the result does not exist yet in *either* arrangement, so the scalar half
interlocks (`s_hz`) — the same rule the vector half already states. Distance 2
needs nothing at all, because the file was written at the end of N+1. The forward
comparator this deleted was the late input to the L1 tag cone.

**Only a real scalar read stalls.** `C_RDS1` / `C_RDS2` in the predecoded control
word say whether an instruction reads the scalar file at all — an ordinary RV32I
opcode has its operands in the *vector* file, so comparing its `rs1`/`rs2` here
would stall it against a scalar write it never reads, on most instructions,
constantly.

**The 33rd bit is the branch's zero flag, and its PLACEMENT is the whole story.**
The file is `reg [32:0]` — `{zero, value}` — so the branch's zero test is
**stored, not computed**: as a 32-bit reduce in front of the per-wave PC's clock
enable it was the 1,024-endpoint cone that held the PE at 182 MHz.

Where the compare itself sits was tried three times:

| where | result |
|---|---|
| a separate `reg [WAVES*32-1:0]` shadow array | +702 LUT, +512 FF, **−3.7 MHz** — the indexed-flop-array anti-pattern |
| `sres == 0` **after** the ALU, with the ALU still behind the file read | one endpoint, 17 levels, **−23 MHz** |
| `sres == 0` after the ALU, with the ALU **in front** of the file read | **built** — the cone starts at `a1_q`/`a2_q`, so ALU + compare + write fits |

The RTL is `sfile[aad_q] <= {(sres == 32'd0), sres}` — which *looks* like the row
that lost 23 MHz and is not, because the operand register moved in front of the
ALU underneath it. The rule:

> A stored flag only pays when the thing it is computed from starts at a
> register. It is the same statement as every other fix on this page.

### One ALU, and four classes rather than one case

custom-2 and custom-3 used to build a case statement each and mux between them —
two adders, two shifters, and a mux level after the slowest thing in the cone.
`kht_predec` maps both encodings onto one 4-bit `sop`, so there is one datapath
with a muxed operand.

Writing that datapath as a *single* case statement then cost what merging it
saved. The reported path was:

```
   s_op2[4:0] = s_sh ──> LUT ──> CARRY8 ──> CARRY8 ──> CARRY8 ──> CARRY8 ──> ...
                                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                 the tool folded the SHIFTER into the ADDER's
                                 carry chain, so the shift amount sits in front
                                 of all 32 bits of carry it has nothing to do with
```

Split into four parallel cones — add/sub, compare, shift, logic — with one mux
at the end, the shifter and the adder no longer share a chain:

```
   a1_q, a2_q ──┬──> s_add   (CARRY8 x4)         ──┐
                ├──> s_lt                          │
                ├──> s_ltu                         ├──> 5 ways, 2 steps ──> salu_r
                ├──> s_shf   (barrel, a_sh)      ──┤     LUT6 + MUXF7
                └──> s_log   (xor/or/and)        ──┘
```

**Five ways in two steps, not four and not one.** Collapsing SLT/SLTU to make it
four, and folding the fast path in to make one five-way, each **lost 9.4 MHz**:
Vivado maps this form as LUT6 + MUXF7, and a MUXF7 is dedicated silicon at
0.067 ns where another routed LUT level is 0.22.

## The LSU serialises lanes

A **staging decision, not a design.** Until the coalescer exists, a per-lane
access walks its active lanes one at a time through the existing single-miss L1.

```
                          lsu_need && !all_lds_q      lane_last, last phase
   [ IDLE ] ──────────────────────────> [ RUN ] ─────────────────────┐
      ^     │                              |  ^                      v
      |     │                              |  | l1_stall: hold       [ DRAIN ]
      |     │                              +--+   ln_q AND ph_q         |
      |     │                                                           v
      |     │  lsu_need && all_lds_q     lds_done                    [ DONE ]
      |     └────────────────────> [ LDS_WAIT ] ───────────────────────> |
      |                                                                  |
      +──────────────────── go (the instruction retires) ────────────────+
```

**The banked path is one branch of the same walk, not a second one.** When every
active lane's address decodes to `R_LDS` — which `all_lds_q` says, from a
registered decision — the whole access hands over to `kht_lds` and comes back in
as many passes as the banks need, instead of walking lanes. Both arrivals meet at
`DRAIN`, so `req_ctr` is `req_q + lds_passes` and the witness stays one number
whichever path served the instruction.

`lsu_done` is what stops the walk restarting. Without it the finished walk drops
`hold`, the same instruction is still in `f2`, `per_lane` is still true, and it
**starts again** — a load that never retires and a PC that never advances.

### Three phases per lane, and what each one is for

```
   phase 0   compute this lane's address and REGISTER it (ea_r, rgn_r, lon_r, wd_r)
   phase 1   issue the request from that register; a miss holds here
   phase 2   TAKE the word, on a cycle with no request in flight
```

The two outer phases were bought for different reasons and neither is optional.

**Phase 0 exists for timing, and it is the assembled PE's binding path that
bought it.** `ea` combinational into `l1_addr` put the 32-bit address adder,
`rv_l1`'s tag compare and `stall` in ONE cycle — and `stall` feeds `hold`, which
gates every register in the core. `rv_l1` already separates `probe_addr` (EX,
starts the tag read) from `addr` (MEM, does the compare); wiring both to the same
wire threw that separation away. So `l1_probe` stays combinational — it only
addresses the tag RAM, so the adder ends at a memory input rather than at a
comparator — and `l1_addr` comes from the register.

`lane_on` is registered in phase 0 with the address for the same reason: `mask`
is a 16:1 mux over the per-wave mask array, so a combinational `lane_on` put that
mux, `l1_req`, `rv_l1`'s `stall` and `hold` in one cone. That was the binding
path at 337.6 MHz, and one register on a one-bit signal was **worth 50 MHz** —
the largest single win of the frequency campaign.

**Phase 2 exists for correctness**, and `rv_l1`'s read interface is why:

```
   1.  l1_rdata(k)  is the data for the access at k-1        (registered read)
   2.  a MISS on a new address zeroes l1_rdata immediately
   3.  a miss completes only while l1_req STANDS             (held handshake)
```

A one-cycle-per-lane walk cannot survive (1) and (2) at once — the cycle in
which lane *n*'s word is readable is the same cycle lane *n+1*'s address goes
out, so any miss there destroys the word before it is captured:

```
  ONE CYCLE PER LANE -- BROKEN
  cycle        k          k+1              k+2
  ln_q         2          3                3
  address      a2         a3  (MISS)       a3
  l1_rdata     d1         0000  <-- d2 destroyed before capture
  capture      d1->buf[1] 0000->buf[2]     ...
                          ^^^^ lane 2 silently reads ZERO
```

The observed signature was exactly that: **only the lanes that immediately
preceded a miss came back zero** — 2, 4 and 7 of eight, which reads like a
random data corruption until the lines are lined up against the misses.

So the word is taken in a phase of its own, on a cycle when no request is in
flight and no new miss can displace it:

```
  A LOAD, THREE PHASES PER LANE
  cycle        k          k+1        k+2        k+3        k+4        k+5
  ph_q         0          1          2          0          1          2
  ln_q         2          2          2          3          3          3
  l1_probe     ea(2)      .          .          ea(3)      .          .
  l1_addr      .          ea_r=a2    .          .          ea_r=a3    .
  l1_req       0          1          0          0          1          0
  l1_rdata     .          .          d2         .          .          d3
  capture                            d2->buf[2]                       d3->buf[3]
```

A **store** takes two phases, not three: it has nothing to collect, so phase 1
retires the lane directly.

Dropping `l1_req` while waiting for a word was tried and is wrong: `rv_l1`
abandons the fill, `l1_stall` goes low because nothing is asking, and the capture
reads zero. The trace said it in one line — `ph 1 ... req 0 stl 0 rdata 00000000`.
Phase 1 is where a miss is held, and the request stands for the whole of it.

The `DRAIN` state still exists so the last lane's word lands before the
writeback reads `ld_buf`:

```
   RUN(ln = LANES-1, last phase) --> DRAIN --> DONE --> the instruction retires
```

### Where a store's data comes from

```
   RV32I  sw  rs2, imm(rs1)       vmem  vsinw2  x7, s1
   ---------------------------    ---------------------------
   base  <- x[rs1] per lane       base  <- s[ss1]   SCALAR
   data  <- x[rs2] per lane       data  <- x[rd]    <-- the rd FIELD
                                  offset<- lane     (lane-linear)

   there is no fourth register field for a vmem store's data, so
   read port 1 serves it:   x_rs1(mem_store ? rd : rs1)
```

The SIMD tier's `vst` does exactly the same thing for exactly the same reason.

### The counters are the coalescer witness

```
   req_ctr    / gather_ctr
   ----------------------
   today:     8 / 1  = LANES        (the serial walk)
   with G5:   1 / 1  = 1            (lanes agree -> one request)
```

Counted **now**, before a coalescer exists. A witness that only appears once the
optimisation lands cannot show the optimisation working. The three addressing
tiers are already distinguished in the encoding, so the coalescer replaces the
walk **without the ISA moving**.

## The cross-lane reductions

Three shapes, and only one of them is deep:

```
   ballot     one OR per lane, then AND with the mask         1 level
   vreadfirst a tree of 32-bit MUXES, left subtree preferred  log2(LANES) muxes
   redux*     a tree of 32-bit ADD / MIN / MAX / AND / OR     log2(LANES) ADDERS
```

**The arithmetic tree was the whole machine's binding path, twice.** Written as
a sequential loop it is `LANES` *chained* 32-bit operations:

```
   AS A LOOP (LANES = 8)          44 logic levels, 9 CARRY8   ->  71.7 MHz
   v0 -> + -> + -> + -> + -> + -> + -> + -> result

   AS A TREE                      21 logic levels, 6 CARRY8   -> 154.1 MHz
   v0 v1  v2 v3  v4 v5  v6 v7
    \ /    \ /    \ /    \ /
     +      +      +      +        level 1
      \    /        \    /
        +              +           level 2
          \          /
              +                    level 3 -> result

   AS A PIPELINED TREE             one adder per cycle
   level 1 -> [reg] -> level 2 -> [reg] -> level 3 -> [reg] -> result
```

A `redux` is therefore held `log2(LANES)` extra cycles on top of the one it
already owes for its operand. That is architecturally free: it is a rare
instruction, it already stalls, and its operands are held stable so the pipeline
simply fills.

**An inactive lane contributes the identity**, which is what makes a tree agree
with a masked sequential reduction:

```
   REDUXADD / REDUXOR -> 0        REDUXMIN -> 0x7FFF_FFFF
   REDUXAND -> 0xFFFF_FFFF        REDUXMAX -> 0x8000_0000
```

Starting min and max at zero clamps every result against zero — invisible
whenever the data straddles it, which is why `reduxmax` over 1..8 passed while
being wrong.

## The banked LDS (G4)

`kht_lds` replaces the flat scratchpad with **LANES banks, word-interleaved**,
and puts the conflict resolver inside the block so the gate is one parameter and
the `LANES × LANES` comparison is measured where it is spent.

```
   addr[LNW-1:0]   is the BANK        consecutive words are different banks
   addr >> LNW     is the ROW within it

   word:   0    1    2    3    4    5    6    7    8    9   10  ...
   bank:   0    1    2    3    4    5    6    7    0    1    2  ...
           |    |    |    |    |    |    |    |
        +--v-+--v-+--v-+--v-+--v-+--v-+--v-+--v-+
        | b0 | b1 | b2 | b3 | b4 | b5 | b6 | b7 |   each an rv_spad,
        +----+----+----+----+----+----+----+----+   WORDS/LANES deep
```

That interleave is why **stride 1 is conflict-free and stride LANES is the worst
case** — the same trade every GPU makes.

### One lane per bank per pass

```
   pass:  todo ──> for each bank, the LOWEST outstanding lane wanting it
                        |
                        v
                  served ──> banks driven ──> words returned ──> todo &= ~served
```

**Forward progress**, which the sequencer rests on: every pass serves the lowest
outstanding lane, because that lane is by construction the lowest lane on its own
bank. So a sequence ends in at most LANES passes and cannot stall. The block
asserts it rather than trusting the argument.

### Measured on hardware by `gpu_lds.s`

```
   lane i -> word i      banks 0..7, all distinct        1 pass
   lane i -> word 7-i    banks 7..0, all distinct        1 pass   <- crossbar
   lane i -> word 8i     every lane on bank 0            8 passes
```

The reversed case is the one that proves the **return crossbar**: bank 7's word
has to reach lane 0, rather than lane 0 always taking bank 0.

```
   TR ... LDS ph 0 we 0 todo 11111111 served 11111111 | rd0 00000064 rd7 0000006b
                                      ^^^^^^^^^^^^^^^ eight lanes, ONE pass
```

### Two cycles a pass, and `done` is a cycle after that

The banks register their address, so a pass is drive-then-take. And the last
pass's words land at the edge that *ends* phase 1 — so `done` is raised the cycle
after "no lanes left", or the caller reads `lrdata` one pass stale:

```
   ph      0    1    0    1    0    1
   todo    A    A    B    B    C    C    0
   lrdata            a         b         c
   fin                                   ___/‾‾‾\___   <- done
```

## Regions

```
   ea[31] = 1  ──────────────────────────────> R_DRAM   via rv_l1
   ea[31] = 0
        ea[30:28] = 1 ────────────────────────> R_SPAD
                    2 ────────────────────────> R_CTL
                    4 ────────────────────────> R_LDS
                    else ─────────────────────> R_BAD ──> FAULT, no request
```

`R_BAD` suppressing `l1_req` is what made Trap 4 so quiet: the walk computed a
garbage `ea`, landed outside every region, issued nothing, and the shader still
reported the right halt word and the right cause.

## The kick must not overtake the data it is the doorbell for

Boot is not a mechanism here. A shader image arrives as a `CU_DATA` burst into
the instruction window and its constants as another into the scratchpad, then the
standard kick — the same write path every unit has, which is why there is no
loader to go wrong. But the two arrive on **different queues**:

```
   CU_INST ──> the instruction FIFO ──> the kick FSM
   CU_DATA ──> the receive queue    ──> the window writer   (granule walk, 8 words)

   the framework preserves order ON THE WIRE and not ACROSS the two queues
```

So the kick can reach the head of its FIFO while the last granule of the shader
is still being walked into the window. `K_IDLE` therefore waits on `rx_quiet` —
no flit pending, no granule walk in progress, the burst FSM idle — before it
accepts the kick. That cannot deadlock, because `rx_quiet` is cleared by this
unit's **own** progress rather than by another flit arriving.

**The kick's `op` field is the wave count**, clamped to what the build carries.
`op = 1` is exactly the single-wave case it always was, so this generalises the
field rather than redefining it and a launch needs no new protocol word.

And the completion is stricter than "halted":

```
   K_RUN --> K_DONE  when  core_halted && pipe_empty && req_idle && wr_out == 0
```

The completion is the host's only sequencing point, so it has to mean every write
this shader issued has been **acknowledged**, not merely sent. `pipe_empty` counts
`f1` too: with two fetches in flight, a completion built while `f1` still carries
one reports a unit that has not finished executing.

## A halt flushes before it completes

The unit's completion is the host's **only** sequencing point, and a write-back
L1 holds a shader's stores in dirty lines that nobody else will ever push out.

```
                ecall / ebreak / fault
                        |
                        v
   [ H_RUN ] ──> [ H_FLUSH ] ──> [ H_RISE ] ──> [ H_FALL ] ──> [ H_DONE ]
    live<-0       l1_flush        wait for       wait for      halted <- 1
                  ONE-CYCLE       busy to        busy to
                  PULSE           RISE           FALL

   rv_l1 acts on `flush` in L_IDLE only and ASSERTS if it is raised later,
   so the request must be a pulse -- and busy does not rise in the pulse
   cycle, which is why the wait is split in two.
```

Without the flush: the shader retires, the host reads DRAM, and finds it
unchanged. The flush is not finished until every writeback has been
**acknowledged**, which is what makes the completion mean the data is *in
memory* rather than merely issued.

`go` is gated on `hst == H_RUN`, because the instruction already in the window
behind an `ecall` would otherwise retire too and overwrite the cause and the
halt word with its own.

### The halt word

```
   ecall has NO source operand.  a0 is a value the program LEFT BEHIND.

   writeback ──> vt_wr_rd == x10 ?  ──> vt_wr_mask[0] ?  ──> a0_q
                     (a0)                 (lane 0 active)

   reading it from the ecall's own rs1  --> reports x0
   latching it at the halt              --> captures a0 BEFORE it exists,
                                            because the instruction before an
                                            ecall writes a0 one cycle AFTER
                                            it retires
```

Lane 0 only, and only when lane 0 was active, because that is exactly what the
golden model records.

## Traps, collected

Every row is a failure that happened, not one that was anticipated. They are
listed because each looks like something else while you are in it.

| Symptom | Cause |
|---|---|
| every PC executes twice | the window was driven from the architectural PC, which lags the fetch |
| one instruction silently never happens | `imem_addr` ran on while `f1` was held, so the resumed `f2 <= f1` no longer named the word in flight |
| wedges on the first back-to-back dependency | `kht_unit` was fed the **combined** hold, so its own stall was an input to itself |
| a store lands at a nonsense address, `rgn` is BAD | the walk started in the same cycle the instruction retired, and decoded the next one |
| deadlock on the first store that uses the previous result | `lsu_need` was in `base_hold`, freezing the MEM register so the hazard never cleared |
| a load never retires, the PC never advances | no `lsu_done`: the finished walk dropped `hold` and immediately restarted |
| every lane takes the else-branch; "all-zero active mask" is reported | `split` read its predicate from the **registered** vector port in EX, so it got the previous instruction's operand |
| halt word is X, or is the wrong instruction's | `a0` read from the `ecall`'s `rs1`, or latched before the writeback committed |
| shader passes every check, DRAM is unchanged | no flush: the stores were sitting in dirty L1 lines |
| only the lanes that preceded a cache miss read zero | a one-cycle walk captured lane *n*'s word on the cycle lane *n+1*'s miss zeroed `l1_rdata` |
| a load returns zero and the fill never completes | `l1_req` was dropped while waiting for the word; `rv_l1` completes a miss only while the request stands |
| `s2v` returns `x[rs1] + x[rs2]` | it set a write enable with no scalar path into the writeback mux |
| a vmem load computes the right address, fetches the right word, and writes nothing | `is_vmem` was missing from `vt_wen` while `x_ld_sel` already selected the gathered data |
| the banked LDS resolves the wrong lanes on its first cycle | it reads `vt_rd2` for **every** lane combinationally in EX, so it needs the same warm-up `split` does |
| every wave computes the same address and they collide | the *model's* `rdctl 5` read the flat control table instead of the wave id — invisible with one wave, fatal with two |
| one wave's `ecall` ends the whole dispatch | `ecall` must retire **one wave**; only a fault kills the unit |
| the unit closes at 324 MHz and the core containing it at 72 | the cross-lane reduction was a **serial chain** of LANES 32-bit adds, and it lives in `kht_core` where the unit-only ladder never looked |
| a hierarchical reference simulates and will not synthesise | `u_vt.v1_rd` — read ports must be **ports** (`rd1_o`/`rd2_o`) |
| a Vivado run reports clean and every LUT figure is unconstrained | the OOC `create_clock` was guarded by a `get_ports` test that evaluates before the design exists. The XDC is now unconditional and the script **errors** if `get_clocks` is empty |
| three dependent `vfma` launch in consecutive cycles with stale addends | `fpend` blocks **fetch**, but the front end is three deep — the float must also redirect its own wave to `pc+4` to kill the two already in flight |
| `mul x10, x6, x8` jumps forty bytes and skips nine instructions | `is_imul` was added to `br_take` and not to `redir_pc`, so a multiply took `f2_pc + imm_i` — and an R-type's imm field is `funct7\|rs2` |
| a wave comes back one instruction late for every cycle it waited on a float | the per-wave PC increment was gated on the fetch, not on `rdy[cur]`. Invisible before G9: the only unready wave used to be a **dead** one |
| the machine wedges with sixteen waves runnable, on a `vfmul` | a held instruction is `go` on every cycle of the hold, so the float re-launched the lane array every cycle and `f_soon` never cleared. `x_defer` must carry every core-level hold that is not `base_hold` |
| a `join` underflows and faults in perfectly balanced code | the stack committed on `go` rather than `go_c`, so a join sitting under another wave's `f_soon` popped once per cycle |
| a `vfma` reads its addend from before the instruction that set it | `vd` is a **source** for `vfma` and was not compared as one — seen as `c = 0x0400` where the shader had just built `0x4000` |
| the halt word is whatever `a0` held before the multiply that computed it | the a0 snoop watched the MEM stage's write intent; a float or multiply retires through its own slot, so the probe must be the register file's **write port** |
| the shader runs a half-written image and faults, or runs the previous one | `noc_cu_base` sorts CU_INST and CU_DATA into different queues, so a kick can reach the head while the last granule is still being walked in. The kick waits on `rx_quiet` |

## The subgroup butterfly (G8)

```
   shflxor  vd, vs1, ss2      vd[i] = vs1[i ^ m]      m uniform, from a scalar
   bcast    vd, vs1, lane     vd[i] = vs1[L]          L uniform, an immediate
```

**One network serves both.** Lane *i* must end up holding `vs1[src]`, so the
per-lane control is `src ^ i` either way:

```
   shflxor:  src = i ^ m   ->  ctl = m         (the same in every lane)
   bcast:    src = L       ->  ctl = i ^ L     (differs per lane -- fine)
```

**log2(LANES) stages, not a crossbar.** Stage *k* conditionally swaps across
distance 2^k, under bit *k* of that lane's control:

```
   LANES = 8, ctl = 5 (binary 101)

   stage 0 (dist 1, ctl[0]=1)   0<->1  2<->3  4<->5  6<->7
   stage 1 (dist 2, ctl[1]=0)   ....... no swap .......
   stage 2 (dist 4, ctl[2]=1)   0<->4  1<->5  2<->6  3<->7

   lane 0 ends holding vs1[5]  =  0 ^ 5      correct
```

That is **O(LANES · log LANES)** where an all-to-all select is O(LANES²) — the
shape [`kht_lds`'s resolver](#the-banked-lds-g4) pays for, and the reason it is
worth naming the difference.

### The masked case is resolved before the network

The ISA fixes that a lane whose source is inactive **reads its own value**. That
cannot be decided inside the butterfly — once data has moved one stage, "was my
source active" is no longer a question the intermediate lanes can answer. So the
control is zeroed up front:

```
   ctl[i] = mask[src[i]] ? (i ^ src[i]) : 0
                           ^^^^^^^^^^^^   ^^^ read your own value
```

`gpu_shfl.s` exercises this with an xor of 4 and lanes 0–1 masked off: lane 4's
source is lane 0, which is inactive, so lane 4 must keep its own value.

### Control is WB-stage

The network consumes `w1_q`, the **writeback** stage's copy of the operand — so
`w_sh_idx`, `w_sh_bcast` and `w_mask` drive it, not their EX-stage originals. The
registered read port made EX-stage control one cycle early, and the writeback
stage that put the lane ALU behind a flop makes it two. This is the same trap
[Trap 6](#trap-6--the-vector-read-is-registered-and-ex-consumers-forget-it)
describes, avoided rather than repeated.

## The float tier and the multiplier: one shadow pipe, two producers

Both are 15 cycles, II = 1, and that is not a coincidence — giving the
multiplier the float tier's exact latency lets it ride machinery that already
existed instead of needing a second mechanism.

```
   EX                                        +15                    WB
   ---------------------------------------------------------------------
   w1_q (vs1) --+
   w2_q (vs2) --+--> kht_fpu   8 x khs_float_lane --> fpu_y --+
   w3_q (vd)  --+       ^                                     |
                        |                              fsh_mul[FLAT]
   w1_q,w2_q ------> kht_imul  8 x 33x33 signed  --> imul_y --+--> fwb_data
                        (3 real stages + a 12-stage FLOP pad) |
                                                              v
                                      fwb_v / fwb_wa / fwb_mask --> VRF port
```

`fsh_*` is a shadow shift register exactly `FLAT` deep, carrying the valid bit,
the destination address, the write mask and the wave. **It must match `vec_alu`'s
own depth**, because it carries what the lane array does not: if the two disagree
a result lands on the wrong register with no witness. It is free-running, like
the lane array it shadows — `vec_alu` has no clock enable, so gating the shadow
would desynchronise the two.

### A float redirects its own wave, and that is the whole of how fpend works

`fpend` blocks **fetch**, but the front end is three deep, so by the time a float
issues, two more instructions of that wave are already in flight. That was
measured as three dependent `vfma` launching in consecutive cycles with stale
addends.

The fix reuses the branch machinery unchanged: `is_flt` and `is_imul` join
`br_take`, so the instruction is treated as a **redirect to its own `pc+4`**.
That kills exactly the two wrong-path fetches and rewinds `nxt` to the
instruction after the float, so the wave resumes there once its result has
landed.

> **A multi-cycle unit redirects to the NEXT instruction, not to a branch
> target.** Adding `is_imul` to `br_take` without also adding it to `redir_pc`
> sent every multiply to `f2_pc + imm_i` — and an R-type's immediate field *is*
> `funct7|rs2`, so `mul x10, x6, x8` jumped forty bytes and skipped nine
> instructions.

> **`rdy[cur]` gates the PC increment, not just the fetch.** Advancing a wave's
> pointer on a cycle that issued no fetch skips an instruction — and it was
> invisible until G9, because before `fpend` the only unready wave was a *dead*
> one, whose pointer nobody reads again. A wave blocked on a float comes back,
> and came back one instruction late for every cycle it waited.

**The pad is flip-flops and the RTL says so explicitly** —
`(* srl_style = "register" *)` refuses the SRL16E the shape would otherwise map
to. An SRL16E is **one LUT per bit at any depth**, and this PE is LUT-bound while
the flop half of the CLB is idle: −256 LUT for +3,329 FF at an identical
365.6 MHz. Measured at the 2.857 ns ask; at 2.500 the same change read as
−15.6 MHz, which was an artifact of asking for timing the design was not going to
meet.

`fsh_v` is a shadow shift register carrying valid, destination and mask; the
result that emerges at `FLAT` is selected by `fsh_mul[FLAT]`. **There is no
per-register scoreboard**, and that is the point: with `WAVES >= depth` no two
in-flight instructions share a wave, so a per-wave pending bit (`fpend`)
restores the barrel-scheduling invariant that a multi-cycle unit breaks.

### The width bit is delayed to the result, not read at the operands

```
   launch    op[2] --> hpipe[0] --> ... --> hpipe[ALAT-1] = y_half
                                                              |
   y_e8 --+--> vec_cvt_e8_to_f16 --> {16'd0, y16} --+          |
          |                                         +--> vy <--+
          +--> vec_cvt_e8_to_f32 --> y32 -----------+
```

**`y_e8` emerges 15 cycles after launch, by which time `op` belongs to whatever
the scheduler picked next.** Selecting the output conversion from the live `op`
writes a narrow result into a wide destination for any wave that is not running
alone — so it passes at one wave and fails at sixteen. One bit deep enough to
reach the result; Vivado maps it to an SRL.

### Operand width is per instruction, and it is not a build option

```
   vfma  vfmul  vfadd  vfsub        funct7 0-3    wide operands
   vfma_h vfmul_h vfadd_h vfsub_h   funct7 4-7    narrow operands
                                           ^
                    f7[2] -> kht_fpu.half -> khs_float_lane.wide(!half)
```

`wide` is a **port** on `khs_float_lane`, not a parameter, and that module's
header states the contract: both input formats and the one compute format *are*
the contract, not options — there is no parameter that removes either edge. A
build either has the float tier or faults on all eight encodings.

The three conversions are not symmetric. `FP32 → E8M15` copies the exponent
verbatim and rounds off only mantissa below bit 8; `FP16 → E8M15` is exact; and
`E8M15 → FP16` is the one direction that is both lossy **and** range-limited,
saturating silently on a finite overflow. That is why the wide form is the
*default* encoding and the narrow one carries the suffix.

> **The lanes above `FLANES` return ZERO, and zero is a plausible float answer.**
> `kht_fpu`'s `g_nolane` assigns `32'd0` there because `FLANES < LANES` has no
> walk sequencer to feed them, so a shader run on a reduced build gets silently
> wrong upper lanes and no fault — the one place this PE breaks its own rule that
> a build which cannot do something faults instead of answering plausibly. It is
> guarded by convention only: `FLANES` must equal `LANES` in any build that runs
> a shader. Reduced builds are for area measurement.

## What is encoded but has no datapath

```
   HAS_SHFL = 0:   shflxor, bcast  --> illegal --> FAULT (cause 3)
   HAS_SHFL = 1:   both built on the butterfly above
   HAS_FLT  = 0:   all eight float ops AND every RV32M op --> FAULT
   HAS_FLT  = 1:   the tier is built, and it takes BOTH operand widths

   bar          :  decoded, read by NOTHING --> retires as a NO-OP
```

**RV32M is gated on `HAS_FLT`, not on a gate of its own**, because the multiplier
shares the float tier's retire slot and its per-wave pending bit —
`is_imul = ictl[C_IMUL] && (HAS_FLT != 0)`. There is no float-free build with a
multiplier, and asking for one gets a fault rather than the ordinary ALU's opinion
of a `funct7` it does not know.

**`bar` is the one exception to the fault rule, and it should not be.**
`kht_predec` sets `C_BAR`; nothing in `kht_core` reads it, and the golden model
has no barrier either — so a workgroup barrier retires silently. With one wave
per workgroup a no-op is correct; with more than one it is a race with no
witness. Every other unbuilt thing here faults.

With the gate off these two decode and would otherwise set a write enable with
no shuffle datapath behind it, writing the ALU result. **A build that cannot do
something faults rather than returning a plausible wrong answer** — running
`gpu_shfl.s` against a `HAS_SHFL = 0` build halts with cause 3, which is the
fault working, not a regression.

`s2v` was in the same state and was **built** instead, because a scalar-to-vector
broadcast is fundamental and costs almost nothing — the value is uniform by
construction, so it rides in on the immediate port and the broadcast is a mux
input rather than a network:

```
   x_imm = is_s2v ? sv1 : vt_imm        (captured in EX; the scalar file
        |                                reads combinationally, so no warm-up)
        v
   vwdata[lane] = w_ld_sel ? load
                : w_laneid ? lane
                : w_smov   ? w_imm      <-- the same word in every lane
                : w_shfl   ? shfl_y[lane]
                           : alu_y[lane]
```

The float and multiply results do **not** appear in that mux. They arrive fifteen
cycles later through their own retire slot and take the register file's write port
by a mux at the port itself (`fwb_v ? ... : ...`), which is an arbitration-free
choice because the core dropped `go` two cycles earlier to empty writeback for
exactly this.

## Debugging rule this bring-up earned

Trap 4 cost two rounds of arithmetic guessing about which operand was wrong.
What actually solved it was adding five fields to one trace line:

```
  TR <t> LSU ln 0 ea 80000000 rgn 5 vmem 1 lin 1 sc 2 sv1 80000000 off 0 wd 1
                                         ^^^^^^
                              `vmem 0` on a vsinw2 was the entire answer
```

**Probe the state; do not re-read the source.** The bench's `KHT_TRACE` block is
bounded so a wedged run cannot fill the log.
