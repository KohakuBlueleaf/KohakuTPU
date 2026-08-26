---
title: Programs across several meshes
summary: Why a machine with more than one mesh needs a new axis in the IR, what that axis constrains rather than schedules, and where sharding is decided.
tags:
  - integrate
  - compiler
  - mesh
  - design
---

# Programs across several meshes

> **Kind: Convention throughout, and free.** The framework has no opinion about
> how a compiler represents meshes; the two facts it *does* fix — that a mesh
> owns its memory, and how a transfer crosses a boundary — are Fixed protocol and
> are specified in [spec/memory-protocol.md](../spec/memory-protocol.md) §9.3.
> Everything else on this page is one design worked out against those two, offered
> as a starting point.

A **mesh** is one grid of routers, the endpoints hanging off them, and one system
node with its own DRAM behind it. A device image holds up to four.

[mesh-topology.md](mesh-topology.md) §6 reaches one conclusion about big
machines: **size a mesh to one die, and reach for more dies by adding meshes.**
This page is what that costs on the software side, and it is more than it looks.

A second mesh is not a wider first one. It is a second memory, and every
consequence on this page follows from that.

---

## 1. The constraint: a mesh owns its memory

A mesh's units fetch operands through *their* memory agent, which serves *that*
mesh's memory. Nothing about a second mesh's memory is visible in the first
mesh's address space. So an address is not a number — it is a **pair**, `(mesh,
offset)` — and the same offset on two meshes names two different bytes.

Three things follow immediately, and a compiler that misses any of them
generates programs that are wrong rather than slow:

- **A value is not shared by being addressed.** Two meshes needing the same
  weights need two copies, uploaded separately. There is no view, no mapping and
  no coherence.
- **Two regions on different meshes never alias.** A dependency inferred from
  overlapping byte ranges must compare the mesh first, or every mesh serialises
  behind every other for no reason.
- **A fetch cannot be shared across meshes.** Coalescing gives one read request
  extra destinations, and a destination is a coordinate — which identifies a unit
  only within one mesh.

The framework's `Region` therefore carries its mesh
(`compiler/kohakuaccel/ir/l2.py`), and both `overlaps` and coalescing group on it.

---

## 2. Describing a machine that is not uniform

Meshes on one device need not be alike, and assuming they are is the failure this
part exists to prevent: a machine description with one flat unit table says the
wrong thing about every mesh that differs from the first.

`MachineSpec` therefore holds a `MeshSpec` per mesh — its units, memory ports and
orchestrator — and the flat accessors answer for a default one, so every caller
written before the machine had several keeps working
(`compiler/kohakuaccel/machinespec.py`).

Two design rules worth copying:

- **A unit type a mesh does not carry is ABSENT from its table, never present and
  empty.** "No vector core here" is a fact about the silicon; a placement that has
  to refuse should read it off the table rather than off a length.
- **A refusal names where the work could go instead.** `coords("VC", mesh=1)`
  raising "mesh_1 has no 'VC' units, and meshes carrying 'VC': [0, 2, 3]" is
  actionable; "no such unit" is a dead end. On a machine whose meshes differ, the
  useful answer is almost never *give up* — it is *put this stage somewhere else*.

---

## 3. The mesh axis constrains; it does not schedule

This is the distinction an implementation gets wrong first.

Placement across the units *within* a mesh is a **scheduling** decision: any unit
of the right type can run the task, and the pass picks one by load or by hops.
The framework is free to choose, and choosing badly costs time.

Which **mesh** a task runs on is not that. A task cannot run where its operands
are not, whatever the load says. The mesh is decided by the data, and placement's
job is to *honour* it, not to optimise it:

```python
task.mesh          # stated, or None to infer it
```

`Place` resolves the mesh before it considers a coordinate, in this order
(`compiler/kohakuaccel/passes/place.py`):

1. the task's own `mesh`, if it names one;
2. otherwise the mesh its **reads** live on — a unit fetches through its own
   memory agent, so its operands fix where it runs;
3. otherwise the mesh its **writes** live on;
4. otherwise the machine's default.

Reads decide before writes because the two are not symmetric: **operands must be
local, results need not be.** A unit writing to another mesh is an ordinary
remote transfer; a unit reading operands from two meshes at once has no mesh to
run on, and that is refused rather than guessed.

One consequence that is easy to miss: **load must be accumulated per (mesh,
coordinate)**, not per coordinate. The same coordinate on two meshes is two
different units, and one counter for both balances a task against work on a mesh
it cannot see.

---

## 4. Traffic between meshes is a result, not an input

`Place` leaves `place.remote` in the context: the tasks whose regions touch a
mesh other than the one they run on. That set is worth having explicitly.

- It is exactly **the traffic the interlink has to carry**, so a cost model has
  something to price and a report has something to show.
- It is exactly **what a backend without an interlink must refuse**. A machine
  with one mesh produces an empty set, so the check costs nothing to leave in.

Note the direction of the dependency: nothing *asks* for cross-mesh traffic. It
falls out of where the data was placed, which is the point — the compiler should
report the interconnect cost of an allocation, not be handed it.

---

## 5. Sharding is an allocation decision, not a scheduling one

The instinct from single-device compilers is to treat a split as a loop
transformation: pick a tiling, and the data follows. That is backwards here.

Because a mesh's memory is separate, **a sharded value is a separate upload per
mesh**, decided when the value goes to the device and not when the kernel that
reads it is scheduled. By the time a scheduler is looking at a kernel, the
question is already answered.

That inverts the usual layering, and three difficulties come with it:

- **A value's split axis is chosen by its CONSUMER.** The same tensor is split one
  way if the next operation contracts over the split axis and another way if it
  does not. So placement cannot be decided when a value is created — only when
  the graph around it is known.
- **Residency outlives the kernel that motivated it.** Weights that stay resident
  across calls carry their placement with them, and re-splitting later means
  re-uploading across the interconnect.
- **Capacity is per mesh, and it is hard.** A value too large for one mesh must
  span several whether or not splitting it buys any speed.

The honest first version is **to let the user place values, and to REFUSE rather
than silently re-shard when a consumer disagrees**. A loud refusal is worth more
than a placement heuristic nobody can predict, and it is the thing that makes the
automatic version testable later — the refusals are the cases the automatic
version has to handle.

---

## 6. Collectives belong in the IR

When a producer's split and a consumer's expectation disagree, something has to
move. That something is a **collective**, and it belongs at the graph level as an
operation the compiler inserts, for the same reason a layout conversion does:

- it has a **cost** a scheduler must be able to see;
- it has a **shape contract** a verifier must be able to check;
- and it must be inserted **where the disagreement is**, which only the graph
  knows.

A collective lowered from the graph level is one the compiler can move, merge or
eliminate. One written by hand inside a kernel is none of those things.

Two properties of the underlying transport shape what the lowering may assume,
and both are framework-level facts ([mesh-topology.md](mesh-topology.md) §7):

- **The mesh does not learn that other meshes exist.** A remote transfer is
  addressed to the local memory agent, with the real destination in header
  fields. Instructions encoded before an interlink existed still mean what they
  meant, because zero reads as local.
- **The links form a topology, and it has a diameter.** A collective's schedule is
  a walk over that topology. Two meshes not directly joined cost two hops, so the
  order a reduction walks is a property of the machine, not a free choice.

---

## 7. The spectrum

The same discipline the rest of the stack uses — the automatic thing and the
manual thing are the same mechanism at different settings — applies one level up.
Four rungs, all legal at once:

| rung | who decides the split | what it buys |
|---|---|---|
| 1 | nobody — the whole machine is one device, the compiler places | correct by default; slow where it crosses a link |
| 2 | placement chosen so traffic fits the topology | removes the crossings rung 1 did not see |
| 3 | a deliberate parallel split, as across cards | the split a model author already knows they want |
| 4 | placement aware that meshes differ | uses a mesh that lacks a unit type for the work it *can* do |

**These are not four implementations.** They are one implementation with
progressively more of the decisions taken from the user, which is why rung 4's
scheduler improvements land in rung 1 for free: rung 1 *is* "the scheduler decides
everything".

The build order that follows from this is worth stating, because the tempting one
is wrong. **Ship rung 1, plus the manual knob at 2, 3 and 4. Then write real
kernels at 2, 3 and 4 and measure them against rung 1.** Those hand-written
kernels are the specification for the automatic version. Designing the scheduler
first means guessing at what wins on an interconnect whose bandwidth nobody has
measured yet — and every cost model on an unmeasured link is fiction.

---

## 8. Open questions

- **Whether the mesh axis should appear in the kernel language at all**, or only
  as a property of values. It is placement-bearing, unlike an ordinary grid
  dimension, so it does not behave like the loops beside it.
- **No cost model for a collective exists, and there is no measured link rate to
  build one on.** Every timing of the interlink in this tree predates the memory
  mover's rebuild and **must not be quoted**; there is no current figure. What
  survives the retirement is a structural argument rather than a number: the
  engine driving a transfer, not the link, has been the limit every time it was
  looked at, so a cost model keyed on link bandwidth would be modelling the wrong
  thing. Anything more than that needs a fresh measurement, and until one exists
  a collective's cost is **unmodelled**, not estimated.
- **Capacity is not checked.** Nothing refuses an allocation that does not fit a
  mesh's memory; it becomes a runtime failure with no diagnosis.
- **Whether a project's parallel splits should be a solver or a rule.** Within
  one workload family the pairing that avoids a collective is usually known and
  could simply be spelled out. Whether that generalises is not established, and
  a rule that is wrong for the next workload is worse than no rule.
- **Nothing checks that a machine description matches the device.** A mesh map
  generates hardware and a machine description tells software the same thing
  twice, with nothing comparing them ([software-stack.md](software-stack.md) §7).
