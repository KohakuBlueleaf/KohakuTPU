# Vector core V2

`src/kohakutpu/vector2/`, top `v2_core`. It has the same ports as v1's `vec_core`, so
`vec_cu` frames either one (`-d VEC_CORE_V2` picks V2). The lanes are v1's: 16 E8M15 ALUs
with one SFU each. The format, the FMA and the transcendentals are in
[vector-core.md](vector-core.md) §1-4. This page covers what V2 changes: the control
machine, the register file, the operand network, and the ISA.

Machine code: `compiler/kohakutpu/hw/vector2.py` (encoders, `Kernel2`).

## 1. Structure

One instruction stream feeds five engines. Each engine owns its ports, so no engine
waits on another for a port.

| engine | queue | does | ports |
|---|---|---|---|
| front end | — | fetch, decode, scalar state, loops; dispatches one instruction a cycle | IMEM read |
| FILL | FQ (4) | `VFILL` walks on AGU walker 0 | NoC read, L1 write |
| DRAIN | DQ (4) | `VDRAIN` walks on AGU walker 1 | L1 read, NoC write |
| UNPACK | UQ (8), UQ1 (8, `DUALQ`) | L1 word → GT4 → fp16/fp32 → E8 → register chunk, one word a cycle | L1 read, RF write |
| MATH | MQ (8) | one beat a cycle into the lanes | RF reads a, b, c; RF write |
| PACK | PQ (8), PQ1 (8, `DUALQ`) | register chunk → E8 → fp16/fp32 → GT4 → L1 word, one word a cycle | RF read d, L1 write |

**L1**: 512 × 256 bit in two banks (the word address MSB). Each bank has one read port
and one write port. On a bank, fill writes win and pack yields; unpack reads win and
drain yields.

**Register file**: 32 registers × 8 chunks × 16 lanes × 24 bit, in two banks by chunk
parity. Each bank has one write port and four read copies (a, b, c for math, d for
pack), all block RAM: 2 banks × 4 copies × 16 lanes = 128 RAMB18. A streaming op writes
chunk *i* on beat *i*, so math alternates banks every cycle. An unpack write is offered
one cycle ahead and taken when math leaves that bank free next cycle. After that the
two run out of phase and both write every cycle.

## 2. Ordering

**Register hazards never stall dispatch.** Each register keeps four pairs of 6-bit
counts, dispatched (d) and finished (c), per access kind:

| count | access |
|---|---|
| W | every write (math and unpack) |
| UW | unpack writes |
| MR | math reads |
| PR | pack reads |

A queue entry carries the d counts it must wait for, read at its own dispatch. The
engine starts the entry once each c count reaches its snapshot (`reached(c, s)` =
`c - s` is non-negative mod 64). So an entry waits for exactly the older accesses.

- Math waits W of each register operand (RAW), plus UW and PR of vd (WAW against an
  unpack, WAR against a pack).
- Unpack waits W − UW of vd (older math writes), UW of vd (older unpack writes), and MR
  and PR of vd (WAR).
- Pack waits W and PR of vs.

One W count serves every RAW because the writes to a register finish in dispatch order:
math in its pipeline, and every write (math or unpack) waits for the register's older
writes. Pack reads of one register finish in order across the pack queues. At most 31
accesses of one kind are outstanding on one register (8 queued plus VL − 1 in the
pipeline), so 6 bits do not wrap.

**L1 hazards are the program's.** `VSYNC` makes one engine queue, or the front end,
wait until every instruction of a kind dispatched before it has finished (§4).

**Engine selection.** With `DUALQ = 1`, the unpack (pack) engine starts queue 0's
head when it is ready, else queue 1's. A stream that waits on L1 data then does not
hold the other stream. Sync entries at either head pop by themselves once satisfied.

## 3. Register file and operand network

Two registered stages sit between the read and the ALU input.

- **s1, bank select and merge.** `P = sel_k(A, B)` goes to a and `Z = sel_k(B, A)`
  goes to the crossbar. A lane picks by its own bit k. A plain op has merge off, so
  P = A and Z = B.
- **s2, lane crossbar on Z.** The crossbar output goes to b, and also to c with `xc`.
  The S/K constant selects follow.

