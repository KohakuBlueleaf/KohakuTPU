# Internal architecture, drawn

Reference sketches for the four compute blocks, at the level of detail a
schematic wants. Every number is from the RTL, not from a spec that drifted.

Sources: `mx_mac.v`, `mx_tcu.v`, `mx_acu_fp.v`, `mx_cluster_node.v`,
`mx_cluster_mgr.v`, `vec_core.v`, `vec_lanes.v`.

---

## 1. mx_mac -- one DSP48E2, two int7 MACs

The cell everything else is built from. Two weights share one activation, so a
DSP does two MACs. The packed operand is formed in the **pre-adder**, which
costs no fabric -- it is pure wiring.

```
                     w_hi (7b)          w_lo (7b)        a / activation (7b)
                        |                  |                     |
                 << S   |                  |                     |
                 (S=19, |                  |                     |
                  wiring)                  |                     |
                        v                  v                     v
                   +----------------------------+          +-----------+
                   |  A port         D port     |          |  B port   |
                   |  w_hi<<19       w_lo       |          |  a        |
                   +--------------+-------------+          +-----+-----+
                                  |                              |
                                  v                              |
                          +---------------+                      |
                          |   PRE-ADDER   |  A + D               |
                          +-------+-------+                      |
                                  |                              |
                                  +--------------+---------------+
                                                 v
                                         +---------------+
                                         |   MULTIPLIER  |   M = (A+D) * B
                                         +-------+-------+     = w_hi*a*2^19
                                                 |             + w_lo*a
                                                 v
   PCIN  ------------------------------> +---------------+
   (cascade from DSP above, or 0)        |   DSP ALU     |   Z + W + X + Y
                                         |   X,Y = M     |
   C  ---------------------------------> |   Z   = PCIN  |
   (cross-TCU partial, LAST stage only)  |   W   = C     |
                                         +-------+-------+
                                                 |
                                                 v
                                          PCOUT (to DSP below)
```

**Why S = 19 and not 20.** The packed operand must fit 27 bits signed. Worst
case `w_hi = w_lo = -64` gives `-64*2^20 - 64 = -67,108,928` against a limit of
`-67,108,864` -- over by exactly 64. At S = 19 the guard is 5 bits, which is
cascade depth 32, exactly one K=32 block.

**Why the cross-TCU partial is on W, not Z.** On Z it would enter at stage 0, so
the upstream TCU's result would have to be ready before this TCU starts -- 8
cycles of operand skew per TCU. On W at the last stage the two results are
contemporary and need two cycles of alignment.

---

## 2. mx_tcu -- the tensor core, 4 x 8 x 4

128 MACs per cycle, one 4x8x4 tile per cycle, 64 DSPs. The tile decomposes into
**8 chains of 8 stages**: `chain(p,j)` where `p` = row pair 0..1 and `j` =
column 0..3, stage `k` = 0..7.

```
   a_in[223:0]                                     b_in[223:0]
   A[i][k], i=0..3 rows, k=0..7                    B[k][j], k=0..7, j=0..3
   (4 x 8 x int7)                                  (8 x 4 x int7)
        |                                                |
        +------------------------+-----------------------+
                                 v
              +--------------------------------------+
              |  OPERAND SKEW -- one SRL32 per stage |
              |  stage k delayed by k cycles         |
              |  bundle = 4 rows A + 4 cols B = 56b  |
              |  (a cascade adds one pipeline stage  |
              |   per DSP, so stage k's operands     |
              |   must arrive k cycles after stage 0)|
              +--------------------------------------+
                 |        |        |             |
                 v        v        v             v
        chain 0        chain 1   chain 2  ...  chain 7      8 chains
      (p=0,j=0)      (p=0,j=1)                (p=1,j=3)     = 2 pairs x 4 cols
     +---------+    +---------+    +----+     +---------+
 k=0 | mx_mac  |    | mx_mac  |    |    |     | mx_mac  |   Z = 0
     |  Z=0    |    |  Z=0    |    |    |     |  Z=0    |
     +----||---+    +----||---+    +----+     +----||---+
          || PCOUT->PCIN, dedicated cascade, no fabric
     +----vv---+    +----vv---+    +----+     +----vv---+
 k=1 | mx_mac  |    | mx_mac  |    |    |     | mx_mac  |
     +----||---+    +----||---+    +----+     +----||---+
          ||             ||                        ||
         ...            ...                       ...       stages k=2..6
          ||             ||                        ||
     +----vv---+    +----vv---+    +----+     +----vv---+
 k=7 | mx_mac  |    | mx_mac  |    |    |     | mx_mac  |   W = C
     |  W=C  <----------------------------------------------- part_in[383:0]
     +----|----+    +----|----+    +----+     +----|----+    (from prev TCU)
          |              |                         |
          +--------------+------+------------------+
                                v
                        part_out[383:0]      8 chains x 48b
                        (to next TCU, or to the accumulator)
```

