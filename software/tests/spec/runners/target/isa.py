"""ISA target kernels and benches on the Rust compile seam."""

import subprocess
import sys
from pathlib import Path

import numpy as np
from harness.registry import mutant, patched, runner

from kohakutpu.application.tools import l1 as L1RUN
from kohakutpu.language import examples
from kohakutpu.language.text import module
from kohakutpu.stack import kernel
from kohakutpu.stack.buffer import Buffer

from .isa_values import EXAMPLES

ROOT = Path(__file__).resolve().parents[5]
TEMPLATE = """target ktpu.v9

image t(x, y)
    desc a0 = x walk=(32, 16)
    desc a1 = y walk=(32, {yw})
    vfill a0 -> l1[0]
    vbar
    vunpk v0 <- l1[0] f16
    vunpk v1 <- l1[8] f16
{body}
{pack}
    vsync drain on=pack
    vdrain a1 <- l1[16]
    halt

fn t.l1(x: f16[256] @dram, y: f16[{yn}] @dram)
    send vc[0]
        run t(x, y)
    barrier
"""


def _x(spec_):
    rng = np.random.default_rng(spec_.get("seed", 0))
    n = spec_.get("n", 256)
    if spec_.get("subnormal"):
        x = (rng.integers(1, 1024, n) * 2.0**-24).astype(np.float16)
    else:
        x = rng.uniform(spec_.get("lo", -2.0), spec_.get("hi", 2.0), n).astype(
            np.float16
        )
    for i in spec_.get("zeros", []):
        x[i] = 0
    for i, v in spec_.get("set", {}).items():
        x[int(i)] = v
    return x


def _lanes(x, f):
    return np.array([f(c, ln) for c in range(8) for ln in range(16)], np.float64)


KERNEL = {
    "x0": lambda a, b: a,
    "x1": lambda a, b: b,
    "add": lambda a, b: a + b,
    "sub": lambda a, b: a - b,
    "mul": lambda a, b: a * b,
    "max": lambda a, b: np.maximum(a, b),
    "sel": lambda a, b: np.where(b != 0, a, 0.0),
    "exp2": lambda a, b: np.exp2(a),
    "log2": lambda a, b: np.log2(a),
    "inv": lambda a, b: 1 / a,
    "rsqrt": lambda a, b: 1 / np.sqrt(a),
    "fma": lambda a, b: a * b + b,
    "fnma": lambda a, b: b - a * b,
    "exp2d": lambda a, b: np.exp2(a - np.floor(b)),
    "one_minus": lambda a, b: 1 - a,
    "s_two_three": lambda a, b: 2 * a + 3 * b,
    "half_plus3": lambda a, b: 0.5 * a + 3,
    "plus5": lambda a, b: a + 5,
    "gt": lambda a, b: (a > b).astype(np.float64),
    "lt": lambda a, b: (a < b).astype(np.float64),
    "eq": lambda a, b: (a == b).astype(np.float64),
    "le": lambda a, b: (a <= b).astype(np.float64),
    "neg_sq_add": lambda a, b: -(a * a + b),
    "vl40": lambda a, b: np.concatenate([2 * a[:40], b[40:]]),
    "chunks": lambda a, b: np.concatenate([a[:64] + a[64:], b[64:]]),
    "chunk0": lambda a, b: a * np.tile(b[:16], 8),
    "xor3": lambda a, b: _lanes(a, lambda c, ln: a[16 * c + (ln ^ 3)]),
    "xor8c": lambda a, b: _lanes(a, lambda c, ln: a[16 * c + (ln ^ 8)]),
    "bcast5": lambda a, b: _lanes(a, lambda c, ln: a[(5 + c) % 16]),
    "bcast_sh1": lambda a, b: _lanes(a, lambda c, ln: a[(2 + (c >> 1)) % 16]),
    "merge4": lambda a, b: _lanes(
        a,
        lambda c, ln: (
            a[16 * c + ln] + a[16 * c + (ln ^ 4)]
            if ln & 4 == 0
            else b[16 * c + ln] + b[16 * c + (ln ^ 4)]
        ),
    ),
    "walk3": lambda a, b: np.concatenate([np.zeros(32), a[:48], np.zeros(48)]),
    "scale2_20": lambda a, b: a * 2.0**20,
    "gt4": lambda a, b: np.array(
        [
            np.concatenate([a, b])[16 * (4 * q + w) + 4 * i + j]
            for q in range(2)
            for i in range(4)
            for w in range(4)
            for j in range(4)
        ],
        np.float64,
    ),
    "strided": lambda a, b: np.concatenate([a, b]).reshape(16, 16)[0::2].ravel(),
    "ones": lambda a, b: np.ones(128),
    "zeros": lambda a, b: np.zeros(128),
}


