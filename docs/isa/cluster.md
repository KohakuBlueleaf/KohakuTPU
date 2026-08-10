# The CU instruction flits

The three instructions a cluster executes.

Source: [`src/kohakutpu/matmul/mx_cluster_cu.v`](../../src/kohakutpu/matmul/mx_cluster_cu.v)
(decode and sequencing),
[`mx_cluster_mgr.v`](../../src/kohakutpu/matmul/mx_cluster_mgr.v) (the GEMM
sweep), [`src/ktpu/hw/matmul.py`](../../src/ktpu/hw/matmul.py)
(`_flit`, the encoder).

> [`../compute/tensor-isa.md`](../compute/tensor-isa.md) describes a larger ISA
> — tensor descriptors, `SETD`, `SYNC`, hardware `LOOP` — that was the design
> agreed before building. **None of that is implemented.** What exists is the
> three opcodes below, with addresses computed by the driver. This document is
> the implemented set; that one is the intended one.

---

## 1. One cluster, one port

```
   NoC <-> manager <-> tcu -> tcu -> tcu -> tcu -> acu
            L1           direct DSP cascade      resident tile
```

One NoC local at `(CU_X, CU_Y)` carries everything: instructions and fill
responses in; fetch descriptors, drain writes and completion signals out.
Inbound is demultiplexed by flit type, the way `mag.v` shares its memory ports.
Outbound, the fetch descriptor and the drain share one queue and **the fetch
descriptor wins** — a FILL is a single flit the sequencer is blocked on, so
delaying it costs a whole memory round trip, while a drain flit is bulk traffic
behind arithmetic that has already finished and waits at most one flit.

> **This was two ports until 2026-08-10**, a manager at `MGR_X/MGR_Y` and an
> accumulator at `ACU_X/ACU_Y`. The second endpoint bought no bandwidth — the
> link is full duplex and the two ends loaded opposite directions of it. What it
> cost was a router local: eight clusters at two locals each force a 4x4 mesh
> where one local each fits 2x4, which is eight `NoCRouter` instances at 3,281
> LUT apiece.
>
> The risk was head-of-line blocking on the shared outbound queue. Measured at
> 128x128x128 on two clusters: **`cu_send` 0.0%, `out_bp` 0.0%** — the CU is
> never blocked sending. The machine is drain-bound (`cu_dwait` 62.8%), which is
> what makes the priority above the right way round rather than arbitrary.
>
> **The flit layout below did not change.** Source and destination coordinates
> keep their positions and simply carry `CU_X/CU_Y` in both directions.

One port, not five. The chain eats eight 256-bit operand words per cycle and a
port delivers one, so feeding the TCUs directly from the NoC is an 8x deficit no
matter how many ports are spent on it. Reuse closes the gap instead: a
`Gm x Gn` sub-tile block needs `4(Gm+Gn)/(Gm*Gn)` words per cycle, which is
0.375 at 16x32.

## 2. Flit layout

The header is the standard one from [memory.md](memory.md) §1. `CU_INST` is type
`0x5`; the driver stamps txn `0x40`.

| bits | field | used by | meaning |
|---|---|---|---|
| `[259]` | `last` | header | last instruction of the batch — see §6 |
| `[255:252]` | `op` | all | `1` FILL, `2` GEMM, `3` DRAIN |
| `[251:218]` | `addr` | FILL, DRAIN | byte address: operand base / destination base |
| `[217:202]` | `n` | FILL, DRAIN | L1 entries / sub-tiles |
| `[201]` | `sel` | FILL | `0` = A, `1` = B |
| `[200]` | `acc` | GEMM | accumulate into the resident tile |
| `[199:192]` | `gm` | GEMM | row groups |
| `[191:184]` | `gn` | GEMM | column groups |
| `[183:176]` | `nk` | GEMM | K blocks |
| `[175:168]` | `anchor` | GEMM | common output exponent |
| `[167:144]` | `peers` | FILL | up to 3 co-destinations, `{y,x}` each — §3.1 |
| `[143:142]` | `npeer` | FILL | how many `peers` are present |
| `[141]` | `preq` | FILL | the operand is already int7+E5M3 in memory — §3.2 |
| `[140:133]` | `eoff` | FILL | first L1 entry to write — §4.6 |
| `[132:125]` | `aoff` | GEMM | base L1 entry on the A side — §4.6 |
| `[124:117]` | `boff` | GEMM | base L1 entry on the B side — §4.6 |
| `[116]` | `emit` | GEMM | hand each sub-tile out as it finishes — §5.1 |
| `[115]` | `fuse` | DRAIN | barrier only; the sweep already wrote them — §5.1 |
| `[114]` | `abank` | GEMM | which 256-entry half of L1 A this sweep reads — §4.6 |
| `[113]` | `bbank` | GEMM | ... and of L1 B |
| `[112]` | `fbank` | FILL | which half this fill writes |
| `[111]` | `dnode` | DRAIN | send to a NoC node instead of to memory — §10 |
| `[110:107]` | `dst_x` | DRAIN | destination node |
| `[106:103]` | `dst_y` | DRAIN | |
| `[102:95]` | `buf` | DRAIN | the destination's buffer id — §9.3 |
| `[94:87]` | `dflags` | DRAIN | copied into the descriptor's `flags` byte |
| `[86:83]` | `dack_y` | DRAIN | where the receiver's completion goes — §9.2 |
| `[82:79]` | `dack_x` | DRAIN | `0` = back to this cluster |
| `[78:0]` | — | | unused |

