---
title: Kernel templates and offline verification
summary: Kernel split options, device-grant selection and replay, plus offline binding proofs and parameter confinement.
tags:
  - spec
  - stack
---
# Kernel templates and offline verification

## Kernel split options

A logical kernel receives an ordered `NodeSet` of physical sysnode IDs, checked
against `Machine::nodes()`. The generator and tracer use this grant independently
of the local MG/VC counts. `SplitRequest` carries consumer form/scale requirements,
input forms, numerical contract, dispatch profile and an optional forced option
ID. `SplitOption` records the axis, participant and active-node counts, shares,
physical output ownership, symbolic events, legality witness and cost. A
`PlannedKernel` carries the executable graph or an authoritative L1 executable.
(code: ktpu-kernels/src/split.rs:18-36, :40-69, :112-176)

Physical regions are byte runs over allocation identities. `validate_ownership`
requires distinct writers to own distinct 64-byte lines and the byte union to
cover exactly the declared valid output. Bindings must resolve aliases to
canonical physical identities; this ownership check alone is not a numerical or
all-agent happens-before certificate. `partition_tiles(T,P)` uses balanced cuts
`p*q+ceil(p*r/P)`, where `q=T/P` and `r=T%P`. Empty shares stay in the plan but
`emit_shares` emits no work for them. Explicit sysnode scopes place each nonempty share on its granted
physical ID. (code: ktpu-kernels/src/split.rs:180-200, :204-276, :278-293)

`call_split` dispatches to family planners. Graph families expose distributed
options where their ownership and local schedules permit them; generators with
only a local implementation expose `p1`, on the first granted node. Producer
forms and generator precision remain the default contract. The entry refuses
unsupported consumer forms, explicit scale groups and unsupported numerical
contracts rather than claiming a conversion it does not emit. Input-form labels
and mapped-read capabilities remain request metadata; generator arguments select
the reads. A split plan is not a certificate for a new binding, topology or
consumer form. (code: ktpu-kernels/src/call.rs:1584-1628, :1629-1686,
:1687-1873, :1874-1974)

### Pricing and selection

`CostFeatures::of_shares` obtains each complete local schedule's existing L2/L1
cost and removes the launch intercept already included by `graph::cycles`.
`LocalTiming` takes the slowest share and adds `vc.launch_front` once; it does not
divide an unsplit kernel time by P. Local conversions, scratch copies, prologues,
epilogues and waits are part of the emitted schedule. Estimates retain revision,
provenance, uncertainty and `measured=false`: they are model prices, not device
measurements. Selection orders by makespan, conversion count, event count and
option ID. A forced ID must name an eligible option; priced planners still price
that option. Cached choices retain prices across forced selections. Forcing
is selection, not a request to measure an otherwise unpriced local path: the
matmul one-node planner selects its ownership option without pricing unless
its caller requests `price_unsplit` for a dependent sequence. The generic local
`p1` path likewise computes timing only when `price_unsplit` is requested. These
default cost records are not measured zero-cycle execution times.
(code: ktpu-kernels/src/split/cost.rs:10-35, :94-151, :154-217;
ktpu-kernels/src/gen/matmul/split.rs:758-788;
ktpu-kernels/src/call.rs:1953-1974)

Dependent work is priced as dependent work. Matmul K reductions and output-line
assembly use the complete composed graph, B delivery uses its emitted transport
model, convolution uses the patch/product sequence, and GroupNorm includes its
population tree and normalization. These are not independent-share divisions.
Row-stage sequences likewise use the complete composed graph for makespan while
retaining per-node local costs. (code: ktpu-kernels/src/gen/matmul/split.rs:823-868;
ktpu-kernels/src/call.rs:1126-1151;
ktpu-kernels/src/gen/conv.rs:447-473;
ktpu-kernels/src/gen/groupnorm/distributed.rs:410-438, :535-567)

### Matmul

`matmul.mm_kernel` offers `m`, `n` and every eligible factorization `mn-PxQ` of
the grant. M cuts preserve the row quantum (the class tile for classed kernels);
N cuts preserve packed column groups. N and mixed column cuts require an
independent output-column domain, not a column reduction or row-statistic
output. Batched products also expose `groups`. Each share reads global packed
operands at its origin. Boundary outputs needing a common physical-line owner
use private output and explicit assembly, counted as a conversion.
(code: ktpu-kernels/src/gen/matmul/split.rs:160-202, :204-430)

