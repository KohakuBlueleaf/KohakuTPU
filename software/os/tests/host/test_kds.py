"""The dispatcher (kds) on the core-only and card models (model host).

    KOS_BUILD=~/kos/build KOS_OUT=~/kos/out pytest software/os/tests/host/test_kds.py -n 1

Every property has an app that checks it and a mutant image (built here with
-D KDS_MUT=<n>) that the same app must fail on. Card cases need the card
models and the driver path (KOS_CARD_MODELS, KOS_DRIVER_PATH, as
test_card.py); 2-die cases run with KOS_SLOW=1.
"""

import json
import os
import pathlib
import subprocess
import sys

import pytest

TOOLS = pathlib.Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))

from boot import load, parse_flags
from run_core import run

BUILD = pathlib.Path(os.environ.get("KOS_BUILD", pathlib.Path.home() / "kos/build"))
OUT = pathlib.Path(os.environ.get("KOS_OUT", pathlib.Path.home() / "kos/out"))
MODELS = pathlib.Path(
    os.environ.get("KOS_CARD_MODELS", pathlib.Path.home() / "ktpu/build")
)
DRIVER = os.environ.get("KOS_DRIVER_PATH", "")
C1N = MODELS / "v9t6/vlt_card_v9t6_1n"
C2N = MODELS / "t2s/vlt_card_v9t6_2n"

needs_core = pytest.mark.skipif(
    not (BUILD / "core2/obj_dir/vsim").exists(), reason="no core models"
)
needs_card = pytest.mark.skipif(
    not DRIVER or not C1N.exists(), reason="no card models or driver path"
)
needs_2n = pytest.mark.skipif(
    not C2N.exists() or os.environ.get("KOS_SLOW") != "1",
    reason="no 2-die model, or KOS_SLOW unset",
)

#: mutant -> what it breaks
MUTANTS = {
    1: "predecessors ignored",
    2: "DEP messages dropped",
    3: "free extents never merge",
    4: "predecessor order unchecked",
    5: "unit patches get the user address",
    6: "no release before a unit task",
    7: "no acquire after a unit task",
    8: "L0 QUERY returns a superpage leaf's own page",
}


@pytest.fixture(scope="session")
def mutant_images(tmp_path_factory):
    """Build each mutant image once: {n: path of kos.elf}."""
    root = tmp_path_factory.mktemp("kds_mutants")
    images = {}
    for n in MUTANTS:
        out = root / f"mut{n}"
        subprocess.run(
            [
                sys.executable,
                str(TOOLS / "build.py"),
                "kos",
                "--out",
                str(out),
                "-D",
                f"KDS_MUT={n}",
            ],
            check=True,
            capture_output=True,
        )
        images[n] = out / "kos.elf"
    return images


def core(board: str, cmd: str, image: pathlib.Path | None = None, cycles=60_000_000):
    return run(
        load(board),
        BUILD / board,
        image or OUT / "kos.elf",
        cmd,
        parse_flags(""),
        0,
        cycles,
    )


def passed(r: dict) -> bool:
    return not r["fails"] and r["rc"] == 0


def must_pass(r: dict) -> dict:
    assert passed(r), "\n".join(r["fails"]) + r["output"][-4000:]
    return r["measures"]["0"]


# ---- the core models: graphs, the arena, refusals ----


@needs_core
@pytest.mark.parametrize("board", ["core2", "core4s"])
@pytest.mark.parametrize("flags", ["", "-u"])
def test_chain(board, flags):
    """A chain over every node: each step checks it got its predecessor's
    word, carried by a move into its landing zone."""
    m = must_pass(core(board, f"kdschain 12 {flags}"))
    assert m["kdschain.steps"] == 12


@needs_core
@pytest.mark.parametrize("board", ["core2", "core4s"])
def test_reduce(board):
    """Every node sums its own values; the sums meet on node 0."""
    must_pass(core(board, "kdsreduce 256"))


