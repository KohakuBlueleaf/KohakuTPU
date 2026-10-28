"""The node's ka_printf (kohakuaccel/lib/stdio/stdio.c) against a Python model
of its numbers: software/firmware/tests/host/printf_cases.c runs it natively
(WSL gcc) on every base, width and pad over edge values (0, the type's max and
min, negatives, powers of the base); each output must be the model's. The
model is the firmware's own convention: the sign is part of the number, the
pad goes before both ("%06d" of -12 is "000-12")."""

import os
import pathlib
import re
import shutil
import subprocess

import pytest

SOFTWARE = pathlib.Path(__file__).resolve().parents[2]
FW = SOFTWARE / "firmware"
OUT = SOFTWARE.parent / "build" / "fw" / "host"

pytestmark = pytest.mark.skipif(shutil.which("wsl") is None, reason="needs WSL gcc")

#: kind -> (bits, signed): i int, u unsigned, l int64_t, L uint64_t
KINDS = {"i": (32, True), "u": (32, False), "l": (64, True), "L": (64, False)}
#: kind -> the formats that take it
FORMATS = {
    "i": ["%d", "%5d", "%05d", "%1d"],
    "u": ["%u", "%x", "%8x", "%08x", "%012u"],
    "l": ["%ld", "%22ld", "%022ld"],
    "L": ["%lu", "%lx", "%016lx", "%24lu"],
}


def wsl(p: pathlib.Path) -> str:
    s = p.resolve().as_posix()
    m = re.match(r"^([A-Za-z]):/(.*)$", s)
    return f"/mnt/{m.group(1).lower()}/{m.group(2)}" if m else s


def values(kind: str) -> list[int]:
    bits, signed = KINDS[kind]
    top = (1 << (bits - 1)) - 1 if signed else (1 << bits) - 1
    got = {0, 1, 7, 9, 10, 15, 16, 99, 100, 255, 256, 65535, 12345, top, top - 1}
    got |= {10**k for k in range(1, 20) if 10**k <= top}
    got |= {16**k for k in range(1, 16) if 16**k <= top}
    if signed:
        got |= {-v for v in list(got) if v} | {-top - 1}
    return sorted(got)


def model(fmt: str, v: int) -> str:
    m = re.fullmatch(r"%(0?)(\d*)(l?)([dux])", fmt)
    pad, width, conv = "0" if m[1] else " ", int(m[2] or 0), m[4]
    digits = format(abs(v), "x") if conv == "x" else str(abs(v))
    s = ("-" if v < 0 else "") + digits
    return pad * (width - len(s)) + s


@pytest.fixture(scope="module")
def printf_bin() -> pathlib.Path:
    OUT.mkdir(parents=True, exist_ok=True)
    exe = OUT / f"printf_cases_{os.environ.get('PYTEST_XDIST_WORKER', 'main')}"
    cmd = (
        f"gcc -std=c11 -O1 -Wall -Wextra -Werror -I{wsl(FW / 'kohakuaccel/include')} "
        f"{wsl(FW / 'tests/host/printf_cases.c')} "
        f"{wsl(FW / 'kohakuaccel/lib/stdio/stdio.c')} -o {wsl(exe)}"
    )
    done = subprocess.run(
        ["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-lc", cmd],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return exe


def test_numbers_print_as_the_model_says(printf_bin):
    cases = [(k, f, v) for k, fs in FORMATS.items() for f in fs for v in values(k)]
    lines = [f"{k} {f} {v}" for k, f, v in cases]
    done = subprocess.run(
        ["wsl", "-d", "Ubuntu-24.04", "--", wsl(printf_bin)],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    got = done.stdout.splitlines()
    assert len(got) == len(cases)
    bad = [
        (line, g, model(f, v))
        for line, g, (_, f, v) in zip(lines, got, cases, strict=True)
    ]
    bad = [b for b in bad if b[1] != b[2]]
    assert bad == [], bad[:10]