For two or four participants, non-classed products with nonempty K expose
`k-tree` and `k-serial`. They partition whole packed K chunks, accumulate using
native FP22 peer reduction and run the epilogue at the reduction root. Empty K
shares do not contribute. A requested reduction order filters options by their
actual edge list. With four live participants the tree joins 0 into 1, 3 into 2,
then 1 into 2; serial reduction sends each earlier active rank to the last active
rank. Changing this order changes the numerical contract, not just placement.
(code: ktpu-kernels/src/gen/matmul/split.rs:466-611;
ktpu-kernels/src/gen/matmul/peer.rs:43-66)

B-pass options retain the M or mixed M/N ownership but share raw packed B words:
`b-pass`, `b-pass-relay`, `b-pass-PxQ` and `b-pass-PxQ-relay`. Direct delivery sends from the first participating node to each destination MG;
relay delivery forwards through successive participants' VC0 buffers and each
participant feeds its local MGs. They have separate candidates. VC0 owns two half-L1 buffers during MG
stages; private-A prerequisites retire before B-ready and receiver-scoped
ready/credit events protect bank generations. The emitted L1, not the precursor
graph, is the executable. Its manifest records actual sender reads and events.
The price sums emitted node budgets plus one launch intercept, mesh-hop word
traffic, remote payloads, bank events, relay words, compute issues and private
fills. This serial upper estimate is explicitly unfitted; it asserts no measured
crossover. Other independent M/N options use the slowest complete local share.
(code: ktpu-kernels/src/gen/matmul/split.rs:431-465, :747-803, :823-868;
ktpu-kernels/src/gen/matmul/b_pass.rs:38-97, :240-352;
ktpu-kernels/src/split/cost.rs:37-92)

Packing itself has `destination-tiles` options. `matmul.quant_kernel` and
`matmul.quant_walk_kernel` assign complete physical `(side,g,nk)` tiles, preserving
global source coordinates, valid tails and finite-status checking. Mapped reads
and ordinary or walked reads retain their respective source contracts. Local
schedules are priced through the shared vector split planner. Classed packing
and materialized prepared-read sequences retain their complete local `p1`
schedule; the dispatcher states the reason in its legality witness.
(code: ktpu-kernels/src/gen/matmul/destination.rs:31-128, :149-303;
ktpu-kernels/src/call.rs:1741-1815, :1929-1967)

### Vector rows and destination tiles

Vector `rows` options partition independent rows while retaining each complete
reduction width and global operand strides. Flat vector work uses `tokens`, one
complete VL row per work item, with the last physical extent clipped to the
caller's whole words. Row cuts coalesce adjacent quantums until every output
plane has disjoint line ownership. Row-local chains and fused row stages can
share that partition; other supported row/flat sequences compose distributed
stages with L2 access ordering for their intermediates. Every nonempty local
schedule is emitted and priced, including exact tails and scratch copies.
(code: ktpu-kernels/src/gen/vector/split.rs:78-192, :196-299;
ktpu-kernels/src/call.rs:1410-1579)

Copy, cast, eligible walked copy/fill and transpose use `destination-tiles`.
Copies partition complete destination words with line-safe cuts. Walked
conversion normalizes destination traversal, including negative strides, while
preserving the corresponding global source walk. Casts use complete vector
trips. Transpose partitions complete hand-tile/GT4 destination rectangles; a
row pitch that cannot support independent column-line ownership keeps the whole
row width together. These planners use the vector split emitter and complete
local timing. Walks with ordered overlapping or partial-line destinations,
indexed lane classes and staged relayout sequences retain their local `p1`
option instead of inventing independent owners.
(code: ktpu-kernels/src/gen/convert.rs:50-111;
ktpu-kernels/src/gen/convert/destination.rs:20-133, :136-252;
ktpu-kernels/src/gen/transpose/destination.rs:40-137;
ktpu-kernels/src/call.rs:1476-1485, :1816-1867, :1929-1967)

### Convolution and GroupNorm

Convolution exposes `spatial` (matmul M), `channels` (matmul N, when output
columns are independent) and, for grouped convolution, `groups`. Patch production
and contraction are composed over the same global patch allocation. Channel
splits use a row-distributed patch producer; spatial/group splits produce the
patches their product shares consume. The selected makespan is the complete
patch/product graph cost, with its cross-node dependencies, not the product
price alone. Forcing selects among these family IDs.
(code: ktpu-kernels/src/gen/conv.rs:256-340, :439-473)

