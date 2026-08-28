---
title: Fused crossbar-cache — kx_mempath_e
summary: M AXI masters to N DRAM channels through one fused system in which the cache, the crossbar and the clock crossings are a single structure. AXI exists only at the two edges; inside, wide data lives in one array per home and every select is a registered binary index.
tags:
  - axi
  - cache
  - crossbar
  - kohakuaxi
  - design
---

# Fused crossbar-cache — `kx_mempath_e`

> **Kind: Yours throughout — a general AXI memory path, not a framework
> contract.** The fused structure, the per-home array, the engine grouping and
> the per-port clock model are this project's design. Where it meets a DRAM
> controller or a master it presents ordinary AXI4, and nothing on either side
> knows what is between them.

`src/kohakuaxi/` — M AXI4 masters in, N AXI4 DRAM channels out, and **one
system** between them. It is the second of KohakuAXI's two systems; the first is
the [station bus](station-bus.md), which is a different structure for a
different job and shares no module with this one.

**Provenance for every figure on this page: `xcvu13p-fhgb2104-2L-e`, Vivado
2024.2, out-of-context synthesis at a 3.333 ns (300 MHz) ask, one synthesis per
row via `scripts/tcl/ooc_kx.tcl`.** Every row is the *whole* system — caches,
engines, crossbar and edges together — never a bare switch. Nothing on this page
is placed or routed.

---

## 1. What it is

### 1.1 The problem it answers

A vendor memory path is a crossbar IP in front of a cache IP in front of each
DRAM controller. Each of the three is an AXI endpoint, so the data crosses an
AXI boundary twice on the way in and twice on the way out, and each boundary
carries its own buffering, its own width and ID machinery, and — if the clocks
differ — its own converters. The wide data is copied at every one of those
boundaries.

`kx_mempath_e` removes the internal boundaries. AXI is spoken at exactly two
places: where a master attaches and where a DRAM controller attaches. Between
them there is no AXI-shaped structure at all: no address channel handshake, no
per-hop FIFO, no ID table. Wide data enters the system once, is stored once, and
leaves once.

### 1.2 Vocabulary

| | |
|---|---|
| **master** | an external AXI4 manager, index `m` of `M` |
| **home** | one DRAM channel and the cache that fronts it, index `h` of `N_HOME`. A home owns an address range selected by `addr[HOME_LSB +: log2 N]` |
| **IO width** | `W`, the AXI data width at every port — 512 bits as measured |
| **line** | `K × W` bits, the unit the cache stores and fills. `K = 1` is one IO word per line |
| **engine** | the control machine that serves requests for one or more homes; carries no wide data |
| **edge** | the per-port module that either passes the AXI channels through as wires or crosses them into the fabric clock |

### 1.3 The structure

```
   master 0 ... master M-1            (each on its own clock, or on clk)
      │               │
   [edge m]       [edge m]            kx_link × 5 per master: AW AR W R B
      │               │                 wire when MCDC[m]=0, async FIFO when 1
   ═══╪═══════════════╪═══════════  fabric, ONE clock: clk  ═══════════════
      │  route by addr[HOME_LSB +: log2 N]      registered binary-index muxes
      │               │
   ┌──┴──────┐   ┌────┴─────┐            ┌──────────┐
   │ rd eng  │   │ wr eng   │  … × N     │ kx_carray│ × N   the only wide store:
   │ control │   │ control  │            │ URAM row │       {valid, tag, K×W line}
   └──┬──────┘   └────┬─────┘            └────┬─────┘
      │               │                       │
   [edge h]       [edge h]            kx_link × 5 per home: AW AR W R B
      │               │                 wire when HCDC[h]=0, async FIFO when 1
   DRAM ch 0 ... DRAM ch N-1          (each on its own clock, or on clk)
```

Three kinds of module, and each carries exactly one kind of thing:

