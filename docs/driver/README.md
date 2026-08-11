# The driver system

The compiler and runtime for KohakuTPU. What turns a tensor program into flits,
uploads them, kicks the machine and reads the answer back.

```
docs/driver/
├── README.md        this file: the stack, the layers, and the decisions
├── ir.md            the three IR levels, in detail
├── scheduling.md    how level 2 decides, and what it may not decide
├── hardware-api.md  the low-level surface: the transport contract, the four
│                    backends, and where topology lives
├── simulators.md    what runs a schedule without hardware, and what each proves
├── dsl.md           the authoring language and how it traces
├── caveats.md       everything that has bitten us, and why none of it is
│                    visible from the code that contains it
├── compiler-gaps.md what ktpu.codegen cannot emit, and what to build first
└── tinygrad.md      the automatic frontend, and when it lands
```

**Read [`caveats.md`](caveats.md) before trusting a number off the card**, and
[`compiler-gaps.md`](compiler-gaps.md) before assuming the compiler path can run
something. Both are current as of 2026-08-11.

The kernel ABI — how a kernel is staged, entered and retires — never became a
page of its own; it is [`../isa/kernel.md`](../isa/kernel.md) and
[`../isa/agent.md`](../isa/agent.md), written against the RTL.

---

## 1. Where this is going, and why there is a new one

There used to be a `driver/` tree beside `src/`. It has been **retired**: what
was still needed moved into `src/ktpu/hw/` — formats, the MXFP7 model, the
device register map, the flit encoder, the operand packer, the live xsim
session — and the rest went with it. `scripts/py/` took the entry points
(`server.py`, `repl.py`, `run_matmul.py`, `run_quant_check.py`), and
`src/ktpu/viz/` the visualiser page.

`ktpu.hw` is not legacy code kept out of sentiment: it is the RTL-facing half of
the stack and the thing every encoder is checked against. `tests/ktpu/
integration/test_machine_code.py` compares ktpu's own flits to `ktpu.hw.bench`'s
byte for byte, and `ktpu.hw.sim.Session` is what actually runs xsim.

Its *planner* was replaced rather than extended, because of what it was: a very
good *GEMM tiler* that grew a control-program generator, an operand packer, a
cost model and a visualiser around itself. Every one of those is sound — which
is why they survived the move. What that planner did
not have is **a representation** — the tiling decision, the grid, the memory
placement and the instruction encoding all happen in the same pass over the same
Python objects, so there is nowhere to stand between "what the user asked for"
and "these bytes". That was fine while the only thing to compile was a GEMM.

Two things changed:

- the **vector core** ([`../compute/vector-core.md`](../compute/vector-core.md))
  is programmable, so there is now something to *compile* rather than
  parameterise;
- the dispatch defect in `.plan/measurements/dispatch-n-only.md` — work split on
  N alone, a shape's parallelism silently vanishing — was **not findable by
  reading the code**, because the decision was distributed across three files.
  A scheduling decision you cannot print is a scheduling decision you cannot
  review.

The new stack exists to make each of those a value you can hold.

---

## 2. The layers

```
   user program  (own DSL, or tinygrad, or hand-built graph)
        │
        ▼
   ┌─────────────────────────────────────────────────────────┐
   │  LEVEL 1   GRAPH IR      tensors, ops, value semantics   │
   │            shapes, dtypes, no machine in sight           │
   └─────────────────────────────────────────────────────────┘
        │   fuse, choose engine, tile, place, pick the grid
        ▼
   ┌─────────────────────────────────────────────────────────┐
   │  LEVEL 2   SCHEDULE IR   tiles, grid, memory space,      │
   │            engine assignment, loop order, residency      │
   └─────────────────────────────────────────────────────────┘
        │   encode
        ▼
   ┌─────────────────────────────────────────────────────────┐
   │  LEVEL 3   TARGET IR     concrete instructions and flits │
   │            addresses, opcodes, rounds                     │
   └─────────────────────────────────────────────────────────┘
        │
        ▼
   runtime  →  hardware API  →  AXI
```

**Every interesting decision lives at level 2.** Which engine runs an op,
whether two ops fuse, what the tile is, how the grid is shaped, what stays
resident in L1 — all of it. Level 1 is what the user meant; level 3 is bytes.
The middle is the compiler, and it is the only level where a human should have
to argue about performance.

That is also the answer to "why an IR at all": the dispatch defect was a level-2
decision with no level-2 to live in.

---

## 3. Why our own IR, and not MLIR

Recorded here because it is the decision most likely to be revisited, and it
should be revisited on evidence rather than fashion.

**Nothing upstream can model this machine.** Not as a complaint — as a fact
about how specific it is:

| | |
|---|---|
| dtypes | `int7 + E5M3` block scale, `E8M15`, `S1E7M16` accumulator — none exist in any external type system |
| the matmul cluster | a **macro-op**, not a loop nest: `FILL / GEMM / DRAIN` |
| tile choice | arithmetic intensity *discounted by padding*, against L1 entry counts and a 512-sub-tile output budget |
| rounds | `STAGE_FLITS` admits passes to a round; a round is a host round trip |

In MLIR, Triton and tinygrad alike, **the matmul cluster is an escape hatch** —
a custom op that pattern matching targets. So adopting any of them buys the
*vector* path and the *fusion* layer, and never the matmul path. Worth knowing
before paying for it.

