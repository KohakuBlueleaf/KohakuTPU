"""Bounded eight-case slices for the costly 197-column kernel witnesses."""

import json
import sys

from relay_gates import OUT, ROOT, run


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    results = []
    rows = [*range(1, 66), 197]
    for op in ("softmax", "layernorm", "max", "prod"):
        for start in range(0, len(rows), 8):
            ids = [
                f"application/tests/kernels/test_vit_rows.py::test_logical_rows[{op}-197-{r}]"
                for r in rows[start : start + 8]
            ]
            results.append(
                run(
                    f"vit-slice-{op}-{start}",
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        *ids,
                        "-q",
                        "-n",
                        "4",
                        "-p",
                        "no:cacheprovider",
                        "--tb=short",
                    ],
                    ROOT / "software",
                    115,
                )
            )
    (OUT / "vit-slices.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8"
    )
    return int(any(r["code"] for r in results))


if __name__ == "__main__":
    sys.exit(main())
