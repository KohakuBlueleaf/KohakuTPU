---
title: Host memory model
summary: The driver owns one shared DRAM arena and a staging arena per node.
tags:
  - spec
  - stack
  - l0
---
# Host memory model

The driver owns one shared DRAM arena and a staging arena per node. The firmware
executes packages; it does not allocate memory or apply relocations.

## Handles and address translation

`Device.alloc` returns a card virtual address. A view adds its byte offset to
that address. The driver's `SoftwareMMU` assigns non-reused virtual ranges and
records the physical base, accessible extent, arena and allocation generation.
Physical reuse does not revive a retired virtual range. Staging virtual ranges
preserve their sysnode and staging-domain bits.

A package buffer-table index is its local handle. The binding list supplies
virtual bases in buffer-table order. `_rs.bind(..., translations=...)` accepts
live `(virtual base, physical base, extent)` entries from the driver. Rust
validates every handle and its entire buffer extent before ordinary or template
binding. Unknown handles, overlapping virtual translations, overflow and insufficient extents refuse
with `RUL.023`. An empty translation list is not a physical-address fallback.
`Template.bind_handles` uses the same resolver. Host reads and writes also
resolve virtual addresses. Bound wire packages contain physical payload fields
and no buffer or relocation table.

- **L0.MEM.007** `unproven` Handle translation preserves view offsets, refuses
  stale generations and does not alter the source package. Vector:
  `RUL.023.handles`; mutant: `memory/identity-translation`. (code: software/kohakuaccel/driver/kohakuaccel/driver/runtime/mmu.py:38-42, :89-150; software/kohakuaccel/compiler/crates/ka-package/src/format/bind.rs:31-68, :293-333)

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

- **L0.MEM.008** `unproven` Flat/cast kernels with incomplete vector rows compute in padded scratch and copy only the declared destination word extent out. Reverse kernels with incomplete trips likewise use private output scratch and copy only destination words. These paths avoid rounded-row writes beyond the output extent; named sentinel vectors are not established as RTL evidence here. Mutants can disable the exact-extent paths. (code: software/kohakutpu/compiler/crates/ktpu-kernels/src/gen/vector.rs:1567-1598, :1631-1664; software/kohakutpu/compiler/crates/ktpu-kernels/src/gen/convert.rs:109-112; software/kohakutpu/compiler/crates/ktpu-kernels/src/gen/transpose.rs:260-318; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)

## Submission and completion

- **L0.MEM.001** `unproven` Submissions retain distinct referenced allocations, including interior views and scratch windows. Owner free does not recycle referenced storage. Successful collection releases its references once. When no submissions remain and recording is inactive, collection releases only scratch windows with no remaining references; recording pins can retain other windows. (code: software/kohakutpu/application/kohakutpu/application/runtime/device/allocation.py:86-93,134-147; software/kohakutpu/application/kohakutpu/application/runtime/device/submission.py:208,239-258,423-455)
- **L0.MEM.002** `unproven` Failed waits preserve reservations. Uncertain
  publication quarantines spans and refuses further runtime submissions. Device
  close first shuts down the card, then revokes all reservations, including
  recording pins. If card shutdown fails, reservations remain. Vector:
  `L0.BIND.027.teardown`; mutant: `memory/no-teardown`. (code: software/kohakutpu/application/kohakutpu/application/runtime/device/submission.py:67-75, :382-398, :423-449; software/kohakutpu/application/kohakutpu/application/runtime/device/__init__.py:311-332)
