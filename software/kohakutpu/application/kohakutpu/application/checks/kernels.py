"""Independent tinygrad CPU-AST execution and documented numerical budgets.

No KTPU plan evaluator, numerical emulator or behaviour model is an oracle.
The data AST and an absolute-error AST both compile with tinygrad's CPU backend.
"""

import math
from collections import Counter
from dataclasses import replace

import numpy as np
from tinygrad.device import Buffer, Device
from tinygrad.dtype import dtypes
from tinygrad.helpers import Context
from tinygrad.uop import Ops
from tinygrad.uop.ops import UOp

from kohakutpu.application.ktpugrad import seam

NUMERICS = "docs/spec/stack/numerics/formats.md"
RELATIVE_FLOOR = 2**-14
U16, U8, U22 = 2**-11, 2**-15, 2**-14
FLOAT_ALU = {
    Ops.ADD,
    Ops.SUB,
    Ops.MUL,
    Ops.MULACC,
    Ops.FDIV,
    Ops.RECIPROCAL,
    Ops.SQRT,
    Ops.EXP2,
    Ops.LOG2,
    Ops.SIN,
    Ops.NEG,
    Ops.MAX,
}


def f16_storage(a, drain=False):
    """NUM.F16.003-005; GEMM drain additionally flushes NUM.ACC.005."""
    x = np.asarray(a, np.float32)
    finite = np.isfinite(x)
    rounded = np.where(finite, np.clip(x, -65504, 65504), x).astype(np.float16)
    if drain:
        rounded = np.where(np.abs(x) < 2**-14, np.float16(0), rounded)
    return rounded


def gamma(n, u):
    return n * u / (1 - n * u) if n * u < 1 else math.inf


def tolerance_class(ast, kind):
    nodes = list(ast.toposort())
    if kind == "matmul":
        return "matmul"
    if any(u.op is Ops.REDUCE for u in nodes):
        return "reduction"

    def movement(u):
        if u.op is Ops.INDEX:
            return u.src[0].op is Ops.PARAM
        if u.op is Ops.CONST:
            # Only literal storage values, not arbitrary f32 lane immediates.
            return isinstance(u.arg, (int, float)) and float(
                f16_storage(u.arg)
            ) == float(u.arg)
        if u.op is Ops.WHERE:
            index_only = not any(v.op is Ops.INDEX for v in u.src[0].toposort())
            return index_only and movement(u.src[1]) and movement(u.src[2])
        return False

    stores = [u for u in nodes if u.op is Ops.STORE]
    return (
        "exact" if stores and all(movement(u.src[1]) for u in stores) else "elementwise"
    )


def _abs(x):
    return x.maximum(-x)


def _const(x):
    return UOp.const(float(x), dtypes.float)


def _round_error(x, u=U8):
    return _abs(x) * u


def _promote(ast):
    """Keep all schedule ops/indices; only float storage/register types change."""
    mapped = {}
    for u in ast.toposort():
        dtype = (
            dtypes.float
            if dtypes.is_float(u.dtype) or u.dtype == dtypes.weakfloat
            else u.dtype
        )
        arg = u.arg
        if u.op is Ops.PARAM:
            arg = replace(arg, dtype=dtype, device="CPU")
        mapped[u] = u.replace(dtype=dtype, src=tuple(mapped[s] for s in u.src), arg=arg)
    return mapped[ast]