> **The bank bits and the drain destination were specified for the same bits**
> and do not share them. `[114:112]` belong to `GEMM`/`FILL` and the
> destination would have been `DRAIN`-only, so overlapping them would have
> worked — and would have been exactly the opcode-dependent field this section
> warns about two paragraphs down. The destination moved down three bits
> instead. Nothing is lost: 87 bits are still spare.

> **`n` is 16 bits, and it had to become so.** The resident tile holds 512
> sub-tiles, so a `DRAIN` names 512 — which an 8-bit field wraps to 0, draining
> the beginning of the tile a second time and reporting nothing. Every field
> below it moved **down** rather than sharing bits with `gm`/`gn` on the
> grounds that a FILL never uses them. A field whose meaning depends on the
> opcode is how a decode bug survives review; the payload has 142 spare bits
> and no reason to overlap anything.

RTL decode and driver encode agree on every field, checked against
`mx_cluster_cu.v`'s decode block and `matmul._flit`.

Field widths matter more than they look. An unsized value in the wrong place
shifts every field below it and elaborates cleanly — `(WA_BASE + b*4) * 32`
written straight into a concatenation contributes 32 bits, not the 34 the
address field is, and the whole payload came out 4 bits short. The CU read
nonsense and executed nothing: `blocks=0`, no memory traffic, no error anywhere.

`addr` is checked for `x` at the producer in simulation. An `x` in an address is
invisible downstream and fatal — memory returns `x`, the quantiser packs it, the
accumulator sums it, and the drained tile is a plausible-looking zero. The
symptom is "the compute is wrong", pointing at the datapath several modules away
from the one-line cause.

## 3. `FILL addr, n, sel, preq`

Load `n` L1 entries starting at byte `addr` into the A side (`sel = 0`) or the B
side (`sel = 1`).

One L1 entry is 4 lanes x 32 K elements: 256 bytes as FP16 source, 128 bytes
once quantised. The CU issues **one** `MEM_RD_REQ` for the whole run (§3.1)
with `BLAYOUT = sel` and `QUANT = !preq`, and MAG returns exactly 4
`MEM_RD_RESP` flits per entry either way. The CU reassembles each set of four
into one 928-bit entry:

| bits | contents |
|---|---|
| `[895:0]` | 128 int7 elements, 7 bits each |
| `[927:896]` | 4 x E5M3 block scale, one per lane |

Element index within `[895:0]` differs by side, which is where the transpose
happens:

| side | element | index |
|---|---|---|
| A | row `i`, K `k` | `i*32 + k` |
| B | K `k`, column `j` | `k*4 + j` |

The reassembly is unrolled over the four response words rather than indexed by a
counter. A variable part-select write builds a barrel mux across all 928 bits.

`n = 0` is coerced to 1. A `FILL` is one instruction per contiguous run of
entries, which is the whole reason operands are stored tile-major —
see [kernel.md](kernel.md) §3.

**Capacity.** `GA` entries for A and `GB` entries for B — **128 and 256** in
`mag_driver_tb`. One chunk needs `gm*nk = 64` and `gn*nk = 128` at the 16×32
tile, and both are doubled: A because it is double-buffered, B because every K
chunk of a column band stays resident across the m loop (§4.6). The write
address is truncated to `$clog2(GA)` bits, so `n > GA` **wraps and overwrites
entries from 0** with no error, and the sweep then reads whatever survived.

### 3.1 One descriptor, and who else receives it

A `FILL` is **one memory flit**, not `n` of them. It carries `{base, count}`
with a `STREAM` flag and MAG walks the address sequence itself, returning
`count × 4` responses each tagged `{entry, word}`. The CU has no requester and
no receive cursor: a response says which L1 slot it belongs to and which
quarter of it, so arrival order carries no meaning.

That tagging is what makes the rest expressible. Previously a response was
identified only by the order it turned up in, so a second outstanding read had
nowhere to be named — which is why an earlier attempt to keep several reads in
flight could not have worked at any depth. It was not a tuning failure; the
field did not exist.

`peers` names the other clusters reading the same bytes at the same moment.
The lowest node index in the set issues the descriptor and the rest issue
nothing, so MAG reads the operand from DRAM once and runs the quantiser over it
once however many clusters want it.

> **`peers` is decoded and the driver does not set it.** A follower cannot tell
> *which* fill an arriving entry belongs to, so a shared A entry reaching a
> cluster that is still executing its `FILL B` is written into the B side of L1.
> Measured at 256-cube: 85.1 → 105.1 GFLOP/s and the worst element 1.0 → 2.2e+02
> against the MXFP7 model. It needs a rendezvous — MAG must not start a shared
> stream until every subscriber has asked — which is not built.

### 3.2 `preq` — where the quantisation was paid

`preq` says the operand was converted on its way **into** memory, so it is
already int7+E5M3 there. The fetch is then 4 words per entry with a 128-byte
stride instead of 8 FP16 beats with a 256-byte stride, and memory's quantiser is
not involved at all.