**Latency:** operands at `t` -> `part_out` at `t + 11`. Stage `k`'s operands
arrive at `t+k`, its P at `t+k+4`, so stage 7 completes at `t+11`.

**FIRST = 1** removes the upstream partial for the head of a cluster.

---

## 3. mx_acu_fp -- the accumulator

Floating-point accumulation into a **resident output tile**, with peer transfer
so one matmul can span clusters. This block sets the cluster's critical path.

```
   part_in[383:0]    sa[31:0]   sb[31:0]    anchor[7:0]   op[2:0]  tile_addr
   (2 chains)        A scales   B scales    2 x SBIAS               cmd_valid
        |               |          |             |            |        |
        v               v          v             v            |        |
   +------------------------------------------------+         |        |
   | STAGE 1   unpack both chains, take |value| AND |         |        |
   |           apply the block scale -- ONE DSP     |         |        |
   |                                                |         |        |
   |   magnitude BEFORE the multiply: taking it     |         |        |
   |   after puts a 30-bit two's-complement carry   |         |        |
   |   chain between the DSP output register and    |         |        |
   |   the leading-one search                       |         |        |
   +------------------------+-----------------------+         |        |
                            | val_r                           |        |
                            v                                 |        |
   +------------------------------------------------+         |        |
   | STAGE 2a  leading-one search (mx_lead1)        |         |        |
   |           -> one-hot the shift multiplies by   |         |        |
   |           NOTHING else between val_r and the   |         |        |
   |           shift DSPs' input registers          |         |        |
   +------------------------+-----------------------+         |        |
                            v                                 |        |
   +------------------------------------------------+         |        |
   | STAGE 2a2 normalising shift, 2 DSP mults/lane  |         |        |
   +------------------------+-----------------------+         |        |
                            v                                 |        |
   +------------------------------------------------+         |        |
   | STAGE 2b  round and assemble -> accumulator fp |         |        |
   +------------------------+-----------------------+         |        |
                            v                                 v        v
   +--------------------------------------------------------------------+
   | STAGE 3   READ the tile, compare exponents, align                  |
   |                                                                    |
   |   Nothing combinational in front of the tile address. Every select  |
   |   is registered a stage early and registered ALREADY DECODED       |
   |   (a_zero_r, b_peer_r, b_zero_r, addr_r2) -- an encoded id would    |
   |   put its equality test back in front of the mux this empties.     |
   +------------------------+-------------------------------------------+
                            |                            ^
                            v                            | read
   +------------------------------------------+     +----+-----------------+
   | STAGE 4  add, leading-one search, shift  |     |  RESIDENT TILE       |
   +------------------------+-----------------+     |  DEPTH sub-tiles     |
                            |                       |  4x4 each            |
                            v                       |  TILE_PRIM:          |
   +------------------------------------------+     |   "block" = 5 BRAM36 |
   | STAGE 5  round, assemble, WRITE BACK ---------->|   any depth <= 512   |
   +------------------------+-----------------+     |   "ultra" = URAM     |
                            |                       |  ONE BANK            |
                            v                       +----------------------+
   +------------------------------------------+
   | STAGE 6  (EMIT only) convert to FP16     |
   +------------------------+-----------------+
                            v
                        emit_valid / emit_data
```

**One bank, not two.** Depth is not the constraint -- throughput is. Only the
accumulate loop (stage 3 read -> stage 5 write) costs anything to lengthen, and
the price is banks. It runs on ONE bank because the ISA sweeps **K outermost**,
which replaces the structure with a contract: consecutive commands to the same
tile address must be at least `REUSE_MIN` cycles apart.

**ACC_MW** sets the mantissa -- 16 = FP24, 14 = FP22, 12 = FP20. E7 is fixed;
range is not the tunable. MW=14 measures identically to MW=16 and costs less.

---

## 4. mx_cluster_node -- the matmul cluster

Manager + 4-TCU cascade + accumulator. Two NoC ports: the manager takes one for
operands and its own memory requests, the accumulator takes one for results and
peer transfer.

