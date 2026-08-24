---
title: KohakuTPU's instruction sets
summary: One worked example of spending the framework's instruction payload — three cluster opcodes, an accumulator command set, and a 32-bit vector word — and why each level is narrower than the one above it.
tags:
  - kohakutpu
  - isa
  - instructions
---

# KohakuTPU's instruction sets

The framework delivers an instruction to a compute unit and carries a completion
back. **What the bits mean is the project's to decide**, and this page is one
worked example of deciding it.

KohakuTPU spends them three ways, at three different scales:

| | what it expresses | width |
|---|---|---|
| **cluster** | fill, sweep, drain | one 256-bit payload, three opcodes |
| **accumulator** | load, add, emit | 3 bits plus a tile address, never on the mesh |
| **vector core** | a kernel: loops, predicates, addressing | 32-bit words, 8 per payload |

Read this as evidence about the shape of the problem, not as a specification of
the framework. A different project will spend the same bits completely
differently, and the only thing it inherits is the envelope. The normative
envelope is [spec/instruction-encoding.md](../../spec/instruction-encoding.md);
the guidance drawn from this example is
[integrate/instruction-set.md](../../integrate/instruction-set.md).

**What is fixed and what is this project's**, since the two are easy to conflate
on a page like this:

| | |
|---|---|
| **fixed protocol** | that an instruction arrives as a payload with a header, that a completion signal returns, that a completion is what refills dispatch credit, and that the `last` bit distinguishes the end of a batch |
| **yours** | every field inside the payload, how many opcodes there are, what they mean, and whether one instruction is a macro-op or a word of a program |

The strongest evidence for the second row is inside this project: **the same four
envelope bits mean different things at a cluster and at a vector core**, resolved
only by which node the flit was addressed to (§7.1). Two units on one framework
did not converge on one instruction set, and nothing asked them to.

---

## 1. Five levels, each narrower than the one above

The host writes a **control program** into the orchestrator: a list of writes and
polls with no branches, the whole point being that the host is not in the loop
once it writes `GO`. Those writes land in the **dispatch agent's** registers,
which name a staged program and a destination node and kick it. The agent streams
**cluster instruction flits** into the mesh, three opcodes a cluster executes one
at a time. Each `GEMM` expands into hundreds of **accumulator commands** against
the resident output tile. Underneath all of it, operand and result movement is
the **memory protocol** between a unit and the memory agent.

**None of them can express the level below, and that is deliberate.** The control
program cannot loop, so the driver unrolls. The cluster cannot address DRAM
affinely, so the driver computes addresses. The accumulator has no addressing at
all, so the manager sweeps.

The vector core breaks the pattern on purpose. The five above are all
straight-line because each describes a fixed dataflow; a vector core runs
*kernels*, which are written rather than generated, so it is the only one with a
loop and a predicate. **It is a sixth consumer on the mesh, not a sixth layer** —
nothing above it changes.

The ratio is the justification for the layering. In the worked example in §7, one
`GEMM` flit becomes 256 accumulator commands, one `FILL` flit becomes 128
response flits, and four flits become a whole matmul. **Every level exists to
stop the level above it from having to say the same thing 256 times.**

---

## 2. The cluster's three opcodes

```
   FILL  addr, n, sel            load n L1 entries from memory
   GEMM  gm, gn, nk, anchor, acc sweep gm x gn output sub-tiles over nk K blocks
   DRAIN addr, n, fuse, last     get n resident sub-tiles out
```

One 256-bit payload each, with the opcode in `[255:252]`. Two things about the
layout are worth more than the field list:

**No field's meaning depends on the opcode.** When the entry count had to grow
from 8 bits to 16 — because the resident tile reached 512 sub-tiles and a `DRAIN`
naming 512 wrapped to 0, silently draining the beginning of the tile a second
time — every field below it moved **down** rather than sharing bits with `gm`/`gn`
on the grounds that a `FILL` never uses those. A field whose meaning depends on
the opcode is how a decode bug survives review, and the payload has 87 spare bits
and no reason to overlap anything.

