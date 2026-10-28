"""Mesh-level jobs for tests/sru/sru_cu_tb.v, from the independent SRU oracle
(scripts/py/sru/oracle.py; ISA.SR.010-013, ISA.SR.015, ISA.SR.040-047,
ISA.SR.050-053).

    python scripts/py/sru/cu_vectors.py

build/sru/cu.cmd, `op addr data` in hex, one a line:
  0 memory word (32-byte index)    1 CU instruction (its payload)
  2 producer word (CU_DATA to the SRU's A, sent on credits, for the next RUN)
  3 expected memory word            4 expected peer word (CU_DATA from the SRU)
"""

import argparse
import pathlib
import random

import core_vectors as cv
import oracle

ROOT = pathlib.Path(__file__).resolve().parents[3]

TABLE_AT = 0x100000 // 32
COLS_AT = 0x110000 // 32
ROWS_AT = 0x120000 // 32
DATA_AT = 0x200000 // 32
OUT_AT = 0x300000 // 32
STORE_AT = 0x380000 // 32
BENCH_X, BENCH_Y = 1, 1
F_LANES = 16


def ins_cfg(addr, value):
    return (1 << 252) | (addr << 244) | value


def ins_load(what, first, waddr, words):
    return (
        (2 << 252)
        | (what << 250)
        | (first << 240)
        | ((waddr * 32) << 200)
        | (words << 188)
    )


def ins_store(row, waddr, rows):
    return (4 << 252) | (row << 240) | ((waddr * 32) << 200) | (rows << 188)


