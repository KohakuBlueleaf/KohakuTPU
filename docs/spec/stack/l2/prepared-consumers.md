# Layout preparation inside consumers

A runtime `forms.Pending` describes a logical FP16 read as a sequence of source
and destination walks. It does not allocate its logical tensor until a reader
requires one. A consumer can execute those relayout steps inside its package,
using the same relayout planner as a standalone conversion.

The generator `prepared.rows_kernel` supplies this mechanism to rows kernels.
`kernels.prepared` supplies it to Python's kernels. The single-package
column reduction tree, `matmul.quant_kernel`, and `matmul.mm_kernel` epilogue
extras also accept prepared plans.
They preserve their numerical operations: a tree retains each FP16 partial
store and a packing retains finite classification and MX7 entry order.

## Direct consumer-shaped destinations

A relayout plan normally writes memory lines. Its final destination can instead
be the consumer's own row-shaped scratch when every final writer uses byte or
word addressing (`Fill`, `Words`, `Runs`, `Pull`). There is no extra copy solely
to change that scratch's declared dimensions.

Gather and scatter indices address rows of a table. The L2/DSL
`table_stride=BYTES` attribute specifies that row stride independently of the
table's declared dimensions; without it the stride is the declared row size.
A relayout `Runs` writer uses the original memory-line stride when the consumer
row width differs. The Rust mover lowerer propagates this stride into the L1
transfer's `pitch`. Changing a buffer's dimensions without preserving that
stride changes addresses, not just types, and is invalid.

This preparation remains staged through DRAM. It is not a peer-L1 stream or a
claim to remove the lane-select arithmetic. Only measured package/shape-copy
savings justify it. Kernel-level finite-status and dependency barriers remain.

## Measured alternatives

`runtime.costs` reads an immutable JSON deployment profile named by
`KTPU_COST_PROFILE`. The file contains:

- `schedule`: `finite_batch` and `overlap`, which must match current packing
  settings;
- `recipes`: canonical JSON keys containing alternative kind, board, and the
  address-independent generator/layout recipe;
- each record's `original` and `alternative` cycle costs, `verified` value
  witness, and provenance.

Only a verified, strictly cheaper alternative is admitted. This admitted view
is cached per profile path and indexed by alternative kind and board; record
keys are decoded once, not on each query. An absent profile,
an unmeasured board/recipe, or incompatible schedule retains ordinary execution.
The profile is not a whitelist embedded in kernel code. The same selection
works for any measured shape and can prefer different schedules on one-core
models and two-core deployments.

Prepared quant reads compare their logical `(row, column)` source map, not an
intermediate row pitch. Replacing a dense temporary by a padded one is allowed
only when the source bounds/offset/strides and every logical destination axis
match, columns do not wrap, and the packed geometry is unchanged. For compound
maps, every prefix step must match exactly and only the final step may change
intermediate row pitch. A final identity walk is omitted. Later steps read the
previous step's buffer, not the original source. A measured quant recipe can
retain `pad_strategy="staged"` where that board benefits from homogeneous row
groups. Matmul epilogue extras use exact generator and step recipes, including
the unchanged output geometry and expression.

The `staged_padding` alternative chooses a padded DRAM tensor instead of native
partial-entry partitions. This trades traffic for core balance and fewer image
changes. The cost belongs to the board and recipe; there is no universal claim
that fewer transferred bytes wins.

Set the profile before capture/prebuild and execution. The prebuild command
retraces implicit captures under current settings; saved capture reuse requires
explicit `--capture`, whose provenance is the caller's responsibility. A library must include
the selected recipes. Profiles should be regenerated after changes affecting
lowering cost. Model-derived entries targeting a different deployment topology
must be identified as extrapolation and checked on that deployment.