GroupNorm's `domains` option assigns complete NCHW normalization populations
(batch times groups); `spatial` splits the elements within each population.
Counts exclude padding. Each leaf computes a mean and centered M2, transported
as F32 vector state with E8M15 arithmetic. For populations A and B, with
`delta=meanB-meanA`, the merge is `n=nA+nB`,
`mean=meanA+delta*nB/n`, `M2=M2A+M2B+delta*delta*nA*nB/n`.
An empty population is the identity. Adjacent pairs merge left to right at each
tree level, carrying an unmatched state unchanged. The root supplies mean and
`rsqrt(M2/n+eps)` to output owners, which apply channel gamma/beta. Complete-domain
and split-domain candidates are priced using the emitted population tree, F32
transport, normalization and any private-output copy; forcing still emits and
prices the selected candidate. The output is padded domain-major storage with
one writer per line, not a claim of bitwise equality between reduction trees.
(code: ktpu-kernels/src/gen/groupnorm/domains.rs:18-44;
ktpu-kernels/src/gen/groupnorm.rs:19-60;
ktpu-kernels/src/gen/groupnorm/distributed.rs:62-161, :227-302, :303-438,
:439-457, :535-585)

## Attention ownership and execution

Attention assignments offer `heads`, `query-rows`, each factor pair
`heads-query-PhxPq`, and `balanced` contiguous flattened head/query-tile ranges.
Every span retains global query coordinates and the complete key range of its
head. Empty participants remain in the assignment. The motif's ownership unit
is 128 query rows for flash/flashd and 256 for srflash. Physical output ownership
is validated before each complete local L1 schedule is timed and the cheapest
eligible option chosen. Forced options undergo the same admission and pricing.
(code: ktpu-kernels/src/gen/attention.rs:66-152;
ktpu-kernels/src/gen/attention/flash_split.rs:206-371)

All three recorded generators have executable adapters: `attention.flash`,
`attention.flashd` and `attention.srflash`. They accept `heads`, `queries` and an
optional `contract`; keys are 1024 and Dk=Dv=64, with unmasked MX7 scores. Admission
allows 1..10 heads and whole motif query groups up to 1024 rows. Flash uses
sticky-overflow redo; flashd requires the caller's `unit-scores` contract and
uses a direct score ring; srflash uses SR row state. Four distinct MG roles are
required, plus two VCs for flash/flashd or two SRs for srflash. These resource
checks do not establish numerical eligibility for arbitrary attention inputs.
(code: ktpu-kernels/src/call.rs:1647-1686;
ktpu-kernels/src/gen/attention/motif.rs:57-161;
ktpu-kernels/src/gen/attention/flash_split.rs:214-239)

Adapters substitute ownership coordinates, external addresses and pipeline
bounds into the hand programs, retaining their local rings, banks and instruction
streams. Flash scratch per logical rank is 1 MiB of scores, 64 KiB of partials
and 16 KiB of sums/offsets; flashd uses the latter two; srflash uses 256 KiB of
partials. The SR adapter emits at most two heads per pipeline and whole 256-row
normalization groups. Each physical node has an explicit scope, and successive
pipelines have private symbols with node-local scratch. `PlannedKernel.l1` is
the executable; the accompanying graph supplies port metadata, not a replacement
executable. (code: ktpu-kernels/src/gen/attention/flash.rs:59-134;
ktpu-kernels/src/gen/attention/flashd.rs:12-57;
ktpu-kernels/src/gen/attention/srflash.rs:12-60;
ktpu-kernels/src/gen/attention/motif.rs:65-79;
ktpu-kernels/src/gen/attention/flash_split.rs:127-198, :363-371)

The eager attention API selects flash automatically for eligible inputs without
a bounded contract and flashd for `unit-scores`; it also accepts explicit
flash/flashd selection. Precise scores are ineligible for those MX7 motifs.
An ineligible automatic choice uses dense attention and records its reason;
an ineligible explicit choice raises `ValueError`. Srflash is a recorded
planning generator, not an eager attention selector. Eager motif execution
pre-scales Q by log2(e)/8, packs Q/K with the selected nk and V with nk=16, and
copies logical query rows from the 1024-row-pitch output.
(code: software/kohakutpu/application/kohakutpu/application/kernels/attention.py:25-81;
software/kohakutpu/application/kohakutpu/application/api/attention.py:41-47, :137-193)