- **L0.MEM.003** `unproven` Allocation pressure collects oldest first and retries after each completion, then may evict idle packages. `max_inflight` is positive or `None`; reaching a finite limit collects oldest first. Host accesses collect in order through the last conflicting use of the accessed allocation: reads wait for writers, writes for all uses. (code: software/kohakutpu/application/kohakutpu/application/runtime/device/allocation.py:122-132, :168-198; software/kohakutpu/application/kohakutpu/application/runtime/device/submission.py:77-96, :203-217; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)
- **L0.MEM.009** `unproven` Runtime package allocations use the shared allocator with owner `package`. Submission retains a package tag before publication; final completion retires that tag, while progress does not. Pins and other tags retain storage. Eligible spilled packages may remain in a bounded resident cache; eviction calls `unspill` when supplied, or coalesces queue-local staging spans. (code: software/kohakutpu/application/kohakutpu/application/runtime/card/common.py:130-141; software/kohakuaccel/driver/kohakuaccel/driver/node/queue/client.py:827-831, :868-875, :997-1011, :1126-1190; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)

## Recording and TinyJit

- **L0.MEM.004** `unproven` Recording uses the main heap and `Recorded` owns pins. Closing is idempotent after successful cleanup, and destruction closes the log. Replay refuses closed or foreign-device logs. Ordinary replay retains independent references and quarantines them after uncertain publication; native replay uses its recording’s ownership and records uncertainty when the native run remains busy. (code: software/kohakutpu/application/kohakutpu/application/runtime/device/common.py:146-175; software/kohakutpu/application/kohakutpu/application/runtime/device/allocation.py:134-155; software/kohakutpu/application/kohakutpu/application/runtime/device/replay.py:52-107, :192-225; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)
- **L0.MEM.005** `unproven` TinyJit closes superseded captures while retaining graph placement pins. Placement keys include call site, allocation signature and occurrence. Reuse collects through the last outstanding reference, then reuses the span only if its owner released it and its placement pin is the sole reference; otherwise allocation obtains and pins another span and updates the key. Placement close drops pins without revoking live owners. Replay admission requires equal entry states, event logs and exit states across consecutive captures without host reads; replay also requires the captured entry state. (code: software/kohakutpu/application/kohakutpu/application/runtime/memory/placement.py:18-69, :76-82; software/kohakutpu/application/kohakutpu/application/ktpugrad/device.py:405-434, :447-497; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)

## Accounting

- **L0.MEM.006** `unproven` Every arena reservation has an owner tag and requested
  size. `Arena.stats()` reports requested and reserved usage, peak reserved
  bytes, cumulative allocation count, free bytes, largest free span, free span
  count, external fragmentation (`free - largest`) and owner totals. Pending
  frees remain charged. Vector: `L0.BIND.028.counters`; mutant: `memory/no-peak`. (code: software/kohakutpu/application/kohakutpu/application/runtime/memory/arena.py:72-80, :124-166, :189-205)

## Boot regions and firmware

The host allocates each sysnode's queue from its staging arena with owner
`firmware:queue:<node>`. Debug regions use shared DRAM with owner
`firmware:debug:<node>`. The boot table carries base, extent and use: DEBUG=1,
QUEUE=2. Queue regions survive image reloads and reserve their spans until
teardown. Debug regions are reused across debug image reloads.

The host refuses duplicate uses, overlaps, empty extents and address overflow.
Firmware validates populated regions and requires a QUEUE region matching the
queue header pointer, with space for the declared rings and completion records.
An undersized region is refused before queue service becomes ready.

- **L0.MEM.010** `unproven` Fatal state fallback does not treat an arbitrary aligned boot queue pointer as writable. It validates boot identity and regions, locates the queue region, and requires a matching nonzero aligned base and extent covering the firmware state line before publishing. (code: software/kohakuaccel/firmware/queue/service.c:25-45; evidence: implementation inspection only; no card or named RTL witness established for this complete contract.)

Dispatcher builds exclude `os/heap`. Opcodes 3..5 return `BAD_OP`; the local and
remote queue clients expose no heap operations. Offset 0x340 is reserved ABI
space, not heap statistics. A package with relocations returns `BAD_RELOC`,
detail 0 and value its relocation count before dispatch. The separate mutation
image disables these refusals and queue extent validation; it is never a runtime
default. Vectors: `RUL.024.host_contract`, `RUL.024.card_refuses`,
`RUL.022.host_bound`, `RUL.022.card_refuses`.