It is a property of the **tensor**, carried by the instruction. Memory holds no
map of which addresses are which format and must not learn one: the driver is
the only party that knows which tensors are reused enough to be worth
converting once, and it says so on every `FILL` that reads them. The driver
still never constructs int7+E5M3 — it marks the upload and the hardware
converts. See [memory.md](memory.md) §6.4.

Per `FILL`, so the two operands of one `GEMM` may differ. That is the normal
inference case: weights uploaded once and pre-quantised, activations produced
this layer and quantised on read.

## 4. `GEMM gm, gn, nk, anchor, acc`

Sweep `gm x gn` output sub-tiles across `nk` K blocks.

```
   for kb in 0 .. nk-1:            <- K OUTERMOST
     for g in 0 .. gm-1:
       for h in 0 .. gn-1:
         chain <- L1A[g*nk + kb], L1B[h*nk + kb]
         acu   <- (kb == 0 && !acc) ? LOAD : ADD   at tile address g*gn + h
```

Each of `gm`, `gn`, `nk` is coerced to 1 if written as 0.

### 4.1 Why K is outermost

An output sub-tile address recurs every `gm*gn` cycles instead of every cycle.
That is what lets the accumulator be a plain memory with a synchronous read: with
K inner, back-to-back same-address accumulation cannot close a pipelined adder
loop at all, and the previous design paid for it with three rotating banks plus a
fold on `EMIT`.

It is a loop-order choice with an architectural consequence — it deletes the
banks, the fold, the zero mask, and three quarters of the tile memory. What it
costs in exchange is that the caller now owes `REUSE_MIN`, which was previously
structural: [acu.md](acu.md) §6.

### 4.2 The `acc` bit

`acc` is what makes `s1_first = (kb == 0) && !acc_r`.

Without it, every `GEMM` starts its first K block with `OP_LOAD`, which
overwrites the resident tile. An output tile could then only ever be produced by
**one** instruction, and a K longer than L1 could not be expressed at all — the
only way to compute it would be to drain a partial tile to memory per chunk and
read it back.

With it, K is split into chunks that chain into the same resident tile. The tile
is written to memory once, not once per chunk. That is the difference between
`M*N` of C traffic and `M*N*(K/Kc)`, in both directions. At K=4096 with a
128-element chunk that is 32x.

A driver emits `acc = (ko > 0)` across a pass's K chunks.

### 4.3 `anchor`

The block scales are stored with their exponents biased by `SBIAS = 20`. The
accumulator computes `ea + eb - anchor`, so `ANCHOR = 2*SBIAS = 40` cancels both
stored biases and leaves the true exponent sum. It is a constant of the format,
not a tunable — the driver imports it from `mxfp7.ANCHOR`.

### 4.4 Reuse pacing

The accumulator requires `REUSE_MIN = 5` cycles between two commands to the same
tile address. K outermost gives `gm*gn` cycles by construction, so any tiling
with `gm*gn >= 5` needs nothing. Below that the manager inserts idle cycles:

| `gm*gn` | idle cycles inserted |
|---|---|
| 1 | 4 |
| 2 | 2 |
| 3, 4 | 1 |
| >= 5 | 0 |

`nk <= 1` never paces at all — one K block never revisits an address.

The idle cycles cost nothing at any tiling big enough to be worth running. A
tiling smaller than `REUSE_MIN` that did not pace would accumulate onto a stale
tile, silently, and only for part of the K sweep.

### 4.5 `GEMM` retires when the sweep STARTS

The instruction is done with the sequencer the moment the manager takes it. The
sweep runs on for `gm*gn*nk` cycles afterwards and needs nothing more from the
CU, so holding the instruction there only stopped the CU from doing the one
thing that would overlap with it — filling the other half of L1. Measured: FILL
was 22.3% of the machine's time with the array idle through every cycle of it.

What still waits, and where:

| | waits for |
|---|---|
| the next `GEMM` | `!gemm_busy` — one sweep at a time |
| an **emitting** `GEMM` | also `!drain_busy` — it sets the write base |
| an issuing `DRAIN` | `!gemm_busy` — it takes the accumulator's control mux |
| a fused `DRAIN` | nothing; it is a barrier on its own results |
| `FILL` | nothing at all, which is the point |

A `FILL` not waiting is what makes L1 a shared resource with no interlock. The
driver owns the banking (§4.6); `mx_cluster_mgr` has a simulation-only check
that fires if a fill lands inside the range the running sweep is reading,
because the alternative is a few corrupted sub-tiles and no report.

### 4.6 `aoff`, `boff`, `eoff` — L1 is addressable, and banked

`eoff` is where a `FILL` lands; `aoff` and `boff` are where a sweep reads. They
buy two different things.

**Double buffering.** Consecutive K chunks go to alternate halves of L1 A, so
the fill for chunk *i+1* runs while chunk *i* is still being swept. Two banks
is exactly enough: a sweep waits for the one before it, so only two chunks are
ever live.

**Residency.** B does not change across the m loop. Re-filling it per m-tile
was a quarter of all memory traffic at the 256-cube — 4,096 beats of 16,384. If
every K chunk of B fits at once, `kernel.plan` fills it once per column band and
leaves it, walking m over it in place. That is also why the driver iterates the
column band OUTERMOST: with m outside, B for the next band would overwrite the
current one before the m loop came back to it.