**Field widths matter more than they look.** An unsized value in the wrong place
shifts every field below it and elaborates cleanly: an expression written
straight into a concatenation contributes 32 bits rather than the 34 the address
field is, the whole payload came out four bits short, and the cluster read
nonsense and executed nothing — no memory traffic, no error anywhere. Addresses
are checked for `x` at the producer in simulation for the same class of reason:
an `x` in an address is invisible downstream and fatal, because memory returns
`x`, the quantiser packs it, the accumulator sums it, and the drained tile is a
plausible-looking zero.

### 2.1 One port, not five

The cascade eats eight 256-bit operand words per cycle and a mesh port delivers
one, so feeding the compute units directly from the mesh is an 8x deficit no
matter how many ports are spent on it. **Reuse closes the gap instead** — the
resident tile makes the demand `4(Gm+Gn)/(Gm·Gn)` words per cycle, 0.375 at 16x32
([accumulator.md](accumulator.md) §1).

A cluster was two endpoints until the second one was measured to buy nothing: the
link is full duplex and the two ends loaded opposite directions of it. What it
cost was a router local, and eight clusters at two locals each force a 4x4 mesh
where one local each fits 2x4 — eight extra routers. Merging them risked
head-of-line blocking on the shared outbound queue, which was measured and did
not appear: the CU is never blocked sending.

Outbound, the fetch descriptor and the drain share one queue and **the fetch
descriptor wins**. A `FILL` is a single flit the sequencer is blocked on, so
delaying it costs a whole memory round trip, while a drain flit is bulk traffic
behind arithmetic that has already finished and waits at most one flit.

---

## 3. `FILL` — one descriptor, not a loop

Load `n` L1 entries starting at a byte address into the A side or the B side. One
L1 entry is 4 lanes x 32 K elements: 256 bytes as FP16 source, 128 bytes once
quantised, and 928 bits in L1 — 128 int7 elements plus four E5M3 scales.

**A `FILL` is one memory flit, not `n` of them.** It carries `{base, count}` with
a streaming flag and the memory agent walks the address sequence itself,
returning `count x 4` responses each tagged `{entry, word}`. The compute unit has
no requester and no receive cursor: a response says which L1 slot it belongs to
and which quarter of it, **so arrival order carries no meaning**.

That tagging is what makes everything else expressible. Previously a response was
identified only by the order it turned up in, so a second outstanding read had
nowhere to be named — an earlier attempt to keep several reads in flight could
not have worked at any depth. It was not a tuning failure; the field did not
exist.

`FILL` is one state in the sequencer, not three. It used to be
request → receive → write per entry, which is the shape a per-entry requester
forces; **the descriptor removed the requester rather than pipelining it.**

Three fields on it are worth naming:

- **`preq` is RESERVED** and the cluster no longer decodes it. It used to say the
  operand was already int7+E5M3 so the quantiser would not be involved; now that
  is the only case there is — a fetch is never transformed, so every `FILL` reads
  4 words per entry and no request can ask for anything else
  ([number-format.md](number-format.md) §5.1). Memory still holds no map of which
  addresses are which format: the driver states the format by *scheduling the
  conversion*, not by flagging the read.
- **`eoff`** is which L1 entry the run starts at. It costs nothing to carry: it
  rides in the memory request's transaction field, which memory echoes back
  alongside each entry's position in the run, so the response names the exact
  slot and the receiver still needs no cursor.
- **`peers`** names up to three other clusters reading the same bytes at the same
  moment; the lowest node index issues the descriptor and the rest issue nothing,
  so memory reads the operand once and runs the quantiser over it once however
  many clusters want it. **It is decoded and the driver does not set it.** A
  follower cannot tell *which* fill an arriving entry belongs to, so a shared A
  entry reaching a cluster still executing its `FILL B` lands in the B side of
  L1. Measured at the 256-cube it raised the rate and destroyed the answer
  ([results.md](results.md) §8.3). It needs a rendezvous, which is not built.

**Capacity wraps silently.** The write address truncates, so `n` beyond L1's
capacity overwrites entries from 0 with no error and the sweep then reads
whatever survived. That is one of several silent-wrap limits the driver has to
respect rather than discover ([compiler.md](compiler.md) §2).

---

## 4. `GEMM` — the sweep

