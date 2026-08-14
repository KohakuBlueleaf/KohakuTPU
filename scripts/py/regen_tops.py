#!/usr/bin/env python3
"""Regenerate every reproducible top in src/synth_top from its map.

    python scripts/py/regen_tops.py            # rewrite
    python scripts/py/regen_tops.py --check    # fail if any is stale

Without a recorded map and flag set per top the outputs drift: `ktpu_min_1m.v`
still carried the pre-chain interlink header after mag_switch.v moved to CH_SEQ.

`MESH_ID` is read back out of the existing file -- it is the one argument no map
carries, and four tops differ by it alone.

The five `ktpu_mesh_*` tops are absent because no map reproduces them and
nothing references them. See src/synth_top/STALE-TOPS.md.
"""

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "py"))

import gen_mesh

TOPS = pathlib.Path("src/synth_top")
MAPS = TOPS / "maps"

# (module, map, ilink, single_master[, (l2_mag, l2_cu, l2_vec)])
MANIFEST = [
    ("ktpu_min_1m", "mesh_1x1_min.txt", True, True),
    # The same minimal mesh WITH staging, so a bench can prove the memory mover
    # reaches L2 by address -- nothing else instantiates a store in a real top.
    ("ktpu_min_1m_l2", "mesh_1x1_min.txt", True, True, (True, False, False)),
    # multimesh_v5 instantiates these two and sets L2 CONFIG properties on them.
    # Vivado IGNORES an override naming a parameter that does not exist.
    ("ktpu_ship_2x1_6c0v_1m", "mesh_2x1_6+0.txt", True, True, (True, True, True)),
    ("ktpu_ship_2x1_6c0v_il", "mesh_2x1_6+0.txt", True, False),
    ("ktpu_ship_2x2", "mesh_2x2_4cu4vec.txt", False, False),
    ("ktpu_ship_2x2_il", "mesh_2x2_4+4.txt", True, False),
    ("ktpu_ship_2x2_6c0v_il", "mesh_2x2_6+0.txt", True, False),
    ("ktpu_ship_2x2_6c2v_1m", "mesh_2x2_6+2.txt", True, True, (True, True, True)),
    ("ktpu_ship_2x2_6c2v_il", "mesh_2x2_6+2.txt", True, False),
    ("ktpu_ship_2x2_6c4v_il", "mesh_2x2_6+4.txt", True, False),
    ("ktpu_ship_2x3", "mesh_2x3_6cu3vec.txt", False, False),
    ("ktpu_ship_3x2", "mesh_3x2_6+4.txt", False, False),
    ("ktpu_ship_3x2_il", "mesh_3x2_6+4.txt", True, False),
    ("ktpu_ship_3x2_6c3v_il", "mesh_3x2_6+3.txt", True, False),
]

MESH_ID_RE = re.compile(r"parameter integer MESH_ID\s*=\s*(\d+)")


def mesh_id_of(path):
    """The MESH_ID reset value already in `path`, or 0 if it has none."""
    if not path.exists():
        return 0
    m = MESH_ID_RE.search(path.read_text(encoding="utf-8"))
    return int(m.group(1)) if m else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report, do not write")
    # ADDITIVE: the pumped top's PORTS differ -- mat_clk2x/mat_div_clr against
    # mat_clk -- so replacing in place breaks every bench and the v5 BD at once.
    ap.add_argument(
        "--pump",
        action="store_true",
        help="also emit <name>_pump.v using the double-pumped cluster CU",
    )
    args = ap.parse_args()

    stale = []
    for entry in MANIFEST:
        name, mapname, ilink, single = entry[:4]
        l2_mag, l2_cu, l2_vec = entry[4] if len(entry) > 4 else (False, False, False)
        out = ROOT / TOPS / f"{name}.v"
        src = ROOT / MAPS / mapname
        mesh = gen_mesh.Mesh(gen_mesh.parse_map(src.read_text(encoding="utf-8")))
        text = gen_mesh.emit(
            mesh,
            name,
            ilink,
            mesh_id_of(out),
            single,
            l2_mag=l2_mag,
            l2_cu=l2_cu,
            l2_vec=l2_vec,
        )
        if args.pump:
            pout = ROOT / TOPS / f"{name}_pump.v"
            pmesh = gen_mesh.Mesh(gen_mesh.parse_map(src.read_text(encoding="utf-8")))
            ptext = gen_mesh.emit(
                pmesh,
                f"{name}_pump",
                ilink,
                mesh_id_of(pout),
                single,
                mat_pump=True,
                l2_mag=l2_mag,
                l2_cu=l2_cu,
                l2_vec=l2_vec,
            )
            if not (pout.exists() and pout.read_text(encoding="utf-8") == ptext):
                stale.append(f"{name}_pump")
                if not args.check:
                    pout.write_text(ptext, encoding="utf-8")
        if out.exists() and out.read_text(encoding="utf-8") == text:
            continue
        stale.append(name)
        if not args.check:
            out.write_text(text, encoding="utf-8")

    verb = "stale" if args.check else "rewritten"
    print(
        f"{len(stale)} of {len(MANIFEST)} {verb}"
        + (": " + " ".join(stale) if stale else "")
    )
    sys.exit(1 if (args.check and stale) else 0)


if __name__ == "__main__":
    main()
