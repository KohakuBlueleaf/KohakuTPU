---
title: FP16-to-MXFP7 conversion backends
summary: The machine fact `conversion.fp16_mx7` is a versioned reliability contract, independent of vector-core geometry and mover transform support.
tags:
  - spec
  - stack
  - numerics
---
# FP16-to-MXFP7 conversion backends

The machine fact `conversion.fp16_mx7` is a versioned reliability contract,
independent of vector-core geometry and mover transform support.
`allowed_backends` controls admission; `preferred_backend` supplies the default.
An explicit operation backend selects an allowed backend. Without one, an
`nk >= 2` destination selects vector packing; a read map or in-place padding
also selects vector packing when allowed. Read maps and in-place padding
with a non-vector backend, disallowed backends, and mover quantisation into
paired destinations are refused. (code: software/kohakutpu/compiler/crates/ktpu-l2/src/lower/mover/packing.rs:20-85)

The policy permits only vector packing on multimesh_v9t6; its default permits
both backends and prefers mover quantisation. Environment or explicit
`machine_of` backend selection changes the preference without widening the
allowed list or overriding operation selection. A truthy explicit backend
argument takes precedence over the environment. (code: scripts/py/machine/conversion.json:2-20; software/kohakutpu/application/kohakutpu/application/runtime/machine.py:76-95)

Receiving-board reliability admission is independent of optional checker mode.
Ordinary packages use the receiving card's serialized machine; launch recipes
must match machine and sysnode placement. Admission verdicts cache package bytes,
receiving machine, placement and bindings. Disallowing vector MX7 invokes lifted
schedule verification; disallowing mover quant invokes command execution analysis
with a dynamic-proof fallback. (code: software/kohakutpu/application/kohakutpu/application/runtime/packages.py:194-205, :229-246, :250-262; software/kohakutpu/compiler/crates/ktpu-verify/src/lines.rs:550-569)

## K pairs and the quantiser (RUL.099)

NUM.MX7.008 and NUM.ACC.008 describe paired-block zero handling.
A quant destination with nk >= 2 selects vector_mx7 unless explicitly overridden;
an explicit mover backend is refused and the selected backend must be allowed.
PAIRING defaults to unpaired: on a mover-preferring machine that disallows pairing;
vector permits pairing, as does either setting on a non-mover-preferring machine.
Permission does not guarantee nk=2 for every contraction. (code: software/kohakutpu/compiler/crates/ktpu-l2/src/lower/mover/packing.rs:20-85; software/kohakutpu/compiler/crates/ktpu-machine/src/pairing.rs:12-42)

## Finite input contract

The image unpacks inputs, waits, applies enabled zero lifting, classifies when
checked, then packs. Zero lifting uses vector selection before classification.
(code: software/kohakutpu/compiler/crates/ktpu-l2/src/lower/mover/packing/image.rs:178-215; software/kohakutpu/compiler/crates/ktpu-l2/src/lower/mover/packing/finish.rs:27-48)

Checked vector packing classifies unpacked values before VPACK. When zero
lifting is enabled, it replaces zero lanes before classification. The comparison
classifier computes absolute values, compares them with 65536, writes invalid-lane
markers under predicates, then feeds those markers to unpredicated EXP2D. The
alternative classifier multiplies values by zero and feeds the result to EXP2D:
finite inputs produce zero; infinities and NaNs produce NaN. Classification uses
temporary registers; zero lifting does modify the numerical register beforehand.
(code: software/kohakutpu/compiler/crates/ktpu-l2/src/lower/mover/packing/image.rs:190-215;
software/kohakutpu/compiler/crates/ktpu-l2/src/lower/mover/packing/finish.rs:27-48, :55-101;
src/kohakutpu/vector2/v2_alu.v:337-348)

Checked quantisation drains one status record per participating core. Inline
checking barriers before loading/asserting records; deferred checking consumes
pending records at quant_check, and ending with unchecked records is refused.
Nonzero assertions produce CONTRACT (0x24), detail 1; card collection translates
this to NonfinitePackingError, a NodeError subclass. Device collection records
failure, discards later in-flight results and refuses subsequent device access;
it does not publish a successful Run. This does not cancel already published
hardware work or prohibit raw card reads and diagnostic dumps.
(code: software/kohakutpu/compiler/crates/ktpu-l2/src/lower/mover/packing.rs:98-138;
software/kohakutpu/compiler/crates/ktpu-l2/src/lower/mover/quant_status.rs:160-236;
software/kohakuaccel/firmware/package/interp.c:1622-1626;
software/kohakutpu/application/kohakutpu/application/runtime/memory/packing.py:14-32;
software/kohakutpu/application/kohakutpu/application/runtime/card/collection.py:126-135, :175-190, :244-265;
software/kohakutpu/application/kohakutpu/application/runtime/device/submission.py:42-61, :410-462;
software/kohakutpu/application/kohakutpu/application/runtime/device/allocation.py:227-238)

