# Vector packing schedules

`quant` converts FP16 rows to MX7A or MX7B entries. The machine's
`conversion.fp16_mx7` policy admits a backend. Unproven FP16 input is checked
on the device before successful package completion. `finite=1` is a
caller-validated input contract, not a performance switch for arbitrary input.

## Independent classifiers

`finite_batch=1`, `2` or `4` controls independent finite classifiers. Each
entry owns four vector registers and a predicate. Unpacks finish before math;
absolute values and constants for separate entries issue independently.
Math completion waits order comparisons, masked materialization and EXP2D.
The sticky accumulator is v31; a four-entry batch uses v0–v15. Pack completion
precedes register reuse. The numerical operations and packed order are unchanged.

The application defaults to `KTPU_PIPE_FINITE_BATCH=4`. The low-level DSL
attribute defaults to 1. Explicitly finite input uses the unchecked path.

## Padding without a DRAM tensor

`quant DST <- SRC pad=1` converts a two-dimensional word-aligned FP16 source
view into a destination view covering complete 4-row by 32-column entries.
The source can have fewer rows and columns than the destination. Source
columns, pitch and offset are multiples of 16 FP16 values (one 32-byte word).
Source rows need not be a multiple of four. Destination row and K tile strides
retain their `gm`/`gn` and `nk` layout; `nk` is not restricted to one.

The lowerer partitions the view at valid extents and packed-layout boundaries.
Full interior entries keep the ordinary batched double-buffered schedule.
A partial entry zeros its register and unpacks only valid row words into the
corresponding chunks. Entirely padded entries pack zeros without source DMA
or finite classification. No source padding is read and no padding is staged
in DRAM. Invalid destination extents are rejected before partitioning.

On the `vector_mx7` backend, the application `quant_kernel` uses this rule by
default for ordinary and class-shaped kernels and each batch. `pad_strategy="staged"` is a measured
alternative: fill a padded FP16 scratch, copy logical values, then quantize
whole entries. It can be cheaper when splitting partial rows leaves cores
idle and multiplies node dispatch. Runtime admission uses a board-specific
cost profile, not a global `KTPU_PIPE_PAD` switch. `quant_walk_kernel` still materializes arbitrary
source walks into a dense tensor of **logical values only**, then uses the same
padding rule on the vector backend. This walk staging is not claimed to be a
peer-stream pipeline.

The trace machine's `packing_backend` is the machine file's selected
`conversion.fp16_mx7.preferred_backend`, including runtime overrides. With
`mover_quant`, both row and walk producers stage required row/column padding
in a zero-filled scratch tensor before quantization; already complete views
quantize directly. They omit `pad`, `pad_cols`, `finite_batch` and `overlap`:
finite-classifier batching and output-drain overlap schedule the vector
backend only. Native lowering still enforces `allowed_backends`; the producer
does not override board admission.

`pad_cols=16` remains the low-level form for an explicit 16-column to 32-column
view with matching row counts. Both padding forms require the vector backend;
requesting them with mover-quant is refused rather than silently staging.

## Deferred finite status

A checked quant drains each core's sticky finite accumulator into a status
buffer, then a node barrier and one `assert_zero` per core follow it. Given
`status=VIEW`, the quant drains each core of each piece holding source
entries into its own 128-byte record of `VIEW` (`f16[64]` a record) with no
barrier, and a later `quant_check VIEW` checks every record pending inside it
behind one barrier (L2.MOV.019). The records a quant takes are
`ktpu_machine::packing::quant_status_bytes`, the rule the lowering and the
contraction kernel's quant ring both size by. A pending record is a linear
resource: nothing writes over it before its check, and a function does not
end with one unchecked.

## Overlapping output drains

With `overlap=1`, an output drain can run under the next tile's computation.
Before packing into a reused output slot, the pack queue waits on drains
with slack `slots-1`: zero for one slot, one for alternating slots. Only the
other slot's youngest drain is exempt. Pack completion still precedes drain
issue; unpack/math/pack register dependencies and node tile retirement remain
ordered. The application defaults to `KTPU_PIPE_OVERLAP=1`; the low-level DSL
default is off. Batch and overlap settings are part of the kernel cache key
and captured graph. Libraries must match the settings and source revision.

## Witnesses

`test_pipe_general_padding.py::test_class_batched_padding_bytes_and_mutant`
combines class generation, two batches, nk=2, partial rows, empty rows, a
16-column tail and full extra padded K chunks. It compares MX7B packed bytes
to the independent reference, checks output guards and poisons every invalid
source word with signaling NaNs. The nonzero-padding mutant must fail the
byte witness. It also executes the logical-only arbitrary-walk staging path.

`test_pipe_batch.py` witnesses classifier bytes and nonfinite rejection.
`test_pipe_overlap.py` witnesses output-slot retirement: removing its wait
is rejected statically and corrupts a bounded model execution.
