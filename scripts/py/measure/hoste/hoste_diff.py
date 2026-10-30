"""Locate trusted local HOSTE transcript differences."""

import argparse
import pickle
from pathlib import Path


def differences(a, b, path=()):
    if type(a) is not type(b):
        print(path, type(a), type(b))
    elif isinstance(a, (list, tuple)):
        if len(a) != len(b):
            print(path, "length", len(a), len(b))
        for i, (x, y) in enumerate(zip(a, b)):
            differences(x, y, (*path, i))
    elif isinstance(a, dict):
        for key in a.keys() | b.keys():
            differences(a.get(key), b.get(key), (*path, key))
    elif isinstance(a, bytes) and a != b:
        positions = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        print(path, "bytes", len(a), len(b), "diff", len(positions), positions[:24])
        for i in positions[:8]:
            start = i // 8 * 8
            print(start, a[start : start + 8].hex(), b[start : start + 8].hex())
    elif a != b:
        print(path, repr(a)[:200], repr(b)[:200])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("left", type=Path)
    ap.add_argument("right", type=Path)
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()
    with args.left.open("rb") as source:
        left = pickle.load(source)
    with args.right.open("rb") as source:
        right = pickle.load(source)
    limit = args.limit
    for i, (a, b) in enumerate(zip(left, right)):
        if a != b:
            print("event", i)
            differences(a, b)
            limit -= 1
            if not limit:
                break


if __name__ == "__main__":
    main()