```
   for kb in 0 .. nk-1:            <- K OUTERMOST
     for g in 0 .. gm-1:
       for h in 0 .. gn-1:
         chain <- L1A[g*nk + kb], L1B[h*nk + kb]
         acu   <- (kb == 0 && !acc) ? LOAD : ADD   at tile address g*gn + h
```

### 4.1 Why K is outermost

An output sub-tile address recurs every `gm*gn` cycles instead of every cycle.
**That is what lets the accumulator be a plain memory with a synchronous read.**
With K inner, back-to-back same-address accumulation cannot close a pipelined
adder loop at all, and the previous design paid for it with three rotating banks
plus a fold on emit.

It is a loop-order choice with an architectural consequence — it deletes the
banks, the fold, the zero mask and three quarters of the tile memory
([accumulator.md](accumulator.md) §4). What it costs in exchange is that the
caller now owes a pacing contract that used to be structural.

> Two different loops both mention K and they nest the opposite way. **Across
> chunks** the driver puts K innermost, so the output tile stays resident. **Within
> one `GEMM`** the K-block sweep is outermost over sub-tiles, so a tile address
> recurs every `gm*gn` cycles. They are different levels and both are
> load-bearing.

### 4.2 The `acc` bit

`acc` is what makes the first K block of a sweep an `ADD` rather than a `LOAD`.
Without it every `GEMM` starts by overwriting the resident tile, so **an output
tile could only ever be produced by one instruction**, and a K longer than L1
could not be expressed at all — the only way to compute it would be to drain a
partial tile to memory per chunk and read it back.

With it, K is split into chunks that chain into the same resident tile, and the
tile is written to memory once rather than once per chunk. That is the difference
between `M*N` of result traffic and `M*N*(K/Kc)` in both directions; at K=4096
with a 128-element chunk it is 32x.

### 4.3 `anchor`

A constant of the number format rather than a tunable — it cancels the bias
stored in both operands' scale exponents
([number-format.md](number-format.md) §4). It is a field only because the
accumulator has no other way to be told which bias convention its operands were
stored under.

`DRAIN` carries the same field and it is **dead**: during a drain the cluster
forces the anchor and both scales to zero, because emit reads the tile and
converts without applying any scale. Decoded and discarded.

### 4.4 Reuse pacing

The accumulator requires 5 cycles between two commands to the same tile address.
K outermost gives `gm*gn` cycles by construction, so any tiling with
`gm*gn >= 5` needs nothing; below that the manager inserts idle cycles (4 at
`gm*gn = 1`, 2 at 2, 1 at 3 or 4). `nk <= 1` never paces at all, because one K
block never revisits an address.

The idle cycles cost nothing at any tiling worth running. A tiling smaller than
the contract that did *not* pace would accumulate onto a stale tile, silently,
and only for part of the K sweep.

### 4.5 It retires when the sweep *starts*

The instruction is done with the sequencer the moment the manager takes it. The
sweep runs on for `gm*gn*nk` cycles afterwards and needs nothing more from the
CU, so holding the instruction there only stopped the CU from doing the one thing
that would overlap with it — filling the other half of L1. Measured, `FILL` was
22.3% of the machine's time with the array idle through every cycle of it.

What *is* finished is a separate question, and getting it wrong was expensive
twice:

- **Not "the last tile has been issued."** The cascade is ~19 cycles deep, so
  when the counters finish there are still that many results in flight. Reporting
  done there let `DRAIN` seize the accumulator's control mux and cut them off —
  the tail sub-tiles came back as zeros.
- **Not the command FIFO's empty flag.** That flag deasserts two cycles after a
  push, so there is a hole where the block reads idle with commands still queued.
  Only a tiling short enough to finish inside that hole can hit it, which is why
  every bench down to 3 sub-tiles passed and a 2-sub-tile one did not.

Counting issued-minus-retired is exact and owes nothing to FIFO timing.

### 4.6 L1 is addressable and banked, and the offset is the real ceiling

`aoff` and `boff` say where a sweep reads; `eoff` says where a fill lands. They
buy two different things:

- **Double buffering.** Consecutive K chunks go to alternate halves of L1 A, so
  the fill for chunk *i+1* runs while chunk *i* is still being swept. Two banks
  is exactly enough, because a sweep waits for the one before it and only two
  chunks are ever live.