`eoff` costs nothing to carry: it rides in the memory request's `txn` field,
which memory already echoes plus each entry's position in the run — so the
response names the exact L1 slot and the receiver still needs no cursor.

**L1 is two banks of 256, not one flat 512.** `aoff`, `boff` and `eoff` are
8-bit fields, so a 512-entry L1 cannot be named by an offset alone: `abank`,
`bbank` and `fbank` pick the half, and the address is the bank concatenated
with the offset, truncated to `$clog2(GA)`.

That truncation is what makes it free in both directions. At `GA = 256` the
bank bit falls off the top and the address is what it always was, so **`0` is
the lower half and every instruction written before these bits existed still
addresses exactly what it did**. The alternative — widening `aoff` to 9 bits —
moves every field below it, which is how a decode bug survives review.

Widening is also what broke first: `GA = 512` alone makes `AAW = 9` while
`gemm_aoff` stays 8, and `gemm_aoff[AAW-1:0]` overruns the field. Dropping to
256 to avoid it threw away half the L1 for nothing, because **13 RAMB36 per
port is the cost at any depth up to 512** — width sets the primitive count and
the depth is already paid for, the same argument that put `TILES` at 512.

The offset **cannot carry into the bank**: the running address `aoff + g*nk + kb`
stays 8 bits, so a sweep that overruns its region wraps inside its own half
rather than walking into the other bank's operands. That is the same silent
wrap §4.7 already describes, kept silent in the same way.

A `FILL` and a `GEMM` name their banks separately, which is the double
buffering above with twice the L1 behind it: fill one half while the other is
swept, then flip a bit instead of recomputing offsets. A `CU_DATA` stream needs
no bank field at all — `off` counts granules through the whole 512-entry
buffer, so the bank is simply where the entry index runs off the end of a byte.

> **Two banks is the ceiling.** A deeper L1 needs a second bank bit;
> `mx_cluster_mgr` reports it at elaboration rather than silently dropping the
> top address bit.

### 4.7 When `GEMM` is finished

`gemm_busy` is `run || s1_valid || s1b_valid || s2_valid || (pending != 0)`,
where `pending` counts issued-minus-retired sub-tiles, plus the accumulator's own
`busy`.

Not "the last tile has been issued". The cascade is ~19 cycles deep, so when the
counters finish there are still that many results in flight. Reporting done
there let `DRAIN` seize the accumulator's control mux and cut them off — the tail
sub-tiles came back as zeros.

Nor `!cmd_empty` on the ACU command FIFO. That is the FIFO's registered flag and
it deasserts two cycles after a push, so there is a hole where the block reads
idle with commands still queued. Only a tiling short enough to finish inside that
hole can hit it, which is why every bench down to `gm*gn = 3` passed and a
2-sub-tile one did not. Counting issued-minus-retired is exact and owes nothing
to FIFO timing.

**Capacity.** `gm*nk <= GA`, `gn*nk <= GB`, `gm*gn <= TILES`. All three wrap
silently when exceeded: L1 addresses truncate to `$clog2(GA)` bits and the tile
address to `$clog2(TILES)`. `mag_driver_tb` runs `TILES = 512`, `GA = 128`,
`GB = 256` — the 16x32 tile with `nk = 4`, two banks of A and every K chunk of
one column band of B (§4.6). `TILES` is 512 because that is what the tile
memory **already costs**: a sub-tile is 352 bits against a 72-bit BRAM36 port,
so the array is `ceil(352/72) = 5` primitives at any depth up to 512, and
running 64 left 448 sub-tiles of paid-for depth unused.

> **`gm*nk <= GA` is not the binding constraint any more; `2*gm*nk <= GA` is**,
> because A is double-buffered. And a `FILL` may not name more than 255 entries
> whatever L1 holds — the memory request's streaming count is 8 bits, and 256
> wraps to 0, which memory coerces to 1. `kernel.choose_tile` caps `nk` for
> both.

The generated tops now build `GA = GB = 512` — two banks of 256 (§4.6). The
figure the *compiler* plans against is `bench.py`'s `L1_A_ENTRIES` /
`L1_B_ENTRIES`, still 128/256: capacity is an upper bound the planner need not
fill, and at 928 bits wide the cost is 13 RAMB36 per port at **any** depth up
to 512, so the headroom is free either way.

## 5. `DRAIN addr, n, anchor, fuse, last`

Get `n` resident sub-tiles into memory as FP16, starting at byte `addr`.
Sub-tile `t` goes to `addr + t*32` — one 256-bit word per 4x4 sub-tile, in the
manager's sweep order, row group major.

Or into another unit rather than into memory — §10.

### 5.1 Fused: the sweep hands them out

A sub-tile's **last accumulation already computes its finished value** at stage
5 of the accumulator. A separate `DRAIN` reads the same address back and passes
it through the same pipeline to recover it — and that second pass needs one
accumulator command per sub-tile, which a sweep has none spare of: it issues a
command every cycle. So a drain could never overlap a sweep, and it was 24% of
the machine's time.

`GEMM.emit` fuses them. Every issue of the sweep's **last** K block becomes
`OP_ADD_EMIT`: it writes the tile back *and* hands the value out, same command,
same cycle, no re-read. The emitting `GEMM` carries the destination in its own
`addr`, because it starts producing sub-tiles long before the `DRAIN` behind it
is decoded. `DRAIN.fuse` then means "these already left; wait for them", and it
does **not** wait for `gemm_busy` — so one tile's results can still be draining
while the next tile's sweep runs.

