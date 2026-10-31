"""The Sv39 back-to-back access defect and the memory-gap pass (model host).

    cd software/os/tests/host
    KOS_BUILD=~/kos/build pytest --rootdir . test_xlat.py -n 1

tests/xlat runs each case in S with satp on and in M. Built plainly, the four
back-to-back cases differ (the defect is in the RTL); built through
tools/memgap.py, none does (the pass is what translated code relies on).
"""

import os
import pathlib
import re
import subprocess
import sys

import pytest

OS = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(OS / "tools"))

import build
import memgap

BUILD = pathlib.Path(os.environ.get("KOS_BUILD", pathlib.Path.home() / "kos/build"))
XLAT = OS / "tests/xlat"
CFLAGS = [
    "--target=riscv64-unknown-elf",
    "-march=rv64ima_zicsr_zifencei",
    "-mabi=lp64",
    "-mcmodel=medany",
    "-O2",
    "-ffreestanding",
    "-nostdlib",
    "-fno-builtin",
]

pytestmark = pytest.mark.skipif(
    not (BUILD / "core2/obj_dir/vsim").exists(), reason="no core models"
)


def image(tmp: pathlib.Path, gap: bool) -> pathlib.Path:
    asm = tmp / "xlat.s"
    subprocess.run(
        [build.tool("clang"), *CFLAGS, "-S", str(XLAT / "xlat.c"), "-o", str(asm)],
        check=True,
    )
    if gap:
        asm.write_text(memgap.apply(asm.read_text())[0])
    elf = tmp / ("xlat_gap.elf" if gap else "xlat.elf")
    link = [
        build.tool("clang"),
        *CFLAGS,
        "-fuse-ld=lld",
        "-T",
        str(XLAT / "xlat.ld"),
        str(asm),
        "-o",
        str(elf),
    ]
    subprocess.run(link, check=True)
    return elf


def differing(elf: pathlib.Path) -> tuple[int, str]:
    vsim = BUILD / "core2/obj_dir/vsim"
    args = [str(vsim), "--elf-N", "0", str(elf), "--max-cycles", "2000000"]
    p = subprocess.run(args, capture_output=True, text=True, check=False)
    m = re.search(r"@ xlat\.cases_differing (\d+)", p.stdout)
    assert m, p.stdout[-2000:]
    return int(m[1]), p.stdout


def test_defect_reproduces(tmp_path):
    n, out = differing(image(tmp_path, gap=False))
    assert n == 4, out
    for case in ("dram,spad", "dram,dram,dram", "dram,st spad", "st dram,dram"):
        assert f"DIFF {case} " in out


def test_memgap_pass_removes_it(tmp_path):
    n, out = differing(image(tmp_path, gap=True))
    assert n == 0, out


def test_memgap_pass_shape():
    text = "\tld a0, 0(a1)\n\tsd a0, 8(a1)\n.L1:\n\tld a2, 0(a3)\n\taddi a2, a2, 1\n\tsw a2, 0(a3)\n"
    out, added = memgap.apply(text)
    assert added == 2
    assert out.split("\n")[:6] == [
        "\tld a0, 0(a1)",
        "\tnop",
        "\tsd a0, 8(a1)",
        ".L1:",
        "\tnop",
        "\tld a2, 0(a3)",
    ]
