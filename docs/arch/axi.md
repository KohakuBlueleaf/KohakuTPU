---
title: AXI surface
summary: The boundary to everything that is not the framework — host, DRAM, debug — the dialects spoken on each side of it, and the discipline that makes crossing it survivable.
tags:
  - architecture
  - axi
  - boundary
---

# AXI surface

`src/kohakuaccel/axi/` — the layer where the framework meets things it did not
write.

**Where it sits.** Everything inside the accelerator speaks **flits**: one
fixed-width message, one link, one cycle ([noc](noc/)). Everything outside it —
a host DMA engine, a DDR controller, a debug bridge, a vendor interconnect —
speaks **AXI**. This layer is the translation, and it is the only place in the
tree where both dialects appear.

Two things are worth separating before anything else, because "AXI" in this
tree means both:

| | what it is |
|---|---|
| **the AXI surface** | the boundary itself: the slave face a host writes, the master face that drives memory, and the discipline every AXI interface here obeys |
| **the station bus** | the on-chip AXI *fabric* that carries traffic between those faces across the die. It converts AXI into its own flits internally and back at the far end — [projects/kohakuaxi](../projects/kohakuaxi/station-bus.md) |
| **the fused crossbar-cache** | the *memory* path from several AXI masters to several DRAM channels, with a cache per channel. One system with AXI only at its edges: no internal AXI hop, no per-hop buffering, a clock crossing only at a port that declares one — [projects/kohakuaxi/xbar-cache](../projects/kohakuaxi/xbar-cache.md) |

The second and third are two systems for two jobs and are never one thing: the
station bus reaches endpoints of many widths and clocks with host traffic, the
crossbar-cache reaches DRAM at one width with memory traffic.

## What it owns

- **A slave surface for the host.** One address space that decodes into a
  memory window, a control window, an instruction staging window and a
  pass-through window for whatever a client wants to expose.
- **A master boundary to memory.** Concentrating several internal requesters
  onto the channels a memory controller actually presents, converting the beat
  width, and crossing into the memory's clock domain.
- **The on-chip AXI fabric** that gets a manager's transaction from wherever it
  is on the die to the subordinate that answers it.
- **The burst and handshake discipline** every AXI interface in the tree
  follows.

## The problem it solves

Nothing outside the framework speaks flits. A host DMA engine, a memory
controller, a debug bridge and a vendor interconnect all speak AXI, and AXI is
substantially larger than what this machine uses: out-of-order completion by
ID, burst reordering, exclusive access, narrow transfers, cache and protection
attributes.

Two failure modes follow if this boundary is left implicit. The first is
importing AXI's generality inwards, so the fabric grows machinery to satisfy a
bus nobody asked for. The second is exporting the fabric's assumptions
outwards, so a vendor interconnect meets an interface that is *nearly* AXI and
does something arbitrary about the difference.

This layer exists so that the conversion happens once, in modules whose job is
only conversion.

## One decode is the whole control plane

An AXI write's **address** decides what it is: memory, control register,
instruction staging, or a raw flit to inject. That is the reason there is no
separate control fabric, and the reason a debug bridge can inject mesh traffic
with an ordinary AXI write and nothing else.

Two windows are wide rather than deep, and both are deliberate. The **staging
window** is sized from the number of instruction slots rather than fixed at one
page — a fixed page silently decodes the tail of a long program as register
writes, and the symptom is a program that stops early with no error. The
**pass-through window** forwards writes verbatim with the offset preserved, so
a client behind it keeps its own register offsets rather than having them
renumbered by an index.

## The three AXI roles, and the discipline they share

| Role | Shape | Where |
|---|---|---|
| manager | the framework reads and writes memory | one per memory port, plus upload, mover, interlink landing |
| subordinate | host writes registers, staging, memory | the control agent, the memory window |
| model | a subordinate that behaves like memory, for simulation | `src/kohakuaccel/verif/axi4_ram.v`, `axi_ram.v` |

