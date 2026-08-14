"""Emit CU_INST payloads from the COMPILER's encoder for an RTL bench to run.

    python scripts/py/gen_prog.py build/prog/mm_mesh_fill.hex

A bench that reads this is running the bits `kohakutpu.hw.matmul` actually
emits, not a hand-built copy of them. The two have diverged before: the vector
side had a second encoder in `isa/`, and the live path was the other one.

One 256-bit payload per line, MSB first, as $readmemh wants.
"""

import pathlib
import sys

from kohakutpu.hw import matmul as HW

MASK256 = (1 << 256) - 1

#: mm_mesh_tb section 3. nk/anchor are GEMM fields a FILL ignores, and zeroing
#: them matches the hand-built payload the bench has always injected.
PROGRAM = [
    ("fill A_MSRC n=1", HW._flit(op=HW.OP_FILL, addr=0x1000, n=1, nk=0, anchor=0)),
    ("gemm 1x1 nk=1", HW._flit(op=HW.OP_GEMM, gm=1, gn=1, nk=1)),
    ("drain A_MDST n=1", HW._flit(op=HW.OP_DRAIN, addr=0x2000, n=1, nk=0, anchor=0)),
]


def check_round_trip() -> None:
    """Every payload must decode at mx_cluster_cu's own part-selects.

    Raises AssertionError naming the opcode whose address does not survive
    `{inst_flit[68 -: 6], inst_flit[251 -: 34]}`.
    """
    for name, flit in PROGRAM:
        lo = (flit >> 218) & ((1 << 34) - 1)
        hi = (flit >> 63) & ((1 << 6) - 1)
        got = (hi << 34) | lo
        want = {"fill": 0x1000, "drain": 0x2000}.get(name.split()[0], 0)
        assert got == want, f"{name}: address {got:#x} decoded, expected {want:#x}"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    check_round_trip()
    out = pathlib.Path(argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for name, flit in PROGRAM:
        payload = flit & MASK256
        lines.append(f"{payload:064x}  // {name}")
    out.write_text("\n".join(lines) + "\n", encoding="ascii")
    print(f"@@@ wrote {len(PROGRAM)} payload(s) to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
