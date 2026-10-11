"""Build and run `v2_flow_tb` (tests/vector2/v2_flow_tb.v) on Verilator.

The program waits for 8 landed granules, drains them to memory, waits for 4
credits (3 sent before the RUN), drains again and posts a CREDIT of 300. The
control program drops the first wait: the bench must then FAIL, which proves
its data check reads what the wait protects.

    python scripts/py/v2_flow.py [--build-root build/v2flow]
"""

import argparse
import os
import pathlib
import subprocess
import sys

from kohakutpu.compiler.encode import vector as A

ROOT = pathlib.Path(__file__).resolve().parents[2]
PAYLOAD = (1 << 256) - 1
VLT_ENV = (
    pathlib.Path(os.environ.get("USERPROFILE", "~")) / "micromamba/envs/vlt/Library"
)


def program(control: bool) -> list:
    first = A.vsync(A.W_FE, on=A.K_FILL) if control else A.vwait(A.W_FE, A.RX, 8)
    words = [
        first,
        A.vdrain(0, 256),
        A.vwait(A.W_D, A.CR, 4),
        A.vdrain(1, 256),
        A.vpost((5, 6), 300),
        A.vsync(A.W_FE, on=A.K_DRAIN),
        A.vhalt(),
    ]
    flits = [A.desc_flit(0, 0, 0x1000), A.desc_flit(0, 1, A.dim(32, 8))]
    flits += [A.desc_flit(0, f, 0) for f in (2, 3, 4)]
    flits += [A.desc_flit(1, 0, 0x2000), A.desc_flit(1, 1, A.dim(32, 4))]
    flits += [A.desc_flit(1, f, 0) for f in (2, 3, 4)]
    flits += [A.imem_flit(i, w) for i, w in enumerate(words)] + [A.run_flit(0)]
    return [f & PAYLOAD for f in flits]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-root", default="build/v2flow")
    a = ap.parse_args()
    env = dict(os.environ)
    if os.name == "nt":
        env["PATH"] = str(VLT_ENV / "bin") + os.pathsep + env["PATH"]
        env["VERILATOR_ROOT"] = str(VLT_ENV)
    cmd = [
        sys.executable,
        str(ROOT / "scripts/py/vlt.py"),
        "v2_flow",
        "--native",
        "--keep",
        "--rtl",
        "-j",
        "16",
        "--build-root",
        a.build_root,
        "-d",
        "VEC_CORE_V2",
    ]
    subprocess.run(cmd, cwd=ROOT, env=env, check=False, capture_output=True, text=True)
    exe = (
        ROOT
        / a.build_root
        / "vlt_v2_flow"
        / "obj_dir"
        / ("vsim.exe" if os.name == "nt" else "vsim")
    )
    if not exe.exists():
        print(f"v2_flow_tb did not build: {exe}")
        return 1
    ok = True
    for control in (False, True):
        hexf = ROOT / a.build_root / f"prog_{'control' if control else 'flow'}.hex"
        hexf.write_text("".join(f"{w:064x}\n" for w in program(control)))
        p = subprocess.run(
            [str(exe), f"+prog={hexf.as_posix()}"],
            cwd=exe.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        out = p.stdout + p.stderr
        passed = "  PASS -- v2_flow_tb" in out
        tag = "control" if control else "flow"
        print(f"--- {tag}")
        print(
            "\n".join(
                ln
                for ln in out.splitlines()
                if "@@@" in ln or "ERROR" in ln or "PASS" in ln or "FAIL" in ln
            )
        )
        ok &= passed != control
    print("PASS -- v2_flow (flow passes, control fails)" if ok else "FAIL -- v2_flow")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
