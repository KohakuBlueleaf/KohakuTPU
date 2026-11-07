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
consumer form. (code: ktpu-kernels/src/call.rs:2054-2106, :2107-2164,
:2165-2352, :2353-2453)

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
(code: ktpu-kernels/src/split/cost.rs:10-35, :94-186, :189-252;
ktpu-kernels/src/gen/matmul/split.rs:775-805;
ktpu-kernels/src/call.rs:2441-2453)

Dependent work is priced as dependent work. Matmul K reductions and output-line
assembly use the complete composed graph, B delivery uses its emitted transport
model, convolution uses the patch/product sequence, and GroupNorm includes its
population tree and normalization. These are not independent-share divisions.
Row-stage sequences likewise use the complete composed graph for makespan while
retaining per-node local costs. (code: ktpu-kernels/src/gen/matmul/split.rs:840-890;
ktpu-kernels/src/call.rs:1426-1442, :1481-1506;
ktpu-kernels/src/gen/conv.rs:447-473;
ktpu-kernels/src/gen/groupnorm/distributed.rs:410-438, :535-567)

### Independent planar stage groups

A fused descriptor may supply `group_ends`: increasing exclusive stage indices
covering the package. These are scheduling boundaries, not generated-statement
positions. The ordinary local package keeps its complete stage order and shared
scratch/mask lifetimes (DSL.SPEC.008, L2.PASS.004).

The `groups` candidate proves complete private definitions before private reads,
immutable public inputs, and public outputs written only by strided scatters.
Plane stages in one group have a common batch/head domain. Materialized attention
may consume the private planes and scatter its completed output, including an
embedded streamed merge. Every original plane belongs to one balanced grant
share; boundaries inside a batch/head chunk form smaller typed plane descriptors
with the original global source/destination origins. Scratch names are fresh
against public ports and other groups. Each owner emits its complete subsequence,
with private storage and a private compact-mask cache. Mask identity includes the
key capacity, key tile, valid-key count and tail input. Whole-row vector bands
use complete lane quanta, including single-head shares (API.ROW.004).

Read manifests include the strided source rows and shared mask/tail inputs.
Output manifests retain exact per-token scatter intervals; distinct owners must
not touch the same 64-byte line (DSL.KERN.020). Unsupported dependency patterns
retain ordinary lowering. A malformed boundary list is refused with API.OP.001.
An ineligible distributed candidate does not remove the ordinary implementation;
a forced `groups` request requires a legal candidate.

Both `p1` and `groups` prices include complete finalization, quantization status,
mask initialization, extraction, transpose, scratch and merge work. Distributed
node budgets come from one scheduled complete module; LocalTiming adds one
launch intercept to the maximum budget. Default selection compares these emitted
prices; `p1` is not selected merely because a private edge exists. Native staged
projection attention uses these same descriptors and ownership rules.

Within one compiler invocation, complete graph prices are shared by exact graph
and machine digest, with at most 32 entries per thread. Nested constructors share
the scope; results do not survive its exit. This reduces repeated lowering of an
identical schedule without changing any candidate's cost or emitted instructions.

### Matmul

Ring epilogues queue virtual VC instances within their owning sysnode. The
number of logical instances does not grant additional sysnodes: only the split
planner or explicit peer-root scope supplies ownership. Local ring storage and
packed-input lifetimes therefore remain eligible for shared chain scheduling.

`matmul.mm_kernel` offers `m`, `n` and every eligible factorization `mn-PxQ` of
the grant. M cuts preserve the row quantum (the class tile for classed kernels);
N cuts preserve packed column groups. N and mixed column cuts require an
independent output-column domain, not a column reduction or row-statistic
output. Batched products also expose `groups`. Each share reads global packed
operands at its origin. Boundary outputs needing a common physical-line owner
use private output and explicit assembly, counted as a conversion.
(code: ktpu-kernels/src/gen/matmul/split.rs:160-611, :204-430)

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
(code: ktpu-kernels/src/gen/matmul/split.rs:431-465, :747-820, :840-890;
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
ktpu-kernels/src/call.rs:2220-2294, :2408-2446)

### Checked quant schedules in typed products

A `matmul.mm_kernel` descriptor may select `quant_backend: "vector_mx7"`.
Straight-line quant operations without an existing deferred status receive
private stage status slices, one per operation. One `quant_check` at the end
of each node schedule consumes those slices (L2.MOV.010, L2.MOV.018).
Existing scheduling attributes are retained; absent attributes select
finite batching, overlap, pipelining, zero classification, streamed axes,
fill overlap and zero reuse. Quant operations inside loops or conditional
regions and operations with an explicit status retain the emitter's policy.
Absence of this option, or `"mover_quant"`, retains the generator's policy;
it does not override a backend already chosen for a packing format.

