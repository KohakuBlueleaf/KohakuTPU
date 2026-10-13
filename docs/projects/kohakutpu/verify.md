---
title: Static verification of node programs
summary: The abstract machine a compiled package runs on, the proofs that one run of it decides deadlock, rerun safety, port exclusivity and L1 races for every schedule of the hardware, and the dispatch scheduler built on it.
tags:
  - kohakutpu
  - compiler
  - verification
---

# Static verification of node programs

`kohakutpu.compiler.verify` decides, before a package reaches a card, whether
it can hang, whether it leaves state that changes its next run, whether two
streams can collide on a unit's receive port, and whether any two accesses to
a unit's local memory are unordered. It does so by running the packages once on
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
| `race` | two accesses to one L1 location, at least one a write, with neither happening before the other | §7.2 |
| `bounds` | a burst the receiver rejects (past its buffer), or a peer drain past 256 words (F_LEN) | direct |
| `route` | a credit or a burst aimed where no unit is | direct |
| `loop` | a node loop whose trip count is data: checked for 0..`trips` trips (§5.2) | bounded |
| `model` | something the machine does not decide: a FETCH step (words written at run time), a VWAIT / VPOST / peer VDRAIN under a data-dependent skip (Lemma 4) | — |

Entry points:

- `verify.check(programs, machine)` builds the packages of a program's first
  run and its rerun (the vector cores' programs then resident), schedules them
  (§6) and runs both, back to back, on one machine. Empty means none of the
  above can happen.
- `verify.schedule(programs, machine, fetch, residents)` is the dispatch
  scheduler; `kohakutpu.compiler.build.packages` and `VerilatorCard.run` send
  only what it returns.

## 2. The machine

### 2.1 Processes

Every process is sequential and deterministic.

| process | one per | actions |
|---|---|---|
| node | mesh | the package's steps (`node.py`) |
| CU | vector core | IMEM / DESC words; a RUN; its completion at VHALT |
| front end | vector core | fetch, scalars, VLOOP, VSKIPZ, marks; dispatch into an engine queue |
| F, D, U, M, P engine | vector core | its queue in order (`vec_cu.v`, `v2_core.v`) |
| sequencer | cluster | FILL, GEMM, DRAIN, POST in order (`mx_cluster_cu.v`) |
| drain queue | cluster | the write port: bursts of what sweeps emitted and drains sent |
| port | unit | none of its own: each burst landing is one of its events |
| array | cluster | none of its own: each sweep's L1 reads are one of its events |

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
| node AWAIT u n | unit completions + DATA_RECEIVED from u | expected (+n) |
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

The machine runs the block (the constrained path) and reports any of the
three inside it as `model`. Races inside the block are checked on the path
taken. A skipped block writes nothing, so the path the machine runs holds
every access of either path.

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
(`target.vcopies`; `kohakutpu.compiler.l2.banks.lint`). A register file is
two banks by chunk parity, each with that many read copies; math reads take
them first. One `bank` finding names a math statement with the beats it
costs: on two copies, a beat reading three distinct words of one bank holds
the math engine a cycle, and a beat that uses both copies of a bank leaves the
pack engine none that cycle. These are cycles, never wrong values, so `check`
does not report them. The vector lowering places compiled values to avoid them
(`banks.rotate`); a hand-written image avoids them by the same rotation.

## 11. Where it runs

- `kohakutpu.compiler.build.packages` → `verify.schedule`: every package a
  `VerilatorCard` runs is scheduled.
- `software/compiler/tests/test_verify.py`: the examples verify clean, and
  each finding kind fires on a program built to have exactly that hazard.
- Cost: about 80k events a second in Python. The 2-head compiled flash
  checks (two runs, two passes) in 26 s.
