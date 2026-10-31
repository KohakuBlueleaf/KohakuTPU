"""The sig word's slot field runs a cycle ahead of its count (model host).

    cd software/os/tests/host
    KOS_BUILD=~/kos/build pytest --rootdir . test_sigslot.py -n 1

tests/sigslot on core2: the first word naming the selected slot carries the
previous slot's count every time, the next word never does — which is why
hw_slot reads once more.
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

BUILD = pathlib.Path(os.environ.get("KOS_BUILD", pathlib.Path.home() / "kos/build"))
SRC = OS / "tests/sigslot/sigslot.c"
CFLAGS = [
    "--target=riscv64-unknown-elf",
    "-march=rv64ima_zicsr_zifencei",
    "-mabi=lp64",
    "-mcmodel=medany",
    "-O2",
    "-ffreestanding",
    "-nostdlib",
    "-fno-builtin",
    "-fuse-ld=lld",
    "-T",
    str(OS / "tests/xlat/xlat.ld"),
]

pytestmark = pytest.mark.skipif(
    not (BUILD / "core2/obj_dir/vsim").exists(), reason="no core models"
)


def test_first_matching_word_carries_the_old_count(tmp_path):
    a, b = tmp_path / "a.elf", tmp_path / "b.elf"
    subprocess.run(
        [build.tool("clang"), *CFLAGS, "-DROLE_A", str(SRC), "-o", str(a)], check=True
    )
    subprocess.run([build.tool("clang"), *CFLAGS, str(SRC), "-o", str(b)], check=True)
    vsim = BUILD / "core2/obj_dir/vsim"
    args = [
        str(vsim),
        "--elf-N",
        "0",
        str(a),
        "--elf-N",
        "1",
        str(b),
        "--max-cycles",
        "400000",
    ]
    out = subprocess.run(args, capture_output=True, text=True, check=False).stdout
    m = {k: int(v) for k, v in re.findall(r"@ slot\.(\S+) (\d+)", out)}
    assert m.get("selections") == 64, out
    assert m["first_matching_word_wrong"] == 64, out
    assert m["next_word_wrong"] == 0, out
