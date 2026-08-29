---
title: Fused crossbar-cache — kx_xache
summary: M AXI masters to N DRAM channels through one fused system in which the cache, the crossbar and the clock crossings are a single structure. AXI exists only at the two edges; inside, wide data lives in one array per home and every select is a registered binary index.
tags:
  - axi
  - cache
  - crossbar
  - kohakuaxi
  - design
---

# Fused crossbar-cache — `kx_xache`

> **Kind: Yours throughout — a general AXI memory path, not a framework
> contract.** The fused structure, the per-home array, the engine grouping and
> the per-port clock model are this project's design. Where it meets a DRAM
> controller or a master it presents ordinary AXI4, and nothing on either side
> knows what is between them.

`src/kohakuaxi/` — M AXI4 masters in, N AXI4 DRAM channels out, and **one
system** between them. It is the second of KohakuAXI's two systems; the first is
the [station bus](station-bus.md), which is a different structure for a
different job and shares no module with this one.

**Naming.** The family prefix is `kx_` — **KX = Kohaku-Xache System**, and
**Xache = xbar-cache**. The top module is `kx_xache`; its parts are
`kx_carray`, `kx_rd_engine`, `kx_wr_engine`, `kx_link`, `kx_scdc` and
`kx_perm`. "The Xache" and "the xbar-cache" name the same thing on every page.

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

`kx_xache` removes the internal boundaries. AXI is spoken at exactly two
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
| `kx_rd_engine` or `kx_rd_pipe`, `kx_wr_engine` | **control**: arbitration, the request record, the DRAM address channel, the response fields `{id, resp, last}`, and the *index* of the home or master whose data the fabric should select. `RD_PIPE` picks the one-beat read engine or the streaming one | one per home (SAMD) or one for all homes (SASD), independently for read and write |
| `kx_link` / `kx_scdc` | **a clock crossing, or nothing**: one AXI channel across the fabric edge | five per master, five per home |

The **crossbar** is not a module. It is two families of wide muxes in
`kx_xache` itself — an N:1 per master on the read side selecting a home's
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

Two read engines exist, chosen by `RD_PIPE`. Both serve **one burst at a time
per engine**; they differ in what a burst costs.

**The array's lookup port pipelines** in either case: a lookup is `{idx, tag,
sub}` on one cycle, the tag and sub-word ride beside the RAM's own latency, the
row lands `RD_LAT` cycles later (4 for URAM, 1 for BRAM) and is compared against
the tag that travelled with it, and the served word is captured into the array's
registered `word` only when the engine says `rd_take`. The RAM's enable is tied
high, so the pipeline advances every cycle whether or not a lookup was issued.

**`RD_PIPE = 0` — the one-beat engine, `kx_rd_engine`.** One lookup at a time:

| state | what happens |
|---|---|
| `IDLE` | rotate-mask round robin over the (home, master) pairs that are valid and whose home is not flushing; latch address, length, id; select the home |
| `ISSUE` | present the lookup — one cycle |
| `WAIT` | `RD_LAT + 1` cycles for the registered hit and word |
| `CHK` | hit: publish `{id, resp=OKAY, last}` and the home index, go to `DRAIN`. Miss: raise the home's DRAM `AR` for **one line** (`arlen = K − 1`) and go to `FETCH` |
| `FETCH` | the DRAM `R` beats fill the array straight off the home's R channel; on the last beat the served word is captured from the fill and the response is published |
| `DRAIN` | hold the response until the master's edge accepts it; for a burst, advance one IO word and return to `ISSUE` |

Every beat is a full round: `RD_LAT + 4` cycles on a hit, plus one DRAM round
trip per line on a miss. The data path is `x_rdata[m] = c_word[ridx_m]`, with
`ridx_m` delayed one cycle through a flop together with valid; the engine holds
in `DRAIN` until that delayed valid is accepted.

**`RD_PIPE = 1` — the streaming engine, `kx_rd_pipe`.** The burst streams:

- **Lookups issue one per cycle** down the burst, `lk` beats ahead. Within a
  4 KB burst only the page offset moves, so the per-beat address is one
  6-bit add; tag and sub-word come from it.
- **A landing is taken** — captured into the array's `word`, `r_val` raised —
  when it is the beat the master needs next (`dr_next`) and hits and there is
  room (the previous beat was accepted this cycle, or nothing is held). One
  beat per cycle when the master keeps up.
- **A landing that cannot be taken is dropped and replayed.** The master
  stalled, or this burst is not its master's oldest — every later beat in the
  pipe will also be dropped, so the issuer restarts at the first beat not
  already held. The array is write-through, so any lookup may be re-done; no
  wide data is ever buffered. A stall costs `RD_LAT + 1` idle cycles when it
  clears.
- **A miss on the needed beat becomes one DRAM read for the rest of the
  burst** — line-aligned, from the missing beat's line to the last beat's line,
  `INCR`, within the same 4 KB page. Its beats fill the array as they arrive
  (`fill_lim` advances a line at a time); lookups for beats beyond `fill_lim`
  wait, and once their lines are written they land as hits. Misses stream at
  the DRAM's rate with one fetch in flight per engine; no beat is served from
  the R channel directly, so the array's served-word register stays the only
  wide register and the `word` capture keeps one source.
- **Responses are ordered per master.** Each accepted `AR` takes a sequence
  number; an engine presents only when its burst's number is the master's
  oldest, and the fabric's R data select is the home of that burst, known from
  the `AR` and registered the cycle it becomes current — no valid delay.
- **The grant is registered and the round robin is a tree.** The engine's
  rotate-mask round robin over the (home, master) slots picks the lowest set
  bit as a tree of 4-wide groups — one LUT level per stage, three stages for
  up to 256 slots — and the pick is registered before it reaches the AR
  bookkeeping; AXI holds `ARVALID`, so a pick one cycle old is re-qualified
  against the live valids and granted. (The textbook `x & (~x + 1)` over 16
  slots synthesised as a 13-level LUT ripple, not a carry chain, and held the
  shared read engine at 294 MHz; the tree took it to 381.)

With `RD_OUTQ > 1` a master may have that many bursts accepted across the
homes at once (consecutive 4 KB pages go to consecutive homes under the
interleave, §3.1), so while one engine drains the oldest, the others fetch and
fill the younger ones and drain them at hit speed when their turn comes.
`RD_OUTQ > 1` requires `RD_PIPE = 1`; the one-beat engine has no ordering and
the build refuses the combination.

The fill path in both engines yields to the master write port: a fill beat is
taken only when no master write lands on the array that cycle
(`fill_ready`), so a write is never dropped under a fill.

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

- `RD_OUTQ` outstanding reads (1 by default) and one outstanding write per
  master. Read responses to one master return in `AR` order whatever their
  homes; a master issuing distinct IDs gains nothing from that, a master reusing
  one ID needs it.
- An engine serves one burst at a time; with SAMD, different homes proceed in
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

### 3.1 Channel interleaving is an address permutation, and costs nothing

The home is a field of the address, `addr[HOME_LSB +: log2 N]`, and every home
receives the whole address. So which bits *are* the home bits is only a question
of which wires land on that field. `kx_perm` applies `NSWAP` bit-pair swaps to
each master's address at the master edge, before anything reads it:

```
   interleave at 2^G bytes  =  rotate the field [G, HOME_LSB + log2 N) down by log2 N
   pairs (i, i + log2 N) for i = G .. HOME_LSB-1, in order      (one byte of bit index per pair)

   G = 12, N = 4:  NSWAP = 20,  (12,14) (13,15) (14,16) ... (31,33)

   master   a39..a34 | a33 a32 | a31 ............. a14 | a13 a12 | a11 ... a0
   fabric   a39..a34 | a13 a12 | a33 ............. a16 | a15 a14 | a11 ... a0
                       ^^^^^^^ home select          ^^^^^^^^^^^^^^^^^^^^^^^^^
                       = interleave at 4 KB         the home's own address:
                                                    dense, every bit still live