## Eager grants and TinyJit replay

Every eager generated call obtains the device grant: the current `home` node in
an explicit node scope, otherwise the card's ordered nodes. With one sysnode,
no forced option and an ordinary graph generator, `invocation` uses ordinary
cached generation without split pricing and reports `p1`. Hand L1 attention
motifs always go through planning, including a one-sysnode grant. Multi-node
calls and forced calls use cached split generation, retaining source and
selection metadata for capture and library lookup. A forced `p1` is not a
universal option: it must be exposed by that family's planner.
(code: software/kohakutpu/application/kohakutpu/application/runtime/generated.py:19-27,
:52-88, :149-161)

Standalone Python template factories retain a logical `TemplateCall` alongside
an inlineable Python body. Device specialization validates the declared specs
and constants, then uses the same generated invocation path; the specialization
cache includes the grant and forced option. Explicitly authored DSL kernels
retain their ordinary tracing path.
(code: software/kohakutpu/application/kohakutpu/application/kernels/template.py:30-55;
software/kohakutpu/application/kohakutpu/application/runtime/device/kernels.py:133-187)

`KTPU_SPLIT_<GENERATOR>_OPTION` forces a family option, with dots replaced by
underscores and the generator uppercased: for example
`KTPU_SPLIT_MATMUL_MM_KERNEL_OPTION`, `KTPU_SPLIT_ATTENTION_FLASH_OPTION` or
`KTPU_SPLIT_GROUPNORM_KERNEL_OPTION`. This takes precedence over
`KTPU_SPLIT_OPTION`, which applies only to `matmul.mm_kernel` and `conv.kernel`.
Absent forcing, each planner chooses among its eligible candidates as described
above. (code: software/kohakutpu/application/kohakutpu/application/runtime/generated.py:78-88)

TinyJit admits a recording after two equal read-free captures. A recording with
multi-node work is prepared as split replay: graph-backed launches are grouped
between host writes and opaque/executable-L1 boundaries. Each pure region goes
through `SplitRunner`, which composes the graph, resolves external physical
aliases under allocation pins and compiles the region together for node ordering
and event-slot allocation. Optional `KTPU_GRAPH_FUSE=1` rewrites run within these
boundaries; split composition itself does not require that knob. Opaque L1
launches retain their own executable. Replay preserves host writes, submits
prepared regions through ordinary submission and waits for the previous replay
epoch before reusing its scratch and event slots. Split replay matches storage
and runtime private state but does not require or restore resident-core digests;
package replay retains that additional state contract.
(code: software/kohakutpu/application/kohakutpu/application/ktpugrad/device.py:380-394,
:438-509;
software/kohakutpu/application/kohakutpu/application/runtime/graph/split_recorded.py:15-19,
:31-91, :122-187;
software/kohakutpu/application/kohakutpu/application/runtime/graph/split.py:24-36, :50-92)

Queue admission can overlap runs on disjoint physical lines, including runs
using the same multi-node grant. A single common node FIFO already orders its
runs. Otherwise admission compares bound read/write line intervals and leased
receiver/slot resources; unknown graph contracts retain allocation-wide
conflicts. Dependency completion can be observed without publishing success
past an earlier fault. Host reads and writes still collect through their last
conflicting allocation use. This is access-based admission, not a promise that
all independent work executes in parallel.
(code: software/kohakutpu/application/kohakutpu/application/runtime/device/submission.py:79-98,
:126-149, :443-482;
software/kohakutpu/application/kohakutpu/application/runtime/graph/access.py:18-61;
software/kohakutpu/application/kohakutpu/application/runtime/graph/leases.py:8-28)

## Verification domain

