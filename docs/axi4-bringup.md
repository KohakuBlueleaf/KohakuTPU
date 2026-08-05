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

Same probe against `src/hakuaxi/axi4_ram.v`: `RVALID OK (after 1 cyc)`,
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

`src/hakuaxi/axi4_ram.v` — a minimal, correct AXI4-Full slave RAM.
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
.\tests\run_axi_sim.ps1 -Dut src\hakuaxi\mine.v   # your slave, same ports
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