def _error_ast(ast, category, quant_errors, path=None):
    """Forward absolute-error propagation on the *CPU* values, not card outputs.

    Full-ULP E8/FP22 terms cover the effective-subtraction corner. Eight E8
    ULPs per seed is an explicit diagnostic policy: NUM.E8.003 says 'a few'
    but supplies no certified count. Reports distinguish this from a proof.
    """
    errors = {}
    z = _const(0)
    depth = (path or {}).get("f16_stage_upper_bound", 0)
    cluster_blocks = (path or {}).get("max_cluster_blocks", 0)
    for u in ast.toposort():
        es = [errors[s] for s in u.src]
        e = z
        if u.op is Ops.INDEX and dtypes.is_float(u.dtype):
            base = u.src[0]
            if base.op is Ops.PARAM:
                e = _const(quant_errors.get(base.arg.slot, 0.0))
        elif u.op is Ops.CONST and dtypes.is_float(u.dtype):
            e = _round_error(u)
        elif u.op is Ops.CAST or u.op is Ops.NEG:
            e = es[0]
        elif u.op in (Ops.ADD, Ops.SUB):
            e = es[0] + es[1]
        elif u.op in (Ops.MUL, Ops.MULACC):
            a, b = u.src[:2]
            e = _abs(a) * es[1] + _abs(b) * es[0] + es[0] * es[1]
            if u.op is Ops.MULACC:
                e = e + es[2]
        elif u.op is Ops.MAX:
            e = es[0].maximum(es[1])
        elif u.op in (Ops.RECIPROCAL, Ops.FDIV):
            a = u.src[-1]
            ea = es[-1]
            # Crossing zero is uncertifiable: no finite tolerance is accepted.
            denom = _abs(a) - ea
            safe = denom > 0
            inv_error = safe.where(ea / (_abs(a) * denom), _const(math.inf))
            e = (
                inv_error
                if u.op is Ops.RECIPROCAL
                else (_abs(u.src[0]) * inv_error + es[0] / denom)
            )
        elif u.op is Ops.SQRT:
            a = u.src[0]
            lo = (a - es[0]).maximum(0).sqrt()
            hi = (a + es[0]).maximum(0).sqrt()
            e = (a >= es[0]).where((u - lo).maximum(hi - u), _const(math.inf))
        elif u.op is Ops.EXP2:
            e = _abs(u) * (es[0].exp2() - 1)
        elif u.op is Ops.LOG2:
            a = _abs(u.src[0])
            ratio = es[0] / a
            e = (ratio < 1).where(-(1 - ratio).log2(), _const(math.inf))
        elif u.op is Ops.SIN:
            e = es[0]
        elif u.op in (Ops.CMPLT, Ops.CMPEQ, Ops.CMPNE):
            # A possibly changed branch has no finite smooth error budget.
            delta = _abs(u.src[0] - u.src[1])
            e = ((es[0] + es[1]) > 0).where(
                (delta <= es[0] + es[1]).where(_const(math.inf), z), z
            )
        elif u.op is Ops.WHERE:
            e = u.src[0].where(es[1], es[2]) + es[0]
        elif u.op is Ops.REDUCE:
            value, ranges = u.src[0], u.src[1:]
            n = math.prod(int(r.src[0].arg) for r in ranges)
            if u.arg[0] is Ops.ADD:
                total = (_abs(value) + es[0]).reduce(*ranges, arg=u.arg)
                steps = math.ceil(n / 32) if category == "matmul" else n
                unit = U22 if category == "matmul" else U8
                e = es[0].reduce(*ranges, arg=u.arg) + total * gamma(steps, unit)
                if path is not None:
                    # Storage errors act on partial magnitudes, before cancellation.
                    accumulate = (1 + gamma(n, U8)) * (
                        1 + gamma(2 * cluster_blocks, U22)
                    ) * (1 + gamma(n, 2**-24)) - 1
                    storage = gamma(depth, U16)
                    growth = (1 + accumulate) * (1 + storage)
                    extra = total * (growth - 1)
                    extra = extra + n * depth * 2**-25 * growth
                    # No finite rounding-only guarantee when a partial may saturate.
                    e = e + (total * growth <= 65504).where(extra, _const(math.inf))
                    if cluster_blocks:
                        e = e + depth * 2**-14
            elif u.arg[0] is Ops.MAX:
                e = es[0].reduce(*ranges, arg=u.arg)
            else:
                raise ValueError(f"no certified budget for reduction {u.arg}")
            # A lowered reduction can materialise an f16 intermediate.
            e = e + (_abs(u) + e) * U16 + 2**-24
            if category == "matmul":
                # The drain is inside the AST's epilogue, not its final store.
                e = e + 2**-14 + (_abs(u) - 65504).maximum(0)
        elif u.op is Ops.STORE:
            # Both the card output and the CPU reference are rounded to storage.
            e = es[1] * (1 + U16) + 2 * _round_error(u.src[1], U16) + 2**-24
            errors[u] = e
            continue
        elif u.op in (Ops.END, Ops.SINK):
            e = es[0] if es else z
        elif dtypes.is_float(u.dtype) and u.op not in (
            Ops.PARAM,
            Ops.INDEX,
            Ops.CONST,
        ):
            raise ValueError(f"no numerical budget for {u.op.name}")
        if not dtypes.is_float(u.dtype) and u.op not in (
            Ops.STORE,
            Ops.END,
            Ops.SINK,
            Ops.CMPLT,
            Ops.CMPEQ,
            Ops.CMPNE,
            Ops.WHERE,
            Ops.CAST,
        ):
            e = z
        if u.op in FLOAT_ALU and dtypes.is_float(u.dtype):
            seeds = (
                8
                if u.op
                in (Ops.EXP2, Ops.LOG2, Ops.SIN, Ops.RECIPROCAL, Ops.FDIV, Ops.SQRT)
                else 1
            )
            e = e + (_abs(u) + e) * (seeds * U8)
        errors[u] = e
    subs = {
        u: u.replace(src=(u.src[0], errors[u]))
        for u in ast.toposort()
        if u.op is Ops.STORE
    }
    return ast.substitute(subs)