- **STACK.TEMPLATE.001** `unproven` For a fixed machine, package set, inclusive integer parameter box, physical address bindings and effective buffer extents, the affine-binder/certified-mover/exhaustive-checker-quotient proof mode returns `PROVEN` only after establishing binder legality and an empty checker result for its certified quotient of the box. Each simulated binding must retain its emitted package order when scheduled first with empty resident instruction memory and then with the state left by the first schedule. Proof mode rejects any checker finding. Certified static mover execution may be reused across bindings while the independent line checker is rerun. This host-side guarantee does not establish safety under arbitrary initial core state, alternating bindings, composition with other templates, changed physical addresses or extents, or numerical equivalence to a generator. (code: software/kohakutpu/compiler/crates/ktpu/src/templates/proof.rs:141-208, :215-274, :276-328, software/kohakutpu/compiler/crates/ktpu/src/templates/proof_mover.rs:63-112; evidence: offline proof implementation, not silicon or RTL-test evidence)

`template_verify(..., mode="proof")` returns either `PROVEN` with an argument certificate or `REFUSED` with the property, reason and a counterexample when available. `mode="sample"` is separate and returns coverage points, not a universal proof. It checks every corner, boundary-adjacent faces (minimum plus one, maximum minus one and one where in range), and a digest-seeded interior sample. A sampling budget that cannot cover required points refuses rather than dropping them. Each point runs twice. The digest seeds sampling; it is not an assumption of the proof.

A vector core advances its unpack pointer by half its local memory on each trip. Even trip counts leave a final unpack disjoint from an outstanding fill; an odd trip count makes them overlap. Counts two and four pass even when the fill-to-unpack synchronization edge is absent. The adjacent binding three exposes a real checker race. Restoring that edge makes all three bindings pass.

## Parameter confinement

A host-side confinement certificate classifies every parameter by its patch sites. The conservative data-only case is a vector IMEM instruction whose changed bits are confined to the ISA-defined `XB.xk` field. Field positions come from the selected machine's ISA formats. Certificates name each decoded patch field and record its category: register, address, extent, loop count, synchronization, opcode, arithmetic immediate, or XB amount. A field category describes the ISA operand, not a proof of checker independence. Arithmetic immediates without an independence witness remain in checker coverage; unrecognized fields refuse certificate derivation.

The classifier checks affine value bounds, constant low bits, relocation overlap, dispatch structure, and resident image contexts. An XB-shaped word following `VSETI` is not classified as an instruction: it can instead be a scalar immediate controlling a loop. A parameter is data-only only when every site it influences qualifies. Hazard verification fixes data-only parameters at their lower bound and retains corner, adjacent, and seeded coverage for the remaining parameters. Numerical equivalence is checked separately against directly compiled bindings.

The checker removes `XB.xk` from its decoded field representation. Reading that removed field fails explicitly; the `verify.confinement/read-xk` mutant witnesses this restriction. The `verify.confinement/loop-data` mutant misclassifies a loop immediate and exposes the odd-trip race described above.

Confinement alone does not prove the hazard-relevant box. A class certificate also does not prove safety under arbitrary buffer aliasing or changed relocation environments. Store admission records the pairwise physical overlap environment used by coverage, including relative offsets for overlapping buffers on the same mesh. Binding rejects an environment that differs (`L0.BIND.044`), even when distinct allocation handles translate to the same physical bytes. Proof capabilities pin the complete physical binding vector, which also preserves line phases and rounded-line neighbours; preserving byte overlap alone would not discharge line ownership.

## What one binding establishes

The checker schedules each launch against the prior launch's resident IMEM and descriptors, then executes the ordered launches on an abstract machine. One execution tallies counter contributions; a second computes conservative happens-before clocks using those totals. The model checks completion/deadlock (`L1.VER.006`), counter and acknowledgement rest (`.007`), receive-port exclusivity and shared-fill/response collisions (`.008`), unordered conflicting local-memory accesses (`.009`), modeled receiver buffer/peer-drain limits (`.010`), routes (`.011`), data-dependent loop coverage (`.012`) and unsupported model operations or event exhaustion (`.013`). The independent mover/xform line checker evaluates node control and patches and rejects competing partial write-line owners (`L1.VER.017`), unknown footprints and its own budget exhaustion. IMEM fetches without resident words produce a `model` finding; this is not a general hardware IMEM-capacity or arbitrary-residency theorem. Register-bank lint (`.014`) is a separate L1-text check, not part of the package-only `check` API.

The ordinary runtime gate refuses its designated safety finding kinds; `loop`, `model` and bank diagnostics are not ordinarily refusing. **Proof mode refuses every finding**, including unsupported modeling, bounded data-dependent loops and resource exhaustion. It never treats an incomplete execution as a proof.

