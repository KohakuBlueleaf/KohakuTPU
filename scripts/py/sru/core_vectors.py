"""Jobs for tests/sru/sru_core_tb.v: a command file the bench executes.

    python scripts/py/sru/core_vectors.py --profile strong|rom|mid|spu|pack

The jobs a profile can run (profiles.py), each pass's program scheduled for
the profile's F_LANES. build/sru/core_<profile>.cmd, one command a line,
`op addr data` in hex:
  0 cfg write   1 table entry   2 column constants   3 row state
  4 start job   5 A word   6 B word   7 expected out word
  8 expected row state after the job   9 end of job

Every job's expectations come from scripts/py/sru/core.py.
"""

import argparse
import pathlib
import random

import cfg as C
import core
import e8
import fn
import numpy as np
import profiles
from kohakutpu.compiler.isa import sru as sru_isa

ROOT = pathlib.Path(__file__).resolve().parents[3]

ONE = 0x3F8000


def e8w(v):
    return e8.encode(e8.Fraction(v).limit_denominator(1 << 40))


def f16(v):
    return int(np.array([v], dtype=np.float16).view(np.uint16)[0])


def rnd_words(rng, n, scale):
    return [[f16(rng.gauss(0, scale)) for _ in range(16)] for _ in range(n)]


def job(name, g, passes, ops=(), fin=()):
    """passes: per pass {P fields}, with "PROG": a list of ops appended to
    the op store (NOPS / OPBASE filled in)."""
    gg = {k: 0 for k in C.G}
    gg.update(g)
    store = list(ops)
    ps = []
    for pp in range(4):
        d = {k: 0 for k in C.P}
        if pp < len(passes):
            src = dict(passes[pp])
            prog = src.pop("PROG", [])
            d.update(src)
            d["OPBASE"], d["NOPS"] = len(store), len(prog)
            store += prog
        ps.append(d)
    assert len(store) <= C.OP_DEPTH
    fins = [{"OP": 0, "DST": 0, "A": 0, "B": 0, "C": 0} for _ in range(C.FIN_DEPTH)]
    for i, f in enumerate(fin):
        fins[i].update(f)
    return {"name": name, "G": gg, "P": ps, "OPS": store, "FIN": fins}


def cfg_words(j, f_lanes, h_taps=2):
    return sru_isa.words(j["G"], j["P"], j["OPS"], j["FIN"], f_lanes, h_taps)


def fma(a="X", b="K", c="K", **kw):
    return dict(KIND=0, A=a, B=b, C=c, **kw)


def fun(a="X", b="K", **kw):
    return dict(KIND=1, A=a, B=b, **kw)


def H(k):
    return ("H", k)