What replaces the command slots is buffering and backpressure. The last K block
completes one sub-tile per cycle while memory retires a burst of 8 in ~11
cycles, so the queue is 128 deep to carry the burst until the gap after it;
past that the **sweep** is held, because once a command is in the accumulator
its result arrives ~19 cycles later whatever happens downstream.

> **`OP_LOAD_EMIT` does not exist**, so an emitting sweep must not also be the
> one that opens the tile. It never is in practice: the emitting sweep is the
> last K chunk, so either an earlier chunk opened the tile (`acc = 1`) or there
> is one chunk with `nk > 1` and the first and last K blocks differ. Only a
> single chunk of a single K block collides, and `kernel.plan` falls back to an
> issuing `DRAIN` there. `mx_cluster_mgr` reports the collision if it ever
> happens.

### 5.2 Issuing, the fallback

The drain sequencer does not count cycles: the CU used to count 10 after issuing
`EMIT`, and deepening the accumulator pipeline made 10 too few, so it wrote a
zero result while every unit test still passed. It issues `EMIT`s ahead of the
results and bounds itself by issued-minus-taken against a 16-deep result queue,
so the accumulator is not stalled waiting for the write port.

Emitted sub-tiles are collected into bursts of `WBURST = 8` and written as one
`MEM_WR_REQ` descriptor with `len = 7` followed by 8 `MEM_WR_DATA` flits.
Consecutive `EMIT`s address *different* sub-tiles, so the accumulator's
`REUSE_MIN` does not apply between them.

> **One transaction per sub-tile does not fit.** Memory retires one single-beat
> write per visit to its idle state — about 4 cycles, 3 of them the RAM — while
> two clusters can produce a pair every cycle between them. Pipelining the drain
> without bursting just wedges the memory port's write intake queue.
> `WBURST = 1` reduces
> exactly to the old behaviour, which is what makes it safe to turn down.

The burst must be **closed when the drain runs dry**, not only when it is full,
or a tile whose count is not a multiple of `WBURST` leaves its tail in the
buffer forever. And a `DRAIN` is finished when the last write has **left the
CU**, not when the accumulator stops producing — retiring earlier makes the
instruction's completion signal run ahead of the memory traffic it stands for.

> **`DRAIN`'s `anchor` field is dead.** The driver encodes `anchor=ANCHOR` on
> `DRAIN` and the CU latches it into `anch_r`, but `mx_cluster_node` forces
> `anchor`, `sa` and `sb` to zero into the accumulator for the whole time
> `drain_busy` is high. `EMIT` reads the tile and converts; it applies no scale.
> The field is decoded and discarded.

**Capacity.** The tile address truncates to `$clog2(TILES)` bits, so `n > TILES`
wraps to sub-tile 0 and drains the beginning of the tile a second time. `n`
itself is 16 bits, and had to become so when `TILES` reached 512 — see §2.

## 6. `last` and completion

`last` is a **header** bit at `[259]`, not a payload field — the same bit the
memory protocol uses for the final beat of a burst.

`noc_cu_base` latches it when it issues the instruction and reports retirement
as `SIG_BATCH_COMPLETE` (`0x01`) instead of `SIG_INST_COMPLETE` (`0x00`), with
the program id as the argument. The driver sets it on the `DRAIN` that ends each
pass and nowhere else.

Two consequences, both documented where they bite:

* `SIG_DONE` counts every signal type, so the expected total is the flit count
  — see [agent.md](agent.md) §6.
* Dispatch credit is returned only for `INST_COMPLETE`, so the `last` flit costs
  a credit permanently — see [agent.md](agent.md) §6.1.

## 7. Sequencing

The CU executes one instruction at a time. `noc_cu_base` holds an instruction
FIFO of `INST_DEPTH = 32` and will not issue while a signal cannot be queued:
executing an instruction that cannot be reported is worse than not executing it,
because the report is what returns the dispatch credit.

```
   S_IDLE  decode, latch fields
   FILL    S_FILL: one descriptor out, then place responses until n arrive
   GEMM    S_GEMM -> S_GWAIT until !gemm_busy
   DRAIN   S_DRAIN -> S_DWAIT until !drain_busy AND the write port is idle
   S_DONE  exec_done, exec_result = nfill + ngemm + ndrain
```

`FILL` is **one** state, not three. It used to be `S_FREQ -> S_FRCV -> S_FWR`
per entry, which is the shape a per-entry requester forces; the descriptor
removed the requester rather than pipelining it.

`fills_done` / `gemms_done` / `drains_done` are free-running counters exposed to
the bench.

## 8. A pass

The four instructions one cluster runs for one output tile of one K chunk:

```
   FILL  A, gm*nk entries
   FILL  B, gn*nk entries
   GEMM  gm, gn, nk, anchor, acc = (chunk > 0)
   DRAIN C, gm*gn sub-tiles, last = 1        (only after the final chunk)
```

Multiple K chunks repeat the first three and drain once at the end. That is
`3*chunks + 1` flits per pass, which is what bounds how many passes fit in the
staging window.

## 9. `CU_DATA` — one unit writing into another

