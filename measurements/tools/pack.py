"""Pack complete OR failed traces, including provenance, intervals and diagnostics.

Failed attempts retain their complete original tree under raw/ (gzipped files),
not just the successful prefix. meta.json is written last; pack.json commits the
pack. No pack is interpreted as evidence that a card run succeeded.
"""

import gzip
import hashlib
import json
import pathlib
import shutil
import sys


def pack(trace, out):
    metadata = json.loads((trace / "meta.json").read_text("utf-8"))
    out.mkdir(parents=True, exist_ok=True)
    timed = [p.name for p in trace.glob("timed.*.jsonl")]
    for name in (
        "card.jsonl",
        "units.jsonl",
        "profile.json",
        "launches.jsonl",
        "inputs.jsonl",
        "timebase.jsonl",
        "retirements.jsonl",
        "outputs.jsonl",
        *timed,
    ):
        src = trace / name
        if src.exists():
            (out / f"{name}.gz").write_bytes(gzip.compress(src.read_bytes()))
    for name in (
        "waste.json",
        "steady.json",
        "report.json",
        "attempt.json",
        "post.json",
        "provenance.json",
        "command.json",
        "ring-loss.json",
        "clock-checks.json",
    ):
        if (trace / name).exists():
            shutil.copyfile(trace / name, out / name)
    sizes, programs = {}, {}
    for path in sorted((trace / "kernels").glob("*.l1")):
        l2_path = path.with_suffix(".l2")
        l2 = l2_path.read_text("utf-8") if l2_path.exists() else ""
        programs[path.stem] = l2
        sizes[path.stem] = {
            "l1": len(path.read_text("utf-8").splitlines()),
            "l2": len(l2.splitlines()),
        }
    graphs = {
        p.name: json.loads(p.read_text("utf-8"))
        for p in (trace / "kernels").glob("*.graph.json")
    }
    (out / "kernels.graph.json.gz").write_bytes(
        gzip.compress(json.dumps(graphs).encode())
    )
    (out / "kernels.json").write_text(json.dumps(sizes), "utf-8")
    (out / "kernels.l2.json.gz").write_bytes(
        gzip.compress(json.dumps(programs).encode())
    )
    # Keep diagnostics and preflight even for successes; a failed attempt keeps
    # every byte, including native packages and incomplete kernel/launch files.
    post_path = trace / "post.json"
    post_failed = post_path.exists() and not json.loads(
        post_path.read_text("utf-8")
    ).get("complete")
    failed = (
        metadata.get("done") != "exited"
        or (trace / "failure.txt").exists()
        or post_failed
    )
    manifest = {}
    for source in sorted(p for p in trace.rglob("*") if p.is_file()):
        relative = source.relative_to(trace)
        if not failed and not (
            relative.parts[0] == "preflight"
            or any(
                word in source.name
                for word in ("failure", "klog", "kds-", ".log", ".stderr")
            )
        ):
            continue
        raw = source.read_bytes()
        target = out / "raw" / relative.with_name(relative.name + ".gz")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(gzip.compress(raw))
        manifest[str(relative)] = hashlib.sha256(raw).hexdigest()
    (out / "meta.json").write_text(json.dumps(metadata, indent=1), "utf-8")
    (out / "pack.json").write_text(
        json.dumps({"schema": 2, "failed": failed, "raw_sha256": manifest}, indent=1),
        "utf-8",
    )


def main():
    pack(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]))


if __name__ == "__main__":
    main()
