"""Build firmware-tool packages offline; no transport or model is opened."""

import importlib.util
import json
from pathlib import Path

from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF

ROOT = Path(__file__).resolve().parents[3]
CASES = json.loads(
    (ROOT / "software/rust/golden/bindings/package.json").read_text(encoding="utf-8")
)["cases"]


def tool(name):
    spec = importlib.util.spec_from_file_location(
        f"firmware_{name}", ROOT / f"software/firmware/tools/{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_firmware_packages():
    text = (MF.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")
    want = {c["case"]: c["output"] for c in CASES if c["case"].startswith("build-")}
    two = tool("two_node")
    wait, push = two.packages(0x00800000, 0x1000800000, text, words=64, slot=0)
    assert wait.hex() == want["build-two-node-wait"]["bytes"]
    assert push.hex() == want["build-two-node-push"]["bytes"]
    _, actual = two.packages(two.SRC, (1 << 36) + two.DST, text)
    decoded = _rs.package_read(actual, text)
    codes = _rs.package_format(text)
    assert decoded["steps"][-1] == (codes["op"]["RING"], 0, 1, 7, 0x10001)


def test_signature_is_the_frozen_package_signature():
    # Byte-order witness: FNV over these unit words, computed independently.
    words = [0x1234, 0xFFFF0000, 0x123456789ABCDEF0]
    expected = 0xCBF29CE484222325
    for word in words:
        for byte in word.to_bytes(8, "little"):
            expected = ((expected ^ byte) * 0x100000001B3) & ((1 << 64) - 1)
    assert _rs.package_signature(words) == expected
