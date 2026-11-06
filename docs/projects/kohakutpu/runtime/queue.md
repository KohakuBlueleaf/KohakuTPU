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
need and pinned by the same path. A shared scratch window is a write access:
its users order through their single-node FIFO or through admission conflicts.
Runs using disjoint windows can overlap. A replayed graph pins one window a
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
Single-node kernels on the same node rely on FIFO ordering. Across queues,
admission compares physical read/write-line access sets and observes completion only for the
conflicting pending runs, not an unrelated submission prefix. Observed results
and pins stay pending until their submission-order prefix can retire, so a
later success is not exposed ahead of an earlier fault. This also applies
to multi-node runs, including two runs with the same grant: different shares can
consume one another's writes. Read/read and disjoint-line accesses do not order.
Raw L1 recipes without a graph port contract retain allocation-wide access
sets; generated native recipes bind their cached contracts during the existing
allocation walk. Scratch windows are writes in their own memory space, so overlapping uses
of one window always order. `KTPU_NODE_OVERLAP=0` selects serialized cross-queue
admission; overlap is enabled by default. Doorbell receiver/slot pairs are also
exclusive access resources. Their footprint is extracted once from immutable
package RING/WAIT_SLOT steps and retained with the launch. Admission observes
only launches sharing one of those resources before reuse; equal slot numbers
on different receivers do not conflict. Native recipes derive this footprint
at construction rather than trusting caller metadata. LIST replay unions its
members' leases. Completion of the complete multi-node launch establishes sender
quiescence and consumption before another artifact reuses its slots.

These lifetime guarantees belong to `Device` submissions. The lower-level
`NodeCard` queue interface is a transport primitive; applications using it
directly own ordering and must not mix unmanaged slot users with managed runs.

A split run stages and publishes its shares on every participating queue before
any of them is collected, through one `submit_launch` / `collect` pair on both
card and device double. Its shares order one another through device doorbells.
`Device.on_node(k)` grants one node and places its scratch on `k`; each home has
its own DRAM window.

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

A captured graph orders its launches by their accesses
(`runtime/graph/dependencies.py`): each launch's reads and writes over the
canonical allocations, in 64-byte lines, give RAW, WAR and WAW edges; reads of
the same lines and disjoint lines give none, and transitive edges are dropped.
Host-effect boundaries of the capture stay. `SplitRunner`
(`runtime/graph/split.py`) composes the launches of a graph of split kernels
into one L2 lowering under their physical bindings (`compile_split_graph`), so
node ordering and slot leasing see every event of the graph at once: an edge
between shares on one sysnode rides that queue's FIFO order, an edge between
sysnodes becomes a ring/wait pair on a leased doorbell slot (9-15). The prepared
graph keeps the lowered edges (producer, consumer, regions, slot) as `events`.

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
