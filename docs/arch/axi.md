---
title: AXI surface
summary: The boundary to everything that is not the framework — host, DRAM, debug — and the discipline that makes crossing it survivable.
tags:
  - architecture
  - axi
  - boundary
---

# AXI surface

`src/kohakuaxi/` — the layer where the framework meets things it did not write.

## What it owns

Everything the accelerator must be, to whatever is outside it:

- **A slave surface for the host.** One address space that decodes into a
  memory window, a control window, an instruction staging window and a
  pass-through window for whatever a client wants to expose.
- **A master boundary to memory.** Concentrating several internal requesters
  onto the channels a memory controller actually presents, converting the beat
  width, and crossing into the memory's clock domain.
- **Reference implementations of both roles**, plus the memory model that
  stands in for DRAM in simulation.
- **The burst and handshake discipline** every AXI interface in the tree
  follows.
- **The control-program engine**, which turns a run of the machine into one host
  transaction.

## The problem it solves

Nothing outside the framework speaks flits. A host DMA engine, a memory
controller, a debug bridge and a vendor interconnect all speak AXI, and AXI is
substantially larger than what this machine uses: out-of-order completion by ID,
burst reordering, exclusive access, narrow transfers, cache and protection
attributes.

Two failure modes follow if this boundary is left implicit. The first is
importing AXI's generality inwards, so the fabric grows machinery to satisfy a
bus nobody asked for. The second is exporting the fabric's assumptions outwards,
so a vendor interconnect meets an interface that is *nearly* AXI and does
something arbitrary about the difference.

This layer exists so that the conversion happens once, in modules whose job is
only conversion.

## The model

### One decode is the whole control plane

An AXI write's **address** decides what it is: memory, control register,
instruction staging, or a raw flit to inject. That is the reason there is no
separate control fabric, and the reason a debug bridge can inject mesh traffic
with an ordinary AXI write and nothing else.

Two windows are wide rather than deep, and both are deliberate. The **staging
window** is sized from the number of instruction slots rather than fixed at one
page — a fixed page silently decodes the tail of a long program as register
writes, and the symptom is a program that stops early with no error. The
**pass-through window** forwards writes verbatim with the offset preserved, so a
client behind it keeps its own register offsets rather than having them
renumbered by an index.

### The three AXI roles, and the discipline they share

| Role | Shape | Where |
|---|---|---|
| slave | host writes registers, staging, memory | the control agent, the memory window |
| master | the framework reads and writes memory | one per memory port, plus upload, mover, interlink landing |
| model | a slave that behaves like memory, for simulation | `axi4_ram.v`, `axi_ram.v` |

Four rules hold across all of them, and each has a specific failure behind it:

1. **`VALID` is never a function of `READY`.** The reverse is a combinational
   loop between two compliant devices, and the AXI specification forbids it for
   exactly that reason.
2. **A burst ends because a counter says so, not because `WLAST` arrived.** A
   requester that miscounts its own data must not be able to desynchronise the
   response.
3. **`BID` / `RID` echo `AWID` / `ARID`.** AXI4 requires it, and this layer
   depends on it structurally — see the ID trick below.
4. **A burst must not cross a 4 KB boundary**, and `AxLEN` maxes at 255. An
   interconnect is permitted to do arbitrary things if you break the first rule;
   some split, some stall, some corrupt.

`axi4_master.v` exists to encode rules 2 through 4 once. It takes a command —
address, beat count, direction — and emits as many legal bursts as required. It
keeps one burst outstanding at a time, which is deliberate for a reference: the
state machine is readable and checkable. It is also the first thing to change
for a production master, because with real memory latency, single-outstanding
leaves most of the bandwidth unused.

### Concentration: arbitrate first, then cross

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

**Response routing is the ID, not a table.** The requester index is prepended to
`AWID` / `ARID`, so `BID` / `RID` say where the response goes. No scoreboard is
kept and none has to be sized. The cost is that the slave's ID width must be
wide enough to carry the index, which is why the module derives it rather than
taking it as a parameter.