| module | carries | count |
|---|---|---|
| `kx_carray` | **wide data**: the URAM row array, the hit compare, the served word, the fill line, the write port | one per home |
| `kx_rd_engine`, `kx_wr_engine` | **control**: arbitration, the request record, the DRAM address channel, the response fields `{id, resp, last}`, and the *index* of the home or master whose data the fabric should select | one per home (SAMD) or one for all homes (SASD), independently for read and write |
| `kx_link` / `kx_scdc` | **a clock crossing, or nothing**: one AXI channel across the fabric edge | five per master, five per home |

The **crossbar** is not a module. It is two families of wide muxes in
`kx_mempath_e` itself — an N:1 per master on the read side selecting a home's
served word, an M:1 per home on the write side selecting a master's W beat —
driven by *registered binary* indices the engines publish. §3 says why that
form and no other.

---

## 2. How a request is served

### 2.1 Routing

A master's `AW`/`AR` address selects its home with `addr[HOME_LSB +: log2 N]`.
Every (home, master) pair has a *valid* line into that home's engine —
`x_arvalid[m] && (home(m) == h)` — and the engine's *ready* for the pair comes
back the same way. There is no decode table and no address translation: the home
index bits pass through to DRAM unmodified, and each home's DRAM sees the full
address.

The master index is prepended to the AXI ID on the way to DRAM (`IDW = ID_W +
log2 M`), so a DRAM response identifies its master without a scoreboard. The
engine strips it back off before the response reaches the master's edge.

### 2.2 Read

The read engine is a five-state machine holding **one request at a time** per
engine:

| state | what happens |
|---|---|
| `IDLE` | rotate-mask round robin over the (home, master) pairs that are valid and whose home is not flushing; latch address, length, id; select the home |
| `ISSUE` | present `{idx, tag, sub}` to the selected home's array with `rd_en` — one cycle |
| `WAIT` | `RD_LAT + 1` cycles for the array's registered hit and word (`RD_LAT` is 4 for URAM, 1 for BRAM) |
| `CHK` | hit: publish `{id, resp=OKAY, last}` and the home index, go to `DRAIN`. Miss: raise the home's DRAM `AR` and go to `FETCH` |
| `FETCH` | the array takes the DRAM `R` beats straight off the home's R channel into its line; on the last beat the served word is captured and the response is published |
| `DRAIN` | hold the response until the master's edge accepts it; for a burst, advance the address one IO word and return to `ISSUE` |

The **fill comes straight from the home's R channel** into the array: the engine
sees only `fill_done`. The DRAM `AR` is always one full line — `arlen = K − 1`,
`arsize = W`, `INCR`, line-aligned — whatever the master asked for; the master's
burst is walked one IO word per engine round.

The data path is one mux: `x_rdata[m] = c_word[ridx_m]`, where `ridx_m` is the
home index the engine published, **delayed one cycle through a flop together
with valid**. The engine holds its response in `DRAIN` until the master accepts
that delayed valid, so the flop costs nothing in correctness and is what lets
the N:1 pack as one LUT6 + MUXF7 per bit.

### 2.3 Write

The write engine holds one write per engine and streams W at one beat per cycle:

| state | what happens |
|---|---|
| `IDLE` | round robin over (home, master) pairs; latch the AW record; publish the granted source as a one-hot `gsel`, a binary `gidx`, and the home as `hsel`; raise the home's DRAM `AW` |
| `AW` | wait for the home's `awready` — the grant cannot move under a pending AW |
| `DATA` | each cycle the granted master has W and the home has `wready`, one beat goes to the home's DRAM `W` **and** to the home's array (write-through); the array index advances one IO word per beat |
| `RESP` | wait for the home's `B`, republish it to the owning master |

The wide path is `wdata_h = x_wdata[widx_h]` per home, `widx_h` being the
engine's *registered* `gidx`; the same beat drives the DRAM `W` port, the
array's write word and the array's `wr_full` (all strobes set).

### 2.4 The cache