A cluster's operands do not have to come from memory and its results do not
have to go there. `CU_DATA` is the flit type that carries a bulk transfer
between two NoC endpoints: another cluster's partial sums, or a vector core's
output landing directly in L1.

### 9.1 The type is `0x8`

**Not the `0x4` [`../noc/spec.md`](../noc/spec.md) names.** `0x4` is
`MEM_WR_DATA`. The two would be indistinguishable at MAG, which demultiplexes
inbound flits by type precisely so that a write's data flits can be recognised
after the mesh has interleaved something between them — see
[memory.md](memory.md) §1. A `CU_DATA` flit reaching a memory port would enter
the write queue as data and be stored.

### 9.2 A stream is a descriptor and its data

One descriptor flit, then `len+1` pure data flits, the last of them carrying the
header's `last` bit.

| bits | field | meaning |
|---|---|---|
| `[255:248]` | `buf` | which buffer of the receiver — §9.3 |
| `[247:232]` | `off` | first 32-byte granule within that buffer |
| `[231:224]` | `len` | data flits minus one |
| `[223:216]` | `flags` | bit 0 `signal_on_complete` |
| `[215:212]` | `ack_y` | where the completion goes; `0` = the sender |
| `[211:208]` | `ack_x` | |
| `[207:0]` | — | unused |

`off` counts **32-byte granules**, which is one data flit's payload, and it
advances by one per data flit. A stream is therefore a run: the receiver needs
no cursor of its own and no per-buffer state beyond the one it is filling.

Data flits carry payload only — no length, no index, nothing that has to agree
with the descriptor. That is what keeps the receiver from having two opinions
about where a flit goes.

**`flags[0]` asks the receiver to report.** It answers with a `CU_SIGNAL` of
code `SIG_DATA_RECEIVED` (`0x03`) and `arg = buf`, addressed to the
**descriptor's** source. Without it a unit that sends and waits would block
for ever: `noc_cu_base` signals on instruction retirement and a burst is not an
instruction, so it has no other way to report itself.

Two rules make it trustworthy, and the vector core implements the same pair:

* **It wins the outbound arbiter**, over the fetch descriptor and the drain
  both. It is one flit, the sender is blocked on it, and the receive path is
  held until it leaves — so anything queued in front of it is paid twice.
* **The receive path stalls while one is pending**, so a second burst cannot
  overwrite the completion the first has not sent yet.

A **rejected** burst is still acknowledged (§9.3). A signal that only arrives
when the data was good is a signal the sender cannot wait on.

**`ack_x`/`ack_y` say where it goes, and `0` means the descriptor's source** —
which is what every burst did before the field existed, so host→CU is unchanged.
It exists because **a CU→CU transfer's completion is otherwise unobservable**:
the descriptor's source is the *sending* unit, and no CU consumes an ack, so
nothing could sequence a reader behind a writer. A cluster sending to a vector
core names the orchestrator, and the host waits on `NODE_STATUS` for it.

`(0,0)` is a safe sentinel because `gen_mesh.py` requires the mesh corners
empty — a corner touches no router, so no endpoint can ever live there.

> **The ack destination gets its own registers, not the sender's.** They hold
> the same value whenever the ack is not redirected, so one shared pair passes
> every test *except* the case the field exists for. The interleave check
> (§9.2) compares a data flit's source against the open stream's; against the
> ack destination instead, a redirected burst faults on every flit.

The cluster puts it at `[86:83]`/`[82:79]` of the `DRAIN`; the vector core puts
it elsewhere. Only the **descriptor** wire format is shared.

> **One open stream per receiver.** The mesh interleaves, and a receiver holds
> exactly one `{buf, off, left}`, so a second sender's descriptor is consumed as
> the first sender's data. `mx_cluster_cu` reports it: the header `last` bit
> stops agreeing with the descriptor's own `len`. There is no arbitration — the
> driver owns it, the same way it owns L1 banking (§4.6).

### 9.3 A cluster's buffers

| `buf` | destination | granule |
|---|---|---|
| `0` | L1 A | one of an entry's 4 words — `entry = off >> 2`, `word = off & 3` |
| `1` | L1 B | likewise |
| `2` | the resident output tile, through `OP_ADD_PEER` | half a sub-tile — `subtile = off >> 1` |

`0` and `1` match `sel` on a `FILL`, and a data flit for them is **byte-identical
to a `MEM_RD_RESP` payload**: 32 int7 elements and the entry's 4 E5M3 scales,
placed by the same permutation, transposed for B. A sender into L1 is
substituting for MAG and owes the same format — see §3.

Literally the same permutation: the two sources meet at one placement block in
`mx_cluster_cu` and differ only in where `{entry, word}` comes from. Building
the 928-bit unrolled write twice would have been ~900 LUT of duplicate, and the
copy that no bench exercised would have been the one that drifted.

**A burst is rejected, never wrapped.** `off` is 16 bits against an L1 of `GA`
entries, so a burst that runs off the end would silently overwrite the bottom
of the buffer — the quietest possible corruption. The descriptor is range
checked against the named buffer (`GA*4`, `GB*4`, `TILES*2` granules), an
unknown `buf` fails it, and a peer burst starting on an odd granule fails it
too because a sub-tile is a pair.

