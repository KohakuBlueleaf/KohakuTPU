# AXI4 bring-up: why InstReceiver fails on hardware

Short version: **the read path can never return data that was written over AXI.**
The write side is fine. Everything else below is real but secondary.

All findings reproduced in simulation with `tests/axi/axi4_ram_tb.v`, which models a
*hostile* master (delayed READY, late RREADY, random stalls) instead of a
cooperative one.

## Root cause (proven, not guessed)

`S_AXI_RDATA` is driven from `data_read`, which is loaded from `data_reg[]` and
gated by `data_valid_reg[]`:

```verilog
if (data_next) begin
    if (data_valid_reg[read_inst_addr]) begin
        data_read <= data_reg[read_inst_addr];
        data_read_valid <= 1;
    end else begin
        data_read <= 0;
        data_read_valid <= 0;      // <-- read produces nothing, forever
    end
end
```

`data_valid_reg[i]` is set **only** by the `data_valid` input port — the
compute-unit write-back. And every AXI write explicitly clears it:

```verilog
if (write_valid) begin
    instruction_reg[write_inst_addr] <= S_AXI_WDATA;
    instruction_valid_reg[write_inst_addr] <= 1;
    data_valid_reg[write_inst_addr] <= 0;     // <-- line 254
end
```

So `write addr X` → `read addr X` finds `data_valid_reg[X] == 0` →
`data_read_valid` stays 0 → **`RVALID` never asserts** → the AXI read never
completes. There is no timeout in AXI: the interconnect waits forever, and
`run_hw_axi` reports failure.

Measured, with `RREADY` held high the entire time (maximally cooperative):

```
=== STEP 1: single write to addr 0, BREADY high the whole time ===
  AWREADY      OK  (after 0 cyc)
  WREADY       OK  (after 0 cyc)
  BVALID       OK  (after 0 cyc)
=== STEP 2: read back addr 0 with RREADY HIGH FROM THE START ===
  ARREADY      OK  (after 0 cyc)
  RVALID   TIMEOUT (after 200 cyc)
```

Same probe against `src/kohakuaxi/axi4_ram.v`: `RVALID OK (after 1 cyc)`,
`RDATA = deadbeefcafebabe`.

**Why simulation passed.** `tests/axi/inst_receive_tb2.v` Test Case 3 drives
`data_valid` to populate `data_reg[]` *before* Test Case 4 reads. On the FPGA
nothing drives that port, so the array is always empty. The bench tested a path
the hardware never takes.

This is a design question, not a typo: the module is an instruction FIFO whose
read port returns compute-unit results, not a RAM. Write-then-read-back is not
something it was built to do — but that is exactly what `scripts/test_axi.tcl`
does, and what `create_hw_axi_txn` does.

## The other bugs (all real, all would bite later)

**1. `RVALID` computed from `RREADY` — AXI4 spec violation (A3.2.1).**

```verilog
assign S_AXI_RVALID = S_AXI_RREADY && data_read_valid;   // illegal
```

A slave must never wait for READY before asserting VALID. Any master that waits
for `RVALID` before raising `RREADY` deadlocks instantly, and this creates a
combinational path from the interconnect's `RREADY` back into `RVALID`. READY may
depend on VALID; never the reverse. `S_AXI_RLAST` inherits the problem, since it
is `S_AXI_RVALID && ...`.

**2. `write_state_reg` is one bit wide.**

```verilog
reg write_state_reg=WRITE_WAIT, write_state_next;      // 1 bit!
reg [1:0] read_state_reg=READ_WAIT, read_state_next;   // read side is correct
```

Verilog gives both names on that line the declared width — none, so 1 bit.
`WRITE_LAST` is `2'b10`, which truncates to `1'b0` = `WRITE_WAIT`. The
`WRITE_LAST` state is unreachable. That state exists precisely to hold `BVALID`
when `BREADY` is low at the end of a burst — so when a real interconnect delays
`BREADY`, the write response is silently dropped and the bus hangs. Your bench
always has `BREADY` high at that moment, so it never enters the path.

