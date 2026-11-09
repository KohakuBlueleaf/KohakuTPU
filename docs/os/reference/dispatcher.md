---
title: KohakuAccel OS dispatcher (kds)
summary: The user-mode dispatcher — one server per node owning that node's units, an arena of physically contiguous memory and the parts of task graphs; graphs of unit instructions, mover copies, CPU functions and joins with dependencies, loaded across nodes and run with completion collected at the graph's home; buffers named by references that bind on any die.
tags:
  - os
  - reference
  - dispatch
  - dispatcher
---

# The dispatcher (kds)

`software/kohakuaccel/os/dispatch/` (the API in `include/kos/kds.h`). It runs in user
mode, linked into the image like `libkos`; the kernel gives it the
primitives listed [below](#kernel-primitives-it-uses), and nothing in it
knows any unit's instruction set.

```
  client thread ──SUBMIT──▶ home server (its node) ──PART/GO──▶ other servers
       ▲                         │  ▲                              │
       └────────── reply ────────┘  └────── NDONE, DEP ────────────┘
  each server: its node's units (SYS_DISPATCH / SYS_ENGINE), its mover,
               its arena (SYS_MMAP + SYS_VTOP), its parts of graphs
```

## Servers

`kds_start(flags, arena_bytes, node_mask)` spawns one server thread on
every node in the mask and returns when each has answered READY (status
from its set-up). `kds_stop()`, on the same node, sends QUIT to each and
joins them. While servers run, the first 32 KB of every landing zone belong
to them (`KDS_LZ_USER` is the first byte left to applications).

| flag | effect |
|---|---|
| `KDS_F_ENGINE` | unit tasks go in batches of up to 8 through `SYS_ENGINE` (a unit the engine refuses goes through `SYS_DISPATCH`) |
| `KDS_F_UNCACHED` | arenas are mapped through the uncached alias: no release/acquire sweeps around unit tasks |
| `KDS_F_BESTFIT` | the arena takes the smallest free extent that fits (else the first) |

A server's state is one `SYS_MMAP` (4 pages) and a plain per-node global
pointer to it, so every thread of a node sees its own node's server. A
stopped server unmaps its arena chunks and its state: the node's free frames
are what they were before it started.

`kds_server_stat(&s)` reads this node's server's counters: loop passes,
retirement wakes, tasks finished, unit calls, messages in and out, and the
cycles spent taking messages, running tasks, issuing, sending, waiting in
`SYS_EVENT` and retiring.

### The loop

User code runs from DRAM, so a pass fetches only the phases that have work
(flags for ready tasks, pending unit tasks, DEPs, the outbox, instructions
in flight):

```
loop:
    take the messages the last SYS_EVENT counted on the server channel
    run every ready task (NOP, CPU, MOVE at once; UNIT onto its unit's list)
    issue pending unit tasks within each unit's credit
    send the coalesced DEPs and whatever waits in the outbox
    SYS_EVENT(retirement target, timeout, counts, units, server channel)
        -> returns when the node's retirement count reaches the target or a
           message is queued; the copy of every unit's retired and fault
           counts retires unit tasks
```

The **target** is the retirement count now plus the fewest retirements that
change anything: per unit, the first in-flight task with successors, the
last in-flight task of a part whose every remaining task is in flight, half
the ring when tasks wait for the unit's credit, else the ring's last; the
least over the units. A stream of instructions with nothing depending on
them is retired in one wake, a chain wakes once per instruction. Every
retirement on the node counts toward the target, so the wait can end early
but never late. The wait's timeout is the nearest graph deadline, or the
outbox's retry (20 000 cycles) when a send found no room. Sends never wait
(`KOS_SEND_NOWAIT`): a message that finds no room stays in the outbox, in
order per destination, so two servers sending to each other with full queues
do not deadlock.

## Messages

Every message is 40 bytes (`struct kds_msg`: type, flag, count, graph id,
four words) on a server's channel `KDS_CHAN`; a client request carries its
reply channel (`KDS_REPLY(tid)`) and gets exactly one reply. Messages carry
no shared-memory data, so the dispatcher sends them with plain `SYS_SEND` /
`SYS_RECV` (no release/acquire sweeps).

| message | from → to | carries |
|---|---|---|
| SUBMIT | client → its node's server | the task array's address and count, a timeout |
| PART | home → executor | a chunk of the executor's tasks in its landing-zone slice; first: the part's size |
| PART_ACK | executor → home | the chunk is copied out |
| GO | home → executor | every part is loaded: run |
| DEP | executor → executor | up to 16 finished task ids whose successors run there |
| NDONE | executor → home | the part is over: status, failed task, detail, busy cycles |
| ABORT | home → executor | stop the part (it answers NDONE) |
| ALLOC, FREE, FREE_OWNER, TRIM, STAT | client → any server | arena requests |
| QUIT, READY | starter ↔ server | |

## Graphs

A graph is an array of 128-byte task records (`struct kds_task`) in the
client's node's memory, built in order with:

| call | task | runs on |
|---|---|---|
| `kds_nop(g, node, pred, n)` | a join or fan-out point | `node` |
| `kds_unit(g, unit, w, pred, n)` | one instruction (four opaque words) to `unit` | the node on the unit's mesh |
| `kds_patch(g, task, ref, lo, width, shift)` | bits `[lo, lo + width)` of the task's instruction get `pa(ref) >> shift` at bind time (up to 3 per task; a field split over two places is two patches) | — |
| `kds_move(g, src, dst, words, pred, n)` | one mover copy of `words` 32-byte words from a buffer to a buffer of the same node or any node's landing zone | the source's node |
| `kds_cpu(g, node, fn, a0, a1, a2, pred, n)` | `fn(args)` with `args` = {a0, a1, a2}; a nonzero return fails the task (the image is the same on every node, so `fn` is valid everywhere) | `node` |

### Nonblocking leased events

`KDS_WAIT_SLOT` carries the local user slot in `a[0]`, the amount in `a[1]`,
and a peek flag in `a[3]` (zero consumes). `KDS_RELEASE_SLOT` carries slot,
amount, destination OS node and fence flag in `a[0..3]`. Both records run on
the submitting resident's node. Admission checks the slot range 9–15, count
widths, flags and destination. Ordinary predecessor tasks remain local.

An unavailable event or full ring egress defers the task on a pass-local list,
using its existing task link. The ready ring drains completely, including newly
ready successors, before deferred tasks return to its tail. Each pending
operation is probed at most once per pass while other ready work reaches the feeder. Pending operations bound `SYS_EVENT` sleep to one cycle;
the graph's ordinary deadline remains authoritative. Each probe is constant
work, with no changes to unit-word dispatch or retirement. Dirty CPU writes
are released before a ring; a successful acquire sets the existing hardware-dirty
flag so later CPU readers use writeback plus invalidation. Unit-memory release
frontiers remain the graph producer's responsibility. Release completion means
enqueue, so slot reclamation additionally requires the receiver's consumption
and the whole-epoch no-late-sender proof.

The host `Graph.wait_slot` and `Graph.release_slot` builders create these explicit
tasks. `Graph.package_parts` lowers KAPK `WAIT_SLOT` and RING, including
embedded ENGINE rings, to these operations. Its node-local `KDS_DE_FEED` tasks
append descriptors in one persistent epoch: they complete at enqueue, not unit
retirement. Mailbox state, mover configuration and completion frontiers survive
an acquire. `KDS_DE_DRAIN` waits nonblockingly for the queued publication frontier
before a ring; `KDS_DE_FINISH` joins the epoch and rearms counters only after
local work retires. Neither event pause resets native incoming completion counts.
The adapter retains DE batching/fetch and does not relay graph edges through DEP.

A local resident arms receive counters before READY, so early remote completions
are counted even before its first feed. Counter indices are dense in local boot
unit order rather than the full multi-node registry. Descriptor payload storage
stays protected until FINISH; failure leaves it quarantined. `SYS_DE_RUN` keeps
its synchronous operation for legacy streams and adds OPEN, FEED, DRAIN, FINISH
and CLOSE operations in its third argument.

### Node-local retained mailbox groups

`ostpu capacity bytes retain local` takes a fourth numeric argument: `local=1`
starts only the invoking node's server, while the default `0` keeps full-mask
startup. A multi-part host starts one local resident per participant. The DONE
granule's third word advertises `OSTPU_CAP_SLOTS` and, in local mode,
`OSTPU_CAP_LOCAL | OSTPU_CAP_EPOCH`. Older images do not advertise these capabilities.

`ResidentGraph.submit` publishes a request and returns its sequence token;
`collect` reads the matching terminal status. `run` composes those operations.
`PreparedMultiGraph` claims every resident, preflights every part before uploading,
retains allocation references and user-slot leases, and publishes all local
requests before collection. Its one completion object includes every node's
result. Successful replay reuses the uploads and content/binding-derived epoch;
another preparation cannot replace a claimed resident. One host lock protects
single-flight transitions. Failure poisons the group and all residents and keeps
pins and leases; a timeout is not permission to close or reclaim them. Collection
still attempts the other published parts. Failure then publishes a request-only
STOP (`OSTPU_REQ_CANCEL`, request bit 63) on every participant and attempts every
acknowledgement without overwriting the active command's timeout/repeat count.
The current execution remains governed by its execution bound. Unacknowledged
stops remain quarantined; even an acknowledgement does not release failed epoch
pins. `ostpu` retains task/data storage and the server after an unsuccessful
epoch instead of freeing hardware-visible memory on STOP. A clean stop releases
idle DE ownership, allowing another local resident on the running node.

`Graph.package_parts` validates CPU ownership and translates each immutable bound
node part using the existing package adapter's capabilities. `BoundGraphArtifact`
contains the machine identity, node packages and binding-generation/layout metadata;
its digest includes all of them. Its event admission checks balanced tokens for
static RING/WAIT_SLOT and embedded ENGINE rings. Dynamic control/patch effects
are outside the common KAOS package capability set and refuse at preparation.
`PreparedSplitGraph.bind_artifact` binds its composed graph once, retaining private
node-qualified scratch and canonical aliases. `prepare_firmware` and
`prepare_kaos` consume the same cached artifact without a second address bind or
slot allocation. The existing captured/eager submission entry remains available.

### Models on P sysnodes

The model recorder selects the retained backend with `--backend kaos` or
`--backend firmware`. Both selections enable TinyJit and send captured pure
regions through `PreparedSplitGraph.bind_artifact`, including a one-node region.
The same workload, compiler, bindings and split-event allocation feed either
`prepare_kaos` or `prepare_firmware`; neither adapter repartitions the graph.
Ordinary initialization and capture launches use the selected card backend too.
The eager KAOS adapter takes each already-bound `Package.to_bytes()` from
the ordinary card builder or native wire submission and retains the complete
package as a `NodePart`. Headers, unit tables, steps and payloads travel together;
`Package.bound()` is a diagnostic list of relocated payload words, not a wire
package. The adapter does not bind or decode the package a second time before
constructing the artifact.
Host writes remain between pure regions. This is a Device backend of the existing
model entry, not a separate model-capture format.

Build the resident image, then run from the repository root in the installed
software Python environment:

```sh
python software/kohakuaccel/os/tools/build.py kos --out build/os && \
KOS_IMAGE=build/os/kos.elf python software/tests/perf/record.py vitb \
  --card --backend kaos --board multimesh_v9t6 --nodes 2 --batch 4 --tag .b4
python software/tests/perf/record.py vitb \
  --card --backend firmware --board multimesh_v9t6 --nodes 2 --batch 4 --tag .b4
```

`vitb` is the ViT-B workload. Use `--nodes 1`, `2` or `4` and `--batch 1`, `4` or
`16`; include the batch in `--tag` so configurations have distinct directories.
The clock profile must already be set, exactly as for the firmware card entry;
`KOHAKUTPU_PROFILE` selects the profile to verify. Both paths use XDMA.

The boot owner in `software/kohakutpu/os/tools/ostpu/model.py` reuses `run_card`'s
transport, ELF arrays, node loader and boot tables. `--os-board` names the OS
board description (default `card_v9t6_4n`); it must describe the selected machine
and contain every requested participant. It selects the first P node/mesh pairs
and their units, stops all participating CPUs before painting, loads every
participant, releases the load windows in reverse node order, then attaches
the local `ResidentGraph` mailboxes. No attachment waits before every loaded
CPU has started; node zero starts last for the OS boot handshake.
`--kos-image` overrides `KOS_IMAGE`, whose default is `build/os/kos.elf`.
The linker enforces the 32-KiB IMEM limit for `.text` and at least 256 bytes of
free space. The build prints the exact `.text` size in `kos.size`; `kos.map`
also records `__k_text_free`. One-shot L0 mapping/setup and kernel startup,
unit/message-table initialization and the interlink handshake reside in the
per-node DRAM image alongside user text. They execute untranslated at their
physical load addresses with resident callbacks and owner-local state setters.
Early boot-table/private-span validation, MMU/log helpers, the cache-maintenance
callback and steady-state kernel operations remain in IMEM. The cache command
and its busy-rise/busy-fall polls execute together there; the live busy interval
must not be hidden by a private-image instruction refill. Units, syscalls, messages and scheduler keep
`-O2` and the selected HOT flags. The host loader and boot table format are
shared by both pieces.

The stage harness `software/tests/run.py` prepares the default HOT `kos` image
at `build/os/kos.elf` before starting the timed `firmware/emu` suite or emulator
mutant consumers. Its `.elf.stage.json` receipt records the complete set and
content digests of OS sources, assembly, headers, linker scripts, shared firmware
headers, `build.py`, `memgap.py` and `toolchain.py`, plus the toolchain-selection
environment, resolved tool paths/content and built image digest. The builder and
stage harness share tool discovery; an in-place tool upgrade or a newly preferred
installation also invalidates reuse. Reuse requires an exact match: source removal, preserved
source timestamps and an externally replaced image cannot select stale code.
An image without a matching receipt is rebuilt with the default HOT/memory-gap
policy. A relative `KOS_IMAGE` is resolved once in the harness invocation
working directory and propagated as an absolute path to the builder and every
consumer, so their repository-root working directory cannot select another ELF.
It needs the RISC-V LLVM toolchain selected by `KOS_LLVM`
(or the build tool's normal discovery). A failed build stops the harness before
any test can consume an older image, with compiler/linker diagnostics on stderr;
unsuccessful builds leave no selectable image. The stage budget is 30 seconds,
with no heavy-test exception for the physical-heap witness.
The physical-heap witness in `software/tests/firmware/emu/test_os.py` consumes
that supplied `KOS_IMAGE`, like the other OS tests; it does not compile.
It boots `card_v9t6_4n` on `KA_EMU_MODEL`: four nodes, each receiving the same
16-unit table (four local units). Private-page message reduction exercises
all participants and the 2-GiB physical shared grant. ka-emu models cache busy
as a read-cleared flag, not a timed sweep, and does not model instruction-refill
latency. This witness checks boot and reduction, not the card's cache-busy timing;
the native unit harness separately requires the resident barrier before pending
allocation. Direct
pytest invocation requires the caller to build and supply the image first.

The same emulator stage boots `parallel ostpu 4096 4096 1 1 -e` with
`shared_phys` on all four nodes. One case uses the stock board's zero shared
span and private layout; the other uses the model entry's capacity-derived
private spans and 2-GiB physical heap. The test waits for `OSTPU_MAGIC` at every
node's application mailbox, not a console banner or process exit. The emulator's
`--until-word N:0xADDR:0xVALUE` option samples all requested physical words every
1024 iterations and at termination, stopping when they match together. The
ordinary cycle bound still applies, and unsuccessful node exits remain failures.
This permits testing persistent services without changing their executable image.
The startup consumer supplies an explicit quiescent DE hardware fixture through
`--control-word N:0xOFFSET:0xVALUE`: a full-width register initialization before
CPU execution. The DE status register advertises its signature, idle queue and
no faults; counter registers start at zero. ka-emu's generic register storage
accepts the startup control/map writes. No descriptors are submitted before the
mailbox stopping condition. This fixture covers software startup and publication,
not DE execution, counter timing, cache timing or the card's missing-READY cause.
It does not initialize mailbox magic. The option is an emulator fixture interface,
not a card command: actual hardware writes to DE status enqueue WAIT entries.

A log header's `where user` address is the selected task's saved resume PC, not
a live processor PC. With `KOS_LOCAL_LOG`, USER transitions and switch counts
remain local until a log synchronization, such as a console write. A quiet
resident may therefore leave an earlier USER or IDLE snapshot visible. Diagnose
a missing READY using the mailbox and actual halted PC/task state; neither an
init banner nor an unchanged log-header PC establishes where execution stopped.

Permanent cold startup records use `kos_progress(KOS_W_*, a, b)` in user mode.
They are ordinary SYS_WRITE records in the persistent log and console, encoded
as `@where CODE A B` (decimal code, hexadecimal operands). `klog` adds the latest
complete record as `progress`, `progress_a`, `progress_b`, leaving scheduler
`where` fields intact. `run_card` reads the full log at its existing 30-second
progress interval and appends the named progress to each node's summary.

| progress | operands / boundary |
|---|---|
| `app_args`, `app_enter` | app index and argument pointer / decoded argc |
| `init_args`, `parallel_args` | copied bytes; second operand 0 copying, 1 complete |
| `run_issue`, `run_ack` | destination node; returned tid or signed status at acknowledgment |
| `join_issue`, `join_ack` | destination node; tid before join, status/exit afterward |
| `ostpu_args`, `mailbox` | argc; then mailbox VA and size before clearing |
| `server_start`, `server_started` | capacity/local mode; then returned startup status |
| `server_state`, `server_ack` | state allocation bytes/starter; then starter/status before reply |
| `de_open`, `de_opened` | node and server flags / returned status |
| `arena`, `arena_done` | node and allocation bytes / returned status |
| `task_alloc`, `task_allocated`, `data_alloc`, `data_allocated` | requested storage followed by returned status |
| `mailbox_fill`, `release` | task/data references; then mailbox VA before release |
| `ready_write`, `ready` | mailbox VA before the fenced READY store / after publication |

A RUN_ACK for a remote node is recorded after SYS_RUN receives its M_RUN reply.
The records observe the user-side syscall boundary, not an internal kernel
message-receive PC. Parent and resident records can interleave on node 0; the
persistent log retains their sequence and destination operands. All call sites
are in app entry, init/parallel startup or resident/server initialization, before
any graph servicing. No progress logging runs per graph, instruction word,
retirement or device poll. Marker code occupies user `.utext`, not resident
kernel `.text`; the existing 32-KiB limit and 256-byte margin remain enforced.

The boot command is **`parallel ostpu capacity 4096 1 1 -e`**. The general
`parallel APP ARGS` utility starts APP on every node before joining any child.
If a launch fails after another child started, or a join fails without confirming
termination, it shuts down the boot with that status. Residents can run forever
or wait for missing participants; returning to init or joining them indefinitely
is not a cleanup strategy. Ordinary child exit codes are collected after all
successful launches. Only node 0 runs init, so remote local residents are started through `kos_run`,
not by assuming each CPU interprets its boot command. Each CPU thus runs
`ostpu capacity bytes retain local` with bytes=4096, retain=1, local=1 and `-e`.
Build `kos` from the same source tree as the host tools.
`--os-capacity` defaults to 65534 task records and is rounded to keep the data
arena 256-byte aligned; records and their descriptor payloads must fit that
resident byte budget. The resident's `bytes` argument is 4096; descriptor storage stays in its task
region, not in the model heap. `--os-bytes` sizes the separate shared model heap
(default 2147483648 bytes). Private spans contain the OS, task storage and KDS
allocator space; they are carved at distinct offsets, followed by the shared
heap spelled through mesh 0 on every node. The complete layout must fit physical
DRAM without overlapping the driver's reserved scratch. The application uses
this shared heap and the ordinary node-qualified staging arenas. `UnitGlobal` carries
both DRAM and staging accesses; mailbox I/O uses `run_card`'s physical mapping.
No second allocator or per-model address relocation is involved.

Boot uses `shared_phys` (`KOS_F_SHARED_PHYS`). This grants the shared physical
span to device I/O without mapping it into the fixed user shared/uncached
aliases: a multi-gigabyte model heap must not cover the user stack or mmap
virtual addresses. Physical mover admission still checks the full shared span.
The kernel stores the mapped shared size separately at boot; virtual shared
queries/translation use that size (zero here), while `KOS_I_SHPA` still reports
the physical base. The boot table also grants each mesh's implemented staging above `KOS_STG_END`,
excluding the OS page tables and message/start rings. Physical admission selects
that span for special addresses, or the existing DRAM spans otherwise: one
constant-time address-space branch, no added DRAM-path memory read, and no new
unit-word or retirement work. Without the flag, mapped shared applications keep their
existing mapping and heap paths.

One active graph owns the local residents. Preparation uploads every part before
publication; replay publishes every participant before collecting any. Repeated
calls to that graph keep its uploads. Switching to another pure region or an
ordinary launch replaces the completed resident graph. A failed epoch retains
its pins and leases and refuses reuse. `KOHAKUTPU_GRAPH_TIMEOUT_CYCLES` sets the
whole-graph execution bound (default 1099511627776 cycles); the resident host
wait is bounded at 1800 seconds. No edge is synchronized by a host poll.

The trace directory has a `.kaos` or `.firmware` suffix. It contains the ordinary
`meta.json`, `launches.jsonl`, `kernels/`, profile/compile files when requested,
and workload `report.json`. `matrix.py` and `waste.py` read these ordinary traces
without a backend-specific reader. `card.jsonl` has one row per retired host run,
with the backend tag and every participating node's cycle/stat record. `cycles`
and `max_node_cycles` mean the maximum **local duration**, not a globally
synchronized hardware interval. For retained regions, `makespan_seconds` is the
host-observed group interval, from first publication through collection of all
participants; it includes publication/poll/transport cost, excludes preparation
and output reads, and does not assume synchronized CPU cycle counters. `wall`
also includes the host's preparation and retirement work.

`outputs.jsonl` preserves each public Device read's bytes, in read order, for
comparison with the firmware run. Compare the same workload/input sequence, not
physical addresses: separately booted processes have separate allocation
identities. Every KAOS completion retains the existing KDS counters, including
DE entries/streams, unit batches, task/message counts and phase cycles.
For kernel trap counters, build a profiled image and request `--measure`:

```sh
python software/kohakuaccel/os/tools/build.py kos --out build/os-profile \
  -D KOS_PROF=1 -D OSTPU_PROFILE_SYS=30 && \
KOS_IMAGE=build/os-profile/kos.elf python software/tests/perf/record.py vitb \
  --card --backend kaos --nodes 4 --batch 16 --tag .b16.profile --measure
```

Here 30 is `SYS_DE_RUN`; `syscall_profile` contains the existing mailbox profile
words for that selected syscall. KAOS `units.jsonl` contains per-node KDS/kernel
statistics, tagged `kaos`, not firmware unit-counter records. Firmware
`--measure` uses its existing measure image and unit ring. Profiling changes the
measured execution cost and is recorded separately. `--traced` belongs to the
ordinary firmware debug entry, not these retained-backend selections.

### Dependency-boundary timestamps

`KDS_STAMP` writes four 64-bit words into a local, aligned 32-byte arena span:
controller cycle, server-wide completed-task count, unit-call count, and label
`a[1]`. The output reference is `a[0]`. The task acquires hardware-written data
before sampling and releases its writes before completing. Its predecessors and
successors determine the observation boundary; it is not a passive hardware probe.

`KOHAKUTPU_OS_STAMPS=1` inserts these tasks before and after each recorded package
(default `0`). Generated stamp storage is separate from immutable instruction
payloads. Enabling the knob requires an image implementing `KDS_STAMP`. Repeated
execution overwrites the records: the final read contains the last repetition.
Counts include instrumentation tasks and other work sharing the server; unit-call
counts include dispatch as well as engine calls. Timestamp differences include
instrumentation and scheduling and must not be reported as pure hardware idle.

### Retained local executor state

With a started server, a serial client may call `kds_retain(g)`, run an immutable
graph repeatedly with `kds_run`, then call `kds_retain(NULL)` before changing or
freeing the graph or any referenced buffer. The initial run validates the client's
records with `check()` and copies them into a server-owned arena block (`p->t`).
A successful all-local execution keeps its task arrays and sorted successor pairs
in `LG_CACHED`. A subsequent retained hit reuses that validated server-owned
`p->t` copy directly (resetting dependency counts, ready state and
`k->cpu_dirty = k->hw_dirty = 1`
without reallocating, re-copying records, re-sorting pairs or re-running `check()`,
which is safe because `p->t` is private to `KDS_SERVER` and the single serial
client contract forbids mutating or freeing referenced buffers while retained).
Failed executions never enter the cache; issued instructions drain before their
storage is freed. Cached parts are inactive and do not match dependency/abort
messages. Remote graphs retain the ordinary lifecycle.

Retention is an explicit single-caller scope, not a shared cache with eviction or
mutation detection. `ostpu capacity bytes retain` selects this behavior with its
third numeric argument (`0` by default). When the host includes a non-zero graph
identity in `rpad[0]` (derived from the packed graph digest and preparation
sequence), the service retains the cached executor arrays across consecutive
mailbox requests that keep the same `(rpad[0], task_count)` pair and releases
when the identity changes or the session stops; legacy requests with
`rpad[0] == 0` release before acknowledging the request. When `rpad[1]`
(`OSTPU_REQ_WRITE_REF_WORD`) is non-zero, `ostpu` writes four 64-bit words
`rpad[2] + 0..3` (`OSTPU_REQ_WRITE_VAL_WORD`) through the cached arena view
`kds_va(rpad[1])` immediately before `kds_run` (used by the B3 retained
coherence witness).

A diagnostic build with `-D KDS_TRACE` writes a last-progress sample at landing
zone offset `KDS_LZ_USER + 0x400`, which that build reserves from application use.
Five 64-bit words are sequence, phase, argument A, argument B, and controller-cycle
time. Phases are submit=1, allocation=2, load=3, GO=4, ready task=5, completed
task=6, engine batch=7, retained reset=8. It is an optional diagnostic sample,
not a synchronized trace ring or hardware idle measurement. Ordinary builds
compile out these writes.

### Resident instruction segments

A wire record of kind `KDS_STREAM` carries a local arena reference in `a[0]`
and a count of **1–32 instructions** in `a[1]`, capped by the unit's credit depth.
Each instruction is four opaque
64-bit words. The payload remains immutable and allocated through completion;
addresses inside it are already bound. The stream's unit must be local to the
home. The server checks that the complete payload lies in a mapped contiguous
range. It does not assign the instruction words any accelerator-specific meaning.

A segment is one dependency task with the final instruction's ticket. The
in-flight ring counts tasks separately from instruction credit; waiting targets
use ticket differences, not the number of segment records. With `KDS_F_ENGINE`
a segment uses consecutive `SYS_ENGINE` calls of at most eight words each;
otherwise its words use `SYS_DISPATCH`. An
accepted prefix remains tracked if a later dispatch fails. Unit faults fail the
owning part, whose storage stays alive until its accepted instructions drain.

The host OS adapter selects this representation with `KOHAKUTPU_OS_SEGMENT=2..32`
(default `1`, individual instructions). `KOHAKUTPU_OS_COMPACT=1` reuses an existing
barrier frontier instead of adding another final join (default `0`). Segments
split at partial AWAIT boundaries so that completion dependencies retain their
meaning. The adapter validates package unit types against its receiving topology
when producing the graph; matching coordinates alone do not identify a unit type.
Its prepared single-node recordings are immutable by caller contract. Constant
whole-word writes in a recording become resident `KDS_MOVE` copies, with data
stored alongside the task array. Variable inputs are uploaded outside the log.

`KOHAKUTPU_OS_PRUNE=1` removes explicit transitive predecessors from joins during
host preparation (default `0`). Each task's predecessor ancestry is a host bitset;
a join retains only dependencies not already covered by another predecessor.
Independent branches are retained. This optimization does not infer implicit FIFO
ordering and does not change the instructions, task payloads, or device syscall
batch limit. Ancestor storage grows quadratically with task count in the worst
case; it is host preparation state, not an OS arena allocation.

### Synchronous mover descriptors

`KOHAKUTPU_OS_MOVERS=1` enables host-bound MOVER/MWAIT lowering (default `0`,
which refuses those steps). Mover register writes travel only through
`KDS_DE_STREAM` (below): with `KOHAKUTPU_OS_DE_STREAM=0` each mover GO becomes
its own one-move `KDS_DE_STREAM` task — the register writes, then a `WAIT` on
the mover's done count — so `SYS_DE_RUN` is the single path by which user
code programs the mover. Register offsets outside the mover's writable set are
refused by the host lowerer and by the kernel. The existing `SYS_MOVE`
translated 1-D copy interface remains available.

The host lowerer conservatively orders mover commands after preceding unit work
and later work after each mover completion. MWAIT counts are absolute within
the package. This synchronous implementation does not overlap posted mover work
with unit computation. Firmware ENGINE, control-flow and remote mover packages
are not implemented by this adapter.

### Pre-compiled dispatch-engine command streams

`KOHAKUTPU_OS_DE_STREAM=1` (default) lowers each host-bound package into a single
`KDS_DE_STREAM` task (`kind` 6). `a[0]` names a 32-byte-aligned
local arena payload, `a[1]` holds the command count (`1..16384`), and `a[2]`
holds any trailing 32-byte-aligned instruction/mover blob bytes. The command
prefix is packed in 32-byte groups of four 64-bit words `(codes, v0, v1, v2)`:
bytes `0..2` of `codes` hold three 8-bit entry codes (`0..4` for mailbox
`DST/ARG0..3` with unchanged words elided, `5` for mailbox `GO`, `8 + (reg >> 3)`
for mover registers, `24 + (reg >> 3)` for interlink registers, `32` for
`WAIT` with `(source << 56) | target`, and `33` (`KOS_DE_CHECK_ZERO`) to drain
the queue and assert that the 64-bit status word at physical address `v` is
zero; bit `0x40` adds `stream_pa << 24` to a
`MEM_RD_REQ` `ARG3` instruction-fetch descriptor, and bit `0x80` adds
`stream_pa << 4` to a mover `MV(0x10)` source descriptor), and `v0..v2` hold the
corresponding 64-bit values. Credit waits, `AWAIT`, `MWAIT`, and final package
barriers are encoded directly as hardware `WAIT` commands inside the stream.

`KOHAKUTPU_OS_DE_FETCH=1` (default `1` when `KOHAKUTPU_OS_DE_STREAM=1`) lowers
`DISPATCH` and `REPEAT` steps on units with a fetch port into `MEM_RD_REQ`
instruction-fetch descriptors (`0x40` stream-relative relocation) with the
instruction words appended in the stream's blob tail. `KOHAKUTPU_OS_DE_FUSE=1`
(default, `0` or stamps per launch keep one task a package) fuses consecutive
single-node package launches and resident constant writes into a single
continuous `KDS_DE_STREAM` task up to the 16,384-entry `SYS_DE_RUN` bound. Constant
writes travel as stream mover commands (`move_de`) on either setting; single
unfused packages exceeding 16,384 entries are likewise split across chained
`KDS_DE_STREAM` tasks. An `AWAIT` above the words a package sent to a unit waits
for a peer's acknowledgements; `SYS_DE_RUN` counts those retirements as sent.

`SYS_DE_RUN` (30), exposed by `kos_de_run`, translates the contiguous arena
buffer once via `move_pa`, quiesces any in-flight unit, mover or engine work,
maps local units (`u < 16`), resets the dispatch engine's completion counters,
and feeds the command groups from IMEM-resident kernel code (`k_de_stream`)
until the engine queue drains and the mover is idle.

`struct kos_kernel_stat` totals every stream that ran whole, without a stamp per
launch: `de_streams`, `de_entries`, and four cycle counters that add up to each
stream's span. `cyc_de_open` empties and maps the engine, `cyc_de_load` validates,
relocates and queues the entries, `cyc_de_room` waits for the engine between
entries, and `cyc_de_tail` is the drain after the last entry. `moves` and
`cyc_move` total `k_move`. `struct kds_server_stat` carries the same fields, so an
`ostpu` run reports their change over the run beside the server's own.

Every byte range the stream makes hardware touch is checked with
`k_pa_ok(pa, bytes)` — whole range inside one of user-mapped `SYS_MMAP` frames
(`umap`), the user landing zone (`KOS_USER_LAND_SIZE`), or shared DRAM — before
the entry is queued; a failing range returns `KOS_EFAULT`, a malformed entry
`KOS_EINVAL`:

- `KOS_DE_CHECK_ZERO`: the 8-byte status word.
- Mailbox `DST`: untyped (an instruction to a unit) or typed `MEM_RD_REQ`
  only; any other flit type is refused. `GO` uses the `DST`/`ARG2`/`ARG3`
  values this stream wrote; a `GO` before any `DST` is refused.
- `MEM_RD_REQ` instruction fetch: `ARG2` must name one peer with one-word
  entries and `ARG3` length `0` and flags `INST|STREAM`, so the fetch reads
  exactly `count * 32` bytes from `ARG3`'s address, which is checked.
- Mover: the kernel shadows the walker registers this stream wrote (`HDR`,
  `DIM` staged then loaded by `AXIS` into the walker `HDR`/`DIM`/`WINDOW`
  last selected). At `CTRL` with `GO`, every walker the mode reads or writes
  (source and destination; destination only for `GENERATE`/`FILL`) must have
  its header and every walked level written by this stream; its extent is
  `[base + sum of negative level spans, base + sum of positive spans + 32)`
  (`+ 128` for an `XFORM` destination, which writes four output words per
  element), with each level span `(count - 1) * stride` within the walker's
  32-bit partial sum. A `GATHER` or `SCATTER` also needs the indexed side's
  `HDR`, `INDEX` and pitch registers from this stream. The kernel drains the
  queue and the mover and counts the move's rows (the walked side's elements
  over the row words); row `r` reads index entry `r mod 2048`. It copies
  `min(rows, 2048)` entries, rounded up to 8, into a kernel buffer: the
  caller's entries within its `INDEX` count, zeros past it. It checks
  `[base, base + max(index * pitch mod 2^32) + 32 * row_words)` (a float index
  is converted as the mover converts it), then queues an `INDEX` write naming
  its own buffer and that entry count before the GO. The move therefore reads
  only indices the kernel checked. An `INDEX` count over 2048 is `KOS_EINVAL`,
  as the mover faults it (`F_IDXLEN`). Register offsets outside `CTRL`, `HDR`, `DIM`,
  `AXIS`, `WINDOW`, `INDEX`, the pitch and the transform registers are refused.
- Interlink `KOS_DE_IL_RING` writes are restricted to user signal slots
  `KOS_SLOT_USER..HW_SIG_SLOTS-1`.

Unit instruction words themselves are passed through as `SYS_DISPATCH`
passes them: a unit's own address operands are that unit's to check.

Each queued `KOS_DE_MB_GO` increments `kunits[u].sent` by its word count (`1`
for a direct instruction, `n` for a `MEM_RD_REQ` fetch) as it is written to the queue (after any wait for queue
room, so a wait that fails has not counted it), and records the entry in a 64-slot ring `q_go`. Every wait
loop (`CHECK_ZERO` drain, queue refill, and final drain) polls `k_units_poll()`
for unit completions and faults (`HW_SIG_FAULT_CODE`), checks mover faults
(`HW_MV_FAULT`), and enforces the progress timeout `KOS_DE_TIMEOUT` (counting
unit retirements `k_events`, DE `issued`, and mover `done` as progress). On exit
(both normal and error), `k_de_stream` disables the dispatch engine
(`HW_R_DE_CTL = 0`), polls `k_units_poll()` to harvest any engine-claimed
completions, subtracts any unissued `q_go` entries (`[issued, pushed)`) from
`kunits[u].sent`, clears the engine and unmaps all local units in `HW_R_DE_MAP`
(`valid = 0`, `de_ready = 0`, `de_mapped[u] = 0`), and drains any remaining
in-flight unit instructions (`kunits[u].sent != retired[u]`) and mover activity.
A mover fault stays latched until the mover's next GO and holds the node's
external interrupt, so `k_poll` starts with `k_mover_clear`: with a fault
latched it runs a one-word `k_move` within the kernel's index buffer, whose GO
clears it. `k_move` and `k_de_stream` return the fault before a poll runs.
`KDS_DE_STREAM` flushes dirty CPU lines (`kos_release` when `k->cpu_dirty`)
before `SYS_DE_RUN` and sets `k->hw_dirty = 1` on completion so subsequent
`KDS_CPU` tasks and `answer()` (`kos_acquire` when `k->hw_dirty`, `kos_release`
when `k->cpu_dirty`) keep L1 and DRAM coherent.

When compiled with `-D KOS_LAUNCH_RING`, slot `62` (`KOS_LRING_STEP_SLOT`) at
landing-zone offset `0x9000` (`KDS_LZ_USER + 0x1000`) records the per-step IPC
and cache breakdown (`step_total`, `client_cache_acq`, `client_send`,
`server_wake_recv`, `check_or_reuse`, `cache_release`, `de_run`,
`cache_acquire`, `server_reply`, `r.cycles`, `cyc_msgs`, `retained_hit`). When
`KOHAKUTPU_OS_LAUNCH_RING=1` is also enabled, `KDS_LAUNCH_MARK` tasks (`kind` 7)
bracket each captured launch and write 96-byte `struct kos_launch_rec` records
in slots `0..min(launches, 62)-1` (`cyc_total`, `cyc_walk`, `cyc_dep`, `cyc_xlat`,
`cyc_build`, `cyc_submit`, `cyc_wait`, `cyc_retire`, `cyc_sys`, `syscalls`,
`batches`, `id`).

Each task names up to **4 predecessors, each an earlier task** — every graph
is acyclic by construction, and the server refuses any record that breaks it
(`KOS_EINVAL` with the task's index). Fan-in or fan-out beyond 4 goes through
NOP tasks. At most 65 535 tasks per graph.

`kds_run(g, timeout, &r)` submits the graph to this node's server (the
**home**) and waits for its answer:

| `r.status` | when | `r.failed`, `r.detail` |
|---|---|---|
| `KOS_OK` | every task finished | — |
| `KOS_EINVAL` | a bad record (kind, predecessor, patch, unit, move size, node) | its index |
| `KOS_ENOENT` | a task's node has no server | its index |
| `KOS_EFAULT` | a task failed | its index; a CPU task's return, a unit's fault count, a mover's fault |
| `KOS_ETIME` | the deadline (`timeout` cycles from the submit) passed | — |
| `KOS_EAGAIN` | the home tracks 4 graphs already | — |

`r.cycles` is the home's count from the submit to its answer.

### What the home does

1. **Check.** Every record as above; each task's node (UNIT: the unit's
   mesh's node; MOVE: the source's node); each task's `succ_nodes`, the
   nodes its successors run on.
