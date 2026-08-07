# What each change makes the hardware do differently

For every proposed change: the signal-level behaviour before, the behaviour
after, and the property that makes the second faster. No arithmetic that does
not follow from a behaviour change.

> **Where things actually stand is [`perf.md`](perf.md) §0,** not here. This is
> the design record — the signal-level argument for each change. At the
> 256-cube on 2 clusters, both operands pre-quantised: **538.3 GFLOP/s**, 87.6%
> of peak, from a 42.0 baseline — 12.8x. Every shape and cluster count is in
> `perf.md` §0, the verdict on every lever is in
> [`optimization.md`](optimization.md), and `.plans/optimization-log.md` is the
> internal running record of what each change measured.
>
> Changes below that have landed, with their measured effect: tagging and the
> streaming descriptor (§1, §2), MAG's FSM split and emit buffer (§3), the
> tile at `TILES = 512` (§4), burst `DRAIN` writes. Since then, and not
> described here: pre-quantised operands with quantise-on-upload, addressable
> L1 (banked A, resident B, `GEMM` retiring on issue), the fused drain
> (`OP_ADD_EMIT`), and **several MAG ports, one per mesh row**. The
> first three are written up in `docs/isa/memory.md` §6 and
> `docs/isa/cluster.md` §4.5–4.7 and §5.1; the last is
> [`mas/spec.md`](mas/spec.md) §2.4.
>
> **The rates quoted here predate the mesh layout change** — a cluster is now a
> column of a band rather than a (row, left-column) pair
> ([`system.md`](system.md) §2.3). None of the signal-level arguments below
> depend on where a cluster sits on the mesh, so they are unaffected; the
> numbers are figures for the previous topology. The new layout passes at 2 and
> 8 CU and costs about three points of peak at 8 CU, in routing rather than in
> anything described here ([`perf.md`](perf.md) §0.1).

---

## 1. Tagging — turns MAG from an FSM into a pipeline

### Before

`mag.v` holds **one** return context for the whole module:

```verilog
    rq_x, rq_y, rq_txn          // who asked, latched when the read is taken
```

and gates intake on being idle:

```verilog
    wire take_rd = in_valid && (in_ty == T_MEM_RD_REQ) && (st == S_IDLE);
```

So the sequence for every read is forced:

```
   take_rd ──> S_Q_FILL ──> S_Q_WAIT ──> S_Q_EMIT ──> S_IDLE ──> take_rd
               ^                                                   |
               +-------- no second read may enter this window -----+
```

`rq_*` is a single register set. A second read entering would overwrite the
first requester's identity, so the FSM structurally *cannot* admit one. The AXI
read channel sits idle through all of `S_Q_EMIT`, and the NoC out channel sits
idle through all of `S_Q_FILL`, because one shared piece of state serialises
two independent channels.

### After

The requester's identity travels **with the data** instead of being held in a
register:

```
   request  {dst, txn, base, count}
   response {dst, txn, entry_idx, word_idx, payload}
                     ^^^^^^^^^^^^^^^^^^^^^
                     enough to place the flit with no state anywhere
```

`take_rd` drops its `st == S_IDLE` condition. MAG stops being a state machine
over *requests* and becomes a pipeline over *beats*: read-issue, quantise, emit
are three stages that each hold their own in-flight item.

### Why that is faster

Not because any stage got quicker — none did. Because **the AXI channel and the
NoC channel stop excluding each other.** They are physically independent and
were serialised only by shared state.

The CU changes correspondingly: it has no fetch cursor. `rcv_ent`, `fl` and the
assembly register disappear, because a flit says where it belongs. Arrival
order stops being load-bearing, which is the property that makes everything in
§2–4 expressible.

---

## 2. Streaming — deletes the CU's requester

### Before

`mx_cluster_cu.v` `S_FILL` is a loop that issues one request per entry:

```verilog
    if (!send_valid && (req_ent < n_r) && ...) begin
        send_flit  <= rd_req(base_r + req_ent * FP16_ENTRY_BYTES, l1_sel);
        send_valid <= 1'b1;
        req_ent    <= req_ent + 1;
    end
```

