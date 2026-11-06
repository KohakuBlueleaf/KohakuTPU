"""The package-validation harness, built once per session and run case by case."""

import subprocess

import native
import pytest


@pytest.fixture(scope="session")
def validation(tmp_path_factory):
    """`validation(case)` runs one named case of the harness; a failing case exits
    nonzero."""
    exe = native.on_double(
        tmp_path_factory.mktemp("validation"),
        "package_validation",
        ["firmware/dispatch/engine.c", "firmware/unit/enumerate.c"],
        app="firmware/package/interp.c",
        freestanding=False,
        change=(
            "static KA_HOT uint64_t rd(uint64_t off)\n{",
            "static KA_HOT uint64_t rd(uint64_t off)\n{\n    read_boundary(off);",
        ),
    )

    def run(case):
        subprocess.run([str(exe), case], check=True, timeout=10)

    return run
