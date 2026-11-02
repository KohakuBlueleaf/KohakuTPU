"""Pack a recorded trace for the zoo, on the host: raw records gzipped, binds and each
kernel's L2 kept, L1 as a line count, so `data/` stays small enough to keep and a hash
still names its program.

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
    launches = (trace / "launches.jsonl").read_bytes()
    (out / "launches.jsonl.gz").write_bytes(gzip.compress(launches))
    sizes, programs = {}, {}
    for path in sorted((trace / "kernels").glob("*.l1")):
        l2 = path.with_suffix(".l2").read_text("utf-8")
        programs[path.stem] = l2
        sizes[path.stem] = {
            "l1": len(path.read_text("utf-8").splitlines()),
            "l2": len(l2.splitlines()),
        }
    (out / "kernels.json").write_text(json.dumps(sizes), "utf-8")
    (out / "kernels.l2.json.gz").write_bytes(
        gzip.compress(json.dumps(programs).encode())
    )
    (out / "meta.json").write_text((trace / "meta.json").read_text("utf-8"), "utf-8")


if __name__ == "__main__":
    main()