def _close(got, want, tol):
    got, want = np.asarray(got, np.float64), np.asarray(want, np.float64)
    if tol.get("exact"):
        return bool(
            np.array_equal(
                got.astype(np.float16).view(np.uint16),
                want.astype(np.float16).view(np.uint16),
            )
        )
    return bool(
        np.all(
            np.abs(got - want)
            <= tol.get("atol", 0.0) + tol.get("rtol", 2e-3) * np.abs(want)
        )
    )


def _card_run(card, m, fn, inputs, reps=1, programs=None):
    held = card.mark()
    try:
        binds, layout = {}, L1RUN.bufs(m, fn, card.machine_json)
        for p, b in layout:
            data = L1RUN.raw(b, inputs[p]) if p in inputs else bytes(b.nbytes)
            binds[p] = card.put(
                np.frombuffer(data, np.uint8), b.space, b.layout.get("node")
            )
        if programs is None:
            programs = kernel.compile(m, fn, card.machine, level="l1").programs
        progs = programs(binds)
        for _ in range(reps):
            card.run(progs[None] if list(progs) == [None] else progs)
        out = {}
        for p, b in layout:
            data = card.get(binds[p], b.nbytes)
            out[p] = data if b.dtype.startswith("mx7") else b.unpack(data)
            out[p + ".raw"] = bytes(data)
        return out
    finally:
        card.release(held)


@runner("isa.card.kernel")
def card_kernel(inp, ctx):
    card = ctx["session"].dev.card
    mx = inp.get("pack") in ("mx7a", "mx7b")
    text = inp.get("text") or TEMPLATE.format(
        body="\n".join("    " + ln for ln in inp["body"]),
        pack=f"    vpack v2 -> l1[16] {inp.get('pack','f16')}",
        yw=4 if mx else 8,
        yn=64 if mx else 128,
    )
    x = _x(inp.get("x", {}))
    out = _card_run(card, module.read(text), inp.get("fn", "t"), {"x": x})
    x0, x1 = x[:128].astype(np.float64), x[128:].astype(np.float64)
    if mx:
        want = KERNEL[inp["check"]](x0, x1).astype(np.float16).reshape(4, 32)
        group = "gm" if inp["pack"] == "mx7a" else "gn"
        b = Buffer.of(f"{inp['pack']}[4,32] @dram({group}=1,nk=1)", card.machine_json)
        return {"ok": out["y.raw"] == b.pack(want)}
    checks = inp.get("checks", {"y": inp.get("check")})
    return {
        "ok": all(
            _close(out[p], KERNEL[c](x0, x1), inp.get("tol", {}))
            for p, c in sorted(checks.items())
        )
    }


@runner("isa.card.example")
def card_example(inp, ctx):
    card = ctx["session"].dev.card
    name, make = EXAMPLES[inp["case"]]
    programs = None
    if inp["case"].endswith(".l2"):
        c = kernel.compile(
            examples.load(examples.example(name, "l2")), name, card.machine
        )
        m, programs = c.l1, c.programs
    else:
        m = examples.load(examples.example(name))
    ins, ok = make(card.machine_json) if inp["case"] == "convert.l2" else make()
    out = _card_run(card, m, m.names()[0], ins, inp.get("reps", 2), programs)
    return {"ok": bool(ok(out))}


XSIM_ARGS = []


@mutant("isa.bench/short")
def short_bench():
    with patched(sys.modules[__name__], "XSIM_ARGS", ["--max-time", "1ns"]):
        yield


@runner("isa.bench")
def bench(inp, ctx):
    cmd = [sys.executable, str(ROOT / "scripts/py/xsim.py"), inp["bench"], *XSIM_ARGS]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
    log = r.stdout + r.stderr
    passed = r.returncode == 0 and "PASS" in log and "FAIL" not in log
    if not passed:
        print(log)
    return {"pass": passed}
