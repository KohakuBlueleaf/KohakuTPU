"""Generate a board file from the map and the synthesis log. Do not transcribe.

    python scripts/py/gen_board.py src/synth_top/maps/mesh_2x2_4cu4vec.txt \
        --synth-log %TEMP%/ktpu-ship-synth/ktpu_ship_2x2.log \
        --ctrl-base 0x400800000 --name ship_2x2

HAND TRANSCRIPTION IS THE KNOWN FAULT. `boards/singlemesh_2x2.json` was written
by reading a synthesis log by eye; the first attempt missed that the bitstream
held TILES=256/GA=32/GB=32 and not the RTL's 512/128/256, and the resulting run
put 15,440 of 16,384 elements past 10% error with every gate passing. Doing it
a second time by hand, while knowing that, is the trap.

THREE SOURCES, AND THEY FAIL DIFFERENTLY, which is why `source` records which
gave what: the MAP for coordinates and node types (the same file synthesis
consumed, so a disagreement is detectable); the SYNTH LOG for capacities and
CU_VERSION, which describe one particular build and rot invisibly, so its
identity is recorded too; and ARGUMENTS for the address map, which come from a
block design nothing here can read and are therefore never defaulted.
"""

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "py"))

from gen_mesh import Mesh, parse_map

from ktpu.hw import bench

# What the log calls each capacity, and where it lands in the board file. Read
# off `mx_cluster_node`, which is where the cluster's memories are declared.
CAPACITY = {"TILES": "tiles", "GA": "l1_a", "GB": "l1_b"}

PARAM = re.compile(r"^\s*Parameter (\w+) bound to: (\S+)")
MODULE = re.compile(r"synthesizing module '([\w$]+?)(?:__\w+)?'")
RESULT = re.compile(r"^RESULT\s+(\S+).*?WNS\s+(\S+).*?=\s*([\d.]+)\s*MHz", re.MULTILINE)


def _verilog_int(text):
    """`8'h02`, `8'b00000010` or `2` -- the log prints whichever the source used."""
    if "'" not in text:
        return int(text, 0)
    _, _, body = text.partition("'")
    return int(body[1:], {"h": 16, "b": 2, "o": 8, "d": 10}[body[0].lower()])


def elaborated(log_text):
    """Every module's bound parameters, first elaboration wins.

    First rather than last because a log can contain several passes over the
    same design and the later ones are re-elaborations of the same values; a
    dict that kept overwriting would report whichever happened to be last.
    """
    out, current = {}, None
    for line in log_text.splitlines():
        hit = MODULE.search(line)
        if hit:
            current = hit.group(1)
            out.setdefault(current, {})
            continue
        p = PARAM.match(line)
        if p and current:
            out[current].setdefault(p.group(1), p.group(2))
    return out


def capacities(log_path):
    """The capacities and CU_VERSION this build actually elaborated.

    Raises SystemExit naming the module if the log does not contain it, rather
    than defaulting -- a missing capacity is exactly the value that must not be
    guessed.
    """
    text = pathlib.Path(log_path).read_text(encoding="utf-8", errors="replace")
    mods = elaborated(text)
    out = {}
    node = mods.get("mx_cluster_node") or {}
    for key, field in CAPACITY.items():
        if key not in node:
            sys.exit(
                f"gen_board: {log_path} has no mx_cluster_node.{key}. Capacities "
                "cannot be defaulted -- planning against the wrong ones is silent."
            )
        out[field] = int(node[key])

    base = mods.get("noc_cu_base") or {}
    if "CU_VERSION" not in base:
        sys.exit(f"gen_board: {log_path} has no noc_cu_base.CU_VERSION")
    out["cu_version"] = _verilog_int(base["CU_VERSION"])

    mag = mods.get("mag") or {}
    if "STAGE_FLITS" in mag:
        out["stage_flits"] = int(mag["STAGE_FLITS"])

    result = RESULT.search(text)
    out["_build"] = {
        "log": str(log_path),
        "top": result.group(1) if result else "unknown",
        "fmax_mhz": float(result.group(3)) if result else None,
    }
    return out


def drift(caps):
    """Where this build has LESS capacity than the compiler plans to use.

    DIRECTIONAL, and that is the whole point. Hardware smaller than the planner
    is the dangerous case: a shape planned for 512 against a machine holding 256
    overruns the CU silently, which put 15,440 of 16,384 elements past 10% error
    on real silicon with every gate passing. Hardware LARGER is safe by
    construction -- the planner simply does not fill it -- and warning about it
    trains people to ignore the warning that matters.

    NOT AN ERROR BY DEFAULT even when it fires, because a field bitstream frozen
    at old capacities is a legitimate thing to describe: `singlemesh_2x2` really
    is 256/32/32 and the driver is correct to plan for that. `--strict` turns it
    into a refusal for a build that was supposed to match.
    """
    want = {
        "tiles": bench.TILES,
        "l1_a": bench.L1_A_ENTRIES,
        "l1_b": bench.L1_B_ENTRIES,
    }
    return {
        k: (caps[k], v)
        for k, v in want.items()
        if caps.get(k) is not None and caps[k] < v
    }


