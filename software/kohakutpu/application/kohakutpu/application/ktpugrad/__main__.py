"""``python -m kohakutpu.application.ktpugrad``: the KTPU device for tinygrad.

--autoload [--site DIR]   write the .pth line that lets DEV=KTPU work in
                          any script of this interpreter, no import needed
--remove [--site DIR]     take it out again
--check                   one small tensor program on KTPU vs numpy
"""

import argparse
import pathlib
import sysconfig

import numpy as np
from tinygrad.tensor import Tensor

from kohakutpu.application.ktpugrad.device import NAME
from kohakutpu.application.runtime import device as D

PTH = "kohakutpu_ktpu.pth"
LINE = "import kohakutpu.application.tinygrad_hook\n"


def site(where: str | None) -> pathlib.Path:
    return pathlib.Path(where or sysconfig.get_paths()["purelib"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kohakutpu.application.ktpugrad")
    ap.add_argument("--autoload", action="store_true")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument(
        "--site",
        default=None,
        help="the site-packages directory (default: this interpreter's)",
    )
    a = ap.parse_args(argv)
    if a.autoload:
        p = site(a.site) / PTH
        p.write_text(LINE, encoding="utf-8")
        print(f"wrote {p}: DEV={NAME} now finds the device in every script")
    if a.remove:
        p = site(a.site) / PTH
        if p.exists():
            p.unlink()
            print(f"removed {p}")
    if a.check:
        rng = np.random.default_rng(0)
        x = rng.standard_normal((64, 128)).astype(np.float16)
        got = Tensor(x, device=NAME).softmax().numpy().astype(np.float64)
        e = np.exp(x - x.max(1, keepdims=True).astype(np.float64))
        err = np.abs(got - e / e.sum(1, keepdims=True)).max()
        runs = D.device().runs
        print(
            f"{NAME} softmax [64, 128]: max abs err {err:.2e}; {len(runs)} kernels, {sum(r.cycles for r in runs)} cycles"
        )
    if not (a.autoload or a.remove or a.check):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
