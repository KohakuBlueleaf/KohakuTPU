"""The core-only cluster models (os/sim/build.py) under small bare programs:
what only hardware can say, in as few cycles as say it. `KOS_BUILD` holds the
models, `KOS_LLVM` a clang and ld.lld that reach riscv64."""

import os
import pathlib
import re
import subprocess
import sys

import pytest

SOFTWARE = pathlib.Path(__file__).resolve().parents[2]
OS = SOFTWARE / "kohakuaccel" / "os"
PROGRAMS = pathlib.Path(__file__).resolve().parent / "programs"
BUILD = pathlib.Path(os.environ.get("KOS_BUILD", pathlib.Path.home() / "kos/build"))
sys.path.insert(0, str(OS / "tools"))

import build as os_build
import memgap
from boot import load
from run_core import run

CFLAGS = [
    "--target=riscv64-unknown-elf",
    "-march=rv64ima_zicsr_zifencei",
    "-mabi=lp64",
    "-mcmodel=medany",
    "-mno-relax",
    "-O2",
    "-ffreestanding",
    "-nostdlib",
    "-fno-builtin",
    "-fuse-ld=lld",
    f"-I{OS / 'include'}",
    f"-I{OS.parent / 'firmware' / 'include'}",
]


def clang(*args):
    subprocess.run([os_build.tool("clang"), *CFLAGS, *args], check=True)


def measures(text):
    """`@ name value` lines of a run's console, the node's prefix dropped."""
    return {k: int(v) for k, v in re.findall(r"@ (\S+) (-?\d+)", text)}


def core_model(*args):
    vsim = BUILD / "core2" / "obj_dir" / "vsim"
    cmd = [str(vsim), *[str(a) for a in args]]
    return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout


@pytest.fixture(scope="session")
def m0(tmp_path_factory):
    """The hardware questions, one image on every node (programs/m0/m0.c), all
    of it through the memory-gap pass as every translated program must be."""
    src = PROGRAMS / "m0"
    out = tmp_path_factory.mktemp("m0")
    elf = out / "m0.elf"
    gapped = []
    for c in (
        src / "crt.S",
        src / "m0.c",
        OS / "lib" / "string.c",
        OS / "lib" / "fmt.c",
    ):
        asm = out / f"{c.stem}.s"
        clang("-S" if c.suffix == ".c" else "-E", str(c), "-o", str(asm))
        asm.write_text(memgap.apply(asm.read_text())[0])
        gapped.append(str(asm))
    clang("-T", str(src / "m0.ld"), *gapped, "-o", str(elf))
    return elf


@pytest.fixture
def probe(m0):
    def go(board, cmd, cycles):
        return run(load(board), BUILD / board, m0, cmd, 0, 0, cycles)

    return go


@pytest.fixture
def gapped(tmp_path):
    """A translated program, optionally through the memory-gap pass."""

    def image(gap):
        asm = tmp_path / "xlat.s"
        src = PROGRAMS / "xlat"
        clang("-S", str(src / "xlat.c"), "-o", str(asm))
        if gap:
            asm.write_text(memgap.apply(asm.read_text())[0])
        elf = tmp_path / f"xlat{int(gap)}.elf"
        clang("-T", str(src / "xlat.ld"), str(asm), "-o", str(elf))
        return elf

    return image
