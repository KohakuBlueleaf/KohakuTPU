"""Generate machine files: every hardware fact of an image, from its sources.

    python -m machine --ver v9t6           # machines/multimesh_v9t6.json
    python -m machine --all                # every image with a build config
    python -m machine --all --check        # fail if a file differs from its sources

Run from scripts/py. The rows are machine/table.py; the readers machine/read.
"""

import argparse
import json
import sys
from pathlib import Path

from kohakutpu.language.machine import schema_version

from .read.image import ROOT, Image
from .table import ROWS, check_address, check_unique

OUT = ROOT / "machines"
#: Images whose build config the generator reads (scripts/tcl/<ver>/00_config.tcl).
IMAGES = ("v9", "v9t4", "v9t5", "v9t6", "v9t7", "v9t8")
#: Die shapes not built as a whole image: a base image's card and firmware
#: with another die module (and board, for units the generated board lacks).
VARIANTS = {
    "vlt_v9t6_2n": ("v9t6", {"board": "vlt_v9t6_2n"}),
    "multimesh_v9t8-6c2v": ("v9t8", {"die": "ktpu_ship_2x2_6c2v_1m_nol2_pump"}),
    "sru_v9t7": (
        "v9t7",
        {"die": "ktpu_ship_1x2_4c2s_1m_nol2_pump", "board": "sru_v9t7"},
    ),
    # The smallest Verilated card (xsim.py card_v9t6s_1n): v9t6 with one node
    # on the 1 MG + 1 VC simulation die with the v9t6 system ports
    # (gen_board.py --die --compute --name).
    "vlt_v9t6s_1n": (
        "v9t6",
        {"die": "ktpu_sim_1x2_1c1v_1m_nol2_pump", "board": "vlt_v9t6s_1n"},
    ),
}


def nest(flat: dict) -> dict:
    out: dict = {}
    for path, value in flat.items():
        node = out
        *head, last = path.split(".")
        for key in head:
            node = node.setdefault(key, {})
        node[last] = value
    return out


def machine(name: str, sets=()) -> dict:
    ver, kw = VARIANTS.get(name, (name, {}))
    img = Image(ver, **kw)
    values, sources = {}, {}
    for path, read in ROWS:
        values[path], sources[path] = read(img)
    out_name = name if name in VARIANTS else values["card.name"]
    changed = {}
    for setting in sets:
        path, sep, text = setting.partition("=")
        if not sep or path not in values:
            raise ValueError(f"--set must name an existing row: {setting}")
        value = json.loads(text)
        if type(value) is not type(values[path]):
            raise ValueError(f"--set {path}: wrong JSON type")
        values[path] = value
        sources[path] = "fixture: --set"
        changed[path] = value
    if changed:
        out_name += "+" + ",".join(
            f"{path.rsplit('.', 1)[-1]}={json.dumps(value, separators=(',', ':'))}"
            for path, value in changed.items()
        )
    check_address(values)
    policy = {
        key.rsplit(".", 1)[-1]: value
        for key, value in values.items()
        if key.startswith("conversion.fp16_mx7.")
    }
    if (
        policy["version"] != 2
        or policy["vector_slots"] not in (1, 2)
        or policy["vector_cores"] < 0
        or not isinstance(policy["vector_looped"], bool)
        or not policy["allowed_backends"]
        or any(
            b not in ("mover_quant", "vector_mx7") for b in policy["allowed_backends"]
        )
        or policy["preferred_backend"] not in policy["allowed_backends"]
        or policy["nonfinite_policy"] != "reject_vector"
    ):
        raise ValueError("invalid conversion.fp16_mx7 policy")
    return {
        "schema": schema_version(),
        "name": out_name,
        "image": ver,
        **kw,
        "machine": nest(values),
        "sources": sources,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ver", action="append", default=[])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--set", action="append", default=[], dest="sets")
    ap.add_argument("--out", type=Path, default=OUT, help="machine file directory")
    a = ap.parse_args()
    check_unique()
    if a.sets and a.out.resolve().is_relative_to(OUT.resolve()):
        ap.error("--set fixtures require --out outside machines/")
    vers = [*IMAGES, *VARIANTS] if a.all else a.ver
    if not vers:
        ap.error("name --ver or --all")
    bad = 0
    for ver in vers:
        m = machine(ver, a.sets)
        text = json.dumps(m, indent=1) + "\n"
        path = a.out / f"{m['name']}.json"
        if a.check:
            same = path.exists() and path.read_text(encoding="utf-8") == text
            bad += not same
            print(f"{path.name}: {'matches' if same else 'differs from its sources'}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {path} ({len(m['sources'])} facts)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
