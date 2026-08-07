# The CU framework — what every compute unit must have

`src/kohakunoc/noc_cu_base.v`

A compute unit is whatever you want it to be. What it must *also* be is a
well-behaved NoC node, and that part is identical for every CU in the machine.
This document separates the two: the obligations, why each exists, and the
handshake `noc_cu_base` gives you so you only write the datapath.

The short version: **a CU author writes a datapath and nothing else.** If you
find yourself building a flit header, you are doing the framework's job.

---

## 1. The five obligations

Every node hanging off a router owes the rest of the machine these. They are not
style guidance — violating any one of them can wedge the control plane or the
mesh itself.

### 1.1 Hold `valid` until you see `busy` low — and accept only when your own is

The NoC link is a **busy/valid pair with retry**, not AXI `valid`/`ready`. Both
ends owe one line each, and neither is separately choosable
([spec.md](spec.md) §2.1):

- **sending:** assert `valid`, and hold `valid` and `data` *unchanged* until a
  cycle in which `busy` is low. That cycle is the transfer. Withdrawing the flit
  because `busy` went high destroys it.
- **receiving:** accept **iff** `valid && !busy`. Never on a cycle your own
  `busy` is high, and never twice.

The two are one contract because each half is unsound without the other. A
sender that commits a cycle ahead and does not retry loses the flit whenever the
receiver raises `busy` in between; a receiver that takes whatever is valid
enqueues a *holding* sender's flit once per cycle of backpressure, which is
duplication rather than loss and presents identically downstream.

This is the single most common way to lose packets, and it is invisible in a
lightly-loaded test — you only see it when a FIFO actually fills. `noc_inport.v`
carries the assertion for the sender's half: a flit offered while `busy` must
still be offered next cycle, unchanged.

### 1.2 Keep `busy` transient — a queue you are actually draining

The mesh applies hop-by-hop backpressure, so refusing is legal and losing is
not: raise `busy` and the flit will be re-offered. What is fatal is raising it
forever — pressure backs into your router, then into the router behind it, and
head-of-line blocking in a mesh is a real deadlock source. `busy` must be tied
to a queue that something is emptying.

Do not reach for an almost-full margin instead. Retry is what makes plain
`full` safe, and `sync_fifo`'s `wr_almost` is not the margin it looks like:
`USE_ADV_FEATURES` is zero, so `prog_full` is tied low and `wr_almost` reduces
to `wr_busy`. A CU that wants real headroom has to count its own occupancy
([spec.md](spec.md) §2.1).

### 1.3 Expose an ordered instruction FIFO with visible free space

`CU_INST` must be buffered separately from everything else, delivered in order,
and the free space must be readable (`inst_space`, and via `CU_CTRL` register 1).

The reason is credit-based dispatch. The orchestrator will not send more
instructions than the CU has room for, but it can only honour that if the CU
publishes the number. A CU that lies about its space, or that shares one queue
between instructions and data, will drop instructions under load.

### 1.4 Answer `CU_CTRL`

Four mandatory words, so a controller can enumerate a CU it has never heard of:
type, version, buffer count, instruction depth, busy, error, free space. Without
this, every CU type needs a hardcoded entry in the host software, and adding a
CU means editing the driver.

`CU_CTRL` must be answered even while the datapath is busy or faulted — it is
the mechanism by which software finds out that a CU is busy or faulted.

### 1.5 Signal completion

Every instruction retires with a `CU_SIGNAL` back to whoever sent it. This is
not merely informational: **`SIG_INST_COMPLETE` is what returns a dispatch
credit.** A CU that executes an instruction without signalling permanently
consumes one credit, and the orchestrator eventually stalls with no error
anywhere.

The reply goes to the `src` of the `CU_INST` flit, so a CU never needs to be
configured with its orchestrator's address.

Completions are **queued** (16 deep), not held in a single register. A datapath
can retire faster than a congested link drains, and one register would let each
new completion overwrite the last -- losing credits with no symptom until the
orchestrator stalls. When that queue fills, `inst_valid` drops: the framework
would rather stop issuing than execute an instruction it cannot report.

---

## 2. What the framework hands you

Instantiate `noc_cu_base`, and you implement three interfaces. That is the whole
contract.

```
   ---- instruction issue ------------------------------------------
   inst_flit[FLIT_WIDTH-1:0]   the flit; your operands are in the payload
   inst_valid                  one is available
   inst_ready                  you took it            (you drive)

   ---- retirement -------------------------------------------------
   exec_done                   pulse: the instruction finished  (you drive)
   exec_result[31:0]           whatever you want reported       (you drive)
   exec_fault                  it failed instead                (you drive)

   ---- packets that are not instructions --------------------------
   send_flit / send_valid / send_ready     you originate  (CU_DATA, MEM_RD_REQ…)
   recv_flit / recv_valid / recv_ready     addressed to you
```

Plus `inst_space` and `busy` for visibility.

Issue is strictly one-at-a-time: `inst_valid` drops until you report `exec_done`,
and also while the completion queue is full.
If your datapath is pipelined and you want several in flight, buffer them behind
your own `inst_ready` and report `exec_done` in order — the framework tracks one
reply context (`src`, `id`, `last`) at a time.

### The minimum viable CU

