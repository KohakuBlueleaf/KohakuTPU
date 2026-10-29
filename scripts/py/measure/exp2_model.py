"""Every EXP2 / EXP2D user on the Verilated v9t6s model, compiled under
`l2.vector/exp2-wrapped-domain` (no guard) and with the domain guard at each
range-analysis setting (`exp2_ranges`): cycles and the error against a
float64 reference.

    python scripts/py/measure/exp2_model.py compile --artifact A.pkl
    python scripts/py/measure/exp2_model.py execute --artifact A.pkl --output R.json

`compile` runs the compiler; `execute` is one model session and runs none.
"exp2d constant" is `exp2` written as `exp2d(x, 0)`: its guard is EXP2D's
four ops where "kt.exp2" has EXP2's three, on the same rows. "clamped exp2"
and "exp2 of tanh" have arguments interval analysis proves in range.
"""

import argparse
import json
import math
import pickle
import time
from pathlib import Path

import kohakutpu.language.dsl as kt
import numpy as np
from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language.examples.dsl import acts
from kohakutpu.language.examples.dsl.vector.activation import activation
from kohakutpu.language.examples.dsl.vector.softmax import softmax
from kohakutpu.language.numerics import fp16
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

ROOT = Path(__file__).resolve().parents[3]
BOARD = "vlt_v9t6s_1n"
MUTANT = "l2.vector/exp2-wrapped-domain"
#: (variant, mutant, exp2_ranges) each user compiles as.
VARIANTS = [
    ("unguarded", MUTANT, None),
    ("off", None, "off"),
    ("intervals", None, "intervals"),
    ("rowmax", None, "rowmax"),
]
#: One argument of each class EXP2's domain splits, fp16-exact.
CLASSES = [-math.inf, -300.0, -256.0, -255.5, -192.0, -128.5, -128.0, -127.5]
CLASSES += [-127.0, -126.5, -126.0, -15.0, -0.0, 0.0, 15.0, 127.5, 128.0]
CLASSES += [128.5, 192.0, 255.5, 256.0, 300.0, math.inf, math.nan]
#: Arguments of x * log2(e) users: x * log2(e) crosses every class edge.
SCALED = [-300.0, -200.0, -160.0, -133.0, -100.0, -90.0, -88.5, -88.0, -50.0]
SCALED += [-1.0, 0.0, 1.0, 50.0, 88.0, 88.5, 90.0, 100.0, 133.0, 160.0, 200.0]


#: The kernels' buffers: 16 rows of 128, and score blocks of 64 columns.
ROWS_IN, ROWS_OUT = kt.In(16, 128), kt.Out(16, 128)
BLOCK_IN, BLOCK_OUT = kt.In(16, 64), kt.Out(16, 64)


def e8_exp2(x):
    """2^x in E8M15: +inf from 2^128, +0 below 2^-126."""
    with np.errstate(over="ignore"):
        out = np.exp2(np.asarray(x, np.float64))
    return np.where(out < 2.0**-126, 0.0, np.where(out >= 2.0**128, np.inf, out))


def rows(values, shape):
    return np.resize(np.array(values, dtype="<f2"), shape)


def tanh(v):
    """tanh as 2 sigmoid(2v) - 1, in [-1, 1]."""
    return 2.0 * acts.sigmoid(2.0 * v) - 1.0


#: `direct`'s ops on the loaded rows.
DIRECT = {
    "exp2": kt.exp2,
    "exp2d": lambda v: kt.exp2d(v, 0.0),
    "clamped": lambda v: kt.exp2(kt.maximum(kt.minimum(v, 100.0), -100.0)),
    "tanh": lambda v: kt.exp2(8.0 * tanh(v)),
}


@kt.kernel
def direct(x=ROWS_IN, y=ROWS_OUT, *, op="exp2"):
    for c in kt.par(1, on=kt.vc):
        for t in kt.range(1):
            v = kt.load(x[t * 16 : t * 16 + 16, :], gt4=True)
            kt.store(y[t * 16 : t * 16 + 16, :], DIRECT[op](v))


@kt.kernel
def softmax_d(x=ROWS_IN, y=ROWS_OUT):
    for c in kt.par(1, on=kt.vc):
        for t in kt.range(1):
            s = kt.load(x[t * 16 : t * 16 + 16, :], gt4=True)
            p = kt.exp2d(s, kt.rmax(s))
            kt.store(y[t * 16 : t * 16 + 16, :], p * kt.recip(kt.rsum(p)))


