"""C4's inspection of emitted ordering: in an L1 fn, each sysnode's waits before
each of its rings, and the units it sent work to that no wait covers at that ring
(work the ring's release frontier misses). It reads what the current compiler
emits; it patches nothing.

    python software/tests/perf/h/frontier.py --board multimesh_v9t6 \
        software/tests/perf/h/gen/witness/release_frontier.ktpu
"""

import argparse
import collections
import pathlib
import re
import sys

from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language.text import module as M
from kohakutpu.stack import kernel

SEND = re.compile(r"send (\S+)$")
MARK = re.compile(r"(\w+) = mark (\S+)$")
#: A unit wait, to all work sent or up to a mark (not a slot, bell or moves wait).
UNIT_WAIT = re.compile(r"wait (\S+)(?: upto=(\w+))?$")


def rings(l1_text: str, fn: str) -> list[dict]:
    """Each ring statement of `fn`: its sysnode, the waits since the block's last
    ring, and the units with work no wait covers yet."""
    lines = l1_text.split("\n")
    start = next(i for i, s in enumerate(lines) if s.startswith(f"fn {fn}.l1("))
    out: list[dict] = []
    sysnode, sent, done, marks, waits = -1, collections.Counter(), {}, {}, []
    for line in lines[start + 1 :]:
        if line.startswith("fn "):
            break
        s, depth = line.strip(), len(line) - len(line.lstrip())
        if depth == 4 and s.startswith("node "):
            sysnode, sent, done, marks, waits = (
                int(s.split()[1]),
                collections.Counter(),
                {},
                {},
                [],
            )
        if depth != 8:
            continue
        send, mark, wait = SEND.match(s), MARK.match(s), UNIT_WAIT.match(s)
        if send:
            sent[send[1]] += 1
        if mark:
            marks[mark[1]] = sent[mark[2]]
        if s.startswith("wait "):
            waits.append(s)
        if wait:
            done[wait[1]] = marks[wait[2]] if wait[2] else sent[wait[1]]
        if s.startswith("ring "):
            open_ = sorted(u for u, k in sent.items() if k > done.get(u, 0))
            out.append(dict(sysnode=sysnode, ring=s, waits=waits, uncovered=open_))
            waits = []
    return out


def emitted(path: pathlib.Path, board: str, sysnodes: int, fn: str | None = None):
    """The L1 text the current compiler emits for `path`'s fn, and the fn's name."""
    text = M.read(path.read_text("utf-8"), str(path))
    name = fn or text.names()[0]
    level = "l2" if f"fn {name}.l2(" in text.source else "l1"
    c = kernel.compile(text, name, machine_of(board, nodes=sysnodes), level=level)
    return c.l1_text, name


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", required=True)
    ap.add_argument("--sysnodes", type=int, default=4, help="the machine's sysnodes")
    ap.add_argument("--fn", default=None)
    ap.add_argument("path", type=pathlib.Path)
    a = ap.parse_args()
    l1_text, name = emitted(a.path, a.board, a.sysnodes, a.fn)
    for r in rings(l1_text, name):
        print(f"sysnode {r['sysnode']}: {r['ring']}")
        for w in r["waits"]:
            print(f"    after {w}")
        print(f"    uncovered: {', '.join(r['uncovered']) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
