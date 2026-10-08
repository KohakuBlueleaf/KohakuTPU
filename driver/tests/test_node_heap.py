"""The firmware's region heap (C) against kohakuaccel.node.heap (Python).

firmware/tests/host/heap_trace.c runs firmware/kohakuaccel/os/heap/heap.c
natively (WSL gcc) over a random trace; every operation's status and address,
the C table's own consistency check, and the whole block table every 64
operations must equal the Python policy's. The trace also asserts, in Python,
the properties the policy promises: aligned, granule-rounded, disjoint blocks.
"""

import itertools
import pathlib
import random
import re
import shutil
import subprocess

import pytest
from kohakuaccel.node.heap import Heap
from kohakuaccel.node.queue import layout as L

ROOT = pathlib.Path(__file__).resolve().parents[2]
FW = ROOT / "firmware"
OUT = ROOT / "build" / "fw" / "host"

pytestmark = pytest.mark.skipif(shutil.which("wsl") is None, reason="needs WSL gcc")


def wsl(p: pathlib.Path) -> str:
    s = p.resolve().as_posix()
    m = re.match(r"^([A-Za-z]):/(.*)$", s)
    return f"/mnt/{m.group(1).lower()}/{m.group(2)}" if m else s


@pytest.fixture(scope="module")
def trace_bin() -> pathlib.Path:
    OUT.mkdir(parents=True, exist_ok=True)
    exe = OUT / "heap_trace"
    cmd = (
        f"gcc -std=c11 -O1 -Wall -Wextra -Werror -I{wsl(FW / 'kohakuaccel/include')} "
        f"{wsl(FW / 'tests/host/heap_trace.c')} {wsl(FW / 'kohakuaccel/os/heap/heap.c')} "
        f"-o {wsl(exe)}"
    )
    done = subprocess.run(
        ["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-lc", cmd],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return exe


def run_trace(exe: pathlib.Path, lines: list[str]) -> list[str]:
    done = subprocess.run(
        ["wsl", "-d", "Ubuntu-24.04", "--", wsl(exe)],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines()


def make_trace(seed: int, base: int, nbytes: int, granule: int, cap: int, ops: int):
    """Ops plus the Python heap's expected outputs, in the harness's format."""
    rng = random.Random(seed)
    ref = Heap(base, nbytes, granule, cap)
    lines = [f"i {base} {nbytes} {granule} {cap}"]
    want = [(ref.rc, 0, len(ref.blocks))]
    live: list[int] = []
    for k in range(ops):
        if live and rng.random() < 0.45:
            addr = live.pop(rng.randrange(len(live)))
            if rng.random() < 0.05:
                addr += granule  # inside a block, or past it: BAD_FREE either way
                live.append(addr - granule)
            lines.append(f"f {addr}")
            rc, v = ref.free(addr), 0
        else:
            size = rng.choice(
                (1, granule, 3 * granule + 1, rng.randrange(1, nbytes // 8))
            )
            if rng.random() < 0.02:
                size = nbytes + granule
            align = rng.choice(
                (0, 0, granule, 4 * granule, 4096, 3 if k % 97 == 0 else 0)
            )
            lines.append(f"a {size} {align} {k}")
            rc, v = ref.alloc(size, align, k)
            if rc == L.OK:
                live.append(v)
                assert v % max(align, granule) == 0 and (v - base) % granule == 0
        want.append((rc, v, len(ref.blocks)))
        if k % 64 == 63:
            lines.append("d")
            want.append([tuple(b) for b in ref.blocks])
        used = sorted((b[0], b[0] + b[1]) for b in ref.blocks if b[2])
        assert all(a[1] <= b[0] for a, b in itertools.pairwise(used))
    return lines, want


def compare(out: list[str], want: list) -> int:
    i, n_ops = 0, 0
    for w in want:
        if isinstance(w, list):
            got = []
            while out[i] != "E":
                _, off, ln, used, tag = out[i].split()
                got.append((int(off), int(ln), int(used), int(tag)))
                i += 1
            i += 1
            assert got == [tuple(b) for b in w]
        else:
            rc, v, chk, n = (int(x) for x in out[i].split())
            assert (rc, v, n) == w, (n_ops, out[i], w)
            assert chk == 0, (n_ops, out[i])
            i += 1
            n_ops += 1
    assert i == len(out)
    return n_ops


@pytest.mark.parametrize(
    "seed,base,nbytes,granule,cap",
    [
        (1, 0x40_0000, 1 << 20, 64, 64),
        (2, 1 << 39 | 1 << 36 | 0x10000, 64 << 10, 64, 64),
        (3, 0x1000, 8192, 32, 5),  # a tiny table: NO_SLOTS is reached
        (4, 0x40, 1 << 16, 64, 1024),  # an unaligned-to-4K base: pads in front
    ],
)
def test_c_heap_matches_the_python_policy(trace_bin, seed, base, nbytes, granule, cap):
    lines, want = make_trace(seed, base, nbytes, granule, cap, ops=3000)
    out = run_trace(trace_bin, lines)
    assert compare(out, want) == 3001
    statuses = {w[0] for w in want if isinstance(w, tuple)}
    assert L.OK in statuses and L.BAD_FREE in statuses and L.NO_MEMORY in statuses
    if cap == 5:
        assert L.NO_SLOTS in statuses


def test_bad_geometry_is_refused(trace_bin):
    cases = [(0x40, 4096, 48, 8), (0x20, 4096, 64, 8), (0, 32, 64, 8), (0, 4096, 64, 2)]
    lines = [f"i {b} {n} {g} {c}" for b, n, g, c in cases]
    out = run_trace(trace_bin, lines)
    for (b, n, g, c), got in zip(cases, out):
        assert int(got.split()[0]) == Heap(b, n, g, c).rc == L.BAD_ARG