The binder independently checks parameter bounds and names, checked affine arithmetic, unsigned field widths and split fields, disjoint patch writes, whitelisted step-value sites, relocation widths, buffer-relative starting offsets and repeat relocation ranges. `buffer_sizes` is a fixed, validated effective-extent override, not a symbolic function of the parameters. Handle binding additionally checks remaining live allocation extents. Neither starting-offset validation nor the abstract checker is a blanket proof that every DRAM walk lies inside a declared allocation; applications retain their ordinary allocation and footprint obligations.

## Whole-box decision procedure

The accepted proof method is `affine-binder/certified-mover/exhaustive-checker-quotient`. Its argument certificate is version 2; the separate ISA confinement certificate is also version 2.

1. Validate a common box across meshes and the lower binding. Derive confinement from the actual package and selected ISA, never from caller-supplied field labels. Parameter-valued opcode and synchronization fields are refused. Other field categories are not assumed independent.
2. For each patch `c + sum(a_i * p_i)`, compute exact minimum and maximum in checked `i128`, choosing each parameter endpoint by the sign of its coefficient. Bind both extremising points to discharge unsigned width, split-field and buffer-offset inequalities **before** constructing mover summaries. Record the affine form, field category, extrema and discharge. Unsupported or overflowing expressions refuse. Buffer extents remain fixed.
3. Remove unused parameters and resident-instruction `XB.xk` dimensions under their confinement witnesses. Arithmetic immediates have no exemption. With package `REPEAT`, every used dimension remains relevant because increment carries are not covered by the XB witness. Static mover dimensions may also be removed under the single-owner theorem below; every mesh must independently discharge an omitted dimension.
4. Enumerate the **entire Cartesian product** of all remaining inclusive integer domains. The residue certificate uses modulus `hi - lo + 1` and lists every representative: it is injective on this box, not a machine-state period. Every remaining loop count, including zero, parity classes and tails, and every remaining interior address/extent combination, is executed exactly rather than extrapolated.
5. Require unchanged emitted dispatch order on the first and second launch and an empty checker result at every quotient point. Reuse the already computed first schedule. For static mover-only packages, the invariant simulation result may be shared between assignments, while the independent line checker is re-executed at every remaining assignment. If the product exceeds the cap, refuse the parameter/property. No corner-only or sampled fallback can produce `PROVEN`.

For any full-box binding, keep its remaining coordinates and replace its omitted coordinates with their lower bounds. Affine extrema establish binder legality. XB confinement establishes unchanged checker instruction semantics; the mover theorem establishes unchanged abstract execution and clean line checking at every omitted coordinate. The quotient point was checked, and all remaining coordinates are covered by finite universal enumeration. No observation that a pointer repeats after two trips is used to assert that counter histories, queue states or synchronization clocks repeat.

### Static mover theorem and incremental re-execution

The static mover witness requires an empty unit table, fixed steps drawn only from `MOVER`, `MWAIT`, `BARRIER` and `END`, and complete, unshifted DIM.count/DIM.stride patches. Patches cannot change register selectors, GO commands, headers, node control, or dispatch consumers. Fixed relocation results are part of the pinned bound package; the binder proves that DIM patches do not overlap relocation writes. On this subset the abstract node model consumes no mover payload operands: it advances the same steps and clocks for every binding. Thus dispatch order, completion, counters, ports, routes and local-memory event checks are invariant. Only the independent line checker consumes the changing DIM operands. Sharing this invariant result across adjacent assignments is incremental checker re-execution, not sharing unproved machine state.

For **one ordinary GO stream**, excluding transforms and scatter, its source DIM operands have no line-checker consumers. For its destination, exact affine extrema establish strictly positive counts; affine congruences establish that every stride is a multiple of the machine's word size. The fixed base must also be word aligned. For each dimension with count maximum `C` and signed stride interval `[s_min,s_max]`, enclose every integer visit's displacement by `[min(0,(C-1)*s_min), max(0,(C-1)*s_max)]`. Summing these enclosures with the base proves nonnegative `u64` destinations. The product of count maxima must fit the line checker's visit budget. Signed stride intervals crossing the encoding discontinuity refuse the summary. Windows can only remove visits, so these obligations conservatively cover every enabled visit. There cannot be two competing partial-line owners when there is only one GO stream.

