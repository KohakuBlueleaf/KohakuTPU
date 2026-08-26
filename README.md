# KohakuAccel & KohakuTPU

![KohakuTPU overall architecture](image/README/KohakuTPU-new-arch.png)

**KohakuAccel is an open hardware and software platform for building FPGA
accelerators. KohakuTPU is the AI accelerator built on it. This repository
holds both: the RTL, the compiler, and the driver.**

> Work in progress, built more for fun and for learning than for production. If
> you want to make it work for real, PRs are welcome.

The idea comes from how ML research works. A field moves fast when there is a
standard codebase to fork, like BasicSR or taming-transformers. Accelerator
research has no such codebase. Every new machine rebuilds the same transport,
dispatch and memory plumbing before its first interesting instruction runs.

KohakuAccel is that standard codebase. **To build a new accelerator, you write
a new compute unit and a new instruction set. Everything else is reused
unchanged: the host link, the on-card interconnect, the memory agent, the
mesh, dispatch, completion, the compiler IR, and the driver.** KohakuTPU is
the first machine built this way. It is deliberately not the only one in the
tree.

---

## KohakuAccel: the platform

### What the framework removes

The framework does not remove the design work. It removes **the connection
problem.**

You design the whole compute unit: the datapath, the memories, the pipeline,
and what the instructions mean. The framework has no opinion about any of
that. What the framework fully defines is how you receive and how you send:
the port, the flit format, dispatch, credits, completion, faults, discovery,
memory requests, unit-to-unit transfer, and cross-mesh addressing. That work
is unglamorous. It is where the silent failures live. You do not have to work
it out. [`docs/integrate/`](docs/integrate/README.md) is the surface you build
against, and [`docs/glossary.md`](docs/glossary.md) defines every word on this
page that means something specific here — flit, granule, station, mover, MAG,
system node, kick, completion — in one alphabetical place.

Ownership has four categories, not two ([full table](docs/integrate/what-you-own.md)):

| | examples | may you change it |
|---|---|---|
| **Fixed protocol** | flit format, port handshake, memory encoding, credits | No. If you change it, you are off the framework |
| **Customizable addon** | the read-path transform in the memory agent, L2 staging, the endpoint adapter | Yes. That is what the slot is for |
| **Convention** | how a well-behaved unit is shaped, each marked *forced* or *free* | Follow or don't, but know which is which |
| **Yours** | datapath, memories, instruction semantics, pipeline depth | Entirely |

### What ships

**Hardware, `src/kohakuaccel/`.** The spine that every accelerator reuses:

- `axi/` is the station bus. A line of stations carries host traffic (XDMA and
  JTAG) to every die of a multi-SLR part, with per-station clocks and link
  CDCs.
- `sysnode/` is the system node, one per mesh. The agent (`mag`, the *memory
  access gateway*) turns mesh traffic into DRAM traffic, with streaming fetches
  and multicast; the memory mover walks six-dimensional strided descriptors and
  carries a swappable transform slot on its read return; and the interlink joins
  one mesh to the next.

  **A control processor is structural, not an option.** There is no parameter
  that removes it: the node cannot be built without a processor, and the mover
  is that processor's execution unit rather than a peer with a command window.
  What *is* a parameter is `CPU_RV64` (default 0), and it chooses **which**
  processor — the RV32 complex, which answers on the mesh at `(0,0)`, or the
  RV64 complex, which has no mesh presence and is loaded through a host window.
- `noc/` is the mesh: XY routers, the orchestrator, the L2 endpoint adapter,
  and `noc_cu_base`. Every compute unit wraps `noc_cu_base`. It handles
  framing, discovery, completion, and credits, so a unit conforms by
  construction.
- `common/` and `verif/` hold fifos, CDC primitives, reset entry, AXI RAM
  models, and `kh_port_check`. The checker makes the port conventions
  executable instead of prose.
- `pe/rv32/` is the RV32I controller PE, a compute unit that happens to be a
  processor. `SIMD_EN` names a wide datapath it does not own — a slot, 0 by
  default, filled by [KohakuMPE](docs/projects/kohakumpe/README.md).
- `pe/rv64-sys/` is the RV64IMA + Zicsr system core: an in-order pipeline with a
  branch predictor, an Sv39 page-table walker, a write-back L1 and a
  machine-mode trap and interrupt model. It ships in two wrappers — a mesh
  compute unit (`rv64_sys_pe`) and a shell-less core that fuses to MAG
  (`rv64_syscore`). [`docs/arch/cpu/`](docs/arch/cpu/README.md) says why the
  framework carries two processors, how to choose, and what the RV64 branch does
  not do yet.