class CPUKernel:
    """The original scheduled AST, promoted to f32 and compiled independently."""

    def __init__(self, ast):
        self.ast = _promote(ast)
        self.params = {u.arg.slot: u for u in self.ast.toposort() if u.op is Ops.PARAM}
        self.outputs = sorted(
            {u.src[0].src[0].arg.slot for u in self.ast.toposort() if u.op is Ops.STORE}
        )

    @staticmethod
    def execute(ast, arrays):
        with Context(NOOPT=1, BEAM=0, NUM_CPU_THREADS=1):
            program = seam._TO_PROGRAM(ast, Device["CPU"].renderer)
        buffers = {
            slot: Buffer("CPU", a.size, dtypes.float).ensure_allocated()
            for slot, a in arrays.items()
        }
        for slot, a in arrays.items():
            buffers[slot].allocator._copyin(
                buffers[slot]._buf,
                memoryview(np.ascontiguousarray(a, np.float32)).cast("B"),
            )
        runtime = Device["CPU"].runtime(program.to_elf())
        gs, ls = program.arg.launch_dims({})
        runtime(
            *[buffers[i]._buf for i in program.arg.globals],
            global_size=gs,
            local_size=ls,
            vals=(),
            wait=True,
        )
        return {
            i: np.frombuffer(buffers[i].as_memoryview(), np.float32).copy()
            for i in program.arg.outs
        }

    def reference(self, arrays, category, quant_slots=(), path=None):
        values = self.execute(self.ast, arrays)
        if (path or {}).get("uncertifiable_reasons"):
            return values, {i: np.full_like(a, np.inf) for i, a in values.items()}
        if category == "exact":
            return values, {i: np.zeros_like(a) for i, a in values.items()}
        quant = {}
        if (path or {}).get("contains_mxfp7"):
            # A vector plan may become a cluster product only inside run._vector.
            quant_slots = {
                u.src[0].arg.slot
                for red in self.ast.toposort()
                if red.op is Ops.REDUCE
                for u in red.src[0].toposort()
                if u.op is Ops.INDEX
                and u.src[0].op is Ops.PARAM
                and dtypes.is_float(u.dtype)
            }
        if category == "matmul" or (path or {}).get("contains_mxfp7"):
            for slot in quant_slots:
                peak = float(np.abs(arrays[slot]).max(initial=0))
                # Scale rounds upward on an E5M3 grid; reciprocal table is Q12.
                scale = max((15 / 8) * 2**-20, peak * (9 / 8) / 63)
                if peak >= 2**15:
                    # At the range edge use the largest legal E5M3 scale.
                    scale = max(scale, 3840.0)
                quant[slot] = scale / 2 + peak * 15 / 65536
        error_ast = _error_ast(self.ast, category, quant, path)
        bounds = self.execute(error_ast, arrays)
        return values, bounds


