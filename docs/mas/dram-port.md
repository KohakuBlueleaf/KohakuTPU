# `mag_dram_port` -- N internal requesters, one AXI master

Replaces the per-mesh SmartConnect by never forming more than one AXI
interface.

**THE BASELINE IS `mag` + THE SMARTCONNECT, NOT THE SMARTCONNECT ALONE.**
MAG's own `MP1` masters are not free: each `mag_mem_port` carries a full set of
registered AW/W/AR outputs. The comparison is therefore whole-MAG against
whole-MAG:

All OOC on xcvu13p, constraint applied BEFORE `synth_design` -- creating the
clock afterwards leaves synthesis unconstrained and the Fmax meaningless.

| | LUT | FF | Fmax |
|---|---|---|---|
| 5S/1M SmartConnect | 24,786 | 35,621 | -- |
| `mag` ILINK=1 MEM_PORTS=2 (MP1=5) | 30,942 | 32,964 | 331.8 MHz |
| **baseline total** | **55,728** | **68,585** | |
| **`mag_dram_port` N=5, MW=512** | **1,942** | **1,634** | **361.7 MHz** |

Deleting the SmartConnect alone saves **22,844 LUT / 33,987 FF per mesh**,
about 91k LUT and 136k FF across four. `mag_dram_port` is faster than `mag`,
so it does not become the critical path.

`N = MEM_PORTS + 3`: one requester per NoC memory port, plus the host upload,
the memory mover, and the interlink's landing channel. `MEM_PORTS >= 1`, so
`N >= 4`.

> **That `+3` counts the interlink's landing channel**, so it is the `ILINK=1`
> figure. A single-mesh MAG presents `MP1 = MEM_PORTS + 2` and `N` follows it —
> [`../interlink/boundary.md`](../interlink/boundary.md) §2, which is also where
> the master *order* is fixed.
>
> **This is a different lever from `src/kohakuaxi/axi_n1.v`**, which merges MAG's
> existing masters *outside* it at 955 LUT
> ([`../general_cores/slr.md`](../general_cores/slr.md) §2.4). `axi_n1` removes
> the merge from the fabric; this removes the need to merge. They are
> alternatives, not stages, and §8 is what decides between them.

**NOT WIRED INTO `mag.v`.** This is a standalone module developed and tested on
its own. System testing uses a COPY of the MAG top, never an edit of `mag.v` --
the shipping single-master path stays untouched until this one is proven on
silicon.

## 1. Why the order is arbitrate -> pack -> cross

Three jobs on this path, and each wants to happen exactly once.

* **Arbitrate first.** Crossing per requester needs `5N` async FIFOs; crossing
  after arbitration needs five, whatever `N` is.
* **Pack second.** The DRAM port is 512 bits and an internal beat is 256, so
  packing before the crossing carries **half as many transactions** for the
  same bandwidth. Packing is cheap HERE and expensive outside: `mag_mem_port`
  drives `awsize`/`awburst`/`wstrb`/`arsize`/`arburst` as **constants**, so
  every burst is aligned INCR with full strobes. An external AXI upsizer may
  not assume any of that and must implement narrow transfers, WRAP and
  arbitrary strobes. Same function, an order of magnitude apart in cost, purely
  because of where it sits.
* **Cross last**, once, on a stream that is already one master and already the
  memory's native width.

Exposing `N` masters forces this order to be wrong: the fabric must convert or
cross before it knows what it is merging.

## 2. Interface

```verilog
module mag_dram_port #(
    parameter integer N       = 5,     // MEM_PORTS + 3
    parameter integer ADDR_W  = 34,
    parameter integer SW      = 256,   // internal beat
    parameter integer MW      = 512,   // DRAM beat; MW = SW * R, R a power of 2
    parameter integer ID_W    = 4,     // >= clog2(N)
    parameter integer RD_OUTST= 4,     // outstanding reads per requester
    parameter integer WQ_DEPTH= 64,    // 512-bit write beats in flight
    parameter integer RQ_DEPTH= 64
)(
    // ---- internal side, s_aclk = the mesh clock ----
    input  wire                 s_aclk,
    input  wire                 s_aresetn,

    input  wire [N-1:0]         q_valid,    // request
    output wire [N-1:0]         q_ready,
    input  wire [N*ADDR_W-1:0]  q_addr,     // byte address, SW/8 aligned
    input  wire [N*16-1:0]      q_len,      // beats of SW, 0 = one beat
    input  wire [N-1:0]         q_write,

    input  wire [N-1:0]         w_valid,    // write data, in request order
    output wire [N-1:0]         w_ready,
    input  wire [N*SW-1:0]      w_data,
    input  wire [N-1:0]         w_last,

    output wire [N-1:0]         r_valid,    // read data, back to originator
    input  wire [N-1:0]         r_ready,
    output wire [N*SW-1:0]      r_data,
    output wire [N-1:0]         r_last,

    output wire [N-1:0]         b_valid,    // write burst retired
    // ---- DRAM side, m_aclk = the MIG's ui_clk ----
    input  wire                 m_aclk,
    input  wire                 m_aresetn
    /* one AXI4 master at MW bits: aw/w/b/ar/r */
);
```

