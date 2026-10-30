"""Apply `planner_regold`'s JSON-pointer patches to the plan goldens.

Each patched file is first shown to re-serialise to its own bytes in one of
the goldens' layouts (``json.dumps`` indent 1 or 2, keys sorted or not, its
own line ends), so the patched values are the only bytes that change. Originals go to ``OUT/originals/``, patched files to
``OUT/updated/``; with ``--write`` the patched files replace the goldens.

    python scripts/py/measure/mmeff/apply_patches.py OUT [--write]
"""

import argparse
import json
import pathlib
import shutil
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[4]
PLAN = ROOT / "software" / "rust" / "golden" / "plan"


#: The layouts the goldens are written in: (indent, sorted keys).
LAYOUTS = [(1, False), (1, True), (2, False), (2, True)]


def dumps(doc, newline: str, layout) -> str:
    indent, sort_keys = layout
    return (json.dumps(doc, indent=indent, sort_keys=sort_keys) + "\n").replace(
        "\n", newline
    )


def put(doc, pointer: str, value) -> None:
    *path, last = pointer.lstrip("/").split("/")
    node = doc
    for key in path:
        node = node[int(key)] if isinstance(node, list) else node[key]
    if isinstance(node, list):
        node[int(last)] = value
    else:
        assert last in node, pointer
        node[last] = value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=pathlib.Path)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    by_file = defaultdict(list)
    for p in json.loads((args.out / "patches.json").read_text()):
        by_file[p["file"]].append(p)
    for sub in ("originals", "updated"):
        (args.out / sub).mkdir(parents=True, exist_ok=True)
    for name, patches in sorted(by_file.items()):
        src = PLAN / name
        text = src.read_bytes().decode()
        newline = "\r\n" if "\r\n" in text else "\n"
        doc = json.loads(text)
        layout = next(
            (x for x in LAYOUTS if dumps(doc, newline, x) == text),
            None,
        )
        assert layout is not None, f"{name} re-serialises in no layout to its bytes"
        for p in patches:
            put(doc, p["pointer"], p["value"])
        shutil.copyfile(src, args.out / "originals" / name)
        (args.out / "updated" / name).write_bytes(dumps(doc, newline, layout).encode())
        if args.write:
            shutil.copyfile(args.out / "updated" / name, src)
    print(f"{len(by_file)} plan goldens patched{' and written' if args.write else ''}")


if __name__ == "__main__":
    main()
