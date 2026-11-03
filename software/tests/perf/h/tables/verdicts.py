"""Verdict counts of hrun outputs (`data/h1/<row>.<board>.mid[.<variant>]`): per fn
and phase, the structural / precision tallies, the median cycles of timing-valid runs
and the first failure reasons.

    python -m h.tables.verdicts measurements/data/h1/c1.multimesh_v9t6.mid ..."""

import collections
import gzip
import json
import pathlib
import statistics
import sys


def runs(path: pathlib.Path) -> list[dict]:
    """A row's runs, from `runs.jsonl` or `runs.jsonl.gz`."""
    plain = path / "runs.jsonl"
    with plain.open() if plain.exists() else gzip.open(f"{plain}.gz", "rt") as f:
        return [json.loads(line) for line in f]


def table(path: pathlib.Path) -> list[str]:
    rows = runs(path)
    by = collections.defaultdict(list)
    for r in rows:
        by[(r["name"], r.get("phase", "all"))].append(r)
    out = [f"== {path}: {len(rows)} runs"]
    for (name, phase), rs in sorted(by.items()):
        if "verdict" not in rs[0]:
            out.append(f"{name} {phase} ERROR {rs[0].get('error', '')[:150]}")
            continue
        s = collections.Counter(r["verdict"]["structural"] for r in rs)
        p = collections.Counter(r["verdict"]["precision"] for r in rs)
        t = [r["cycles"] for r in rs if r["verdict"].get("timing") == "VALID"]
        med = statistics.median(t) if t else "-"
        why = next((r["verdict"]["reasons"] for r in rs if r["verdict"]["reasons"]), "")
        out.append(
            f"{name} {phase} n={len(rs)} {dict(s)} {dict(p)} valid={len(t)} "
            f"med={med} {str(why)[:160]}"
        )
    return out


def main() -> None:
    for path in sys.argv[1:]:
        print("\n".join(table(pathlib.Path(path))))


if __name__ == "__main__":
    main()
