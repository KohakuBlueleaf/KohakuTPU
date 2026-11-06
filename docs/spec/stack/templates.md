---
title: Offline template verification
summary: Offline proof that every in-box binding of a template kernel is legal, and the parameter confinement it rests on.
tags:
  - spec
  - stack
---
# Offline template verification

## Kernel split options

A logical kernel receives an ordered `NodeSet` of physical sysnode IDs, checked
against `Machine::nodes()`. The generator's `Hw.nodes` and the tracer's existing
`Facts.nodes` read that same fact. Local MG/VC counts remain per-sysnode counts.
`SplitRequest` retains the consumer form/scale requirements, input forms,
numerical contract, dispatch profile and optional forced option ID.

`SplitOption` describes the axis, participant count, active nodes, tile shares,
output specs/physical ownership, symbolic events, legality witness and cost.
Physical regions are byte runs over canonical allocation identities, not logical
bounding rectangles. `validate_ownership` checks distinct writers at 64-byte
line granularity and exact coverage of the valid output. Bindings must resolve
aliases to those canonical identities before checking. This is an ownership
check, not an all-agent happens-before or numerical certificate.

`partition_tiles(T,P)` uses the vector scheduler's `balanced_cut`: with
`q=T/P`, `r=T%P`, boundary `p*q+ceil(p*r/P)`. Empty shares are retained in the
plan and `emit_shares` emits no work for them. `Trace::on_node` emits an explicit
node scope for the granted physical ID; it does not infer nodes from unit counts.

`call_split` is the planning entry; `call` retains its existing interface and
path. Graph generators expose the `p1` compatibility option, explicitly
placed on the first granted node, with `active_nodes=1` and the other granted
shares empty. Kernel construction scopes the generator body before local unit
pars are traced, so automatic unit-count expansion cannot escape that node;
composite calls retain the same construction scope. Machine facts are unchanged.
The plan retains the original request and producer tensor specs;
its physical region lists remain unbound. Consumer conversion and distributed
family options are not implemented by this compatibility option. It admits the
producer's form and unchanged generator numerics; an incompatible required form,
explicit scale groups or a different precision/reduction order has no matching
option and is refused. Input-form labels and mapped-read capabilities remain
request metadata; generator arguments continue to determine the actual reads. A split plan
is not a certificate for a new binding, topology or consumer form.

`CostModel::estimate` prices options. `LocalTiming` uses each complete share's
existing L2/L1 timing and the machine's `vc.launch_front` intercept once per
sysnode, taking the slowest share rather than dividing a full-kernel time by P.
The intercept already present in `graph::cycles` is removed before assembling
features. Conversions and event waits belong in those complete local schedules.
Estimates carry revision/provenance, `uncertainty="unfitted"` and
`measured=false`. `choose_split` orders by makespan, conversion count, event
count and option ID; forcing still prices the selected option.
`composed::split_sequences` compares dependent sequences of these makespans,
including explicit conversion kernels and the machine's inter-launch gaps;
it does not add the parallel sysnodes' costs together.

Code: `software/kohakutpu/compiler/crates/ktpu-kernels/src/split.rs`
(`NodeSet`, `partition_tiles`, `validate_ownership`, `emit_shares`),
`software/kohakutpu/compiler/crates/ktpu-kernels/src/split/cost.rs`
(`CostFeatures`, `LocalTiming`, `choose_split`),
`software/kohakutpu/compiler/crates/ktpu-kernels/src/call.rs` (`call_split`),
and `software/kohakutpu/compiler/crates/ktpu-kernels/src/trace/control.rs`
(`Trace::on_node`). Evidence: implementation and staged behavioural witnesses;
no device result is asserted.

## Attention ownership

`gen::attention::AttentionSpec` records flattened batch/head count, independent
query and key lengths, key and value widths, mask semantics and score precision.
`assignments` enumerates head cuts, query-tile cuts per head, every head/query
factor pair of the grant, and balanced contiguous flattened head/query-tile
ranges. Each span carries global query coordinates, the complete key range and
its head index. Empty participants remain in the assignment. The query ownership
unit is supplied by the caller; valid endpoints exclude query padding.