| `VCFG XB` mode | b lane *l* reads |
|---|---|
| none | lane *l* |
| xor k | lane *l* ⊕ k (four ops: a 16-lane all-reduce) |
| rot k | lane (*l* + k) mod 16 |
| bcast lb, sh | lane (lb + (beat ≫ sh)) mod 16, into every lane |
| merge k | pair-merge: a = sel_k(A, B), b = xor_k(sel_k(B, A)) |

With merge, 16 row vectors reduce to one vector (lane *r* = row *r*) in 8 + 4 + 2 + 1
= 15 ops.

**Chunk addressing** (`VCFG CHK`, or `CHKVL` with VL): each operand a, b, c and the
destination d has a base chunk (0-7) and a stride (0 or 1). Stride 0 reads one chunk
on every beat. The setting applies to an op only when its `om` bit is set. With `om`
clear, an op reads every operand at chunk *i* on beat *i*, unswizzled and unpredicated.

**Predicates**: four, P0-P3, 128 bits each (one per lane per chunk). A compare writes
P[pr]. An `om` op with pm = 1 writes only the lanes P[pr] sets, and with pm = 2 the
rest.

## 4. VSYNC, marks, slack

`VSYNC waiter, on, slack, mark` makes `waiter` wait until every `on` instruction
dispatched before it has finished, except the youngest `slack` of them. That exception
is the other half of a double buffer.

| waiter | | kind (`on`) | |
|---|---|---|---|
| 0 W_FE | the front end (dispatch stops) | 0 K_F | VFILL |
| 1 W_U | unpack queue `q` | 1 K_U | unpack queue 0 |
| 2 W_M | the math queue | 2 K_M | math |
| 3 W_P | pack queue `q` | 3 K_P | pack queue 0 |
| 4 W_A | the fill queue | 4 K_D | VDRAIN |
| 5 W_MK | records mark `mark` (no wait) | 5 K_U1 | unpack queue 1 |
| 6 W_D | the drain queue | 6 K_P1 | pack queue 1 |

A sync to a queue rides that queue as an entry, so only that engine stops. A mark
records a kind and its target. A later `VSYNC ... mark` waits for exactly those
instructions, however many of the kind have been dispatched since. A front-end sync
compares registers: a per-kind outstanding count against `slack`, or a per-mark
"reached" bit that the core refreshes every cycle and clears when the mark is set.

A waiter or kind above 6 faults (F_SYNC). With `DUALQ = 0`, queue 1 does not exist.
An instruction that names it runs on queue 0, and K_U1 / K_P1 count as K_U / K_P, so a
program written for two queues keeps its ordering on one.

## 5. DMA

Two walkers of one AGU share the 8-descriptor table (base + 4 × (stride, bound), v1's
layout). Walker 0 serves fills and walker 1 serves drains, so a load and a send run
at once. The two starts share one table read, and a drain start wins a tie.

A walker's address is a register. It keeps the address where its current dimension 1,
2 and 3 runs began, so each step is one two-input add, and it counts each dimension
down. Where dimension 0 is contiguous (stride 32 B), a fill step takes
min(cap, words left in dimension 0). `cap` is the room before the next 256-word L1
boundary, the words left in the fill, and 255. A chunk register sits between the walker
and the fill's run coalescer. A run grows while each next chunk continues it at the
next address. A run holds at most 255 words and never crosses a 256-word L1 boundary.
It goes out as one read request.

A `rel` VFILL/VDRAIN adds its descriptor's running offset, which then advances by the
descriptor's increment (`VCFG AINC`).

## 6. Unpack and pack

| mode | one L1 word ↔ |
|---|---|
| 0 F16 | one chunk, 16 fp16 |
| 1 GT4 | four words as a 4 × 4 array of 64-bit granules, transposed (out word *i* = granule *i* of in words 0-3). A matmul drain word is a 4 × 4 sub-tile, so GT4 turns four of them into four flat rows. Chunk count a multiple of 4 (F_GT4). |
| 2 F32 | half a chunk, 8 fp32 (two words a chunk) |

