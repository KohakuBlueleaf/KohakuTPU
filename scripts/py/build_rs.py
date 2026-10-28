"""Build the Rust stack's Python module (`kohakutpu.compiler._rs`) with
maturin, for one of the two builds; the one place that names their cargo
feature sets and target dirs.

    python scripts/py/build_rs.py spec    # spec tests: python + mutants
    python scripts/py/build_rs.py prod    # production: python only

`spec` carries the stage crates' spec-vector mutants (`_rs.MUTANTS`,
`_rs.compile(mutant=)`); `prod` carries none, and `mutant=` raises there.
The wheel is built first (`maturin build`; a build cut short changes
nothing, and rerunning resumes it), then its module replaces the installed
one, the old moved aside to ``build/rs-old`` (running test workers hold it).
"""

import argparse
import os
import pathlib
import subprocess
import sys
import time
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
COMPILER = ROOT / "software" / "compiler"
PACKAGE = COMPILER / "kohakutpu" / "compiler"
WHEELS = ROOT / "build" / "rs-wheels"
#: Each build's cargo features of crate ktpu, and its own cargo target dir
#: (under build/): short paths fit Windows linker limits; sharing a dir with
#: another invalidates the other's artifacts on every build.
FEATURES = {"spec": ["python", "mutants"], "prod": ["python"]}
TARGET = {"spec": "rs", "prod": "rs-prod"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("build", choices=sorted(FEATURES))
    ap.add_argument("--target-dir", help="default: build/TARGET[build]")
    ap.add_argument("--release", action="store_true")
    a = ap.parse_args()
    env = {k: v for k, v in os.environ.items() if k != "CONDA_PREFIX"}
    env["VIRTUAL_ENV"] = str(ROOT / ".venv")
    target = a.target_dir or ROOT / "build" / TARGET[a.build]
    env["CARGO_TARGET_DIR"] = str(target)
    out = WHEELS / a.build
    cmd = ["maturin", "build", "--features", ",".join(FEATURES[a.build])]
    cmd += ["--out", str(out), "--interpreter", sys.executable]
    if a.release:
        cmd.append("--release")
    t0 = time.time()
    if subprocess.run(cmd, cwd=COMPILER, env=env, check=False).returncode:
        return 1
    wheel = max(out.glob("*.whl"), key=lambda p: p.stat().st_mtime)
    if wheel.stat().st_mtime < t0:
        print(f"no new wheel in {out}", file=sys.stderr)
        return 1
    with zipfile.ZipFile(wheel) as z:
        (member,) = [
            n
            for n in z.namelist()
            if n.startswith("kohakutpu/compiler/_rs.") and n.endswith((".pyd", ".so"))
        ]
        data = z.read(member)
    aside = ROOT / "build" / "rs-old"
    aside.mkdir(parents=True, exist_ok=True)
    for old in [*PACKAGE.glob("_rs.*.pyd"), *PACKAGE.glob("_rs.*.so")]:
        old.rename(aside / f"{old.name}.{time.strftime('%Y%m%d-%H%M%S')}")
    (PACKAGE / pathlib.PurePosixPath(member).name).write_bytes(data)
    print(f"installed {member} from {wheel.name} ({a.build}: {FEATURES[a.build]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
