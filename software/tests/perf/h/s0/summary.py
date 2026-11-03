"""An S0 directory's per-shape summary, from its records alone.

    python -m h.s0.summary measurements/data/h1/s0.multimesh_v9t8.mid

`summary.csv` has a row a point (case, R, variant) over its warm replays:
medians of C (the card's cycles of a replay call), D (cycles - steps), C-D, the
ring cycles of every launch, of the body launches and per repetition, each kind's,
each unit's busy/retired change, f (body per repetition over the family's full
case at the same R and variant), the work share and f/share, and the outputs'
check headlines. `launches.csv` has a row a launch position of a replay call.
`kernels.json` gives each emitted kernel its L2 signature and the g1 ViT-B kernels
of the same board with that signature.
"""

import csv
import gzip
import json
import pathlib
import statistics
import sys
from collections import defaultdict

import traces

ROOT = pathlib.Path(__file__).resolve().parents[5]
G1 = ROOT / "measurements" / "data"
#: A point: its case, repetitions and S0.2 variant.
KEY = ("case", "R", "variant")


def jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line]


def median(values: list) -> float | None:
    return statistics.median(values) if values else None


def signature(l2: str) -> str:
    found = traces.SIGNATURE.search(l2)
    return found[1] if found else ""


def kernels(out: pathlib.Path, board: str, used: dict[str, set]) -> dict:
    """Each kernel's signature, the g1 kernels sharing it and the cases using it."""
    g1 = G1 / f"vitb.g1j.{board}.1n" / "kernels.l2.json.gz"
    seen = defaultdict(list)
    if g1.exists():
        for name, l2 in json.loads(gzip.decompress(g1.read_bytes())).items():
            seen[signature(l2)].append(name)
    got = {}
    for path in sorted((out / "kernels").glob("*.l2")):
        sig = signature(path.read_text("utf-8"))
        got[path.stem] = dict(
            signature=sig, g1=seen.get(sig, []), cases=sorted(used.get(path.stem, ()))
        )
    return got


def unit_columns(units: list[dict]) -> dict:
    """(sysnode, x, y) -> "MG0".. in coordinate order within each unit type."""
    keys = sorted(
        {(r["node"], u["unit"], u["x"], u["y"]) for r in units for u in r["units"]}
    )
    names, count = {}, defaultdict(int)
    for sysnode, kind, x, y in keys:
        names[(sysnode, x, y)] = f"{kind}{count[(sysnode, kind)]}"
        count[(sysnode, kind)] += 1
    return names


def by_call(rows: list[dict]) -> dict:
    """Rows by point, then by call."""
    grouped = defaultdict(lambda: defaultdict(list))
    for r in rows:
        grouped[tuple(r[k] for k in KEY)][r["call"]].append(r)
    return grouped


def point_row(p: dict, cards: list, replayed: list, columns: dict) -> dict:
    """Point `p`'s row: per warm replay call its card rows and ring rows, medians."""
    body, acc = set(p["body"]), defaultdict(list)
    for crow, urow in zip(cards, replayed, strict=True):
        c = sum(r["cycles"] for r in crow)
        d = sum(r["cycles"] - r.get("steps", r["cycles"]) for r in crow)
        acc["C"].append(c)
        acc["D"].append(d)
        acc["C_minus_D"].append(c - d)
        acc["launches"].append(len(urow))
        acc["ring"].append(sum(u["cycles"] for u in urow))
        acc["body"].append(
            sum(u["cycles"] for u in urow if not body or u["kind"] in body)
        )
        each = defaultdict(int)
        for u in urow:
            each[f"kind_{u['kind']}"] += u["cycles"]
            for unit in u["units"]:
                name = columns[(u["node"], unit["x"], unit["y"])]
                each[f"busy_{name}"] += unit["d_busy"]
                each[f"retired_{name}"] += unit["d_retired"]
        for name, v in each.items():
            acc[name].append(v)
    check = p["check"]
    row = {x: p[x] for x in KEY} | dict(
        family=p["family"],
        full=p["full"],
        share=p["share"],
        shape="x".join(str(s) for s in p["shape"]),
        replays=len(cards),
        ring_joined=p["ring"]["joined"],
    )
    row |= {name: median(v) for name, v in acc.items()}
    row["body_per_rep"] = row["body"] / p["R"] if row.get("body") else None
    return row | dict(
        max_abs_f32=check["card_f32"]["absolute"]["max"],
        max_abs_mxfp7=check["card_mxfp7"]["absolute"]["max"],
        max_abs_mxfp7_f32=check["mxfp7_f32"]["absolute"]["max"],
        outside_footprint=check["outside_footprint"],
        nonfinite=check["nonfinite"],
        witness_unequal=sum(w["unequal"] for w in check["witness"].values()),
    )


def launch_row(p: dict, pos: int, us: list[dict], columns: dict) -> dict:
    """Launch position `pos` of point `p`'s replays: its ring cycles and unit busy."""
    cycles = [u["cycles"] for u in us]
    unit = defaultdict(list)
    for u in us:
        for x in u["units"]:
            unit[f"busy_{columns[(u['node'], x['x'], x['y'])]}"].append(x["d_busy"])
    return (
        {x: p[x] for x in KEY}
        | dict(pos=pos, name=us[0]["name"], kind=us[0]["kind"], n=len(us))
        | dict(cycles=median(cycles), min=min(cycles), max=max(cycles))
        | {n: median(v) for n, v in unit.items()}
    )


def summarise(out: pathlib.Path) -> None:
    meta = json.loads((out / "meta.json").read_text("utf-8"))
    points = [p for p in jsonl(out / "cases.jsonl") if "error" not in p]
    card = [r for r in jsonl(out / "card.jsonl") if "case" in r]
    units = [r for r in jsonl(out / "units.jsonl") if "case" in r]
    columns = unit_columns(units)
    card_calls, unit_calls = by_call(card), by_call(units)
    rows, launches, used = [], [], defaultdict(set)
    for r in units + card:
        used[r["name"]].add(r["case"])
    for p in points:
        k = tuple(p[x] for x in KEY)
        warm = [c["call"] for c in p["calls"] if c["replayed"]]
        replayed = [[u for u in unit_calls[k][c] if u["log"] >= 0] for c in warm]
        rows.append(point_row(p, [card_calls[k][c] for c in warm], replayed, columns))
        positions = defaultdict(list)
        for u in (u for us in replayed for u in us):
            positions[u["log"]].append(u)
        launches += [
            launch_row(p, pos, us, columns) for pos, us in sorted(positions.items())
        ]
    full = {(r["case"], r["R"], r["variant"]): r.get("body_per_rep") for r in rows}
    for r in rows:
        base = full.get((r["full"], r["R"], r["variant"]))
        r["f"] = r["body_per_rep"] / base if base and r["body_per_rep"] else None
        r["f_over_share"] = r["f"] / r["share"] if r["f"] else None
    write_csv(out / "summary.csv", rows)
    write_csv(out / "launches.csv", launches)
    found = kernels(out, meta["board"], used)
    (out / "kernels.json").write_text(json.dumps(found, indent=1), "utf-8")


def write_csv(path: pathlib.Path, rows: list[dict]) -> None:
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fields)
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    summarise(pathlib.Path(sys.argv[1]))