@kt.kernel
def attention(s0=BLOCK_IN, s1=BLOCK_IN, out=BLOCK_OUT):
    """Online statistics over two score blocks: the row max of both, each
    block's exp2(s - max) and sum, and block 0's normalised probabilities."""
    m0, m1, rm = (kt.buffer((16,), "f16") for _ in range(3))
    p0 = kt.buffer((16, 64), "f16")
    z0, z1, total = (kt.buffer((16,), "f16") for _ in range(3))

    for src, dst in ((s0, m0), (s1, m1)):
        for _ in kt.par(1, on=kt.vc):
            for _ in kt.range(1):
                kt.store(dst[0:16], kt.rmax(kt.load(src[0:16, :], gt4=True)))
    for _ in kt.par(1, on=kt.vc):
        for _ in kt.range(1):
            both = kt.maximum(kt.unpack(kt.load(m0[0:16])), kt.load(m1[0:16]))
            kt.store(rm[0:16], both)
    for src, z, keep in ((s0, z0, True), (s1, z1, False)):
        for _ in kt.par(1, on=kt.vc):
            for _ in kt.range(1):
                prob = kt.exp2(kt.load(src[0:16, :], gt4=True) - kt.load(rm[0:16]))
                if keep:
                    kt.store(p0[0:16, :], prob)
                kt.store(z[0:16], kt.rsum(prob))
    for _ in kt.par(1, on=kt.vc):
        for _ in kt.range(1):
            kt.store(total[0:16], kt.unpack(kt.load(z0[0:16])) + kt.load(z1[0:16]))
    for _ in kt.par(1, on=kt.vc):
        for _ in kt.range(1):
            inv = kt.recip(kt.load(total[0:16]))
            kt.store(out[0:16, :], kt.load(p0[0:16, :], gt4=True) * inv)


def cases(machine):
    """`(user, kernel, given, consts, inputs, output, reference)` of every user."""
    dsl = K.dsl_machine(machine)
    x16 = {"x": (16, 128), "y": (16, 128)}
    classes = rows(CLASSES, (16, 128))
    scaled = rows(SCALED, (16, 128))
    sf = scaled.astype(np.float64)
    out = []
    cf = classes.astype(np.float64)
    for user, op, ref in (
        ("kt.exp2", "exp2", e8_exp2(cf)),
        ("exp2d constant", "exp2d", e8_exp2(cf)),
        ("clamped exp2", "clamped", e8_exp2(np.clip(cf, -100.0, 100.0))),
        ("exp2 of tanh", "tanh", e8_exp2(8.0 * np.tanh(cf))),
    ):
        out.append(
            (user, direct.trace(x16, machine=dsl, op=op), {"x": classes}, "y", ref)
        )

    e = X.op("exp2", X.op("mul", X.Load(0), math.log2(math.e)))
    k, given, _ = V.rows_kernel(
        e, (V.Operand("full", pitch=128),), 16, 128, V.Operand("full", pitch=128), 1
    )
    ref = e8_exp2(sf * math.log2(math.e))
    out.append(("exp", k.trace(given, machine=dsl), {"in0": scaled}, "out", ref))

    for act, ref in (
        ("sigmoid", 1.0 / (1.0 + np.exp(-sf))),
        ("silu", sf / (1.0 + np.exp(-sf))),
        ("gelu", sf / (1.0 + np.exp(-acts.GELU_K * sf))),
    ):
        traced = activation.trace(x16, act=act, machine=dsl)
        out.append((act, traced, {"x": scaled}, "y", ref))

    logits = np.full((16, 128), -300.0, dtype="<f2")
    logits[0::2, :8] = [0.0, -90.0, -100.0, -133.0, -160.0, -200.0, -300.0, -50.0]
    logits[1::2, :8] = [50.0, -45.0, -50.0, -83.0, -110.0, -150.0, 49.0, 48.0]
    lf = logits.astype(np.float64)
    shifted = np.exp(lf - lf.max(-1, keepdims=True))
    ref = shifted / shifted.sum(-1, keepdims=True)
    out.append(("softmax", softmax.trace(x16, machine=dsl), {"x": logits}, "y", ref))
    # exp2d(s, max s) is 2^(s - floor(max s)): base 2, the max floored.
    p = np.exp2(lf - np.floor(lf.max(-1, keepdims=True)))
    ref = p / p.sum(-1, keepdims=True)
    out.append(
        ("exp2d softmax", softmax_d.trace(x16, machine=dsl), {"x": logits}, "y", ref)
    )

    s0 = np.full((16, 64), -300.0, dtype="<f2")
    s0[:, :2] = [0.0, -1.0]
    s1 = np.full((16, 64), -300.0, dtype="<f2")
    s1[:, :3] = [-192.0, -150.0, -128.5]
    p0, p1 = (np.exp2(s.astype(np.float64)) for s in (s0, s1))
    ref = p0 / (p0.sum(-1, keepdims=True) + p1.sum(-1, keepdims=True))
    given = {"s0": (16, 64), "s1": (16, 64), "out": (16, 64)}
    traced = attention.trace(given, machine=dsl)
    out.append(("attention online", traced, {"s0": s0, "s1": s1}, "out", ref))

    x = X.Load(0)
    mean = X.op("mul", X.Reduce("sum", x), 1.0 / 128.0)
    d = X.op("sub", x, mean)
    var = X.op("mul", X.Reduce("sum", X.op("mul", d, d)), 1.0 / 128.0)
    norm = X.op("mul", d, X.op("rsqrt", X.op("add", var, 1e-5)))
    u = X.op("add", X.op("mul", norm, X.Load(1)), X.Load(2))
    inner = X.op("add", u, X.op("mul", 0.044715, X.op("mul", X.op("mul", u, u), u)))
    z = X.op("mul", -2.0 * math.sqrt(2.0 / math.pi) / math.log(2.0), inner)
    gelu = X.op("mul", u, X.op("recip", X.op("add", 1.0, X.op("exp2", z))))
    operands = (V.Operand("full", pitch=128), V.Operand("col"), V.Operand("col"))
    k, given, _ = V.rows_kernel(
        gelu, operands, 16, 128, V.Operand("full", pitch=128), 1
    )
    xs = np.tile(np.array([-1.0, 1.0], dtype="<f2"), (16, 64))
    gamma = rows([12.046875, 13.0, 16.0, 32.0, 2.0, 0.5], (128,))
    beta = np.zeros(128, dtype="<f2")
    uf = xs.astype(np.float64) * gamma.astype(np.float64)
    ref = 0.5 * uf * (1.0 + np.tanh(math.sqrt(2.0 / math.pi) * (uf + 0.044715 * uf**3)))
    inputs = {"in0": xs, "in1": gamma, "in2": beta}
    out.append(("layernorm tanh-gelu", k.trace(given, machine=dsl), inputs, "out", ref))
    return out