Four rules hold across all of them, and each has a specific failure behind it:

1. **`VALID` is never a function of `READY`.** The reverse is a combinational
   loop between two compliant devices, and the AXI specification forbids it for
   exactly that reason.
2. **A burst ends because a counter says so, not because `WLAST` arrived.** A
   requester that miscounts its own data must not be able to desynchronise the
   response.
3. **`BID` / `RID` echo `AWID` / `ARID`.** AXI4 requires it, and this layer
   depends on it structurally — see [the ID trick](#concentration-arbitrate-first-then-cross).
4. **A burst must not cross a 4 KB boundary**, and `AxLEN` maxes at 255. An
   interconnect is permitted to do arbitrary things if you break the first
   rule; some split, some stall, some corrupt.

The reference manager that encoded rules 2 through 4 once —
`axi4_master.v` — has been **retired to `src/attic/legacy-axi/`**. It kept one
burst outstanding at a time, which was right for something meant to be read and
checked and wrong for anything driving real memory latency. The rules did not
retire with it; every manager in the tree still obeys them.

## The station bus

The shipped on-chip AXI fabric. It is what carries a host transaction from the
edge of the die to the subordinate that answers it, and it is the thing to
reach for rather than wiring a manager straight to its target.

**The shape is a line of stations**, not a tree with a root. Each station holds
some number of **manager shims** and **subordinate shims** on two shared paths,
and up to two **link ports** joining it to its neighbours.

| piece | what it does |
|---|---|
| manager shim (`sb_nmu`) | one external AXI manager onto the station: decode the address to a destination, re-express the burst as flits, reserve response space |
| subordinate shim (`sb_nsu`) | the reverse, at the far end: flits back into an AXI burst at a subordinate |
| hub (`sb_hub`) | N sources to 1 to N destinations. Injection is a mux; ejection is a broadcast with per-destination valid gating, so there is **no sources × destinations term** in it |
| link (`sb_link`) | one direction of a station-to-station hop, pipelined and **credit flow controlled** |

Five structural decisions carry most of the design:

**Request and response are separate physical paths.** They share no buffer, so
a response can never block behind a request. That is the same class of
guarantee end-to-end credit gives the mesh, arranged differently.

**Two asynchronous FIFOs per manager shim, not five.** Address and write data
pack into one request stream, and write response and read data into one
response stream — and a unidirectional stream is what an asynchronous FIFO
handles well. The shim takes **no parameter describing the clock
relationship**: not the ratio, not whether the two clocks are synchronous.
That is what makes it automatic.

**Arbitration is packet-atomic.** A hub grant is held until the last beat,
which gives AXI4's no-interleaving rule for free and commits a whole burst to
one destination.

**Links use credits, not ready.** A `valid`/`ready` link costs a bubble per
pipeline stage, or a skid buffer at every stage, and makes `ready` a long
backwards path across the die. With credits the datapath is a plain shift
register with no backpressure at all, so the pipeline depth is free to grow —
depth only sets how many credits are needed to cover it.

**Width conversion happens in the shim, natively.** A narrow port *places* its
beat into a byte lane of the flit, which is a demux, and extraction back is a
small mux. A manager wider than the flit splits its beat into several flits
instead. So a 32-bit control port and a 512-bit host port share one fabric
without a conversion block between them.

Its cost, its topology as shipped, and its measured figures belong with the
project that produced them:
[projects/kohakuaxi](../projects/kohakuaxi/station-bus.md).

### Three rules the station bus imposes on whoever configures it

These are the ones that fail quietly, so they are worth stating at
architecture level rather than leaving in a parameter list.

#### `REQ_DEPTH` must cover the longest burst

**`REQ_DEPTH` >= the largest `AxLEN` this port may issue, plus one.** A request
packet longer than the FIFO wedges the port: earlier packets always drain
because they carry a packet-complete token, and an incomplete one has no token
yet, so nothing can move.

This is not a theoretical bound. `sb_line4.v` records that **16-deep FIFOs
wedged every burst over 16 beats on v6.5 hardware** — the failure was observed
on silicon, not in a bench.

**The request queue has no automatic floor, and the response queue does.** The
shim derives a minimum response depth from the port's declared burst limit and
raises `RSP_DEPTH` to it silently. It does **not** do the same for `REQ_DEPTH`:
whatever you pass is what you get, subject only to the vendor FIFO's own
depth-16 minimum. So the request queue is the one an integrator has to size by
hand.

**Size it against what the port may legally issue, which is set by its width.**
A port that does not declare a burst limit inherits the 4 KB bound, and that
bound is a function of the data width:

| port width | longest legal burst | note |
|---|---|---|
| 32-bit | 256 beats | 4 KB / 4 B is 1024, capped by AXI4's `AxLEN` at 256 |
| 64-bit | 256 beats | 4 KB / 8 B is 512, capped the same way |
| 512-bit | **64 beats** | 4 KB / 64 B. A deeper queue here is sized for a burst that cannot legally arrive |

So the obligation is a **pairing**: either declare the real burst limit and use
a shallow queue, or leave the limit unbounded and size the queue to the width's
4 KB bound. **A shallow queue with no declared limit is the one combination
that wedges.** On an AXI4-Lite manager the declaration is a fact rather than a
hope, because Lite is single-beat by protocol.

The shipped line station is the worked example (`sb_line4.v`): its two bulk
managers — 64-bit and 512-bit — take `REQ_DEPTH` and `RSP_DEPTH` of **256** and
no burst limit, while its 32-bit control manager takes **16** paired with
`MAX_BURST = 1`. A second topology derives the same rule from the width
directly (`sb_root9.v`): 64 for the 512-bit bulk manager, *"not 512: AXI4's
4 KB rule caps a 512-bit port at 64 beats, so a deeper queue is sized for a
burst that cannot legally arrive"*, and 16 with `MAX_BURST = 1` for the control
port.

**Getting it right is close to free, and free in block RAM.** `sb_line4.v`
records the cost of raising those queues to 256 as **+71 LUT at a 64-bit port,
+88 LUT at a 512-bit port, and +0 BRAM** — because a `RAMB36` row is 512 deep,
so the previous depth of 64 was paying for rows it never used. *(Figures quoted
from that file's inline comment; no report in this tree reproduces them.)*

**Dropping the packet-complete token removes the guarantee entirely.** It is
there to save logic, and it makes the hub hold its grant through an underrun,
stalling the whole station — **and it removes the property that `REQ_DEPTH`
covers a burst at all.**

#### Credit is reserved in flits, not beats

A request is injected only once space for its **complete response** is
reserved. That reservation is counted in **flits — the unit the response queue
actually holds — not in AXI beats.**

The distinction is invisible until the port is wider than the fabric. A manager
whose beat splits into several flits needs reserved room for every one of them,
so a 512-bit manager over a 256-bit fabric needs 128 entries where 64 looks
perfectly reasonable. **Under-sizing hangs the port; it never overflows it** —
the credit check simply never passes, so the symptom is silence rather than
corruption.

The shipped topologies compute the response depth from exactly that product.
`sb_root9.v` sizes its bulk response queue at **128 when the fabric is narrower
than the manager and 64 when it is not** — 64 legal beats, doubled because *"a
512-bit manager on a 256-bit fabric returns two per beat"*.

The shim also protects itself, which is why this is a rule about *declaring*
rather than about sizing: it raises the response depth to the burst limit times
the split factor, whatever depth was asked for. What it cannot protect against
is a manager issuing a longer burst than the limit it was configured with — a
simulation assertion reports a read asking for more credits than the queue can
ever hold, and silicon does not.

The same counting rule appears in the mesh, for the same reason
([flits-and-links](noc/flits-and-links.md#two-kinds-of-flow-control-for-two-different-failures)).

#### An AXI4-Lite subordinate needs a converter, and a burst limit

A Lite subordinate accepts one beat per complete handshake and may legally
ignore `WSTRB`. `sb_axi2lite` sits at every Lite port and does the conversion:
one Lite handshake per beat, IDs captured and reflected, responses coalesced
worst-case, and **zero-strobe write beats consumed but never issued** — because
a Lite subordinate that ignores `WSTRB` would otherwise write zeros over live
data.

On the manager side, a Lite manager is single-beat by protocol, so its shim is
told so explicitly. Declaring the limit means a longer burst **hangs the port
rather than corrupting it**, which is the failure you want.

## What the boundary drops

Two AXI features do not survive the crossing, and both fail silently in
silicon.

**Burst type.** A flit's memory descriptor is an address, a length and a small
flags field; there is no burst-type field to carry
([flits-and-links](noc/flits-and-links.md#what-a-flit-deliberately-does-not-carry)),
and the memory port at the far end drives `INCR` unconditionally. So a manager
that issues `WRAP` or `FIXED` gets **`INCR` executed instead**. A simulation
assertion in the manager shim reports it. Nothing in silicon does.

**Optional attributes.** Lock, cache, prot, QoS and region are not carried, and
that is deliberate rather than unfinished: no manager in the design drives
them and every subordinate takes its defaults. A design that needs exclusive
access has to say so at a higher level; the fabric has no way to express it.

Both are cases where the honest statement is *this is not supported*, not *this
is supported and slower* — and the reason they are stated here is that neither
returns an error.

## Concentration: arbitrate first, then cross

Several internal requesters have to reach one memory. The naive structure
crosses each requester into the memory's clock domain and then arbitrates
there, which needs five asynchronous FIFOs *per requester*. Arbitrating first
and crossing once needs five in total, whatever the requester count is.

```
  requester domain                              memory domain
  N x AW --round robin--> [awq] ------------------------> AW
         push index
  wsel  ----------------> W mux, head until wlast --> [wq] --> W
  N x AR --round robin--> [arq] ------------------------> AR
  N x B  <-- demux by id --------------- [bq] <---------- B
  N x R  <-- demux by id --------------- [rq] <---------- R
```

**Response routing is the ID, not a table.** The requester index is prepended
to `AWID` / `ARID`, so `BID` / `RID` say where the response goes. No scoreboard
is kept and none has to be sized. The cost is that the subordinate's ID width
must be wide enough to carry the index, which is why the module derives it
rather than taking it as a parameter.

What this deliberately is *not*: address decode (there is one subordinate, so
there is nothing to decode), protocol conversion, or arbitrary topology.

## The fused crossbar-cache

Where several masters reach several DRAM channels *and* each channel wants a
cache, the concentrator above is not the shape: it has one subordinate. The
vendor shape is a crossbar IP in front of a cache IP per channel, which is three
AXI endpoints in series and copies every wide beat at each of them.

`kx_xache` (`src/kohakuaxi/`) keeps AXI at the two edges and nothing
AXI-shaped between:

| piece | what it carries |
|---|---|
| one array per home (`kx_carray`) | the only wide store: a URAM row of `{valid, tag, line}`, the hit compare, the served word, and the fill taken straight off that home's DRAM `R` channel |
| engines (`kx_rd_engine` or `kx_rd_pipe`, `kx_wr_engine`) | control only — arbitration, one request record, the DRAM address channel, and the *index* of the home or master the fabric should select. The read engine is a knob: one beat per array round, or a lookup every cycle with a miss fetching the rest of the burst and `RD_OUTQ` bursts queued per master in order |
| the crossbar | not a module: an N:1 per master and an M:1 per home on **registered binary** indices the engines publish |
| edges (`kx_link`) | per port and per channel, a wire when the port shares the fabric clock and an asynchronous FIFO when it does not |

Three properties follow and each is the reason for a design choice elsewhere on
this page:

**A crossing exists only where a port says its clock differs.** The fabric
runs on one clock; each master and each home carries one bit saying whether it
is on that clock. So the crossing count is the count of ports that differ,
which clock the fabric runs on is the integrator's choice, and a cross-die port
is simply a port that differs.

**The engine grouping is a knob, on each side independently.** One engine per
home serves every home in parallel; one engine for all homes serialises them
and collapses the write-side fan-in. Read and write choose separately, because
their per-home costs are not alike.

**The line is `K` IO words.** At one word per line a full-strobe write
allocates; at more, a write invalidates and a fill assembles the line from the
channel's burst. The number and its costs are the project page's.

The whole measured table, the per-knob costs and the vendor path at the same
shape are [projects/kohakuaxi/xbar-cache](../projects/kohakuaxi/xbar-cache.md).
No figure appears here.

## Width belongs at the boundary

The mesh's internal beat matches the flit payload, so that nothing in the
fabric or the memory agent ever gears between two widths. Real memory is wider.
The packing therefore happens in the same module as the concentration and the
clock crossing, at the edge — which is what lets a device image change its
memory width without any module inside the mesh knowing.

## Clock domains

The routers and the control agent share one clock, so there is no crossing
inside either. Domain boundaries exist in exactly three places:

- **memory**, in the concentrator above, through asynchronous FIFOs;
- **the host**, in whichever vendor interconnect merges the debug bridge and
  the DMA engine onto the control path — which is already multi-clock and is
  the right place to leave it;
- **a station-bus manager port**, whose shim crosses into the bus clock with no
  parameter describing the relationship;
- **a crossbar-cache port that declares itself off the fabric clock**, per
  port, at the edge; a port on the fabric clock has no crossing at all.

A fourth exists inside a mesh and belongs to that system rather than this one:
a compute unit may run on its own clock behind a local-link crossing, while
router-to-router stays one domain
([noc](noc/README.md#one-clock-per-mesh-and-one-exception)).

Which clocks exist and what they may be retuned to is [physical](physical/).

## The control-program engine

`src/kohakuaccel/verif/main_orch.v` is an AXI subordinate so the host can load a
program, and an AXI manager so it can execute one. Three opcodes:

```
  WR    addr, data          issue an AXI write
  POLL  addr, mask, want    read addr until (data & mask) == want
  DONE  code                stop, latch code, raise the done flag
```

Three is enough because the machine's entire control surface is memory-mapped;
branches or arithmetic here would duplicate the host to no purpose. The value
is that a run of the machine becomes **one host transaction**. The host is not
in the loop per poll, and the same program works over a debug bridge and over a
production DMA path — which matters more than it sounds, because a single debug
read can cost milliseconds against microseconds of compute.

It is not a fabric node. Its reach into the mesh is an AXI write into a control
window, which the control agent turns into a flit — so dispatch, configuration
and debug injection all share one mechanism.

It now lives under `verif/` rather than beside the synthesisable AXI modules,
which reflects how it is used: as a driver-side sequencer exercised in
simulation, not as part of a shipped image.

## The memory models

Simulation needs a subordinate that behaves like memory. Two exist and their
difference is instructive: one is a **reference** — it implements `INCR`,
`FIXED` and `WRAP` bursts, byte strobes and ID reflection, and exists to be
read as the correct shape of a subordinate. The other is a **stub** — `INCR`
only, one outstanding transaction per port, several independent channels over
one array — and exists so that a system test fails because the system is wrong
rather than because the stub grew its own bugs.

A single-port model is a model of a narrower memory system than any real
target, and once enough compute units sit behind it the stub becomes the answer
rather than the scenery. Multiple independent channels over one address space
is what a multi-channel controller actually offers, and it is the honest shape
to test against.

Note that the *reference* model implements the two burst types the fabric
cannot express. That is not a contradiction: it is a model of a real
subordinate, and a real subordinate has to handle what a real manager might
send it.

## How it maps to real circuit

**Concentration is five queues and two round-robins, and that is the point.**
The cost does not grow with requester count the way a crossbar does, because
there is no crossbar: there is one subordinate, so arbitration is a mux and
response routing is a decode of bits that are already in flight.

The depths split by job. Address queues only have to cover the crossing
latency, so they are small and want distributed RAM. Write and read data queues
are sized for burst throughput, so they are deep and wide and want block RAM.
The parameters are separate for that reason. The station shims take the same
split further: each FIFO class names its own storage primitive, and a design
that will not spend block RAM on its interconnect sets all of them to
distributed.

**The vendor comparison is the honest way to size expectations.** A
general-purpose interconnect configured to do this job carries address decode,
width conversion for widths you do not use, and protocol machinery you do not
drive. Replacing it with modules that only arbitrate, only route responses by
ID, and only cross one clock boundary is a large reduction — and the reduction
comes from what was removed, not from cleverness. Where an interconnect is
genuinely doing several jobs at once — width conversion *and* multi-subordinate
decode *and* three clock domains — it stays, because nothing here replaces it.

For the measured version of that comparison, see
[projects/kohakutpu/results](../projects/kohakutpu/results.md) and
[projects/kohakuaxi](../projects/kohakuaxi/station-bus.md). No figure appears
on this page: framework pages carry none.

**Swapping vendor IP for RTL moves the wiring from a block design's inference
to your port list.** The rule that survives it: an unconnected output is
harmless; an undriven input is the fault. Interface-inference attributes on the
port list are what let a block design still tie clocks, resets and interfaces
up on its own — see [workflow/build](../workflow/build.md).

## Fixed protocol, addon, convention, or yours

| Thing | Category |
|---|---|
| the four discipline rules, on every AXI interface in the tree | **fixed protocol**. They are what makes vendor IP behave |
| the window structure of the host address space — memory, control, staging, pass-through | **fixed protocol** — [spec/control-registers](../spec/control-registers.md) |
| the control program's three opcodes | **fixed protocol** |
| **DRAM-port beat packing** — the ratio between the internal beat and the memory beat | **customizable addon**. The concentrator is written around a ratio, not around a width |
| queue depths, and which of them are block RAM | **customizable** — the split by job is the part to keep |
| the station-bus topology: how many stations, which port goes where | **yours**, per device image |
| the conventions below | **convention** — one forced by the build flow, two free |
| **where each window lands in the address map** | **yours**, per device image — see [ship](ship/) |
| what a pass-through window means to the client behind it | **yours** |

## Conventions

**Command a submodule through a slice of the control window, never through
loose sideband ports.** *(Forced, by the build flow rather than by logic.)* A
block design carries clock, reset and AXI across a module boundary and nothing
else. Sideband ports do not get wired, and the failure is that a shipped engine
is commandable by nothing — which is exactly what happened to the memory mover
before its command path moved into the window. Preserve the client's own
register offsets inside the slice, so it keeps its own numbering.

**Reach a memory agent through the station bus, not directly.** *(Forced.)* A
manager wired straight at an agent bypasses the decode, the credit reservation
and the width conversion, all three of which are the bus's job. The failure is
not a compile error.

**One run of the machine should be one host transaction.** *(Free.)* That is
what the control program's three opcodes are for. A host that polls per step
works, and costs a host round trip per poll — which on a debug path can be
milliseconds against microseconds of compute. The same program then runs
unchanged over debug and production paths.

**When you replace vendor IP with RTL, remember that an unconnected output is
harmless and an undriven input is the fault.** *(Free.)* Keep the
interface-inference attributes on the ports so the tool still ties clocks,
resets and interfaces up on its own — see
[workflow/build](../workflow/build.md).

## What a compute-unit author must know

Almost nothing, and that is the intent. A compute unit never sees AXI. It emits
memory requests as flits and the memory agent deals with bursts, boundaries and
widths.

Three things leak through and are worth knowing:

1. **Your requests become bursts, and bursts have rules.** A very short entry
   is a very short burst, and burst overhead is paid per request. Asking for a
   run of entries is not only a latency optimisation; it is what makes the
   generated bursts worth issuing.
2. **The host's view of your unit is an address.** Instruction staging, control
   registers and status all arrive through this surface. If you want something
   observable from software, the path is the fabric's control-register
   interface — not a new AXI port.
3. **You cannot ask for a `WRAP` or `FIXED` access.** There is no way to
   express one, and no error if you assume there is.

## What this layer deliberately does not do

- **No exclusive access.** `AxLOCK` is not carried. Mutual exclusion between
  two writers is expressed above this layer or not at all.
- **No cache or protection attributes.** `AxCACHE`, `AxPROT`, `AxQOS` and
  `AxREGION` are dropped. Nothing here is coherent with a host cache.
- **No out-of-order completion within a manager port.** The ID carries a
  routing index, not a reordering tag.
- **No burst splitting at the boundary.** A manager that violates the 4 KB rule
  is a bug at the manager, not something this layer repairs.
- **No error recovery.** A failed transaction returns its AXI response and
  nothing retries it.
- **No address translation.** Windows decode; they do not remap page by page.

## What this system does not own

| Not owned | Who owns it |
|---|---|
| flits, routing, the compute-unit port | [noc](noc/) |
| descriptors, and what a memory request means | [sysnode](sysnode/) |
| the DRAM controller itself | vendor IP. This layer terminates at its AXI interface |
| the host DMA engine | vendor IP, likewise |
| the address map's *values* — where each window lands | [ship](ship/) fixes them per device image; this layer only decodes |
| which clocks exist, and their frequencies | [physical](physical/) |
| pipelining a bus that crosses a die boundary | [physical](physical/) |
| credit, and end-to-end flow control **in the mesh** | the fabric's endpoints. The station bus runs its own, separately |

## Where today's source disagrees

Three of the four complaints this page used to carry have been resolved, and
recording that is as useful as recording what is left.

**Resolved.** `src/kohakuaccel/axi/` is no longer four unrelated things in one
directory: it is now `simple/`, `station/`, `link/`, `topo/` and `bd/`. The two
memory models have moved to `src/kohakuaccel/verif/` alongside the other
bench-only modules. The superseded `instruction_receiver.v` is gone, and
`axi4_master.v` has been retired to `src/attic/legacy-axi/`.

**`main_orch.v`'s place is settled.** It is a host-side driver — a driver-side
sequencer the host loads and runs over JTAG or PCIe, for bring-up and for
scripting the host side of a simulation — and `verif/` is the right home for it.
On-card orchestration (dispatch, completions, the memory choreography) lives in
the RV64 runtime host inside the system node; `main_orch` is only the host's
scripted reach into the same MAG control window the debug bridge and DMA engine
use. "Where does control live" now has a single answer: the runtime host.

**Still true.**

**There are two implementations of N-to-1 concentration.**
`src/kohakuaccel/axi/simple/axi_n1.v` and
`src/kohakuaccel/sysnode/core/mag_dram_port.v` solve the same problem with the
same structure — round robin, five queues, index-in-ID response routing,
asynchronous crossing. `mag_dram_port` additionally packs the internal beat up
to the memory beat and carries byte strobes. They should be one module with the
packing ratio as a parameter, in this package.

**`src/reference/poc/` contains copies of framework modules**, including
`noc_cu_base.v` and `async_fifo.v`. A measurement harness that carries its own
divergent copy of the module under test is the one arrangement guaranteed to
produce numbers that describe nothing.