`n` is already in the instruction. The CU spends a NoC flit, a MAG queue slot
and a full round trip to re-state, 32 times, information it was given once.

### After

`FILL sel, bank, base, count, stride` is one flit. MAG's read engine generates
the address sequence itself — it already generates AXI burst addresses, so this
is the same counter moved one level up.

```
   before   CU: req(0) ..wait.. req(1) ..wait.. req(2) ..wait..  x32
   after    CU: desc(base,32) ......................................
            MAG:      burst burst burst burst burst burst burst ...
```

### Why that is faster

The round trip is not shortened; it stops being **per entry**. The CU's request
path is on the critical loop 32 times before and once after. Everything after
the first entry proceeds at MAG's service rate, which is a throughput, not a
latency.

Second-order but real: MAG's intake queue stops carrying 32 near-identical
requests, and the DRAM controller sees one sequential run instead of 32
separately-arbitrated reads.

---

## 3. Overlap in MAG — uses two channels at once

### Before

```verilog
   S_Q_FILL : collect 8 AXI beats into q_w0..3     AXI busy, NoC idle
   S_Q_WAIT : quantiser latency                     both idle
   S_Q_EMIT : send 4 flits from q_w0..3             NoC busy, AXI idle
```

One quantiser output buffer, so the next entry's fetch cannot begin until the
previous entry's last flit has left.

### After

A staging register in front of a separate **emit buffer**, so the finished
entry is handed over in one cycle and the next fetch starts in that same cycle:

```
   quantiser out (q_w0..3)          pre-quantised beats (p_w0..3)
              \                     /
               ---> emit buffer (e_w0..3, e_act) ---> 4 flits on the NoC

   entry n    fill ....... emit
   entry n+1       fill ....... emit
   entry n+2            fill ....... emit
                   |-- both channels active --|
```

`RS_WAIT` copies into `e_w0..3` and re-enters `RS_FILL` on the same edge. The
emit buffer is what makes the handover cheap: without it the quantiser's output
registers *are* the emit source, so a fetch cannot start until the last flit of
the previous entry has left.

### Why that is faster

The AXI read of entry *n+1* and the NoC emit of entry *n* use **different
wires**. They were serialised by a single buffer, not by any resource conflict.
Adding the second buffer lets the module's two interfaces run concurrently, and
throughput becomes the slower of the two rather than their sum.

---

## 4. Multicast — makes the quantiser run once per byte, not once per reader

### Before

The driver gives each cluster its own N-slice, so all clusters sweep the **same
rows of A**. Each issues its own request:

```
   CU0: rd_req A[m]  ──> MAG: AXI read A[m], quantise, emit ──> CU0
   CU1: rd_req A[m]  ──> MAG: AXI read A[m], quantise, emit ──> CU1
   ...                                    ^^^^^^^^^
   CU7: rd_req A[m]  ──> MAG: AXI read A[m], quantise, emit ──> CU7
```

Eight AXI reads of one address, and **eight runs of the quantiser** — which
`MAGSTATE` measures as 70% of MAG's busy time — producing eight bit-identical
results.

### After

The descriptor names **extra destinations** — `peers`, up to three `{y,x}` node
indices, plus `npeer` saying how many are present. The requester is always one
destination, so a shared fetch reaches **at most four** clusters:

```
   CU0 issues the descriptor, peers = {CU1, CU2, CU3}, npeer = 3
        ──> memory port: AXI read A[m] once, quantise once
        ──> emitter re-sends the SAME latched words once per destination,
            each with a different header
```

> **Not router replication.** An earlier draft of this section had the mask
> handed to the mesh so one input could be forwarded to several output ports.
> That is [`noc/spec.md`](noc/spec.md) §12's deferred multicast, and it is not
> what exists: the emitter's `e_dst` loop walks the destination list and emits a
> full set of four flits to each. So the **flit count on `noc_out` is
> unchanged** — this saves DRAM reads and quantiser passes, and nothing else.

### Why that is faster

This one does not remove waiting — it removes **duplicated work**. MAG's
quantiser is a fixed-throughput unit shared by every cluster behind it; running
it C times for one result consumes C times the only resource that all clusters
contend for.