```
        NoC port A                                        NoC port B
   (operands, mem requests)                          (results, peer transfer)
            |                                                    ^
            v                                                    |
   +---------------------------------------+                     |
   |  mx_cluster_mgr                       |                     |
   |                                       |                     |
   |  +---------------+ +---------------+  |                     |
   |  |   L1 A        | |   L1 B        |  |                     |
   |  | GA entries    | | GB entries    |  |                     |
   |  | A rows 4g..   | | B cols 4h..   |  |                     |
   |  |  4g+3, all    | |  4h+3, all    |  |                     |
   |  |  32 K         | |  32 K         |  |                     |
   |  | 896b + 4x     | | 896b + 4x     |  |                     |
   |  |  E5M3 scale   | |  E5M3 scale   |  |                     |
   |  | = 928b/entry  | | = 928b/entry  |  |                     |
   |  |               | |               |  |                     |
   |  | 2 BANKS of    | | 2 BANKS of    |  |                     |
   |  | 256; aoff/    | | boff are 8b   |  |                     |
   |  | boff index    | | WITHIN a bank |  |                     |
   |  | abank/bbank   | | pick the half |  |                     |
   |  +-------+-------+ +-------+-------+  |                     |
   |          |                 |          |                     |
   |  +-------v-----------------v-------+  |                     |
   |  |  GEMM SWEEP                     |  |                     |
   |  |  K IS THE OUTER LOOP:           |  |                     |
   |  |   for kb in 0..NK-1:            |  |                     |
   |  |    for g in 0..Gm-1:            |  |                     |
   |  |     for h in 0..Gn-1            |  |                     |
   |  |  so an accumulator address      |  |                     |
   |  |  recurs every Gm*Gn cycles,     |  |                     |
   |  |  not every cycle                |  |                     |
   |  +-------+-------------------+-----+  |                     |
   +----------|-------------------|--------+                     |
              | A[4][32] B[32][4] | {op, addr, scales}           |
              | 8 x 256b/cycle    v                              |
              |            +--------------+                      |
              |            | ACU CMD FIFO |  not a matched       |
              |            |              |  delay: the chain's  |
              |            |              |  ~19 cycles depend   |
              |            |              |  on NTCU and the     |
              |            |              |  skew SRLs, so each  |
              |            |              |  issue pushes and    |
              |            |              |  every part_valid    |
              |            |              |  pops one            |
              |            +------+-------+                      |
              v                   |                              |
   +---------------------+        |                              |
   | mx_tcu #0 FIRST=1   |        |                              |
   +----------+----------+        |                              |
              | part 384b         |                              |
   +----------v----------+        |                              |
   | mx_tcu #1           |        |     DSP cascade:             |
   +----------+----------+        |     PCOUT/PCIN + W,          |
              |                   |     no fabric between        |
   +----------v----------+        |                              |
   | mx_tcu #2           |        |                              |
   +----------+----------+        |                              |
              |                   |                              |
   +----------v----------+        |                              |
   | mx_tcu #3           |        |                              |
   +----------+----------+        |                              |
              | part_out 384b     |                              |
              v                   v                              |
   +------------------------------------------+                  |
   |  mx_acu_fp   (diagram 3)                 |                  |
   |  resident output tile, TILES sub-tiles   |------------------+
   +------------------------------------------+   emit
              ^
              |  control MUXED between the manager (during a sweep)
              |  and the DRAIN SEQUENCER (during an explicit drain).
              |  The sequencer WAITS on emit_valid rather than counting
              |  cycles -- the ACU's read-to-emit depth is a design
              |  variable, and a blind drain returns the wrong sub-tile.
   +----------+----------+
   |  DRAIN SEQUENCER    |  drain_start, drain_n
   +---------------------+
```

**Why L1 exists.** The chain eats `A[4][32] + B[32][4]` every cycle -- eight
256-bit operand words -- and a NoC port delivers one. The gap is closed by
**reuse**, not bandwidth.

**`sweep_busy` vs `gemm_busy`.** `sweep_busy` is the sweep alone, without the
~19-cycle cascade and the accumulator's settling tail. The next sweep only needs
that one; a DRAIN needs `gemm_busy`, because it takes the accumulator's control
mux and would cut the cascade off.

---

## 5. vec_core -- the vector core

Sequencer, L1 scratchpad, address generator and 16 lanes. The memory port is
**abstract** -- one 256-bit word per request, tagged -- so the core carries no
NoC knowledge; `vec_cu.v` does the framing.

