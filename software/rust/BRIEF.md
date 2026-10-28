# Rust stack contribution brief

Read the specification for the stage under `docs/spec/stack/` and the repository
rules before changing a crate. The compiler is Rust; Python supplies tracing,
runtime orchestration and the `kohakutpu.stack` binding seam.

## Stage ownership

| Crates | Responsibility |
|---|---|
| `ktpu-machine`, `ktpu-isa` | Typed machine facts and instruction encodings |
| `ktpu-l0`, `ktpu-cache` | Packages, relocation, dispatch and caching |
| `ktpu-l1` | L1 parsing, scheduling and image emission |
| `ktpu-l2` | Typed L2 and lowering |
| `ktpu-verify` | Static checking and clause-bearing refusals |
| `ktpu-graph`, `ktpu-kernels`, `ktpu-plan` | Graphs, generators and tensor planning |
| `ktpu`, `ktpu-spec` | Pipeline, native bindings and spec runners |

## Contract

- Preserve specified package bytes, encodings and text formats.
- Implement the accepted rulings in `docs/spec/stack/rulings.md`.
- Obtain hardware facts from `ktpu_machine::Machine`.
- Refusals identify their specification clause.
- Frozen witnesses live under `golden/`; see `golden/README.md` for comparisons
  and machine digest regeneration. Do not introduce a Python compiler or model.
- The harness registers native runners and mutants unconditionally. Build the
  extension with `scripts/py/build_rs.py spec` for mutation tests, or `prod` for
  production. The spec build enables `python` and `mutants`; production enables
  only `python`. `_rs.MUTANTS` lists the available mutation switches.
- Every fix needs a check that fails when the fix is reversed.

## Verification

Run Cargo tests one crate at a time with bounded jobs, and run golden comparisons
in bounded shards. Run `cargo fmt --all --check` and clippy with `-D warnings` on
changed crates. Python imports stay at module scope, with ruff and black clean.
Follow repository machine-safety rules for pytest worker counts and serialized
model execution. Never rebuild a supplied model or change RTL for a stack job.
