# R19: proposed next round

Investigation only: no compiler, runtime, firmware or RTL implementation changes
were made. This plan uses the corrected Round i1 metric: second-input launch-cycle
sums, not a retained whole-model replay or synchronized global model makespan.

## Cost basis

Paths below are under `measurements/data/i1/`. For each board, **F1/F4** means
`vitb.i11.firmware.b1.p{1,4}.multimesh_<board>.{1,4}n.firmware`, and **E4** means
`vitb.i11.fw.b1.p4.multimesh_<board>.4n`.

| Second-input cycles, F4 `card.jsonl.gz` joined to `launches.jsonl.gz` | v9t6 | v9t8 |
|---|---:|---:|
| Matmul | 10,578,057 | 11,136,117 |
| Quant | 5,431,353 | 4,516,544 |
| DSL | 2,712,841 | 2,755,407 |
| Move | 834,095 | 813,266 |
| Convert | 0 | 373,073 |
| Total | 19,556,346 | 19,594,407 |

F1 totals are 42,072,185 and 40,249,814 cycles. The F1/F4 factors are 2.1513
and 2.0541. To exceed 3× in this metric requires saving more than
`19,556,346 - 42,072,185/3 = 5,532,284.33` and
`19,594,407 - 40,249,814/3 = 6,177,802.33` cycles. Matmul accounts for
59.86%/65.10% of the arithmetic excess over F1/4; quant for 27.25%/20.95%.
This reference is not a claim that perfect scaling is attainable.

## Ordered work

1. **Make the evidence identify what ran.** Aggregate completion records by
   launch and node, then observe an actual retained replay and global input
   interval. Current backend traces report recorded 1/replayed 0
   (`F4/meta.json`; `ktpugrad/device.py:459-506`). The eager recorder appends
   one record per node but associates successive records with successive
   launches (`software/tests/perf/record.py:315-355,369-372`;
   `runtime/card/collection.py:232-278`). Reconstructing E4 from its per-node
   `units.jsonl.gz` gives 19,358,031/20,410,862 cycles, not the old
   39,241,797/41,631,776. This correction has **zero physical speed gain**.
   Keep input outputs and the actual f32/MXFP7 reference arrays; i1's cited NPZ
   is missing. Gate changed schedules with general F-014 handling for
   software-controlled reads, not only the current 256-word descriptor pattern
   (`ktpu-l1/src/lower/image/fill.rs:7-57`). The shared cut model must use the
   RTL's 16×32-byte streamed words (`mag_mem_port.v:378-385`), unlike the current
   `reach.rs:27-42` census. CPU ARLEN remains zero (`rv64_nport.v:94-95`), so
   disclose residual exposure rather than promise a complete software cure.
   No measured speed gain is assigned to this correctness gate.

2. **Select actual-value K packing, owner tiles and MG/VC overlap jointly.**
   Start with FC2/projection and FC1; compare complete producer-consumer
   schedules, not isolated products. In F4/v9t8 captured L2, FC2
   `mm_ff8a45e455` uses three MGs, 100×128 tiles, and 96 K steps with nk=1;
   its 12 calls cost 3,285,412 cycles. FC1 `mm_4d9451a006` writes VC-packed
   q0 but the consumer remains unpaired, because pairing follows the board's
   preferred quantizer (`ktpu-machine/src/pairing.rs:18-42`). Preserve canonical
   zero scales; do not pair mover-produced zero blocks without a valid contract.
   A local 100×384 owner can instead have four 100×96 tiles, subject to full
   traffic/epilogue pricing. Three/four MGs × one/two pump halves × 197/200
   useful rows is a 36.94% useful-compute ceiling before other costs
   (`mx_cluster_mgr_pump.v:94-99`; `machines/multimesh_v9t8.json:188-207`).

   E4/v9t8 FC2 mean local duration is 272,179 cycles; mean maximum MG fill-wait
   238,288 and compute 79,391 (modulo-2^32 deltas, 12 calls × four nodes).
   These counters overlap (`mx_cluster_cu.v:550-562`); pairing alone is not a
   promised end-to-end doubling. Reduce repeated operand requests with legal
   K chunking/residency/sharing as well (`gen/matmul.rs:1769-1781`).

   F4/v9t8 FC1 has one MG wave then four VC streams (captured L2 lines 19–28),
   costing 3,564,214 cycles over 12 calls. E4 mean maximum VC busy is 120,386.
   A linear two-wave overlap model saves `12×120,386/2 = 722,316` cycles;
   extra fill/setup costs may negate it. Using the fourth MG has an optimistic
   whole-FC2-bucket ceiling of `0.25×3,285,412 = 821,353` cycles, not a forecast.
   **Witness:** chosen and alternative emitted schedules, useful MACs,
   per-node fill/compute/VC counters, correct outputs and lower complete-chain
   duration. Reject changes that only move cost into packing, copies or waits.

