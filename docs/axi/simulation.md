# AXI simulation

General setup, tool traps and bench conventions: [`docs/simulation.md`](../simulation.md).

```powershell
.\tests\run_axi_sim.ps1                              # both benches
.\tests\run_axi_sim.ps1 -Dut src\kohakuaxi\my_slave.v  # your slave, hostile bench
.\tests\run_axi_sim.ps1 -Sim iverilog                # second opinion
```

| Bench | DUT | Checks |
|---|---|---|
| slave (hostile master) | `axi4_ram.v` | 291 |
| master (burst legality + data) | `axi4_master.v` driving `axi4_ram.v` | 2744 |

---

## 1. Slave bench — `tests/axi/axi4_ram_tb.v`

Drives the slave from a **deliberately hostile master**:

- `RREADY` is raised only *after* `RVALID` has been observed
- `BREADY` starts low, so the slave must **hold** `BVALID` rather than pulse it
- random stall cycles on every channel
- VALID-stability assertions: once asserted, VALID must stay up with an unchanged
  payload until READY
- a watchdog, because a protocol break shows up as a hang

**Why hostile.** A cooperative bench — `RREADY` tied high before the read is
issued — completely hides the most common slave bug, `RVALID` computed from
`RREADY`. `docs/axi/bringup.md` is the case study: `InstReceiver` passed its own
testbench and deadlocked on hardware on the first transaction.

Coverage: single beats, `BREADY` held low, bursts of 4/16/256, back-to-back with
differing IDs, and `WSTRB` byte-enables.

**`-Dut` swaps in your own slave** against the same bench, provided it has the same
port list. This is the intended way to qualify a new AXI slave before it goes near
hardware — a stalled slave hangs the whole SmartConnect, and `reset_hw_axi` does
not clear it; only reprogramming does.

---

## 2. Master bench — `tests/axi/axi4_master_tb.v`

`axi4_master` drives `axi4_ram`, so both sides are cross-validated: the slave was
proven by a hostile master, the master by a validated slave plus an independent
bus monitor.

**The bus monitor checks burst *legality*, separately from data.** On every
accepted `AW`/`AR` it asserts:

- `AxLEN <= 255`
- the burst does not cross a **4 KB boundary**
- `WLAST` lands on the last beat of **every** burst, not just the last burst of a
  command

That separation is the point. Data can round-trip perfectly while the master emits
illegal bursts that a simple slave tolerates and a real interconnect does not —
SmartConnect will split or stall, other fabrics corrupt. Inferring legality from a
correct payload does not work, so it is asserted directly.

Transfers exercised, with the burst counts they produce (write + read):

| Transfer | Beats | Bursts |
|---|---|---|
| single beat | 1 | 2 |
| short burst | 4 | 2 |
| 16 beats | 16 | 2 |
| 256 beats, page-aligned | 256 | **2** — max burst, correctly *not* split |
| crosses a 4 KB boundary | 64 | **4** — split into 2 each way |
| 700 beats | 700 | 6 |
| unaligned + crossing | 300 | 6 |

The page-aligned 256-beat case staying at one burst each way is the check that the
splitter is not being needlessly conservative.

---

## 3. Failure symptoms

| Symptom | Usually means |
|---|---|
| `WATCHDOG TIMEOUT` | VALID computed from READY; a burst with no `B` response or no `RLAST`; a state register too narrow to hold its states |
| `LEGALITY FAIL: ... crosses a 4 KB boundary` | burst splitting is wrong — check the beats-to-boundary calculation |
| `LEGALITY FAIL: WLAST not on the last beat` | `WLAST` derived from the caller's stream instead of the burst counter |
| `LEGALITY FAIL: AWLEN > 255` | burst length not clamped to 256 beats |
| `DATA FAIL @addr beat N` | address increment or byte-enable handling |
| `PROTOCOL ERROR: RVALID dropped before RREADY` | read data not held until accepted |
| `BID/RID != AWID/ARID` | IDs not reflected; with >1 master, responses route to the wrong place |

---

## 4. Design rules these benches enforce

Both `axi4_ram.v` and `axi4_master.v` follow the same discipline, and the benches
exist to keep it:

1. **VALID is never a function of READY.** READY may depend on VALID; never the
   reverse. This is AXI4 §A3.2.1 and the single most common way a hand-written
   slave deadlocks.
2. **Burst length comes from a counter, not from the data stream.** A master that
   miscounts `WLAST` must not be able to desynchronise the response.
3. **Every accepted `AW` produces exactly one `B`; every accepted `AR` produces
   exactly `ARLEN+1` `R` beats with `RLAST` on the last.** There is no timeout in
   AXI — a dropped response hangs forever.
4. **`BID`/`RID` echo `AWID`/`ARID`.** The interconnect routes responses by ID.
