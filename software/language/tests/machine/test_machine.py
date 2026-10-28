"""Machine files: every image's file matches its sources, and the reader
returns what the hardware is built with."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from kohakutpu.language import machine

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / "scripts" / "py"


def generate(*args):
    return subprocess.run(
        [sys.executable, "-m", "machine", *args],
        cwd=SCRIPTS,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_every_machine_file_matches_its_sources():
    got = generate("--all", "--check")
    assert got.returncode == 0, got.stdout + got.stderr
    assert "differs" not in got.stdout


#: sr.ldep / sr.wlead / sr.taps read from a copy of sru_pool.v under `root`
#: (its OUT_AT, its `wsd` delay line, its `h0` history), printed as JSON.
SR_ROWS = """
import json, sys
from pathlib import Path
from machine import table
from machine.read import image
image.ROOT = Path(sys.argv[1])
img = object.__new__(image.Image)
print(json.dumps([table.sr_ldep(img)[0], table.sr_wlead(img)[0], table.sr_taps(img)[0]]))
"""


def sr_rows(root: Path) -> list:
    got = subprocess.run(
        [sys.executable, "-c", SR_ROWS, str(root)],
        cwd=SCRIPTS,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(got.stdout)


def test_the_sru_timing_rows_follow_the_pool_rtl(tmp_path):
    rel = Path("src/kohakutpu/sru/pool/sru_pool.v")
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert sr_rows(ROOT) == [18, 2, 32]
    moved = (
        text.replace(
            "localparam integer OUT_AT = 16;", "localparam integer OUT_AT = 17;"
        )
        .replace("reg  [3:0] wsd;", "reg  [5:0] wsd;")
        .replace("reg [23:0] h0 [0:31];", "reg [23:0] h0 [0:63];")
    )
    assert (
        moved.count("OUT_AT = 17") == 1
        and "[5:0] wsd" in moved
        and "h0 [0:63]" in moved
    )
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text(moved, encoding="utf-8")
    assert sr_rows(tmp_path) == [19, 4, 64]


def test_check_fails_on_a_file_that_drifted(tmp_path):
    doc = json.loads(
        (ROOT / "machines" / "multimesh_v9t6.json").read_text(encoding="utf-8")
    )
    doc["machine"]["vc"]["regs"] = 32
    (tmp_path / "multimesh_v9t6.json").write_text(
        json.dumps(doc, indent=1) + "\n", encoding="utf-8"
    )
    got = generate("--ver", "v9t6", "--check", "--out", str(tmp_path))
    assert got.returncode == 1 and "differs" in got.stdout


def test_v9t6_is_the_card_as_built():
    m = machine.load("multimesh_v9t6")
    assert len(m.die.units.mg) == 2 and len(m.die.units.vc) == 2
    assert (m.vc.build, m.vc.regs, m.vc.rf_copies) == ("v21", 128, 2)
    assert m.vc.lanes * 2 == m.die.word_bytes
    assert m.get("vc.lanes") == 16 and m.vc.chunks == 8
    assert m.source("vc.rf_copies").startswith("src/kohakutpu/vector2/v2_core.v:")


def test_dies_differ_where_the_hardware_does():
    four, six, sru = (
        machine.load(n) for n in ("multimesh_v9t8", "multimesh_v9t8-6c2v", "sru_v9t7")
    )
    assert (len(four.die.units.mg), len(six.die.units.mg), len(sru.die.units.mg)) == (
        4,
        6,
        4,
    )
    assert (
        sru.die.units.vc == []
        and len(sru.die.units.sr) == 2
        and sru.sr.profile.F_LANES == 16
    )
    assert four.sr.profile is None


VC_TIMING = {
    "math_math": 20,
    "xbar": 0,
    "unpack_math": 9,
    "math_pack": 20,
    "cond": 2,
    "mx7_pack_words": 16,
}


@pytest.mark.parametrize("name", sorted(VC_TIMING))
def test_vc_timing_rows_name_measured_records(name):
    m = machine.load("multimesh_v9t6")
    src = m.source(f"vc.timing.{name}")
    path, provenance = src.split(":", 1)
    assert path.startswith("machines/measured/vc_timing.")
    record = json.loads((ROOT / path).read_text(encoding="utf-8"))
    parameter = record["parameters"][name]
    assert provenance == f"{name}; vec2_replay/{','.join(parameter['benches'])}"
    assert parameter["benches"]
    assert m.get(f"vc.timing.{name}") == parameter["value"] == VC_TIMING[name]


def _schema_texts() -> list[str]:
    paths = sorted(machine.SCHEMA_DIR.glob("*.rs"), key=lambda p: p.name)
    return [p.read_text(encoding="utf-8") for p in paths]


def test_a_new_schema_field_changes_the_version_and_a_comment_does_not():
    texts = _schema_texts()
    base = machine.schema_version_of(texts)
    assert base == machine.schema_version()
    at = next(i for i, t in enumerate(texts) if "pub gt4_rows: u32," in t)

    def edit(new: str) -> str:
        changed = list(texts)
        changed[at] = texts[at].replace("pub gt4_rows: u32,", new, 1)
        return machine.schema_version_of(changed)

    assert edit("pub gt4_rows: u32,\n    pub gt4_extra: u32,") != base
    assert edit("pub gt4_rows: u64,") != base
    assert edit("// a note\n    pub gt4_rows:   u32, // rows") == base


def test_every_machine_file_is_written_for_the_trees_schema():
    for name in machine.names():
        doc = json.loads((machine.MACHINES / f"{name}.json").read_text("utf-8"))
        assert doc["schema"] == machine.schema_version(), name


def test_a_machine_file_of_another_schema_is_refused(tmp_path, monkeypatch):
    doc = json.loads(
        (ROOT / "machines" / "multimesh_v9t6.json").read_text(encoding="utf-8")
    )
    monkeypatch.setattr(machine, "MACHINES", tmp_path)
    for old in (1, "0000000000000000", None):
        doc["schema"] = old
        (tmp_path / "old.json").write_text(json.dumps(doc), encoding="utf-8")
        with pytest.raises(ValueError, match="regenerate the machine files"):
            machine.load("old")


def test_an_unknown_fact_is_refused():
    m = machine.load("multimesh_v9t6")
    with pytest.raises(AttributeError, match="vc.nonexistent"):
        _ = m.vc.nonexistent
