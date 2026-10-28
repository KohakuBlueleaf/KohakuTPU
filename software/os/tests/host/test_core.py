"""KohakuAccel OS on the core-only cluster models (run on the model host).

    KOS_BUILD=~/kos/build KOS_OUT=~/kos/out pytest software/os/tests/host -n 1

Each case boots the image on every node of a board and checks what the apps
print: no FAIL line, every node exited 0, and the case's own facts.
"""

import os
import pathlib
import sys

import pytest

TOOLS = pathlib.Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))

from boot import load, parse_flags
from run_core import run

BUILD = pathlib.Path(os.environ.get("KOS_BUILD", pathlib.Path.home() / "kos/build"))
OUT = pathlib.Path(os.environ.get("KOS_OUT", pathlib.Path.home() / "kos/out"))

pytestmark = pytest.mark.skipif(
    not (BUILD / "core2/obj_dir/vsim").exists(), reason="no core models"
)

# One app per case, small sizes: every case stays under a minute of model time
# (~100-150 kcycles/s; a remote spawn alone is ~80 k cycles). The measured
# runs (RESULTS.md) use the full sizes through tools/run_core.py.
SMP = {
    "hello": ("hello", {}),
    "preduce": ("preduce 512", {}),
    "psort": ("psort 256", {}),
    "pmatmul": ("pmatmul 8", {}),
    "workq": ("workq 8", {"workq.tasks": 8, "workq.workers": 4}),
    "stress": ("stress 1 1 2", {"stress.lock_ops": 1 * 4 * 2}),
    "pipeline": ("pipeline 16", {"pipeline.messages": 16 * 3}),
    "msgsum": ("msgsum 128", {"msgsum.n": 128}),
}
AMP = {
    "hello": ("hello", {}),
    "pipeline": ("pipeline 32", {"pipeline.messages": 32}),
    "msgsum": ("msgsum 256", {"msgsum.n": 256}),
    "mover": ("mover 32", {}),
}
TICK = 50_000
# Each knob with the apps whose paths it changes: (flags, tick, apps).
SMP_KNOBS = [
    ("", 0, list(SMP)),
    ("shared_uncached", 0, ["preduce", "psort", "pmatmul"]),
    ("pt_dram", 0, ["hello", "pipeline"]),
    ("lock_bakery", 0, ["stress"]),
    ("poll", 0, ["pipeline", "msgsum"]),
    ("", TICK, ["workq", "stress"]),
]
AMP_KNOBS = [
    ("", 0, list(AMP)),
    ("pt_dram", 0, ["hello", "mover"]),
    ("poll", 0, ["pipeline", "msgsum"]),
    ("", TICK, ["pipeline", "msgsum"]),
]


def cases(knobs):
    return [
        pytest.param(app, flags, tick, id=f"{app}-{flags or 'none'}-{tick}")
        for flags, tick, apps in knobs
        for app in apps
    ]


def check(r: dict, want: dict) -> None:
    for k, v in want.items():
        assert r["measures"]["0"][k] == v, k


def boot(
    board: str,
    cmd: str,
    flags: str = "",
    tick: int = 0,
    cycles: int = 400_000_000,
    image: str = "kos.elf",
    boots: int = 1,
) -> dict:
    r = run(
        load(board),
        BUILD / board,
        OUT / image,
        cmd,
        parse_flags(flags),
        tick,
        cycles,
        boots=boots,
    )
    assert not r["fails"], "\n".join(r["fails"]) + r["output"][-3000:]
    assert r["rc"] == 0, r["output"][-3000:]
    return r


@pytest.mark.parametrize("app,flags,tick", cases(SMP_KNOBS))
def test_smp_apps_core4s(app, flags, tick):
    """The shared-memory algorithms on four nodes, each under the knobs that
    change its paths."""
    cmd, want = SMP[app]
    check(boot("core4s", cmd, flags, tick), want)