def compare_output(got, want, bound, category):
    """Reject nonfinite/uncertifiable data and report signed f16 ULP distances."""
    g, w = np.asarray(got, np.float64).ravel(), np.asarray(want, np.float64).ravel()
    b = np.broadcast_to(np.asarray(bound, np.float64), np.shape(want)).ravel()
    if g.shape != w.shape or not g.size:
        raise ValueError("output shapes must match and be nonempty")
    finite = np.isfinite(g) & np.isfinite(w) & np.isfinite(b) & (b >= 0)
    err = np.abs(g - w)
    with np.errstate(over="ignore", invalid="ignore"):
        gh, wh = g.astype(np.float16), w.astype(np.float16)

    def ordered(x):
        bits = x.view(np.uint16).astype(np.int32)
        return np.where(bits & 0x8000, 0x8000 - (bits & 0x7FFF), 0x8000 + bits)

    ulps = np.abs(ordered(gh) - ordered(wh))
    histogram = {
        "0": int((ulps == 0).sum()),
        "1": int((ulps == 1).sum()),
        "2-4": int(((ulps >= 2) & (ulps <= 4)).sum()),
        "5-16": int(((ulps >= 5) & (ulps <= 16)).sum()),
        "17-256": int(((ulps >= 17) & (ulps <= 256)).sum()),
        ">256": int((ulps > 256).sum()),
    }
    exact = g.astype(np.float16).tobytes() == w.astype(np.float16).tobytes()
    passed = bool(finite.all() and (exact if category == "exact" else np.all(err <= b)))
    return dict(
        elements=g.size,
        max_absolute_error=float(err.max()) if finite.all() else None,
        max_relative_error=(
            float((err / np.maximum(np.abs(w), RELATIVE_FLOOR)).max())
            if finite.all()
            else None
        ),
        normalized_max_error=(
            float(err.max() / max(float(np.abs(w).max()), RELATIVE_FLOOR))
            if finite.all()
            else None
        ),
        relative_floor=RELATIVE_FLOOR,
        ulp_f16=histogram,
        max_absolute_bound=float(b.max()) if np.isfinite(b).all() else None,
        uncertifiable_elements=int((~finite).sum()),
        failing_elements=(
            int((~finite | (err > b)).sum()) if category != "exact" else int(not passed)
        ),
        passed=passed,
    )


def op_summary(ast):
    return dict(sorted(Counter(u.op.name for u in ast.toposort()).items()))


def policy(category, path=None):
    return dict(
        category=category,
        path_aware=path is not None,
        lowered_mxfp7=bool((path or {}).get("contains_mxfp7")),
        f16_stage_upper_bound=(path or {}).get("f16_stage_upper_bound", 0),
        lowered_reduction_source=f"{NUMERICS}#lowered-tinygrad-reduction-error-budget",
        source=f"{NUMERICS}: NUM.TOL.006, NUM.F16.003-005, NUM.E8.002-003, NUM.MX7.002-003, NUM.ACC.002-005",
        rule=(
            "bit-exact f16 storage (including sign of zero)"
            if category == "exact"
            else "forward absolute-error propagation: E8 full ULP per ALU, 8 ULP per seed; "
            "f16 input/output RNE; sums gamma_n*sum(abs(x)); MXFP7 half-scale + "
            "reciprocal-table error and FP22 accumulation; observed f16 stage depth "
            "times pre-cancellation absolute summands; no relative bound near cancellation"
        ),
        e8_full_ulp=U8,
        f16_unit_roundoff=U16,
        fp22_full_ulp=U22,
        seed_ulp_policy=8,
        certified=category == "exact",
        qualification="NUM.E8.003 supplies no certified seed ULP count; 8 is a diagnostic policy, not a hardware guarantee.",
    )
