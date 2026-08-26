# The `--cc` harness

`vlt.py` runs `--binary` today (`scripts/py/vlt.py:155`). That produces a
standalone executable running a Verilog testbench, with no way in from outside.
**Every "online simulation" goal in this directory needs `--cc` instead**, where
Verilator emits a C++ class and the harness owns `main()`, the clock, and
`eval()`.

This document is the concrete plan for that harness, and the argument that
**SysCore phase 1 is the cheapest place to build it first.**

---

## 1. Build it for a bare core before building it for a card

[card-backend.md](card-backend.md) wants the harness driving
`S_AXI_CTRL`/`S_AXI_MEM` into a station bus into a populated mesh. That is the
right destination and it is blocked today: three model-validation items are open
in [status.md](status.md), and one of them is `ctrlpe_mesh` — the card chain
itself — failing at "the processor never started".

A standalone RV64 core has none of those dependencies. No station bus, no NoC,
no `mag_link`, no `sb_line4`, and essentially no XPM in the path. Its interface
is a clock, a reset, an instruction port and a data port.

So the ordering that gets a working harness soonest:

1. **`--cc` a bare core.** Clock, reset, memory ports. Prove `step()` and one
   memory transaction.
2. **Differential testing against a golden ISA model** (§3). This is what the
   harness is *for* on a CPU, and it is impossible under `--binary`.
3. **Then** grow the same harness outward to AXI and the card chain, by which
   time the shim questions are settled independently.

Each step is useful on its own, and step 1's code is the same `step()`/`eval()`
loop the card backend needs.

---

## 2. What the harness owns

```cpp
// The whole of it, structurally.
auto ctx = std::make_unique<VerilatedContext>();
auto top = std::make_unique<Vsyscore>(ctx.get());

void step() {                    // one full clock period
    top->clk = 0; top->eval(); ctx->timeInc(HALF);
    top->clk = 1; top->eval(); ctx->timeInc(HALF);
}
```

Everything else is protocol against ports the design already has.

| the harness owns | why |
|---|---|
| **the clock plan** | one clock for a bare core; several later. Nothing needs an MMCM modelled |
| **memory** | a C++ sparse map behind the core's ports, or `src/kohakuaccel/verif/axi_ram.v` if an AXI face is wanted. A map is faster and easier to inspect |
| **program load** | write the image into the map before releasing reset. There is no simulator-specific loader to write |
| **the outside interface** | §4 |

---

## 3. Differential testing — the reason `--cc` matters for a CPU

A testbench compares a result at the end. **A co-simulation compares
architectural state at every retirement**, which is how CPU cores are actually
verified, and it needs a C++ harness because both models have to be stepped in
lockstep by the same loop.

```
   step the RTL one retire  ->  read PC, the register that changed, its value
   step the golden model    ->  same three things
   compare; on mismatch, stop and print both
```

The golden model is an ISA simulator — Spike (`riscv-isa-sim`) is the reference
implementation and exposes exactly this stepping interface. What this buys over
a directed suite:

- **The failing instruction is named**, not the failing test. A directed test
  says "case 7 wrong"; a co-simulation says "instruction at `0x...`, `x14`
  should be `0x...`".
- **The RISC-V test suites become cheap.** They are millions of cycles. At
  xsim's speed that is hours per run; the measured Verilator figures in
  [status.md](status.md) are 100–300× faster on run time, with build paid once.
- **Random program generation becomes viable.** Generate, run both, compare —
  which finds the corners a directed suite was written to miss.

**This is the single strongest argument for `--cc` in this repo**, and it applies
to SysCore before it applies to anything else, because SysCore is the only thing
here that is a general-purpose CPU with an external specification to check
against.

---

## 4. The outside interface — reuse the protocol that exists

`driver/kohakuaccel/daemon/server.py` already marshals `read64`, `write64`,
`read_block`, `write_block` and `program` over JSON lines, and
`DaemonTransport` already speaks it. **Speak that and the Python driver connects
to the simulator with no driver change at all.**

For a bare core, the same loop with a smaller verb set is enough:

| verb | meaning at the core level |
|---|---|
| `write_mem` / `read_mem` | poke the memory map behind the core |
| `step N` | advance N clocks |
| `run_until` | halt, a PC, or a cycle budget |
| `regs` | architectural state, for the comparison in §3 |

That is a debugger interface, and having it is worth as much as the driver seam:
it makes an interactive session against the real RTL possible from Python.

---

## 5. What Verilator cannot check, and it matters here

Stated plainly because SysCore's budget depends on it.

- **It does not infer BRAM or URAM.** A design that simulates perfectly can
  still fall out of block RAM in Vivado — this tree has a measured case where a
  74-bit ROM came back **2,798 LUT and zero BRAM** because a block-RAM port is 72
  bits at its widest. Verilator will never see that.
- **It says nothing about LUT, DSP or Fmax.** Those need Vivado OOC.
- **It does not model Xilinx timing at all.** A passing simulation is a
  statement about function, never about frequency.

So the loop is two-sided and both sides are needed:

```
   Verilator   correctness, fast, iterate freely
   Vivado OOC  resources and timing, slow, gate on it
```

Neither substitutes for the other, and the phase gates in the SysCore plan are
on the Vivado side for exactly this reason.

---

## 6. Prerequisites, honestly

For the **bare core** path: none beyond Verilator itself. The open items in
[status.md](status.md) are all in FIFO shims, the station bus and the NoC — none
of which a standalone core touches.

For the **card** path: the three open items must be settled first. A harness
built on a FIFO that is one word too shallow produces confident wrong answers,
which is what the capacity bug already cost once.

---

## 7. Work items

1. `--cc` mode in `vlt.py`, emitting the C++ class instead of a binary. It
   reuses the same `BENCHES` file list, the same shims-first ordering and the
   same `PE_DIR` handling.
2. A minimal `main()` with `step()` and a memory map, for a bare core.
3. The register/PC observation path for §3 — either hierarchical references
   (`--public-flat-rw`) or a small trace port on the core.
4. Spike (or equivalent) stepped alongside, with the comparison loop.
5. The JSON-line server of §4, so Python can drive it.
6. Only then, grow it to AXI and the card chain.

Items 1–4 are what SysCore phase 1 needs. Items 5–6 are what the card backend
needs, and they build on the same object.
