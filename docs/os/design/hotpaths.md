# Configurable kernel hot paths

The kernel and machine layer have opt-in build definitions. Supply a definition
with `software/os/tools/build.py kos -D NAME`; omit it to use the comparison
implementation. Definitions apply to the complete image, including user code.

| Definition | Implementation |
|---|---|
| `KOS_FAST` | Assembly SELF, TIME, INFO NODE/NODES/MESH. With `KOS_CHAN_FIFO`, ready EVENT uses assembly for zero records; `KOS_EVENT_ASM` also enables record-copying ready EVENT. Trace, polling mode, and `KOS_PROF` use the C trap path. |
| `KOS_SPLIT` | Save caller registers at supervisor entry. C returns next/old thread pointers in a0/a1, after restoring its callee registers. Assembly spills s0–s11 only when changing live threads. Initial and exited threads use full restoration. |
| `KOS_SMALL_CALL` | Out-of-line WRITE, STAT, DISPATCH, ENGINE, RUN, INFO and trace helpers; size-oriented code generation; bypass the scheduler when the current thread remains RUN. |
| `KOS_XLAT` | Kernel Sv39 assembly query and one positive translation per thread, tagged by the address-space mapping generation. Mapping and unmapping attempts invalidate before L0 can partially change a range. |
| `KOS_POLL_EVENTS` | Intrusive per-kind waiter lists, direct channel receiver wakes, doorbell-gated ring draining and an empty-pending-table bypass. |
| `KOS_LOCAL_LOG` | Scratchpad switch counter and current-thread record. Publish on console output, non-user progress transitions, timer ticks and kernel-stat queries. A busy cooperative user thread need not update the DRAM heartbeat. |
| `KOS_CHAN_FIFO` | A shared 63-message pool, exact 32-bit channel keys in hash buckets, and a FIFO/count per occupied channel. Receive unlinks an index without moving other messages. Hash collisions compare full keys. |
| `KOS_L0_SPLIT` | Machine C handlers preserve the callee registers rather than saving them into the trap frame. |
| `KOS_L0_FAST` | Assembly NOP and SET_TIMER; QUERY calls the assembly walker using only its required scratch registers. |
| `KOS_REMOTE_BATCH` | Remote SYS_ENGINE moves up to eight opaque instructions to a reserved landing slot and receives one reply after retirement. Message handlers use out-of-line, size-oriented code generation. |
| `KOS_COMPACT_XLAT` | One out-of-line `k_user_pa` instead of expanding the cache lookup and walker call into each copying routine. |
| `KOS_EVENT_ASM` | Replaces the C event-record gather with assembly; allocates only the clamped records on the kernel stack, then calls the page-aware copy routine. |
| `KOS_WAIT_ACTIVE` | With `KOS_POLL_EVENTS`, tracks the number of polled waiters at insertion/removal and skips empty waiter lists. Unit, message and pending sources are still polled first. |

## Translation and traps

The assembly walker returns the equivalent 4-KB PTE inside a 4-KB, 2-MB or
1-GB leaf. Page tables in DRAM are accessed through the uncached alias;
staging tables retain their staging addresses. The per-thread cache belongs
to the node's shared user address space, not to an independent process address
space. All kernel map/unmap entry points advance the generation, including
stack teardown. An invalid page is not cached.

Fast supervisor calls still account thread cycles, traps and syscalls. Their
assembly field offsets are checked at compile time. The kernel executes with
translation disabled: entry clears `satp` before data access, and return
restores it after the final data access. The memory-gap pass remains enabled
for all translated user code, including the register witness assembly.

## EVENT record copies

Ready EVENT requests with records bypass the general syscall/scheduler path
when `KOS_FAST`, `KOS_CHAN_FIFO` and `KOS_EVENT_ASM` are enabled. Entry saves
only the caller registers the helper can clobber; it restores all user
registers except the ABI results a0/a1. Callee registers are preserved by the
helper ABI. User cycles are accounted before the copy, not after it.

The gather returns `(retired, faults)` records for `min(requested, knunits)`.
The page-aware copy preserves unaligned destinations, page crossings and
partial-prefix writes on EFAULT. Zero records do not touch the destination.
The same assembly gather serves blocked EVENT wakes and the trace/poll/profile
C path. Errors in C EVENT results are widened as signed values before combining
with the unsigned queued-message count.

## Event polling

The interlink doorbell count gates the peer-slot scan. A remembered incomplete
drain forces another scan when the inbox was full, even after the doorbell
was cleared. Signal-slot reads retain their required extra read, and boot
retains the existing start handshake.

The hardware exposes separate completion-queue, RX and engine counters, not
one combined changed-source register. Unit polling therefore still reads
those sources. Memory-word waits and deadlines remain polled. Receive wakes
are delivered at channel insertion; every removal unlinks the waiter before
putting its thread on the ready queue.

## Remote instruction ownership

With `KOS_REMOTE_BATCH`, the upper 16 KB of the 64-KB landing zone is reserved
for kernel batches. User mappings and SYS_MOVE landing bounds expose
`KOS_USER_LAND_SIZE` (48 KB); the physical span still reserves 64 KB.
Each `(source node, source thread)` owns a 256-byte destination slot until its
reply. A synchronous mover transfer precedes the request; the destination
reads the uncached slot. The source's staging page is allocated on first use.

The receiver reserves a pending record and captures the unit's fault count
**before** issuing instructions. If capacity is unavailable, no instruction
is submitted. The reply carries an error or the retired ticket. A caller need
not issue UNIT_WAIT again after a successful remote batch. Local SYS_ENGINE
continues returning an issued ticket, which still requires a wait.

## Measurements and witnesses

`tools/hotpaths/measure.py` builds cumulative variants and saves each image,
section sizes, console output and JSON measurements under its output directory.
`tools/hotpaths/costs.py` reports linked machine/kernel/library text totals and
trap prologues. `kprof` measures the instrumented C path, not the assembly leaf
path. `khot.self_cycles` measures a SELF call between user CSR timestamps;
`bench.syscall` additionally includes loop and user-code fetch costs.

`khot` checks register preservation, stale translation rejection, blocked
receive wakeups, FIFO order/counts and leaf results. `khot 8` on a shared-memory
board exercises a kernel copy beyond the first 4-KB page of a superpage.
`KOS_HOT_MUT` selects the mutation witnessed by a corresponding host test.
`kevent`, run after a completed unit workload, checks record contents, count
clamping, unaligned page crossings, EFAULT with a copied prefix, caller-register
preservation and channel-triggered wake. It reports `kevent.copy_cycles`.
`tools/hotpaths/witness.py batch|event` runs one model and requires a specific
semantic failure from mutant 7 (missing final batch instruction) or 9 (fault
record replaced by retired count), rather than accepting any nonzero exit.
`measure.py --build-only` compiles without consuming a model run; variants 10,
11, 12 and 13 successively include the base optimized set, compact translation,
assembly EVENT and active-waiter tracking.

`mg [unit]` and `kdsmg rounds [unit] [-flags]` accept an optional unit index to
bound a card-model measurement; omitting it includes all MG units.