**The build list is `scripts/py/xsim.py`**, and only that. Each library also
carries a `FILES.f` inventory, generated from the tree by
`scripts/py/filesf.py` and checked in the standard suite — but **nothing builds
from one**, so adding a file to a manifest by hand has no effect on any build.

**Contracts, [`docs/spec/`](docs/spec/README.md).** Normative pages, one per
surface: flit format, compute-unit port, memory protocol, control registers,
instruction encoding, and the transform slot. Each page gives every field and
every MUST. A known-divergences section records where reality and declaration
differ.

**Extension points, `src/templates/`.** Each template is a working skeleton
**with its self-checking bench**, because a template without a bench is a
trap:

| template | the slot |
|---|---|
| `cu/` | a compute unit on `noc_cu_base`: accept and retire, discovery, disposal, backpressure, all demonstrated |
| `transform/` | the transform slot on the memory mover's read return, as an identity occupant |
| `adapter/` | the endpoint-link adapter, which observes or intercepts between a router and its endpoint |

**The generator, `scripts/py/gen_mesh.py`.** It emits a mesh top from a text
picture of the mesh. A project registers its own unit tokens with `--tokens`,
a Python file that maps a token to instance text. A new accelerator never
edits the generator. The `--split-reset` option plants a reset synchronizer
at each clock domain entry, so only the raw reset ever crosses domains.

**Software, `driver/kohakuaccel/` and `compiler/kohakuaccel/`.** The same
split, and it is enforced: the framework imports nothing from any project, and
a test fails the moment it does. The driver owns transports, dispatch,
completion, and device discovery. The compiler ships a three-level IR (graph,
schedule, program). The middle level does placement, packing, coalescing, and
completion accounting. It is machine-determined and identical for every
workload. A declarative ISA toolkit turns a field table into an encoder, a
decoder, a validator, and a disassembler. See [`compiler/`](compiler/README.md).

### The proof: `examples/saxpy`

Claims about frameworks are cheap. The platform carries an acceptance test: a
second, unrelated accelerator built from the framework alone.

- **Software half** (`driver/examples/saxpy/`). One instruction, `y = a*x + y`
  over float32. About 60 lines of ISA and unit model, registered as CU_TYPE
  `'SX'`.
- **Hardware half** (`src/examples/saxpy/`). `saxpy_cu.v` is built from the CU
  template. It decodes the same ISA field for field, and does plain reads and
  a burst write against the real memory agent. Its bench runs with the
  convention checker mounted: `python scripts/py/xsim.py saxpy_cu`.
- **Composed.** A three-line token table and a map picture generate a real
  mesh: a router, the memory agent, the orchestrator, and two saxpy units.
  The mesh bench drives it the way a host drives the card. It uploads
  operands over AXI, stages and dispatches the program through the
  orchestrator, observes completion in the status mirror, and reads the
  results back bit-exact: `python scripts/py/xsim.py saxpy_mesh`.

Both print a verdict and a check count. When they are green, "a new accelerator
is a new compute unit plus a new ISA" is demonstrated rather than claimed.

### Building your own

For a project named `NAME`, these five files are yours and nothing else is:

```
src/examples/NAME/NAME_cu.v         your unit: datapath + noc_cu_base wrap
                  tokens_NAME.py    token -> instance text, for gen_mesh
                  NAME.map          the mesh picture
driver/examples/NAME/isa.py         how a shape becomes instruction words
                     unit.py        type registration + simulation model
```

`saxpy` is that shape filled in, and it is the only example in the tree that
runs end to end: `src/examples/saxpy/` and `driver/examples/saxpy/`, checked by
the `saxpy_cu` and `saxpy_mesh` benches.

Start from `docs/integrate/README.md`. Copy `src/templates/cu/`, which is a
conforming unit with a bench of its own. Keep `kh_port_check` mounted in your
bench from day one — it is what catches a protocol violation at the port
instead of six modules downstream.

---

## KohakuTPU: the machine

The flagship project: matrix and vector units on the KohakuAccel mesh, four
meshes on one device, programmed from Python.

```python
from kohakuaccel.lang import dims, loop, units
from kohakutpu.lang import kernel

from kohakutpu import lang as L

M, K, N = dims("M, K, N")
LOG2E = 1.4426950408889634


@kernel
def linear_silu(
    x=L.In(..., M, K), w=L.In(N, K), y=L.Out(..., M, N), *, gm=8, gn=8, nk=2
):
    """silu(x @ w.T), with the activation fused onto the accumulator."""
    with units(x.tiles(gm), w.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            acc += x[i, k] @ w[j, k]
        y[i, j] <<= acc * L.recip(L.exp2(acc * -LOG2E) + 1.0)
```