@pytest.mark.parametrize("app,flags,tick", cases(AMP_KNOBS))
def test_amp_apps_core2(app, flags, tick):
    """Message passing only: the board has no shared region. `hello` is the
    first cross-node message: with a slot read that takes the first word
    naming the slot, node 0 drains phantom messages and node 1 waits for
    credit forever."""
    cmd, want = AMP[app]
    check(boot("core2", cmd, flags, tick), want)


@pytest.mark.skipif(
    not (BUILD / "core3s/obj_dir/vsim").exists(), reason="no core3s model"
)
def test_second_board_core3s():
    """A board with no units and three nodes: the same image, the utilities
    and the algorithms, nothing TPU-specific."""
    r = boot("core3s", "ps; kstat; units; workq 6")
    m = r["measures"]["0"]
    assert m["units.count"] == 0 and m["workq.workers"] == 3


def test_channel_flow_control():
    """80 items through a 64-entry channel queue: senders must wait, not drop."""
    r = boot("core4s", "pipeline 80; kstat")
    assert r["measures"]["0"]["pipeline.messages"] == 80 * 3


def test_landing_zone_with_dram_page_tables():
    """With pt_dram the page-table pool sits below the log, so every node
    still finds a peer's landing zone at its span's tail."""
    boot("core2", "mover 32", "pt_dram")


@pytest.mark.parametrize(
    "board,image,cmd",
    [
        ("core4s", "m0.elf", "ring"),
        ("core2", "kos.elf", "hello"),
        ("core4s", "kos.elf", "hello"),
    ],
)
def test_second_boot(board, image, cmd):
    """Two boots in one model: the interlink keeps the first boot's slot counts
    and sent-ring count across the processor reset, as a card does across
    runs. Without the start handshake (include/kos/start.h) the second boot's
    first ring waits on a sent count it never wrote and every node hangs."""
    r = boot(board, cmd, image=image, boots=2, cycles=10_000_000)
    assert "boot 0: ok" in r["output"] and "boot 1: ok" in r["output"]


@pytest.mark.parametrize("image,cmd", [("kos.elf", "hello"), ("m0.elf", "ring")])
def test_log_area(image, cmd):
    """Each node's log area, read from DRAM after the run, holds its whole
    console and says it is done."""
    r = boot("core2", cmd, image=image, cycles=50_000_000)
    for i, nd in enumerate(r["nodes"]):
        lines = [
            ln[len(f"[{i}] ") :]
            for ln in r["output"].splitlines()
            if ln.startswith(f"[{i}] ")
        ]
        assert nd["log"].splitlines() == lines
        assert nd["where"] == "done" and nd["where_a"] == 0


@pytest.mark.parametrize("image", ["kos.elf", "m0.elf"])
def test_progress_word_of_a_hang(image):
    """Node 1 held in reset: node 0 never finishes, its progress word says it
    waits in the start handshake for node 1 (b = 1 + 1), and its log in DRAM
    already holds its banner (the log is written uncached; a cached one
    keeps it in L1 while the node hangs)."""
    r = run(
        load("core2"),
        BUILD / "core2",
        OUT / image,
        "hello" if image == "kos.elf" else "ring",
        0,
        0,
        1_000_000,
        hold=[1],
    )
    assert r["rc"] != 0 and "NEVER FINISHED" in r["output"]
    nd = r["nodes"][0]
    assert nd["where"] == "start" and nd["where_b"] == 2
    banner = "KohakuAccel OS: node 0" if image == "kos.elf" else "m0 node 0"
    assert nd["log"].startswith(banner)


def test_bench_core4s():
    r = boot("core4s", "bench 2")
    m = r["measures"]["0"]
    for k in (
        "bench.syscall",
        "bench.l0",
        "bench.spawn_remote",
        "bench.atomic_remote",
        "bench.chan_rtt",
    ):
        assert m[k] > 0