- **Residency.** B does not change across the m loop, and re-filling it per
  m-tile was a quarter of all memory traffic at the 256-cube — 4,096 beats of
  16,384. If every K chunk of B fits at once, the driver fills it once per column
  band and walks m over it in place. That is also why the driver iterates the
  column band **outermost**: with m outside, B for the next band would overwrite
  the current one before the m loop came back to it.

**The offsets are 8-bit fields, and that is a ceiling independent of capacity.**
L1 is two banks of 256 rather than one flat 512; a bank bit picks the half and
the address is bank concatenated with offset. So a chunk may not exceed 256
entries whatever L1 holds — and this is the ceiling the driver got wrong for a
session, planning 288-entry B chunks whose last offset wrapped onto entry 0, so
every sub-tile past the wrap multiplied *another K block's* B. It was measured on
the card as a worst element of 8.23e+02 where the shape one step smaller was
exactly right ([results.md](results.md) §9.2).

Two properties of that fix are worth carrying:

- **Truncation makes the bank bit free in both directions.** At 256 entries per
  side the bank bit falls off the top and the address is what it always was, so
  zero is the lower half and every instruction written before the bits existed
  still addresses exactly what it did. The alternative — widening the offset to 9
  bits — moves every field below it.
- **The offset cannot carry into the bank.** A sweep that overruns its region
  wraps inside its own half rather than walking into the other bank's operands.

> **Two banks is the ceiling**, and a deeper L1 needs a second bank bit, which
> the manager reports at elaboration rather than silently dropping.

---

## 5. `DRAIN` — and where results actually go

Get `n` resident sub-tiles into memory as FP16, one 256-bit word per 4x4
sub-tile, in the manager's sweep order. Or into another unit rather than into
memory.

### 5.1 Fused: the sweep hands them out

A sub-tile's **last accumulation already computes its finished value** at stage 5
of the accumulator. A separate `DRAIN` reads the same address back and passes it
through the same pipeline to recover it — and that second pass needs one
accumulator command per sub-tile, which a sweep has none spare of, because it
issues a command every cycle. So a drain could never overlap a sweep, and it was
24% of the machine's time.

`GEMM.emit` fuses them. Every issue of the sweep's **last** K block becomes an
add-and-emit: it writes the tile back *and* hands the value out, same command,
same cycle, no re-read. `DRAIN.fuse` then means "these already left; wait for
them", and it does not wait for the sweep — so one tile's results can still be
draining while the next tile's sweep runs.

What replaces the command slots is buffering and backpressure. The last K block
completes one sub-tile per cycle while memory retires a burst in about eleven, so
the queue carries the burst until the gap after it; past that the **sweep** is
held, because once a command is in the accumulator its result arrives ~19 cycles
later whatever happens downstream.

> There is no load-and-emit opcode, so an emitting sweep must not also be the one
> that opens the tile. It never is in practice — the emitting sweep is the last K
> chunk — and only a single chunk of a single K block collides, where the driver
> falls back to an issuing `DRAIN` and the manager reports the collision if it
> ever happens.

### 5.2 Bursts, and why one transaction per sub-tile does not fit

Emitted sub-tiles are collected into bursts of 8 and written as one descriptor
plus 8 data flits. Memory retires one single-beat write per visit to its idle
state — about 4 cycles — while two clusters can produce a pair every cycle
between them, so pipelining the drain without bursting just wedges the memory
port's write intake queue. A burst size of 1 reduces exactly to the old
behaviour, which is what makes it safe to turn down.

Two rules that are easy to get wrong: **the burst must be closed when the drain
runs dry**, not only when it is full, or a tile whose count is not a multiple of
the burst leaves its tail in the buffer forever; and **a `DRAIN` is finished when
the last write has left the unit**, not when the accumulator stops producing, or
the completion signal runs ahead of the memory traffic it stands for.

The drain sequencer also does not count cycles. It used to count 10 after issuing
an emit, and deepening the accumulator pipeline made 10 too few, so it wrote a
zero result while every unit test still passed.

### 5.3 Into another unit: bulk transfer between endpoints

