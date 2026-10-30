"""Print largest exclusive/inclusive profile rows from a HOSTE measurement."""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()
    report = json.loads(args.path.read_text(encoding="utf-8"))
    rows = report["profile"]
    for row in rows[: args.limit]:
        print(json.dumps({k: v for k, v in row.items() if k != "callers"}))


if __name__ == "__main__":
    main()
