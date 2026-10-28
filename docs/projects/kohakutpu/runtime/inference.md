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