Each home's `kx_carray` is one simple-dual-port RAM of `SETS` rows, each row
`{valid, tag, K × W}`, direct-mapped, indexed by `addr[LINE_LSB +: SET_W]`. It is
**write-through** — DRAM is always written, so the array is never dirty and has
no writeback path — and its write port has a single data select: fill line or
the write word.

| `K` | on a write beat | on a fill |
|---|---|---|
| **1** | **allocate on a full-strobe beat**: the row takes the word with `valid = 1`; a partial-strobe beat clears `valid` (invalidate) | the line *is* the R beat; the row is written on the single beat |
| **> 1** | **invalidate**: the row's `valid` is cleared. The line cannot be assembled from one IO word, and merging into a URAM row is not a single-port write | `K − 1` beats are buffered per slice with per-slice enables, and the last beat completes the line in the same write |

A hit's served word is `rd_row[q_sub × W +: W]` on the **binary** sub-word index
latched at `rd_en` (a wire at `K = 1`), captured into a register when the row
lands; on a fill the same register captures the fill's sub-word instead. The
two captures never coincide — a fill only follows a miss — so they are two
clock enables and not a 2:1 mux per bit.

**Reset flushes the array**: `flush_busy` walks every row writing `valid = 0`,
and an engine will not grant a request to a home that is flushing. Neither
engine nor array reset the data; only valid bits, the request records and the
pointers reset.

**One write port serves everything.** Priority is flush, then (`K > 1`)
invalidate, then fill, then (`K = 1`) allocate. The write-side inputs are
registered a cycle before the port, so the fabric's M:1 select, the fill/word
2:1 and the strobe gating are never in one cone.

### 2.5 Ordering and outstanding

- One outstanding read and one outstanding write per master.
- An engine serves one request at a time; with SAMD, different homes proceed in
  parallel, and two masters to the same home serialise at that home's engine.
- Reads and writes to a home are served by different engines and are **not
  ordered against each other** by the fabric. A read that follows a write to
  the same line from the same master sees the write, because the array write
  and the DRAM write both happen before the write's `B` is returned and the
  master's read cannot issue until its own `B` is taken. Across masters the
  usual AXI rule applies: nothing is ordered until a response has been seen.
- `WRAP` and `FIXED` bursts are executed as `INCR`; the DRAM request is always
  `INCR` and the walk is one IO word forward per beat.

---

## 3. Clock model

The whole fabric — arrays, engines, crossbar — runs on **one clock, `clk`**.
Every port declares, per port, whether its own clock is that clock:

| parameter | meaning |
|---|---|
| `MCDC[m]` | 1: master `m` is on `m_clk[m]` and its five channels cross at its edge. 0: master `m` is on `clk` and its edge is five wires |
| `HCDC[h]` | 1: home `h`'s DRAM is on `h_clk[h]` and crosses at its edge. 0: it is on `clk` and its edge is wires |