This is a conservative all-integer enclosure and congruence proof, not sampling, an exact optimization of correlated products, or a general pairwise-overlap solver. The certificate records the destination base, count/stride intervals, address enclosure, maximum visit product and theorem. A failed summary records its reason and retains exact enumeration. Multiple GO streams, transforms and scatter require ordinary line checking at every remaining assignment; an excessive enumeration product still refuses. When every mesh has the static execution witness, `simulation_bindings` is one even when `checker_bindings` is larger; without it both counts include every quotient assignment. No loop-period reduction is implemented.

`verify.template/mover-no-summary` loses the cap-one proof of a large safe walk. `verify.template/mover-no-guards` incorrectly accepts interior misalignment, a second partial-line owner, zero counts, negative destinations and excessive visit products. `verify.template/mover-no-projection` repeats invariant simulation at every assignment and is detected by the simulation-count witness, with bound bytes still equal to the full path.

Overlapping affine access intervals require simultaneous inequalities (`a_start < b_end` and `b_start < a_end`); safe corners do not prove those inequalities infeasible. For multiple potentially conflicting access sites, this implementation decides such hazards by full integer enumeration within budget, **not** a symbolic linear solver or compressed periodic-loop proof. A single-owner mover summary cannot discharge vector-core fill/unpack hazards. Larger unsupported domains are deliberately `REFUSED`. The two-parameter witness has fill offset `64 + 16*x + 48*y`, unpack offset 128, and `x,y in [0,2]`: only the interior `(1,1)` overlaps. The odd-trip witness is rejected at three without consulting sampling. The `verify.template/no-residues` mutant drops interior representatives and is killed by that witness; `verify.template/repeat-data` is killed by the REPEAT confinement-boundary witness.

## Verification and admission knobs

- `_rs.template_verify(..., mode="sample"|"proof", cap=...)` selects the algorithm. Sampling retains its `interior` and `confined` knobs; proof always derives its own confinement and never consumes seeded points.
- `TemplateStore(..., verification="sample"|"proof"|"both")`, or `KOHAKUTPU_TEMPLATE_VERIFICATION`, selects stored evidence. The default is `sample`. `both` measures and stores both independently. `proof` stores proof coverage for accepted boxes; a refused box retains sampled coverage and the per-member byte-proof path, never universal admission. `KOHAKUTPU_TEMPLATE_PROOF_CAP` sets the store's exhaustive-binding cap (default 2048).
- `_rs.template_certify(package, table, bindings, machine, cap=...)` and `TemplateStore.proven(id, bindings, cap=...)` produce an opaque in-process `TemplateProof` capability. Its `bind(params_json)` admits any in-box L0 binding without compiling, checking again or running a per-member byte-equality proof. It owns immutable package contents, physical bindings and effective extents. A serialized `PROVEN` report is informational, not a capability; loading one cannot forge admission. New addresses, extents or a new process require certification again.

The graph-based `ClassLibrary` retains byte equality to direct compilation. Checker safety alone cannot prove that a proposed parameter map implements a requested graph or supplies correct allocation/output metadata. Explicit L0-template admission and graph-class admission are distinct APIs. Production defaults retain per-member graph proofs: on the reference LeNet b8/b1 and ViT b1 captures, whole-box proof cost is 85.838/6.926/0.013 seconds versus 5.175/0.659/0.022 seconds for sampling, while the compared accepted members' byte proofs total 0.169/0.037/0.001 seconds. The explicit proof path is useful when its domain and repeated admission savings justify that one-time cost, not as an unconditional replacement for graph equivalence.

## Reference capture coverage

Offline rebuilds of the reference captures classify all 50 stored production class templates at cap 2048. Exact fallbacks are not relabelled as proven templates. Per-template argument certificates and refusals are emitted by the proof mode; `scripts/py/templates/prebuild.py --capture FILE` rebuilds the specified capture without retracing.

| capture | class templates | PROVEN | REFUSED | captured sources with a proven candidate | captured launches with a proven candidate |
|---|---:|---:|---:|---:|---:|
| LeNet b8, multimesh | 22 | 22 | 0 | 71 / 298 | 263 / 714 |
| LeNet b1, multimesh | 17 | 17 | 0 | 56 / 241 | 237 / 618 |
| ViT b1, multimesh | 6 | 6 | 0 | 16 / 44 | 241 / 563 |
| LeNet b1 forward, single-node machine | 5 | 5 | 0 | 16 / 86 | 35 / 127 |

