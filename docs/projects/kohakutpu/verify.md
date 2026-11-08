---
title: Static verification of node programs
summary: The abstract machine a compiled package runs on, the proofs that one run of it decides deadlock, rerun safety, port exclusivity and L1 races for every schedule of the hardware, and the dispatch scheduler built on it.
tags:
  - kohakutpu
  - compiler
  - verification
---

# Static verification of node programs

`ktpu-verify` decides, before a package reaches a card, whether
it can hang, whether it leaves state that changes its next run, whether two
streams can collide on a unit's receive port, and whether any two accesses to
a unit's local memory or shared physical memory are unordered. It does so by running the packages once on
an abstract machine. This page defines that machine and proves that the one
run answers each question for **every** timing the hardware can take.

The same machine orders the node's dispatches (§6), so the compiler cannot
emit a dispatch order that deadlocks.

## 1. What it decides

| finding | meaning | decided by |
|---|---|---|
| `deadlock` | some process can never reach its end, under every schedule | Theorem 1 + Theorem 2 |
| `rest` | a counter is left above what its waiters took (credits, landed granules, doorbells, a DATA_RECEIVED later than its package): the next run starts from another state | Theorem 4 |
| `port` | two bursts from different senders into one unit are not ordered, so their flits can interleave in the unit's one receive register; a burst can queue behind a shared fill's responses at a sharer not yet in its FILL; or an L1 burst can land while a memory FILL's responses arrive (one entry-assembly register) | §7.3 |
| `race` | conflicting local or physical byte accesses without their required causal or same-source write order | §7.2, §7.5 |
| `line` | independent writers partially own one physical DRAM write line without serialization | §7.5 |
| `bounds` | a burst the receiver rejects (past its buffer), or a peer drain past 256 words (F_LEN) | direct |
| `route` | a credit or a burst aimed where no unit is | direct |
| `loop` | a node loop whose trip count is data: checked for 0..`trips` trips (§5.2) | bounded |
| `model` | an unresolved footprint, unavailable instruction value, unsupported operation, or exhausted proof budget; refuses certification | §7.5 |

Entry points:

- `ktpu_verify::check` checks a sequence of whole-card package sets.
- `ktpu_verify::check_with_memory` additionally accepts physical package sections
  and initialized address-bearing input bytes. `CardStack::check_memory` builds
  the cold and retained packages against this preparation-time context.
- `ktpu_verify::schedule` orders dispatches (§6), not physical admission.
  An indexed GO retains its architectural completion even when its input bytes
  are unavailable or its footprint exceeds the proof budget. These are
  unresolved `model` obligations, not missing mover completions. `check`,
  `check_scheduled` and `check_with_memory` include them in the refusing verdict;
  an order is not a certificate. The Rust stack returns both packages and the
  verification findings through `_rs.compile`.
- `kohakutpu.application.runtime.packages.findings` applies the runtime gate
  to the compiled verdict: `refuse` raises `Refused` for unsafe findings,
  `warn` returns them, and `off` suppresses reporting. Rust owns the order
  and verifier caches. Python does not rebuild or reverify a package.

### Bound arguments and symbolic storage

A package proof covers the supplied bound instructions and physical memory
context. Shape-class generation emits literal loop bounds and initializes its
node counters from those literals; count-driven F-016 copies preserve those
registers in locally acknowledged spill storage. Their arithmetic remains
concrete throughout the bound proof. Unknown loads are never assigned a
representative value. A bound proof is not a certificate for other class points.
Offline template `proof::prove` retains its exhaustive finite-box obligations and
certified parameter-independence reductions; sampled verification is not a
universal certificate.

Forward symbolic alternatives share one append-only BDD arena. Predicates remain
branch-local; fresh inputs, node identities and interned word identities belong
to the shared arena. Creating a branch or retained continuation copies an arena
handle, not its tables. Arithmetic and feasible-successor predicates remain
exact. A mutex protects arena allocation across host users; recursive BDD apply
runs under one outer operation lock. This does not claim a polynomial bound for
arbitrary symbolic multiplication or division.

## 2. The machine

### 2.1 Processes

Every process is sequential and deterministic.

| process | one per | actions |
|---|---|---|
| node | mesh | the package's steps (`ktpu-verify/src/sim/node/`) |
| dispatch engine | mesh | queued mailbox, mover and interlink writes and explicit modular counter waits |
| CU | vector core | IMEM / DESC words; a RUN; its completion at VHALT |
| front end | vector core | fetch, scalars, VLOOP, VSKIPZ, marks; dispatch into an engine queue |
| F, D, U, M, P engine | vector core | its queue in order (`vec_cu.v`, `v2_core.v`) |
| sequencer | cluster | FILL, GEMM, DRAIN, POST in order (`mx_cluster_cu.v`) |
| drain queue | cluster | the write port: bursts of what sweeps emitted and drains sent |
| port | unit | none of its own: each burst landing is one of its events |
| array | cluster | none of its own: each sweep's L1 reads are one of its events |

