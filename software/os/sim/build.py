"""Build the core-only cluster model: NODES system nodes, no compute units.

    python software/os/sim/build.py --nodes 4 --shared 1 --out build/kos/core4s

The RTL list is xsim.py's `rv64_node_pair` bench (imported, never copied) with
its top swapped for `rtl/kos_cluster.v`; the flags and the model wrappers are
scripts/py/vlt.py's. The model is `<out>/obj_dir/vsim`, driven by
`harness/kos_cluster_main.cpp`.
"""

import argparse
import importlib.util
import pathlib
import shutil
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
TOP = "kos_cluster"
PAIR_TOP = "src/kohakuaccel/verif/rv64_node_pair.v"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# xsim.py imports its siblings (gen_card) by name.
sys.path.insert(0, str(ROOT / "scripts/py"))
vlt = _load("vlt", ROOT / "scripts/py/vlt.py")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", type=int, default=2)
    ap.add_argument("--shared", type=int, choices=(0, 1), default=0)
    ap.add_argument(
        "--dram-words", type=int, default=65536, help="64-byte words per RAM"
    )
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", "-j", type=int, default=4)
    ap.add_argument("--opt-fast", default="-O3")
    a = ap.parse_args()
    if not 1 <= a.nodes <= 4:
        raise SystemExit("the chain holds 1..4 nodes (two mesh-id bits)")

    xsim = vlt.load_xsim()
    srcs = [s for s in xsim.BENCHES["rv64_node_pair"][1] if s != PAIR_TOP]
    files = [ROOT / p for p in dict.fromkeys(srcs)] + [HERE / "rtl" / f"{TOP}.v"]
    work = pathlib.Path(a.out)
    if not work.is_absolute():
        work = ROOT / work
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    predef = work / "kohaku_predef.vh"
    predef.write_text(
        f'`define PE_DIR "{(ROOT / "tests/pe/build").as_posix()}"\n'
        f'`define SRU_DIR "{(ROOT / "build/sru").as_posix()}"\n',
        encoding="utf-8",
    )
    files, model_inc = vlt.apply_models(files, work)
    lines = [vlt.to_native(p) for p in sorted(vlt.SHIMS.glob("*.v"))]
    lines += [vlt.to_native(predef)]
    lines += [f"-I{vlt.to_native(ROOT / d)}" for d in xsim.INCDIRS]
    lines += [f"-I{vlt.to_native(d)}" for d in dict.fromkeys(model_inc)]
    lines += [vlt.to_native(p) for p in files]
    (work / "vlt.f").write_text("\n".join(lines) + "\n", encoding="utf-8")

    defines = xsim.DEFINES.get("rv64_node_pair", []) + ["MX_MODEL=1"]
    cxx = (
        f"-march=native -std=c++17 -DVTOP=V{TOP} -DKOS_NODES={a.nodes} "
        f"-DKOS_SHARED={a.shared} -DKOS_DRAM_WORDS={a.dram_words}"
    )
    cmd = vlt.native_cmd(a.opt_fast)
    cmd += ["-sv", "--no-timing", "-Wno-fatal", "--timescale", "1ns/1ps"]
    cmd += ["--cc", "--exe", "--build", "-o", "vsim", "-CFLAGS", cxx, "-j", str(a.jobs)]
    cmd += [f"-Wno-{w}" for w in vlt.SILENCED]
    cmd += [f"+define+{d}" for d in defines]
    cmd += [
        f"-GNODES={a.nodes}",
        f"-GSHARED={a.shared}",
        f"-GDRAM_WORDS={a.dram_words}",
    ]
    cmd += [
        "--top-module",
        TOP,
        "-f",
        "vlt.f",
        str(HERE / "harness" / f"{TOP}_main.cpp"),
    ]

    t0 = time.monotonic()
    bp = subprocess.run(
        cmd, cwd=work, check=False, capture_output=True, text=True, errors="replace"
    )
    log = (bp.stdout or "") + (bp.stderr or "")
    (work / "build.log").write_text(log, encoding="utf-8")
    for ln in log.splitlines():
        if ln.startswith("%Error") or " error:" in ln:
            print(ln)
    vsim = work / "obj_dir" / "vsim"
    ok = vsim.exists()
    print(
        f"{TOP} nodes={a.nodes} shared={a.shared} words={a.dram_words}: "
        f"{'built' if ok else 'FAILED'} in {time.monotonic() - t0:.0f}s -> {vsim}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
