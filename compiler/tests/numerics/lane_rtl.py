"""`kohakutpu.model.lane` against `vec_alu.v` under Verilator, bit for bit.

    python compiler/tests/numerics/lane_rtl.py -o build/lane_rtl [--golden NPZ]

Builds `tests/vector/vec_alu_dump_tb.v` (Verilator in WSL, as
`scripts/py/vlt.py` runs it), streams a dense operand sweep through the ALU,
and diffs every result word and compare bit against the model. `--golden`
keeps a subset as the regression vectors `test_lane_seeds.py` checks: per
opcode, the words where the model's exactly-rounded predecessor disagrees with
the RTL first, then a random draw.
"""

import argparse
import importlib.util
import json
import pathlib
import shlex
import subprocess
import sys
import time

import numpy as np
from kohakutpu.model import ALU_OP, LANE_PRED, e8_value, lane, to_e8m15

ROOT = pathlib.Path(__file__).resolve().parents[3]
TB = "tests/vector/vec_alu_dump_tb.v"
TOP = "vec_alu_dump_tb"
#: The sim rule: a run that has not finished in five minutes is killed.
RUN_LIMIT = 300

#: Zeros, infinities, NaNs, one, the extreme normals.
SPECIAL = np.array(
    [0x000000, 0x800000, 0x7F8000, 0xFF8000, 0x7FC000, 0xFFC000, 0x3F8000]
    + [0xBF8000, 0x008000, 0x808000, 0x7F7FFF, 0xFF7FFF, 0x400000, 0x3FFFFF],
    np.int64,
)
FMA_OPS = ("VADD", "VSUB", "VMUL", "VFMA", "VFNMA")
MOVE_OPS = ("VMOV", "VNEG", "VABS", "VMAX", "VMIN", "VSEL") + LANE_PRED

#: What each opcode computes in exact arithmetic, for choosing golden vectors.
EXACT = {
    "VADD": lambda a, b, c: a + c,
    "VSUB": lambda a, b, c: a - c,
    "VMUL": lambda a, b, c: a * b,
    "VFMA": lambda a, b, c: a * b + c,
    "VFNMA": lambda a, b, c: c - a * b,
    "VEXP2": lambda a, b, c: np.exp2(a),
    "VLOG2": lambda a, b, c: np.log2(a),
    "VINV": lambda a, b, c: 1.0 / a,
    "VRSQRT": lambda a, b, c: 1.0 / np.sqrt(a),
}


