"""Package serialization and lowering retain the native mutation scope."""

import json

from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF


def test_package_build_mutates_serialization_and_lowering():
    text = (MF.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")
    builder = json.dumps({"credit": 32, "types": [[1, 1, "MG"]]})
    calls = json.dumps([["unit", [1, 1]], ["await_", 0, 1]])
    normal = _rs.package_build(builder, calls, text, lower=True)
    for mutant in ("l0.pkg/version", "l0.lower/wait_code"):
        changed = _rs.package_build(builder, calls, text, lower=True, mutant=mutant)
        assert changed != normal, mutant