A cluster's operands do not have to come from memory and its results do not have
to go there. A separate flit type carries a bulk transfer between two mesh
endpoints: one descriptor flit, then `len+1` pure data flits, the last carrying
the header's `last` bit. The offset counts 32-byte granules and advances by one
per data flit, so a stream is a run and the receiver needs no cursor and no
per-buffer state beyond the one it is filling. Data flits carry payload only —
nothing that has to agree with the descriptor, which is what keeps the receiver
from having two opinions about where a flit goes.

A cluster exposes three buffers:

| buffer | destination | granule |
|---|---|---|
| 0 | L1 A | one of an entry's 4 words |
| 1 | L1 B | likewise |
| 2 | the resident output tile, through `ADD_PEER` | half a sub-tile |

Buffers 0 and 1 are **byte-identical to a memory response payload**, so a sender
into L1 is substituting for the memory agent and owes the same format — literally
the same permutation, because the two sources meet at one placement block and
differ only in where the entry and word indices come from. Building the 928-bit
unrolled write twice would have been ~900 LUT of duplicate, and the copy that no
bench exercised would have been the one that drifted.

**Buffer 2 selects the accumulator's opcode, not just the flit header.** A
cluster draining into another cluster's L1 does not typecheck — a drain emits
FP16 and L1 takes int7+E5M3 — so cluster-to-cluster is buffer 2, and that is the
only version worth having, because a K-split through memory would round to FP16
in between. A send result is wider than a 256-bit path, so it becomes two
granules and a send drain issues every other cycle; the rates already match.

Four properties make it trustworthy, and each one is a bug that was closed:

- **A burst is rejected, never wrapped.** The descriptor is range-checked against
  the named buffer, an unknown buffer id fails it, and a peer burst starting on
  an odd granule fails it too because a sub-tile is a pair. A burst that ran off
  the end would silently overwrite the bottom of the buffer, which is the
  quietest possible corruption.
- **A rejected burst is counted out and then dropped**, not skipped: the receiver
  still consumes every data flit, because otherwise the next data flit is read as
  a descriptor and one bad burst desynchronises every burst after it. Dropping
  rather than holding is forced — an unconsumed flit fills the receive FIFO and
  wedges the instruction stream behind it.
- **A rejected burst is still acknowledged.** A signal that only arrives when the
  data was good is a signal the sender cannot wait on.
- **The acknowledgement has its own destination field.** Zero means the
  descriptor's source, which is what every host-originated burst uses. A
  unit-to-unit transfer's completion is otherwise unobservable: the source is the
  *sending* unit and no compute unit consumes an ack, so nothing could sequence a
  reader behind a writer. The registers for it are separate from the sender's,
  because sharing them passes every test *except* the case the field exists for.

**One open stream per receiver.** The mesh interleaves and a receiver holds
exactly one `{buf, off, left}`, so a second sender's descriptor would be consumed
as the first sender's data. There is no arbitration — the driver owns it, the
same way it owns L1 banking — and it is *detected*: the header's `last` bit stops
agreeing with the descriptor's own length.

Three contracts on peer accumulation specifically, none of them structural: the
receiving tile must already be open, because the peer op reads the address it
writes and the tile memory has no reset; a peer stream and a sweep may not
overlap, tested once at the head of the stream rather than per sub-tile; and the
reuse contract still applies within a stream.

> **A self-addressed peer burst deadlocks by construction**, and the hang is real
> rather than an artefact of testing: the receive path cannot accumulate until
> the send drain has retired, so holding the flits backpressures the very drain
> that is trying to retire. Nothing needs that configuration — peer exists to
> span clusters — and the bench tests the round trip by capturing the burst and
> replaying it after the drain finishes.

---

## 6. The accumulator's command set

Three bits and a tile address, presented with a valid strobe. It never appears on
the mesh; the manager issues it once per cycle of a sweep.

| value | name | built | effect |
|---|---|---|---|
| 0 | `NOP` | yes | nothing |
| 1 | `LOAD` | yes | `tile[addr] = chain` |
| 2 | `ADD` | yes | `tile[addr] += chain` |
| 3 | `ADD_PEER` | yes | `tile[addr] += peer_in` |
| 4 | `SEND` | yes | `peer_out = tile[addr]` |
| 5 | `EMIT` | yes | `emit_out = fp16(tile[addr])` |
| 6 | `FWD` | **no** | `peer_out = chain` |
| 7 | `ADD_EMIT` | yes | `tile[addr] += chain`, **and** emit the result |

