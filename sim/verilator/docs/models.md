# Simulation models: quiescence-gated modules

`sim/verilator/models/` holds wrappers that make a Verilator build faster
without changing what it computes. Each wrapper takes the name of an RTL module
and puts the unmodified RTL, renamed `<name>__rtl`, behind a clock gate that
withholds an edge only when the edge cannot change the module's state. The RTL
under `src/` is read and never edited: `scripts/py/vlt.py` copies the source
file into the build directory with the one `module` line renamed.

`vlt.py --rtl` builds without them.

## Why

An idle card spends most of its simulation time evaluating pipelines that have
nothing in them. Idle card_v9_1n profile, by RTL path:

| Instance | Share |
|---|---|
| 4 x `mx_cluster_core` (the TCU cascade, `en` tied 1) | 17.8% |
| 4 x `mx_acu_fp_pump` | 11.3% |
| 2 x 16 `vec_alu` | 13.0% |

Each of these evaluates its whole datapath on every edge of its clock, idle or
not, because the hardware does: the pipelines carry no valid gating on their
payload registers.

## The exactness argument

Let `s_t` be a module's inputs sampled at posedge `t`. The wrapped module's
state *converges*: under inputs held constant, with `idle` high, its state
stops changing within `D` edges, and an edge applied to a converged state
leaves it unchanged.

`vlt_qgate` withholds posedge `t` iff `s_t == s_{t-1} == ... == s_{t-1-SETTLE}`
with `idle` high throughout. When `SETTLE + 1 >= D`, the free-running module's
state after edge `t-1` is the converged state for `s_t`, so edge `t` leaves it
unchanged and withholding it is exact. By induction the gated module's state
equals the free-running one's after every edge, and its outputs, which are
functions of that state and its inputs, are equal at every instant.

The gate is an ICG: its enable latch is transparent while `clk` is low and
closed at the rise. The decision therefore sees exactly the inputs the edge
samples, including inputs from a faster clock that move between this clock's
negedge and posedge (`mx_acu_fp_pump`'s partials come from the 2x domain), and
`gclk = clk & run` cannot glitch.

### Each module's convergence

`SETTLE` must reach `D - 1`. The measured minimum is the smallest
`VLT_QGATE_SETTLE` at which the module's differential bench (below) passes; for
the two pure pipelines it is exactly `D - 1`.

| Module | Why it converges | `idle` | measured min | `SETTLE` |
|---|---|---|---|---|
| `mx_cluster_core` | feed-forward, `D` = 17: operand skew flops, DSP models, control shift | 1 | 16 | 32 |
| `vec_alu` | feed-forward, `D` = 15: delay lines, three DSP models, a synchronous ROM | 1 | 14 | 32 |
| `mx_acu_fp` | pipeline plus a tile RAM written only by a valid command, and `busy_tail` counting down to 0 | `!cmd_valid` | <= 10 | 32 |
| `mx_acu_fp_pump` | as `mx_acu_fp`, with the two-stage merge in front | `!cmd_valid` | 13..18 | 32 |
| `vec_lanes` | the 4 x ALAT metadata line drains; its last write-back reaches the register file and ripples through up to five chained ALUs, whose own gates then saturate | `!iss_valid && !ls_we && !red_init` | 121..140 | 255 |

A module with a free-running counter, or one whose RAM can be written with its
inputs held, does not converge and cannot be wrapped this way. The accumulators
do not converge under a held `cmd_valid` (each edge accumulates again), which
is why their `idle` is `!cmd_valid`.

**Public memories.** A harness `PK` into a RAM inside a gated module is not an
input change and would not open its gate. The driver pokes only the RV64
instruction memory and scratchpad, neither inside a gated module.

## Verification

**Differential benches** (`sim/verilator/models/tests/`, `vlt.py gate_<x>`):
`<x>__rtl` free-running beside the gated `<x>` on the same stimulus, every
output compared at every edge. The stimulus alternates random bursts with held
stretches, flips single input bits inside held stretches, pulses reset, and for
the pumped accumulator moves the 2x-domain partials on the intermediate 2x edge.

| Bench | Edges | Withheld | Mismatches |
|---|---|---|---|
| `gate_core` | 200,000 | 74,699 | 0 |
| `gate_vec_alu` | 200,000 | 80,552 | 0 |
| `gate_acu` | 100,000 | 37,816 | 0 |
| `gate_acu_pump` | 100,000 | 29,273 | 0 |
| `gate_lanes` | 200,000 | 53,543 | 0 |

A bench passes only with zero mismatches and more than a tenth of its edges
withheld, so a gate that never closes cannot pass it.

**Negative control.** `-d VLT_QGATE_SETTLE=n` sets every gate below its
module's `D`: at 2 the four pipeline and accumulator benches fail (68k to 154k
mismatches), at 8 `gate_lanes` does (190k).

**The functional benches** that contain these modules (`vec_alu`, `vec_lanes`,
`vec_cu`, `acu`, `cluster_node`, `cluster_node_pump`) print byte-identical
output with and without the models, cycle counts included.

**The card.** rawmm solo on card_v9_1n gives the same node cycles (196,413 and
185,671) and spans built `--rtl` and with the models.