def compile_cases(args):
    machine = machine_of(BOARD, packing_backend="vector_mx7")
    built = []
    for user, traced, inputs, output, ref in cases(machine):
        for variant, mutant, ranges in VARIANTS:
            got = rs._rs.compile(
                json.dumps(traced.graph),
                rs.machine_json(machine),
                cache_dir="",
                fetch=True,
                mutant=mutant,
                exp2_ranges=ranges,
            )
            assert not got["verdict"]["refused"], (user, got["verdict"])
            l1 = got["l1"]
            guard = ("vmax", "vcmplt", "vcmpgt", "vadd", "vabs", "vmov")
            built.append(
                {
                    "user": user,
                    "variant": variant,
                    "graph": traced.graph,
                    "compiled": got,
                    "inputs": {k: v.tobytes() for k, v in inputs.items()},
                    "output": output,
                    "reference": fp16.write(ref).astype("<f2").tobytes(),
                    "math_lines": sum(
                        ln.strip().split(" ")[0] in guard for ln in l1.splitlines()
                    ),
                    "exp2_lines": sum(
                        ln.strip().startswith(("vexp2 ", "vexp2d "))
                        for ln in l1.splitlines()
                    ),
                    "l1_lines": len(l1.splitlines()),
                }
            )
            print(json.dumps({"user": user, "variant": variant}), flush=True)
    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    args.artifact.write_bytes(
        pickle.dumps({"machine": machine.machine_json, "cases": built})
    )


def kind(a):
    """NaN, +inf, -inf or finite, per lane."""
    return np.where(np.isnan(a), 0, np.where(np.isinf(a), np.sign(a) * 2, 1))


def errors(got, want):
    """`(max |error| over lanes finite in both, lanes whose kind differs)`."""
    g = np.frombuffer(got, "<f2").astype(np.float64)
    w = np.frombuffer(want, "<f2").astype(np.float64)
    both = np.isfinite(g) & np.isfinite(w)
    worst = float(np.max(np.abs(g[both] - w[both]), initial=0.0))
    return worst, int(np.sum(kind(g) != kind(w)))


def execute(args):
    bundle = pickle.loads(args.artifact.read_bytes())
    log = args.output.with_suffix(".log")
    started = time.monotonic()
    dev = D.Device(
        kind="sim",
        board=BOARD,
        nodes=1,
        build=args.build,
        log=log,
        classes=False,
    )
    dev.machine = machine_of(BOARD, packing_backend="vector_mx7")
    assert dev.machine.machine_json == bundle["machine"]
    records = []
    try:
        for case in bundle["cases"]:
            comp = generated.from_output(dev, case["graph"], case["compiled"])
            binds = {}
            for name, data in case["inputs"].items():
                binds[name] = dev.alloc(len(data))
                dev.write(binds[name], data)
            size = len(case["reference"])
            binds[case["output"]] = dev.alloc(size)
            try:
                cycles = [
                    dev.run(comp, binds, kind="dsl").cycles for _ in range(args.repeat)
                ]
                got = dev.read(binds[case["output"]], size)
            finally:
                for addr in binds.values():
                    dev.free(addr)
            worst, classes = errors(got, case["reference"])
            keys = ("user", "variant", "math_lines", "exp2_lines", "l1_lines")
            record = {k: case[k] for k in keys}
            record.update(cycles=cycles, max_abs_error=worst, class_mismatches=classes)
            records.append(record)
            print(json.dumps(record), flush=True)
    finally:
        dev.close()
        args.output.write_text(
            json.dumps(
                {"records": records, "wall_seconds": time.monotonic() - started},
                indent=2,
            ),
            encoding="utf-8",
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("compile", "execute"))
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument(
        "--build", type=Path, default=ROOT / "build/v9t6s/vlt_card_v9t6s_1n"
    )
    args = parser.parse_args()
    (compile_cases if args.mode == "compile" else execute)(args)


if __name__ == "__main__":
    main()
