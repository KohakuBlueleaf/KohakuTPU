"""cpuprobe: the node processor's datapaths against a Python reference.

    python software/firmware/tools/cpuprobe.py --emit-header
    python software/firmware/tools/cpuprobe.py --bench build/cpuprobe/vlt_rv64_node_pair
    python software/firmware/tools/cpuprobe.py --card multimesh_v9t4 --nodes 0,1,2,3 \\
        [--rounds 8] [--seed 0x...] [--transport jtag | --build MODEL]

`reference(rounds, seed)` is the firmware (kohakuaccel/apps/cpuprobe/main.c)
in Python, check by check; the hashes the processor reports are compared to it.
`--emit-header` writes the default run's hashes into the firmware's expect.h,
which is how the bench run checks itself.

`--bench` runs the sysnode-only bench (xsim.py `rv64_node_pair`: two whole
system nodes, their DRAM and the interlink, no mesh) with cpuprobe on mesh 0
and cpuprobe_m1 on mesh 1, and checks each node's console lines.

`--card` loads cpuprobe through each node's load window (the card's path),
with a boot block naming a host-painted DRAM region, and checks the result
block there. One node at a time, each a separate load and run.
"""

import argparse
import pathlib
import re
import struct
import subprocess
import sys
import time

from kohakuaccel.driver.node.boot import BootArgs, NodeBoot
from kohakuaccel.driver.node.boot.args import idle_bound
from kohakuaccel.driver.transport.jtag import JtagTransport
from kohakuaccel.driver.transport.rebase import UnitGlobal
from kohakuaccel.driver.transport.verilator import VerilatorTransport
from kohakutpu.driver.clock.card import load_board
from kohakutpu.driver.host import board_map

ROOT = pathlib.Path(__file__).resolve().parents[3]
FW = ROOT / "build" / "fw"
HEADER = ROOT / "software/firmware/kohakuaccel/apps/cpuprobe/expect.h"

M = (1 << 64) - 1
M32 = (1 << 32) - 1
SEED = 0x6B6F68616B755450  # "PTukahok"
MAGIC = 0x3142525055504355  # "UCPUPRB1"
NAMES = ("alu", "mul", "div", "flow", "call", "spad", "dram", "amo", "csr", "ctrl")
RESULT_OFF, DRAM_OFF, DRAM_BYTES = 0x40, 0x1000, 16 << 10
#: Result block words: magic, fails, rounds, seed, cycles, 3 pad, hashes, cycles.
RESULT_WORDS = 8 + 2 * len(NAMES)
#: The card region a node's run uses, one 64 KB slot per node. In the
#: processor's node range (at or above 0x1000_0000): below it a mesh-0 address
#: is the on-chip window, not DRAM.
CARD_REGION = 0x2000_0000


def s64(v: int) -> int:
    v &= M
    return v - (1 << 64) if v >> 63 else v


def s32(v: int) -> int:
    v &= M32
    return v - (1 << 32) if v >> 31 else v


def sx32(v: int) -> int:
    return s32(v) & M


def mix(h: int, v: int) -> int:
    h = ((h ^ (v & M)) * 0x100000001B3) & M
    return h ^ (h >> 29)


class Rng:
    """splitmix64, as `rnd()` in main.c."""

    def __init__(self, seed: int) -> None:
        self.st = seed & M

    def __call__(self) -> int:
        self.st = (self.st + 0x9E3779B97F4A7C15) & M
        z = self.st
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & M
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & M
        return z ^ (z >> 31)


# ---- the RISC-V M extension, by the specification's table (zero, overflow)
def _tdiv(a: int, b: int) -> int:
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


def div(a, b):
    sa, sb = s64(a), s64(b)
    if sb == 0:
        return M
    if sa == -(1 << 63) and sb == -1:
        return a & M
    return _tdiv(sa, sb) & M


def divu(a, b):
    return M if b == 0 else a // b


def rem(a, b):
    sa, sb = s64(a), s64(b)
    if sb == 0:
        return a & M
    if sa == -(1 << 63) and sb == -1:
        return 0
    return (sa - sb * _tdiv(sa, sb)) & M


def remu(a, b):
    return a if b == 0 else a % b


def divw(a, b):
    sa, sb = s32(a), s32(b)
    if sb == 0:
        return M
    if sa == -(1 << 31) and sb == -1:
        return sx32(sa)
    return sx32(_tdiv(sa, sb))