A rejected burst is **counted out and then dropped**, not skipped: the receiver
still consumes `len+1` data flits, because otherwise the next data flit is read
as a descriptor and one bad burst desynchronises every burst after it. Nothing
is written, the completion signal is still sent, and a sticky error reports
`SIG_FAULT` at the next instruction boundary — then clears, so one malformed
burst is one fault and not a CU that faults for ever.

Dropping rather than holding is forced: an unconsumed flit fills the receive
FIFO, raises `noc_in_busy` for good, and wedges the instruction stream behind
it.

### 9.4 `buf = 2` — peer accumulation

`mx_acu_fp` has always implemented `OP_ADD_PEER`, `OP_SEND` and `OP_FWD` so that
one matmul can span clusters. `mx_cluster_node` grounded the ports —
`peer_in` tied to zero, `peer_out` and `peer_valid` left open — from the merge
of the manager and accumulator nodes until 2026-08-10, so none of it was
reachable.

A peer sub-tile is `16 x (ACC_MW+8)` bits — **352 at `ACC_MW = 14`**, the
accumulator's own float, not FP16. It does not fit one payload, so it is two
granules: `2t` carries bits `[255:0]` and `2t+1` carries the rest in its
payload's low bits. A stream whose `off` is odd is a fault, reported.

The value is added into the resident tile, so the sub-tile keeps its full
accumulator precision across the transfer. That is the whole point: a K-split
across clusters that went through memory would round to FP16 in between, and a
`DRAIN`+`FILL` round trip costs the write, the read and the quantiser pass.

Three contracts, none of them structural:

* **The tile must already be open.** `OP_ADD_PEER` reads the tile address it
  writes, and the tile memory has no reset. The receiving cluster must have run
  a `GEMM` that opened those sub-tiles with `OP_LOAD` first — see [acu.md](acu.md).
* **A peer stream and a sweep may not overlap.** The stream waits for
  `gemm_busy` at its head and then holds the accumulator's control mux for its
  whole length; `S_GEMM` and `S_DRAIN` wait for the stream to close. Tested once
  rather than per sub-tile, because `gemm_busy` covers the accumulator's
  `REUSE_MIN` tail and re-testing it would idle ~12 cycles a sub-tile.
* **`REUSE_MIN` still applies.** Consecutive peer sub-tiles address different
  tiles, so it does not bind between them, and the accumulator takes one
  command per cycle. A stream that revisits a sub-tile within 5 cycles does not.

The command is issued through the drain sequencer's own `d_op`/`d_addr`/`d_cmd`
registers rather than as a third input to the accumulator's control mux. That
mux is one level deep by design — `mx_acu_fp` timing rule 2 — and a third source
would put a second level in front of the tile address. The sequencer is idle
whenever a peer sub-tile may issue, so the registers are free.

### 9.5 How it is tested

`tests/matmul/mx_cluster_data_tb.v`, bench `cluster_data`. Nothing in the
machine sends `CU_DATA` yet, so the bench is the sender: it drives flits at the
CU's NoC local and reads the drain's memory writes back off it.

* **`buf` 0/1** — the same GEMM the other benches run, with the operands
  arriving as `CU_DATA` instead of as MAG responses. Graded against the FP64
  model at the tile's peak, which is how `mx_cluster_node_tb` grades.
* **`buf` 2** — against an **exactly zero** tile, so no float model is needed:
  zero plus 1.0 is 1.0, and FP16 1.0 is `0x3C00` or the sub-tile did not reach
  the accumulator, or reached the wrong address.
* **the two banks** (§4.6) — the lower one gets zeros and the upper the real
  operands, so one model catches both directions. A bank bit lost on the read
  path sweeps zeros; lost on the write path, the lower bank is not zero any
  more. Run at `GA = GB = 512`, the only shape where the bit is not optimised
  away.
* **cu→cu, out and back** (§10.2) — the cluster sends its resident tile to
  itself as `buf` 2 and the bench hands it back, so `OP_ADD_PEER` adds the tile
  to itself and the answer must be exactly **2T**. That is `OP_SEND`, the
  two-granule split, the descriptor build, the receive demux, `OP_ADD_PEER` and
  the completion signal in one check, and 2T needs no model of the
  accumulator's float — only the tile the ordinary path already produced.

### 9.6 What it costs

`mx_cluster_cu`, out of context at 300 MHz on `xcvu13p-fhgb2104-2L-e`, in the
bench defaults (`TILES = 256`, `GA = GB = 32`, `L1_PRIM = distributed`) so the
steps are comparable — the production shape is below.

| | LUT | FF | BRAM | Fmax |
|---|---|---|---|---|
| before, peer grounded | 14,808 | 17,385 | 9 | 346.6 MHz |
| + `buf` 2, peer reachable | 15,802 | 18,833 | 9 | 345.6 MHz |
| + `buf` 0/1 into L1 | 16,107 | 18,831 | 9 | 345.6 MHz |
| + banking, `OP_SEND`, signals, rejection | 17,351 | 20,159 | 9 | 343.8 MHz |
| **total** | **+2,543** | **+2,774** | — | **−2.8** |

**The largest single item is not the receiver.** Grounding `peer_in` let
synthesis prune `mx_acu_fp`'s four 352-bit peer pipeline registers and the
352-bit mux in front of the align stage — logic paid for in the source and
absent from the netlist. That is the first 994 LUT and 1,448 FF.