Two sources drive the port and they are muxed by an **explicit** drain rather
than arbitrated: `DRAIN` takes the control port the cycle it starts, which is why
"the sweep is busy" has to cover the whole in-flight cascade (§4.5).

`ADD_EMIT` removes the need for a command slot rather than finding one (§5.1),
and in the pipeline it is `ADD`'s operand selects with `EMIT`'s output — one term
in three expressions and nothing at all in stage 3, the path that closes timing.
The explicit `EMIT` stays, because an output tile drained without a completing
sweep still needs it.

`ADD_PEER` and `SEND` were dead code that compiled for a long time — the
accumulator implemented them and the node above tied `peer_in` to zero and left
`peer_out` open, so `SEND` drove nothing and `ADD_PEER` added zero. `FWD` still
has no command source, so the *direct* accumulator-to-accumulator chain it exists
for is unbuilt: a send leaves through the drain queue as two granules, not on a
dedicated wire.

---

## 7. The vector core's instruction set

Thirty-two opcodes in a 32-bit word — the sixth instruction set and the first
that can branch. Eight instructions fit one mesh payload, so a 128-instruction
kernel is 16 flits against 5 to 97 for a single GEMM pass.

**Every arithmetic instruction has the same shape, because every one of them is
the same FMA underneath.** Three independent source selectors is the whole
flexibility budget:

```
  sa/sb/sc   00 V  vector register    01 S  scalar, broadcast
             10 C  CHAIN, the previous stage's result
             11 K  constant register

  pm         00 unpredicated   01 where P[pr] set   10 where P[pr] clear
```

so one opcode covers many operations: an FMA with `sb = K(1.0)`, `sc = K(0.0)` is
a move; with `sb = S` it is a scale; with `sc = C` it is a chain step.

Loads and stores need an address rather than three operands, so they overlay the
same word with a dtype field, an address descriptor id and a signed offset. **The
cast is a field on the store**, not an instruction — which is why the compiler
folds a convert into its consumer at no cost.

The architectural state is sixteen vector registers of up to 128 elements striped
across the lanes, sixteen broadcast scalars, four predicate registers, four
constant registers, eight address descriptors, plus the active vector length and
the lane topology. **Vector length is a register, not a constant**, so one kernel
body handles a tail without a second code path.

Three things in the opcode list are worth singling out:

- **The four transcendentals are one pass.** That is the single most consequential
  line in the set: on a GPU they are quarter-rate, and softmax, normalisation and
  every activation are transcendental-bound rather than FMA-bound.
- **The reduction's kind field includes `SUMSQ` and `DOT`**, both free from the
  tree node being an FMA rather than an adder.
- **One reduction kind keeps its elementwise result** — the exp-and-sum used by
  softmax writes back per element *and* reduces, in one pass, with the vector
  destination riding in a field a unary leaf leaves free. It is capability-gated:
  a core without it **faults** rather than mis-executing.

Deliberately absent: integer arithmetic beyond dtype conversion, bitwise ops,
scatter/gather with computed indices, and any data-dependent branch other than a
loop counter ([vector-core.md](vector-core.md) §9).

### 7.1 A vector instruction is not a flit

This is the structural difference from the cluster and the reason the two
encoders in [compiler.md](compiler.md) §4 have not merged. The 32-bit word is
**cargo**: it rides inside an envelope that says "write this into instruction
memory at this address". A kernel is therefore *a program, not an instruction* —
N writes to instruction memory, M descriptor writes, then one run.

**And the envelope opcode space is shared.** The same four bits mean
instruction-memory/descriptor/run at a vector node and fill/sweep/drain at a
cluster node, resolved only by the destination. So an encoder cannot be
node-agnostic, and a mistake there is silently executed rather than rejected.

---

## 8. One pass, end to end

`C[32,32] = A[32,128] @ B.T[32,128]` on the cluster at `(x=1, y=1)`. Deliberately
small — the tile is small because the *problem* is, since the compiler's padding
discount picks the shape that wastes least on a 32x32 output.