2. **Load**, one graph at a time (each peer has one landing-zone slice per
   home): the home's own part is copied in directly; every other node's part
   goes in chunks of up to 64 records, gathered into a staging block of the
   home's arena, **moved by the mover** into the executor's slice
   `[home × 8 KB, + 8 KB)`, and announced by PART; the executor copies the
   records out and answers PART_ACK; the next chunk follows.
3. **GO** to every part once all are loaded.
4. **Collect** NDONE from every part; answer the client when all have
   reported. A failure, or the deadline, sends ABORT to every part still
   running and answers at once (later reports are counted and ignored).

### What an executor does with its part

- **Load.** The records in id order; each task's count of unfinished
  predecessors; a successor index (sorted `(predecessor id, local task)`
  pairs, binary search); the tasks with no predecessors in the ready ring.
  DEPs that arrive before GO count down, but nothing runs before GO.
- **Run.** NOP: done. CPU: the function, in the server thread. MOVE:
  `SYS_MOVE` (into a landing zone with `KOS_MV_LAND(n)`). UNIT: the patches
  are bound (physical address of each reference *as this node names it*),
  then the task joins its unit's pending list.
- **Issue.** Per unit, at most `min(depth, 32)` instructions in flight;
  consecutive pending tasks of a part go as one `SYS_ENGINE` batch of up to 8
  (`KDS_F_ENGINE`) or one `SYS_DISPATCH` each. A unit runs its instructions
  in issue order, so tickets retire in order and the retired count from
  `SYS_EVENT` completes a prefix of the unit's in-flight ring. A fault
  (the unit's fault count rose) fails the oldest outstanding task's part;
  accepted instructions still drain before that part's storage is released.
- **Finish.** A finished task counts down its successors here; its id goes
  into the DEP for each other node in its `succ_nodes` (coalesced, up to 16
  ids per message, sent at the end of the loop pass). When the part's last
  task finishes, or one fails, or ABORT comes, the executor sends NDONE
  (after its DEPs) and frees the part's memory once no unit instruction of
  it is in flight.

### Coherence on an executor

Units write DRAM behind the L1; the cache is per node and shared by every
thread of the node. With cached arenas the executor writes the L1 back
(**release**) before issuing a unit task when a CPU task ran, or a new part
opened (a client thread here may have written what its units read), since
the last release; it writes back and drops the L1 (**acquire**) before a CPU
task when a unit task finished here, or a DEP came (a unit on another die
may have written here), since the last acquire. The home acquires once more
before it answers, so the client reads what units wrote. `SYS_MOVE` does
both sweeps around a copy. `KDS_F_UNCACHED` maps the arenas uncached and
skips every sweep.

## Memory: arenas and references

Each server owns its node's **arena**: chunks, each one `SYS_MMAP`
(physically contiguous, zeroed) whose physical address `SYS_VTOP` gives once;
the first of `arena_bytes` at start, another of `max(arena_bytes, request)`
when nothing fits. Blocks are 64-byte aligned multiples of 64 (one writer
per L1 line; the mover needs 32). Free extents are kept sorted by (chunk,
offset) and merge with both neighbours on free.

A block is named by a **reference**, `node << 56 | offset in that node's
private span` (`kds_ref`). Any node turns it into the physical address its
units and mover use: `SYS_INFO(KOS_I_SPAN, node) + offset` — that node's
span as *this* node names it (its own spelling for its own span, the owner's
mesh bits for a peer's). So one graph binds the same buffer correctly on
whichever die's unit uses it. `kds_land(node, off)` is the reference of a
landing-zone byte.

| call | does |
|---|---|
| `kds_alloc(node, bytes, &ref)` | a block on `node`'s arena, owned by the calling thread |
| `kds_free(ref)` | gives it back |
| `kds_free_owner(node)` | gives back every block the calling thread took there; how many |
| `kds_trim(node)` | unmaps every fully free chunk (`SYS_MUNMAP`); bytes returned |
| `kds_arena_stat(node, &s)` | chunks, bytes, free bytes, largest free extent, used and free extents |
| `kds_va(ref)` | on the reference's own node: its user address (an arena block or the landing zone); else null |
| `kds_pa(ref)` | the physical address this node's units and mover name it by |

## Kernel primitives it uses

| call | for |
|---|---|
| `SYS_EVENT` | the loop's one wait: a retirement target or a message on the server channel; copies every unit's retired and fault counts |
| `SYS_RECV(.., KOS_NOWAIT)`, `SYS_SEND(.., KOS_SEND_NOWAIT)` | draining without blocking; the outbox |
| `SYS_VTOP` | a chunk's physical address |
| `SYS_MUNMAP` | trim |
| `SYS_MMAP(.., KOS_MMAP_UNCACHED)` | uncached arenas |
| `SYS_INFO(KOS_I_SPAN / KOS_I_SPANSZ / KOS_I_MESHOF, node)` | binding references; units to nodes |

Details in [syscalls.md](syscalls.md).

## Applications

`kdschain`, `kdsreduce`, `kdsbad`, `kdsmulti`, `kdsalloc`, `kdsbench`
(`apps/kds/`) and `kdsmg` (`apps/ktpu/`, the one that knows MG's
instruction format); [use/apps.md](../use/apps.md#dispatcher-applications).
Measured costs: [performance.md](../hardware/performance.md#the-dispatcher).

## Limits

| | |
|---|---|
| tasks per graph | 65 535 |
| predecessors / patches per task | 4 / 3 |
| graphs a node runs parts of at once / a home tracks | 4 / 4 |
| unit instructions in flight per unit | `min(depth, 32)` |
| arena chunks / free and used extents per node | 16 / 128 each |
| graph records | in the submitting client's node's memory (the home reads them in place) |