The last line expresses the epilogue as part of the matmul. The fused path is
built and simulated but **not yet proven on silicon**. Today's scheduler still
stages the activation through DRAM between the two units. See
[`fused-epilogue.md`](docs/projects/kohakutpu/fused-epilogue.md). Write the
kernel, call it like a function, and the compiler places it:

```python
from kohakutpu import api as ktpu

y = linear_silu(ktpu.tensor(x), ktpu.tensor(w))  # no launcher, no addresses
print(y.numpy())  # the only line that crosses the link
```

### Status

**Hardware, implemented.** Synthesised, implemented, and running on a real
FPGA: the matrix clusters, the vector cores, the NoC mesh and its routers, the
system node with its mover and transform slot, the interlink that joins four
meshes, and **40-bit addressing** with one global space across all four
([`address-map.md`](docs/address-map.md)).

**Hardware, synthesised but not yet on silicon.** These are verified in
simulation against real instruction streams, and carried through synthesis in
the four-mesh design, but they have not run on hardware yet:

- **L2.** Staging in the memory agent, reached by address, and an adapter at
  the NoC endpoint, reached by instruction. Either is optional, and
  `gen_mesh.py` selects them independently. The agent's banks are split rather
  than one array, and how the banking and the pipelining were arrived at is a
  measured table in [`results.md`](docs/projects/kohakutpu/results.md) — read it
  there, with the conditions each row was taken under, rather than as a
  frequency quoted here.
- **Per-mesh, per-component clock control.** One generator per mesh. The
  matrix core, the vector core, and the fabric sit on separate outputs.
- **Double-pumped matrix core.** The DSPs take a 2x clock. A `BUFGCE_DIV`
  derives the fabric's 1x from it, so the two are edge-aligned by
  construction.
- **Per-domain reset architecture.** Every clock domain releases its reset
  locally through a domain-entry synchronizer. Only the raw reset crosses
  domains.

**Hardware, built but not finished: the RV64 system processor.** Every mesh has
a control processor, and `CPU_RV64` chooses which. It defaults to 0, so what
ships is the RV32 complex. The RV64 branch elaborates, simulates and runs
programs — core, mover, transform slot, memory path, host window and console are
all connected — but in the node configuration the hub's compute-unit port is
tied off in both directions, the interlink doorbell is unconnected, and
`irq_summary` and `pe_status` are tied off
(`src/kohakuaccel/sysnode/sysnode.v`). It cannot yet dispatch an instruction to
a compute unit or consume the completion that comes back, which is the job the
configuration exists for. See
[`docs/arch/cpu/rv64-sys/`](docs/arch/cpu/rv64-sys/README.md).

**Software: a working driver and compiler stack.** Kernels compile to cluster
*and* vector programs. Flash attention runs. Tinygrad works as an optional
frontend into the same kernel library.

Every measured number, with the conditions it was taken under, is in
[`results.md`](docs/projects/kohakutpu/results.md). Unless a row there says
otherwise, a figure is `xcvu13p-fhgb2104-2L-e` under Vivado 2024.2,
**out-of-context synthesis only** — no place, no route. **No frequency anywhere
in this repository is a closed-timing result**, and synthesis slack is
optimistic, so read one as an upper bound on the logic rather than as a speed
the assembled machine runs at.

### What makes it interesting

**A number format built for the DSP.** Elements are int7 with an **E5M3**
scale shared by a block of 32. This is a microscaling format, but the scale is
deliberately *not* a power of two. An E8M0 scale wastes up to a full bit of
significand, depending on where a block's peak falls in its binade. Three
mantissa bits put that peak at 63 every time. The field is still 8 bits, and the
p50 relative error drops from 0.54% to 0.38% — E8M0 against E5M3, measured per
element on correlated operands,
[`results.md`](docs/projects/kohakutpu/results.md) §6.1.

**MACs that cost zero LUTs.** Four tensor CUs chain through the DSP48E2's
`PCOUT -> PCIN` cascade. The multiply *and* the whole K=32 reduction happen
inside the DSPs. The fabric holds control, not arithmetic.

**Two mesh ports per cluster, not five.** The DSP chain eats eight operand
words per cycle, and a port delivers one, so more ports never close that gap.
Holding a large output tile resident does close it. A `Gm x Gn` block needs
`4(Gm+Gn)/(Gm*Gn)` words per cycle, which is 0.375 at 16x32. This is an
arithmetic property, not a concession.

**A compiler that knows the machine has no threads.** Six levels, and only
adjacent levels may appear in one piece of code. A unit is *programmed*, not
commanded. There is no `program_id` and no `__syncthreads`. The grid places
independent programs.

### Future work

- **Vector ISA** improvements.
- **Driver** improvements.

---

## Quickstart

Python 3.13+, and numpy is the only hard dependency.