3. **Propagate ownership and packing through row/attention chains.** F4's
   36 activation quant calls cost 1,765,523/949,419 cycles; 12 mapped V packs
   cost 1,194,926/1,202,834; 12 probability packs cost 1,095,110/1,102,079
   (`launches.jsonl.gz`, `card.jsonl.gz`, and captured
   `row_shares_{53d406c6ff,a93d955bf1,9d88e34d8c,725b4f48fa}` L2).
   Retain consumer-compatible owner-local layouts instead of a separate
   materialize/read/pack phase. Distributed chain links currently retain DRAM
   residency (`gen/chain/policy.rs:31-83`). FC1 already producer-packs; do not
   count that as a missing optimization.

   Its FP16 out store is a separate liveness candidate: all 12 F4 FC1 output
   spans on each board have no subsequent launch binding inside them
   (`launches.jsonl.gz`), yet `gen/vector.rs:1036-1048` stores FP16 before
   unpacking the same local pack and quantizing q0. Preserve pack/unpack
   rounding; remove only a proven unobserved store. That is
   `12×200×3072×2 = 14,745,600` bytes/input, not a measured latency saving.
   **Witness:** no hidden reader/alias contract, fewer intermediate bytes and
   lower complete-chain duration with the same rounding/nonfinite semantics.
   A 50% standalone-quant reduction is a **scenario**, saving
   2,715,676.5/2,258,272 cycles, not an established achievable result.

4. **Then reduce boundary and in-order WAIT costs.** Summing head+copy+bind+
   barrier on each F4 launch's slowest node gives 505,429/563,029 cycles,
   2.58%/2.87% of the input (`F4/card.jsonl.gz`). This bounds only the recorded
   non-step component, not all feed or synchronization. Larger retained chains
   and ordering independent sends before blocking waits may save part of it.
   Hardware WAIT blocks later entries and interlink commands wait outstanding
   mover work (`dispatch_engine.v:162-183`); the firmware feeds entries
   serially (`interp.c:873-921`). Cross-node skew/serialization has no separately
   measured cycle share here. **Witness:** global interval plus per-node
   package/dependency timeline and DE head/issued/queued state, not differences
   between unsynchronized local timestamps. Runtime already submits every
   participating queue before collection (`runtime/card/submission.py:98-114`).
   The independent-model-node overlap restriction is not an explanation to
   rediscover for these tensor-parallel launches.

   Use KAOS more ambitiously only after diagnosing its unresolved dependency
   and DE failures. Completed i1 KAOS rows cost 1.25–1.42× the corresponding
   firmware launch sums, comparing different scopes (`kaos.b1.p{1,2}...`
   `card.jsonl.gz`; `backends.py:19-39`). P4/v9t6 stalls at seq 337 of the
   second input; P2/v9t8 returns −7 at seq 484 (`launches.jsonl.gz`, `klog.json`).

5. **Leave ISSUES 83 lower-bound pricing off the product-speed critical path.**
   Proven pruning that preserves the chosen schedule gives **zero direct
   card-cycle saving**. The generic strict-bound framework already exists at
   `ktpu-kernels/src/split/cost.rs:272-293`. Compiler/test throughput is not
   the owner's focus for this round.

## Fault observation before another broad sweep

On the next BAD_STEP, before reset/reuse, read the failed package span back
against the retained host bytes. Exact bytes favor a transient read/cache/address
problem; changed bytes show persistent memory alteration. This is not unique
proof of F-014 (`interp.c:878-893`; issue85 failure directories).

For a stuck KAOS epoch, retain pending task/expected slot, actual slot count,
ring written/sent frontier and DE head. Missing/mismapped release supports
lowering; written-but-unsent release supports egress. For −7, retain the first
failing DE entry/check/address/status: range failure, nonzero CHECK_ZERO and
unit faults collapse into the same task-level code (`os/kernel/units.c:519-527`,
`os/dispatch/graph.c:428-450`). There is no single scalar read that separates
all mechanisms. These are proposed observations, not work performed in R19.

## Goal budget and acceptance

A scenario of 40% lower matmul plus 50% lower standalone quant gives
`19,556,346 - 0.4×10,578,057 - 0.5×5,431,353 = 12,609,446.7` cycles on v9t6
and `19,594,407 - 0.4×11,136,117 - 0.5×4,516,544 = 12,881,688.2` on v9t8:
3.34×/3.12× the F1 totals. Current evidence does **not** predict these gains.
Do not add component savings already inside those buckets. A near-linear
product claim also needs synchronized global batch-1 latency, correct outputs,
and actual retained replay. VC busy is not lane utilization; the ≥50% vector
lane target needs active-lane/issued-work accounting. Matmul MFU must use useful
logical MACs and the agreed hardware peak, not busy time or padded work alone.