The CPU enqueues ENGINE entries from the package's current code and value bytes.
The dispatch engine has its own clock and bounded FIFO. Enqueue completion lets
the CPU proceed to the next step without acquiring engine execution. Reusing a
FIFO slot acquires the entry that freed it; firmware reads an entry's bytes before
its push waits for room. A code word is read once for its three entry slots.

A mailbox GO delivers its instruction, or requests the encoded relative/absolute
fetch. It introduces no implicit instruction-credit wait. Fetched instruction
bytes retain their physical or private package provenance. Mover entries use the
same register decoder, footprints and response clocks as ordinary MOVER steps.
An engine interlink write waits for the mover frontier. Unsupported control codes
and unavailable instruction bytes retain the ordinary refusing model finding.

ENGINE WAIT uses the machine counter width and acquires the earliest satisfying
completion frontier following its previously observed frontier. Eager simulation
of later completions cannot turn an old modular target into a later epoch's
publication. CPU fallback drains acquire the engine frontier; direct ALU, JUMP
and PATCH execution does not. Each retained package has its own payload origin
and completion baseline, while the hardware mailbox state persists.

### 2.2 Counters and guards

Shared state is a set of **counters**. A counter is a natural number that only
grows. Each action of a process:

- is **enabled** when every counter in a finite set it names has reached a
  threshold, the set and the thresholds being functions of that process's own
  history only;
- then changes its own local state and **adds** non-negative amounts, again
  functions of its own history, to counters.

What a hardware unit "spends" or "takes" — credits, landed entries, granules, doorbells,
completions — the model keeps as the taker's own threshold, never by
decrementing the counter. A FIFO between two processes (node → unit words,
front end → engine, sequencer → drain queue) is a counter of items written
plus items that are never changed once written.

Every wait in the hardware is one of these guards:

| wait (hardware) | counter | threshold (own history) |
|---|---|---|
| node DISPATCH: a word / a fetch request of n | unit completions | sent + n − credit |
| node AWAIT u n | instruction retirements from u (not DATA_RECEIVED) | expected (+n) |
| node BARRIER, package end | unit completions | sent |
| node RECV n | flits landed at the node | taken + n |
| node WAIT_BELL m n / WAIT_SLOT s n | doorbells from m / slot s | consumed + n |
| node SEND … ack | DATA_RECEIVED from the unit | acks taken + 1 |
| unit instruction | words delivered | pc + 1 |
| cluster `fill … recv n` | entries landed on that side (COUNT) | taken + n |
| cluster shared FILL, k-th of a group | each sharer's arrivals | k (each) |
| cluster emitting GEMM | sub-tiles that left the drain queue | everything pushed before |
| cluster plain DRAIN, POST, GEMM (array idle) | sub-tiles that left | pushed − DQ_ROOM |
| cluster fused DRAIN | sub-tiles that left | emitted |
| cluster burst of a `credit` drain, sub-tile j of g | credits received | spent + min(j + 8, g) (Lemma 5) |
| vector `vwait rx/cr n` | granules landed (COUNT) / credits received | every earlier vwait's n, + n |
| vector VSYNC / VBAR / a mark | instructions of that kind finished | count at dispatch − slack |
| vector register hazard | the older entry's engine finished | its index |
| vector queue room | that engine's head | dispatched − depth + 1 |
| vector CU completion | front-end halts | RUNs started |
| host between runs | nodes that ended run k | number of nodes |

The vector RX and CR counters, the cluster's landed-entry and credit counters,
the doorbell and slot counts, and IMEM, DESC, AINC and the walk pointers persist
across RUNs and packages, as in the RTL.

## 3. One run decides every schedule

Write a state as the vector **n** of how many actions each process has taken.
Because every amount added is a function of the adding process's own history,
the counters are a function **C(n)**, and **C** is monotone: n ≤ m implies
C(n) ≤ C(m) componentwise.

**Lemma 1 (persistence).** If process i's next action is enabled in state n,
it stays enabled in every state m ≥ n with m_i = n_i.

*Proof.* Its guard compares counters to thresholds fixed by i's history,
which is n_i = m_i. Counters only grow: C(m) ≥ C(n). ∎

**Theorem 1 (one final state).** Every maximal execution ends in the same
state. In particular, which processes are not at their end — a deadlock — does
not depend on the schedule, and running any one schedule to its end decides
it.

*Proof.* Let two maximal executions end in n and m. Walk the execution that
ends in n, through states n⁽⁰⁾ = 0, n⁽¹⁾, …, n. Suppose n⁽ᵗ⁾ ≤ m and step t
is an action of process i. If n⁽ᵗ⁾_i < m_i then n⁽ᵗ⁺¹⁾ ≤ m. If n⁽ᵗ⁾_i = m_i,
that action is enabled in n⁽ᵗ⁾, so by Lemma 1 it is enabled in m: m is not
maximal, a contradiction. By induction n ≤ m. Symmetrically m ≤ n. ∎

`Machine.run` takes every enabled action of every process, round after round,
until none is enabled: a maximal execution. Its end state is the one state
every schedule reaches.

