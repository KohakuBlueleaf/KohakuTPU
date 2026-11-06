---
title: Repeated forward inference
summary: "`kohakutpu.application.ktpugrad.Inference` wraps a forward callable returning one tinygrad tensor."
tags:
  - projects
  - kohakutpu
  - runtime
---
# Repeated forward inference

`kohakutpu.application.ktpugrad.Inference` wraps a forward callable returning one
tinygrad tensor. It realises the result and uses TinyJit by default. On KTPU,
TinyJit batches use `KTPUGraph`, which records packages and then replays an equal,
read-free recording from an equal entry state. Inputs are copied into stable
slots before replay, so changing image bytes does not reuse the previous image.

```python
from kohakutpu.application.ktpugrad import Inference

predict = Inference(model)
for image in images:
    logits = predict(image).numpy()
```

The wrapper accepts tensor positional arguments. It normalises them to realised
contiguous tensors and retains at most eight specialisations, keyed by runtime
device identity and input shapes, element types and tinygrad devices. A different
shape creates another TinyJit rather than replaying a graph with incompatible
bindings. Model parameters remain unchanged while the wrapper lives. Call
`predict.reset()` before replacing parameters. Results follow TinyJit's ownership
contract: subsequent calls reuse result buffers, so consume a result before the
next call or clone it.

`Inference(model, replay=False)` selects eager execution. With no explicit
argument, `KTPU_INFERENCE_REPLAY=0` selects eager and `1` selects replay. The ViT
example and MNIST classifier's KTPU accuracy path use this wrapper. ViT's explicit
state observers run eagerly because observation reads intermediate results.

## Numerical programs

Captured planning can compute a different numerical program from separate eager
kernels. `KTPUGraph._plan` uses `ktpugrad.run.units`: elementwise read-through
composition substitutes the producer expression into its consumer, and row
regions hold private normalization and softmax intermediates in registers.
Those expressions have no intervening f16 store/load. Separate eager kernels
pack their intermediate outputs to f16 before the next kernel reads them. The
vector hardware computes in E8M15 register precision; the L2 device double uses
its value simulator. Neither execution should be described as keeping a private
row intermediate at f16 precision when its store has been eliminated.

This expression composition is distinct from `KTPU_GRAPH_FUSE` neighbour
composition: the latter namespaces graph bodies and moves private edges to
scratch while retaining their element types. `SplitRunner` composes the selected
graph bodies without selecting another matmul split or K reduction order.

A recording is verified by its events and entry/exit state, not by equality to
an unfused eager result. Its `history` distinguishes recording/planning from an
actual replay. Replaying the same prepared program with the same input preserves
its bytes, including its rounding boundaries. Comparing fused execution to eager
execution or to an f32 reference instead reports absolute and relative error
maxima, means and medians, with no tolerance-based pass label. Matmul K splits
likewise report their changed accumulation order statistically; exact split
options retain byte equality between equivalent P=1 and P>1 eager programs.

## Several nodes

The shared runtime generated-call path grants every available sysnode to a
kernel invocation. This is
kernel parallelism, including batch 1: contractions split their matrix tiles,
attention splits heads/query tiles, and row and destination kernels partition
complete ownership tiles. An explicit `on_node(n)` scope grants only that node.
Single-node devices use the P=1 selection and the same source/compiled caches.
Each invocation records its requested `nodes`, selected `option_id` and `p`,
and `active_nodes`; small
kernels can have empty shares without pretending those shares did useful work.
A generator with only a P=1 option reports `p=1`, independently of the grant.
Legacy Python-DSL native compositions use their own construction path.

A verified TinyJit recording containing split launches is prepared through
`SplitRunner`. Each graph-backed run-only region is composed before L2 lowering.
Opaque L1 programs remain explicit executable boundaries. With
`KTPU_GRAPH_FUSE=1`, neighbour fusion runs before whole-region lowering and slot
leasing, with the quant, elementwise, norm and layout controls. Private
cross-node edges use shared DRAM; single-node native capture retains stage-local
edges. Live outputs and every access crossing a region boundary are exports,
including a freed temporary read after a host write. Canonical
physical read/write-line access sets define RAW, WAR and WAW edges; the lowering
leases cross-sysnode slots for the whole region. Each CPU receives its node's
package, all parts are published before collection, and a retained instance
finishes its preceding epoch before reusing its slots. Host writes separate
regions and retain their original order. Allocations remain pinned by the log
and prepared regions. Physical bindings establish alias dependencies, while
packages remain relocatable: compiler-layout scratch is bound through the
ordinary per-space runtime windows. Deferred quantization records remain live
across node scopes until their finite checks; a remote check acquires the
producer's completed scope before reading its records. A recording without split
launches keeps package replay.

`Resident` and `Sharded` are explicit request/batch-distribution utilities, not
the implementation of a device's kernel-parallel grant. They are unnecessary
for spreading a batch-1 model over the card.

## Dispatch controls

- `KTPU_PLAN_CACHE=0|1` controls bounded exact topology memoization. Dynamic
  storage validity, addresses and host writes are interpreted on every eager call.
- `KTPU_SPECIALISE_CACHE=0|1` controls a device's bounded DSL specialisation cache.
  A hit skips tracing and library lookup. Keys include the kernel identity,
  effective constants, port shapes/layouts, machine, mutation context, library
  root and library policy. `device.invalidate_library()` clears both compiled
  graph and specialisation views after replacing library content.
- `KOHAKUTPU_PACKAGE_CACHE_BYTES` bounds idle resident queue packages in bytes.
  Zero retires packages at their final completion; a positive value retains an
  LRU of completed uploads. Live tags and prepared recordings keep their own
  references.
- `KOHAKUTPU_PREPARED_REPLAY=0|1` selects prepared uploads for a stable recording;
  `KOHAKUTPU_REPLAY_PACKAGE_BYTES` bounds their storage separately.
- `KOHAKUTPU_SUBMIT_BATCH` controls SQ publication grouping. Entries and binding
  blocks are written before the tail is published.
- `KOHAKUTPU_EAGER_FLUSH=0|1` controls publication at the end of an eager plan's
  submission scope. The default, `1`, publishes a partial batch before the host
  begins planning an unrelated kernel. This does not wait for completion.
- `KOHAKUTPU_QUEUE_DEPTH` limits uncollected runtime launches; node ring capacity
  and pinned-byte limits still apply. Host accesses wait through the final
  conflicting launch. Replay publishes its final partial batch before returning.

## Measurement

`python -m examples.tinygrad.dispatch_measure MODEL --compare --depth-sweep`
measures the eager controls off/on and forward replay at depths 1, 2, 4 and 8.
`MODEL` is `vit`, `lenet` or `mlp`. Each result includes CPU time per launch,
cProfile output, inclusive CPU/wall phase totals, graph replay counts and queue
upload counters. A simulator or card execution also records firmware cycles and
idle cycles. The profiled sample is excluded from the steady CPU summary.

Offline execution compiles and admits packages but does not perform tensor
arithmetic. Its cycle values are absent, not a prediction of silicon idle. The
Verilated model advances in response to transport commands; host CPU time there
includes the simulator protocol, not the simulator process's CPU time. Silicon
host starvation must be measured on the card rather than extrapolated from
simulator wall time.
