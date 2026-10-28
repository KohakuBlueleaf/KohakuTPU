"""Emit CU_INST payloads from the compiler's cluster ISA for an RTL bench to run.

    python scripts/py/gen_prog.py build/prog/mm_mesh_fill.hex

A bench that reads this runs the Rust ISA encoder's bits through
`_rs.mg_encode`, not a hand-built copy of them.

One 256-bit payload per line, MSB first, as $readmemh wants.
"""

import pathlib
import sys

from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF

MACHINE = (MF.MACHINES / "multimesh_v9t8.json").read_text(encoding="utf-8")

#: mm_mesh_tb section 3. nk/anchor are GEMM fields a FILL or DRAIN ignores;
#: they are zero here, as in the payload the bench injects.
PROGRAM = [
    (
        "fill A_MSRC n=1",
        _rs.mg_encode("FILL", {"addr": 0x1000, "n": 1, "nk": 0, "anchor": 0}, MACHINE),
    ),
    ("gemm 1x1 nk=1", _rs.mg_encode("GEMM", {"gm": 1, "gn": 1, "nk": 1}, MACHINE)),
    (
        "drain A_MDST n=1",
        _rs.mg_encode("DRAIN", {"addr": 0x2000, "n": 1, "nk": 0, "anchor": 0}, MACHINE),
    ),
]


def check_address_fields() -> None:
    """Every payload must decode at mx_cluster_cu's own part-selects.

    Raises AssertionError naming the opcode whose address does not survive
    `{inst_flit[68 -: 6], inst_flit[251 -: 34]}`.
    """
    for name, payload in PROGRAM:
        lo = (payload >> 218) & ((1 << 34) - 1)
        hi = (payload >> 63) & ((1 << 6) - 1)
        got = (hi << 34) | lo
        want = {"fill": 0x1000, "drain": 0x2000}.get(name.split()[0], 0)
        assert got == want, f"{name}: address {got:#x} decoded, expected {want:#x}"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    check_address_fields()
    out = pathlib.Path(argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{payload:064x}  // {name}" for name, payload in PROGRAM]
    out.write_text("\n".join(lines) + "\n", encoding="ascii")
    print(f"@@@ wrote {len(PROGRAM)} payload(s) to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
