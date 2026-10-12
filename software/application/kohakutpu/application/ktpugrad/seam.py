"""The seam: `engine.realize` calls `to_program(ast, renderer)` by name for
every kernel, the last place it is an ast. For KTPU it returns a PROGRAM
whose binary is the ast's key, the plan filed under it; other devices reach
tinygrad's own. Pinned while installed (the traps each one avoids):

| | | why |
|---|---|---|
| NOOPT | 1 | every OptOps is a loop transform of generated code; we generate none |
| BEAM | 0 | beam search times candidates by RUNNING them (a sim run each) |
| PARALLEL | 0 | the compile pool lowers in subprocesses that never see this seam |
| NO_MEMORY_PLANNER | 1 | the planner suballocates a schedule's buffers as views of one arena; a buffer's forms belong to its own storage (lifetimes: `run_linear` below) |
| DEFAULT_FLOAT | half | the machine's memory is f16 (f32 tensors still run, through f16) |
| SPLIT_REDUCEOP | 0 | a large reduction split as reshape + permute partials: a transposed read here; the runner reduces any size itself |
| WINO | 0 | a winograd conv is constant-matrix transforms over permuted tiles |
| PCONTIG | 0 | partial contiguous stages local buffers; a unit has none to name |
| buffers a kernel | 8 | a vector core walks 8 memory views (the output and 7 reads): the scheduler cuts wider kernels |
| producer inlining | elementwise | a computed producer inlined into a consumer's reduction (`relu(h)` into a product) hides the contraction from the planner: it stays its own kernel; plain reloads and elementwise consumers follow tinygrad (`KTPU_TINYGRAD_FUSE`: `0` keeps every computed producer, `1` is tinygrad's own) |
| buffer lifetime | last read | a schedule holds every buffer it names to its end: `run_linear` frees each KTPU intermediate after its last kernel |
"""

import os
from dataclasses import replace

from tinygrad import tensor
from tinygrad.device import Buffer
from tinygrad.engine import realize
from tinygrad.helpers import (
    BEAM,
    DEBUG,
    DEFAULT_FLOAT,
    NO_MEMORY_PLANNER,
    NOOPT,
    PARALLEL,
    PCONTIG,
    SPLIT_REDUCEOP,
    VALIDATE_WITH_CPU,
    WINO,
    perf_counter_us,
)
from tinygrad.renderer import Estimates
from tinygrad.schedule import rangeify
from tinygrad.uop import Ops
from tinygrad.uop.ops import AxisType, PatternMatcher, ProgramInfo, UOp

from kohakutpu.application.ktpugrad import lower as L
from kohakutpu.application.ktpugrad.device import NAME, register
from kohakutpu.application.ktpugrad.plans import PLANS, KTPUUnsupported

#: The realize module's global the engine calls by name: patched as an attribute.
SEAM = "to_program"
#: tinygrad's own, kept for every other device.
_TO_PROGRAM = getattr(realize, SEAM)
_PINS = (
    (NOOPT, 1),
    (BEAM, 0),
    (PARALLEL, 0),
    (NO_MEMORY_PLANNER, 1),
    (DEFAULT_FLOAT, "half"),
    (SPLIT_REDUCEOP, 0),
    (WINO, 0),
    (PCONTIG, 0),
)
#: Memory views a vector core walks: the scheduler's per-device buffer cap.
MAX_BUFS = 8
#: Producer inlining into KTPU kernels: ``elementwise`` (into elementwise
#: consumers only), ``0`` (never a computed producer), ``1`` (tinygrad's own).
FUSE_ENV = "KTPU_TINYGRAD_FUSE"
FUSE_MODES = ("elementwise", "0", "1")
FUSE_MODE = "elementwise"
#: tinygrad's buffer-removal rules (the module global its scheduler reads).
_PM_REMOVE = rangeify.pm_remove_bufferize


def _on_ktpu(dev) -> bool:
    devs = dev if isinstance(dev, tuple) else (dev,)
    return any(isinstance(d, str) and d.split(":")[0] == NAME for d in devs)


def _reduced(idx) -> bool:
    """The consumer reads the buffer inside one of its reductions."""
    return any(r.arg[-1] == AxisType.REDUCE for s in idx.src[1:] for r in s.ranges)


def _remove_bufferize(src, buf, idx):
    """tinygrad's rule for a KTPU buffer read as a plain reload or by an
    elementwise consumer; a computed producer read inside a reduction stays
    its own kernel (``elementwise``), or every computed one does (``0``)."""
    computed = src.op is not Ops.INDEX
    if (
        _on_ktpu(getattr(buf.arg, "device", None))
        and computed
        and (FUSE_MODE == "0" or _reduced(idx))
    ):
        return None
    return rangeify.remove_bufferize(src, buf, idx)


_PM_KEEP = PatternMatcher(
    [
        (pat, _remove_bufferize if fn is rangeify.remove_bufferize else fn)
        for pat, fn in _PM_REMOVE.patterns
    ]
)
#: The tensor module's global `realize` runs a schedule with.
RUN = "run_linear"
_RUN_LINEAR = getattr(tensor, RUN)


def _held() -> set:
    """Ids of the base buffers some live Tensor's graph names."""
    roots = [t.uop for r in list(tensor.all_tensors) if (t := r()) is not None]
    return {
        id(b.base)
        for u in UOp.sink(*roots).toposort()
        if u.op is Ops.BUFFER and isinstance(b := u.buffer, Buffer)
    }