No `lock`/`cache`/`prot`/`qos`/`region`: no requester drives them and the MIG
takes its defaults, which is the choice `mag.v` and `axi_ram.v` already make.

## 3. Structure

```
  s_aclk                                        |  m_aclk
  ----------------------------------------------|--------------------
  q[N] --RR--> rd arb --> {addr,len,id} --------> [ar fifo] --> AR
       \--RR--> wr arb --> {addr,len,id} -------> [aw fifo] --> AW
                  |
                  +-> wsel fifo {id,mbeats}
  w[N] --mux by wsel head--> PACK R:1 ----------> [w fifo MW] --> W
  r[N] <--demux by id-- UNPACK 1:R <------------- [r fifo MW] <-- R
  b[N] <--demux by id--------------------------- [b fifo] <----- B
```

Five async FIFOs, independent of `N`. `src/common/async_fifo.v` already exists.

**Reads and writes arbitrate separately.** AXI4 forbids W interleaving, so W
beats must appear in AW order; a `wsel` FIFO of `{id, mbeats}` names whose data
the W mux forwards and for how long. Reads have no such rule, so AR issues
freely with `arid = requester index` and R is demuxed by `rid` -- the same
"routing is the ID, not a table" trick `axi_n1` uses, one level lower.

## 4. Packing, and the only genuinely hard part

`R = MW / SW` (2 at 512/256). Master burst length:

```
head_phase = (addr / (SW/8)) % R
mbeats     = ceil((sbeats + head_phase) / R)
```

**A burst can be partial at BOTH ends.** Internal beats are `SW/8` = 32-byte
aligned but the DRAM beat is 64, so a burst starting at an odd 32-byte word
half-fills its first master beat, and an odd length half-fills its last.

* **Write**: this is exactly what `wstrb` is for. Hold a strobe mask; on the
  first master beat mask off lanes below `head_phase`, on `wlast` with the
  accumulator half-full mask off the lanes above. Every other beat is all-ones.
* **Read**: over-fetch is free -- issue the aligned superset and **discard**.
  Per outstanding id keep `{head_phase, sbeats}`; drop `head_phase` sub-beats at
  the start and stop emitting after `sbeats`.

This is the one place a bug would be silent rather than loud, so it is where
the directed tests go: `head_phase` x `sbeats % R` is a 2x2 matrix at R=2 and
every cell needs a case.

## 5. What must not regress

`mag.v` gives each memory port its own channel deliberately -- sharing them
"would leave the read beats of eight clusters on one AR/R pair". So:

* **Reads from different requesters must be concurrently outstanding.**
  `RD_OUTST` per requester, tracked in an `N x RD_OUTST` table indexed by id.
* **A slow reader must not block a writer.** Independent read and write
  arbiters, independent FIFOs; only the W channel serialises, and only per
  burst.
* **`r_ready` can go low.** `mag_mem_port`'s `m_rready` is combinational from
  its own state, so the unpacker needs a skid stage rather than assuming a
  sink that always accepts.

## 6. Why this is worth doing beyond the LUTs

* **Width stops being architecture.** `MW` is a parameter. HBM, a wider MIG or
  a different part is a re-elaborate, not a block-design rewire.
* **One boundary to make SLR-safe.** MAG's AXI is registered outbound and
  completely bare inbound today -- every `m_*ready`, `m_rvalid`,
  `m_rdata[255:0]` and `m_bvalid` is an unregistered `input wire`, and `m_rdata`
  feeds the quantiser combinationally (`mag_mem_port.v:316`). That is why
  **0 of 3,963 SLR crossings used Laguna**: a Laguna site *is* a flip-flop and
  there was no register to pull into one. One master port is one register slice
  to add; six is six chances to miss one.

  > **In multimesh this path stops crossing an SLR at all** -- each mesh reaches
  > its OWN die's DDR4 controller, one per SLR
  > ([`../interlink/topology.md`](../interlink/topology.md) §3.1). The register
  > slice is still wanted, for the reason above and because it is what makes
  > `MW` and the clock domain a parameter rather than a wiring fact; it just is
  > not urgent for Laguna's sake any more.
* **The block design collapses**: per mesh, 6 AXI interfaces plus a
  SmartConnect and its two clocks become one master, one `dram_aclk`, one
  `dram_aresetn`.

## 7. Build order -- `mag.v` IS NOT EDITED

1. `mag_dram_port` standalone + directed TB against `axi_ram.v`, R=2, every
   head/tail phase case, N=4 and N=6.
2. OOC synth: area against the 22k LUT it replaces, Fmax at 320 MHz.
3. System test through a **copy** of the MAG top -- e.g. `mag_1m.v` -- that
   instantiates this instead of flattening `MP1` masters. `mag.v` and every
   generated top that uses it keep working untouched, so a regression in the
   new path cannot take the shipping one with it.
4. Only once silicon agrees does anything consider replacing `mag.v`, and that
   is a separate decision with its own measurement.

## 8. Open question worth measuring first

Five 256-bit AR streams become one AR stream with five IDs. Peak is 19.2 GB/s
either way, but the MIG's reordering and page behaviour under one ID-tagged
stream is not the same as under five ports. Measure against the ~4.8 GB/s bulk
figure already on record before deleting the fallback.
