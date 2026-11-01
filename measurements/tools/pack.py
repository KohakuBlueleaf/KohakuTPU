"""Pack a recorded trace for the zoo, on the host: raw records gzipped, binds dropped,
one row of kernel sizes, so `data/` stays small enough to keep.

    python measurements/tools/pack.py TRACE_DIR OUT_DIR
"""

import gzip
import json
import pathlib
import sys


def main():
    trace, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    timed = [p.name for p in trace.glob("timed.*.jsonl")]
    for name in ("card.jsonl", "units.jsonl", "profile.json", *timed):
        src = trace / name
        if src.exists():
            (out / f"{name}.gz").write_bytes(gzip.compress(src.read_bytes()))
    if (trace / "waste.json").exists():
        waste = json.loads((trace / "waste.json").read_text("utf-8"))
        waste["findings"] = waste["findings"][:400]
        (out / "waste.json").write_text(json.dumps(waste), "utf-8")
    rows = []
    for line in (trace / "launches.jsonl").read_text("utf-8").splitlines():
        row = json.loads(line)
        row.pop("binds", None)
        rows.append(json.dumps(row))
    text = "\n".join(rows) + "\n"
    (out / "launches.jsonl.gz").write_bytes(gzip.compress(text.encode()))
    sizes = {}
    for path in sorted((trace / "kernels").glob("*.l1")):
        l2 = path.with_suffix(".l2")
        sizes[path.stem] = {
            "l1": len(path.read_text("utf-8").splitlines()),
            "l2": len(l2.read_text("utf-8").splitlines()),
        }
    (out / "kernels.json").write_text(json.dumps(sizes), "utf-8")
    (out / "meta.json").write_text((trace / "meta.json").read_text("utf-8"), "utf-8")


if __name__ == "__main__":
    main()