def jobs_list(tabs):
    """(job, table name, uses B, needs) for every job."""
    sig_cs, exp_cs = tabs["sigmoid"][1], tabs["exp2"][1]
    js = []
    # y = x * k + C0 (scale and bias)
    js.append(
        (
            job(
                "scale_bias",
                {"NW": 4, "NSR": 3, "OUT": 1, "ROW0": 0, "COL0": 5},
                [{"EMIT": 1, "PROG": [fma("X", "K", "C", KB=e8w(0.75), CC=0)]}],
            ),
            None,
            False,
            {"POOL"},
        )
    )
    # sigmoid over its whole table and past it: results below FP16's normal
    # range leave as FP16 subnormals
    js.append((job("sig_tail", {"NW": 4, "NSR": 4, "OUT": 1, "ROW0": 4},
                   [{"EMIT": 1, "PROG": [fun("X", "K", FIX=1, CS=sig_cs)]}]),
               "sigmoid", False, {"POOL", "FN_FIX"}))
    # silu = x * sigmoid(x)
    js.append(
        (
            job(
                "silu",
                {"NW": 4, "NSR": 2, "OUT": 1, "ROW0": 8},
                [
                    {
                        "EMIT": 1,
                        "PROG": [
                            fun("X", "K", FIX=1, CS=sig_cs),
                            fma(H(0), "X", "K", KC=0),
                        ],
                    }
                ],
            ),
            "sigmoid",
            False,
            {"POOL", "FN_FIX"},
        )
    )
    # CFG, u + s * (c - u), c on A and u on B
    js.append(
        (
            job(
                "cfg",
                {"NW": 4, "NSR": 2, "OUT": 1, "USE_B": 1, "ROW0": 16},
                [
                    {
                        "EMIT": 1,
                        "PROG": [
                            fma("X", "K", "B", KB=ONE, NC=1),
                            fma(H(0), "K", "B", KB=e8w(4.5)),
                        ],
                    }
                ],
            ),
            None,
            True,
            {"POOL", "B"},
        )
    )
    # flash-like: R1 max, m = max(m, rowmax), P = exp2(x - floor m) to
    # MXFP7 A, l += sum(P)
    js.append(
        (
            job(
                "flash",
                {
                    "NW": 8,
                    "NSR": 3,
                    "OUT": 2,
                    "BUF": 1,
                    "NPASS": 1,
                    "R1": 1,
                    "ROW0": 24,
                },
                [
                    {"FINLEN": 1, "FINBASE": 0},
                    {
                        "R2EN": 1,
                        "EMIT": 1,
                        "FINLEN": 1,
                        "FINBASE": 1,
                        "PROG": [fun("X", "R", RB=0, CS=exp_cs)],
                    },
                ],
                fin=[
                    {"OP": core.MAX, "DST": 0, "A": 0, "B": 4},
                    {"OP": core.ADD, "DST": 1, "A": 1, "C": 5},
                ],
            ),
            "exp2",
            False,
            {"POOL", "FN_EXP", "BUF", "R1", "R2", "MX7"},
        )
    )
    # causal mask (col - row > 0 is -inf) then exp2(x - floor R0)
    js.append(
        (
            job(
                "mask",
                {
                    "NW": 4,
                    "NSR": 2,
                    "OUT": 1,
                    "MASK": 1,
                    "DHI": 0,
                    "CLIM": 0x7FFF,
                    "MVAL": 0xFF8000,
                    "ROW0": 40,
                    "COLOFF": 2,
                },
                [{"EMIT": 1, "PROG": [fun("X", "R", RB=0, CS=exp_cs)]}],
            ),
            "exp2",
            False,
            {"POOL", "FN_EXP", "MASK"},
        )
    )
    # three passes over the buffer: mean, then (x - mean) * C0 + C1 with its sum
    js.append(
        (
            job(
                "norm",
                {
                    "NW": 8,
                    "NSR": 2,
                    "OUT": 1,
                    "BUF": 1,
                    "NPASS": 2,
                    "ROW0": 48,
                    "COL0": 100,
                    "K0": e8w(1 / 32),
                },
                [
                    {},
                    {"R2EN": 1, "FINLEN": 1, "FINBASE": 2},
                    {
                        "R2EN": 1,
                        "EMIT": 1,
                        "FINLEN": 1,
                        "FINBASE": 3,
                        "PROG": [
                            fma("X", "K", "R", KB=ONE, RC=0, NC=1),
                            fma(H(0), "C", "C", CB=0, CC=1),
                        ],
                    },
                ],
                fin=[
                    {},
                    {},
                    {"OP": core.MUL, "DST": 0, "A": 5, "B": 6},
                    {"OP": core.MOV, "DST": 2, "A": 5},
                ],
            ),
            None,
            False,
            {"POOL", "BUF", "R2"},
        )
    )
    # RoPE pair rotation: x*C0 + x[j^1]*C1
    js.append(
        (
            job(
                "rope",
                {"NW": 4, "NSR": 2, "OUT": 1, "ROW0": 60, "COL0": 200},
                [
                    {
                        "EMIT": 1,
                        "PROG": [
                            fma("X", "C", "K", CB=0, KC=0),
                            fma("O", "C", H(0), CB=1),
                        ],
                    }
                ],
            ),
            None,
            False,
            {"POOL"},
        )
    )
    # the format unit alone: f16 through, and MXFP7 B entries
    js.append(
        (
            job("pass_f16", {"NW": 4, "NSR": 2, "OUT": 1, "ROW0": 64}, [{"EMIT": 1}]),
            None,
            False,
            set(),
        )
    )
    js.append(
        (
            job("pass_mx7", {"NW": 8, "NSR": 2, "OUT": 3, "ROW0": 72}, [{"EMIT": 1}]),
            None,
            False,
            {"MX7"},
        )
    )
    # statistics then one op: mean, then (x - mean) with its sum
    js.append(
        (
            job(
                "norm_s1",
                {
                    "NW": 8,
                    "NSR": 2,
                    "OUT": 1,
                    "BUF": 1,
                    "NPASS": 2,
                    "ROW0": 80,
                    "K0": e8w(1 / 32),
                },
                [
                    {},
                    {"R2EN": 1, "FINLEN": 1, "FINBASE": 2},
                    {
                        "R2EN": 1,
                        "EMIT": 1,
                        "FINLEN": 1,
                        "FINBASE": 3,
                        "PROG": [fma("X", "K", "R", KB=ONE, RC=0, NC=1)],
                    },
                ],
                fin=[
                    {},
                    {},
                    {"OP": core.MUL, "DST": 0, "A": 5, "B": 6},
                    {"OP": core.MOV, "DST": 2, "A": 5},
                ],
            ),
            None,
            False,
            {"POOL", "BUF", "R2"},
        )
    )
    # a 4-op chain: both result taps, a skipped result, the partner, R and C
    js.append(
        (
            job(
                "chain4",
                {"NW": 4, "NSR": 3, "OUT": 1, "ROW0": 96, "COL0": 300},
                [
                    {
                        "EMIT": 1,
                        "PROG": [
                            fma("X", "K", "R", KB=e8w(0.5), RC=1),
                            fma(H(0), "X", "C", CC=1),
                            fma(H(1), "R", H(0), RB=2),
                            fma("O", H(2), H(1), NA=1),
                        ],
                    }
                ],
            ),
            None,
            False,
            {"POOL", "H2"},
        )
    )
    # every finalise op, with a negative-zero product (MUL by -0, then MOV of it)
    js.append(
        (
            job(
                "finops",
                {
                    "NW": 8,
                    "NSR": 2,
                    "OUT": 1,
                    "BUF": 1,
                    "NPASS": 1,
                    "R1": 1,
                    "ROW0": 128,
                    "K0": 0x800000,
                    "K1": e8w(0.75),
                    "K2": e8w(-2.5),
                },
                [{}, {"R2EN": 1, "EMIT": 1, "FINLEN": 7, "FINBASE": 0}],
                fin=[
                    {"OP": core.MUL, "DST": 3, "A": 5, "B": 6},
                    {"OP": core.FMA, "DST": 0, "A": 0, "B": 7, "C": 3},
                    {"OP": core.SUB, "DST": 1, "A": 1, "C": 4},
                    {"OP": core.MIN, "DST": 2, "A": 2, "B": 4},
                    {"OP": core.MOV, "DST": 2, "A": 3},
                    {"OP": core.MAX, "DST": 3, "A": 3, "B": 0},
                    {"OP": core.ADD, "DST": 1, "A": 1, "C": 8},
                ],
            ),
            None,
            False,
            {"POOL", "BUF", "R1", "R2"},
        )
    )
    # finalise results that are the stored rows: a signed-zero product, MIN, MAX
    js.append(
        (
            job(
                "finops2",
                {
                    "NW": 8,
                    "NSR": 2,
                    "OUT": 1,
                    "BUF": 1,
                    "NPASS": 1,
                    "R1": 1,
                    "ROW0": 144,
                    "K0": 0x800000,
                },
                [{}, {"R2EN": 1, "EMIT": 1, "FINLEN": 3, "FINBASE": 0}],
                fin=[
                    {"OP": core.MUL, "DST": 2, "A": 5, "B": 6},
                    {"OP": core.MIN, "DST": 1, "A": 1, "B": 4},
                    {"OP": core.MAX, "DST": 0, "A": 0, "B": 4},
                ],
            ),
            None,
            False,
            {"POOL", "BUF", "R1", "R2"},
        )
    )
    # row normalise: sum, R1 = RCP(sum) on lane 0's ROM, then x * R1
    js.append(
        (
            job(
                "rcp_fin",
                {"NW": 8, "NSR": 3, "OUT": 1, "BUF": 1, "NPASS": 2, "ROW0": 160},
                [
                    {},
                    {"R2EN": 1, "FINLEN": 1, "FINBASE": 0},
                    {"EMIT": 1, "PROG": [fma("X", "R", "K", RB=1, KC=0)]},
                ],
                fin=[{"OP": core.RCP, "DST": 1, "A": 5}],
            ),
            None,
            False,
            {"POOL", "BUF", "R2", "FIN_RCP"},
        )
    )
    # RMS norm: mean of squares + eps, R1 = RSQRT, then x * R1 * C0
    js.append(
        (
            job(
                "rsq_fin",
                {
                    "NW": 8,
                    "NSR": 2,
                    "OUT": 1,
                    "BUF": 1,
                    "NPASS": 2,
                    "ROW0": 176,
                    "COL0": 400,
                    "K0": e8w(1 / 32),
                    "K1": e8w(1e-5),
                },
                [
                    {},
                    {"R2EN": 1, "FINLEN": 3, "FINBASE": 0, "PROG": [fma("X", "X", "K", KC=0)]},
                    {
                        "EMIT": 1,
                        "PROG": [fma("X", "R", "K", RB=1, KC=0), fma(H(0), "C", "K", CB=0, KC=0)],
                    },
                ],
                fin=[
                    {"OP": core.MUL, "DST": 3, "A": 5, "B": 6},
                    {"OP": core.ADD, "DST": 3, "A": 3, "C": 7},
                    {"OP": core.RSQRT, "DST": 1, "A": 3},
                ],
            ),
            None,
            False,
            {"POOL", "BUF", "R2", "FIN_RCP", "H2"},
        )
    )
    # every RCP / RSQRT special in the finalise: rows set to 0, -0, inf, -1, tiny
    js.append(
        (
            job(
                "rcp_spec",
                {"NW": 4, "NSR": 2, "OUT": 1, "BUF": 1, "NPASS": 1, "ROW0": 192},
                [{}, {"EMIT": 1, "FINLEN": 2, "FINBASE": 0}],
                fin=[
                    {"OP": core.RCP, "DST": 1, "A": 0},
                    {"OP": core.RSQRT, "DST": 2, "A": 0},
                ],
            ),
            None,
            False,
            {"POOL", "BUF", "FIN_RCP"},
        )
    )
    # elementwise 1/x and rsqrt(x) from the RAM table (MANT / HALF, NEG)
    js.append(
        (
            job(
                "mant_rcp",
                {"NW": 4, "NSR": 2, "OUT": 1, "ROW0": 200},
                [{"EMIT": 1, "PROG": [fun("X", "K", MANT=1, NEG=1, CS=tabs["recip"][1])]}],
            ),
            "recip",
            False,
            {"POOL", "FN_MANT"},
        )
    )
    js.append(
        (
            job(
                "mant_rsq",
                {"NW": 4, "NSR": 2, "OUT": 1, "ROW0": 208},
                [
                    {
                        "EMIT": 1,
                        "PROG": [fun("X", "K", MANT=1, FIX=1, NEG=1, CS=tabs["rsqrt"][1])],
                    }
                ],
            ),
            "rsqrt",
            False,
            {"POOL", "FN_MANT"},
        )
    )
    # past FP16's top: products that round over 65504 (saturate either form)
    js.append(
        (
            job(
                "sat",
                {"NW": 4, "NSR": 2, "OUT": 1, "ROW0": 216},
                [{"EMIT": 1, "PROG": [fma("X", "K", "K", KB=e8w(4096.0), KC=0)]}],
            ),
            None,
            False,
            {"POOL"},
        )
    )
    # function and FMA ops interleaved in one program
    js.append(
        (
            job(
                "mixed",
                {"NW": 4, "NSR": 2, "OUT": 1, "ROW0": 112, "COL0": 320},
                [
                    {
                        "EMIT": 1,
                        "PROG": [
                            fma("X", "K", "K", KB=e8w(0.25), KC=e8w(-1.0)),
                            fun(H(0), "R", RB=0, CS=exp_cs, PRE=1),
                            fma(H(1), H(0), "C", CC=0),
                        ],
                    }
                ],
            ),
            "exp2",
            False,
            {"POOL", "FN_EXP", "H2"},
        )
    )
    return js


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="strong", choices=list(profiles.PROFILES))
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    prof = profiles.PROFILES[args.profile]
    has = profiles.features(args.profile)
    rom = prof["TAB"] == "rom"

    tabs = {n: b() for n, b in fn.TABLES.items()}
    cols = [
        [(f16(rng.gauss(0, 1)), f16(rng.gauss(1, 0.2))) for _ in range(4)]
        for _ in range(512)
    ]
    rows = [
        [e8w(rng.gauss(0, 2)), e8w(rng.gauss(0, 1)), e8w(rng.gauss(0, 1))]
        for _ in range(512)
    ]
    # rcp_spec's R0: +0, -0, inf, -1, the smallest normal, 3, -0.5, NaN
    for i, v in enumerate(
        [0, 0x800000, 0xFF << 15, e8w(-1.0), 1 << 15, e8w(3.0), e8w(-0.5), e8.NAN]
    ):
        rows[192 + i][0] = v
    ieee = bool(prof["F16_IEEE"])
    f2e = core.f2e(ieee)

    lines = []

    def emit(op, a, d):
        lines.append(f"{op:x} {a:x} {d:x}")

    for i, cw in enumerate(cols):
        emit(
            2,
            i,
            sum(
                (
                    (f2e(cw[c][0]) | (f2e(cw[c][1]) << 24))
                    << (48 * c)
                )
                for c in range(4)
            ),
        )
    for i, r in enumerate(rows):
        emit(3, i, r[0] | (r[1] << 24) | (r[2] << 48))

    loaded = None
    for j, tname, use_b, needs in jobs_list(tabs):
        if not needs <= has:
            continue
        if rom and tname:
            table = fn.exp2_table(128)
        elif tname:
            table = tabs[tname]
            if tname != loaded:
                for i, v in enumerate(tabs[tname][0]):
                    emit(1, i, v)
                loaded = tname
        else:
            table = None
        nw, nsr = j["G"]["NW"], j["G"]["NSR"]
        scale = {"flash": 4.0, "mask": 4.0, "sig_tail": 8.0, "sat": 24.0}.get(j["name"], 1.5)
        a = rnd_words(rng, nw * nsr, scale)
        b = rnd_words(rng, nw * nsr, 1.0) if use_b else []
        outs = core.run(j, a, b, table, cols, rows, rom=rom, ieee=ieee)
        for a_, v in cfg_words(j, prof["F_LANES"] or 16, prof["H_TAPS"]).items():
            emit(0, a_, v)
        emit(4, 0, 0)
        for wd in a:
            emit(5, 0, core.pack16(wd))
        for wd in b:
            emit(6, 0, core.pack16(wd))
        for o in outs:
            emit(7, 0, o)
        r0 = j["G"]["ROW0"]
        for i in range(r0, r0 + 4 * nsr):
            emit(8, i, rows[i][0] | (rows[i][1] << 24) | (rows[i][2] << 48))
        emit(9, 0, 0)
        print(f"  {j['name']:10s} {len(a)} words in, {len(outs)} out")

    out = ROOT / "build" / "sru"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"core_{args.profile}.cmd").write_text("\n".join(lines) + "\n")
    print(f"{out / ('core_' + args.profile + '.cmd')} {len(lines)} commands")


if __name__ == "__main__":
    main()