```
   ld_en/ld_kind/ld_addr/ld_data          start / start_pc
   (imem or descriptor)                          |
            |                                    v
            v                        +------------------------+
   +------------------+              |  SEQUENCER             |
   |  IMEM            |------------->|  decode, hazard, fault |
   |  IMEM_DEPTH=512  |              |                        |
   |  32b instruction |              |  ALU ops issue BACK TO |
   +------------------+              |  BACK, gated by a per- |
                                     |  register pending-write|
   +------------------+              |  count: the lane is 14 |
   |  DESCRIPTORS     |------------->|  deep with NO BYPASS   |
   |  8 x {base,      |              +----+-------------+-----+
   |   stride, len..} |                   |             |
   +------------------+                   |             | busy/halted
            |                             |             | fault/fault_code
            v                             |             | cycles
   +------------------+                   |             v
   |  vec_agu         |<------------------+
   |  address gen     |
   +--------+---------+
            |
            v
   +-------------------------------------------------------+
   |  MEMORY PORT (abstract, 256b words, tagged)           |
   |    rd_req_valid/addr/tag  ->   rr_valid/rr_tag/rr_data|
   |    wr_req_valid/addr/data                             |
   |                                                       |
   |  nd_* : a VDRAIN whose sink is a PEER, not memory.    |
   |  Stable from S_MEM0 until the next VFILL/VDRAIN, so   |
   |  vec_cu frames the whole burst and this module still  |
   |  carries no NoC knowledge.                            |
   +----------------------+--------------------------------+
                          |
   cd_valid/cd_addr/      |            SEPARATE from the fill port: every
   cd_data/cd_fault ------+            rr_valid decrements fill_out and a
   (peer CU_DATA landing) |            peer's write has no matching
                          |            fill_issue -- reusing it underflows
                          v            the count and hangs the NEXT barrier
   +-------------------------------------------+
   |  L1 SCRATCHPAD                            |
   |  L1_DEPTH = 512 words x 256b              |
   |  L1_PRIM = "block"                        |
   |                                           |
   |  VFILL is NON-BLOCKING (writes L1 only,   |
   |  overlaps compute); VDRAIN BLOCKS (reads  |
   |  L1 through the port VLD uses)            |
   +---------------------+---------------------+
                         |
                         v
   +-------------------------------------------------------------+
   |  vec_lanes                                                  |
   |                                                             |
   |  +-------------------------------------------------------+  |
   |  |  REGISTER FILE   RF_PRIM = "block"                    |  |
   |  |  whole-chunk port shared with ALU write-back, which   |  |
   |  |  is why VLD/VST/VSHUF/VBCAST/VCVT drain the lanes     |  |
   |  +----+--------------------------------------------+----+  |
   |       | ra / rb / rc                          wa   ^       |
   |       v                                            |       |
   |  +---------------------------------------------------+     |
   |  |  16 x vec_alu   (E8M15 internal)                  |     |
   |  |                                                   |     |
   |  |  ALU  ALU  ALU  ALU  ALU  ALU  ALU  ALU           |     |
   |  |  ALU  ALU  ALU  ALU  ALU  ALU  ALU  ALU           |     |
   |  |                                                   |     |
   |  |  14 stages deep, NO BYPASS                        |     |
   |  +---------------------------------------------------+     |
   |                                                             |
   |  VMODE -- a mode is a factorisation W x D = 16:             |
   |    FLAT  16 x 1     16 results/cycle                        |
   |    D2     8 x 2      8 results/cycle                        |
   |    D4     4 x 4      4 results/cycle                        |
   |    TREE   8+4+2+1 + accumulator                             |
   |                                                             |
   |  GROUP g STAGE t IS ALU g*D + t. Only stage 0 may read a    |
   |  vector register: a later stage runs 14*t cycles behind, so |
   |  a V operand there would need a 24-bit delay line per       |
   |  stage. An ALU that is never a chain head in a mode has NO  |
   |  register-file path in that mode.                           |
   |                                                             |
   |  STRIPING IS BY LANE -- slice s holds elements s, s+16, ... |
   |  A D-deep mode retires W per cycle, so group g at phase p   |
   |  drives slot p*W + g. Over D phases the 16 slices of a      |
   |  chunk are covered exactly once.                            |
   +-------------------------------------------------------------+
```

---

## Numbers worth putting on the drawing

| block | figure |
|---|---|
| `mx_mac` | 1 DSP48E2 = 2 int7 MACs, S = 19 |
| `mx_tcu` | 4x8x4, 64 DSP, 128 MAC/cycle, latency t+11 |
| cluster | 4 TCU cascade + 1 ACU, ~19 cycle chain |
| L1 A/B | 928 b/entry = 896 b elements + 32 b scale |
| L1 banking | 2 banks of 256; offsets are 8 b WITHIN a bank |
| accumulator | 6 stages, 1 bank, 4x4 sub-tiles, FP22 default |
| vector lanes | 16 ALU, 14 deep, no bypass, E8M15 |
| vector L1 | 512 x 256 b |