def divuw(a, b):
    ua, ub = a & M32, b & M32
    return M if ub == 0 else sx32(ua // ub)


def remw(a, b):
    sa, sb = s32(a), s32(b)
    if sb == 0:
        return sx32(sa)
    if sa == -(1 << 31) and sb == -1:
        return 0
    return sx32(sa - sb * _tdiv(sa, sb))


def remuw(a, b):
    ua, ub = a & M32, b & M32
    return sx32(ua) if ub == 0 else sx32(ua % ub)


# ---- the checks
def chk_alu(h, n, rnd):
    for _ in range(n):
        a, b = rnd(), rnd()
        s = b & 63
        w = s & 31
        for v in (
            a + b,
            a - b,
            a & b,
            a | b,
            a ^ b,
            a << s,
            a >> s,
            s64(a) >> s,
            int(s64(a) < s64(b)),
            int(a < b),
            sx32(a + b),
            sx32(a - b),
            sx32((a & M32) << w),
            sx32((a & M32) >> w),
            s32(a) >> w,
            ((a + 0x5A5) & M) ^ 0x3C3,
        ):
            h = mix(h, v & M)
    return h


def chk_mul(h, n, rnd):
    for i in range(n):
        a, b = rnd(), rnd()
        if i & 7 == 0:
            a = 1 << 63
        if i & 15 == 0:
            b = M
        h = mix(h, a * b)
        h = mix(h, (s64(a) * s64(b)) >> 64)
        h = mix(h, (a * b) >> 64)
        h = mix(h, (s64(a) * b) >> 64)
        h = mix(h, sx32(a * b))
    return h


def chk_div(h, n, rnd):
    for i in range(n):
        a, b = rnd(), rnd()
        k = i & 7
        if k == 0:
            b = 0
        elif k == 1:
            a, b = 1 << 63, M
        elif k == 2:
            b >>= 40
        elif k == 3:
            b &= 0xFFFF
        elif k == 4:
            a, b = 0xFFFFFFFF80000000, M
        elif k == 5:
            b = 0xFFFFFFFF00000000
        for op in (div, divu, rem, remu, divw, divuw, remw, remuw):
            h = mix(h, op(a, b))
    return h


def chk_flow(h, n, rnd):
    for i in range(n):
        x, c = rnd(), i
        for k in range(64):
            if (x >> k) & 1:
                c = (c + k) & M
            else:
                c ^= x >> k
        if s64(x) < 0:
            c = ~c & M
        sw = (x >> 3) & 7
        if sw == 0:
            c = (c * 3 + 1) & M
        elif sw == 1:
            c ^= (c << 7) & M
        elif sw == 2:
            c = (c + (x >> 3)) & M
        elif sw == 3:
            c = ((c << 13) | (c >> 51)) & M
        elif sw == 4:
            c = (c - 0x1234567) & M
        elif sw == 5:
            c = ~c & M
        elif sw == 6:
            c ^= x
        else:
            c >>= 1
        h = mix(h, c)
    return h


def chk_call(h, n, rnd):
    h = mix(h, 987)  # fib(16)
    table = (
        lambda v: ((v << 17) | (v >> 47)) & M,
        lambda v: -v & M,
        lambda v: v >> 11,
        lambda v: (v + 0x777) & M,
    )
    for _ in range(n):
        v = rnd()
        h = mix(h, table[v & 3](v))
    return h


def chk_spad(h, n, rnd):
    buf = bytearray(512)
    for _ in range(n):
        v = rnd()
        w, w2 = (v >> 9) & 3, (v >> 29) & 3
        off = v & 511 & ~((1 << w) - 1)
        off2 = (v >> 20) & 511 & ~((1 << w2) - 1)
        nb, nb2 = 1 << w, 1 << w2
        buf[off : off + nb] = ((v >> 16) & ((1 << (8 * nb)) - 1)).to_bytes(nb, "little")
        raw = int.from_bytes(buf[off2 : off2 + nb2], "little")
        if w2 < 3 and (v >> 31) & 1 and raw >> (8 * nb2 - 1):
            raw -= 1 << (8 * nb2)
        h = mix(h, raw & M)
    for k in range(0, 512, 8):
        h = mix(h, int.from_bytes(buf[k : k + 8], "little"))
    return h


def chk_dram(h, rounds, rnd):
    words = DRAM_BYTES // 8
    half = [0] * (2 * words)

    def word(k):
        return half[2 * k] | half[2 * k + 1] << 32

    for _ in range(rounds):
        for k in range(words):
            v = rnd()
            half[2 * k], half[2 * k + 1] = v & M32, v >> 32
        j = 0
        for _k in range(words):
            h = mix(h, word(j))
            j = (j + 9) % words
        for _k in range(words // 4):
            v = rnd()
            half[v % (2 * words)] = v >> 32
        for k in range(0, words, 7):
            h = mix(h, word(k))
        for k in range(3, words, 5):
            h = mix(h, word(k))
    return h


def chk_amo(h, n, rnd):
    d, w = 0, 0
    for i in range(n):
        v = rnd()
        k = i % 10
        if k == 0:
            h, d = mix(h, d), (d + v) & M
        elif k == 1:
            h, d = mix(h, d), v
        elif k == 2:
            h, d = mix(h, d), d ^ v
        elif k == 3:
            h, d = mix(h, d), d & (v | 0x8000000000000001)
        elif k == 4:
            h, d = mix(h, d), d | (v & 0x00FF00FF00FF00FF)
        elif k == 5:
            h, d = mix(h, d), min(s64(d), s64(v)) & M
        elif k == 6:
            h, d = mix(h, d), max(d, v >> 1)
        elif k == 7:
            h, d = mix(h, d), min(d, v)
        elif k == 8:
            h, w = mix(h, sx32(w)), (w + v) & M32
        else:
            h = mix(mix(h, d), 0)  # lr.d's old value, sc.d's success
            d = v ^ 0x55
        h = mix(h, d)
    return mix(h, sx32(w))


def chk_csr(h, rounds, rnd):
    return mix(h, 0)


def chk_ctrl(h, n, rnd):
    def value(r, w):
        return w & 0x0F0F if r == 0 else w  # DST reads back x [3:0], y [11:8]

    regs = [value(r, rnd()) for r in range(5)]
    for _ in range(n):
        v, w = rnd(), rnd()
        r, k = v % 5, (v >> 8) % 5
        regs[r] = value(r, w)
        h = mix(h, regs[k])
    return h


CHECKS = (
    (chk_alu, 256),
    (chk_mul, 256),
    (chk_div, 128),
    (chk_flow, 128),
    (chk_call, 256),
    (chk_spad, 512),
    (chk_dram, 1),
    (chk_amo, 200),
    (chk_csr, 1),
    (chk_ctrl, 256),
)


def reference(rounds: int = 1, seed: int = SEED) -> list[int]:
    """Each check's hash for a run of `rounds` at `seed`, as main.c computes it."""
    out = []
    for c, (fn, per) in enumerate(CHECKS):
        h = 0xCBF29CE484222325 ^ c
        out.append(fn(h, per * rounds, Rng(seed + c)))
    return out


def emit_header() -> str:
    lines = [
        "/* GENERATED by software/firmware/tools/cpuprobe.py --emit-header: the",
        " * default run's hashes (rounds 1, CPUPROBE_SEED). Do not edit by hand. */",
        "#ifndef CPUPROBE_EXPECT_H",
        "#define CPUPROBE_EXPECT_H",
        "",
        "#include <stdint.h>",
        "",
        f"#define CPUPROBE_SEED {SEED:#x}UL",
        "",
        "static const uint64_t cpuprobe_expect[] = {",
    ]
    for name, h in zip(NAMES, reference(), strict=True):
        lines.append(f"    {h:#018x}UL, /* {name} */")
    lines += ["};", "", "#endif", ""]
    return "\n".join(lines)


def compare(got: list[int], want: list[int]) -> list[str]:
    """The names of the checks whose hashes differ."""
    return [n for n, g, w in zip(NAMES, got, want, strict=True) if g != w]


# ---- the sysnode-only bench
def bench(build: pathlib.Path, max_cycles: int) -> int:
    vsim = build / "obj_dir" / "vsim.exe"
    cmd = [
        str(vsim),
        "--elf-a",
        str(FW / "cpuprobe.elf"),
        "--elf-b",
        str(FW / "cpuprobe_m1.elf"),
        "--expect-a",
        "cpuprobe ok",
        "--expect-b",
        "cpuprobe ok",
        "--max-cycles",
        str(max_cycles),
    ]
    t0 = time.monotonic()
    done = subprocess.run(cmd, capture_output=True, text=True, check=False)
    wall = time.monotonic() - t0
    print(done.stdout.rstrip())
    want = reference()
    fails = done.returncode != 0
    blocks = re.split(r"\n  node [AB]:\n", done.stdout)[1:]
    if len(blocks) != 2:
        print(f"expected two node reports, found {len(blocks)}")
        return 1
    for node, text in zip("AB", blocks, strict=True):
        got = {
            m[1]: (int(m[2], 16), int(m[3]))
            for m in re.finditer(r"cpuprobe (\w+) ([0-9a-f]+) (\d+)", text)
        }
        if sorted(got) != sorted(NAMES):
            print(f"node {node}: checks reported {sorted(got)}")
            fails = True
            continue
        bad = compare([got[n][0] for n in NAMES], want)
        print(
            f"node {node}: "
            + ", ".join(f"{n} {got[n][1]}" for n in NAMES)
            + f" cycles; reference {'MISMATCH ' + str(bad) if bad else 'matches'}"
        )
        fails |= bool(bad)
    print(f"bench {wall:.1f}s wall: {'PASS' if not fails else 'FAIL'}")
    return int(fails)


# ---- the card, one node at a time through its load window
def card_run(
    t, m, node, mem, rounds, seed, idle, timeout, wait: int
) -> tuple[bool, dict]:
    """Run cpuprobe on `node`, its boot block's bound `wait` cycles;
    ``(ok, result block)``."""
    region = m["dram"][node] + CARD_REGION + node * 0x10000
    # Every line the run reads is painted first: never-written ECC memory is an
    # error, not zeros, and a cached store fills its line.
    mem.write_block(region, bytes(DRAM_OFF + DRAM_BYTES))
    mem.write_block(region, struct.pack("<4Q", MAGIC, rounds, seed, 0))
    boot = NodeBoot(t, m["ctrl"][node], node, sim=None)
    boot.load(
        FW / "cpuprobe.elf", BootArgs(queue=region, mesh=node, timeout=wait), "axi"
    )
    t0, st = time.monotonic(), {}
    while time.monotonic() - t0 < timeout:
        st = boot.status()
        if st["exited"] or st["halted"]:
            break
        idle()
    raw = mem.read_block(region + RESULT_OFF, 8 * RESULT_WORDS)
    w = struct.unpack(f"<{RESULT_WORDS}Q", raw)
    n = len(NAMES)
    res = {
        "status": st,
        "magic_ok": w[0] == MAGIC,
        "rounds": w[2],
        "seed": w[3],
        "cycles": w[4],
        "hash": list(w[8 : 8 + n]),
        "cyc": list(w[8 + n : 8 + 2 * n]),
        "wall": time.monotonic() - t0,
    }
    bad = compare(res["hash"], reference(rounds, seed))
    res["bad"] = bad
    ok = (
        st.get("exited")
        and res["magic_ok"]
        and res["rounds"] == rounds
        and res["seed"] == seed
        and not bad
    )
    return bool(ok), res


def card(a) -> int:
    board = load_board(a.card)
    m = board_map(board)
    if a.build:
        t = VerilatorTransport(a.build)

        def idle():
            t.run(20000)

    else:
        t = JtagTransport()

        def idle():
            time.sleep(0.05)

    mem = UnitGlobal(t, m["dram"], m["mem"], m["size"], staging=True)
    fails = 0
    for node in (int(x) for x in a.nodes.split(",")):
        ok, r = card_run(
            t,
            m,
            node,
            mem,
            a.rounds,
            a.seed,
            idle,
            a.timeout,
            idle_bound(board, a.timeout),
        )
        print(
            f"node {node}: {'ok' if ok else 'FAIL'} exit {r['status'].get('exit')} "
            f"{r['cycles']} cycles ({r['wall']:.1f}s); "
            + ", ".join(f"{n} {c}" for n, c in zip(NAMES, r["cyc"], strict=True))
            + (f"; MISMATCH {r['bad']}" if r["bad"] else "")
        )
        fails += not ok
    if a.build:
        t.close()
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-header", action="store_true")
    ap.add_argument("--bench", help="the rv64_node_pair model's build directory")
    ap.add_argument("--max-cycles", type=int, default=20_000_000)
    ap.add_argument("--card", help="a board name: run through each node's load window")
    ap.add_argument("--build", help="--card on a Verilated card model, not JTAG")
    ap.add_argument("--nodes", default="0")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--seed", type=lambda s: int(s, 0), default=SEED)
    ap.add_argument("--timeout", type=float, default=600.0)
    a = ap.parse_args()
    if a.emit_header:
        HEADER.write_text(emit_header(), encoding="utf-8")
        print(f"wrote {HEADER.relative_to(ROOT)}")
        return 0
    if a.bench:
        return bench(pathlib.Path(a.bench), a.max_cycles)
    if a.card:
        return card(a)
    ap.error("one of --emit-header, --bench, --card")
    return 2


if __name__ == "__main__":
    sys.exit(main())