```

Consecutive 4 KB pages now go to consecutive homes; each home's local address
is the master's address with the page bits taken out and everything above
shifted down, so it stays dense and one-to-one. The engines, arrays, tags and
DRAM ports are untouched: they read fields, and a bijection on a dense space
does not change what a field means. `NSWAP = 0` is the contiguous map — 4 GB
per home at `HOME_LSB = 32` — and the default.

**A rotation, not a swap of the two fields.** Swapping `[33:32]` with `[13:12]`
directly is also a bijection, but it parks the original bits 33:32 — constant
zero in any space under 16 GB — in the middle of the set-index field
`[20:6]`, so a 2 MB array would only ever use a quarter of its sets. The chain
of `(i, i+log2 N)` swaps shifts the whole field instead and every index bit
keeps varying. The mechanism is the same list of pairs; only the list differs.

Any number of pairs, any `N`: interleaving `N = 2^k` homes at `2^G` is
`HOME_LSB − G` pairs. The assumption the permutation rests on is that the
address space is **dense from 0 to `N × 2^HOME_LSB`**, so the bits above the
home field are zero.

Two bounds are enforced at elaboration, by an undefined-module guard in
`kx_perm` (the build fails, it does not mis-route):

| bound | why |
|---|---|
| every swapped bit ≥ `LINE_LSB` | one cache line must stay inside one home's array |
| every swapped bit ≥ **12** | AXI forbids a burst crossing a 4 KB boundary, so a burst never changes home mid-flight. The write engine holds one `AW` per burst and the read engine walks `+W/8` per beat; both stay correct without a splitter |

So the coarsest legal interleave is 4 KB, which is also what the AMD UMC's
4K option and every vendor crossbar do. Finer than 4 KB is not a wire — it is a
per-beat `AW` in the write engine — and is not built.

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
| `NSWAP`, `SWAP_A`, `SWAP_B` | 0; the 4 KB rotation (20 pairs `(i, i+2)`, `i = 12..31`); the plain 2-pair swap | address-bit swaps at the master edge: channel interleaving, §3.1. Each swapped bit must be ≥ `max(LINE_LSB, 12)` |
| `RD_PIPE` | 0, 1 | 0: the one-beat read engine. 1: the streaming engine — a lookup per cycle, a miss fetches the rest of the burst, responses ordered per master, §2.2 |
| `RD_OUTQ` | 1, 2, 4, 8 | read bursts a master may have accepted at once, across homes. Above 1 requires `RD_PIPE = 1` (enforced at elaboration) |
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

**The current array, the one-beat engine (`RD_PIPE = 0`).** Lookups
pipelined beside the RAM's latency, the fill address from the engine, fills
yielding to writes:

| M | N | K | read | write | crossings | LUT | FF | URAM | BRAM | WNS ns | Fmax MHz |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **4** | **4** | **1** | SAMD | SAMD | none | **8,408** | 7,340 | 256 | 0 | +1.107 | 449 |
| **4** | **4** | **1** | SAMD | SAMD | **4, DRAM side — ship** | **10,323** | 10,760 | 256 | 64 | +1.105 | 449 |
| 4 | 4 | 2 | SAMD | SAMD | none | 12,527 | 13,464 | 480 | 0 | +0.854 | 403 |
| 4 | 4 | 4 | SAMD | SAMD | none | 21,423 | 21,632 | 928 | 0 | +0.690 | 378 |
| 2 | 4 | 1 | SAMD | SAMD | none | 5,287 | 7,249 | 256 | 0 | +1.106 | 449 |
| 8 | 4 | 1 | SAMD | SAMD | none | 14,242 | 7,456 | 256 | 0 | +1.106 | 449 |
| 4 | 8 | 1 | SAMD | SAMD | none | 16,992 | 14,630 | 512 | 0 | +1.107 | 449 |
| 8 | 8 | 1 | SAMD | SAMD | none | 26,370 | 14,888 | 512 | 0 | +1.106 | 449 |
| 4 | 4 | 1 | SASD | SAMD | none | 8,538 | 7,086 | 256 | 0 | +1.103 | 448 |
| 4 | 4 | 1 | SAMD | SASD | none | 6,756 | 6,961 | 256 | 0 | +1.106 | 449 |
| 4 | 4 | 1 | SASD | SASD | none | 6,356 | 6,717 | 256 | 0 | +1.107 | 449 |
| 4 | 4 | 2 | SASD | SASD | none | 10,146 | 12,909 | 480 | 0 | +0.851 | 403 |
| 8 | 8 | 1 | SASD | SASD | none | 15,742 | 13,368 | 512 | 0 | +0.428 | 344 |
| 8 | 8 | 2 | SASD | SASD | none | 23,999 | 25,660 | 960 | 0 | +0.426 | 344 |

The array re-pipelining alone took the ship from 11,865 to 10,323 and the
no-crossing point from 9,914 to 8,408: the served word keeps one clock-enabled
source and the write port's address comes from a wire rather than a latched
copy of the lookup. The streaming engine's rows are §5.4.

**The first array revision** — the rows every number above is compared
against, kept as measured:

| M | N | K | read | write | crossings | LUT | FF | URAM | BRAM | WNS ns | Fmax MHz |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **4** | **4** | **1** | SAMD | SAMD | none | **9,914** | 7,390 | 256 | 0 | +1.301 | 492 |
| **4** | **4** | **1** | SAMD | SAMD | **4, DRAM side — ship** | **11,865** | 10,788 | 256 | 64 | +1.140 | 456 |
| 4 | 4 | 1 | SAMD | SAMD | ship + **4 KB channel interleave**, rotation (`NSWAP = 20`, `(i, i+2)`, `i = 12..31`) | **11,865** | 10,788 | 256 | 64 | +1.140 | 456 |
| 4 | 4 | 1 | SAMD | SAMD | ship + 4 KB interleave as a plain field swap (`NSWAP = 2`, `{33,32} ↔ {13,12}`; idles ¾ of the sets, §3.1) | 11,865 | 10,788 | 256 | 64 | +1.140 | 456 |
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

Fmax is flat across every `M`/`N` shape at `K = 1` (~495 MHz on the first
array, 449 on the current one): the crossbar's depth does not grow with port
count, because a binary-index mux adds one LUT6 + MUXF7 level per doubling.
`K` is the knob that moves timing, through the wider row and the sub-word
select; on the current array the binding path at `K > 1` is the read address
into the URAM cascade.

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
| channel interleave, 4 KB | `NSWAP` 0 → 20 (rotation) and 0 → 2 (swap) | **0** | 0 | 0 | wires: LUT, FF, WNS and Fmax identical to the digit at the ship point, in both forms |

`SETS` does not appear: the array is URAM, and LUT is independent of the row
count. `W` does not appear either — it was held at 512 throughout, and the
crossbar, the edges and the array word all scale with it together.

The rows above are the first array revision's; the current array's one-beat
rows (§5.1, first table) move every figure — read-SASD on it *costs* +130
(one arbiter over all M×N slots replaces N four-way ones), write-SASD saves
1,652, both together 2,052 — and the streaming engine's are §5.4.

The per-knob model fitted to the two current families, with its validation
against every row, is the [resource estimator](../../../docs-web/src/content/estimator.js)
and `scripts/py/kx_cost.py`. Its LUT model is a step table read from the
rows — exact at every measured point, interpolated between them, with the M×N,
SASD and K-under-SASD interactions as two-point terms — and its FF model a
least-squares fit whose worst residual is 1.24% (one-beat) and 2.15%
(streaming).

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
| **fused `kx_xache`, no crossing** | **9,914** | 7,390 | 0 | 256 | 492 |
| **fused `kx_xache`, ship (4 DRAM-side crossings)** | **11,865** | 10,788 | 64 | 256 | 456 |

At the real memory size the fused system is 4.2× fewer LUT (3.5× at the ship
point with its crossings), keeps the memory in URAM where 2,244 BRAM would be
83% of the part's 2,688, and meets the 300 MHz ask where the vendor cache's
244 MHz Fmax cannot. The vendor composition is a Σ of standalone synths, as the station-bus
page's vendor rows are; the fused figure is one synthesis of one netlist.

### 5.4 The streaming engine

The same 14 shapes with `RD_PIPE = 1`, `RD_OUTQ = 4`, on the current array —
the engine to ship. One synthesis per row at the 300 MHz ask; every row meets
it.

| M | N | K | read | write | crossings | LUT | FF | URAM | BRAM | WNS ns | Fmax MHz | one-beat LUT |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **4** | **4** | **1** | SAMD | SAMD | none | **7,839** | 7,763 | 256 | 0 | +1.202 | 469 | 8,408 |
| **4** | **4** | **1** | SAMD | SAMD | **4, DRAM side — ship** | **9,642** | 11,183 | 256 | 64 | +1.202 | 469 | 10,323 |
| 4 | 4 | 2 | SAMD | SAMD | none | 9,881 | 11,811 | 480 | 0 | +0.695 | 379 | 12,527 |
| 4 | 4 | 4 | SAMD | SAMD | none | 15,005 | 19,991 | 928 | 0 | +0.537 | 358 | 21,423 |
| 2 | 4 | 1 | SAMD | SAMD | none | 4,741 | 7,629 | 256 | 0 | +1.141 | 456 | 5,287 |
| 8 | 4 | 1 | SAMD | SAMD | none | 13,177 | 7,947 | 256 | 0 | +1.043 | 437 | 14,242 |
| 4 | 8 | 1 | SAMD | SAMD | none | 15,049 | 15,471 | 512 | 0 | +1.253 | 481 | 16,992 |
| 8 | 8 | 1 | SAMD | SAMD | none | 25,288 | 15,795 | 512 | 0 | +1.202 | 469 | 26,370 |
| 4 | 4 | 1 | SASD | SAMD | none | 5,001 | 7,213 | 256 | 0 | +0.707 | 381 | 8,538 |
| 4 | 4 | 1 | SAMD | SASD | none | 6,161 | 7,384 | 256 | 0 | +1.096 | 447 | 6,756 |
| 4 | 4 | 1 | SASD | SASD | none | 3,366 | 6,834 | 256 | 0 | +0.707 | 381 | 6,356 |
| 4 | 4 | 2 | SASD | SASD | none | 5,056 | 10,940 | 480 | 0 | +0.366 | 337 | 10,146 |
| 8 | 8 | 1 | SASD | SASD | none | 6,456 | 13,534 | 512 | 0 | +0.448 | 347 | 15,742 |
| 8 | 8 | 2 | SASD | SASD | none | 10,562 | 21,720 | 960 | 0 | +0.017 | 302 | 23,999 |

The ship at `RD_OUTQ` 1 / 2 / 4 / 8: 9,607 / 9,607 / 9,642 / 9,678 LUT,
11,147 / 11,151 / 11,183 / 11,231 FF, 445 / 445 / 469 / 469 MHz. The queue
depth is bookkeeping — a sequence number per master and a home-of-sequence
table — not datapath.

The streaming engine is **cheaper than the one-beat engine at every shape**,
by 569 at the digit and 681 at the ship, and by far more where the one-beat
engine's per-beat round trip had its own state: `K = 2` 12,527 → 9,881,
`K = 4` 21,423 → 15,005, SASD both 6,356 → 3,366, 8×8 SASD 15,742 → 6,456. The
one-beat engine held a fill register and a sub-word walk per home; the
streaming engine fills the array straight from `R` and walks the burst with
one 9-bit counter.

**How it got there — four loops, each a full sim gate and one synthesis per
row.** The ship row and the row that bound each loop:

| loop | change | ship, q4 | binding row | note |
|---|---|---|---|---|
| 1 | the streaming engine | 9,595 · 350 MHz | 4×4 K2: +0.049 ns, 304 MHz | `r_seq → turn → accept → restart` compare into the issuer's carry chain, 10–11 levels |
| 2 | turn registered; every compare against `dr_next` precomputed for both outcomes, only the 2:1 behind `accept` | 9,637 · 445 | read-SASD: **−0.295 ns**, 276 MHz | the 16-slot isolate-lowest into the AR bookkeeping, 14 levels |
| 3 | the grant registered | 9,622 · 469 | read-SASD: **−0.074 ns**, 294 MHz | still 13 LUT6 levels: `x & (~x + 1)` synthesised as a LUT ripple, not a carry chain (both-SASD at the same shape mapped differently and passed) |
| 4 | the isolate-lowest as a tree of 4-wide groups | **9,642 · 469** | 8×8 K2 SASD: +0.017 ns, 302 MHz — the `K = 2` URAM address path, not the arbiter | read-SASD **+0.707 ns, 381 MHz**; every row meets the ask |

Loop 2's registered turn costs one bubble per burst (1,087 vs 1,057 cycles
on the 64 KB stream); loop 3's registered grant one cycle per burst start (a
2 KB hit stream 36 → 37 cycles). Loop 4 changed no cycle.

**Per knob, streaming engine.** Marginal costs from adjacent rows of the table
above, *from → to*:

| knob | from → to | ΔLUT | ΔFF | ΔURAM | one-beat ΔLUT |
|---|---|---|---|---|---|
| `M` | 2 → 4 | +3,098 | +134 | 0 | +3,121 |
| `M` | 4 → 8 | +5,338 | +184 | 0 | +5,834 |
| `N_HOME` | 4 → 8 | +7,210 | +7,708 | +256 | +8,584 |
| `M × N` | 4×4 → 8×8 | +4,901 over `ΔM + ΔN` | | | +3,128 |
| `K` | 1 → 2 | +2,042 | +4,048 | +224 | +4,119 |
| `K` | 2 → 4 | +5,124 | +8,180 | +448 | +8,896 |
| read SASD | SAMD → SASD | **−2,838** | −550 | 0 | +130 |
| write SASD | SAMD → SASD | −1,678 | −379 | 0 | −1,652 |
| both SASD | 4×4 | −4,473 | −929 | 0 | −2,052 |
| both SASD | 8×8 | −18,832 | | | −10,628 |
| `K` 1 → 2 under both SASD | 4×4 / 8×8 | +1,690 / +4,106 | | | +3,790 / +8,257 |
| crossing, W/R in BRAM | per port | +451 | +855 | +16 BRAM | +479 |
| `RD_OUTQ` | 1 → 8 | +71 | +84 | 0 | — |

Read-SASD is the lever that changed sign: a per-home streaming engine is about
950 LUT (the lookup pipeline, the burst record, the DRAM fetch), so one for
four homes saves 2,838 where the one-beat engine — 140 LUT per home — saved
nothing and its shared arbiter cost 130. The two SASD savings still do not
add (−2,838 − 1,678 = −4,516 predicted, −4,473 measured, close here), and
`K` under a shared write path grows with M·N (1,690 at 4×4, 4,106 at 8×8),
which is why the estimator carries that as its own two-point term.

---

## 6. Performance

Nothing on this page is a routed figure. §6.1 is read off the state machines
of §2 and is exact for the same-clock case; §6.2–6.4 are the bench's cycle
counter on streaming traffic, §6.5 the master side.

### 6.1 Derived from the state machines

| `RD_PIPE = 0`, the one-beat engine | cycles on `clk` | where they go |
|---|---|---|
| read hit, first beat, URAM (`RD_LAT = 4`) | **9** from AR accept to R valid | ISSUE 1 + WAIT 5 + CHK 1 + DRAIN 1 + the index flop 1 |
| read hit, first beat, BRAM (`RD_LAT = 1`) | 6 | WAIT is 2 |
| read hit, each further beat of a burst | +9 | the engine walks a burst one IO word per round |
| read miss | hit + DRAM AR→R round trip + 2, **per line** | the fill is taken straight off R; the served word lands on the last beat |

| `RD_PIPE = 1`, the streaming engine | cycles on `clk` | where they go |
|---|---|---|
| read hit, first beat | `RD_LAT + 3` — 7 at URAM, 4 at BRAM | issue 1 + the RAM's latency + capture 1 + the turn register 1 |
| read hit, each further beat | **+1** | a lookup per cycle, taken as it lands |
| read miss, first beat | hit + one DRAM AR→R round trip + `RD_LAT + 2` | the line is written as its beat arrives, then re-looked-up |
| read miss, each further beat of the burst | +1 while DRAM streams | one fetch covers the rest of the burst |
| a stall by the master | `RD_LAT + 1` idle cycles when it clears | the dropped landings are replayed from the array |
| a burst boundary | 1 bubble | the next burst's turn registers a cycle after the last beat |

Both engines: writes stream at one beat per cycle per home once `AW` is
accepted, one write outstanding per master; each edge crossing adds one
`async_fifo` traversal each way, **not measured**.

### 6.2 Measured: the one-beat engine, with and without the interleave

`kx_xache_tb` with `TB_PERF`, 4×4 K1 SAMD, block-RAM arrays (`RD_LAT = 1`),
64 lines (4 KB) per home so a 64 KB stream misses, `axi4_ram` behind every
home, one clock. 64-beat (4 KB) bursts, one outstanding per master, cycles on
the fabric clock, GB/s quoted at 300 MHz. Every scenario re-checks its data.

| scenario | contiguous map | 4 KB interleave | DRAM requests per home |
|---|---|---|---|
| 1 master writes 64 KB | 1,104 cycles — **17.8 GB/s** | 1,104 — 17.8 GB/s | 16 / 0 / 0 / 0 → 4 / 4 / 4 / 4 |
| 1 master reads 64 KB, misses | 9,248 cycles — **2.13 GB/s** | 9,248 — 2.13 GB/s | 1,024 / 0 / 0 / 0 → 256 each |
| 1 master reads 2 KB, misses | 290 cycles — 9.06 per beat | same | |
| 1 master reads 2 KB, hits | 194 cycles — **6.06 per beat** | same | |
| 4 masters write 16 KB each | 1,074 cycles — 18.3 GB/s | **477 — 41.2 GB/s** | 16 / 0 / 0 / 0 → 4 / 4 / 4 / 4 |
| 4 masters read 16 KB each, misses | 9,233 cycles — 2.13 GB/s | **4,043 — 4.86 GB/s** | 1,024 / 0 / 0 / 0 → 256 each |

What the table says:

- **The interleave does what it claims.** Pages land on the homes the
  permutation names, the per-home counters are exactly even, and the data
  reads back through it. It costs nothing in cycles on a single stream,
  because a single master has one request outstanding and is served by one
  engine at a time either way.
- **Where it pays is contention.** Four masters streaming distinct regions of
  the same 4 GB all land on home 0 under the contiguous map and serialise on
  its two engines; interleaved at 4 KB they spread — 2.25× on writes, 2.28× on
  reads. Not 4×: the four regions are 16 KB apart, so all four streams start on
  home 0 together and march in step through homes 1, 2, 3.
- **Hit and miss latency match §6.1**: 6.06 cycles per beat on hits at
  `RD_LAT = 1`, 9.06 on misses against a model with a 3-cycle read latency.
- **Reads are engine-bound, and no interleave changes that.** A write stream
  runs at one beat per cycle per home; a read stream runs at one array round
  per beat. The read engine walks a burst one beat at a time even though, with
  the 4 KB bound, every beat of a burst is in the same home and the array
  could take a lookup every cycle. Pipelining that walk is the lever for read
  bandwidth; the crossbar and the arrays are not in the way.

### 6.3 Measured: the granularity

The same four-master scenario — 16 KB per master, regions back to back — with
**16 KB of cache per home** (256 lines; 64 KB in total, equal to the working
set), swept over the interleave granularity `2^G`. `rd_4m` reads the regions
the masters just wrote; `rd_4m_re` reads them again. "DRAM reads" is the
per-home `AR` count over the pass: 0 means every beat hit.

| granularity | 4 masters write | 4 masters read | read again | DRAM reads per home | 1 master reads 64 KB |
|---|---|---|---|---|---|
| contiguous | 1,074 cyc — 18.3 GB/s | 9,233 — 2.13 | 9,233 — 2.13 | 1,024 / 0 / 0 / 0 | 9,248 — 2.13, misses |
| 4 KB | 477 — 41.2 | 2,699 — 7.29 | 2,699 — 7.29 | 0 / 0 / 0 / 0 | 6,176 — 3.18, **all hits** |
| 8 KB | 473 — 41.6 | 2,697 — 7.29 | 2,697 — 7.29 | 0 / 0 / 0 / 0 | 6,176 — 3.18, all hits |
| **16 KB** | **276 — 71.2** | **1,544 — 12.7** | 1,544 — 12.7 | 0 / 0 / 0 / 0 | 6,176 — 3.18, all hits |
| 32 KB | 538 — 36.6 | 4,617 — 4.26 | 4,617 — 4.26 | 512 / 0 / 512 / 0 | 9,248 — 2.13, misses |
| 64 KB | 1,074 — 18.3 | 9,233 — 2.13 | 9,233 — 2.13 | 1,024 / 0 / 0 / 0 | 9,248 — 2.13, misses |

And with **4 KB of cache per home**, where nothing fits and every read misses:

| granularity | 4 masters write | 4 masters read | DRAM reads per home |
|---|---|---|---|
| contiguous | 1,074 — 18.3 | 9,233 — 2.13 | 1,024 / 0 / 0 / 0 |
| 4 KB | 477 — 41.2 | 4,043 — 4.86 | 256 each |
| 16 KB | 276 — 71.2 | **2,312 — 8.50** | 256 each |

Three things decide the granularity, and the table shows each:

1. **Above the burst — forced.** The guard holds `G ≥ 12`; below it a burst
   would change home mid-flight.
2. **At or below the cache per home — for hits.** The 64 KB working set only
   fits the four 16 KB arrays when it is spread over all four, which is any
   granularity ≤ 16 KB: the re-read pass and the single-master stream turn into
   all-hits at 3.18 GB/s. At 32 KB two homes each take 32 KB into 16 KB of
   array and thrash; at 64 KB one home takes all of it.
3. **Equal to a stream's own extent — for parallelism.** 16 KB is the only
   granularity here at which the four streams never meet: each master lives on
   its own home and the aggregate is exactly 4× one home — 71.2 GB/s written,
   12.7 GB/s read on hits, 8.50 GB/s read on misses (4 × 256 beats × 9.03
   cycles, perfectly overlapped). At 4 KB and 8 KB the four streams start on
   home 0 together and march in step, and the read pass takes 2,699 cycles
   against the 1,544 the four engines could deliver — 57% of the parallelism.

So the expectation holds: the efficient band is **burst length ≤ granularity
≤ cache per home**, and inside that band the granularity that matches the
per-stream extent avoids the lockstep loss. A hash (folding higher bits into
the home field) would remove the lockstep loss at fine granularity for any
extent; a swap alone does not, and none is built.

The write ceiling is one beat per cycle per home: 17.8 GB/s per home, 71 GB/s
over four. The read ceiling of the one-beat engine is its serial walk: 3.18
GB/s per home on hits at `RD_LAT = 1`, 2.13 on misses against this model. §6.4
is the streaming engine on the same scenarios.

### 6.4 Measured: the streaming engine and the read queue

Same bench, `RD_PIPE = 1`, block-RAM arrays, 4×4 K1 SAMD, one clock, 64-beat
bursts, and now a DRAM model with **24 cycles from `AR` to the first beat**
(`RD_LAT_CYC`); the one-beat engine is re-run under the same latency for the
comparison. GB/s at 300 MHz; every scenario re-checks its data.

Every figure is the shipped RTL (loop 4 of §5.4); the loops before it are
within a few percent (loop 1's 64 KB stream across the homes was 1,057
cycles, the registered turn and grant cost one cycle each per burst).

**One master, one home.** Contiguous map, 64 KB, 16 KB of cache per home:

| engine | 64 KB read, misses | 2 KB read, misses | 2 KB read, hits |
|---|---|---|---|
| one-beat (`RD_PIPE = 0`) | 33,809 cycles — **0.58 GB/s** | 1,058 — 33 per beat | 194 — 6.06 per beat, 3.17 GB/s |
| streaming, `RD_OUTQ = 1` | 1,553 cycles — **12.7 GB/s** | 66 — 2.06 per beat, 9.3 GB/s | 37 — **1.16 per beat, 16.6 GB/s** |
| streaming, `RD_OUTQ = 4` | 1,538 — 12.8 | 66 | 37 |

A miss stream on one home is 16 bursts × (64 beats + 24 latency + 8) cycles:
one fetch per burst instead of one per line, 22× the one-beat engine at this
latency. Hits stream at one beat per cycle plus the burst's 5-cycle start.
Contiguous 64 KB on one home is the one-channel case: whatever the queue, one
home serves one burst at a time, so `RD_OUTQ` buys nothing here — it needs
the interleave to spread consecutive bursts over the homes.

**One master across the homes.** 4 KB interleave, 4 KB of cache per home so
every read misses, 64 KB read:

| `RD_OUTQ` | cycles | GB/s | DRAM reads per home |
|---|---|---|---|
| 1 | 1,553 | 12.7 | 4 / 4 / 4 / 4 |
| 2 | 1,080 | 18.2 | 4 / 4 / 4 / 4 |
| **4** | **1,076** | **18.3** | 4 / 4 / 4 / 4 |
| 8, eight homes | 1,074 | 18.3 | 2 each |
| 4, DRAM latency 60 | 1,112 | 17.7 | 4 / 4 / 4 / 4 |

With two or more bursts accepted, the younger burst's home fetches while the
older drains, and the master's own port — 512 bits per cycle, 19.2 GB/s — is
what bounds it: 95% of the port at latency 24, 92% at 60. With 16 KB of cache
per home, so the 64 KB hits, the same master reads at 1,044 cycles — **18.8
GB/s**, 98% of its port — where the one-beat engine reads the same hits at
6,161 cycles, 3.19 GB/s. Nothing on the Xache side needs the master to change;
it needs the master to **issue** the next `AR` before the previous burst
drains, which is the master's outstanding depth (§6.5).

**Four masters at once**, 16 KB each, 16 KB of cache per home:

| interleave | one-beat engine | streaming, `RD_OUTQ = 4` |
|---|---|---|
| 4 KB, hits | 2,696 cycles — 7.29 GB/s | 465 — **42.3 GB/s** |
| 16 KB, hits | 1,541 — 12.8 | 270 — **72.8 GB/s** |
| 4 KB, all misses (4 KB of cache) | 14,792 — 1.33 | 581 — **33.8 GB/s** |
| 4 KB, all misses, DRAM latency 60 | | 725 — 27.1 |
| 4 KB, all misses, eight homes, `RD_OUTQ = 8` | | 389 — 50.6 |

The 16 KB row is four ports at their ceiling (four masters' writes at the
same interleave: 71.2 GB/s). At 4 KB the four streams start on home 0
together and march in step, the lockstep loss §6.3 describes; eight homes
halve it.

**Against the goal.** One channel at 300 MHz and 512 bits is 19.2 GB/s. A
single master reads at 18.3 GB/s on misses and 18.8 on hits where the
one-beat engine read at 0.58 and 3.19 (32× and 5.9×), and four masters at
34–73 GB/s, 1.8–3.8× one channel's peak.

### 6.5 The master side

The Xache accepts `RD_OUTQ` bursts from a master; a master that waits for
each burst to drain before issuing the next gets one burst in flight whatever
`RD_OUTQ` is. So single-master speed also needs the master's own outstanding
depth — an `AR` issued while the previous `R` is still streaming, nothing more:
no new signalling, no ID scheme, the plain AXI address/data decoupling.

The framework's master onto DRAM is `mag_dram_port` inside the system node,
and it already carries that depth as `RD_OUT` (exposed on `mag` as
`DRAM_RD_OUT`, default 1). It is verified at 2 and 4 by its component bench
with queued reads and by `mover_chain1/2/4`, and priced alone at 300 MHz:

| `RD_OUT` | LUT | FF | BRAM | Fmax | one requester, 20-word bursts | 256-word bursts |
|---|---|---|---|---|---|---|
| 1 | 2,115 | 1,894 | 16 | 384 | 2,744 MB/s | 8,034 |
| 2 | 2,127 | 1,904 | 16 | 385 | | |
| 4 | 2,244 | 2,104 | 16 | 385 | **8,917 MB/s** | 9,375 |

(`mag_dram_port_bw_tb`, mesh 300 MHz, 106 ns DRAM; the 256-bit internal beat
caps a requester at 9,600 MB/s.) `RD_OUT = 4` costs 129 LUT and 210 FF and
changes nothing else: same Fmax, same queues.

### 6.6 Across the shapes

The same bench at every measured shape, both engines, on the shipped RTL:
K = 1, SAMD both sides, no crossing, 24-cycle DRAM, 64-beat bursts, 16 KB of
cache per home (misses: 4 KB per home), 300 MHz. GB/s, one-beat engine →
streaming engine at `RD_OUTQ = 4`. The LUT is each row of §5.1/§5.4 (one
synthesis each); "Δ" is the streaming engine against the one-beat engine on
the same array.

**One master** (identical at every shape — one master uses one port, and the
homes it does not reach are idle):

| behaviour | 4 KB interleave | 16 KB interleave |
|---|---|---|
| write 64 KB | 17.8 → 17.8 | 17.8 → 17.8 |
| read 64 KB, hits | 3.19 → **18.8** | 3.19 → 18.4 |
| read 64 KB, misses | 0.58 → **18.3** | 0.58 → 13.6 |
| read 2 KB, hits | 3.17 → 16.6 | 3.17 → 16.6 |
| read 2 KB, misses | 0.58 → 9.3 | 0.58 → 9.3 |

**M masters at once**, 16 KB each in distinct regions:

| M × N | LUT one-beat → streaming (Δ) | write, 4 KB / 16 KB ilv | read hits, 4 KB / 16 KB | read misses, 4 KB / 16 KB | ceiling |
|---|---|---|---|---|---|
| 2 × 4 | 5,287 → 4,741 (−546) | 28.7 / 35.6 | 5.10 → **29.7** / 6.38 → **36.4** | 0.93 → 25.3 / 1.16 → 25.5 | 38.4 (2 ports) |
| 4 × 4 | 8,408 → 7,839 (−569); ship 10,323 → 9,642 (−681) | 41.2 / 71.2 | 7.29 → **42.3** / 12.8 → **72.8** | 1.33 → 33.8 / 2.33 → 50.9 | 76.8 (4 ports = 4 channels) |
| 8 × 4 | 14,242 → 13,177 (−1,065) | 52.8 / 73.1 | working set 128 KB > 64 KB of cache: every pass misses | 1.69 → **40.8** / 2.33 → **51.1** | 76.8 (4 channels) |
| 4 × 8 | 16,992 → 15,049 (−1,943) | 57.3 / 71.2 | 10.2 → **59.4** / 12.8 → **72.8** | 1.86 → 50.6 / 2.33 → 50.9 | 76.8 (4 ports) |
| 8 × 8 | 26,370 → 25,288 (−1,082) | 82.4 / 142.5 | 14.6 → **84.6** / 25.5 → **145.7** | 2.66 → 67.7 / 4.65 → 101.9 | 153.6 (8 ports = 8 channels) |

The 16 KB interleave with hits is every port at its ceiling for 4×4, 4×8 and
8×8 (95%); the 4 KB interleave is the lockstep case of §6.3, and adding homes
past the master count (4×8) lifts it from 42 to 59. Misses are bounded by the
channels and the one fetch in flight per engine: 8×8 at 16 KB reads 102 GB/s
from eight channels, 66% of their peak, against 4.65 for the one-beat engine
(22×). The write side is the same for both engines — the write engine already
streamed.

---

## 7. Verification

`tests/axi/kx_xache_tb.v` — the whole system between AXI masters and
`axi4_ram` models, one model per home, run under Verilator (`scripts/py/vlt.py`)
and xsim (`scripts/py/xsim.py kx_xache`). The bench parameters follow the
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
  back individually;
- sixteen consecutive 4 KB pages from one master, with the DRAM-side `AW`
  count per home asserted against the expected distribution — all on home 0
  with the contiguous map, `16 / N` each with the interleave — then read back;
- with `TB_ILV` below bit 12, that the build **fails to elaborate** (the
  `kx_perm` guard), which is the test of the bound rather than of the design;
- a burst that starts mid-line, misses, hits, and is re-read after its tail
  was rewritten (the streaming engine's fetch span);
- 64-beat streams, missing then hitting, under **random `RREADY` stalls** of
  1–3 cycles (the replay path), with a monitor that fails the run if `RVALID`
  ever drops while waiting;
- with `TB_RDQ > 1`: one master issuing eight bursts ahead of its collector,
  missing then hitting, and every master at once on distinct regions, all
  under random stalls — bursts spread over homes by the interleave, so they
  genuinely overlap;
- with `TB_RDQ > 1` and `RD_PIPE = 0`, that the build **fails to elaborate**.

Configurations run: `4×4 K1`, `4×4 K2`, `4×4 K4`, `2×4`, `8×4`, `4×8`, `8×8`,
each at SAMD and at SASD on either and both sides, at 64 URAM per home
(`SETS = 32768`), and the DRAM-side two-clock case at the ship shape. With the
interleave (rotation form, the bench carrying its own reference model of the
permutation and building every test address through its inverse): `4×4 K1` at
4 KB and at 32 KB, the two-clock ship, `4×8` (twenty pairs across three
lanes) and `4×4 K2` SASD on both sides. With the streaming engine, the gate
every loop of §5.4 passed before its synthesis: `4×4` at K1, K2 and K4 and
`RD_OUTQ` 4, `2×4`, `8×4`, `4×8` at `RD_OUTQ` 8, `8×8`, read-SASD, SASD on
both sides, the two-clock ship, the 4 KB interleave, `RD_OUTQ` 1 under a
24-cycle DRAM, and the performance scenarios of §6.4 under the interleave and
the 24-cycle DRAM (thirteen builds, 2,357–6,517 checks each); the one-beat
engine on the same array at K1 and K2. The
component benches beneath it are `kx_carray`, `kx_rd_engine`, `kx_wr_engine`,
`kx_link` and `kx_scdc`, each with its own bench in the same suite.

---

## 8. What it deliberately does not do

- **No set associativity and no replacement policy.** Direct-mapped by the set
  index; a conflicting line evicts on fill. The URAM budget goes to rows, not
  ways.
- **No writeback.** Write-through only, so the array never holds data DRAM does
  not, and a flush is a valid-clear rather than a drain.
- **One DRAM read in flight per engine.** The streaming engine turns a miss
  into one fetch for the rest of the burst, but does not overlap two fetches on
  one home; a miss stream on one home runs at `L / (L + latency)` of the
  channel. Across homes, `RD_OUTQ` overlaps them.
- **No interleaving below 4 KB, and no address hashing.** The permutation is
  a bit swap; a burst-splitting write engine and an XOR fold of higher bits
  into the home field are both possible and neither is built.
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

RTL: `src/kohakuaxi/` — `kx_xache.v` (the system), `kx_carray.v`,
`kx_rd_engine.v`, `kx_wr_engine.v`, `kx_link.v`, `kx_scdc.v`. Measurement:
`scripts/tcl/ooc_kx.tcl` (one configuration, every report), `scripts/py/kx_cost.py`
(the per-knob model and its validation gate).
