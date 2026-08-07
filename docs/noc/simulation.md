# NoC simulation

General setup, tool traps and bench conventions: [`docs/simulation.md`](../simulation.md).
Protocol being tested: [`docs/noc/spec.md`](spec.md).

```powershell
.\tests\run_noc_sim.ps1              # all six benches
.\tests\run_noc_sim.ps1 -Size 4      # one specific mesh size
```

`xsim` only — the router instantiates `xpm_fifo_sync`, which needs `-L xpm`.

| Bench | Proves |
|---|---|
| 2×2 mesh | lossless, correctly routed, in-order, terminating |
| 3×3 mesh | the same at a larger size |
| orchestrator | AXI register map → mesh → loopback → RX, and the status mirror |
| system | AXI → dispatch → CU → completion → AXI polling |
| multi-CU | two CU designs on a 3×3 mesh, CU→CU traffic, faults, concurrency |
| CU framework | completions survive a busy outbound link |

---

## 1. Mesh benches — `tests/noc/noc_mesh_tb.v`

Builds an N×N mesh, hangs an injector and receiver off every local port, and checks
the four properties spec §11 claims:

- **lossless** — every injected flit arrives exactly once
- **correct** — it arrives at the node named in `dst_x`/`dst_y`
- **in-order** — per (source, destination) pair, which XY routing guarantees
- **no deadlock** — the run terminates; the watchdog firing *is* the failure

Three phases: all-to-all, a **diagonal flood** (every packet needs both an X and a
Y hop — the pattern that closes a cycle under adaptive routing), then random
traffic.

**Unconnected edge ports are monitored, not tied off.** A flit emitted there is a
routing error; dropping it silently would hide exactly what this bench exists to
catch. That check found the router's east/west swap on its first run — the inport
and outport both documented `0:N 1:E 2:S 3:W 4:L` and both implemented it, but the
router's concatenations listed them as `{local, east, south, west, north}`, on both
sides, so the errors composed instead of cancelling.

Mesh size is a **compile-time define**, not an elaboration generic, because
Vivado's `.bat` wrappers split arguments on `=`. The runner writes
`` `define MESH_N n `` into a generated file and compiles each size into its own
library.

> **What a pass means.** Over the traffic actually exercised: nothing lost,
> everything correctly routed, per-pair order held, and the run finished.
> Deadlock-freedom itself comes from XY routing being acyclic *by construction*
> (spec §2) — no finite test establishes it, so a pass here is corroboration, not
> proof.

---

## 2. Orchestrator bench — `tests/noc/noc_orchestrator_tb.v`

Drives the orchestrator's documented register map through an AXI master into a real
2×2 mesh. A loopback node at `(2,2)` swaps `src`/`dst` and returns each flit, so a
correct round trip is observable purely through `RX_FLIT`.

Covers:

- `CAPS` discovery — flit width, `POS_WIDTH`, grid bounds
- mailbox injection: `TX_FLIT[0..4]` + `TX_KICK`, five 64-bit beats per 288-bit flit
- reception: `RX_STATUS` / `RX_FLIT[0..4]` / `RX_POP`
- the `NODE_STATUS` mirror updating from a `CU_SIGNAL`
- instruction dispatch: stage flits, set `PROG_DST`, kick, confirm the dispatcher
  rewrote the destination

This is deliberately not a register-file unit test. What is worth proving is that a
host can put a flit on the mesh and get one back using only documented registers —
which is exactly what hardware bring-up will do from Tcl.

---

## 3. System bench — `tests/noc/noc_system_tb.v`

The end-to-end control path:

```
AXI writes STAGE[]  ->  PROG_DST / PROG_LEN / PROG_CREDIT  ->  PROG_KICK
   -> orchestrator dispatches CU_INST across the mesh
   -> CU executes, emits INST_COMPLETE per instruction
   -> CU emits BATCH_COMPLETE carrying txn_id as the program id
   -> NODE_STATUS[cu] updates
   -> host polls over AXI until that program id reports complete
