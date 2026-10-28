---
title: software
summary: Rust compilation and Python tracing, runtime and drivers in one workspace.
tags:
  - software
---

# Software

The Python packages form one uv workspace, with a member per component. Names
start with `kohakuaccel` for framework code, `kohakutpu` for the TPU, or
`toyaccel` for the template project. Rust stage crates live in `rust/crates/`.

```text
uv sync
uv run python ../scripts/py/build_rs.py spec
uv run pytest -n 8 -p no:cacheprovider
```

Run hardware tests separately, with `-n 1` and an explicitly named target.
A card or Verilator model is not needed for offline compilation and binding tests.

## Components

| Member | Modules | Responsibility |
|---|---|---|
| `language/` | `kohakutpu.language` | Python DSL tracing, `.ktpu` syntax, machine schema reader, host numerics and examples |
| `compiler/` | `kohakutpu.compiler._rs` | Native extension built from the Rust `ktpu` crate |
| | `kohakutpu.stack` | Small Rust compile, buffer and specialisation adapters |
| | `kohakuaccel.compiler.isa` | Generic instruction-layout toolkit for framework projects |
| `rust/` | `ktpu-machine`, `ktpu-isa`, `ktpu-l0`, `ktpu-l1`, `ktpu-l2`, `ktpu-verify`, `ktpu` | Machine facts, encodings, packages, lowering, scheduling and static verification |
| `driver/` | `kohakuaccel.driver`, `kohakutpu.driver` | Transports, node queues, discovery, clocks, staging and host entry points |
| `simulation/` | `kohakuaccel.simulation` | Callback-driven package interpreter and transport test doubles; computes no tensor values |
| | `kohakutpu.simulation` | Machine-bound Rust package reader and Verilator interfaces |
| `firmware/` | C and Python tools | Node firmware, build configuration and offline package producers |
| `application/` | `kohakutpu.application` | Tensor API, tinygrad integration, kernels, runtime and tools |
| `template/` | `toyaccel.*` | Small framework project ([template](template/README.md)) |

The runtime's machine view comes from `_rs.machine_view`; addresses, staging
capacity, buffers, IMEM accounting and packages use the same machine schema as
the Rust compiler. Compiled tensor values are checked on a card or Verilated
card, never a tensor behaviour model. Frozen goldens witness byte layouts offline.

## Import rules

`tests/test_imports.py` checks imports at module level. Framework modules import
no project, and a project imports no other project. The callback interpreter
receives its package reader from its caller; the framework does not import the
TPU extension.

| Component | May import |
|---|---|
| language | language |
| compiler namespace | compiler namespace |
| stack | stack, native compiler binding, language, runtime package adapters |
| driver | driver, machine-schema language reader |
| simulation | simulation, driver, native compiler binding, language |
| application | every component |

## Verification and style

Tier-0 spec runners call Rust directly; Python retains frontend and runtime
checks and the target-facing runners. There is no Python-versus-Rust compiler
selection mode. `KTPU_DUMP` writes compiled L2/L1 text. Rust owns stage caches;
Python holds bounded in-process specialisation and package memos.

Run `ruff check .` and `black --check .` from this directory. Rust uses
`cargo fmt --check` and crate-scoped tests and clippy.