**3. `write_valid` infers a latch.** It is assigned inside `always @*` but has no
default before the `case` and no `default:` arm. Synthesis will infer a latch on
the signal that gates RAM writes. This is a class of bug that behaves differently
in simulation and hardware by construction.

**4. Use-before-declaration.** `read_inst_addr` is used at line 84 and declared at
line 280. Icarus accepts it; Vivado's `xvlog` rejects it outright:

```
ERROR: [VRFC 10-3380] identifier 'read_inst_addr' is used before its declaration
```

Worth knowing generally: iverilog is permissive, Vivado's simulator is stricter,
and Vivado *synthesis* is a third frontend again. An undeclared identifier can
become an implicit **1-bit** net in a lenient tool — silently correct-looking and
catastrophically wrong.

**5. Burst-length off-by-one, both channels.**

```verilog
write_count_next = S_AXI_AWLEN;
if (write_count_reg > 0) ...     // tests the PREVIOUS transaction's counter
```

```verilog
read_count_next = S_AXI_ARLEN;
if (read_count_next-1 > 0) ...   // ARLEN=0 -> 0-1 = 8'hFF > 0 -> "not last"
```

A single-beat read (`ARLEN=0`) is therefore not flagged as last.

**6. `data_read_valid` is a one-cycle pulse.** If the master does not accept in
that exact cycle the beat is lost. Read data must be held until `RREADY`.

## The reference slave

`src/kohakuaxi/axi4_ram.v` — a minimal, correct AXI4-Full slave RAM.
Parameterised (`DATA_WIDTH`, `ADDR_WIDTH`, `ID_WIDTH`, `DEPTH`), handles
INCR/FIXED/WRAP bursts, `WSTRB` byte-enables, ID reflection, bursts to 256 beats.

Design rules it exists to demonstrate:

1. VALID is always a **register**, never a function of READY.
2. Every accepted AW produces exactly one B; every accepted AR produces exactly
   `ARLEN+1` R beats with `RLAST` on the last. A dropped response hangs forever.
3. `BID`/`RID` echo `AWID`/`ARID` — the interconnect routes responses by ID.
4. The burst counter, not `WLAST`, decides when the burst ends. A master that
   lies about `WLAST` must not be able to desynchronise the response.

Note it takes **`resetn` (active low)**, like every AXI component. `InstReceiver`
takes active-high `rst`; a polarity mistake there looks exactly like a dead slave.

## Testing it

Simulation (auto-detects iverilog, else Vivado xsim):

```powershell
.\tests\run_axi_sim.ps1                                  # the reference slave
.\tests\run_axi_sim.ps1 -Dut src\kohakuaxi\mine.v   # your slave, same ports
.\tests\run_axi_sim.ps1 -Sim xsim                        # force Vivado
```

Run it under **both** simulators before believing a result.

On hardware, the JTAG-to-AXI harness in the `JTAG-DMA-test` project drives it
directly — that path is already validated end-to-end against DDR4:

```powershell
python tools\vtcl.py -c "source tools/jtag_axi.tcl; jaxi::connect"
python tools\mem.py poke <base> DEADBEEFCAFEBABE
python tools\mem.py peek <base> 1
python tools\mem.py test <base> 4096      # address-derived pattern sweep
```

`jaxi::try_read` returns the AXI response instead of raising, which is what you
want when probing a slave whose decode behaviour you're still checking.

## Suggested bring-up order

1. `axi4_ram` alone behind `jtag_axi` — proves clock, reset polarity, address
   decode and the SmartConnect hookup, with RTL known to be correct.
2. Swap in your own slave, same ports, same test. Any failure is now yours alone.
3. Only then put real logic behind the AXI port.