def load(name: str):
    """A `scripts/py` module, with its directory importable as its siblings expect."""
    if str(ROOT / "scripts/py") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts/py"))
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/py/{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def word(s, e, m):
    return (np.asarray(s, np.int64) << 23) | (np.asarray(e, np.int64) << 15) | m


def rand(rng, n, e_lo=1, e_hi=254, sparse=False):
    """`n` random words, exponents in [e_lo, e_hi]; `sparse` keeps few M bits."""
    m = rng.integers(0, 1 << 15, n)
    if sparse:
        m &= rng.integers(0, 1 << 15, n) & rng.integers(0, 1 << 15, n)
    return word(rng.integers(0, 2, n), rng.integers(e_lo, e_hi + 1, n), m)


def grid():
    """Every (a, b, c) of the specials."""
    a, b, c = np.meshgrid(SPECIAL, SPECIAL, SPECIAL, indexing="ij")
    return a.ravel(), b.ravel(), c.ravel()


def sweep(seed: int = 0) -> dict:
    """Per opcode, the operand words ``(a, b, c)`` the sweep issues."""
    rng = np.random.default_rng(seed)
    out = {}
    m_all = np.arange(1 << 15)
    # exp2: |x| from 2^-27 to 512 at every 16th mantissa, both signs, and the
    # whole word space at random.
    e = np.repeat(np.arange(100, 137), 1 << 11)
    m = np.tile(np.arange(0, 1 << 15, 16), 37)
    a = np.concatenate([word(0, e, m), word(1, e, m), rand(rng, 20000), SPECIAL])
    out["VEXP2"] = (a, 0 * a, 0 * a)
    # log2/inv/rsqrt: every mantissa over two octave pairs (rsqrt's parity),
    # then random words of both signs.
    for op in ("VLOG2", "VINV", "VRSQRT"):
        e = np.repeat(np.arange(126, 130), 1 << 15)
        a = np.concatenate([word(0, e, np.tile(m_all, 4)), rand(rng, 20000), SPECIAL])
        out[op] = (a, 0 * a, 0 * a)
    # The FMA family: exponents near 1.0, the addend walked across the whole
    # alignment range around the product; sparse mantissas to reach ties.
    for op in FMA_OPS:
        parts = []
        for sparse in (False, True):
            n = 30000
            a = rand(rng, n, 97, 157, sparse)
            b = rand(rng, n, 97, 157, sparse)
            ec = (
                ((a >> 15) & 0xFF) + ((b >> 15) & 0xFF) - 127 + rng.integers(-50, 51, n)
            )
            c = word(rng.integers(0, 2, n), np.clip(ec, 1, 254), rand(rng, n) & 0x7FFF)
            if sparse:
                c &= ~(rng.integers(0, 1 << 15, n) & rng.integers(0, 1 << 15, n))
            parts.append((a, b, c))
        full = rand(rng, 6000), rand(rng, 6000), rand(rng, 6000)
        parts += [full, grid()]
        out[op] = tuple(np.concatenate(p) for p in zip(*parts))
    for op in MOVE_OPS:
        n = 10000
        a, b = rand(rng, n), rand(rng, n)
        # Equal magnitudes and a zero select often enough to matter.
        b[: n // 4] = a[: n // 4] ^ (rng.integers(0, 2, n // 4) << 23)
        c = np.where(rng.integers(0, 2, n) == 1, rand(rng, n), 0)
        g = grid()
        out[op] = tuple(np.concatenate([x, y]) for x, y in zip((a, b, c), g))
    return out


def build(work: pathlib.Path) -> pathlib.Path:
    """Verilate the dump bench into `work`; the binary's path."""
    vlt, xsim = load("vlt"), load("xsim")
    work.mkdir(parents=True, exist_ok=True)
    srcs = [s for s in xsim.BENCHES["vec_alu"][1] if not s.startswith("tests/")]
    files = [vlt.to_wsl(p) for p in sorted(vlt.SHIMS.glob("*.v"))]
    files += [vlt.to_wsl(ROOT / s) for s in srcs + [TB]]
    (work / "vlt.f").write_text("\n".join(files) + "\n", encoding="utf-8")
    cmd = ["verilator", "-MAKEFLAGS", vlt.opt_make("-O3"), "-sv", "--timing"]
    cmd += ["-Wno-fatal", "--timescale", "1ns/1ps", "--binary", "-o", "vsim", "-j", "8"]
    cmd += [f"-Wno-{w}" for w in vlt.SILENCED] + ["+define+MX_MODEL=1"]
    cmd += ["--top-module", TOP, "-f", "vlt.f"]
    wsl(f"cd {vlt.to_wsl(work)} && {shlex.join(cmd)}", work / "build.log", 1200)
    exe = work / "obj_dir" / "vsim"
    if not exe.exists():
        sys.exit(f"the bench did not build: {work / 'build.log'}")
    return exe


def wsl(cmd: str, log: pathlib.Path, limit: int) -> str:
    p = subprocess.run(
        ["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-lc", cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=limit,
        check=False,
    )
    text = (p.stdout or "") + (p.stderr or "")
    log.write_text(text, encoding="utf-8")
    return text


def run(exe: pathlib.Path, ops, a, b, c, work: pathlib.Path):
    """The RTL's ``(out, pred)`` for each instruction."""
    vlt = load("vlt")
    code = np.array([ALU_OP[o] for o in ops], np.int64)
    w = [
        (int(o) << 72) | (int(x) << 48) | (int(y) << 24) | int(z)
        for o, x, y, z in zip(code, a, b, c)
    ]
    (work / "in.hex").write_text("".join(f"{v:020x}\n" for v in w), encoding="ascii")
    inv = f"+in={vlt.to_wsl(work / 'in.hex')} +out={vlt.to_wsl(work / 'out.txt')} +n={len(w)}"
    t = time.monotonic()
    text = wsl(
        f"cd {vlt.to_wsl(work)} && ./obj_dir/vsim {inv}", work / "run.log", RUN_LIMIT
    )
    print(f"  sim {time.monotonic() - t:.1f}s: {text.strip().splitlines()[-1]}")
    rows = (work / "out.txt").read_text(encoding="ascii").split()
    out = np.array([int(v, 16) for v in rows[0::2]], np.int64)
    return out, np.array([v == "1" for v in rows[1::2]])


def golden_pick(op, a, b, c, rtl, rng, n=256):
    """Up to `n` words exact-then-round gets wrong, then `n` random; and how
    many words exact-then-round gets wrong (by value: it has no -0 rule)."""
    pick = np.zeros(len(a), bool)
    wrong = np.zeros(0, np.int64)
    if op in EXACT:
        with np.errstate(all="ignore"):
            va, vb, vc = e8_value(a), e8_value(b), e8_value(c)
            old = np.float32(to_e8m15(EXACT[op](va, vb, vc)))
        new = e8_value(rtl).astype(np.float32)
        wrong = np.flatnonzero(~((old == new) | (np.isnan(old) & np.isnan(new))))
        pick[rng.permutation(wrong)[:n]] = True
    pick[rng.permutation(len(a))[:n]] = True
    return pick, len(wrong)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--golden", help="write the regression vectors here (.npz)")
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args(argv)
    work = pathlib.Path(args.out).resolve()
    exe = work / "obj_dir" / "vsim"
    if args.rebuild or not exe.exists():
        exe = build(work)
    cases = sweep(args.seed)
    ops = [o for o, (a, _, _) in cases.items() for _ in range(len(a))]
    a, b, c = (np.concatenate([v[k] for v in cases.values()]) for k in range(3))
    rtl, rtl_p = run(exe, ops, a, b, c, work)
    if len(rtl) != len(a):
        sys.exit(f"the RTL retired {len(rtl)} of {len(a)}")
    report, golden, at = {}, {k: [] for k in "oabcrp"}, 0
    rng = np.random.default_rng(args.seed + 1)
    for op, (oa, ob, oc) in cases.items():
        sl = slice(at, at + len(oa))
        at += len(oa)
        got, pred = lane(op, oa, ob, oc)
        bad = got != rtl[sl]
        if op in LANE_PRED:
            bad |= pred != rtl_p[sl]
        for i in np.flatnonzero(bad)[:4]:
            print(
                f"  {op} a={oa[i]:06x} b={ob[i]:06x} c={oc[i]:06x}"
                f" rtl={rtl[sl][i]:06x} model={got[i]:06x}"
            )
        pick, exact = golden_pick(op, oa, ob, oc, rtl[sl], rng)
        report[op] = {"n": len(oa), "ndiff": int(bad.sum()), "exact_ndiff": exact}
        for k, v in zip(
            "oabcrp", (np.full(len(oa), ALU_OP[op]), oa, ob, oc, rtl[sl], rtl_p[sl])
        ):
            golden[k].append(v[pick])
    for op, r in report.items():
        print(
            f"  {op:<7} {r['n']:>8} words   ndiff {r['ndiff']}"
            f"   exact-then-round ndiff {r['exact_ndiff']}"
        )
    (work / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    if args.golden:
        np.savez_compressed(
            args.golden,
            **{
                k: np.concatenate(v).astype(np.uint32 if k != "p" else bool)
                for k, v in golden.items()
            },
        )
        print(f"  golden {sum(len(v) for v in golden['o'])} vectors -> {args.golden}")
    return 1 if any(r["ndiff"] for r in report.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