The policy is shared by ordinary product generation, products in typed chains,
owner-local product stages and distributed product candidates. It runs before
complete-schedule pricing and is part of the split-choice cache identity.
Every node's records are private; neither node IDs nor per-element instructions
are checked at run time by this construction-time transformation.

### Vector rows and destination tiles

Vector `rows` options partition independent rows while retaining each complete
reduction width and global operand strides. Flat vector work uses `tokens`, one
complete VL row per work item, with the last physical extent clipped to the
caller's whole words. Row cuts coalesce adjacent quantums until every output
plane has disjoint line ownership. Row-local chains and fused row stages can
share that partition; other supported row/flat sequences compose distributed
stages with L2 access ordering for their intermediates. `prepared.rows_kernel`
takes the same row partition when every made operand is one relayout whose
source and destination walks lead with the kernel's rows, whose destination
origin is zero and whose element count is given: each share is that
relayout on its own rows (its origin advanced by the share's first row, its
row count and element count the share's), and the relayout's source is a
whole read. Every nonempty local schedule is emitted and priced, including
exact tails and scratch copies.
(code: ktpu-kernels/src/gen/vector/split.rs:89-203, :207-325;
ktpu-kernels/src/call.rs:889-920, :943-1054, :1056-1228, :1245-1301, :1832-2052)

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
(code: ktpu-kernels/src/gen/convert.rs:52-113;
ktpu-kernels/src/gen/convert/destination.rs:20-133, :136-252;
ktpu-kernels/src/gen/transpose/destination.rs:40-137;
ktpu-kernels/src/call.rs:1898-1908, :2295-2346, :2408-2446)

### Logical row boundaries

`convert.rows_kernel` connects whole-word FP16 row domains with different
capacities. `src_rows`, `dst_rows` and `width` define its `src` and `dst` ports.
Expansion initializes the destination padding and copies the valid input
prefix; contraction copies only the destination's logical rows. `classed`
selects fixed mover streams with a node-loop level. A classed initialized
boundary clears the complete destination capacity; an ordinary initialized
boundary clears only rows beyond the input. Explicit `initialize: true`
also supports an equal-capacity input boundary, while `initialize: false`
describes contraction or an ordinary equal-size copy.

Its `destination-tiles` option uses the shared destination-line partitioner.
Each owner fills and copies only its destination rows, with global source and
destination offsets. Padding-only owners have no source reads. Row-size and
byte-offset representability are checked during descriptor construction;
there are no device-time bounds checks. These are the copy and fill operations
of DSL.MOV.001 and DSL.MOV.005, with complete-schedule candidate pricing.

### Pitched row-prefix boundaries

`convert.prefix_kernel` extends the same boundary emitter to a rectangular
prefix of a wider allocation. The FP16 ports have shapes `[src_rows, src_pitch]`
and `[dst_rows, dst_pitch]`. `src_first` and `dst_first` are row origins (default
zero); `width` is the number of columns copied or initialized. `rows` gives the
destination row interval and `copied_rows` its valid input prefix (default
`rows`). Both pitches default to `width`.

Every extent and origin is checked before construction: positive whole-word
row pitches and width, width not exceeding either pitch, representable byte
extents, and source/destination intervals inside their allocations. When fewer
rows are copied than the destination interval, initialization is required.
Ordinary initialization zeros only the padded prefix rows; classed initialization
zeros the entire destination rectangle before the copy. Other rows and columns
are untouched. The source and destination walks have independent byte strides.
There is no device-time bounds test (DSL.MOV.001, DSL.MOV.005).

`destination-tiles` partitions only this destination rectangle using the shared
line-ownership planner. Read regions stop at `copied_rows` and retain source
pitch and global origin; padding-only owners issue no read. The earlier
`convert.rows_kernel` delegates to this emitter with zero origins and both
pitches equal to width, so its default full-row fill/copy schedules are shared,
not duplicated.

### Named prefix stages in fused packages

A fused step `{kind: "prefix", src, dst, boundary}` applies the row-prefix
emitter to named package buffers. `boundary` has the same fields and checked
extents as `convert.prefix_kernel`. The named FP16 allocation specifications
must match those extents. `copied_rows: 0` with initialization represents an
exact fill-only rectangle; it introduces no source read. Initialization that
would erase an aliased copied source is refused with API.OP.001 before emission.

A single public prefix stage uses `destination-tiles` with global origins and
exact read/write regions renamed to its package buffers. Same-allocation
partitioning requires matching row coordinates or disjoint source/destination
intervals. Ordered overlapping copies keep their ordinary local schedule.
Mixed Rows/Flat/Prefix packages use the dependent `rows` planner; each stage
preserves its accesses and only newly overwritten bytes replace prior output
ownership. In-place Rows operands remain reads as well as writes.

This is also the capacity mechanism for normalized products. A prefix loads
logical inputs into the normalization output allocation, complete row stages
normalize it in place, and a fill-only prefix clears any padded rows consumed
by the product. The product reads a checked prefix of that allocation and
writes full output bands directly with a local final tail. No second full
activation allocation or normalization-output copy is introduced. These
staging lifetimes implement DSL.SPEC.008 and L2.PASS.004; prefix transfer and
initialization follow DSL.MOV.001 and DSL.MOV.005. All checks and ownership
planning occur during construction, not per device step or data element.

### Strided plane stages in fused packages

A fused step `{kind: "planes", planes: {...}}` extracts a batched/grouped FP16
plane domain between named public or private FP16 allocations. A public
destination must be writable. The descriptor names
`src` and `dst`, positive `batches`, `groups`, `rows`, `columns`, `padded_rows`,
and source element coordinates `source_offset`, `batch_stride`, `group_stride`
and `row_stride`. Rows after the valid input rows are zero. Strides and columns
are whole words, nonnegative, and checked against the source extent before
emission. Singleton axes never advance and encode a zero byte stride even when
the supplied element stride would overflow a byte conversion.

With `mask: null`, output is `[batches * groups * padded_rows, columns]`.
With a named `mask` input, padded rows also contain whole words and output is
`[batches * groups * columns, padded_rows]`: exact GT4/lane-mask transpose,
using the caller's masks. The transpose emitter is shared with ordinary GT4
transposes (TG.RUN.006), including pitched/global origins. Nonclassed inactive
trip words need no initialization: XOR permutations remain within each word,
and the final GT4 walk reads only extracted words. The lane-mask phase loads
and stores the same private row block: all permutations stay within that block,
and distinct VC instances own disjoint blocks. Its input staging allocation is
also its output allocation; no second permutation buffer is required.

Packages with one common plane domain, public bindings and distinct outputs
expose `planes`.
Each owner extracts, pads and transposes its complete planes, keeping global
output origins. Source manifests enumerate only valid gathered rows; mask reads
are replicated and destination lines have one owner. Other valid packages keep
their ordered local schedule. Private temporary placement uses the package's
`scratch_space`; common disjoint-lifetime pooling can reuse the extraction
buffer for the final transpose scratch (L2.PASS.004). No native-body rewrite
or ownership adapter is involved. These rules apply to every typed plane stage.

### Inverse planes and materialized attention stages

`planes.scatter: true` reverses the untransposed plane transfer. The planar
source is `[batches * groups * padded_rows, columns]`; the offset and strides
now describe its destination rectangle. Only valid rows are copied. It never
clears a public destination, so other batches/heads retain their bytes.
Transposed scatter and source/destination aliasing are refused (API.OP.001).
Scatter and private bindings keep the ordered local candidate until a split
planner proves their complete dependencies; they do not inherit extraction's
independent-plane option.

`{kind: "attention", attention: {...}}` is a named materialized planar stage,
not a projection-specific wrapper. It reads `q`, `k`, `vt` and optional `tail`,
and writes `out`, which may reuse Q after all Q packing. Shapes are governed by
positive `heads`, `queries`, `keys`, `dim`, `query_tile`, `query_cores`,
`key_tile`, `valid_keys`, `block_rows` and `blocked`. Query-core count controls
capacity rounding, not ownership. Complete FP16 score tiles feed FP16 partial
maxima, FP16 global maxima, MX7 probabilities and FP16 partial/global sums;
PV drains FP16 product tiles before reciprocal normalization. Partial reductions
retain increasing key-tile order. A caller tail word supplies the incomplete
key mask. Wholly padded key tiles are entirely masked, not repetitions of the
last partial tile. All accepted blocks must exactly cover the padded query
capacity; malformed domains and checked-extent overflow use API.OP.001.

The typed schedule preserves grouped QK/PV tile sweeps, one-time operand
packing and mask construction, optional sequential head/query blocks, and the
logical query crop after secondary tile padding. Named allocation placement,
lifetimes and outer quant-backend selection use common fused/sequence lowering
(L2.PASS.004). No native generated-body adapter or source-index scratch alias is
part of the descriptor.

Local virtual VC instances use `Trace::par_local` (DSL.CTRL.001): placement may
queue them, but cannot silently expand them across nodes behind a local split
candidate. A dependent attention package currently retains its legal P1
candidate on a wider grant; no multi-node ownership/performance is claimed.
Ordered chunk sequences retain global input/output coordinates and one public
signature, even when their outputs cover disjoint head rectangles.

### Materialization and scheduling policies for grouped attention

The same Attention stage also records `head_group`, `score_tile`,
`incremental_max`, `sum_columns`, `compact_mask` and `direct_quant`. Zero-valued
sizes retain the original defaults; nonzero sizes must exactly divide their
logical domains. A positive head group uses one shared score/probability/product
workspace for successive complete groups. The score contraction tile can differ
from the reduction tile, without another score materialization.

Incremental maxima store the first reduced maximum in FP16, then compare each
subsequent unrounded reduction against the stored previous maximum before the
next FP16 store. `sum_columns` partitions each original FP32 probability value
using register-column views (DSL.VAL.005, DSL.VEC.030), not a reread of MX7
probabilities. Each partial sum is stored in FP16 and the final sum folds partials
in increasing column order. Incremental mode places PV before that sum fold.
Direct packing retains explicit Q/K quantization and separate V planes; the
outer sequence still owns backend selection and finite-status scheduling.

A compact mask is a four-row broadcast loaded once per VC instance. Identical
(tile width, valid-key count, tail input) masks are initialized once per fused
package and retained across all its stages. The tail must be a public input
and may not be any stage's named write destination, including Copy, Rows,
Quant, Prefix, Planes or an attention merge; otherwise construction refuses
API.OP.001. This rule is checked over the whole package, not inferred from a
port declaration alone. Both maxima policies support compact masks.

An optional `merge` carries an inverse-plane descriptor whose source is the
attention output and whose domain exactly matches all head planes. Each completed
head group can scatter immediately, retaining global destination coordinates
and untouched bytes. Blocked query schedules and source/key/value overwrite
aliases are refused. Without it, a following inverse-plane stage performs the
whole merge. A strided scatter range can cross batch boundaries; its source
origin advances in planar coordinates, destination in its own batch/head strides.

`pipeline_lookahead` additionally considers ordinary input copy/fill/GT4/quant
motion across compute regions. It proves whole-allocation RAW/WAR/WAW freedom
after pooling, treats unknown effects and finite-status checks as barriers, and
never selects public output transfers. Both fully decorated complete schedules
are priced; only a non-regressing candidate is retained (L2.PASS.004). There are
no positional slices, new device-time guards or extra transfers. A proved-safe
candidate need not reproduce a legacy order that used aliased live buffers.

### Bulk convolution patch windows

`conv.unfold_kernel` takes a checked `ConvSpec`, `first_batch`, `batches`,
`capacity` and `scratch_space` (`stage` or `dram`). It reads the complete BHWC
input allocation and materializes a contiguous ungrouped batch window into
`[capacity, Kpad]`. Capacity covers the window in complete four-row bands;
unused rows and K lanes are zero. Global input batch origins never become local
row origins. Spatial padding uses one zeroed image allocation, a bounded image
copy and the native five-level patch walk for contiguous kernel columns.
Dilated columns retain a separate tap level. All extent/byte-stride arithmetic
is checked before emission; non-advancing axes use zero strides. The shared
ConvSpec geometry supplies both this bulk path and destination-owned patches.

This typed stage deliberately retains its local candidate; a chain may compose
it with independently planned products without claiming that halo reads are
independent patch owners. Batch-chunk sequences keep global output/residual views
and one public interface. Native local convolution records its stage-pressure,
chunk-size and ring policy in descriptors; common chain/sequence lowering owns
composition and disjoint-lifetime stage reuse (L2.PASS.004). There is no native
body concatenation, scratch-space rewrite or graph-name-based adapter. The bulk
fill/copy behavior follows DSL.MOV.001 and DSL.MOV.005; invalid domains use
API.OP.001. Validation occurs at construction, not per pixel or instruction.

### Ordered typed sequences

`kernel.sequence` takes a nonempty `members` array of typed calls with identical
ordered public signatures. It preserves member order, including overlapping
writes; it does not infer independent output owners merely from concatenation.
Public views remain global. Private buffers, values and loop symbols receive
collision-checked scopes from the same emitter used by row-share composition.
The emitted scope maps are also used by split-region manifests, rather than
reconstructing names from an assumed prefix.

Select `quant_backend` on the outer sequence. Members must leave that policy
at its generator default; a member-level override is refused with API.OP.001
rather than silently retaining an earlier check. Already explicit status
lifetimes retain the existing quant policy's semantics. The outer policy is
applied before shared-lifetime pooling and complete-schedule candidate pricing.
The local `p1` option preserves ordered writes. The dependent `rows` option
plans each member on the grant, retains their order, and merges final output
ownership by subtracting overwritten ranges before adding the later writer.
It does not assume members are mutually independent. Public signatures must
agree. Collision-selected scope maps and accepted storage aliases are applied
to member manifests before composition; scratch/status regions are then rebuilt
from the emitted node scopes. The complete emitted graph supplies node budgets.

Sequence-wide private stage pooling applies at both P1 and multi-node selection.
The distributed form uses equal-capacity aliases so its already-physical byte
regions remain valid. All read/write manifests follow the returned aliases;
status scheduling precedes candidate pricing. P1 may additionally use the shared
allocator's checked resizable capacities.

### Optional pipeline-tail lookahead

`pipeline_lookahead: true` requests the same construction pass for typed chains
and sequences. After storage placement, a pipelined loop's last iteration is
examined with that loop variable fixed to its final value. Statically inactive
conditions contribute no accesses; unknown conditions retain every access.
Consecutive Copy/Fill/Quant statements may move before that final iteration
only when their allocation read/write sets have no RAW, WAR or WAW conflict.
Shared read-only allocations do not create a dependency. Quant status views
are writes, and an additive drain both reads and writes its destination.
Checks, stream receives, sends, remote drains, sticky regions and opaque mapped
accesses are barriers. The allocation-level rule does not infer disjoint
subregions of an aliased buffer. Tail effects are analyzed once; candidate mover
effects accumulate without repeatedly traversing the product.

The public construction-time `chain::independent_tail` proof is used by the
scheduler and existing stage witnesses. It assumes already-valid graph regions;
it does not replace view/type validation. In particular, a next-input quant
cannot overwrite a packed activation allocation still read by the final product
wave, even if all other source/destination buffers are distinct.

The loop is split into prefix and final iteration with fresh bound definitions;
references to enclosing node/pipeline variables are preserved. Both complete
schedules are priced, and an unlowerable or slower candidate is not selected.
This mover-hoist rule adds no mover, value computation, rounding point or
runtime condition.

Ready compute regions may also advance across consecutive independent private
transfers (L2.PASS.004). The proof uses the whole compute region, including all
pipeline iterations, and the same RAW/WAR/WAW, status and opaque-effect rules.
It never crosses another compute region or a public-output transfer. This
permits an earlier contraction to start while transfers for a later contraction
remain pending. Input-hoisted and ready-compute schedules are both decorated
with the chosen quant policy before scheduled-L1 pricing; ready-compute is
selected only when both prices exist and it is no slower. The pass runs only
at construction and adds no operation, storage or runtime guard.

The same policy can split a status-free full quantization into three packed
column windows around the producer's final wave. After lifetime aliasing, the
compiler proves that the last wave reads only an activation prefix and writes
only the final output columns. It quantizes completed middle columns before
that wave, then quantizes the activation prefix and final output columns after
it. Each cut must align to the destination's full `nk * 32` packing group; the
destination must use the standard positive `gm`/`nk` MX7a layout. All rows are
retained, and every element is quantized once.

Coverage is proved from normalized affine coordinates, not merely a bounding
interval: run strides must leave no holes, row and column variables must be
independent, and nested loop bounds must be constant. A modulo is removed only
when its quotient is invariant over the whole domain. Repeated materialized
pointwise stores may cover the same rectangle and must finish before the early
quant. Guards, mapped/keyword-bearing producer values, explicit quant statuses
and nonstandard layouts retain their existing schedules. No FP16 rounding point
is removed. There are three mover operations in place of one, so selection still
uses the full emitted schedule, not a compute-only estimate.

Storage and ordering are competing complete schedules for both chains and
sequences. A legal pooled allocation is constructed before pricing even when
pooling alone is slower: its aliases can enable packed-column windows. Separate,
pooled, ordered-separate and ordered-pooled schedules compete against the
existing decorated-and-pooled baseline. Selection uses scheduled L1 cycles,
including transfers, dependencies and status work; an intermediate allocation
price cannot discard a potentially cheaper complete ordering.

For sequences selecting a vector quant backend, pending quants are reordered
before compiler-owned status records are assigned. Each resulting window owns
one record and the node scope retains its final check. The selected storage
alias map follows the chosen graph into distributed manifests. Existing
explicit statuses are never stripped or subdivided by this mechanism.

Member order is the dependency baseline across reused allocations
(L2.PASS.004). A next member's quantization cannot overwrite live packed
activations, and its producer cannot overwrite a ring still consumed by the
previous member. Storage sharing does not authorize motion across those
accesses. Independent mover prefixes remain eligible for the same lookahead
proof; all conflicting work retains its dependency order.

Native generated MLPs retain typed chains for individual products and their
materialized FP16 activation. Stage-bounded row chunks are ordered
`kernel.sequence` members with global input/output prefixes, including the
two-chunk case. Their complete local schedule is priced against correct
materialized stage order, not against an ordering that overwrites live data.

### Typed chain interfaces and capacity residency

A `kernel.chain` descriptor may declare an ordered `interface`. Each entry is
`{"side": "first" | "second", "port": <constituent name>, "name": <public name>}`.
Every remaining public port appears exactly once. Connected private ports are
not public. The compiler applies one hygienic rename to the graph, signature
and split regions; private buffers, values and loop variables cannot collide
with the selected public names. Both constituent port maps remain available
for nested descriptor planning. Interface validation uses API.OP.001.

The optional nonnegative `stage_budget` gives a local chain's private-capacity
placement budget in bytes (DSL.SPEC.008, L2.PASS.004). Explicit stage allocations
consume the budget first. Remaining `@mem` allocations are assigned in declaration
order to stage while they fit, otherwise to DRAM. Shared multi-node edges retain
the dependent-stage planner's explicit DRAM residency. A chain-level
`quant_backend` applies the checked-quant schedule after placement and before
complete-schedule pricing.

### Logical prefix views of private capacities

`kernel.chain` may specify `link_view: "prefix"` when a logical FP16 input
occupies the upper-left rectangle of a larger two-dimensional output allocation.
Both extents must be positive and byte-representable, the logical rectangle must
fit, and neither specification may carry a layout transform. The original
consumer graph is validated through the normal graph/L2 layer before widening
its port, so a larger allocation does not hide an invalid logical access.

Whole axes are bounded to the original logical extents; explicit row/column
coordinates keep their meaning while the backing row pitch becomes the
capacity pitch. Only abstract loads and quantization reads without read maps
or byte walks can use this binding. Reshapes, new axes and writes are refused
with API.OP.001. An unspecified `link_view` retains exact-link semantics.

Ordinary, owner-local and dependent distributed chain construction use the
same binding. Distributed source read intervals are split at logical row
boundaries and rebased to the new physical stride, without claiming row gaps.
A changed consumer invalidates any previously lowered L1. The complete composed
sequence is then priced from one scheduled L1 emission, using its actual node
budgets rather than summing stale constituent prices. The L2 local-budget API
shares the authoritative makespan pass and its whole-module memory policy;
its maximum plus launch equals the unchanged makespan model. All of this is
construction-time work, with no added device transfer or condition.

A complete, uninitialized input-prefix copy consumed by one top-level unmapped
quant can become a bounded read of the original public input (DSL.MOV.001,
L2.PASS.004). The source and private destination may have different row pitches.
Every quant axis must lie within the copied rectangle; whole axes on a wider
capacity do not imply this proof. The read retains its logical column bound and
adds the source row origin. The private copy and allocation disappear. Partial
initialization, repeated reads, writes and unproved accesses keep their ordinary
materialized schedule. The proof runs at construction, not per device step.

### Materialized pointwise stages in product waves

`kernel.chain` accepts `materialized_pointwise: true` for an unkept independent
FP16 product followed by a one-input, full-row elementwise vector stage. The
input and output coordinates, pitch, dtype and physical domain must agree;
reductions, offset views and extra consumers are not eligible. This is a
scheduling policy, not expression fusion (API.ROW.002).

The product's FP16 store remains. A pointwise consumer can unpack that exact
stored FP16 pack within the producer's existing guarded VC stream. Its arithmetic
feeds a second FP16 pack and the consumer output store. The first store supplies
the packed readback destination; it is not an unrounded FP32 expression
substitution. L2's packed readback synchronizes the pack and unpack using that
L1 destination (DSL.VEC.002, DSL.VEC.017, L2.OP.025). Source type, GT4 mode and
full tile extent must match the consumer load. The producer's assignment,
guards, stream loop, global origins and ring remain unchanged. Generated
consumer value names are fresh; no free producer index is renamed.

A separately dispatched wave is also a candidate. Its consumer uses ordinary
row-stage decomposition (API.ROW.004): complete trips followed by a smaller
whole-quantum trip when needed. Each stage has its own prologue and one final
stream loop (L2.VC.001, L2.VC.018), retaining the producer assignment and guard.
Its materialization can be in place; the full trip width need not divide the
producer tile. Non-wave producers admit sequential in-place materialization.

The separate composition, dispatched wave and packed-readback stream compete
on complete scheduled-L1 prices, including legality and capacity. A consumer
is embedded only when every producer store is covered and the private edge has
no other producer read. The packed-readback form retains the first private
edge and removes the consumer memory reload and separate VC dispatch. The
wave form retains the reload and can eliminate that edge. Stream receives are
not moved or duplicated, and neither form merges the FP16 rounding points.
The rule adds no device bounds checks, polls or launch-boundary checks.

Ordinary chain construction and `row-local` owner construction use the same
lowering before stage pooling. The optional `link_space` (`stage`, `dram` or
`mem`) places an unkept local link explicitly; cross-node dependency edges
retain the distributed planner's DRAM placement. Validation, affine rebasing,
value hygiene and candidate pricing are construction-time work.

### Direct input-prefix quantization

An unkept chain whose first member is a complete, ordinary contiguous prefix
copy can expose that source interval directly to a single top-level Quant
consumer. The compiler checks matching FP16 source/edge specs, one edge read,
no writes to either source or private edge, no read map, and bounded row-view
rebasing. A full-row axis becomes the source's bounded global row run; explicit
row coordinates receive a checked offset. The copy and private activation
allocation disappear; quantization and its rounding are unchanged.

Padded prefixes, opaque reads, node-wrapped schedules, repeated loads and other
consumer patterns retain their original copy. In particular, this rule does
not exchange a repeatedly read stage value for repeated DRAM accesses. It
removes a transfer before one quantization without adding a device-time branch,
read, or bounds check (DSL.MOV.001, L2.MOV.010). Ordinary chain generation and
singleton dependent-stage selection use the same transformed local graph.

### Streaming a logical output prefix

An unkept typed chain ending in an ordinary, non-initializing
`convert.rows_kernel`, or a contiguous zero-source-origin
`convert.prefix_kernel`, can contract its copy into the producer's row-band
schedule. The compiler proves one private output store, no other consumer of
that edge, a pure VC region without stream receives, and a constant zero-based
row loop. The store's final affine row term must name that exact loop variable
with the band height as its coefficient. Interval analysis proves every other
row coordinate nonnegative, capacity-bounded and aligned to the producer tile;
the column interval must also span the declared width.

Complete-capacity prefixes store directly at the destination's global row
origin with no copy or tail allocation. For a shorter logical prefix, complete
bands likewise store at that global origin. The final partial band
stores into a one-band stage buffer and the mover copies only its logical
prefix. A single producer row tile needs no new conditional. Multiple tiles
select full and final band loops with scalar conditions on the existing tile
coordinate; there is no per-value check or additional bounds-memory access.
The expression, FP16 pack, input views and product/quant rings are unchanged
(API.ROW.002, DSL.MOV.001). Other producer/copy schedules keep their ordinary
composition; the rule does not reject them.

Ordinary chain generation and single-node dependent-chain selection use the
same optimized local graph. The latter rebuilds its private-region manifest
from that graph and prices the complete schedule, including the final copy.
Distributed stage sequences retain their existing global dependency edges and
destination-copy ownership.

The generated native linear factory expresses its input/residual capacity
boundaries, product and output prefix as these typed descriptors. Direct
products retain a matmul descriptor. Both use the eager device-grant planner;
there is no application-side ownership planner or body rewrite in this factory.

### Row-local contraction chains

The `kernel.chain` option `row-local` co-partitions eligible products and row
stages onto the same owners. An eligible product reads ordinary FP16 A, has one
batch with no M padding, writes contiguous zero-origin full rows, and has no
shape-class, prepared input or demanded output packing. Its row domain must
match the other stages, and every private dependency must read the row written
by its producer. Other chain options retain their dependent-stage schedules.
`row-local` is explicitly selected; it is not the automatic dependent-stage
planner's option ID.

Each owner uses the existing contraction emitter, including its quant and
product rings. The shared packed B keeps its original N tile, K chunk and
layout. A is quantized into that owner's local M tile. Extra operands and
public outputs retain global row origins. A common row cut is a multiple of
both stages' physical quantums, not merely the larger quantum.

A zero-origin, contiguous private edge whose accesses all name explicit row
slices is rebased into an owner-local allocation. Whole statistic words remain
allocated. Whole-buffer accesses and packed edges retain their declared
allocations. The transformation changes buffer origins, not arithmetic or
FP16 materialization boundaries (API.OP.007, L2.PASS.004). Kept producer outputs
remain public and are never compacted. A one-node node-0 plan retains the
ordinary unsplit graph. Every multi-node owner's complete local chain is priced
through `graph::cycles` and `LocalTiming`.

### Private stage reuse

Typed chain and fused-package lowering pools private `@stage` buffers with
disjoint lifetimes on the same physical sysnode (L2.PASS.004). Ports and unknown
or mixed-owner allocations are not pooled. Each root loop, including all its
pipeline iterations, is one lifetime interval. The dtype, rank and layout must
agree. Equal capacities share directly; a larger capacity may contain a smaller
one when both use view-derived addressing. All omitted or whole axes are then
bounded to each original occupant's extent before its references are renamed.
Raw walked, mapped and other non-view-derived accesses keep their original
capacity. Different layouts never share an allocation.

Reuse is visible as graph buffer identity before L2 dependency scheduling.
Conflicting reads and writes therefore retain RAW, WAR and WAW dependencies,
including between distinct pipelined loops. The complete separate and pooled
schedules are priced with `graph::cycles`. A pooled candidate is selected only
when it lowers and its cost is no greater. Candidate infeasibility does not
refuse a valid separate schedule. The selected graph preserves each FP16
materialization and quantization boundary; it does not fuse arithmetic across
those boundaries.

### Complete folds in fused row packages

A `fused.kernel` Rows step with `fold: true` computes each expression's complete
row reduction, rather than exposing independent column-block partials. Each
expression contains one reduction and reads ordinary operands. The reduction
uses the same emitter as `reductions.wide_fold_kernel`: FP16 block partials,
opposite-half pairing, odd-middle carry and an FP16 store at each fold level
before the surrounding expression (API.ROW.003). Input and output operand
offsets retain their global origins when independent rows are distributed.

A package's optional `scratch_space` applies to all its private allocations,
including member temporaries (DSL.SPEC.008). Without it each generator retains
its own placement. Independent row shares retain private statistics on their
own sysnodes. In a dependent-stage split, allocations shared between stage
owners use global DRAM; the policy still applies to each member's private
temporaries. `balance_stages: true` assigns successive top-level
singleton VC regions to successive local cores, restarting the cursor at each
explicit sysnode scope. It does not reorder the stages or change their accesses;
L2 instance dependencies still derive from those accesses. These options are
part of the descriptor, so specialization and split generation use the same
local schedule.

The generated native LayerNorm descriptor uses two complete folded Rows steps
(mean and reciprocal standard deviation), followed by affine Rows, with private
statistics and temporaries in stage memory. The independent-row split keeps
both FP16 statistics on the row owner's sysnode. Narrow normalization uses a
single Rows descriptor. Eager specialization supplies the current device grant
through the ordinary generated-template invocation path.

### Convolution and GroupNorm

Convolution exposes `spatial` (matmul M), `channels` (matmul N, when output
columns are independent) and, for grouped convolution, `groups`. Patch production
and contraction are composed over the same global patch allocation. Channel
splits use a row-distributed patch producer; spatial/group splits produce the
patches their product shares consume. The selected makespan is the complete
patch/product graph cost, with its cross-node dependencies, not the product
price alone. Forcing selects among these family IDs.
(code: ktpu-kernels/src/gen/conv.rs:256-473, :439-473)

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
ktpu-kernels/src/gen/attention/flash_split.rs:206-368)