```verilog
noc_cu_base #(.CU_TYPE(16'h1234), .POS_X(3), .POS_Y(4)) base (
    .clk(clk), .resetn(resetn),
    .noc_in_data(noc_in_data),   .noc_in_valid(noc_in_valid),   .noc_in_busy(noc_in_busy),
    .noc_out_data(noc_out_data), .noc_out_valid(noc_out_valid), .noc_out_busy(noc_out_busy),
    .inst_flit(inst_flit), .inst_valid(inst_valid), .inst_ready(inst_ready),
    .exec_done(exec_done), .exec_result(exec_result), .exec_fault(1'b0),
    .send_flit({FLIT_WIDTH{1'b0}}), .send_valid(1'b0), .send_ready(),
    .recv_flit(), .recv_valid(), .recv_ready(1'b1),
    .inst_space(), .busy()
);
```

A CU that does not originate traffic ties `send_valid` low; one that does not
expect any ties `recv_ready` high so the receive queue self-drains. **Do not tie
`recv_ready` low** — the queue fills, `noc_in_busy` sticks high, and obligation
1.2 is violated.

---

## 3. Rules that bit us

Each of these is a bug that was actually found in simulation, not a
hypothetical.

**`exec_result` must be combinational at the point you register it.** Reading
your own `exec_result` in the same `always` block that assigns it gets the
*previous* value — it is a non-blocking assign. `cu_alu` computes `res`
combinationally and uses it for both `exec_result <= res` and `acc <= acc + res`.

**Concatenations must be exactly `FLIT_WIDTH` wide.** Verilog silently
left-pads a short concatenation, which shifts the entire header into the payload
and produces a flit that routes to the wrong node — or nowhere. `cu_relay`
originally built 272 bits into a 288-bit register and its packets vanished
without a single error message. Unsized integer literals in a concatenation
contribute **32 bits**, not their apparent width; always size them.

**One driver per register.** A CU that drives `exec_done` from two `always`
blocks synthesises to X. This bit both `noc_pseudo_cu` and `cu_alu`.

**Declare every net.** An undeclared identifier in a port list becomes an
implicit **1-bit** net in Vivado (IEEE 1364-2005 §3.5) and your 288-bit link
silently becomes one wire. iverilog does not do this, so it will not save you.
The whole codebase uses `` `default_nettype none `` to make this an error.

**Do not gate the framework's TX behind your own.** Signals must win
arbitration, because they carry credits. The framework already prioritises
signals > `CU_CTRL` > your `send_*`; do not build a second arbiter in front of it.

---

## 4. Worked examples

Both live in `tests/noc/` and are deliberately different shapes, so that a
framework accidentally fitted to one style would fail on the other.

| | `cu_alu.v` | `cu_relay.v` |
|---|---|---|
| `CU_TYPE` | `0x0A10` | `0x0E11` |
| shape | consumes instructions only | originates traffic |
| latency | `1 + (opcode & 3)` cycles | until the packet is accepted |
| uses | `exec_fault` | `send_*`, `recv_*` |
| checks | result correctness vs a model | delivery and ordering |

`cu_alu` retires out of step with issue, which is what actually exercises the
framework's in-flight tracking. `cu_relay` retires only once the NoC has taken
the packet, so "instruction complete" means *sent*, not *queued* — worth copying
if your CU originates traffic and the host cares about ordering.

---

## 5. Signal semantics

| code | name | `arg` carries |
|---|---|---|
| `0x00` | `SIG_INST_COMPLETE` | your `exec_result` |
| `0x01` | `SIG_BATCH_COMPLETE` | the **program id** (the flit's `txn` field) |
| `0x04` | `SIG_FAULT` | your `exec_result` |

The batch signal is the one the host polls for: it answers "is program *N*
done?" without tracking individual instructions. It is emitted when the
instruction whose `last` bit is set retires.

A fault reports `exec_result` rather than the program id, on the grounds that
*why* it failed is more useful than *which batch* it was in — the host knows the
batch from the node that signalled. If you need both, encode the batch into your
own `exec_result`.

`CU_SIGNAL` is **not** queued into the orchestrator's RX FIFO; it is summarised
into `NODE_STATUS[n]`. See §6.

---

## 6. Two design decisions worth knowing about

Both were forced by the multi-CU bench and both are load-bearing.

**Signals bypass the orchestrator's RX FIFO.** They update `NODE_STATUS` and are
then dropped. Queuing them meant that a host which never read RX would fill the
16-deep FIFO, raise `noc_in_busy`, and stop the orchestrator accepting
*anything* — including the signals that return dispatch credits. The machine
then stalls silently after exactly `RX_DEPTH` completions with no error
reported anywhere. Signals are now always absorbed.

**The staging buffer is single-use while a dispatch runs.** The dispatcher
streams instructions out of the staging BRAM as it goes, so refilling it before
`PROG_STATUS.running` clears corrupts the program in flight. Wait for the
dispatch to drain — which is *not* the same as waiting for the CU to finish
executing. The CU keeps working while the next program is staged, which is where
the concurrency comes from.

---

## 7. Checklist

Before calling a new CU done:

- [ ] `send_valid` is withheld whenever `send_ready` is low, and a flit already
      offered is **held unchanged** until it is taken
- [ ] `recv_ready` is high, or the receive queue is genuinely drained
- [ ] every issued instruction produces exactly one `exec_done`
- [ ] `exec_done` is a single-cycle pulse
- [ ] every flit you build is exactly `FLIT_WIDTH` bits (count them)
- [ ] `CU_TYPE` is unique
- [ ] `POS_X`/`POS_Y` are inside `GRID_LO..GRID_HI`
- [ ] it passes `tests/run_noc_sim.ps1` when dropped into the multi-CU bench
- [ ] it still retires everything with its outbound link held busy
      (`tests/noc/cu_base_tb.v` does this to `cu_alu`)

See `docs/noc/simulation.md` for how to run it.