For the master side: consider not writing one. Xilinx `axi_datamover` (or
`axi_dma`) takes a simple command stream — address, length, go — and does all the
AXI4 burst generation, 4 KB-boundary splitting and reordering for you. Given four
DDR4 channels you would need at most four instances, and none of that code is
yours to debug.

## Ports a block design cannot infer

Before a synthesis top goes into a block design, sweep its port list. The rule:

> **An unconnected output is harmless. An undriven input is the fault.**

A flattened bus arrives in a BD as loose wires. Nothing connects them, nothing
complains, and the logic behind them is unreachable — synthesis prunes it, the
design builds, meets timing and programs. That is exactly what happened to the
memory mover's `mv_cfg_*` sideband: the shipped bitstream has no nets on it, so
the mover and the PRNG behind it were commandable by nothing. Nothing failed.
The mover simply never moved.

An output left dangling costs at most the cone that feeds it, and the log says so.

Two ways to make an input inferable:

1. **Name the interface.** `X_INTERFACE_INFO` / `X_INTERFACE_PARAMETER` on the
   ports, with `ASSOCIATED_BUSIF` on each clock, so the BD recognises the bus and
   connects the whole thing in one action. `src/synth_top/axi_n1_wrap_4.v` does
   this; `scripts/py/gen_axi_wrap.py` generates it, one wrapper per N because a
   Verilog port list cannot come from a generate block.
2. **Put it behind an interface that already exists.** The mover's command path
   now decodes out of `S_AXI_CTRL` through the orchestrator's aux window instead
   of arriving on pins of its own.

The sweep itself: every input on the top must belong to an inferable interface,
or be a clock or a reset. On `ktpu_ship_2x2` the only port that is neither is
`obs`, and `obs` is an output.

**How to check a wrapper is only wiring.** Synthesise it and the module it wraps
at the same parameters; the areas must be *identical*. A mis-wire that leaves
inputs unconnected lets synthesis prune, so the wrapper comes out **smaller** —
which is why an identical number is evidence here when usually it would be a
coincidence. `axi_n1_wrap_4` and bare `axi_n1` at N=4 both measure 955 LUT /
942 FF / 8.5 BRAM / 604.6 MHz. Register each generated wrapper in
`tests/run_synth_check.ps1`: one that no target names has never been read by a
tool, however green the tier is.

## Block design traps

Two of these cost a real amount of time on the multi-mesh block design, and
neither produces an error at the point of the mistake.

**Tcl's `format %X` is 32-bit.** Every address above 4 GB comes back truncated:
`0x400000000` as `0x0`, `0x400800000` as `0x800000`, `id << 32` as `0`. On a
34-bit map that silently piles all four memory windows and every control
register onto the bottom of the address space. It is not a Vivado clamp — the
one offset that survived was the GPIO's, and the only thing distinguishing it
was that it was passed as a literal rather than through `format`.

```tcl
    format 0x%llX $addr        ;# not %X
```

Worth knowing beyond this script: it fails silently, produces a design that
**validates and builds**, and surfaces much later as overlapping segments.

**`design_1.bd` declares the DDR refclk at 100 MHz and the board is 400.**
`CONFIG.FREQ_HZ {100000000}` on `c1_sys`/`c2_sys`/`c3_sys`; the board's DDR
refclks are 400 MHz and `singlemesh.bd` — the design actually on the card —
says `400160000`. Copying design_1's value produces four
`CRITICAL WARNING [ddr4:2.2-1]`. design_1 was never implemented, which is why its
value was never caught.

> **Follow `singlemesh` wherever the two references disagree.** design_1 is the
> convention for *structure* — how multiple DDR channels are instantiated and
> wired — and `singlemesh` is the authority on any *value*, because it is the one
> that has been through implementation. Reading a parameter off a design that was
> never built is reading an untested claim.

The measured board map those two references describe — which SLR each channel
lands on, its refclk pin and its bank — is in
[`../general_cores/slr.md`](../general_cores/slr.md) §2.2.