```bash
pip install -e .               # the whole tree: compiler, driver, kernels
pip install -e ".[tinygrad]"   # optional, adds the tinygrad frontend
pytest                         # no hardware needed
```

Nothing reaches the card unless you ask for it. Everything runs against unit
models by default, and `--device card` is a decision rather than a fallback.

```bash
python examples/kohakutpu/01_tensors.py       # learn by reading the code
python demos/kohakutpu/flash_attention.py     # learn by reading the output
python -m kohakutpu.viz                       # a kernel, at every level
```

For the RTL there are two simulators and a synthesiser, and they answer different
questions. **Verilator is the inner loop**: `--lint-only` reaches a missing
module, a port mismatch or a bad parameter in seconds, and a built model runs a
long program far faster than xsim can. **xsim is the gate of record** — it is
Vivado's, it propagates X, and the mesh needs `-L xpm`, so iverilog will not do.
**Vivado owns every resource and frequency number**; neither simulator sees
whether an array actually became block RAM.

Verilator is installed **in WSL**. The conda and MSYS2 packages are 4.x, which
has no `--timing`, and every bench here generates its clock with `always #N` —
4.x drops that silently. [`sim/verilator/docs/setup.md`](sim/verilator/docs/setup.md)
has the install and the reasoning; `sim/verilator/` also holds the XPM shims,
the C++ harnesses and the cross-check benches.

Benches run against both a behavioural DSP and the real `DSP48E2`, so a failure
is attributable to one or the other.

```bash
python scripts/py/vlt.py cluster_node --lint-only   # seconds, no C++ build
python scripts/py/xsim.py saxpy_mesh                # the platform acceptance test
python scripts/py/check.py fast                     # no Vivado; 11 s at -j4
python scripts/py/check.py full -j 6                # every bench
```

`check.py`'s own header names each tier and the cost measured for it at `-j4`;
that header is the figure to trust, not one copied into a README.
[`docs/workflow/simulate.md`](docs/workflow/simulate.md) covers which simulator
answers which question, the four levels of test, and what a passing suite does
and does not mean.

`full --counts <file>` records the numbers each check printed and
`--counts-baseline <file>` fails the run when any of them moved. That is what a
refactor or a reformat has to clear: a green suite does not say a count held.

## Documentation

**[`docs/`](docs/README.md)** is written to be read. Every page says what it
does, what it costs, and where it stops.

| | |
|---|---|
| [the glossary](docs/glossary.md) | every project-specific term, what it is, where it sits, and which page covers it properly. Start here if a word is unfamiliar |
| [the framework](docs/integrate/README.md) | what you own, what is fixed, and how to put your own compute unit on it |
| [the machine](docs/projects/kohakutpu/README.md) | KohakuTPU top to bottom, in the order the decisions were forced |
| [writing kernels](docs/projects/kohakutpu/writing-kernels.md) | how much of the schedule to say, and what a tiling actually means |
| [the ISA](docs/projects/kohakutpu/isa.md) | the most accurate description of what it executes |
| [architecture](docs/arch/README.md) · [specs](docs/spec/README.md) · [workflow](docs/workflow/README.md) | the mesh and memory agent, the normative contracts, and the build/measure/bringup practice |

## Repository

```
   src/kohakuaccel/   the hardware framework: station bus, system node, NoC,
                      and the two CPU processing elements -- pe/rv32/ and
                      pe/rv64-sys/
   src/kohakutpu/     this machine: matmul, vector, transform, generated tops
   src/kohakumpe/     a second project: the SIMT processing element, and the
                      SIMD unit that fills the framework's SIMD_EN slot
   src/templates/     the extension points, each with its bench
   src/examples/      saxpy, the platform acceptance test (RTL half)
   src/reference/     retained knowledge: arithmetic cores, PoCs. attic/ holds
                      deletion candidates, and nothing is removed unreviewed
   compiler/          kernels, schedules and machine code  (kohakuaccel + kohakutpu)
   driver/            transports, dispatch, completion     (kohakuaccel + kohakutpu)
   examples/          read the code           demos/    read the output
   tests/             Verilog benches         scripts/  build, simulate, measure
```

`src/kohakutpu/` is Verilog. `compiler/kohakutpu/` and `driver/kohakutpu/` are
Python. The names collide, so when a comment names a path:
`src/kohakutpu/vector/vec_alu.v` is hardware, and
`compiler/kohakutpu/hw/vector.py` is the model of it.

## License

All code in this repository is released under the **Kohaku Code License
2.0**: the RTL, the compiler, the driver, the documentation, and every other
resource in the tree. The license is open access with share-alike, with
commercial thresholds and a tape-out authorization rule for the hardware
design. Read [LICENSE](LICENSE) for the exact terms, and contact
kohaku@kblueleaf.net for custom licensing or exemptions.
