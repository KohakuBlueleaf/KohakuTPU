# MAG — Memory Access Gateway

The single point where a partition touches everything outside it: the host's
AXI, its own DRAM, and its NoC mesh.

```
   MAG  =  MAS (memory engine)  +  agent (dispatch, status)
```

**MAS** keeps its meaning as the memory half — arbiter, and later TLB and cache.
The **agent** is the orchestrator's limb inside the partition. MAG is the module
that contains both and owns the DRAM behind it.

Mirrors `src/kohakumas/` — `mag.v`, `mag_mem_port.v`, `mx_quant.v`, `axi_ram.v`.

> Scope: **one partition** — 1 MAG, 8 clusters, one mesh, one SLR. Four of them
> is a parallelism problem, deliberately deferred; see
> [`../arch-design.md`](../arch-design.md) §6.4a.
>
> One MAG is no longer one memory channel. It presents `MEM_PORTS` independent
> memory ports, one per mesh row, each with its own read engine, quantiser,
> write slots and AXI master — [spec.md](spec.md) §2.4.
>
> Those ports are **all** of MAG's mesh attachments. The agent used to hold one
> of its own on the opposite edge; it now shares the memory ports and is told
> apart from the memory engine by flit type — [spec.md](spec.md) §2.5.

| Doc | Covers |
|---|---|
| [spec.md](spec.md) | The adapter shape, **built**; address slicing, TLB, ordering and bandwidth sizing, **design** |
| [quantiser-timing.md](quantiser-timing.md) | **Built and synthesised.** How `mx_quant` went 32.5 → 400.6 MHz, what each stage costs, and the 330.0 MHz `mag_mem_port` around it |
| [dram-port.md](dram-port.md) | **Standalone, NOT wired into `mag.v`.** `mag_dram_port` — N internal requesters behind **one** AXI master, so arbitrate/pack/cross each happen once |
| [cache.md](cache.md) | **Design.** The memory system — why there are two kinds of cache and why only one has tags |
| [driver.md](driver.md) | **Superseded** by [`../isa/`](../isa/README.md); kept for the measured concurrency numbers |

The instruction sets that cross this boundary — what a CU asks memory for, and
what the agent's dispatch registers do — are [`../isa/memory.md`](../isa/memory.md)
and [`../isa/agent.md`](../isa/agent.md).

## The shape of it

```
                      AXI slave IO  ◄──── SmartConnect ◄── host / main orch
                            │
                      address decode
              memory ┌──────┴──────┐ control
                     ▼             ▼
              upload FSM        agent: dispatch, NODE_STATUS,
              + quantiser         │    NoC packet injection
                     │       ┌────┴────┐
                     │       │  share  │  in : demux by FLIT TYPE
                     │       │  layer  │  out: by DESTINATION ROW
                     │       └─┬──┬──┬─┘
   NoC mem port 0 ───┼─────────┴──┼──┼──► memory port 0 ──► AXI master 0 ─┐
   NoC mem port 1 ───┼────────────┴──┼──► memory port 1 ──► AXI master 1 ─┼─► DDR4
        ...          └───────────────┴──►      ...      ──► AXI master N ─┘
                                                        (memory owned by this MAG)
```

**One port per mesh row, and it is the unit the machine grows by.** A port is a
whole memory server — intake queues, read engine, its own quantiser, write
slots, emitter, AXI channel — because one server for the whole partition is what
stopped the machine scaling, with nothing saturated. [spec.md](spec.md) §2.4
has the measurement.

**They are MAG ports, not memory ports.** Each carries operand traffic *and* the
agent's control traffic on the same wires, and a port attaches to the **NoC**
rather than to a cluster: every cluster can reach every port through the mesh,
so which one its traffic leaves by is a routing choice. Columns nearer MAG exit
by their manager's row, the farther half by their accumulator's, which keeps
both of a band's ports carrying — see [spec.md](spec.md) §2.4 and
[`../system.md`](../system.md) §2.3.