`kx_link` resolves this at elaboration: `SAME = 1` is a combinational
pass-through with no logic; `SAME = 0` instantiates `kx_scdc`, one `async_fifo`
of `CDC_DEPTH` (16, XPM's minimum) per AXI channel. The two wide channels, `W`
and `R`, put their FIFO in **block RAM** (`MEM = "block"`); the three narrow
ones stay in distributed RAM.

So the crossing count is exactly the number of ports whose clock differs from
`clk`, and a port that shares the fabric clock costs nothing at its edge. Which
clock `clk` *is* — a master's, a DRAM's, or a third — is the integrator's
choice, made by setting the CDC bits: a fabric clocked from the consumers with
`MCDC = 0` and `HCDC = all ones` puts every crossing on the DRAM side; the
reverse puts them on the master side; a fabric on its own clock crosses at every
port.

**Cross-SLR is always a crossing.** A port on another die has its own clock in
this model whether or not the frequency is nominally the same, so the SLR
boundary and the clock boundary are the same edge and are paid once.

The clocks are asynchronous to each other in the constraint set (`ooc_kx.tcl`
declares every `m_clk*` and `h_clk*` as its own clock); nothing in the design
assumes a ratio.

---

## 4. Knobs

| parameter | measured values | meaning |
|---|---|---|
| `M` | 2, 4, 8 | masters |
| `N_HOME` | 4, 8 | homes: DRAM channels, each with its cache |
| `W` | 512 | IO width, **shared** by every port and the array word |
| `K` | 1, 2, 4 | line width in IO words |
| `RSAMD` | 0, 1 | 1: one read engine per home (all homes served in parallel). 0: one read engine for all homes |
| `WSAMD` | 0, 1 | the same for the write side, **independently** |
| `MCDC[M-1:0]`, `HCDC[N-1:0]` | none; `HCDC = 1111` | per-port clock crossing, §3 |
| `SETS`, `SET_W` | 32768, 15 | rows per home: 2 MB per home at `K = 1`, **64 URAM** |
| `AW`, `ID_W`, `HOME_LSB` | 40, 4, 32 | address width, master ID width, home-select bit |
| `RAM_STYLE` | `"ultra"` | the array primitive; `"block"` shortens `RD_LAT` to 1 |
| `CDC_DEPTH` | 16 | per-channel crossing FIFO depth |

Every configuration is a target: the RTL is the same at every point of the
grid, and §5 measures the grid rather than one point. The **ship** point is
`M = 4, N_HOME = 4, K = 1`, SAMD both sides, 64 URAM per home, with the four DRAM
edges crossing (`HCDC = 4'b1111`) and the four masters on the fabric clock.

---

## 5. What it costs

### 5.1 The whole table

Every row: `W = 512`, `AW = 40`, `ID_W = 4`, 64 URAM per home (2 MB per home
at `K = 1`), one synthesis at a 300 MHz ask. LUT and FF are the *entire* fused
system.

| M | N | K | read | write | crossings | LUT | FF | URAM | BRAM | WNS ns | Fmax MHz |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **4** | **4** | **1** | SAMD | SAMD | none | **9,914** | 7,390 | 256 | 0 | +1.301 | 492 |
| **4** | **4** | **1** | SAMD | SAMD | **4, DRAM side — ship** | **11,865** | 10,788 | 256 | 64 | +1.140 | 456 |
| 4 | 4 | 1 | SAMD | SAMD | 4, DRAM side, W/R FIFOs in LUTRAM | 14,382 | 19,560 | 256 | 0 | +1.140 | 456 |
| 4 | 4 | 2 | SAMD | SAMD | none | 14,467 | 13,552 | 480 | 0 | +0.570 | 362 |
| 4 | 4 | 4 | SAMD | SAMD | none | 22,847 | 21,688 | 928 | 0 | +0.406 | 342 |
| 2 | 4 | 1 | SAMD | SAMD | none | 6,237 | 7,310 | 256 | 0 | +1.314 | 495 |
| 8 | 4 | 1 | SAMD | SAMD | none | 15,132 | 7,508 | 256 | 0 | +1.325 | 498 |
| 4 | 8 | 1 | SAMD | SAMD | none | 18,219 | 14,778 | 512 | 0 | +1.319 | 497 |
| 8 | 8 | 1 | SAMD | SAMD | none | 28,194 | 15,000 | 512 | 0 | +1.325 | 498 |
| 4 | 4 | 1 | SASD | SAMD | none | 9,543 | 7,199 | 256 | 0 | +1.117 | 451 |
| 4 | 4 | 1 | SAMD | SASD | none | 7,694 | 7,017 | 256 | 0 | +1.217 | 473 |
| 4 | 4 | 1 | SASD | SASD | none | 7,350 | 6,830 | 256 | 0 | +1.116 | 451 |
| 4 | 4 | 2 | SASD | SASD | none | 12,155 | 13,022 | 480 | 0 | +0.569 | 362 |
| 8 | 8 | 1 | SASD | SASD | none | 17,718 | 13,613 | 512 | 0 | +0.709 | 381 |
| 8 | 8 | 2 | SASD | SASD | none | 27,968 | 25,969 | 960 | 0 | +0.571 | 362 |

Fmax is flat at ~495 MHz across every `M`/`N` shape at `K = 1`: the crossbar's
depth does not grow with port count, because a binary-index mux adds one
LUT6 + MUXF7 level per doubling. `K` is the only knob that moves timing, through
the wider row and the sub-word select.

### 5.2 Per knob

Marginal costs read from adjacent rows of §5.1. The sign convention is *from →
to*.

| knob | from → to | ΔLUT | ΔFF | ΔURAM | where it goes |
|---|---|---|---|---|---|
| `M` | 2 → 4 | +3,677 | +80 | 0 | +1,839 per master at N = 4 |
| `M` | 4 → 8 | +5,218 | +118 | 0 | +1,305 per master: a fixed per-master part plus an M·N crossbar part |
| `N_HOME` | 4 → 8 | +8,305 | +7,388 | +256 | ≈ +2,076 per home: array 815, engines 257, crossbar leg ≈ 1,000; +64 URAM |
| `M × N` | 4×4 → 8×8 | +4,757 over `ΔM + ΔN` | | | the crossbar scales as M·N, not M + N |
| `K` | 1 → 2 | +4,553 | +6,162 | +224 | per-home line buffer and the wider row |
| `K` | 2 → 4 | +8,380 | +8,136 | +448 | ≈ +4.2k LUT per extra IO word of line, linear |
| read SASD | SAMD → SASD | −371 | −191 | 0 | the per-home read engine is 140 LUT; sharing saves control only. −41 MHz |
| write SASD | SAMD → SASD | −2,220 | −373 | 0 | collapses N write paths and each path's M:1 fan-in to one. −19 MHz |
| both SASD | | −2,564 | −560 | 0 | additive: −371 − 2,220 = −2,591 predicted, −2,564 measured |
| both SASD at 8×8 | | −10,476 | | | the saving grows with M·N, not as a constant |
| crossing, W/R in BRAM | per port | **+488** | +850 | +16 BRAM | five async FIFOs; the two wide ones in RAMB36 at 1/32 occupancy |
| crossing, W/R in LUTRAM | per port | +1,117 | +3,043 | 0 | the same five FIFOs, all in distributed RAM |

`SETS` does not appear: the array is URAM, and LUT is independent of the row
count. `W` does not appear either — it was held at 512 throughout, and the
crossbar, the edges and the array word all scale with it together.

The per-knob model fitted to this table, with its validation against every row,
is the [resource estimator](../../../docs-web/src/content/estimator.js) and
`scripts/py/kx_cost.py`; its worst error over the table is 2.29% on LUT and
1.02% on FF.

### 5.3 Against the vendor path at the same shape

Two vendor rows, both kept because they answer different questions.

**Vendor at its defaults.** One block design, one synthesis, same 300 MHz clock:
a 4×4 SmartConnect at 512 bits with a `system_cache` per DRAM channel
(`C_CACHE_SIZE` 2 MB requested), from `scripts/tcl/ooc_vendor_xc.tcl`:

| cell | LUT | FF | LUTRAM | SRL | BRAM | URAM |
|---|---|---|---|---|---|---|
| SmartConnect 4×4 @512 | 8,887 | 7,738 | 1,088 | 301 | 0 | 0 |
| `system_cache` × 4 (1,792 / 1,793 / 1,793 / 1,797) | 7,175 | 5,592 | 48 | 752 | 68 | 0 |
| **vendor, composed — the block design's own total** | **16,062** | **13,330** | 1,136 | 1,053 | 68 | 0 |
| **fused, ship shape, no crossing** | **9,914** | **7,390** | 0 | 0 | 0 | 256 |

In that block design the vendor cache did **not** build its 2 MB: it mapped 17
BRAM per cache (≈150 KB usable) at its default data-memory type. So this row
compares the crossbar and the cache *machinery*, not the memory: the fused
system is 38% fewer LUT and 45% fewer FF with 2 MB per home actually present.

**Vendor at a real 2 MB.** `system_cache` alone, 2 MB, 512-bit, data-memory
type 2, from `scripts/tcl/ooc_syscache.tcl`:

| cell | LUT | FF | BRAM | URAM | Fmax |
|---|---|---|---|---|---|
| `system_cache`, 2 MB | 8,279 | 5,238 | 561 | 0 | 244 MHz at a 10 ns request (slack +5.909) |

It still maps to block RAM, not URAM, and its 244 MHz is below the 300 MHz the
fused system is asked for. Composed at the ship shape — the crossbar
plus four of them — and set beside the fused system:

| 4×4 @512, 4 × 2 MB | LUT | FF | BRAM | URAM | Fmax |
|---|---|---|---|---|---|
| SmartConnect 8,887 + 4 × `system_cache` 8,279 | **42,003** | 28,690 | 2,244 | 0 | ≤ 244, cache-bound |
| **fused `kx_mempath_e`, no crossing** | **9,914** | 7,390 | 0 | 256 | 492 |
| **fused `kx_mempath_e`, ship (4 DRAM-side crossings)** | **11,865** | 10,788 | 64 | 256 | 456 |

At the real memory size the fused system is 4.2× fewer LUT (3.5× at the ship
point with its crossings), keeps the memory in URAM where 2,244 BRAM would be
83% of the part's 2,688, and meets the 300 MHz ask where the vendor cache's
244 MHz Fmax cannot. The vendor composition is a Σ of standalone synths, as the station-bus
page's vendor rows are; the fused figure is one synthesis of one netlist.

---

## 6. Performance

Nothing on this page is a routed figure and no cycle counter was run against
the RTL; the numbers here are read off the state machines of §2 and are exact
for the same-clock case.

| | cycles on `clk` | where they go |
|---|---|---|
| read hit, first beat, URAM (`RD_LAT = 4`) | **9** from AR accept to R valid | ISSUE 1 + WAIT 5 + CHK 1 + DRAIN 1 + the index flop 1 |
| read hit, first beat, BRAM (`RD_LAT = 1`) | 6 | WAIT is 2 |
| read hit, each further beat of a burst | +9 | the engine walks a burst one IO word per round |
| read miss | hit + DRAM AR→R round trip + 2 | the fill is taken straight off R; the served word lands on the last beat |
| write, W beats | 1 per cycle | once AW is accepted; the array and the DRAM W port take the same beat |
| write, per burst | AW round trip + beats + B round trip | one write outstanding per master |
| each crossing on the path | one `async_fifo` traversal each way, **not measured** | the FIFO's own latency; no cycle figure is claimed for it |

Throughput follows from one outstanding request per master and one request per
engine: with SAMD, `N_HOME` reads proceed in parallel, each delivering one IO
word every 9 cycles on a hit at URAM latency — 64 bytes per 9 cycles per home,
2.1 GB/s per home at 300 MHz on hits, and every home together 8.5 GB/s at the
ship shape. Writes stream at one beat per cycle per home inside a burst, 19.2
GB/s per home at 300 MHz and 512 bits. Fmax at the ship point is 456 MHz with
crossings and 492 without, against the 300 MHz ask.

That read-side figure is the serial read engine — one array lookup per beat,
not one per line — and it is the number that bounds a read-heavy master, not
the crossbar or the array.

---

## 7. Verification

`tests/axi/kx_mempath_tb.v` — the whole system between AXI masters and
`axi4_ram` models, one model per home, run under Verilator (`scripts/py/vlt.py`)
and xsim (`scripts/py/xsim.py kx_mempath`). The bench parameters follow the
RTL's: `TB_M`, `TB_N`, `TB_K`, `TB_RSAMD`, `TB_WSAMD`, `TB_TWOCLK` (every DRAM
edge crossing, DRAM clock 4.2 ns against a 3.334 ns fabric), `TB_SETS`,
`TB_SET_W`.

