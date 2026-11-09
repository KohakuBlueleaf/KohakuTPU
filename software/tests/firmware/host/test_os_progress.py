"""Cold progress remains visible independently of the scheduler's saved PC."""

import pathlib
import struct
import sys

OS = pathlib.Path(__file__).resolve().parents[3] / "kohakuaccel" / "os"
sys.path.insert(0, str(OS / "tools"))

import klog
from image import abi_const


def test_last_complete_progress_survives_a_wrapped_log_and_partial_write():
    issue = abi_const("KOS_W_RUN_ISSUE")
    arena = abi_const("KOS_W_ARENA")
    ready = abi_const("KOS_W_READY")
    text = (
        b"discarded\n" * klog.LOG
        + f"@where {issue} 2 0\n@where {arena} 2 90000\n".encode()
        + f"@where {ready} 38008000".encode()
    )
    capacity = klog.LOG - 64
    ring = bytearray(capacity)
    for offset, byte in enumerate(text):
        ring[offset % capacity] = byte
    header = [0] * 8
    header[klog.COUNT] = len(text)
    header[klog.WHERE] = abi_const("KOS_W_USER")
    parsed = klog.parse(struct.pack("<8Q", *header) + ring)
    assert parsed["progress"] == "arena"
    assert parsed["progress_a"] == 2 and parsed["progress_b"] == 0x90000
    assert "progress arena a 0x2 b 0x90000" in klog.describe(parsed)
