"""Pending slot operations leave the ready feeder available to local producers."""

import subprocess

import native


def test_slot_waits_and_full_egress_do_not_hold_ready_local_work(tmp_path):
    os_dir = native.SOFTWARE / "kohakuaccel" / "os"
    exe = tmp_path / "kds_slots"
    subprocess.run(
        [
            "gcc",
            f"-I{native.HARNESS / 'shim'}",
            f"-isystem{os_dir / 'include'}",
            f"-I{os_dir / 'dispatch'}",
            *native.FLAGS,
            "-ffunction-sections",
            "-fdata-sections",
            "-Wl,--gc-sections",
            f'-DAPP_SOURCE="{(os_dir / "dispatch/graph.c").as_posix()}"',
            str(native.HARNESS / "kds_slots.c"),
            "-o",
            str(exe),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run([str(exe)], check=True, timeout=5)