```

**Nothing carries results back.** In production a CU writes results straight to
DRAM via `MEM_WR_REQ` to MAS, and the orchestrator never sees them — completion is
all the host learns from this path. The bench models that rather than routing data
home, because a bench that returned results would be testing a path that will not
exist.

Two programs with different ids run in sequence, because *"is program X done"* only
means something if polling can distinguish them. The bench asserts the latest
`last_arg` is the second program's id, and that `signal_count` equals the total
instruction count across both.

`tests/noc/noc_pseudo_cu.v` is a test double conforming to the mandatory CU
interface (spec §6) without computing anything: instruction FIFO in, execute for
`EXEC_CYCLES`, signal out. It **validates every flit it receives** and counts
rejects — which is how a flit-width bug in the bench surfaced immediately as
`bad_count = 4` rather than as mysterious data corruption. It replies to the `src`
in the received flit, so it needs no knowledge of where the orchestrator is.

---

## 4. Multi-CU bench — `tests/noc/noc_multicu_tb.v`

The bench that exercises the CU framework
([`docs/noc/cu-framework.md`](cu-framework.md)) rather than a single test double.
A 3×3 mesh with an orchestrator at `(1,1)` and three CUs of two different designs:

| node | module | shape |
|---|---|---|
| `(3,3)` | `cu_alu` | consumes instructions, variable latency, can fault |
| `(1,3)` | `cu_relay` A | originates `CU_DATA`, retires on *sent* |
| `(3,1)` | `cu_relay` B | receives and fingerprints it |

Two designs rather than three copies of one, because a framework accidentally
fitted to a single CU style passes a homogeneous bench. Six sections:

1. **discovery** — `CU_CTRL` register 0 from each node returns its own `CU_TYPE`,
   version, buffer count and instruction depth. This is what makes an unknown CU
   enumerable instead of hardcoded.
2. **compute** — twelve ALU ops checked against a model computed in the bench,
   including the accumulator. Not "did it signal" but "is the arithmetic right".
3. **fault** — a `0xFF` opcode produces `SIG_FAULT`, and the batch still
   terminates. A CU that faults must not strand the dispatcher.
4. **CU→CU** — relay A sends five `CU_DATA` packets to relay B across two mesh
   hops, with no orchestrator involvement in the data path. This is the traffic
   pattern production actually uses; everything before it is control plane.
5. **concurrency** — a program to the ALU and a program to relay B overlap. The
   staging buffer is refilled only after `PROG_STATUS.running` clears, *not* after
   the CU finishes: the CU executes while the next program is staged.
6. **backpressure** — a 100-instruction program through a 32-deep instruction
   FIFO with 24 credits, so the dispatcher must actually block on credit return
   rather than running to completion in one go.

Section 6 is the one that found the orchestrator's RX head-of-line bug: `CU_SIGNAL`
was being queued into a 16-deep RX FIFO nobody drained, so after 16 completions
`noc_in_busy` stuck high and the orchestrator stopped accepting the very signals
that return credits. Execution stopped at exactly the credit count with no error
anywhere. Signals now bypass RX and update `NODE_STATUS` only.

---

## 5. CU framework bench — `tests/noc/cu_base_tb.v`

No mesh and no orchestrator: `cu_alu` alone, with the testbench driving
`noc_out_busy` directly. The mesh benches cannot reach this case because they
never hold a CU's outbound link busy long enough for completions to pile up.

What is at stake is credit accounting. `SIG_INST_COMPLETE` is what returns a
dispatch credit, so a completion that is generated but never transmitted is a
credit that never comes back — and the orchestrator stalls much later, with
nothing to point at. Three sections:

1. the link is held busy while 8 instructions retire, then freed — all 8 signals
   must arrive, with their args intact
2. the link stutters (7 busy / 3 free) through a 24-instruction program
3. the link is held busy indefinitely — the CU must *stop accepting* rather than
   execute into a void, so `noc_in_busy` going high is the correct behaviour

This bench was written before the fix and failed exactly as predicted: 8
instructions produced **1** signal, because a single `sig_pend` register was
overwritten by each new completion. `noc_cu_base` now queues completions 16 deep
and stops issuing when that queue is full.

---

## 6. Failure symptoms

| Symptom | Usually means |
|---|---|
| `ROUTING ERROR: ... emitted <dir> off-mesh` | route computation sending a flit out an edge port — direction mapping or clamping |
| `OUT OF ORDER: src N -> node, seq X expected Y` | two packets between one pair took different paths; XY routing is not being applied |
| `MISDELIVERY` | a flit reached a node it was not addressed to |
| `sent N, received M` with `M < N` | flits lost or stuck; the watchdog prints the in-flight count |
| `WATCHDOG TIMEOUT` | routing deadlock, or backpressure lost so a FIFO never drains |
| `bad_count` non-zero in the system bench | the CU received something that was not a `CU_INST` addressed to it — usually a malformed header |
| dispatch never completes | credit exhaustion: `PROG_CREDIT` too small, or the CU is not returning `INST_COMPLETE` |

---

## 7. The NoC link protocol, for bench authors

The mesh link is **busy/valid with retry, not AXI valid/ready**, and mixing them
up is the easiest way to write a broken bench:

- a sender asserts `valid` and **holds** `valid` and `data` unchanged until a
  cycle in which `busy` is low; that cycle is the transfer
- a receiver accepts **iff** `valid && !busy`, once, and never on a cycle its own
  `busy` is high
- `OutPortSwitch` is the reference sender: `room = !(out_valid && busy)`, so the
  register only reloads when the flit it holds is gone or going

Both halves are required. A bench sender that drops `valid` when `busy` rises
destroys the flit; a bench receiver that takes whatever is valid enqueues a real
sender's held flit once per cycle of backpressure. The two failures look
identical in a reassembly counter and are opposite in cause —
[spec.md](spec.md) §2.1.

`noc_inport.v` asserts the sender's half and prints `flit LOST -- sender did not
hold`. **That message names the bench, not the mesh**, when it fires against a
hand-written driver.

This is the one place in the project where "VALID must not depend on READY" does
*not* apply, because it is not AXI.