Reaching L1 costs **305 LUT and no registers**, because the two sources share
one placement block (§9.3). A second copy of the permutation would have been
about three times the whole feature.

**In the shape that ships** — `TILES = 512`, `GA = GB = 512`, `L1_PRIM = block`,
which banking is what makes expressible at all:

| LUT | FF | BRAM | DSP | Fmax |
|---|---|---|---|---|
| 16,390 | 18,404 | 35 | 304 | 344.3 MHz |

The ack destination (§9.2) is 22 LUT and 32 FF of that, and does not move the
frequency: it lands on `sg_flit`, which is registered and idle.

Fewer LUTs than the default despite four times the L1, because block RAM is
where 928 bits x 512 belongs: 13 RAMB36 per port, 26 for the two, 5 for the
resident tile and 4 for the receive queue.

> **The critical path moved, and it was worth catching.** `!peer_open` in the
> sequencer's guard put an 8-bit compare behind `drain_busy` on
> `d_out -> the state machine's clock enable`, which took the CU from 345.6 to
> **324.2 MHz** — past the accumulator's own `val_r -> b_plo/b_phi`, which had
> owned every worst path until then. `peer_open` is a level held for a whole
> stream, so registering it gives the cycle back for free: 343.8 MHz, and the
> same LUT count to within five.
>
> It is still the binding path, at 0.43 ns of slack against a 3.33 ns target.
> The accumulator is now second.

## 10. Where a `DRAIN` sends its results

A drain writes to memory, or straight into another unit's buffer. The
destination rides in the `CU_INST` payload's tail (§2), so a `DRAIN` written
before these bits existed — all-zero there — still means "to memory":

| bits | field | meaning |
|---|---|---|
| `[111]` | `dnode` | `0` memory, as before; `1` a NoC node |
| `[110:107]` | `dst_x` | destination node, `dnode = 1` only |
| `[106:103]` | `dst_y` | |
| `[102:95]` | `buf` | the destination's buffer id — §9.3 |
| `[94:87]` | `dflags` | copied into the descriptor's `flags` byte |
| `[86:83]` | `dack_y` | where the receiver sends its completion — §9.2 |
| `[82:79]` | `dack_x` | `0` meaning back to this cluster |

`addr` keeps its meaning either way: the destination base as a **byte address**.
A node-addressed drain sends `addr[20:5]` as the descriptor's granule `off`,
which is the arithmetic the memory path already does — sub-tile `t` goes to
`addr + t*32`, so granule `addr/32 + t`. One field, computed once by the
driver, and the hardware shifts.

The burst structure is unchanged: `WBURST = 8` granules become one descriptor
with `len = 7` and 8 data flits, exactly as they become one `MEM_WR_REQ` with
`len = 7` and 8 `MEM_WR_DATA` flits.

Only the **semantics** are shared with the vector core, not the bit positions —
the two instruction words are different shapes. What must agree is the field
set `{dnode, dst_x, dst_y, buf, dflags}`, `addr` as a byte address with
`[20:5]` as the granule offset, and the `buf` namespace.

### 10.1 `buf = 2` is a different drain, not a different header

A cluster draining into another cluster's **L1 does not typecheck**: a drain
emits FP16 sub-tiles and `buf` 0/1 take int7+E5M3 entries (§9.3). Cluster to
cluster is `buf = 2`, and that is the only version worth having — a K-split
through memory would round to FP16 in between, which is exactly the precision
the peer path exists to preserve.

So **`buf` selects the accumulator's opcode**, not just the flit header:

| `buf` | opcode | what leaves |
|---|---|---|
| 0, 1, or memory | `OP_EMIT` | one 256-bit FP16 sub-tile per granule |
| `2` | `OP_SEND` | the accumulator's own float, two granules per sub-tile |

One field, and no second bit that can disagree with it.

A send result is `PW` bits against a 256-bit path, so it becomes two granules —
the low half on the cycle it arrives and the high half behind it. Widening the
drain queue and the burst buffer to `PW` instead would have cost 96 bits × 16
slots to carry a mode they are not in most of the time. It is also why **a send
drain issues every other cycle**: two granules per command against one flit per
cycle on the link, so the rates already match, and `mx_cluster_node` reports it
if two send results ever collide.

`d_iss` therefore counts sub-tiles while `d_got`/`d_pop`/`d_out` count granules.
`drain_idx` is a granule index in both modes, which is what lets the write port
stay unaware of the difference.

> **A fused emit cannot be a peer send.** `GEMM.emit` issues `OP_ADD_EMIT`,
> which produces FP16. A fused drain to a *node* works for `buf` 0/1 — the
> destination is latched from whichever instruction set `w_base`, so an emitting
> `GEMM` carries it the same way it carries the address (§5.1) — but `buf = 2`
> needs an issuing `DRAIN`.

### 10.2 A self-addressed burst deadlocks, by construction

A cluster sending `buf = 2` **to itself**, with the flits looped back live, will
hang — and the hang is real rather than an artefact of testing. The receive path
cannot accumulate a peer sub-tile until the send drain has retired (§9.4), so
holding the flits backpressures the very drain that is trying to retire.

Nothing needs that configuration; peer exists to span clusters. But
`mx_cluster_data_tb` does run the round trip, by capturing the burst and
replaying it once the drain has finished — store and forward, which tests both
halves against each other without the cycle.
