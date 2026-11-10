"""Package-check's pure byte/physical-line comparison (logic stage)."""

import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "perf_package_check", Path(__file__).resolve().parents[1] / "perf/package_check.py"
)
assert SPEC is not None and SPEC.loader is not None
package_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package_check)


def test_package_diff_reports_exact_offsets_and_only_the_changed_physical_lines():
    uploaded = bytes(range(192))
    assert package_check.package_diff(uploaded, uploaded, 0x4000) == dict(
        equal=True,
        expected_bytes=192,
        actual_bytes=192,
        differing_bytes=0,
        first_differing_offsets=[],
        lines=[],
    )
    readback = bytearray(uploaded)
    for offset in (1, 63, 128, 191):
        readback[offset] ^= 0xFF
    result = package_check.package_diff(uploaded, bytes(readback), 0x4000)
    assert not result["equal"]
    assert result["first_differing_offsets"] == [1, 63, 128, 191]
    assert result["differing_bytes"] == 4
    assert result["lines"] == [
        dict(
            address=0x4000,
            offset=0,
            expected_hex=uploaded[:64].hex(),
            actual_hex=readback[:64].hex(),
        ),
        dict(
            address=0x4080,
            offset=128,
            expected_hex=uploaded[128:].hex(),
            actual_hex=readback[128:].hex(),
        ),
    ]
    truncated = package_check.package_diff(uploaded, uploaded[:-1], 0x4000)
    assert not truncated["equal"] and truncated["first_differing_offsets"] == [191]
    assert truncated["lines"][0]["actual_hex"] == uploaded[128:191].hex()
