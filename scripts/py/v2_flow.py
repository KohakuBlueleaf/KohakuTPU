"""Build and run `v2_flow_tb` (tests/vector2/v2_flow_tb.v) on Verilator.

The program waits for 8 landed granules, drains them to memory, waits for 4
credits (3 sent before the RUN), drains again and posts a CREDIT of 300. The
control program drops the first wait: the bench must then FAIL, which proves
its data check reads what the wait protects.

    python scripts/py/v2_flow.py [--build-root build/v2flow]
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys

from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF

ROOT = pathlib.Path(__file__).resolve().parents[2]
PAYLOAD = (1 << 256) - 1
VLT_ENV = (
    pathlib.Path(os.environ.get("USERPROFILE", "~")) / "micromamba/envs/vlt/Library"
)


def program(control: bool) -> list:
    machine = (MF.MACHINES / "multimesh_v9t8.json").read_text(encoding="utf-8")
    first = (
        {"Sync": {"waiter": 0, "on": 0}}
        if control
        else {"Wait": {"waiter": 0, "counter": 0, "n": 8}}
    )
    instructions = [
        first,
        {"Drain": {"ad": 0, "l1off": 256}},
        {"Wait": {"waiter": 6, "counter": 1, "n": 4}},
        {"Drain": {"ad": 1, "l1off": 256}},
        {"Post": {"dst": [5, 6], "n": 300}},
        {"Sync": {"waiter": 0, "on": 4}},
        "Halt",
    ]
    words = _rs.vc_encode(json.dumps(instructions), machine)
    text = """target ktpu.v9
image flow()
    desc a0 = 4096 walk=(32, 8)
    desc a1 = 8192 walk=(32, 4)
    halt
"""
    _, descs = _rs.image(text, "flow", [], machine)
    return _rs.vc_flits(descs, words, 0, machine)


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
