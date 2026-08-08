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

## 1. One cluster, two ports

```
   NoC <-> manager <-> tcu -> tcu -> tcu -> tcu -> acu <-> NoC
            L1           direct DSP cascade        resident tile
```

Port 0 (manager) carries instructions in, operand fetches out, and completion
signals. Port 1 (accumulator) carries result write-back.

Two ports, not five. The chain eats eight 256-bit operand words per cycle and a
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
| `[114:0]` | — | | unused |

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

### 4.6 `aoff`, `boff`, `eoff` — L1 is addressable

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

## 5. `DRAIN addr, n, anchor, fuse, last`

Get `n` resident sub-tiles into memory as FP16, starting at byte `addr`.
Sub-tile `t` goes to `addr + t*32` — one 256-bit word per 4x4 sub-tile, in the
manager's sweep order, row group major.

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