@needs_core
@pytest.mark.parametrize("board", ["core2", "core4s"])
def test_refusals(board):
    """Forward predecessors and cycles, unknown units and nodes with no
    server are refused; a failing CPU task here or on another node fails the
    graph with its index and return; a deadline answers ETIME; the servers
    work afterwards."""
    must_pass(core(board, "kdsbad"))


@needs_core
@pytest.mark.parametrize(
    "board,flags", [("core2", ""), ("core4s", "-b"), ("core4s", "-u")]
)
def test_arena(board, flags):
    """Random allocation and free on every node: aligned, inside a chunk, no
    overlap, contents kept, translated on every node to the address the
    reference gives; frees merge; big blocks take a chunk trim returns; a
    thread's blocks go with free_owner. On core4s also a system call's copy
    of a shared buffer past the region's first page."""
    m = must_pass(core(board, f"kdsalloc 64 {flags}"))
    if board == "core4s":
        assert m["kdsalloc.shared_copy"] == 1


@needs_core
def test_multi_home():
    """One client per node, each the home of its own chain, all at once."""
    must_pass(core("core4s", "kdsmulti 6"))


@needs_core
def test_bench():
    m = must_pass(core("core4s", "kdsbench 32"))
    for k in ("one", "chain", "wide", "cpu", "hop", "load", "alloc_here", "alloc_far"):
        assert m[f"kdsbench.{k}"] > 0, k


# ---- mutants: each check fails on its mutant ----


@needs_core
@pytest.mark.parametrize(
    "n,board,cmd",
    [
        (1, "core2", "kdschain 6"),
        (2, "core2", "kdschain 6"),
        (3, "core2", "kdsalloc 64"),
        (4, "core2", "kdsbad"),
        (8, "core4s", "kdsalloc 16"),
    ],
)
def test_core_mutants(mutant_images, n, board, cmd):
    r = core(board, cmd, mutant_images[n], cycles=40_000_000)
    assert not passed(r), f"mutant {n} ({MUTANTS[n]}) passed {cmd}"


# ---- the card models: units ----


def card(model: pathlib.Path, board: str, cmd: str, tmp: pathlib.Path, image=None):
    out = tmp / "run.json"
    args = [sys.executable, str(TOOLS / "run_card.py"), "--board", board]
    args += ["--model", str(model), "--image", str(image or OUT / "kos.elf")]
    args += ["--cmd", cmd, "--out", str(tmp / "run"), "--json", str(out)]
    args += ["--timeout", "1500"]
    p = subprocess.run(
        args,
        cwd=TOOLS,
        env=dict(os.environ, PYTHONPATH=DRIVER),
        capture_output=True,
        text=True,
        check=False,
    )
    ok = p.returncode == 0 and "FAIL" not in p.stdout.replace("run_card: PASS", "")
    return ok, p, (json.loads(out.read_text()) if out.exists() else {})


@needs_card
@pytest.mark.parametrize("flags", ["", "-e", "-u"])
def test_mg_1n(tmp_path, flags):
    """MG FILL, FILL, DRAIN with addresses patched at bind time, sentinels
    around the DRAIN checked by a CPU task on the unit's node; rounds issued
    without waits (batched with -e)."""
    ok, p, r = card(C1N, "card_v9t6_1n", f"kdsmg 8 {flags}", tmp_path)
    assert ok, p.stdout[-4000:]
    assert r["measures"]["0"]["kdsmg.units"] == 2


@needs_card
@pytest.mark.parametrize("n", [5, 6, 7])
def test_mg_mutants_1n(tmp_path, mutant_images, n):
    ok, _, _ = card(C1N, "card_v9t6_1n", "kdsmg 0", tmp_path, mutant_images[n])
    assert not ok, f"mutant {n} ({MUTANTS[n]}) passed kdsmg"


@needs_card
@needs_2n
def test_mg_2n(tmp_path):
    """Die 1's units through die 1's server, DRAIN across the interlink into
    node 0's landing zone; a part loaded into die 1 by the mover."""
    ok, p, r = card(C2N, "card_v9t6_2n", "kdsmg 8 -e; kdschain 8", tmp_path)
    assert ok, p.stdout[-4000:]
    assert r["measures"]["0"]["kdsmg.units"] == 4