A process whose action takes "as much as is allowed now" — the node sending a
DISPATCH as far as credit goes, the drain queue sending the sub-tiles credits
allow — is the same process with one action per word or per sub-tile, run
several actions at a time. Batching consecutive actions of one process changes
no state the proof uses.

## 4. The machine bounds the hardware

### 4.1 Weaker guards, same effects

The hardware is the same system of processes and counters with, at some
waits, a **lower** threshold than the machine's (Lemma 5). It also has
resources the machine does not model: the mesh's links and buffers, and stalls
that end on their own (a pending DATA_RECEIVED on a vector or cluster port, an
accumulator peer handshake).

**Lemma 5 (burst splits).** The cluster's write port closes a burst at 8
sub-tiles or whenever its queue runs dry, so where bursts split depends on
timing. A `credit` burst leaves once the credits hold its length. The burst
holding sub-tile j of an item of g sub-tiles starts at or after the item's
start and ends before min(j + 8, g). So no split asks for more than spent +
min(j + 8, g) credits before sub-tile j leaves — the machine's threshold.
Burst splits do not change the totals that land: a vector core counts
granules, a cluster counts an entry when its fourth word lands.

**Theorem 2 (no hang).** If the machine reaches the end of every process,
and there are no `port` findings, the hardware does too.

*Proof.* Let **H** be the hardware system with thresholds θᴴ ≤ θᴹ. The
machine's maximal execution reaching the all-done state is an execution of H,
since each of its actions is enabled at a threshold at least H's. Theorem 1
holds for H (its guards are monotone the same way), so every maximal
execution of H ends in a state ≥ every state H can reach, in particular ≥
all-done.

What remains is that H's extra resources never stall forever:

- A flit waits in the mesh only while its destination port is not ready. A
  unit's receive port is not ready only (a) while a DATA_RECEIVED it owes is
  unsent — bounded, the send path never waits on reception — or (b) while a
  memory response sits at its head and the unit is not in S_FILL. Case (b) is
  exactly the P2 condition of §7.3.
- Two bursts from different senders into one unit interleave their flits at
  the port; the receiver then mis-reads a descriptor (`vec_cu.v` drops and
  faults, `mx_cluster_cu.v` corrupts). P1 of §7.3 rules this out.
- The node drains its completion queue in every wait, and unrequested RX flits
  unless the package RECVs (then they are counted).

Under assumption A1 (§9: the mesh delivers every flit to a ready port), the
hardware reaches the state the machine reaches. ∎

### 4.2 What sees a burst land

A receiver answers DATA_RECEIVED once a burst, to the coordinate in the
burst's ack field, 0 meaning its sender. The node's processor has no
coordinate a field can name (ktpu.md H2), so the node counts only the
acknowledgements of bursts it sent itself (`push ... wait`). Every other
landing is seen by its receiver: COUNT for a vector core's L1 and a
cluster's A / B. An add into a cluster's accumulator is counted by nothing,
so its sender follows it with a COUNTed entry into the receiver's L1: one
source's flits to one destination arrive in order (A1), and the receiver's
`fill recv` of that entry follows the whole add. The machine models exactly
that: one port's landings are ordered events, and a landing's COUNT carries
the port's clock.

## 5. Data-dependent control

The software contract in [L0 steps](../../spec/stack/l0/steps.md) defines
`ASSERT_ZERO`: a zero register continues; a nonzero register ends the package
with `CONTRACT`. The verifier intersects the successful continuation with that
zero predicate. A certainly failing guard is a refusing finding. No memory is
read by the assertion itself, and it joins no producer clock: the producer must
already have been synchronized before the status LOAD. A certificate does not
replace the runtime numerical contract check or authorize replay after a failed
package; failure/quarantine remains the runtime's responsibility.

Instruction waits follow L0.STEP.020: `AWAIT` and saved-frontier waits consume
instruction retirements only. `DATA_RECEIVED` is consumed by a SEND ACK wait and
cannot substitute for a producer's retirement or release its DRAM accesses.

### 5.1 Vector skips

**Lemma 4 (data-dependent skip).** A `VSKIPZ` on a value only known at run
time (a `GETSTK` sticky bit) jumps over a block. If the block contains no
VWAIT, VPOST or peer VDRAIN, the core's effects on every counter outside
itself are the same on both paths.

*Proof.* Inside the core, every wait is on older instructions of the same
core (§2.2): a younger instruction never blocks an older one, so the core
reaches its VHALT on either path. The counters other units read — RX/CR
targets, credits posted, bursts sent — change only through VWAIT, VPOST and
peer VDRAIN. ∎

A peer VDRAIN is also counter-neutral when COUNT and SIGNAL are both zero, its
resolved receiver is a cluster operand buffer (A or B, not accumulator
ADD_PEER), and every destination granule is already initialized and covered by
an unconditional earlier peer drain from this core in the same RUN. The
verifier proves this at the resolved drain, including its descriptor's running
offset. Same-core drain queue order (ISA.VC.140) keeps the unconditional
producer before the overwrite. ISA.VC.130 defines COUNT/SIGNAL and peer
routing. Skipping the overwrite leaves complete operands and no missing
counter increment. A first producer, counted transfer, acknowledgement, or
other receiver does not satisfy this proof.

