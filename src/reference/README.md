# reference — unused but load-bearing

Permanent. Not in any build manifest, excluded from the duplicate-module scan.

| dir | holds |
|---|---|
| `arithmetic/` | the FP library study (fp_alu, conversion, division, exp, log, fma, vector_add, fp8, tensorcore) — the documented reference for the shipped arithmetic's design decisions |
| `intcluster/` | `mx_cluster.v` + `mx_acu.v`, the integer-accumulator cluster superseded by the FP node; still has a passing bench (`cluster`) |
| `legacy-cu/` | `mx_matmul_cu.v` (pre-cluster CU, still driven by the `system`/`system32` benches), `computeunit.v`, `dsp/wrapper.v` |
| `poc/` | the frozen proof-of-concept snapshot (23 files). The endpoint-adapter interface shape in here is the durable part — `templates/adapter` is derived from it |

Benches that drive reference modules keep working: the old `src/` paths remain
until cutover, and at cutover their manifest entries point here.
