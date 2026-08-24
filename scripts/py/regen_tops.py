#!/usr/bin/env python3
"""Regenerate every reproducible top in src/kohakutpu/top/generated from its map.

    python scripts/py/regen_tops.py            # rewrite
    python scripts/py/regen_tops.py --check    # fail if any is stale

Without a recorded map and flag set per top the outputs drift: `ktpu_min_1m.v`
still carried the pre-chain interlink header after mag_switch.v moved to CH_SEQ.

`MESH_ID` is read back out of the existing file -- it is the one argument no map
carries, and four tops differ by it alone.

The five `ktpu_mesh_*` tops are absent because no map reproduces them and
nothing references them. See src/attic/STALE-TOPS.md.
"""

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "py"))

import gen_mesh

TOPS = pathlib.Path("src/kohakutpu/top/generated")
MAPS = pathlib.Path("src/kohakutpu/top/maps")

# (file, map, ilink, single_master[, (l2_mag, l2_cu, l2_vec)[, split_reset
#  [, module]]])
#
# `module` differs from `file` in exactly one place: the split-reset variant
# declares `ktpu_min_1m` so one bench can swap the file and instantiate the
# same name.
MANIFEST = [
    ("ktpu_min_1m", "mesh_1x1_min.txt", True, True),
    # The same minimal mesh with per-domain reset entry. Absent from this list
    # it never regenerated, and `sb_mesh_e2e_sr` was the bench that found out.
    (
        "ktpu_min_1m_split",
        "mesh_1x1_min.txt",
        True,
        True,
        (False, False, False),
        True,
        "ktpu_min_1m",
    ),
    # v6.5-small: per-domain reset entry (kh_rst_sync per MAG/mat/vec domain).
    ("ktpu_ship_1x1_2c2v_1m", "mesh_1x1_2+2.txt", True, True, (True, True, True), True),
    # The same minimal mesh WITH staging, so a bench can prove the memory mover
    # reaches L2 by address -- nothing else instantiates a store in a real top.
    ("ktpu_min_1m_l2", "mesh_1x1_min.txt", True, True, (True, False, False)),
    # multimesh_v5 instantiates these two and sets L2 CONFIG properties on them.
    # Vivado IGNORES an override naming a parameter that does not exist.
    ("ktpu_ship_2x1_6c0v_1m", "mesh_2x1_6+0.txt", True, True, (True, True, True)),
    # SLR-balanced pair: the mesh sharing an SLR with root_smc/xdma/jtag drops to
    # 4 clusters, the other three take a 7th on a port that was already free.
    ("ktpu_ship_1x2_4c0v_1m", "mesh_1x2_4+0.txt", True, True, (True, True, True)),
    ("ktpu_ship_2x2_7c2v_1m", "mesh_2x2_7+2.txt", True, True, (True, True, True)),
    # The probes' 8+2 point. Absent from this list, it never regenerated and
    # silently held whatever the generator emitted when it was first written.
    ("ktpu_ship_2x2_8c2v_1m", "mesh_2x2_8+2.txt", True, True, (True, True, True)),
    # One system node, one router, one cluster, one vector core. Every node has
    # a control processor now, so this top is no longer the one that has one.
    ("ktpu_ctrlpe_1x1", "mesh_1x1_ctrlpe.txt", True, True, (False, False, False), True),
    ("ktpu_ship_2x1_6c0v_il", "mesh_2x1_6+0.txt", True, False),
    ("ktpu_ship_2x2", "mesh_2x2_4cu4vec.txt", False, False),
    ("ktpu_ship_2x2_il", "mesh_2x2_4+4.txt", True, False),
    ("ktpu_ship_2x2_6c0v_il", "mesh_2x2_6+0.txt", True, False),
    ("ktpu_ship_2x2_6c2v_1m", "mesh_2x2_6+2.txt", True, True, (True, True, True), True),
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
        split = entry[5] if len(entry) > 5 else False
        module = entry[6] if len(entry) > 6 else name
        out = ROOT / TOPS / f"{name}.v"
        src = ROOT / MAPS / mapname
        mesh = gen_mesh.Mesh(gen_mesh.parse_map(src.read_text(encoding="utf-8")))
        text = gen_mesh.emit(
            mesh,
            module,
            ilink,
            mesh_id_of(out),
            single,
            l2_mag=l2_mag,
            l2_cu=l2_cu,
            l2_vec=l2_vec,
            split_reset=split,
        )
        if args.pump:
            pout = ROOT / TOPS / f"{name}_pump.v"
            pmesh = gen_mesh.Mesh(gen_mesh.parse_map(src.read_text(encoding="utf-8")))
            ptext = gen_mesh.emit(
                pmesh,
                f"{module}_pump",
                ilink,
                mesh_id_of(pout),
                single,
                mat_pump=True,
                l2_mag=l2_mag,
                l2_cu=l2_cu,
                l2_vec=l2_vec,
                split_reset=split,
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
