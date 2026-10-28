# Host memory model

The driver owns one shared DRAM arena and a staging arena per node. The firmware
executes packages; it does not allocate memory or apply relocations.

## Handles and address translation

`Device.alloc` returns a card virtual address. A view adds its byte offset to
that address. The driver's `SoftwareMMU` assigns non-reused virtual ranges and
records the physical base, accessible extent, arena and allocation generation.
Physical reuse does not revive a retired virtual range. Staging virtual ranges
preserve their node and staging-domain bits.

A package buffer-table index is its local handle. The binding list supplies
virtual bases in buffer-table order. `_rs.bind(..., translations=...)` accepts
live `(virtual base, physical base, extent)` entries from the driver. Rust
validates every handle and its entire buffer extent before ordinary or template
binding. Unknown handles, overlaps, overflow and insufficient extents refuse
with `RUL.023`. An empty translation list is not a physical-address fallback.
`Template.bind_handles` uses the same resolver. Host reads and writes also
resolve virtual addresses. Bound wire packages contain physical payload fields
and no buffer or relocation table.

- **L0.MEM.007** `frozen` Handle translation preserves view offsets, refuses
  stale generations and does not alter the source package. Vector:
  `RUL.023.handles`; mutant: `memory/identity-translation`. Ordinary and template
  binding tests: `compiler/tests/package/test_handle_bind.py`.

## Exact extents

Accessible extents are requested bytes rounded to a 32-byte card word. Placement
may reserve a larger aligned span, but this alignment gap is not accessible
through the handle. There is no per-allocation tail. The compatibility observer
`TAIL` is zero and does not participate in allocation arithmetic.

Flat vector and cast kernels compute incomplete vector rows in declared scratch
and copy only real words in and out. Reverse kernels use declared scratch for
incomplete trips and copy only destination words out. Full trips need no tail
scratch. Row kernels isolate partial bands in scratch. Transpose and lane
relayout round their private scratch but transfer only declared destination
words. Matmul uses explicitly padded packed formats and product temporaries;
column broadcast operands are explicitly expanded when the compute width
exceeds their logical width.

- **L0.MEM.008** `RTL` Flat/cast and reverse kernels preserve a sentinel starting
  immediately after the destination word extent. Tier-0 vectors:
  `RUL.020.flat_extent`, `RUL.020.reverse_extent`; tier-1 vectors:
  `RUL.020.flat_sentinel`, `RUL.020.reverse_sentinel`. Separate allocations are
  covered by `RUL.020.adjacent_cast` and `RUL.020.adjacent_reverse`; the guard
  occupies the next physical reservation and a distinct handle. Their mutants restore
  rounded-row destination accesses. `RUL.020.no_allocation_tail` is frozen.

## Submission and completion

- **L0.MEM.001** `frozen` Submissions retain distinct named allocations,
  including interior views. Owner free does not recycle a referenced span.
  Successful collection releases scratch ownership and submission references
  once. Vector: `L0.BIND.027.dependencies`; mutant: `memory/free-immediate`.
- **L0.MEM.002** `unproven` Failed waits preserve reservations. Uncertain
  publication quarantines spans and refuses further runtime submissions. Device
  close first shuts down the card, then revokes all reservations, including
  recording pins. If card shutdown fails, reservations remain. Vector:
  `L0.BIND.027.teardown`; mutant: `memory/no-teardown`; failed-wait and partial
  publication tests: `application/tests/runtime/test_lifetime.py`.
- **L0.MEM.003** `frozen` Allocation pressure collects oldest first, retrying
  after each completion. `max_inflight` is positive or `None`; reaching it
  collects oldest first. Host reads/writes drain work. Vector:
  `API.MEM.005.oldest`; mutant: `memory/drain-all`.
