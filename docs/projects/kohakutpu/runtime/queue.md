---
title: Runtime submission queues and allocation ownership
summary: Host visibility, bounded pending runs, completion pins and replay on the node's v4 queue.
tags: [kohakutpu, runtime, software]
---

# Runtime submission queues

A **submission** is one compiled kernel's packages published to the firmware
queues on their nodes. A **completion** is the node's tagged result, including
status, failing step and cycle count. Publishing does not imply completion.
The v4 queue's run record supplies the timing phases for that exact tag.

## Submission and collection

`Device.submit(compiled, bindings)` returns `Submitted`. Bindings map kernel
parameter names to runtime allocation handles (including offsets). The runtime
resolves handles through its software MMU and retains the underlying arena
allocations. A run's scratch (the arenas and buffers the compiler kept that
the bindings do not name) is laid out in the scratch **window** of its space:
one per node's staging store and one in DRAM, each grown to the largest run's
need and pinned by the same path. One window a space is sound because the runs
in flight are always on one node in that node's queue order (below), so no two
runs' scratch is live at once; a replayed graph therefore pins one window a
space, not a span per site. A window outgrown while a run or a recording still
pins it stays alive for them and the next run takes a new one. Once no run is
in flight, every window no recording pins is freed: a collected run keeps no
scratch.
`Device.collect(submission)` collects its predecessors first and returns a
`Run`; collecting a completed submission again returns the same result.
`Device.drain()` collects oldest first.

Outside a named `submission_scope`, `Device.run` submits and waits. The tinygrad
device enters a scope for each plan: `KOHAKUTPU_QUEUE=1` or `Device(queued=True)`
makes its generated runs return submissions; setting the knob to `0` or `False`
waits. An offline card with an immediate result is collected immediately.

The scope describes current valid input forms and output allocations. The
access contract is refreshed at each generated submission, so a newly produced
input conversion becomes read-only for subsequent kernels. An output alias
always wins over a read-only declaration. Generic submissions without an
access contract conservatively treat all bindings as writable.

`read` waits through the last pending writer of the addressed allocation;
`write` waits through the last pending use. tinygrad applies these hazards to
all forms of its logical storage. Waits collect the submission prefix so an
earlier fault cannot be attributed to the kernel whose read exposed it.
Same-node kernels rely on FIFO ordering. A run over several nodes, or a run on
a node other than the runs in flight, first collects every run in flight: the
runs in flight are on one node, and a run over several nodes runs alone.

## Working-set limits

Each node admits at most `min(queue.sq_n, queue.cq_n)` uncollected runs, taken
from the attached queue layout, not a separate scheduler constant.
`max_inflight` optionally adds a device-wide limit. The union of allocations
pinned by pending runs is bounded by `queue_bytes` (`KOHAKUTPU_QUEUE_BYTES`),
defaulting to the sum of the runtime arena capacities. Shared bindings count
once, including aliases. Admission collects oldest completions until the next
run fits. A single run larger than the byte budget is refused before publication.
Allocation pressure also collects oldest completions before retrying.

`free` releases allocation ownership. An allocation is not reused while a
submission or recording retains it. Successful collection releases the run's
references (its scratch windows among them); a failed wait does not establish that
DMA has stopped and does not release those references.

## Capture and TinyJit replay

`record()` begins a package/write/read event log without draining.
`recorded()` returns an owning `Recorded` log without waiting. It pins each
event's allocations and records each run's original name, plan and access
contract. Closing a log releases only its capture pins.

`replay(log, wait=False)` submits recorded package events through the same
admission and collection path. Each replayed run obtains independent pins,
so closing a log cannot free its active replay's bindings. Host writes in a
log wait for overlapping pending allocations. Logs containing host reads are
not replayable. `wait=True` uses the same path and then collects its runs.
TinyJit uses non-waiting replay in queued mode, restoring the captured form
and instruction-residency bookkeeping after publication.

## Errors and shutdown

A firmware fault preserves the original `NodeError` and completion object,
adding the owning plan and generated run to its message. Later pending results
are marked discarded with the failed origin named. Host transfers and new
submissions reject a poisoned runtime. Pins remain until successful shutdown,
which stops the card before revoking arena allocations. A partially published
multi-node submission similarly quarantines all references and requires close.
A host timeout alone does not turn an unobserved completion into success.

No RTL or firmware change is required: the interface is the node's v4
submission queue, completion queue and schema-defined run records.