The machine runs the block (the constrained path). VWAIT, VPOST and VHALT
inside it refuse; peer drains must pass the overwrite proof. Races inside the
block, including optional peer writes and receiver assembly conflicts, are
checked on the path taken. A skipped block writes nothing, so the path the
machine runs holds every access of either path.

### 5.2 Node loops

A JUMP on registers the node computed (ALU on immediates) runs exactly. A JUMP
on a loaded value takes the branch `trips` times (3 by default) and then
falls through, and reports `loop`. A segment run on more than one trip must be
scheduled the same way on each (§6), or scheduling fails.

## 6. The dispatch scheduler

Consecutive DISPATCH and REPEAT steps form a **segment**. The words of
different units in a segment are unordered: they run on different units. Each
unit's words keep their order. The firmware sends a segment's steps in
package order, each to the end, so a unit whose FIFO is full of words waiting
on a stream from a unit whose words come later in the segment never drains.

`verify.schedule` builds the packages and runs them with each node `free`:

1. A node runs its segment in package order, sending as far as the dispatch
   rules allow.
2. Only when no process of the whole machine can take an action, a free node
   sends the earliest item that heads its unit's queue and can move, past the
   blocked one. A REPEAT step is sent whole.
3. Every send is logged. The package is rebuilt with each segment's items in
   logged order, as DISPATCH steps over the same payloads. JUMP targets and
   step-patching PATCH steps are renumbered.

**Theorem 3 (scheduled packages complete).** If the scheduling run ends with
every process done, the rebuilt packages run to completion on the hardware.

*Proof.* The scheduling run is an execution of the rebuilt program: each
logged DISPATCH was sent in a state where the dispatch rules allowed all of it,
and the rebuilt package sends exactly the logged items in logged order. That
execution ends with every process done. By Theorem 1 every maximal execution
of the rebuilt program does too, and by Theorem 2 so does the hardware. ∎

A segment the compiler ordered well is left exactly as it was: the scheduler
deviates only where the whole machine is stuck. If no order completes, the
scheduler raises `ScheduleError` with the deadlock — a program error, not a
scheduling one.

The machine starts each scheduling run from what the host's `residents`
records as resident in each vector core (IMEM, DESC). Counters start at rest,
which Theorem 4 guarantees after a verified run.

## 7. Happens-before, races and ports

### 7.1 Clocks

Every event carries a vector clock. An event joins the clocks of what its
guard waited for. For a counter it joins the increments the threshold needed.