All three recorded generators have executable adapters: `attention.flash`,
`attention.flashd` and `attention.srflash`. They accept `heads`, `queries` and an
optional `contract`; keys are 1024 and Dk=Dv=64, with unmasked MX7 scores. Admission
allows 1..10 heads and whole motif query groups up to 1024 rows. Flash uses
sticky-overflow redo; flashd requires the caller's `unit-scores` contract and
uses a direct score ring; srflash uses SR row state. Four distinct MG roles are
required, plus two VCs for flash/flashd or two SRs for srflash. These resource
checks do not establish numerical eligibility for arbitrary attention inputs.
(code: ktpu-kernels/src/call.rs:2125-2164;
ktpu-kernels/src/gen/attention/motif.rs:57-161;
ktpu-kernels/src/gen/attention/flash_split.rs:214-240)

Adapters substitute ownership coordinates, external addresses and pipeline
bounds into the hand programs, retaining their local rings, banks and instruction
streams. Flash scratch per logical rank is 1 MiB of scores, 64 KiB of partials
and 16 KiB of sums/offsets; flashd uses the latter two; srflash uses 256 KiB of
partials. The SR adapter emits at most two heads per pipeline and whole 256-row
normalization groups. Each physical node has an explicit scope, and successive
pipelines have private symbols with node-local scratch. `PlannedKernel.l1` is
the executable; the accompanying graph supplies port metadata, not a replacement
executable. (code: ktpu-kernels/src/gen/attention/flash.rs:59-131;
ktpu-kernels/src/gen/attention/flashd.rs:12-57;
ktpu-kernels/src/gen/attention/srflash.rs:12-60;
ktpu-kernels/src/gen/attention/motif.rs:65-79;
ktpu-kernels/src/gen/attention/flash_split.rs:127-198, :360-368)

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
:63-99, :160-172)