Assignments are logical ownership records, independent of physical packing.
The `attention.flash` recorded generator is available through `call_split`
with an empty argument object. It uses the reference template's ten heads,
1024 queries and keys per head, and Dk=Dv=64. Q is pre-scaled by
log2(e)/sqrt(64). It does not implement masking or precise-score attention.
The eager API does not select this generator.

The planner converts assignments to physical `SplitOption` records. IDs are
`heads`, `query-rows`, `heads-query-PhxPq` (with numeric factors), and `balanced`.
Every share reads its Q rows and all K/V of its heads and writes its Y rows.
`validate_ownership` checks output lines before `choose_split` prices each
complete local L1 schedule with the existing timing model. A forced option
still undergoes validation and pricing. The returned `PlannedKernel.l1` is the
authoritative executable; its `kernel.graph` holds port metadata only and must
not be used as the executable. Graph generators return `l1=None` and retain
their existing graph path.

The flash adapter reads the original template text and substitutes only loop
bounds, head-transition periods and address expressions. For a share with
`nq` query tiles per head, local score tile T maps to head `h0 + T/nq` and
query tile `q0 + T%nq`; the PV head period is `4*nq`. Local rings, bank
selection, waits, marks, vector instructions and redo remain unchanged.
The warm-up emits min(4, score tiles) and the close emits min(5, PV tiles).
Scratch slots per logical rank occupy 1 MiB of S, 64 KiB of O partials and
16 KiB of sums/offsets; the corresponding L1 parameters cover all slots.
Each granted physical node has an explicit `node N` scope. Adjacent head spans
with the same query range share one pipeline; other balanced spans use
successive pipelines with private symbol names and the same node-local slots.

Code: `software/kohakutpu/compiler/crates/ktpu-kernels/src/gen/attention/flash.rs`
and `software/kohakutpu/compiler/crates/ktpu-kernels/src/gen/attention/flash_split.rs`.
The L1 simulator witness in `software/tests/stages/l1_l0/attention_flash.rs`
compares P=1 with the original hand template and forced P=2/4 head, query and
hybrid splits with P=1, byte for byte. Its memory write census observes every
output byte written exactly once. This is an authored witness, not a claim of
a measured result or a certificate for other attention contracts.

The hand L1 programs have distinct execution contracts. `flash` uses four MG
roles, two vector cores, a four-score-tile DRAM ring and eight-tile partial-state
rings, with sticky-overflow redo. `flashd` streams scores with credits and
closing drains; its lazy-offset path does not provide that redo. `srflash` uses
two MG pairs and two SR units. Their inner programs remain unchanged. Only
flash has an executable adapter; flashd and srflash retain resource contracts.
A logical assignment is not evidence for mapping four live MG roles onto two MGs.

`AttentionMotif::bind_roles` checks these resource requirements before returning
separate score and product MG bindings. It refuses fewer than four MGs rather
than aliasing roles. The SR motif requires two SRs, while the vector motifs
require two VCs. This resource contract alone does not establish numerical or
schedule eligibility and is not yet called by eager selection. Its packing and
state metadata describe the hand streams, not arbitrary shapes.

Code: `software/kohakutpu/compiler/crates/ktpu-kernels/src/gen/attention/motif.rs`
(`AttentionMotif`, `bind_roles`, `pack`, `state`). The DSL-stage role witness
checks capacity refusal and distinct live MG bindings.

Code: `software/kohakutpu/compiler/crates/ktpu-kernels/src/gen/attention.rs`
(`AttentionSpec`, `assignments`); ownership witness:
`software/tests/stages/dsl/attention.rs`. Hand programs:
`software/kohakutpu/language/kohakutpu/language/examples/l1/fused/flash.ktpu`
(`redo`, `scores`, `pv`, `flash.l1`),
`software/kohakutpu/language/kohakutpu/language/examples/l1/fused/flashd.ktpu`
(`srow`, `scores`, `pv`), and
`software/kohakutpu/language/kohakutpu/language/examples/l1/sru/srflash.ktpu`
(`scores`, `soft`, `pv`, `norm`). No performance result or numerical certificate
is asserted by this ownership interface.

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
