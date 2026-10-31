"""The trap-path profile (-D KOS_PROF, the kprof app) on a core model.

    cd software/os/tests/host
    KOS_BUILD=~/kos/build KOS_OUT=~/kos/out pytest --rootdir . test_prof.py

kprof checks that each selection profiled exactly the calls it made and that
every stage is nonzero; here the kernel's stages must also fit inside the
round trip the caller timed, and the plain kernel must refuse the key.
"""

import os
import pathlib
import subprocess
import sys

import pytest

TOOLS = pathlib.Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))

from boot import load, parse_flags
from run_core import run

BUILD = pathlib.Path(os.environ.get("KOS_BUILD", pathlib.Path.home() / "kos/build"))
OUT = pathlib.Path(os.environ.get("KOS_OUT", pathlib.Path.home() / "kos/out"))

needs_core = pytest.mark.skipif(
    not (BUILD / "core2/obj_dir/vsim").exists(), reason="no core models"
)

STAGES = ("enter", "save", "to_c", "call", "poll", "schedule", "to_resume")
CALLS = ("yield", "send_here", "recv_ready", "send_remote")


@pytest.fixture(scope="session")
def prof_image(tmp_path_factory):
    out = tmp_path_factory.mktemp("kos_prof")
    subprocess.run(
        [sys.executable, str(TOOLS / "build.py"), "kos", "--out", str(out)]
        + ["-D", "KOS_PROF"],
        check=True,
        capture_output=True,
    )
    return out / "kos.elf"


def core(image: pathlib.Path, cmd: str) -> dict:
    return run(
        load("core2"), BUILD / "core2", image, cmd, parse_flags(""), 0, 40_000_000
    )


@needs_core
def test_kprof(prof_image):
    r = core(prof_image, "kprof 32")
    assert not r["fails"] and r["rc"] == 0, "\n".join(r["fails"]) + r["output"][-4000:]
    m = r["measures"]["0"]
    # the kernel's stages fit inside the round trip the caller timed itself
    inside = sum(m[f"kprof.self.{s}"] for s in STAGES)
    assert 0 < inside < m["kprof.self.total"]
    assert 0 < m["kprof.self.out"] < m["kprof.self.total"]
    for c in CALLS:
        assert m[f"kprof.{c}.calls"] > 0, c


@needs_core
def test_plain_kernel_refuses():
    r = core(OUT / "kos.elf", "kprof 4")
    assert "not a KOS_PROF kernel" in r["output"]