What it drives, per configuration:

- single-beat and burst writes and reads through every master to every home,
  data checked against an address-derived pattern, `rlast` checked per beat;
- the same line read back through a **different** master than wrote it, on the
  same home;
- two masters to the same home concurrently;
- partial-strobe writes, and reads that follow them;
- at `K > 1`, sub-word aliasing: the `K` IO words of one line written and read
  back individually.

Configurations run: `4×4 K1`, `4×4 K2`, `4×4 K4`, `2×4`, `8×4`, `4×8`, `8×8`,
each at SAMD and at SASD on either and both sides, at 64 URAM per home
(`SETS = 32768`), and the DRAM-side two-clock case at the ship shape. The
component benches beneath it are `kx_carray`, `kx_rd_engine`, `kx_wr_engine`,
`kx_link` and `kx_scdc`, each with its own bench in the same suite.

---

## 8. What it deliberately does not do

- **No set associativity and no replacement policy.** Direct-mapped by the set
  index; a conflicting line evicts on fill. The URAM budget goes to rows, not
  ways.
- **No writeback.** Write-through only, so the array never holds data DRAM does
  not, and a flush is a valid-clear rather than a drain.
- **No read pipelining within an engine.** One request in flight per engine,
  one beat per array round. §6 is the consequence.
- **No exclusive access, no cache or protection attributes.** `AxLOCK`,
  `AxCACHE`, `AxPROT`, `AxQOS`, `AxREGION` are not carried; every DRAM request
  is `INCR` at the line size.
