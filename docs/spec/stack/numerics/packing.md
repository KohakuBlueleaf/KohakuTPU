# FP16-to-MXFP7 conversion backends

The machine fact `conversion.fp16_mx7` is a versioned reliability contract,
independent of vector-core geometry and mover transform support.
`allowed_backends` controls admission; `preferred_backend` selects lowering.
The machine generator reads board constraints from
`scripts/py/machine/conversion.json`. `multimesh_v9t6` permits only `vector_mx7`.
Models and other boards permit both implementations and prefer `mover_quant`.

## Selection and identity

`KTPU_FP16_MX7_BACKEND=mover_quant|vector_mx7` selects an allowed backend.
`machine_of(..., packing_backend=...)` is the programmatic equivalent. Neither
widens the board's allowed list. Direct DSL conversions also accept
`quant(..., backend="vector_mx7", finite=True)`; text L2 uses
`quant DST <- SRC backend=vector_mx7 finite=1`.

The serialized machine, including conversion and scheduling facts, keys kernels,
libraries and bound packages. Receiving-board admission checks MOVER and ENGINE
QUANT commands even when optional checks are off. A mover-only policy rejects
vector MX7 instructions. A cached artifact cannot substitute its own policy.

## K pairs and the quantiser (RUL.099)

The cluster merges a K pair's block partials at the larger exponent sum, so an
all-zero block feeding an `nk` >= 2 contraction needs the E=0 field
(NUM.MX7.008, NUM.ACC.008), which only the vector packs give it. A `quant`
into an `nk` >= 2 destination therefore lowers through `vector_mx7`; one naming
`backend=mover_quant` is refused (L2.MOV.018). On a board preferring
`mover_quant`, contractions are planned under the pairing
`ktpu_machine::PAIRING`: `unpaired` plans them `nk` = 1 so mover QUANT feeds
them, `vector` keeps `nk` = 2 and quantises their operands on the vector
cores. `matmul.plan`'s argument `pairing` (Python `KTPU_MX7_PAIRING`) selects
the other; a board preferring `vector_mx7` pairs under either. The pairing is a
plan argument, not a machine fact: a new `conversion` fact changes the schema
hash every machine file carries.

## Finite input contract

FP16 unpack preserves every finite encoding, including signed zero and
subnormals. MX7 packing uses NUM.MX7.002/.003 and NUM.MX7.005's A/B entry order;
into an `nk` >= 2 destination, on the input with every zero as 2^-24
(NUM.MX7.008): one `vsel` a 4x32 entry after its unpack, against a scalar
register holding 2^-24 (`seti s4`).
Version 2 checks unproven inputs **on the card**, without host input transfers.
Each loaded lane is classified before packing; the original numerical register
is not modified. Since this vector ISA has no integer mask operation, the
implementation uses the exact equivalent `abs(unpack(raw)) < 65536` of
`(raw & 0x7c00) != 0x7c00`. Ordered comparison rejects both infinities and every
NaN payload/sign. The full 65,536-encoding classifier has a model witness.

Predicated marker writes materialize invalid lanes. EXP2D's sticky reduction
combines the markers; it is not itself predicated. A reserved vector register
accumulates across RUN boundaries. Each participating VC drains one full-line
status slot. After a barrier the node checks those slots using `ASSERT_ZERO`.
A failing check ends the package with `CONTRACT` (0x24), detail 1
(`NONFINITE_FP16`). Collection raises `NonfinitePackingError`, a `NodeError`
subclass. The runtime invalidates the device's outstanding work; no successful
result is published and failed output cannot be read as valid through the device.

Padding is checked **after** materialization: nonfinite storage outside the live
view is overwritten with zero and is not an input violation. A live nonfinite
lane refuses even when the corresponding output would not subsequently be read.
Signaling NaNs are never silently accepted as quieted output. There is no
raw-integer NaN compatibility backend.

`finite=True` / `finite=1` is an explicit caller proof and skips classification
and status allocation. The caller must maintain this proof for **every** launch
and replay; compilation does not prove tensor values. Omission selects the
checked path. Version-1 policy still requires the explicit proof. Training
provenance alone is not proof. Eager and TinyJit use the same checked package;
recordings contain no finiteness read and recheck current data when replayed.

## Vector schedule

`vector_slots=1|2`, `vector_cores=0..N`, and `vector_looped=false|true` are machine
facts. Zero cores selects all available VCs; explicit values select 1 through N.
Runtime overrides are `KTPU_MX7_SLOTS`, `KTPU_MX7_CORES`, and
`KTPU_MX7_LOOPED=0|1`, or `machine_of`'s `packing_slots`, `packing_cores`, and
`packing_looped` arguments. All participate in cache identity.

Entries are partitioned contiguously over participating VCs. Each entry gathers
four ordered pairs of FP16 words and drains four MX7 words at the original
entry stride. An L1 slot reserves 128 input words followed by 64 output words;
the second slot begins at word 192. The two-slot prologue, steady state and
epilogue prefill the next slot while computing and draining the current slot.
Completion waits protect reuse, and the epilogue never reads past the input.
Checked batches use at most seven entries when emitting an actual two-slot pair
to fit the vector skip displacement; single-slot batches use at most sixteen,
even when two slots were requested but the partition is too small for a pair. Status occupies four
words at L1 word 384.

The vector batch walk uses its architectural loop register. The bounded entry
body is unrolled because the core has only one such register. Looped mode emits
node affine loops over outer geometry, patching descriptor addresses from
relocated literals; unrolled mode emits each outer iteration with constant
buffer-relative descriptor offsets. The default is unrolled, with two slots and
all available VCs. Each outer tile retires on its participating VCs before the
next tile's instruction stream; VCs within a tile execute in parallel. This bounds
completion debt without serializing the image's internal two-slot pipeline.
Backedge dispatches use unit-specific runtime-frontier waits rather than static counts,
and dynamic residency does not elide an image upload needed on a later trip.
Software firmware must advertise package feature bit 0 (contract checks,
immediate relocated loads and runtime-frontier waits); the runtime refuses an older ELF at boot. No RTL
extension is required.
