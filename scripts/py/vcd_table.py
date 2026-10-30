#!/usr/bin/env python3
"""Print a VCD as a cycle table: one row per time, one column per signal.

    python scripts/py/vcd_table.py dump.vcd --match st q_valid q_ready stg_gnt
    python scripts/py/vcd_table.py dump.vcd --match w_ready --from 12000 --rows 40

Signals are matched by substring against the full scope path, so a short name is
usually enough. `--changes` prints only rows where something moved, which is what
makes a stalled handshake obvious: the row repeats and nothing advances.
"""

import argparse
import sys


def parse(path, want):
    """Read `path`, returning (names, times) where times is [(t, {name: val})].

    `want` is a list of substrings; a signal is kept if any matches its full
    dotted path. Values are the raw VCD strings, so an x or z shows as itself
    rather than being coerced to a number.
    """
    ids = {}  # vcd id -> display name
    scope = []
    cur = {}
    out = []
    t = 0
    with open(path, "r", errors="replace") as fh:
        in_defs = True
        for line in fh:
            s = line.strip()
            if not s:
                continue
            if in_defs:
                if s.startswith("$scope"):
                    scope.append(s.split()[2])
                elif s.startswith("$upscope"):
                    if scope:
                        scope.pop()
                elif s.startswith("$var"):
                    p = s.split()
                    vid, nm = p[3], p[4]
                    full = ".".join(scope + [nm])
                    if not want or any(w in full for w in want):
                        ids[vid] = full
                elif s.startswith("$enddefinitions"):
                    in_defs = False
                continue
            if s.startswith("#"):
                if cur:
                    out.append((t, dict(cur)))
                t = int(s[1:])
            elif s[0] in "bB":
                val, vid = s.split()
                if vid in ids:
                    cur[ids[vid]] = val[1:]
            elif s[0] in "rR":
                continue
            else:
                vid = s[1:]
                if vid in ids:
                    cur[ids[vid]] = s[0]
    if cur:
        out.append((t, dict(cur)))
    return sorted(ids.values()), out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vcd")
    ap.add_argument("--match", nargs="*", default=[], help="substrings to keep")
    ap.add_argument("--from", dest="t0", type=int, default=0)
    ap.add_argument("--to", dest="t1", type=int, default=0)
    ap.add_argument("--rows", type=int, default=60)
    ap.add_argument(
        "--changes", action="store_true", help="only rows where a value moved"
    )
    a = ap.parse_args()

    names, rows = parse(a.vcd, a.match)
    if not names:
        sys.exit("no signal matched; run without --match to list what is there")
    # Short labels, but keep enough tail to stay unique.
    short = {}
    for n in names:
        parts = n.split(".")
        for k in range(1, len(parts) + 1):
            lab = ".".join(parts[-k:])
            if sum(1 for m in names if m.endswith(lab)) == 1:
                short[n] = lab
                break
        else:
            short[n] = n

    w = max(len(short[n]) for n in names)
    w = max(w, 6)
    print("time".rjust(10) + "  " + "  ".join(short[n].rjust(w) for n in names))

    prev = None
    shown = 0
    for t, vals in rows:
        if t < a.t0 or (a.t1 and t > a.t1):
            continue
        cells = [vals.get(n, "-") for n in names]
        if a.changes and cells == prev:
            continue
        prev = cells
        print(str(t).rjust(10) + "  " + "  ".join(c.rjust(w) for c in cells))
        shown += 1
        if shown >= a.rows:
            print("  ... truncated, raise --rows or narrow --from/--to")
            break


if __name__ == "__main__":
    main()
