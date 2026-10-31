"""The firmware's tasks and heaps on ka-emu: osprobe checks itself on the node
and exits 0 only when every check passed; the same boots the emulator runs
through its command line. Needs `KA_EMU_MODEL` and `KA_EMU_FIRMWARE`."""

import os
import pathlib
import struct
import subprocess

MODEL = pathlib.Path(os.environ.get("KA_EMU_MODEL", "build/emu"))
FIRMWARE = pathlib.Path(os.environ.get("KA_EMU_FIRMWARE", "build/fw"))
BOOT_PA = 0x10000
QUEUE = 0x1040_0000
BOOT = struct.pack("<QQQQQQQQQ", 0x31544F4F42414B, 3, QUEUE, 0, 0, 1 << 24, 0, 0, 0)


def run_osprobe(tmp_path, boot):
    blob = tmp_path / "boot.bin"
    blob.write_bytes(boot)
    cmd = [str(MODEL / "obj_dir" / "vsim"), "--elf", str(FIRMWARE / "osprobe.elf")]
    cmd += ["--blob", f"0:{BOOT_PA:#x}:{blob}", "--max-cycles", "20000000"]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def test_every_check_passes(tmp_path):
    done = run_osprobe(tmp_path, BOOT)
    assert done.returncode == 0, done.stdout[-1500:]
    assert "FAIL" not in done.stdout


def test_a_missing_boot_block_is_told(tmp_path):
    assert run_osprobe(tmp_path, b"\0" * 8).returncode != 0