**Lemma 3 (what a threshold must follow).** Let a counter be incremented by
owners s with totals T_s over the run, and let an event wait for it to reach
θ. Under every schedule the event follows at least the first θ − Σ_{s'≠s}
T_{s'} increments of each owner s.

*Proof.* Before the event the counter holds ≥ θ, at most Σ_{s'≠s} T_{s'} of
it from other owners. The rest, at least θ − Σ_{s'≠s} T_{s'}, comes from s,
whose increments are ordered by s's own sequence: they are its first ones. ∎

Any bound A_{s'} ≤ T_{s'} on the increments of s' that can precede the event
sharpens the lemma the same way. A node's AWAIT counts a unit's completions
and its DATA_RECEIVED together. The unit completes at most the words that node
has sent it, because the node is the only sender of its words, so
A_unit = sent.

`Counter.clock_to` joins exactly those increments, with the totals taken from
a first run (`Machine.tally`): `check` runs the machine twice. With one owner
it is that owner's increments up to θ. An event's clock therefore holds only
what precedes it under **every** schedule. Happens-before computed this way is
a sub-relation of the true must-happen-before, so a check that asks "is a
before b?" can only err towards reporting.

### 7.2 Races in L1

Accesses:

- vector core: VFILL writes, VDRAIN reads (its walk's length from its L1 offset);
  VUNPK reads, VPACK writes (word w of a walk at base + (w mod 4)·s0 + (w div
  4)·s1, base the offset or the pointer + offset under `rel`; F32 two words a
  chunk, an MX7 pack four). Each is an event of its engine, which is when the
  instruction has finished.
- cluster: FILL writes its entries; a sweep's reads of A and B and its writes
  of the accumulator are an event of the **array**, which the sequencer joins
  only when the hardware waits for the array (POST, plain DRAIN, the next
  GEMM's start; a fused DRAIN joins the last emitting sweep's). A sweep's
  emitted sub-tiles leave from its issue on. A plain DRAIN reads the
  accumulator.
- a burst landing writes its granules (a vector core), entries (a cluster's
  A or B) or sub-tiles (a cluster's accumulator, after a running sweep:
  `peer_open`), as an event of the receiver's port.

`memory.py` keeps, per location, the last write and, per process, the last
read since. Events arrive in the run's order, which extends happens-before.

**Theorem 5 (every race is found).** If two conflicting accesses a, b are
unordered, some race is reported.

*Proof.* Take b after a in the run's order.

- **b writes, a writes.** If a is still the last write when b arrives, the
  pair is checked. Otherwise a later write a′ replaced it, and a′ was checked
  against a. If a ⊀ a′, that pair is reported. If a ≺ a′, then a′ ⊀ b (or else
  a ≺ b), and the same holds of a′ in a's place. The chain of writes is finite.
- **b writes, a reads.** a is checked unless it left the read set:
  - a later read a′ of a's own process replaced it, so a ≺ a′. Then a′ ⊀ b,
    and a′ is checked instead.
  - a write w′ cleared it. Then either a ⊀ w′ (reported at w′), or a ≺ w′ and
    w′ ⊀ b, which reduces to the write case.
- **b reads, a writes.** As in the first case, through the chain of writes. ∎

Every reported pair is two real accesses with no clock ordering. Since
clocks under-approximate must-happen-before (Lemma 3), a report may be
conservative; a clean report is a proof.

### 7.3 Ports

- **P1.** For bursts into one unit from different senders, consecutive in the
  run, the earlier's landing must happen before the later's sending. If every
  consecutive pair is ordered, every pair is (transitivity). The port's events
  then follow a real order, so the clock a landing carries (its port event,
  after the previous landing) adds no false ordering.
- **P2.** A shared FILL's lead issues the read when it reaches its FILL. A
  sharer that has not reached its own FILL then holds a memory response at
  its port's head, blocking every flit behind it. For a sharer S, each burst L
  into S must land before the lead's arrival or be sent after S's arrival.
  Otherwise L can sit behind the response while S waits for something L
  enables.
- **P3.** A cluster assembles FILL responses and CU_DATA words of L1 A/B in
  one entry register. Each L1 burst into a cluster must land before a memory
  FILL's responses can start (the FILL's start, or its lead's arrival for a
  shared FILL) or be sent after the FILL completed. A cluster's FILLs are in
  sequencer order, so the FILLs completed before a burst's sending are a prefix
  (`bisect`); the rest are checked.

### 7.4 DATA_RECEIVED lifetimes

A package counts completions from zero (`interp.c`), so an acknowledgement
arriving after its package ended counts in the next one. Each DATA_RECEIVED
the node counts is attributed to the package whose step sent the burst it
answers (the latest node event in the burst's clock). Its landing must happen
before that package's end. Otherwise it is reported as `rest`.

### 7.5 Shared physical memory

**L1.VER.009, L1.VER.017 and the cross-sysnode verification contract:** one
`GlobalDram` belongs to the card. Its canonical intervals are shared by every
sysnode. Ordinary flat-card DRAM keys discard ingress route bits; staging keys
retain their encoded destination and address. CPU cached and uncached aliases
name the same bytes. No shared-DRAM key contains the issuing mesh.

An access has a resolved byte footprint, a start clock and a completion clock.
RAW and WAR conflict unless one completion happens before the other start.
WAW additionally respects one source's ordered write-response stream; it does
not publish that stream to a reader. Read/read is independent. CPU lane strobes
and native compute-unit word strobes preserve unwritten bytes through MAG's DRAM
width conversion. Those writers own their resolved byte intervals, including
when independent words share a physical 64-byte line. Other DRAM writes retain
whole-line ownership: disjoint bytes within a line require one writer or proved
serialization. Staging is not a DRAM read/modify/write line.

MG fills, the shared-fill leader, plain and emitting drains, VC descriptor walks,
mover and transform reads/writes, CPU protocol accesses, fetched instructions,
package descriptors and PATCH limbs all use this tracker. Footprints are sorted
and coalesced into intervals, not expanded into individual DRAM bytes or lines.
Byte intervals and rounded write-line intervals retain persistent histories by
completion process and access direction. Each history is a binary forest whose
blocks retain the last completion and the componentwise minimum start. The last
completion bounds a same-owner block from above; the minimum start bounds it from
below even when fetch and DMA lifetimes nest. A block is skipped only if all its
accesses are proved ordered in one direction. Read/read and same-stream WAW need
no query, but their records remain available to subsequent conflicting accesses.
Splitting an interval shares history roots instead of copying earlier accesses.
At scheduler sweep boundaries, a branch-local vector-clock minimum bounds every
future physical-access start. Continuing processes, queued dispatch-engine work
and the send clocks of unconsumed physical instruction fetches contribute to it.
An empty unit queue resumes only from a future send, whose producer contributes
its clock. Response-only ports have no independent actions. The shared-FILL
leader remains at its arrival until it registers the physical read; nonleaders
retain their separate port-collision windows without registering another read.
Completed history prefixes below this minimum are retired, including empty
intervals. The minimum is never joined into an execution clock.

Response histories retain the last mark at or below the minimum issuer epoch
and every later mark, preserving predecessor queries rather than advancing their
publication. Saved alternatives share immutable roots and retain their own live
histories. Join checkpoints are unnecessary once no unexplored alternative
remains. Each process receives at most 256 progress events per sweep; fork
resumption preserves its quantum. Collection runs after at least 1024 events at
a sweep boundary, so a nonblocking producer cannot monopolize the interpreter
and postpone every consumer and collection indefinitely.

With P clock components, H historical interval insertions and M response marks,
unretired history storage is O((U + V)P), where U and V are the live suffixes of
H and M (V includes one predecessor per stream). A collector rebuilds changed
suffixes, shares their access events, and releases discarded tree roots. A
prefix with no newly eligible event needs only its forest/oldest-leaf lookup.
Response collection checks the second-oldest mark: the oldest is the retained
predecessor, so an unchanged query floor needs no traversal of the live suffix.
Global access completion records store the owner's epoch, not a full completion
clock; the full start clock and minimum-start summaries remain available for
both ordering directions.
Program storage, counter increments, port-report evidence and live symbolic
alternatives are separate retained state; an unsynchronized process can pin
history. Collection does not turn unresolved history into a certificate or
impose a smaller admission limit.
Exhausting the bounded walk/interval proof produces `model`, never a clean verdict.

A successful VHALT joins every engine and fill-landing clock before releasing
RUN-local dependency entries, queue ordinals and finished-kind histories. Each
subsequent RUN starts after that joined clock. Register/memory provenance,
response histories, RX/CR targets and fill tracking persist. Fault halts do not
release unfinished queues. Queue storage is proportional to the largest RUN,
not the sum of completed RUNs; immutable decoded fields are shared by instruction
word. A local-memory access retains only its owner's event number, the component
used by its race comparison, rather than another full vector-clock root.

P3 port checking indexes FILL-start port epochs with range minima. For each
incoming burst it skips the FILL-completion prefix observed by the sender and
finds the first remaining start earlier than its landing. Shared FILL starts may
be nonmonotone. With F FILLs and B bursts the index takes O(F) space and
O(F + B log F) work without dropping any collision window.

`KTPU_VERIFY_STATS=1` enables a retained-state census at run boundaries and
geometrically spaced scheduler sweeps. It names counter owners, response streams,
interval histories, instruction queues, VC entries and capacities, known-value
publications and symbolic arenas. Clock backing allocations are deduplicated by
identity within the sampled branch. Branch counts are separate; these figures
are not process RSS or a union over all saved alternatives. Stage harness phase
markers distinguish lowering/building from verification. Dynamic IMEM residency
analysis also reports block-input states, distinct shared memory roots and cells.
It retains must-known states only at control-flow block entries and derives
per-instruction elimination masks by replaying each block from its fixed-point
input. Read-only blocks share a memory root; only a changed cell or intersection
copies it. Straight-line step count therefore does not multiply retained IMEM
state. Both incoming paths, loop backedges, opaque units and patched cells keep
their ordinary must-known semantics. The census adds no proof edges, limits, or
device instructions and is disabled by default.

**Round-i verification lines 234–236 / L1.VER.009/.017:** bounded proof work must
retain every physical conflict and refuse unknown footprints. For a fixed target,
memory-proof work is `O(R log R + I log S + C)`: R candidate footprint runs before
normalization, I touched intervals and eligible response-stream queries, S stored
interval/history/response records, and C visited causal-index nodes, conflict
reports and vector-clock component work. Report deduplication uses expected
constant-time hashing. C is not just the number of diagnostic strings: sparse
unordered leaves can require internal history-node comparisons. Genuine conflicting
histories can exhaust the budget and remain refusing. Footprint enumeration is
bounded before allocation. Descriptor dimensions translate and merge intervals;
a contiguous dimension extends its interval directly, including negative strides.
The logical word-count budget is checked independently of this compression.
Contiguous walks therefore take O(rank) footprint work without expanding their
individual words, bytes or lines. Symbolic alternatives are
separate proofs; cold scheduling, retained scheduling and combined execution all
retain the same checks.

**ISA.MV.027/.029/.033/.038/.050/.051:** physical footprints follow issued
transactions. A masked COPY or GATHER destination suppresses both payload
accesses; an enabled COPY destination with a masked source writes IMM without
reading it. SCATTER with a masked source still writes IMM to its indexed
destination. The index cache reads complete card words, including unused lanes
of its final word, independently of destination masking. Unknown u32 indices
need no source-domain proof for wholly masked GATHER rows, but enabled indexed
accesses require visible values or a disjointness proof. fp32 index validation
also applies to masked rows; an unresolved validation remains `model`.

**ISA.VC.124/.125/.128 and L0.MEM.007:** every issued VFILL contributes its
physical read walk, including a prefetch whose result no arithmetic consumes.
Relative offsets advance at dispatch and reset at RUN. Allocator alignment gaps
are not an accessible tail. Local VSYNC/VMARK and a final package barrier do not
retroactively order a read against another VC's earlier writes. Numerical
agreement alone cannot certify these physical accesses.

**ISA.VC.128 and the memory-response contract:** a vector drain's local retirement
is not its external commit. Response processes are distinct from MG/VC execution
and from each other at different destinations. An ACK_FENCE completion carries
local write responses; an internal VSYNC does not. A fenced ring drains preceding
CPU/mover work and carries the destination's inbound write responses. It cannot
release unacknowledged compute work still inside the mesh. The sender learns no
receiver-arrival clock merely by enqueueing a ring. Acquires and consumed markers
carry the observed clocks. Host submission order supplies no dependency.

#### Memory-ordering edges

Round-i verification requires real causality, not simulator visitation order.
The following contracts apply to all MG/VC and node access sites:

| Operation | Required ordering and model edge | Authority |
|---|---|---|
| Two writes from one source to its fixed memory port | Complete bursts issue in source order. WAW on the same response stream is ordered even while acknowledgements are outstanding. No execution clock acquires that completion. | [Memory protocol §7.1](../../spec/memory-protocol.md#71-guaranteed); `src/kohakuaccel/sysnode/core/mag_mem_port.v:605-634,904-913,952-958` |
| Read and write, including one unit's FILL after DRAIN | Independent read/write machines offer no program-order fence. A queued VDRAIN sync only frees its local source storage. | [Memory protocol §7.2](../../spec/memory-protocol.md); ISA.VC.128/.142/.144; ISA.MG.030/.032 |
| Completed FILL before a write | VFILL completion carries the landing clock; a VSYNC/VBAR that consumes it orders subsequent work. MG FILL retires after its completed-entry count reaches its request. | `docs/spec/stack/isa/vector.md:173,184-188`; `docs/spec/stack/isa/cluster.md:71`; `src/kohakutpu/matmul/mx_cluster_cu.v:940-950` |
| Observed CU instruction completion | The endpoint signal joins acknowledged local memory writes, not CU_DATA landings or remote posted writes. The sequencer and core do not acquire the signal just by continuing. | L0.STEP.012; [Compute-unit port §3.3](../../spec/compute-unit-port.md); `src/kohakuaccel/noc/endpoint/noc_cu_base.v:219-233,368-387` |
| `wait U upto=mark`, AWAIT, FRONTIER and DE WAIT | Consume the selected instruction-retirement frontier, not DATA_RECEIVED and not other units' frontiers. End barriers consume all issued frontiers. | L0.STEP.020/.021/.022; L0.DE.006/.009/.017 |
| Mover GO, MWAIT and nonposted move | I_DRAIN consumes outstanding reads/writes before I_DONE. The next GO is after it; the node acquires only the requested completion or an all-done boundary. POSTED itself exports no completion. | ISA.MV.011/.080/.093; L0.STEP.026/.027 |
| CPU/mover remote write response | Route bits select the destination before DRAM alias normalization. The remote response is posted, for DRAM as well as staging; only destination publication includes remote commit. CU ordinary DRAM requests instead reach the issuing mesh. | `docs/spec/stack/l0/steps.md:187-189`; `docs/spec/stack/isa/mover.md:27`; `src/kohakuaccel/sysnode/core/mag.v:796-802,995-998`; `src/kohakuaccel/sysnode/interlink/mag_ilink.v:359-362,550-575` |
| Fenced ring and consumption | Arrival publishes CPU/mover writes and the completed CU frontier released by the sender; the receiving WAIT_SLOT/WAIT_BELL consumes it. Sending alone acquires nothing. | L0.STEP.031/.034/.035/.048; `docs/spec/node-queue.md:310-311` |
| Xache home | A home serializes service but does not select a causal order among independent sources. Reads and writes use different engines. No home-wide clock join is introduced. | `docs/spec/memory-protocol.md:464-467`; `docs/projects/kohakuaxi/xbar-cache.md:238-249`; `src/kohakuaxi/xache/engine/kx_wr_engine.v:129-170` |

The endpoint's RTL allows datapath progress while a signal awaits writes
(`sig_go = sig_pend && wr_clear`, with `send_ready` gated by `sig_go`, not
`sig_pend`). Consequently a pending completion is not an internal FILL fence.
A completed-write response is exported only at the observed completion or release.
Response histories are indexed by `(issuer, destination)` and by local unit / issuing
mesh. Their monotonically issued event numbers select a prefix by predecessor
search; its last response clock already contains every earlier response on that
stream. Completion and release join that one clock, not the entire write history.
CPU/mover publication uses the last eligible destination response; CU publication
uses only the prefix covered by the acquired retirement. Known STORE bytes use the
same indexed response. No lookup joins a later write, another unit or another route.

Write FIFO order is a WAW relation, not completion-before-start for all events.
The interval index therefore retains older posted writes; a query skips a block
only when a genuine completion-before-start relation covers all its accesses. A read can precede a later write and
still race an earlier posted write. Independent sources, including units on one
sysnode, retain the 64-byte ownership obligation; sharing Xache is not a waiver.

The card-stage harness preserves supplied physical aliases and allocates generated
staging in its declared node's aperture (node zero when unspecified). Generated
DRAM uses one physical arena irrespective of symbolic memory-space spelling.
This implements L1.BODY.001/.006 and L0.MEM.007: an address bound to an L1 parameter
is already physical, not a space-relative offset to reinterpret in the verifier.
The complete generated staging allocation must fit that node’s staging capacity;
validating only the aperture prefix is insufficient. Known STORE values use the
same response destination as the write access for their publication provenance.

### 7.6 Physical proof context and retained state

`MemoryContext` fixes physical package section locations and initialized input
bytes at preparation. An unplaced package has its own pinned, allocator-owned
storage identity, disjoint from tensor allocations. Its step and payload sections
have separate `(mesh, run, section)` identities and section-local 64-bit limb
indices. L0.STEP.053/.072 PATCH accesses complete aligned limbs; step and payload
fetches are aligned 16/32-byte records, so limb overlap is exact for private
storage. A placed package instead uses its actual step and payload byte addresses.
Its aliases participate in the same byte proof. This is the managed runtime allocation contract, not a claim that arbitrary
caller-chosen addresses are private.

Only address-bearing values are interpreted. CPU stores establish bytes with
publication clocks; DMA writes invalidate known values. A LOAD or indexed walk
uses an exact value only after its defining event. Unknown indices or patched
addresses without an ownership proof produce `model`. Known index tables are
accepted and their reads are instrumented, so modifying a table needs the same
ordering as modifying data. STOREs into placed descriptors change the subsequently
executed footprint. PATCH touches complete 64-bit limbs, not just the selected bits.

Whole-card scheduling continuations retain visible memory values and invalidations
as well as unit residents. Run-specific package placements retain their run ordinals.
The actual proof executes cold and retained packages together with memory history
intact. Its cache identity includes every bound package, target/source digest,
physical context and event instructions; a single-sysnode or differently bound
certificate cannot certify another alias environment or slot program.

## 8. Reruns

**Theorem 4 (rest).** Suppose a package P₁ followed by its rerun P₂ (P₂ built
against what P₁ left resident) runs to the end with no `rest` finding:

- every counter equals what its waiters took;
- every doorbell and slot a wait reads was taken;
- every DATA_RECEIVED arrived inside its package.

Then every further run of P₂ behaves as the second.

*Proof.* Every guard compares a counter with a threshold accumulated by its
waiter. With no rest, each counter equals its waiter's total threshold at the
end of each run, so the counters minus thresholds are zero at the start of
runs 2 and 3. IMEM and DESC are what the residency built P₂ against. AINC holds
what each image sets: the image encoder writes `ainc aN 0` for every
descriptor a `rel` transfer steps before the image sets it. The walk pointers
are set by the images that use them. So run 3 starts from run 2's start state shifted by constants
in every counter and threshold. Every guard is a difference, so run 3 takes
the same actions. Induct. ∎

The check runs P₁ and P₂ and the host barrier between them (`card.run` waits
for every node before the next submission).

## 9. Assumptions

| | assumption | why it is reasonable |
|---|---|---|
| A1 | The mesh delivers every flit to a destination whose port is ready, and flits of one source to one destination arrive in order. | Dimension-ordered routing with credit flow control. Ordering is a property of the routers (`vec_cu.v` relies on it to detect a second sender). |
| A2 | A unit with an instruction available and its guard satisfied eventually takes it (fairness). | Every unit is a single FSM with no arbitration it can lose forever. |
| A3 | Memory answers every request. | MAG and the DRAM model. A memory fault is a hardware fault, outside the program. |
| A4 | Counter widths: a cluster's landed-entry count is 10 bits, its credit count 16 bits. | Not checked. The outstanding count of every compiled stream is bounded by its ring (2 slots). |
| A5 | A unit's memory write is visible to any reader ordered after that unit's completion. | `noc_cu_base` holds a completion until every MEM_WR_REQ the unit sent is acknowledged (write fence, `ACK_FENCE 1` on clusters and vector cores). Cross-mesh writes are fenced by the ring (`interlink.md`). |
| A6 | A node loop on loaded data is checked for 0..`trips` trips. | Reported as `loop`. Its body being the same on every trip is checked by the scheduler (§6). |

## 10. Register-file banks: `verify.lint`

`verify.lint(module, machine)` checks every image of a module, hand-written
or compiled, against the read copies of `machine`'s vector cores
(`vc.rf_copies`; `ktpu-verify/src/lint.rs`). A register file is
two banks by chunk parity, each with that many read copies; math reads take
them first. One `bank` finding names a math statement with the beats it
costs: on two copies, a beat reading three distinct words of one bank holds
the math engine a cycle, and a beat that uses both copies of a bank leaves the
pack engine none that cycle. These are cycles, never wrong values, so `check`
does not report them. The vector lowering places compiled values to avoid them
(`banks.rotate`); a hand-written image avoids them by the same rotation.

## 11. Where it runs

- The Rust compile pipeline schedules and verifies every compiled package.
- `VerilatorCard(check="refuse")` and `tools.l1 --check` gate the compiled
  verdict; the runtime accepts only prebuilt Rust packages.
- The checker tracks dependencies, counters and accesses. It does not compute
  tensor values and is not a behaviour-model oracle.