- **No coherence between homes.** Each home's cache fronts its own DRAM range;
  an address belongs to exactly one home, so there is nothing to keep coherent.
- **No error recovery.** A DRAM `SLVERR`/`DECERR` is returned to the master on
  the beat it applied to; nothing retries.
- **No runtime observability.** There are no hit counters and no config port;
  a bench reads the internals.

---

## 9. Fixed protocol, addon, convention, or yours

| thing | category |
|---|---|
| AXI4 at both edges — five channels, the handshake, the burst and 4 KB rules | **fixed protocol**, and not ours |
| home selection by `addr[HOME_LSB +: log2 N]`, the master index prepended to the DRAM ID | **fixed protocol** within the system: a DRAM channel sees the whole address and an ID it must echo |
| the per-port clock bits `MCDC` / `HCDC` and which clock `clk` is | **yours**, per deployment; §3 says what each choice costs |
| `M`, `N_HOME`, `K`, `RSAMD`, `WSAMD`, `SETS`, `W` | **customizable** — every point is a target, §5 prices each |
| the array primitive (`RAM_STYLE`) and the crossing FIFO memory | **customizable**; the only effect is `RD_LAT` and where the wide FIFOs land |
| that the fabric is one clock and every select is a registered binary index | **convention with teeth**: it is where the LUT figure comes from, and §5.2 is what the alternatives measured |
| what a master does with the memory behind it | **yours** |

---

## 10. Where to read next

- **[station-bus.md](station-bus.md)** — the other KohakuAXI system: a line of
  stations carrying host traffic across the dies. It is what a host reaches a
  mesh through; this page is what a set of masters reaches DRAM through.
- **[README.md](README.md)** — KohakuAXI in one page.
- **[../../arch/axi.md](../../arch/axi.md)** — the framework's statement of
  its AXI boundary.

RTL: `src/kohakuaxi/` — `kx_mempath_e.v` (the system), `kx_carray.v`,
`kx_rd_engine.v`, `kx_wr_engine.v`, `kx_link.v`, `kx_scdc.v`. Measurement:
`scripts/tcl/ooc_kx.tcl` (one configuration, every report), `scripts/py/kx_cost.py`
(the per-knob model and its validation gate).