**MLIR** is the right answer for a product and the wrong one for now: it is an
LLVM build dependency in a Python + Verilog + Vivado repo, its payoff is
arbitrary PyTorch ingestion via torch-mlir, and that is not what is blocking.
The dtypes would ride as `i24` plus attributes, losing the verification that was
the reason to come.

**Triton** is a superset of that cost, not an alternative to it: its third-party
backend interface is real, but backends are MLIR passes.

**tinygrad** is the one to wire up, and §6 says when.

### What we keep open

The IR is **serialisable**, and its level-1 op set stays close enough to
`linalg` / `vector` that a bridge is a translation rather than a redesign.
Revisit when arbitrary PyTorch ingestion is the blocker rather than a wish.

---

## 4. Conventions

Python, following KohakuTerrarium. **`src/` is the whole project's source root**
— it already holds the Verilog packages, and the compiler goes in beside them
rather than in a directory of its own:

```
   pyproject.toml            at the root
   src/kohakunoc/  …         Verilog, unchanged
   src/ktpu/                 the compiler and runtime
   tests/ktpu/{unit,integration}/    Python, beside the Verilog benches
```

- **`ktpu`, not `kohakutpu`** — `src/kohakutpu/` is the VERILOG compute package
  and lives on. Two importable things of one name is a trap, and a path in a
  comment has to be readable without checking which one it means. It makes a
  good CLI name too.
- ≤ 600 lines per file, one responsibility per module.
- No imports inside functions (optional deps and genuine lazy-init excepted).
- Import order: **whatever `ruff` produces** — stdlib, third-party, `ktpu`, then
  plain alphabetical. Terrarium's convention says shorter paths first; ruff's
  I001 disagrees and rewrites it, and the tool that runs in CI wins. Do not
  hand-order imports against the formatter.
- Modern type hints — `list`, `dict`, `X | None`. `match` over nested `elif`.
- No `print` in library code.
- Tests in tiers: `unit/` one module to one test-class; `integration/` one
  package folder to one workflow test.

**If a compiler pass ever needs to be fast enough to matter, it goes to Rust +
PyO3**, following KohakuVault's layout — not into a faster Python. Nothing needs
that yet and speculating about it now would cost more than it saves.

---

## 5. Dependencies

`numpy` for the numeric model, and nothing else in the core. The point of a pure
Python stack is that it stays inspectable; a dependency has to earn its place by
removing more code than it adds.

The visualiser talks to it over HTTP, as today.

---

## 6. Frontends

Three, in the order they should be built.

1. **Hand-built graph** — construct level-1 IR directly in Python. Always
   available, is what the tests use, and needs no frontend at all.
2. **Our own DSL** ([`dsl.md`](dsl.md)) — traced by letting special value
   objects flow through an ordinary Python function, so there is no parser.
   Runtime-dependent control flow is **rejected**, compile-time control flow is
   ordinary Python and free. This is the path for hand-written kernels, which is
   how peak performance gets reached on the shapes that matter.
3. **tinygrad** — the automatic path, and the fastest wire into the community.
   Pure Python, a small backend surface, and UOps that are already close to the
   vector ISA. Its scheduler fuses, and fusion is the whole game here: an
   unfused elementwise kernel runs the ALUs at 33%, a fused one at 100%
   ([`../compute/vector-bringup.md`](../compute/vector-bringup.md) §2.1).

**Deferred deliberately: the tinygrad-vs-MLIR commitment.** There is no vector
core RTL and no measured cost model yet, so neither choice can be argued with
numbers. Build levels 1–3 and the DSL, which are needed under every option, and
decide when there is something to compile *for*.

---

## 7. The IR interpreter

Once there is an IR there is somewhere to run it that is not the hardware
simulator. `src/ktpu/interp/` executes a schedule directly in Python: a mesh, a
RAM, and the two engines, modelled in software.

It answers two questions xsim cannot answer cheaply, and one it cannot answer at
all:

| question | today | with the interpreter |
|---|---|---|
| does the program compute the right numbers? | full RTL run | seconds, in numpy |
| where do the cycles go? | full RTL run | the cost model, per band |
| is this schedule better than that one? | **cannot ask** — no RTL for the vector core | compare two schedules |

Two levels, and keeping them separate is the point:

- **Functional.** Execute the level-2 schedule against arrays. Engines apply
  their real arithmetic — MXFP7 quantisation, ACC24 accumulation, E8M15 in the
  vector core — so precision is modelled, not idealised. This is what makes a
  kernel checkable before any RTL exists.
- **Timed.** Move flits through a mesh model with the NoC's link contract and
  MAG's port count, charging the cost model per band. Not cycle-accurate and
  not pretending to be; it answers "which band is the limiter" and "is this
  bandwidth- or compute-bound".

xsim stays the authority. The interpreter is what makes it rare: a functional
disagreement is a compiler bug, and only a *timing* disagreement needs the RTL.
When the vector core exists, the same relationship holds that `mxfp7.py` has to
the quantiser today — the software model is the golden reference and the bench
checks against it.

**Not started.** Wanted after level 3 lands, because it interprets a schedule
and there has to be one.

## 8. Examples

`examples/` — runnable, and the first thing to read after this document.

```
   01_trace_a_kernel.py    DSL: tracing, the control-flow guard, shape errors
   02_tile_and_grid.py     tile choice, the 3D grid, split-K verification
```

## 9. Status

| | |
|---|---|
| this document | design |
| levels 1–3 | being built |
| DSL | design |
| tinygrad bridge | not started, deliberately |
| `src/ktpu/hw/` | the RTL-facing half: formats, MXFP7, device map, flit encoder, operand packer, xsim session |
