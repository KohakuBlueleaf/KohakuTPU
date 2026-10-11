---
title: software
summary: The host and node software of KohakuAccel and its projects, one uv workspace with one member per component.
tags:
  - software
---

# software

One uv workspace (`pyproject.toml` here), one member per component. A module is
named `<side>.<component>.*`: the side is `kohakuaccel` (the framework) or a
project (`kohakutpu`, `toyaccel`); `kohakuaccel` and `kohakutpu` are namespace
packages spread across the members.

    uv sync                                     # every member, editable
    uv run pytest                               # every component's tests
    uv run python -m toyaccel.application.run   # the template, end to end

## The components

| member | modules | what it is |
|---|---|---|
| `language/` | `kohakutpu.language` | the `.ktpu` text (`text/`), the L3 and L2 readers and verifiers (`l3/`, `l2/`), the L1 op names (`l1/`), the compilers (`lower/`: L3 -> L2 -> L1; `opt/`: retile, schedule), `pipeline.py`, the numerics (`numerics/mxfp7.py`), the kernels (`kernels/`) |
| `compiler/` | `kohakuaccel.compiler` | the ISA toolkit (`isa.py`: `Field`, `InstFormat`, `InstSet`), the L1 `Program` (`program.py`), the work package (`package/`: format, builder, mover moves, the dispatch-engine lowering), the machine description (`machine.py`) |
| | `kohakutpu.compiler` | the ISA field tables (`isa/`), the encoders (`encode/`), the memory layouts (`layout/`), the L1 body and image emitters (`emit/`), resident IMEM (`imem.py`), `build.py` (a kernel to a package) |
| `driver/` | `kohakuaccel.driver` | transports (`transport/`), flits, registers, staging and discovery (`device/`), the unit registry (`unit/`), node boot, heaps and queues (`node/`), the card daemon (`daemon/`), the kick-list loader (`runtime/`) |
| | `kohakutpu.driver` | the card's clocks (`clock/`), host entry points (`host/`), the unit types (`units/`), the daemon CLI (`daemon/`) |
| | `tools/` | card, clock and mover scripts against a card or a card model |
| `simulation/` | `kohakuaccel.simulation` | the package interpreter (`package.py`), `SimMachine` and its mailbox (`machine/`) |
| | `kohakutpu.simulation` | the Verilator card and the V2 replay bench (`verilator/`) |
| `firmware/` | C | the node firmware (`kohakuaccel/`, `kohakutpu/`), `build.py`, its host tools (`tools/`) |
| `application/` | `kohakutpu.application` | a skeleton (no tensor API, no DSL), and the kernel tools (`tools.run`, `tools.core`) |
| `template/` | `toyaccel.*` | the smallest project, one module per component ([template/README.md](template/README.md)) |

## The import rules

`software/tests/test_imports.py` reads every module's imports and fails on a breach:

| component | may import |
|---|---|
| language | language |
| compiler | compiler, language |
| driver | driver |
| simulation | simulation, driver, compiler, language |
| application | every component |

The framework imports no project, a project imports no other project, and
every import sits at module level. Tests that tie two components together live
in `tests/` (`test_package_mover.py`: the compiler's mover encoding against the
driver's).

## Style

`ruff check .` and `black --check .`, from this directory: the workspace has
its own config, and the repository root's excludes it.
