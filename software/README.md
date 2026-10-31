---
title: software
summary: Rust compilation and Python tracing, runtime and drivers in one workspace.
tags:
  - software
---

# Software

Two owners, each split by component: `kohakuaccel/` is the framework,
`kohakutpu/` the TPU, `template/` the smallest project (`toyaccel`). Python
members form one uv workspace; Rust crates form one Cargo workspace, here.

```text
software/
├── README.md  pyproject.toml  Cargo.toml
├── kohakuaccel/      compiler, driver, simulation, firmware, os
├── kohakutpu/        language, compiler, driver, simulation, firmware, os, application
├── template/         toyaccel
└── tests/            import rules
```

```text
uv sync
uv run python ../scripts/py/build_rs.py prod
```

A card or Verilator model is not needed for offline compilation.

## Components

| Member | Modules | Responsibility |
|---|---|---|
| `kohakuaccel/compiler/` | `kohakuaccel.compiler` | Generic instruction-layout toolkit |
| | crates `ka-machine`, `ka-isa`, `ka-package`, `ka-debug`, `ka-cache` | Machine facts, instruction fields, packages, debug records and caches every project shares |
| `kohakuaccel/driver/` | `kohakuaccel.driver` | Transports, node queues, discovery, staging, the daemon |
| `kohakuaccel/simulation/` | `kohakuaccel.simulation` | Callback-driven package interpreter and transport doubles; computes no tensor values |
| `kohakuaccel/firmware/` | C | Node firmware: queue service, package interpreter, unit enumeration |
| `kohakuaccel/os/` | C and Python tools | KohakuAccelOS |
| `kohakutpu/language/` | `kohakutpu.language` | Python DSL tracing, `.ktpu` syntax, machine schema reader, host numerics, the process environment (`config`) |
| `kohakutpu/compiler/` | `kohakutpu.compiler._rs` | Native extension from crate `ktpu-py`, the one PyO3 crate |
| | `kohakutpu.stack` | The only importers of `_rs`: compile, buffer and specialisation adapters |
| | crates `ktpu-machine`, `ktpu-isa`, `ktpu-l0`, `ktpu-l1`, `ktpu-l2`, `ktpu-graph`, `ktpu-kernels`, `ktpu-verify`, `ktpu` | TPU machine facts, encodings, lowering, scheduling, kernel generators, static verification |
| `kohakutpu/driver/` | `kohakutpu.driver` | Clocks, unit facts, host entry points |
| `kohakutpu/simulation/` | `kohakutpu.simulation` | Machine-bound Rust package reader and Verilator interfaces |
| `kohakutpu/firmware/` | C and Python tools | Unit firmware, build configuration, offline package producers |
| `kohakutpu/os/` | Python tools | The TPU's OS runs (`run_card.py`, `ostpu/`) |
| `kohakutpu/application/` | `kohakutpu.application` | Tensor API, tinygrad integration, the planner, native kernels, runtime, tools |
| `template/` | `toyaccel.*` | Small framework project ([template](template/README.md)) |

Python is the only planner; Rust generates kernels and compiles. The runtime's
machine view comes from `_rs.machine_view`; addresses, staging capacity,
buffers, IMEM accounting and packages use the same machine schema as the Rust
compiler. Compiled tensor values are checked on a card or Verilated card, never
a tensor behaviour model.

## Import rules

`tests/test_imports.py` checks imports at module level. Framework modules import
no project, and a project imports no other project. The callback interpreter
receives its package reader from its caller; the framework does not import the
TPU extension. Framework crates never depend on project crates.

| Component | May import |
|---|---|
| language | language |
| compiler namespace | compiler namespace |
| stack | stack, native compiler binding, language, runtime package adapters |
| driver | driver, machine-schema language reader |
| simulation | simulation, driver, stack, language |
| application | every component |

## Style

`KTPU_DUMP` writes compiled L2/L1 text. Rust owns stage caches; Python holds
bounded in-process specialisation and package memos.

Run `ruff check .` and `black --check .` from this directory. Rust uses
`cargo fmt --check` and crate-scoped clippy.
