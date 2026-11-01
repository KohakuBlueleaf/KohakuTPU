"""Print the raw step record of a traced run's largest kernel, on the host."""

import json
import pathlib
import sys

trace = pathlib.Path(sys.argv[1])
rows = [json.loads(x) for x in (trace / "card.jsonl").read_text("utf-8").splitlines()]
big = max(rows, key=lambda r: r["cycles"])
t = big.get("trace") or {}
print(
    big["name"][:12],
    big["kind"],
    big["cycles"],
    {k: big[k] for k in ("head", "bind", "steps", "barrier", "total", "sent")},
)
print(t.get("header"), t.get("consumed"), len(t.get("packages") or []))
for p in t.get("packages") or []:
    s = p.get("steps") or []
    print(p.get("tag"), len(s), p.get("units"), p.get("fail"))
    for x in s[:4] + s[-4:]:
        print(x)
print("rows with a trace", sum(1 for r in rows if r.get("trace")), "of", len(rows))
print(
    "sum packages", sum(len((r.get("trace") or {}).get("packages") or []) for r in rows)
)
