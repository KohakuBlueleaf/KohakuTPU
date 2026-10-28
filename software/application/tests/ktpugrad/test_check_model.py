"""Bounded one-process reduced LeNet check on the supplied Verilated card.

Run separately with -n 1. KTPU_CHECK_LIBRARY must contain LeNet b1 kernels for
vlt_v9t6s_1n; prepare it offline with scripts/py/templates/prebuild.py.
"""

import json
import os
import pathlib
import subprocess
import sys

import pytest

from kohakutpu.application.runtime import device as D


def test_step_checker_reduced_lenet_on_model(tmp_path):
    root = pathlib.Path(D.ROOT)
    build = root / "build/v9t6s/vlt_card_v9t6s_1n"
    if (
        not (build / "obj_dir/vsim.exe").exists()
        and not (build / "obj_dir/vsim").exists()
    ):
        pytest.skip(f"supplied prebuilt Verilated model missing: {build}")
    library = pathlib.Path(
        os.environ.get("KTPU_CHECK_LIBRARY", root / "build/templates")
    )
    if not library.is_dir():
        pytest.skip(f"prebuilt LeNet b1 library missing: {library}")
    output = tmp_path / "kernels.json"
    command = [
        sys.executable,
        str(root / "scripts/py/check/step_kernels.py"),
        str(output),
        "--device",
        "sim",
        "--board",
        "vlt_v9t6s_1n",
        "--build",
        str(build),
        "--nodes",
        "1",
        "--library",
        str(library),
        "--batch",
        "1",
        "--steps",
        "1",
        "--check-step",
        "0",
        "--no-jit",
        "--kernel",
        "^0:",
        "--data",
        "synthetic",
    ]
    proc = subprocess.run(
        command, cwd=root, capture_output=True, text=True, timeout=540, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["numeric"] and result["passed"]
    assert result["checked_kernels"] == 1
    assert result["counters"]["compile_calls"] == 0
    assert result["counters"]["library_misses"] == 0