Standalone Python template factories retain a logical `TemplateCall` alongside
an inlineable Python body. Device specialization validates the declared specs
and constants, then uses the same generated invocation path; the specialization
cache includes the grant and forced option. Explicitly authored DSL kernels
retain their ordinary tracing path.
(code: software/kohakutpu/application/kohakutpu/application/kernels/template.py:32-57;
software/kohakutpu/application/kohakutpu/application/runtime/device/kernels.py:133-194)

`KTPU_SPLIT_<GENERATOR>_OPTION` forces a family option, with dots replaced by
underscores and the generator uppercased: for example
`KTPU_SPLIT_MATMUL_MM_KERNEL_OPTION`, `KTPU_SPLIT_ATTENTION_FLASH_OPTION` or
`KTPU_SPLIT_GROUPNORM_KERNEL_OPTION`. This takes precedence over
`KTPU_SPLIT_OPTION`, which applies only to `matmul.mm_kernel` and `conv.kernel`.
Absent forcing, each planner chooses among its eligible candidates as described
above. (code: software/kohakutpu/application/kohakutpu/application/runtime/generated.py:89-99)

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
:126-149, :446-485;
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


### Python graph carrier ownership

`kernels.template.GraphKernel` is the existing custom-call carrier for a
generated graph and optional retained TemplateCall. It preserves ordered typed
ports, selection metadata and the trace/check protocol; it performs no body
composition or rewriting. Device specialization follows the retained call and
ordinary grant/cache path. The obsolete `native.composition` module is absent,
not a second ABI or forwarding compatibility surface. Independent frozen graph
oracles live only in test support. This relocation adds no device operations.
