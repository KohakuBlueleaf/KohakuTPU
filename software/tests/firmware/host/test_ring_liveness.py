"""Kernel ring waits terminate without issuing into a full egress queue."""

import subprocess

import native
import pytest


@pytest.fixture(scope="module")
def ring_exe(tmp_path_factory):
    out = tmp_path_factory.mktemp("rings") / "rings"
    os_dir = native.SOFTWARE / "kohakuaccel" / "os"
    subprocess.run(
        [
            "gcc",
            f"-I{native.HARNESS / 'shim'}",
            f"-isystem{os_dir / 'include'}",
            f"-I{os_dir / 'kernel'}",
            *native.FLAGS,
            "-ffunction-sections",
            "-fdata-sections",
            "-Wl,--gc-sections",
            "-DKOS_DE_TIMEOUT=256UL",
            f'-DAPP_SOURCE="{(os_dir / "kernel/msg.c").as_posix()}"',
            str(native.HARNESS / "kos_ring.c"),
            "-o",
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return out


def run_ring(exe, mode):
    return subprocess.run(
        [str(exe), mode],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout


@pytest.mark.parametrize(("mode", "written"), [("room", 4), ("drain", 1)])
def test_stopped_egress_reports_without_another_write(ring_exe, mode, written):
    output = run_ring(ring_exe, mode)
    assert "ring egress timeout" in output
    assert f"writes {written}\n" in output
    assert run_ring(ring_exe, "progress") == "progress 65540\n"