def ins_run(amem, peer, abase, astr, obase, ostr, bbase=0, bstr=0, count=0, words=0):
    return (
        (3 << 252)
        | (amem << 251)
        | (peer << 250)
        | ((abase * 32) << 210)
        | (astr << 194)
        | ((obase * 32) << 154)
        | (ostr << 138)
        | ((bbase * 32) << 98)
        | (bstr << 82)
        | (BENCH_X << 78)
        | (BENCH_Y << 74)
        | (0 << 70)
        | (0 << 54)
        | (count << 53)
        | (BENCH_X << 49)
        | (BENCH_Y << 45)
        | (words << 21)
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=str(ROOT / "build" / "sru"))
    args = ap.parse_args()

    rng = random.Random(args.seed)
    lines = []

    def emit(op, a, d):
        lines.append(f"{op:x} {a:x} {d:x}")

    tabs = oracle.fn_tables()
    cols = [
        [(cv.f16(rng.gauss(0, 1)), cv.f16(rng.gauss(1, 0.2))) for _ in range(4)]
        for _ in range(512)
    ]
    rows = [
        [cv.e8w(rng.gauss(0, 2)), cv.e8w(rng.gauss(0, 1)), cv.e8w(rng.gauss(0, 1))]
        for _ in range(512)
    ]

    for name, base in (("sigmoid", TABLE_AT), ("exp2", TABLE_AT + 256)):
        ents = tabs[name][0]
        for w in range(256):
            emit(0, base + w, sum(ents[4 * w + k] << (64 * k) for k in range(4)))
    for w in range(256):
        word = 0
        for k in range(2):
            cw = cols[2 * w + k]
            word |= sum(
                ((cw[c][0] | (cw[c][1] << 16)) << (32 * c)) for c in range(4)
            ) << (128 * k)
        emit(0, COLS_AT + w, word)
    for i in range(64):
        r = rows[i]
        emit(0, ROWS_AT + i, (r[0] << 8) | (r[1] << 40) | (r[2] << 72))

    insts = []
    insts.append(ins_load(1, 0, COLS_AT, 256))
    insts.append(ins_load(2, 0, ROWS_AT, 64))

    def cfg_all(j):
        for a, v in cv.cfg_words(j, F_LANES).items():
            insts.append(ins_cfg(a, v))

    # -- job 1: silu, A from memory, out to memory (sub-row stride 6 words)
    j1 = cv.job(
        "silu",
        {"NW": 4, "NSR": 3, "OUT": 1, "ROW0": 0},
        [
            {
                "EMIT": 1,
                "PROG": [
                    cv.fun("X", "K", FIX=1, CS=tabs["sigmoid"][1]),
                    cv.fma(cv.H(0), "X", "K", KC=0),
                ],
            }
        ],
    )
    a1 = cv.rnd_words(rng, 12, 1.5)
    for i, wd in enumerate(a1):
        emit(0, DATA_AT + (i // 4) * 6 + i % 4, oracle.pack16(wd))
    out1 = oracle.run_job(j1, a1, [], tabs["sigmoid"], cols, rows)
    insts.append(ins_load(0, 0, TABLE_AT, 256))
    cfg_all(j1)
    insts.append(ins_run(1, 0, DATA_AT, 6, OUT_AT, 5))
    exp_mem = {OUT_AT + (i // 4) * 5 + i % 4: w for i, w in enumerate(out1)}

    # -- job 2: CFG with B from memory
    j2 = cv.job(
        "cfg",
        {"NW": 4, "NSR": 2, "OUT": 1, "USE_B": 1, "ROW0": 8},
        [
            {
                "EMIT": 1,
                "PROG": [
                    cv.fma("X", "K", "B", KB=cv.ONE, NC=1),
                    cv.fma(cv.H(0), "K", "B", KB=cv.e8w(4.5)),
                ],
            }
        ],
    )
    a2 = cv.rnd_words(rng, 8, 1.0)
    b2 = cv.rnd_words(rng, 8, 1.0)
    for i, wd in enumerate(a2):
        emit(0, DATA_AT + 64 + i, oracle.pack16(wd))
    for i, wd in enumerate(b2):
        emit(0, DATA_AT + 128 + i, oracle.pack16(wd))
    out2 = oracle.run_job(j2, a2, b2, None, cols, rows)
    cfg_all(j2)
    insts.append(ins_run(1, 0, DATA_AT + 64, 4, OUT_AT + 64, 4, DATA_AT + 128, 4))
    exp_mem.update({OUT_AT + 64 + i: w for i, w in enumerate(out2)})

    # -- job 3: flash-like, A as a CU_DATA stream, MXFP7 to the peer, rows stored
    j3 = cv.job(
        "flash",
        {"NW": 8, "NSR": 3, "OUT": 2, "BUF": 1, "NPASS": 1, "R1": 1, "ROW0": 16},
        [
            {"FINLEN": 1, "FINBASE": 0},
            {
                "R2EN": 1,
                "EMIT": 1,
                "FINLEN": 1,
                "FINBASE": 1,
                "PROG": [cv.fun("X", "R", RB=0, CS=tabs["exp2"][1])],
            },
        ],
        fin=[
            {"OP": oracle.FIN_MAX, "DST": 0, "A": 0, "B": 4},
            {"OP": oracle.FIN_ADD, "DST": 1, "A": 1, "C": 5},
        ],
    )
    a3 = cv.rnd_words(rng, 24, 4.0)
    out3 = oracle.run_job(j3, a3, [], tabs["exp2"], cols, rows)
    insts.append(ins_load(0, 0, TABLE_AT + 256, 256))
    cfg_all(j3)
    for wd in a3:
        insts.append(("producer", oracle.pack16(wd)))
    insts.append(ins_run(0, 1, 0, 0, 0, 0, count=1, words=len(a3)))
    insts.append(ins_store(16, STORE_AT, 12))
    for i in range(12):
        r = rows[16 + i]
        exp_mem[STORE_AT + i] = (r[0] << 8) | (r[1] << 40) | (r[2] << 72)

    # -- job 4: a 15-word stream (not a multiple of 8: the last post is short),
    # f16 to memory
    j4 = cv.job(
        "scale",
        {"NW": 5, "NSR": 3, "OUT": 1, "ROW0": 40},
        [{"EMIT": 1, "PROG": [cv.fma("X", "K", "K", KB=cv.e8w(0.5), KC=cv.ONE)]}],
    )
    a4 = cv.rnd_words(rng, 15, 1.0)
    out4 = oracle.run_job(j4, a4, [], None, cols, rows)
    cfg_all(j4)
    for wd in a4:
        insts.append(("producer", oracle.pack16(wd)))
    insts.append(ins_run(0, 0, 0, 0, OUT_AT + 160, 5, words=len(a4)))
    exp_mem.update({OUT_AT + 160 + i: w for i, w in enumerate(out4)})

    for x in insts:
        if isinstance(x, tuple):
            emit(2, 0, x[1])
        else:
            emit(1, 0, x)
    for a, w in sorted(exp_mem.items()):
        emit(3, a, w)
    for w in out3:
        emit(4, 0, w)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "cu.cmd").write_text("\n".join(lines) + "\n")
    print(f"{out / 'cu.cmd'} {len(lines)} commands, {len(insts)} instructions")


if __name__ == "__main__":
    main()
