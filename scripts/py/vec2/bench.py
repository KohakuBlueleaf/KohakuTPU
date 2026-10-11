"""Run one V2 vector program on the V2 core alone (vec_replay_tb under VEC_CORE_V2).

    python scripts/py/vec2/bench.py KERNEL [memwait=0] [rebuild=1] [key=value ...]

KERNEL names a function in `kernels.py`: ``kernel(**kw) -> (build, arrays,
reference)``, `build(addr) -> Kernel2` with `addr[name]` the byte address of
each array, then ``out``. The memory answers one word every memwait + 1
cycles. Prints the core's busy cycles, lane beats, lane use (beats / (16 x
busy)), the error against the reference (per part when the reference names
parts) and every engine's busy and stall counters. VL2_DEFS adds Verilog
defines to the model build (comma separated), VL2_TAG names its build root.
"""

import json
import os
import pathlib
import re
import subprocess
import sys

import kernels
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[3]
HERE = pathlib.Path(__file__).resolve().parent
WORDS = 4096
BUILD = "build/vl2" + ("_" + os.environ["VL2_TAG"] if os.environ.get("VL2_TAG") else "")
VLT_ENV = pathlib.Path(
    os.environ.get(
        "KOHAKU_VLT_ENV",
        pathlib.Path(os.environ.get("USERPROFILE", "~"))
        / "micromamba/envs/vlt/Library",
    )
)


def vsim() -> pathlib.Path:
    work = ROOT / BUILD / "vlt_vec2_replay" / "obj_dir"
    return work / ("vsim.exe" if os.name == "nt" else "vsim")


def build_model() -> None:
    env = dict(os.environ)
    if os.name == "nt":
        env["PATH"] = str(VLT_ENV / "bin") + os.pathsep + env["PATH"]
        env["VERILATOR_ROOT"] = str(VLT_ENV)
    defs = ["VEC_CORE_V2"] + [d for d in os.environ.get("VL2_DEFS", "").split(",") if d]
    p = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/py/vlt.py"),
            "vec2_replay",
            "--native",
            "--keep",
            "--rtl",
            "-j",
            "16",
            "--build-root",
            BUILD,
            *[x for d in defs for x in ("-d", d)],
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if not vsim().exists():
        print(p.stdout[-4000:], p.stderr[-4000:])
        raise SystemExit("vec2_replay did not build")