A walk's word *w* is at base + (*w* mod 4)·s0 + (*w* div 4)·s1 (`VCFG USTR`/`PSTR`).
The base is the instruction's offset, or `UPTR`/`PPTR` + offset with `rel`. `UINC` and
`PINC` advance the pointers.

## 7. ISA

32-bit words, opcode in [31:27].

| op | name | fields |
|---|---|---|
| 0x00-0x11 | math (v1's arithmetic group) | vd [26:22], va [21:17], vb [16:12], vc [11:7], sa [6:5], sb [4:3], sc [2:1] (0 V, 1 S, 3 K), om [0] |
| 0x12 | EXP2D | exp2(a − ⌊b⌋); a per-lane sticky bit records a result ≥ 2^TAU |
| 0x13 | VCFG | sel [26:23], payload [22:0]: 0 CHK, 1 XB, 2 UPTR, 3 UINC, 4 USTR, 5 PPTR, 6 PINC, 7 PSTR, 8 GETSTK (S[r] = sticky, clears), 9 AINC, 10 CHKVL (vl − 1 [22:16], chk [15:0]) |
| 0x14 / 0x15 | VUNPK / VPACK | reg [26:22], mode [21:20], chunks − 1 [19:17], chunk base [16:14], rel [13], queue [12], offset [11:0] |
| 0x16 | VSYNC | waiter [26:24], on [23:21], slack [20:15], mark? [14], mark [13:11], queue [10] |
| 0x17 | VSKIPZ | sreg [25:22], skip [7:0]: skip forward if S[sreg] = 0 |
| 0x18 | VSETVL | sreg [20:17] |
| 0x1A | VSETI | sreg [25:22], to K [0]; the next word is the 24-bit immediate |
| 0x1B | VLOOP | sreg [20:17], body [16:7]: zero-overhead loop, S[sreg] times |
| 0x1C | VBAR | queue [10]: an unpack queue waits for every VFILL before it |
| 0x1D / 0x1E | VFILL / VDRAIN | v1's layout; rel [26] |
| 0x1F | VHALT | waits until every engine is idle |

IMEM holds 1024 words. A load's address bit 9 is CU_INST flit bit 242. RUN starts
below word 512.

Faults: F_OPCODE 4, F_LEN 5 (a walk over 256 words), F_LOOP 6 (nested VLOOP), F_VL 7,
F_CUDATA 9, F_GT4 10, F_SRC 11 (selector 2), F_MODE 12 (mode 3), F_SYNC 13.

## 8. Cost

OOC synthesis, xcvu13p-2L, `-flatten_hierarchy rebuilt`, `DUALQ = 0`, L1 block RAM
(`scripts/tcl/ooc_v2_core.tcl`, period 3.0 ns; v1 by `ooc_vec_core.tcl`, 3.333 ns).

| | v1 `vec_core` | V2 `v2_core` |
|---|---|---|
| LUT cells | 32,860 | 35,239 |
| CLB LUTs | 31,113 | 31,257 |
| LUT as memory | 2,639 | 1,632 |
| registers | 25,809 | 34,235 |
| block RAM tiles | 36.5 | 89 |
| DSP | 51 | 54 |
| Fmax | 254.8 MHz | 338.6 MHz |

`DUALQ = 1` adds 692 LUT cells. The register file is 128 RAMB18 (2 banks × 4 read
copies × 16 lanes). A read copy whose address names the other bank resets its output
latch, so the two banks of a port combine by OR inside the next multiplexer.

Front-end instructions whose condition reads a counter or a scalar register (`VSYNC` on
the front end, `VSETVL`, `VSKIPZ`, `VSETI`, `VHALT`) evaluate that condition into a
register and leave on their second cycle in decode. A contiguous fill takes one cycle
between chunks so its size limit comes from a register.

## 9. Measured kernels

One core on `vec_replay_tb` (Verilator, `DUALQ = 0`), run by
`python scripts/py/vec2/bench.py KERNEL [memwait=N] [key=value ...]`. The kernels are
hand-written in `scripts/py/vec2/kernels.py` (`ew`, `softmax`, `layernorm`, `attn`,
`attn_om`). The memory answers one word every `memwait + 1` cycles: 0 gives the core
bound, and 1 gives 0.5 word a cycle, the NoC rate one vector core sees. Lane use is lane
beats ÷ (16 × busy cycles).

| kernel | lane beats | busy, memwait 0 (lane use) | busy, memwait 1 (lane use) |
|---|---|---|---|
| `ew` silu, 8192 | 2,560 | 2,934 (87.2%) | 3,062 (83.6%) |
| `softmax` 128 × 128 | 5,360 | 7,716 (69.5%) | 7,844 (68.3%) |
| `layernorm` 128 × 128 | 5,936 | 9,249 (64.2%) | 9,393 (63.2%) |

### Flash attention

One 32-row query tile, 64-key blocks, head dimension 64. S arrives in sub-tile layout.
Two schedules:

- **O on the vector** (v1's algorithm): running max, P = 2^(S − ⌊m⌋), and O = O·corr + PV
  and l on the core. PV is drained from the mat every block.
- **O on the mat**: P = 2^(S − I), with I an integer per row equal to ⌊row max⌋ of the
  segment's first block. The mat chains P·V into one accumulator per segment. After each
  block's EXP2D, the front end waits on that block's math mark, reads the sticky bit
  (`GETSTK`) and skips the close code when it is clear. A close moves I to ⌊row max⌋ on
  the rows whose largest P reached 2^8, and scales those rows of P by
  corr = 2^(I_old − I_new). It also folds the mat's segment into
  acc = (acc + O_s)·corr, held in memory as fp32. The row sum runs on the vector
  (`rs = 1`) or comes from a ones column in V (`rs = 0`). The end writes
  O = (acc + O_last) / l, and reads acc only when a close happened.

Per block, the O-on-mat kernel interleaves, register by register, the pack of P_j, the
unpack of S_{j+1} and the EXP2D of P_{j+1}. The row sum's first level reads the P
registers before the next unpack overwrites them, and its merges ride between the next
block's EXP2Ds. Program size: 965 words (`rs = 1`), 901 (`rs = 0`).

Cycles per block are the slope between 8 and 24 blocks (`reuse=1`). The fixed cost is what
a tile adds beyond its blocks. The cost per close is measured on 6 blocks with
`drift=4`, which closes four times.

| schedule | cycles / block, memwait 0 | cycles / block, memwait 1 | fixed per tile | per close |
|---|---|---|---|---|
| O on the vector (`attn`) | 1,024 | 1,280 | | |
| O on the mat (`attn_om`), `rs = 1` | 283 | 283 | 1,723 | ~1,680 |
| O on the mat (`attn_om`), `rs = 0` | 214 | 256 | 1,704 | ~1,640 |

On 4 mat + 2 vec, a vector core serves two clusters. One block is 256 cluster-cycles of
S and P·V, which is 128 cycles on two clusters. MFU = 128 ÷ vector cycles per block:

| schedule | MFU, steady state, memwait 1 | bound |
|---|---|---|
| O on the vector | 10% | vector |
| O on the mat, `rs = 1` | 45% | vector math (256 beats a block) |
| O on the mat, `rs = 0` | 50% | S into the core at 0.5 word a cycle |

A query tile of n keys costs fixed + (n / 64) × block + closes × close. With no close,
`rs = 0` reaches 35% MFU at 1,024 keys, 45% at 4,096 and 49% at 16,384. Each close at
16,384 keys costs about 1.2 points.

Error of the O-on-mat kernel against fp64 run with the same segments:
P 2.8 × 10⁻⁵ and O 3.4 × 10⁻⁴ with no close, and P 1.7 × 10⁻⁵ and O 2.7 × 10⁻⁴ with four
closes.

## 10. Parameters

| parameter | default | |
|---|---|---|
| `DUALQ` | 0 (`-d V2_DUALQ` sets 1) | the second unpack and pack queue |
| `IMEM_DEPTH` | 1024 | 512 or 1024 |
| `QD` | 8 | MQ, UQ and PQ depth |
| `HAS_EXPD`, `TAU` | 1, 8 | EXP2D and its sticky threshold |
| `L1_PRIM` | block | `ultra` adds a read cycle |