- **L0.MEM.009** `frozen` Runtime packages use the shared allocator, tagged
  `package`. Every submitted tag retains its package before queue publication.
  Polling a final completion releases that tag; the last reference returns the
  package span. Progress completions do not retire it. The queue's local staging
  fallback reuses coalesced free package spans. Vector: `L0.BIND.027.packages`;
  mutant: `memory/no-package-retirement`.

## Recording and TinyJit

- **L0.MEM.004** `frozen` Recording uses the main heap. A `Recorded` log owns
  pins for its host accesses and kernel bindings. Closing is idempotent; log
  destruction releases pins. Replay refuses a closed log or one from another
  device. Replay holds independent references during execution and quarantines
  them after an uncertain failure. Vector: `L0.BIND.030.recording`; mutant:
  `memory/no-record-pins`.
- **L0.MEM.005** `RTL` TinyJit releases superseded captures before recapturing.
  Each graph also owns placement pins on main-heap extents, indexed by kernel
  call site, allocation signature and occurrence. Scratch and temporary spans
  reuse their physical locations on recapture, independent of unrelated heap
  allocations. Reuse waits for outstanding device uses and refuses spans held
  by another recording. Each new owner gets a fresh virtual handle and generation.
  Live forms retain their owners when graph destruction drops placement pins;
  recording and in-flight pins independently delay physical reclamation.
  Replay still requires two byte-identical event logs and equal storage/core
  states; no address fields are ignored or rewritten during comparison.
  Model test `ktpugrad/test_memory_jit.py` compares changing-input outputs with
  NumPy. Offline `ktpugrad/test_fragmented_jit.py` requires replay after unrelated
  allocation/free patterns and checks graph destruction. Runtime
  `test_placement.py` covers generations, staging, live forms, independent logs
  and queued reuse. Tier-0 `L0.MEM.005.jit_drop` kills `memory/no-jit-drop`;
  tier-1 `L0.MEM.005.jit_card` uses the session's device, fragments its heap,
  checks exact NumPy values and kills both `memory/no-jit-replay` and
  `memory/no-stable-placement`. A one-kernel graph may execute directly instead
  of creating a graph capture.

## Accounting

- **L0.MEM.006** `frozen` Every arena reservation has an owner tag and requested
  size. `Arena.stats()` reports requested and reserved usage, peak reserved
  bytes, cumulative allocation count, free bytes, largest free span, free span
  count, external fragmentation (`free - largest`) and owner totals. Pending
  frees remain charged. Vector: `L0.BIND.028.counters`; mutant: `memory/no-peak`.

## Boot regions and firmware

The host allocates each node's queue from its staging arena with owner
`firmware:queue:<node>`. Debug regions use shared DRAM with owner
`firmware:debug:<node>`. The boot table carries base, extent and use: DEBUG=1,
QUEUE=2. Queue regions survive image reloads and reserve their spans until
teardown. Debug regions are reused across debug image reloads.

The host refuses duplicate uses, overlaps, empty extents and address overflow.
Firmware validates populated regions and requires a QUEUE region matching the
queue header pointer, with space for the declared rings and completion records.
An undersized region is refused before queue service becomes ready.

- **L0.MEM.010** `RTL` Boot queue regions come from the allocator and their
  bounds govern firmware queue accesses. Tier-0 vector: `RUL.024.boot_regions`,
  mutant: `memory/no-region-validation`. Tier-1: `RUL.024.boot_region_card`,
  mutant image: `kohakutpu_memory_mutant`.

Dispatcher builds exclude `os/heap`. Opcodes 3..5 return `BAD_OP`; the local and
remote queue clients expose no heap operations. Offset 0x340 is reserved ABI
space, not heap statistics. A package with relocations returns `BAD_RELOC`,
detail 0 and value its relocation count before dispatch. The separate mutation
image disables these refusals and queue extent validation; it is never a runtime
default. Vectors: `RUL.024.host_contract`, `RUL.024.card_refuses`,
`RUL.022.host_bound`, `RUL.022.card_refuses`.