What this deliberately is *not*: address decode (there is one slave, so there is
nothing to decode), protocol conversion, or arbitrary topology. Optional AXI
signals — lock, cache, prot, QoS, region — are not carried, because no master in
the design drives them and every slave takes its defaults.

### Width belongs at the boundary

The mesh's internal beat matches the flit payload, so that nothing in the fabric
or the memory agent ever gears between two widths. Real memory is wider. The
packing therefore happens in the same module as the concentration and the clock
crossing, at the edge — which is what lets a device image change its memory
width without any module inside the mesh knowing.

### Clock domains

The fabric and the control agent share one clock, so there is no crossing inside
either. There are exactly two places a domain boundary exists:

- **memory**, in the concentrator described above, through asynchronous FIFOs;
- **the host**, in whichever vendor interconnect merges the debug bridge and the
  DMA engine onto the control path — which is already multi-clock and is the
  right place to leave it.

Which clocks exist and what they may be retuned to is [physical](physical/).

### The control-program engine

`main_orch.v` is an AXI slave so the host can load a program, and an AXI master
so it can execute one. Three opcodes:

```
  WR    addr, data          issue an AXI write
  POLL  addr, mask, want    read addr until (data & mask) == want
  DONE  code                stop, latch code, raise the done flag
```

Three is enough because the machine's entire control surface is memory-mapped;
branches or arithmetic here would duplicate the host to no purpose. The value is
that a run of the machine becomes **one host transaction**. The host is not in
the loop per poll, and the same program works over a debug bridge and over a
production DMA path — which matters more than it sounds, because a single debug
read can cost milliseconds against microseconds of compute.

It is not a fabric node. Its reach into the mesh is an AXI write into a control
window, which the control agent turns into a flit — so dispatch, configuration
and debug injection all share one mechanism.

### The memory models

Simulation needs a slave that behaves like memory. Two exist and their
difference is instructive: one is a **reference** — it implements INCR, FIXED
and WRAP bursts, byte strobes and ID reflection, and exists to be read as the
correct shape of a slave. The other is a **stub** — INCR only, one outstanding
transaction per port, several independent channels over one array — and exists
so that a system test fails because the system is wrong rather than because the
stub grew its own bugs.

A single-port model is a model of a narrower memory system than any real target,
and once enough compute units sit behind it the stub becomes the answer rather
than the scenery. Multiple independent channels over one array is what a
multi-channel controller in front of one address space actually offers, and it
is the honest shape to test against.

## How it maps to real circuit

**Concentration is five queues and two round-robins, and that is the point.**
The cost does not grow with requester count the way a crossbar does, because
there is no crossbar: there is one slave, so arbitration is a mux and response
routing is a decode of bits that are already in flight.

The depths split by job. Address queues only have to cover the crossing
latency, so they are small and want distributed RAM. Write and read data queues
are sized for burst throughput, so they are deep and wide and want block RAM.
The parameters are separate for that reason.

**The vendor comparison is the honest way to size expectations.** A
general-purpose interconnect configured to do this job carries address decode,
width conversion for widths you do not use, and protocol machinery you do not
drive. Replacing it with a module that only arbitrates, only routes responses by
ID, and only crosses one clock boundary is a large reduction — and the reduction
comes from what was removed, not from cleverness. Where an interconnect is
genuinely doing several jobs at once — width conversion *and* multi-slave decode
*and* three clock domains — it stays, because nothing here replaces it.

For the measured version of that comparison on the reference instance, see
[projects/kohakutpu/results](../projects/kohakutpu/results.md).

**Swapping vendor IP for RTL moves the wiring from a block design's inference to
your port list.** The rule that survives it: an unconnected output is harmless;
an undriven input is the fault. Interface-inference attributes on the port list
are what let a block design still tie clocks, resets and interfaces up on its
own — see [workflow/build](../workflow/build.md).

## Fixed protocol, addon, convention, or yours