**The host stages four flits** straight into the staging RAM (20 writes), then
loads nine control commands (36 writes), then writes one control register. **57
host writes in total**, after which the host only polls until done.

| slot | op | fields |
|---|---|---|
| 0 | `FILL` | `addr = 0x0000`, `n = 32`, `sel = 0` (A) |
| 1 | `FILL` | `addr = 0x2000`, `n = 32`, `sel = 1` (B) |
| 2 | `GEMM` | `gm = 8`, `gn = 8`, `nk = 4`, `anchor = 40`, `acc = 0` |
| 3 | `DRAIN` | `addr = 0x4000`, `n = 64`, `last = 1` |

The agent reads 20 words out of staging, assembles four flits, rewrites each
header with the destination and its own coordinates, and pushes them into the
mesh. Each push costs one dispatch credit, returned on completion.

Then:

- **`FILL A`** is *one* request flit naming base and count 32. Per entry, one
  8-beat burst of 256 bytes of FP16 through the quantiser, returning **4**
  response flits of 32 int7 elements plus 4 scales, each tagged with its entry and
  word. 8,192 bytes read; 32 entries written; 128 responses.
- **`FILL B`** is the same with the B slot ordering.
- **`GEMM`** sweeps `for kb in 0..3: for g in 0..7: for h in 0..7`, issuing 256
  accumulator commands. Because `acc = 0`, the 64 with `kb = 0` are `LOAD` and the
  remaining 192 are `ADD`. `gm*gn = 64` is well above the pacing threshold, so no
  idle cycles are inserted.
- **`DRAIN`** issues 64 emits, collected into 8 bursts of 8 — **72 flits, not
  128** — each reassembled by memory into a single 8-beat write. 2,048 bytes of
  C, written once.

| | count |
|---|---|
| host writes | 57 |
| cluster instruction flits | 4 |
| memory read requests | 2 — one streaming descriptor per `FILL` |
| memory read responses | 256 |
| write request + data flits | 72 |
| completion signals | 4 |
| accumulator commands | 320 (64 `LOAD`, 192 `ADD`, 64 `EMIT`) |

Each retiring instruction sends a completion signal; the `DRAIN` carries `last`,
so it retires as a batch completion instead of an instruction completion. That
distinction has two consequences the driver must handle: the completion counter
counts every signal type regardless of code, so the expected total is the flit
count; and dispatch credit is returned only for instruction completions, **so the
`last` flit costs a credit permanently** ([compiler.md](compiler.md) §3).

---

## 9. What was designed and is not implemented

A larger cluster ISA was agreed before building: eight opcodes, an N-dimensional
affine tensor-descriptor file with per-dimension `(count, stride, axis, astep)`
and per-axis bounds, a hardware loop, and a sync. Under it a **convolution is a
matmul with a more interesting descriptor** — `M = N·OH·OW`, `K = KH·KW·C`, with
the padding expressed as a negative axis base and an extent, and an
out-of-bounds index delivering zeros and issuing no memory request.

**The descriptor walker is built and its im2col addressing is validated. It is
not wired into the fill engine.** What runs is the three opcodes above, with the
addressing a descriptor would have generated computed by the driver instead.

The trade is worth stating exactly, because it is a live decision rather than a
gap: the driver can compute any address a descriptor could, so nothing is
*unreachable* today — but it must unroll, which is what makes a program grow with
the problem and forces the round-cutting in [compiler.md](compiler.md) §3. A
descriptor moves that loop into hardware. It is the right next step for
convolution; it is not needed for GEMM, which is why it has not happened.

An earlier design went further still and was abandoned: seven instruction types
with nested-loop flags, buffer choices and per-instruction start addresses. **The
reason it collapsed from seven types to three is the most transferable thing in
this project's ISA history.** That set put the loop structure *inside* one
instruction, so the controller had to be able to express every access pattern the
hardware might ever want. The design that replaced it moves the loop up a level:
the manager sweeps a resident output tile, the driver computes the addresses, and
an instruction only says how big the sweep is. One `GEMM` flit expands into
hundreds of accumulator commands without carrying any of them.