Checking classifies supplied image values including generated padding, not all
underlying storage. Entirely zero tiles issue no source descriptor or DMA;
partial-row tiles describe only valid source rows. Storage outside the live view
is not necessarily overwritten. (code: software/kohakutpu/compiler/crates/ktpu-l2/src/lower/mover/packing.rs:811-826; software/kohakutpu/compiler/crates/ktpu-l2/src/lower/mover/packing/image.rs:190-215)

For vector quantisation, only integer finite=1 suppresses checking; deferred
status with finite=1 is refused. This is caller proof, not tensor-value analysis.
The ordinary unmapped vector path refuses checking under conversion version 1
and checks contract-status/register/descriptor/EXP2D capabilities for version 2.
(code: software/kohakutpu/compiler/crates/ktpu-l2/src/lower/mod.rs:182-221;
software/kohakutpu/compiler/crates/ktpu-l2/src/lower/mover/packing.rs:211-242)

## Vector schedule

`vector_slots`, `vector_cores` and `vector_looped` are machine facts.
Explicit non-None packing_slots, packing_cores and packing_looped arguments
override KTPU_MX7_SLOTS, KTPU_MX7_CORES and KTPU_MX7_LOOPED. Overrides are
converted to integers; looped mode accepts only 0 or 1. Machine parsing accepts
one or two slots and a nonnegative core count; packing refuses a resolved count
outside the available vector cores. (code: software/kohakutpu/application/kohakutpu/application/runtime/machine.py:97-112;
software/kohakutpu/compiler/crates/ktpu-machine/src/lib.rs:266-277;
software/kohakutpu/compiler/crates/ktpu-l2/src/lower/mover/packing.rs:339-345)

The ordinary scheduler groups adjacent entries into source-row bursts and partitions
stream groups contiguously: core v receives floor(groups*v/cores) through
floor(groups*(v+1)/cores), with core count capped by stream groups. Full entries
contain four rows of two FP16 words and drain four MX7 words. Partial entries
load live rows and words only and zero remaining lanes; entirely zero entries
issue no source fill. (code: software/kohakutpu/compiler/crates/ktpu-l2/src/lower/mover/packing.rs:609-651, :752-760, :810-836;
software/kohakutpu/compiler/crates/ktpu-l2/src/lower/mover/packing/image.rs:96-134, :352-356)

Each software L1 slot reserves 128 input words and 64 output words; slot two
begins at word 192. Two-slot images prefill the next slot, with completion waits
protecting reuse and an epilogue handling the remaining pair and any odd final
single batch. Batch size also depends on geometry, descriptor increments and
instruction room. The normal cap is sixteen entries, partial entries cap at four,
and checked two-slot partitions of at least fourteen groups per configured core
cap at seven. stream_batch or zero_reuse for entirely zero pieces selects the
sixteen-entry cap. Contiguous grouping has separate caps, and image-size pressure
reduces batch size or grouping. A steady loop exceeding skip displacement can
be specialised on whether it executes rather than always imposing a seven-entry
cap. (code: software/kohakutpu/compiler/crates/ktpu-l2/src/lower/mover/packing/image.rs:61-93, :308-341;
software/kohakutpu/compiler/crates/ktpu-l2/src/lower/mover/packing.rs:622-651, :677-691, :728-750, :770-789)

With zero reuse enabled and no valid input rows, packed-zero output is prepared
once and the immutable slot is drained repeatedly. (code: software/kohakutpu/compiler/crates/ktpu-l2/src/lower/mover/packing/image.rs:218-259)

With vector_looped enabled, ordinary packing emits sysnode counters and affine
source/destination offsets for outer axes 0, 1 and 3. Otherwise it expands those
axes using constant buffer-relative offsets. After issuing streams, each outer
tile waits for all participating vector cores. (code: software/kohakutpu/compiler/crates/ktpu-l2/src/lower/mover/packing.rs:850-894)

When conversion version is at least 2, loading requires bit 0 in the ELF package
feature word; absence raises AbiMismatch before stopping/booting sysnodes.
Dispatcher exports KA_PKG_FEATURES in the sixth .ka_abi word.
(code: software/kohakutpu/application/kohakutpu/application/runtime/card/__init__.py:322-337;
software/kohakuaccel/firmware/apps/dispatcher/main.c:15-22;
software/kohakuaccel/firmware/include/ka/package/format.h:11)
