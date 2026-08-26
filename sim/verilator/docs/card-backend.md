# Verilator as a real backend for the software stack

The use case, stated plainly: **run the driver, the compiler output, and real
programs against the actual RTL, on a laptop, with no card in the loop.**

Not "simulate a bench". Simulate a *card*. Same driver code, same program image,
same doorbells, same status polls — a different backend underneath.

## Why this is cheap here

Because the seam already exists and is two methods wide.

`driver/kohakuaccel/transport/base.py` defines the entire hardware dependency:

```python
class Transport(abc.ABC):
    @abc.abstractmethod
    def write64(self, addr: int, data: int) -> None: ...
    @abc.abstractmethod
    def read64(self, addr: int) -> int: ...
```

`write_block` / `read_block` default to loops over those two. Its own docstring
already names this exact goal:

> *"a driver written against `write64`/`read64` runs unchanged against an
> in-process model, a JTAG-AXI master, or PCIe XDMA."*

And it is already proven in practice, not just intended.
`driver/kohakuaccel/daemon/__main__.py` takes `--backend {jtag,model}`, where
`model_transport()` returns a `MemoryTransport` — a dict-backed fake card that
the whole `Card`/`Device` stack drives without knowing the difference. Existing
backends: `jtag.py`, `xdma.py`, `memory.py`, plus `rebase.py` and `split.py` as
decorators.

So a Verilated card is **a third backend**, not a refactor.

## Does Verilator do this natively? No.

Worth being blunt, because it decides the design:

| Verilator mode | What you get | Fit |
|---|---|---|
| `--binary` | Standalone executable running a Verilog testbench | What `vlt.py` uses today. No external communication. |
| `--cc` | **A C++ class.** You write `main()`, own `eval()` and the clock. | **This is the path.** |
| DPI-C | Call C from Verilog, export Verilog tasks to C | Useful glue, not a transport. |

There is no built-in socket, RPC, or "online" interface. The C++ harness is not
optional — but it is small, because the protocol it must speak already exists.

## The shape

```
  Python driver  (driver/kohakuaccel, driver/kohakutpu)   UNCHANGED
        |
        |  Transport.write64 / read64
        v
  VerilatedTransport  ──socket──>  C++ harness
                                     |
                                     |  drives S_AXI_CTRL + S_AXI_MEM
                                     v
                                   sb_line4 (station bus)
                                     |
                                   sysnode  (MAG + control PE + mover)
                                     |
                                   mesh: routers, matmul, vector
                                     |
                                   axi_ram.v  (DRAM model)
```

The dashed part is the only new code:

```cpp
// One AXI manager, one clock, one request loop.
top->s_axi_ctrl_awaddr = addr;  top->s_axi_ctrl_awvalid = 1;
while (!top->s_axi_ctrl_awready) step();      // step() = toggle clk, eval()
```

`step()` is `clk ^= 1; top->eval(); ctx->timeInc(...)`. Everything else is
ordinary AXI handshaking against ports the design already has.

## What the harness must own

1. **A clock plan.** The ship has six clocks (`docs` says `axi_aclk` carries the
   AXI/AXIS ports). The harness advances each by its own period; nothing here
   needs the MMCM, which is why the wizards do not have to be modelled.
2. **The two host windows.** `S_AXI_CTRL` (32-bit control, through
   `axi_up32to64`) and `S_AXI_MEM` (wide upload). These are the same two windows
   JTAG drives, so the driver's address map needs no change.
3. **DRAM.** `src/kohakuaccel/verif/axi_ram.v` already exists and every mesh
   bench uses it. Start there; swap for a C++ sparse map if memory or speed bite.
4. **A transport protocol.** `driver/kohakuaccel/daemon/server.py` already
   marshals `read64`/`write64`/`read_block`/`write_block`/`program` over JSON
   lines. Speak that and `DaemonTransport` connects to the simulator as-is.

## Program loading is not a new problem

A program reaches the card by `write_block` into the staging window followed by a
doorbell. That is what `ctrlpe_mesh_tb` does in Verilog today — stage granules,
dispatch, the processor runs. Through the harness it is the same driver call it
is on silicon. **There is no simulator-specific loader to write.**

## Why it is worth it

- **A driver bug and an RTL bug stop looking alike.** Today a wrong answer on the
  card could be either; here the whole state is visible and reproducible.
- **No card contention.** JTAG is serialised and one operation at a time; a
  simulated card is per-developer and per-CI-job.
- **No BSOD risk.** XDMA work currently risks the host. A simulated card cannot.
- **Programs before silicon.** Compiler output can be executed against the real
  RTL while the bitstream is still building.

## Order of work

1. `--cc` the mesh top and stand up `main()` with a clock and an AXI manager.
   Prove one `write64`/`read64` round-trip against `S_AXI_CTRL`.
2. Add the JSON-line loop from `daemon/server.py`; point `DaemonTransport` at it.
3. Run `driver/examples/enumerate_card.py` unchanged. That is the acceptance
   test: if enumeration works, the seam is real.
4. Then a real program end to end, mirroring `ctrlpe_mesh`.

**Prerequisite, and it is a real one:** the shims must be trustworthy first. A
harness built on a FIFO that is one word too shallow produces confident, wrong
answers — see [shims.md](shims.md) for what that already cost, and
[status.md](status.md) for what is still open.