**The agent rides those ports; it has none of its own.** Inbound, each port
demuxes by flit type — memory requests to its engine, everything else to the
agent, with the ports round-robining into the agent's single input. Outbound, an
agent flit leaves from the port on its destination's row, so dispatch spreads
across every attachment instead of funnelling through one link. The agent answers
at port 0's coordinate: **one address, two consumers, told apart by what the flit
is rather than by where it went.** [spec.md](spec.md) §2.5.

That leaves the mesh's north, south and east edges unattached — free for the
vector unit and general core the architecture still owes.

**MAG is a slave on the interconnect, not a master.** The SmartConnect's slave
list holds MAGs where it used to hold DDR controllers — nothing was added to it,
and the memory simply moved one level down behind an adapter.

An AXI write's **address** decides what it is: memory, MAG control, or a NoC
packet to inject. That one decode is why there is no separate control fabric,
and why JTAG can inject mesh traffic with an ordinary `jaxi::write`.

Three components make a machine that can actually run something:

```
   main orchestrator   control -- programs, global status. An AXI master now,
                       not a NoC node.
   cluster             compute -- fetches its own operands, writes its results
   MAG                 gateway -- memory, dispatch, and the AXI<->NoC crossing
```

## Two caches, two jobs

The design splits along *who knows the access pattern*:

| | MAS-side | CU-side |
|---|---|---|
| what | TLB + shared cache | internal L2, explicitly managed |
| tags | yes | **no** |
| captures | sharing *between* clusters, at the same moment | reuse *within* one cluster, over time |
| filled by | hardware, on miss | `FILL` instructions with tensor descriptors |
| storage | URAM | URAM |

A transparent cache is the right answer when nobody knows what comes next. Inside
a matmul cluster the manager knows exactly — the descriptor says so — so paying
for a tag lookup buys nothing. See [cache.md](cache.md) §2.

## Status

**Built and passing.** `src/kohakumas/mag.v` + `src/kohakuaxi/main_orch.v`,
exercised end to end by `tests/run_mag_sim.ps1`:

```
   fake AXI master (driver) + main orchestrator <-> xbar <-> MAG
        <-> mesh,  MAG's memory ports <-> multi-channel AXI RAM

   mag_system_tb   C[16,16] = A[16,64] x B[64,16], hand-built program.
                   Cluster 0 quantises on every read and drains explicitly;
                   cluster 1 runs from int7 uploaded through the quantising
                   window and uses a FUSED drain -- the same answer by two
                   paths. K is two blocks because a fused sweep's last K
                   block must not also be its first

   mag_driver_tb   any shape, driver-tiled, operands uploaded over AXI
                   through MAG's memory window.  Mesh is generated and
                   sized by `-d NCL=`, up to 8 clusters -- one cluster per
                   COLUMN of a band, managers on the outer rows and
                   accumulators on the inner ones, with one MAG port per
                   mesh row on the west edge
```

Both identical on the real DSP48E2. The host writes a control program and never
touches the mesh; `mag_driver_tb` is the one that exercises the quantiser and
the tiling, and [`../system.md`](../system.md) §6 reports what it measures.

**The quantiser is built and lives here.** `mx_quant.v` converts FP16 to
int7 + E5M3 as data leaves MAG, one scale per 32-element block. Software
therefore uploads ordinary FP16 and never constructs a quantised value;
`src/ktpu/hw/mxfp7.py` exists only to predict what the hardware will
produce, so a mismatch is a disagreement between two independent implementations
rather than two copies of one mistake. See
[`../isa/memory.md`](../isa/memory.md) §6.

**v1 has no cache and no TLB** — deliberately. At this scale neither is needed,
and both are additive behind the same interface.

> These documents supersede [`../noc/spec.md`](../noc/spec.md) §9, which made L2
> and L3 the same parameterised cache module. That is no longer the plan: the
> CU-side level is not a cache.