At cap 256 the same set has 48 PROVEN and two budget refusals. Those two mover templates have 880 and 1653 exact assignments, with one invariant simulation each; measured whole-box proof costs are 0.104 and 0.046 seconds at cap 2048. Twelve mover boxes whose unreduced products exceed 256 satisfy the single-owner theorem, including a box with 323,338,392 integer assignments before reduction. No accepted template assumes a period or safe interior overlap. Candidate coverage uses the pinned class extents/addresses, not a claim of graph admission for unknown sources or smaller live allocations.

The current captured workloads likewise classify completely: LeNet b8 has 27/27 PROVEN, LeNet b1 18/18, ViT b1 6/6, and single-node LeNet b1 forward 6/6. Their captured sources with proven candidates are 82/317, 58/242, 16/47 and 18/86; corresponding launch counts are 1091/2807, 982/2476, 482/1056 and 74/254. Exact-only sources retain their ordinary fallback and are not universal template proofs.

## Parameter-aware configuration emission

An L1 crossbar operand can carry a sampled bind-time value as `parameter("identity", value)`. The identity is nonempty. For rotation/xor amounts and broadcast lane/shift fields, this marker excludes the field from compile-time equality elision: the encoder emits its XB configuration and leaves the parameter-valued fields unknown afterward. Other tracked fields retain their state. Constant operands retain ordinary equality elision.

The offline compiler option `parameter_rotations=True` declares every rotation operand bind-time data after L2 lowering. Parameterized images do not establish constant IMEM residency; their dispatches load complete images rather than matching sampled instruction bytes against another image. Fixed kernels do not enable this policy.

## Binding

The host-side L0 template binder checks parameter bounds, affine arithmetic, field widths, address relocation widths, and buffer extents. `Template.bind_handles` translates live allocation handles and checks their exact remaining extents before producing ordinary wire-package bytes. No template metadata is sent to the card.

## Production library selection and what is proven

`Device(classes=True)` enables class-first lookup; `classes=False` selects exact entries only. When the API argument is absent, `KOHAKUTPU_LIBRARY_CLASSES=1|0` selects the mode. The default is class-first. Both modes preserve `library_only=True`: a miss refuses instead of compiling.

The class dispatch index groups graph topology independently of integer launch values and stores a finite admitted member domain. Each member carries its launch-specific allocation metadata and buffer extents and references a stored class template and its bind parameters. Admission requires a Rust `template_prove` byte-equality proof: for every admitted launch, binding the shared class template at that launch's parameters produces wire bytes identical to the directly compiled package for that exact launch. Where a class decomposition or compiled package structure differs from the original launch (such as partial-output reductions or unrolled loop structures), the launch is not admitted to the class index and falls through to its exact library entry. Lookup verifies port signatures and the bound-package digest recorded during admission.

A source-key class hit is a candidate, not a final submission choice. After scratch allocation, submission resolves the actual handles through the live MMU, using the member's buffer extents, and compares the complete same-mesh physical overlap relation (including relative offsets) with the stored environment. Only a matching candidate is submitted. Template identity includes the canonical parameter table, stage/machine identity, and certified physical overlap environment. Distinct environments coexist as separately certified entries, even for the same source and table. Dispatch members retain references to these variants across prebuilds. Submission checks all current variants, including the primary when a cached candidate predates a later admission. Only when none matches does submission load the current-version exact entry for that source, check compatible scratch allocation metadata, and bind it through the ordinary exact-package path. Exact and class compiled objects are cached separately; the environment is checked again on each submission, including warm source-cache hits. An exact fallback preserves that launch's original instructions and still enforces ordinary handle, extent, and relocation checks; it does not establish universal safety for arbitrary aliasing in arbitrary kernels.

`class_hits` and `exact_hits` distinguish the two paths; environment fallbacks reclassify a candidate class hit as an exact hit without adding a library hit or compiling; `library_hits` counts both, and `Device.kernel` caches compiled kernels per exact launch source while preserving the class or exact hit accounting on reuse. Graph-class admission proves byte equality per admitted launch against direct compilation, with recorded checker coverage and translation-environment preservation. Whole-box checker safety (`STACK.TEMPLATE.001`) applies only to `PROVEN` templates, at the exact machine, physical bindings and extents of the proof capability; it does not extend graph admission to unadmitted launch sources.