It is also the only change here whose benefit *grows* with cluster count. The
others divide a constant. This divides the part that scales, which is why
without it each added cluster brings its own full copy of A's traffic and
MAG saturates regardless of how good the fetch path is.

> **The last clause is the part measurement falsified.** Adding clusters did
> degrade the fetch path badly — 7.6 → 25.6 → 93.3 cycles per entry at 2, 4 and
> 8 — but **MAG never saturated**: `mem_rd` peaked at 55.6% and `noc_out` at
> 60.2%. What ran out was the single read engine serving every cluster in turn,
> and the fix was one MAG port per mesh row rather than less traffic
> ([`mas/spec.md`](mas/spec.md) §2.4). Multicast is built and deliberately
> disarmed — it needs a rendezvous, and it measured faster *and wrong* without
> one ([`optimization.md`](optimization.md) §J2). "It must be bandwidth" was
> wrong here twice; count the per-channel utilisations before believing it a
> third time ([`perf.md`](perf.md) §2.2).

Note what multicast does **not** need: no storage, no tags, no eviction policy,
no coherence. A cache would divide the DRAM traffic but not the quantiser,
because a cache in front of MAG stores post-DRAM, pre-quantise bytes — and a
cache after the quantiser would have to store C copies anyway.

---

## 5. Why the earlier prefetch attempt could not have worked

```
   protocol as specified              what prefetch required
   ---------------------              ----------------------
   one request in flight              several in flight
   response matched by ARRIVAL ORDER  response matched by ... nothing
   MAG: one rq_x/rq_y/rq_txn          one context per outstanding request
   CU: fl counts 0..3 for "the" entry one counter per outstanding entry
```

The change added a counter that let the CU *count* requests the protocol has no
field to *carry*, against a MAG that cannot represent a second requester. There
is no depth at which that is correct.

Compounding conventions, all unwritten at the time: the burst end was signalled
twice (MAG's `last` bit and the CU counting flits — the CU trusted its own);
`len` was written as 3 by the CU and overridden to 7 by MAG; and
"entry = 256 B = 8 beats = 4 flits = 928 bits" was restated in
`mx_cluster_cu.v`, `mx_quant.v` and `tensor.py` with nothing checking they
agreed.

> Two of those are gone. `len` is now simply **ignored** on an entry read — the
> burst length is a consequence of the entry size and the source format, both
> stated by the flags, so a requester cannot state it inconsistently — and
> `Q_ARLEN`/`P_ARLEN` are **derived** from the entry size and `DATA_W` rather
> than written as 7 and 3 ([`isa/memory.md`](isa/memory.md) §2 and §5). The
> entry-size constant is still restated across the three files.

**The specification forbade concurrency and I changed the implementation.** §1
is the fix, and it has to come first because §2–4 are all unrepresentable
without it.

---

## 6. Build order

Each step is verified on a shape that simulates in under a minute before the
next begins.

| # | change | behaviour it alters | testable by |
|---|---|---|---|
| 1 | tagged responses | `take_rd` no longer needs `S_IDLE`; CU loses its cursor | 64x64x128 correctness, unchanged rate |
| 2 | streaming `FILL` | CU issues 1 flit, not `n` | fill cycles/entry falls |
| 3 | MAG double buffer | AXI and NoC channels concurrent | `MAGSTATE` qfill and qemit overlap |
| 4 | `peers` on the descriptor | quantiser runs once per unique byte | `mem rd` count falls with C constant |
| 5 | kernel emits shared descriptors | one descriptor names up to 4 destinations | 2-cluster rate scales to 8 |

1 changes no throughput by itself — it is the enabling change, and its test is
that nothing breaks. 2 and 3 are local to one module each. 4 and 5 must land
together, since a `peers` list nothing sets does nothing.

> **Steps 1–3 landed; 4 and 5 did not.** The `peers` mechanism is built and
> deliberately disarmed — armed without a rendezvous it measured faster *and
> wrong*, because a shared A entry reaching a cluster still executing its
> `FILL B` is written into the B side of L1. The driver does not set the field.
> [`optimization.md`](optimization.md) §J2 has the revival condition.