def _ktpu_buffers(call, inputs) -> list:
    out = []
    for u in realize.resolve_params(call, inputs):
        b = u.buffer
        if isinstance(b, Buffer) and _on_ktpu(b.device):
            out.append(b.base)
    return out


def run_linear(
    linear, var_vals=None, input_uops=(), update_stats=True, jit=False, wait=False
):
    """tinygrad's `run_linear`, freeing each KTPU intermediate after the last
    kernel that reads it: the schedule holds every buffer it names to its
    end, and the card's memory is its DRAM. No Tensor names an intermediate,
    so none is read again; every other buffer is left to tinygrad."""
    inputs = tuple(input_uops)
    used = [] if jit else [_ktpu_buffers(c, inputs) for c in linear.src]
    if not any(used):
        return _RUN_LINEAR(linear, var_vals, input_uops, update_stats, jit, wait)
    last = {id(b): (i, b) for i, bs in enumerate(used) for b in bs}
    held = _held()
    dead: dict = {}
    for k, (i, b) in last.items():
        if k not in held:
            dead.setdefault(i, []).append(b)
    linear = realize.link_linear(
        realize.compile_linear(
            linear, validate=bool(VALIDATE_WITH_CPU.value), input_uops=list(inputs)
        )
    )
    ctx = realize.ExecContext(
        var_vals or {}, inputs, update_stats, jit, wait or DEBUG >= 2
    )
    for i, call in enumerate(linear.src):
        ets = realize.pm_exec.rewrite(call, ctx) or []
        realize.track_stats(ctx, call, perf_counter_us(), ets)
        for b in dead.get(i, ()):
            if b.is_allocated() and not b.allocated_views:
                b.deallocate()


_saved: list | None = None
#: where `capture` collects asts while it runs
_recording: list | None = None


def to_program(ast, renderer):
    """tinygrad's `to_program`, a plan for KTPU."""
    if _recording is not None:
        _recording.append(ast)
    if getattr(renderer.target, "device", None) != NAME:
        return _TO_PROGRAM(ast, renderer)
    key = ast.key
    if key not in PLANS:
        try:
            PLANS[key] = L.plan(ast)
        except L.Refused as e:
            raise KTPUUnsupported(
                f"KTPU refuses kernel {getattr(ast.arg, 'name', '?')}: {e}"
            ) from None
    p = PLANS[key]
    params = tuple(u for u in ast.toposort() if u.op is Ops.PARAM)
    sink = ast.replace(
        arg=replace(
            ast.arg, estimates=Estimates(ops=p.out_n, lds=p.out_n * 2, mem=p.out_n * 2)
        )
    )
    return UOp(
        Ops.PROGRAM,
        src=(
            sink,
            UOp(Ops.LINEAR, src=params),
            UOp(Ops.SOURCE, arg=f"ktpu {p.kind} {p.name}"),
            UOp(Ops.BINARY, arg=key),
        ),
        arg=ProgramInfo.from_sink(sink, renderer.target),
    )


def install() -> None:
    """Register KTPU and route lowering through the seam. Idempotent."""
    global _saved
    register()
    if _saved is None:
        _saved = [(v, v.value) for v, _ in _PINS]
    for v, want in _PINS:
        v.value = want  # pyright: ignore[reportAttributeAccessIssue]  (process-wide)
    rangeify.DEVICE_MAX_BUFS[NAME] = MAX_BUFS
    global FUSE_MODE
    FUSE_MODE = os.environ.get(FUSE_ENV, "elementwise")
    if FUSE_MODE not in FUSE_MODES:
        raise ValueError(f"{FUSE_ENV}={FUSE_MODE!r}: one of {FUSE_MODES}")
    rangeify.pm_remove_bufferize = _PM_REMOVE if FUSE_MODE == "1" else _PM_KEEP
    setattr(realize, SEAM, to_program)
    setattr(tensor, RUN, run_linear)


def uninstall() -> None:
    """tinygrad's own `to_program` back, and its options unpinned. The
    device stays registered (a live buffer may still be on it)."""
    global _saved
    if _saved is not None:
        for v, old in _saved:
            v.value = old  # pyright: ignore[reportAttributeAccessIssue]
        _saved = None
    rangeify.DEVICE_MAX_BUFS.pop(NAME, None)
    rangeify.pm_remove_bufferize = _PM_REMOVE
    setattr(realize, SEAM, _TO_PROGRAM)
    setattr(tensor, RUN, _RUN_LINEAR)


def installed() -> bool:
    return getattr(realize, SEAM) is to_program


def capture(run, strict: bool = True) -> list:
    """Every ast `run()` hands to lowering, in order -- which kernels an
    expression scheduled. With `strict=False` a refusal ends the run and the
    asts so far are returned."""
    global _recording
    if not installed():
        raise KTPUUnsupported("capture reads the installed seam: install() first")
    held, _recording = _recording, []
    try:
        try:
            run()
        except KTPUUnsupported:
            if strict:
                raise
        return _recording
    finally:
        _recording = held


__all__ = [
    "KTPUUnsupported",
    "capture",
    "install",
    "installed",
    "to_program",
    "uninstall",
]
