"""Stage 0 table: per case at its largest R, the body cycles a repetition, f against
its family's full shape, f over the shape's share, and the structural / precision
columns of `<case>/summary.csv`.

    python -m h.tables.s0 measurements/data/h1/s0.multimesh_v9t8.mid"""

import csv
import pathlib
import sys

HEAD = (
    "case variant R full share body/rep f f/share outside nonfinite "
    "witness_unequal max_abs_mx"
)


def table(root: pathlib.Path) -> list[str]:
    rows: dict[tuple[str, str], dict] = {}
    for f in sorted(root.glob("*/summary.csv")):
        for r in csv.DictReader(f.open()):
            key = (r["case"], r["variant"])
            if key not in rows or int(r["R"]) > int(rows[key]["R"]):
                rows[key] = r
    out = [HEAD]
    for (case, var), r in sorted(rows.items()):
        full = rows.get((r["full"], var)) or rows.get((r["full"], ""))
        body = float(r["body_per_rep"] or 0)
        whole = float(full["body_per_rep"]) if full and full["body_per_rep"] else 0
        f = body / whole if whole else float("nan")
        share = float(r["share"] or 1)
        out.append(
            f"{case} {var or '-'} {r['R']} {r['full']} {share} {body:.0f} {f:.3f} "
            f"{f / share:.2f} {r['outside_footprint']} {r['nonfinite']} "
            f"{r['witness_unequal']} {r['max_abs_mxfp7']}"
        )
    return out


def main() -> None:
    print("\n".join(table(pathlib.Path(sys.argv[1]))))


if __name__ == "__main__":
    main()