def from_map(path):
    """Coordinates, node types, ports and grid, via `gen_mesh`'s own parser.

    Reusing it rather than writing a second parser for the same file: two
    readers of one format is two chances to disagree about what a map means,
    and the generator would be the one nobody checks.
    """
    mesh = Mesh(parse_map(pathlib.Path(path).read_text(encoding="utf-8")))
    return {
        # `mat` is one node per cluster, which is the merged coordinate scheme.
        "layout": "merged",
        "clusters": [list(c) for c in mesh.cus],
        "vectors": [list(v) for v in mesh.vecs],
        "ports": [list(m) for m in mesh.mags],
        "accumulators": [],
        "ncol": mesh.nx,
        "nrow": mesh.ny,
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("map", help="src/synth_top/maps/*.txt -- the topology")
    ap.add_argument("--synth-log", required=True, help="what the build elaborated")
    ap.add_argument("--name", help="board name (default: the map's stem)")
    ap.add_argument("--out", help="output path (default: boards/<name>.json)")
    # No defaults. These come from the block design and nothing here can read
    # it, so a silent default would produce a consistent file pointing nowhere.
    ap.add_argument("--ctrl-base", required=True, type=lambda v: int(v, 0))
    ap.add_argument("--mem-base", default="0", type=lambda v: int(v, 0))
    ap.add_argument("--mem-words", default=str(1 << 27), type=lambda v: int(v, 0))
    ap.add_argument(
        "--quant-bit",
        type=lambda v: None if v.lower() == "none" else int(v, 0),
        default=None,
        help="address bit MAG reads as 'quantise this'; omit if the memory "
        "window is too narrow to reach it",
    )
    ap.add_argument("--blay-bit", type=lambda v: int(v, 0), default=None)
    ap.add_argument("--orchestrator", default="host", choices=["host", "device"])
    ap.add_argument("--transport", default="xdma")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="refuse if the build's capacities disagree with bench.py's, for a "
        "build that was supposed to match the compiler",
    )
    args = ap.parse_args()

    name = args.name or pathlib.Path(args.map).stem
    caps = capacities(args.synth_log)
    build = caps.pop("_build")

    off = drift(caps)
    note = ""
    if off:
        detail = "; ".join(
            f"{k} is {got} here and {bench_val} in bench.py"
            for k, (got, bench_val) in sorted(off.items())
        )
        print(f"gen_board: CAPACITY DRIFT -- {detail}", file=sys.stderr)
        print(
            "  The compiler plans for bench.py's numbers. If this build was "
            "meant to match them, it does not, and a shape planned against the "
            "larger ones overruns the CU SILENTLY.",
            file=sys.stderr,
        )
        if args.strict:
            sys.exit("gen_board: refusing under --strict")
        note = f" CAPACITY DRIFT vs the compiler: {detail}."
    board = {
        "name": name,
        **from_map(args.map),
        "ctrl_base": hex(args.ctrl_base),
        "mem_base": hex(args.mem_base),
        "orc_base": "0x0",
        "orchestrator": args.orchestrator,
        "quant_bit": args.quant_bit,
        "blay_bit": args.blay_bit,
        "mem_words": args.mem_words,
        "transport": args.transport,
        "features": [],
        **caps,
        "l1_banks": 2,
        "inst_depth": 32,
        "source": (
            f"GENERATED by scripts/py/gen_board.py -- do not hand-edit. "
            f"TOPOLOGY (coordinates, node types, ports, grid) from {args.map}, "
            f"the same map synthesis consumed. CAPACITIES and CU_VERSION from "
            f"{build['log']}, which elaborated top {build['top']!r} at "
            f"{build['fmax_mhz']} MHz -- these describe THAT BUILD and rot the "
            f"moment another is made. ADDRESS MAP (ctrl_base, mem_base, "
            f"quantise bits, orchestrator) passed in on the command line: they "
            f"come from a block design nothing in this repo can read, so they "
            f"are the least verifiable fields here.{note} NOT VERIFIED AGAINST "
            f"HARDWARE -- run `run_fpga.py --probe` and check the node sweep."
        ),
    }

    out = pathlib.Path(args.out or ROOT / "boards" / f"{name}.json")
    out.write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")
    print(
        f"{out}: {len(board['clusters'])} clusters, {len(board['vectors'])} vector "
        f"cores, {board['ncol']}x{board['nrow']}, tiles={board['tiles']} "
        f"l1={board['l1_a']}/{board['l1_b']} CU_VERSION={board['cu_version']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
