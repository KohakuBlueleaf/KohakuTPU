"""What a run directory's meta.json holds besides its arguments: the commit, the
firmware image, the stack's knobs, every die's clocks, and the sysnodes' cycle
offsets, bracketed, with their drift."""

import json
import os
import pathlib
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parents[4]


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


#: A tree copied without git (the Linux host's) carries the copy's identity here,
#: written by the side that copied it.
STAMP = ROOT / "COMMIT.json"


def commit() -> dict:
    """HEAD, and whether tracked files differ from it."""
    if not (ROOT / ".git").exists():
        return json.loads(STAMP.read_text(encoding="utf-8"))
    dirty = git("status", "--porcelain", "--untracked-files=no")
    return dict(head=git("rev-parse", "HEAD"), dirty=bool(dirty))


def image(path: pathlib.Path) -> dict:
    """A file by git's index blob when tracked, else by its size and mtime."""
    staged = (ROOT / ".git").exists() and git(
        "ls-files", "--stage", "--", str(path.resolve())
    )
    if staged:
        return dict(path=str(path), blob=staged.split()[1])
    st = path.stat()
    return dict(path=str(path), bytes=st.st_size, mtime=st.st_mtime)


def knobs() -> dict:
    """Every stack knob this process sees."""
    return {
        k: v
        for k, v in sorted(os.environ.items())
        if k.startswith(("KTPU_", "KOHAKUTPU_"))
    }


def timebase(card) -> dict:
    """Each opened sysnode's HR_CYCLES read between two reads of the first
    sysnode's: its offset from the first's count lies in [lo, hi] with no host
    clock (a measure-ring `start` minus the offset is on the first's count, to
    within hi - lo, the reads' cost). `ns`: the host's clock around the middle
    read, for host / device correlation."""
    first = card.nodes[0]

    def cycles(n: int) -> int:
        return card.boots[n][0].win.cycles()

    reads = {}
    for n in card.nodes:
        a = cycles(first)
        t0 = time.perf_counter_ns()
        c = cycles(n)
        ns = (t0, time.perf_counter_ns())
        z = cycles(first)
        reads[n] = dict(cycles=c, first=(a, z), lo=c - z, hi=c - a, ns=ns)
    return reads


def drift(before: dict, after: dict) -> dict:
    """Each sysnode's offset change between two `timebase`s, as bounds, over the
    first sysnode's cycles between them."""
    return {
        n: dict(
            lo=after[n]["lo"] - before[n]["hi"],
            hi=after[n]["hi"] - before[n]["lo"],
            over=after[n]["first"][0] - before[n]["first"][1],
        )
        for n in before
    }