def run(case: str, opts: dict) -> dict:
    build, arrays, ref = getattr(kernels, case)(
        **{k: v for k, v in opts.items() if k not in ("memwait", "rebuild")}
    )
    addr, at = {}, 0x400
    mem = bytearray(WORDS * 32)
    for name, arr in arrays.items():
        raw = np.ascontiguousarray(arr).tobytes()
        addr[name] = at
        mem[at : at + len(raw)] = raw
        at += -(-len(raw) // 256) * 256 + 256
    want = ref()
    addr["out"] = at
    at += -(-want.size * 2 // 256) * 256 + 256
    if at > WORDS * 32:
        raise SystemExit(f"{at} bytes do not fit the replay memory")
    kern = build(addr)
    words = [f & ((1 << 256) - 1) for f in kern.flits()]
    tag = "_".join(f"{k}{v}" for k, v in sorted(opts.items()))
    run_dir = ROOT / BUILD / "run" / f"{case}_{tag}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prog.hex").write_text("".join(f"{w:064x}\n" for w in words))
    lines = [int.from_bytes(mem[32 * i : 32 * i + 32], "little") for i in range(WORDS)]
    (run_dir / "mem.hex").write_text("".join(f"{v:064x}\n" for v in lines))
    args = [
        f"+{k}={(run_dir / f'{k}.hex').resolve().as_posix()}"
        for k in ("prog", "mem", "out")
    ]
    args.append(f"+memwait={opts.get('memwait', 0)}")
    if opts.get("rebuild") or not vsim().exists():
        build_model()
    done = subprocess.run(
        [str(vsim()), *args],
        cwd=vsim().parent,
        capture_output=True,
        text=True,
        check=False,
    )
    out = done.stdout + done.stderr
    (run_dir / "log.txt").write_text(out)
    rep = re.search(
        r"@@@ REPLAY payloads (\d+) completions (\d+) faults (\d+) fault_arg (\S+)", out
    )
    ela = re.search(r"@@@ ELAPSED (\d+)", out)
    vres = re.search(r"VRES \S+ (.*)", out)
    if not (rep and ela and vres):
        print(out[-3000:])
        raise SystemExit("no replay result")
    kv = vres.group(1).split()
    v = {kv[i]: int(kv[i + 1]) for i in range(0, len(kv) - 1, 2)}
    eng = {}
    for m in re.finditer(r"(V2\w+) vec_replay_tb\S* (.*)", out):
        kv2 = m[2].split()
        eng[m[1]] = {kv2[i]: int(kv2[i + 1]) for i in range(0, len(kv2) - 1, 2)}
    raw = b"".join(
        int(ln, 16).to_bytes(32, "little")
        for ln in (run_dir / "out.hex").read_text().split()
        if ln and not ln.startswith(("@", "//"))
    )
    out_raw = raw[addr["out"] : addr["out"] + want.size * 2]
    errs, at = {}, 0
    if getattr(ref, "exact", False):
        # A bit pattern, not a number: the fraction of 16-bit fields that differ.
        g16 = np.frombuffer(out_raw, np.uint16)
        w16 = np.ascontiguousarray(want).view(np.uint16).reshape(-1)
        bad = g16 != w16
        err = float(bad.mean())
        errs["first_bad_field"] = int(np.argmax(bad)) if bad.any() else -1
    else:
        got = np.frombuffer(out_raw, np.float16).astype(np.float64).reshape(want.shape)
        err = float(np.abs(got - want).max() / max(np.abs(want).max(), 1e-30))
    for name, n in getattr(ref, "parts", []):
        g, w = got.reshape(-1)[at : at + n], want.reshape(-1)[at : at + n]
        errs[name] = float(np.abs(g - w).max() / max(np.abs(w).max(), 1e-30))
        at += n
    warn = sorted(set(re.findall(r"(V2_\w+|VEC_WB_COLLISION)", out)))
    return {
        "case": case,
        "opts": opts,
        "payloads": int(rep[1]),
        "completions": int(rep[2]),
        "faults": int(rep[3]),
        "fault_arg": rep[4],
        "elapsed": int(ela[1]),
        "busy": v["busy"],
        "beats": v["beats"],
        "lanes": v["lanes"],
        "lane": round(v["lanes"] / (16 * v["busy"]), 4) if v["busy"] else 0.0,
        "insns": v["insns"],
        "err": err,
        "errs": errs,
        "info": getattr(ref, "info", {}),
        "imem": len(kern.words),
        "warn": warn,
        "res": v,
        "eng": eng,
    }


def main() -> int:
    case = sys.argv[1]
    opts = {}
    for a in sys.argv[2:]:
        k, val = a.split("=", 1)
        try:
            opts[k] = int(val)
        except ValueError:
            try:
                opts[k] = float(val)
            except ValueError:
                opts[k] = val
    row = run(case, opts)
    print(
        f"{case} {opts}: busy {row['busy']} beats {row['beats']} lane {row['lane']:.1%} "
        f"elapsed {row['elapsed']} insns {row['insns']} err {row['err']:.2e} "
        f"faults {row['faults']} ({row['fault_arg']}) "
        f"completions {row['completions']}/{row['payloads']} warn {row['warn']}"
    )
    if row["errs"] or row["info"]:
        print(f"  parts {row['errs']} info {row['info']} imem {row['imem']}")
    for k, d in row["eng"].items():
        print(f"  {k} {d}")
    log = ROOT / BUILD / "results.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
