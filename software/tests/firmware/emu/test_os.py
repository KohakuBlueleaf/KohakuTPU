"""KohakuAccel OS on ka-emu: each app on every node of a board, with each knob
that changes its path. Needs `KA_EMU_MODEL` (a directory holding obj_dir/vsim,
the emulator) and `KOS_IMAGE` (kos.elf), prepared by the stage harness."""

import os
import pathlib
import struct
import sys

import pytest

OS = pathlib.Path(__file__).resolve().parents[3] / "kohakuaccel" / "os"
sys.path.insert(0, str(OS / "tools"))

from boot import load, parse_flags, regions
from image import abi_const
from run_core import run

from kohakutpu.application.runtime.os import abi as A

MODEL = pathlib.Path(os.environ.get("KA_EMU_MODEL", "build/emu"))
IMAGE = pathlib.Path(os.environ.get("KOS_IMAGE", "build/os/kos.elf"))
#: A case's bound: the slowest measured case takes 11 M cycles a node.
CYCLES = 60_000_000
TICK = 50_000

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
}
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
    ("pt_dram", 0, ["hello"]),
    ("poll", 0, ["pipeline", "msgsum"]),
    ("", TICK, ["pipeline", "msgsum"]),
]


def cases(knobs):
    return [
        pytest.param(app, flags, tick, id=f"{app}-{flags or 'none'}-{tick}")
        for flags, tick, apps in knobs
        for app in apps
    ]


def boot(board, cmd, flags, tick):
    b = load(board)
    extra = ["--flat"] if b["model"]["shared"] else None
    return run(b, MODEL, IMAGE, cmd, parse_flags(flags), tick, CYCLES, None, extra)


def checked(r, want):
    assert r["rc"] == 0 and not r["fails"], r["output"][-2000:]
    for k, v in want.items():
        assert r["measures"]["0"][k] == v, k


@pytest.mark.parametrize("app,flags,tick", cases(AMP_KNOBS))
def test_amp(app, flags, tick):
    cmd, want = AMP[app]
    checked(boot("core2", cmd, flags, tick), want)


@pytest.mark.parametrize("app,flags,tick", cases(SMP_KNOBS))
def test_smp(app, flags, tick):
    cmd, want = SMP[app]
    checked(boot("core4s", cmd, flags, tick), want)


def test_a_wrong_answer_is_told():
    r = boot("core2", "msgsum 256", "", 0)
    with pytest.raises(AssertionError):
        checked(r, {"msgsum.n": 255})


def test_physical_shared_heap_leaves_private_user_mappings_available():
    board = load("card_v9t6_4n")
    board["dram"]["shared"] = 2 << 30
    result = run(
        board,
        MODEL,
        IMAGE,
        "msgsum 256",
        parse_flags("shared_phys"),
        0,
        CYCLES,
        None,
        None,
    )
    checked(result, {"msgsum.n": 256})


@pytest.mark.parametrize("shared", [0, 2 << 30], ids=["board", "model-heap"])
def test_parallel_residents_publish_ready_on_every_node(tmp_path, shared):
    board = load("card_v9t6_4n")
    if shared:
        private = 3 * 4096 * A.OSTPU_TASK_BYTES + (8 << 20)
        board["dram"].update(private=private, shared=shared)
        board["spelling"] = dict(private="own", shared="mesh0")
    extra, mailboxes = [], []
    for node in range(board["nodes"]):
        base, size = regions(board, node)[0][:2]
        address = base + size - A.KOS_LAND_SIZE + A.KDS_LZ_USER
        mailbox = tmp_path / f"ready{node}.bin"
        mailboxes.append(mailbox)
        # Quiescent DE fixture: this consumer stops before descriptor submission.
        # Other DE registers start at zero; control/map writes retain their values.
        extra += [
            "--control-word",
            f"{node}:0x300:0xde01000000000000",
            "--until-word",
            f"{node}:{address:#x}:{A.OSTPU_MAGIC:#x}",
            "--dump",
            f"{node}:{address:#x}:8:{mailbox}",
        ]
    result = run(
        board,
        MODEL,
        IMAGE,
        "parallel ostpu 4096 4096 1 1 -e",
        parse_flags("shared_phys"),
        0,
        CYCLES,
        None,
        extra,
    )
    checked(result, {})
    for node, mailbox in enumerate(mailboxes):
        log = result["nodes"][node]
        marker = abi_const("KOS_W_READY_WRITE")
        assert f"@where {marker} " in log["log"], log
        assert struct.unpack("<Q", mailbox.read_bytes())[0] == A.OSTPU_MAGIC, (
            node,
            result["output"][-2000:],
        )