| Thing | Category |
|---|---|
| the four discipline rules, on every AXI interface in the tree | **fixed protocol**. They are what makes vendor IP behave |
| the window structure of the host address space — memory, control, staging, pass-through | **fixed protocol** — [spec/control-registers](../spec/control-registers.md) |
| the control program's three opcodes | **fixed protocol** |
| **DRAM-port beat packing** — the ratio between the internal beat and the memory beat | **customizable addon**. The concentrator is written around a ratio, not around a width |
| queue depths, and which of them are block RAM | **customizable** — the split by job is the part to keep |
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

**One run of the machine should be one host transaction.** *(Free.)* That is
what the control program's three opcodes are for. A host that polls per step
works, and costs a host round trip per poll — which on a debug path can be
milliseconds against microseconds of compute. The same program then runs
unchanged over debug and production paths.

**When you replace vendor IP with RTL, remember that an unconnected output is
harmless and an undriven input is the fault.** *(Free.)* Swapping IP for RTL
moves the wiring from a block design's inference to your port list. Keep the
interface-inference attributes on the ports so the tool still ties clocks,
resets and interfaces up on its own — see
[workflow/build](../workflow/build.md).

## What a compute-unit author must know

Almost nothing, and that is the intent. A compute unit never sees AXI. It emits
memory requests as flits and the memory agent deals with bursts, boundaries and
widths.

Two things leak through and are worth knowing:

1. **Your requests become bursts, and bursts have rules.** A very short entry
   is a very short burst, and burst overhead is paid per request. Asking for a
   run of entries is not only a latency optimisation; it is what makes the
   generated bursts worth issuing.
2. **The host's view of your unit is an address.** Instruction staging, control
   registers and status all arrive through this surface. If you want something
   observable from software, the path is the fabric's control-register interface
   — not a new AXI port.

## What this system does not own

| Not owned | Who owns it |
|---|---|
| flits, routing, the compute-unit port | [noc](noc/) |
| descriptors, and what a memory request means | [mas](mas/) |
| the DRAM controller itself | vendor IP. This layer terminates at its AXI interface |
| the host DMA engine | vendor IP, likewise |
| the address map's *values* — where each window lands | [ship](ship/) fixes them per device image; this layer only decodes |
| which clocks exist, and their frequencies | [physical](physical/) |
| pipelining a bus that crosses a die boundary | [physical](physical/) |
| credit, and end-to-end flow control | the fabric's endpoints |

## Where today's source disagrees

**`src/kohakuaxi/` is four unrelated things in one directory.** Fabric
(`axi_n1.v`, `axi_xbar2.v`), reference models (`axi4_ram.v`, `axi4_master.v`),
a control sequencer (`main_orch.v`), and a superseded block
(`instruction_receiver.v`). The first three are separate concerns with separate
audiences; the fourth is dead.

**`main_orch.v` is the control plane, not AXI plumbing.** It belongs with the
control agent — the two together are how a host drives the machine, and
splitting them across packages is why "where does control live" has no good
answer today.

**There are two implementations of N-to-1 concentration.** `axi_n1.v` in this
package and `mag_dram_port.v` in `src/kohakumas/` solve the same problem with
the same structure — round-robin, five queues, index-in-ID response routing,
asynchronous crossing. `mag_dram_port` additionally packs the internal beat up
to the memory beat. They should be one module with the packing ratio as a
parameter, in this package, and the composition that uses it
(`src/synth_top/mag_1m.v`) should not be sitting in a directory of device tops.

**There are two memory models in two packages.** `src/kohakuaxi/axi4_ram.v` and
`src/kohakumas/axi_ram.v`. They serve different purposes — reference versus
multi-channel stub — which is fine, but both are simulation support and neither
belongs next to synthesisable framework RTL.

**`src/synth_top/poc/` contains copies of framework modules**, including
`noc_cu_base.v` and `async_fifo.v`. A measurement harness that carries its own
divergent copy of the module under test is the one arrangement guaranteed to
produce numbers that describe nothing.
