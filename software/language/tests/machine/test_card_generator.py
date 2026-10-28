"""Generated address fixtures and refusal checks; no model or card."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / "scripts" / "py"
FIXTURES = ROOT / "software/rust/crates/ktpu-machine/tests/fixtures"
VARIANTS = (
    ("die.staging.entry_words=2",),
    ("die.address.mesh_bits=1",),
    ("die.address.mesh_shift=37", "die.address.mesh_bits=1"),
)


READERS = """
import json, re, sys
from machine.read.image import Image
from machine.table import address_fields, staging_param
class Mutated(Image):
    def grep(self, path, pattern):
        match, source = super().grep(path, pattern)
        if sys.argv[1] == 'words' and path.endswith('mag_mem_port.v'):
            match = re.search(pattern, match.group(0).replace('(4)', '(2)'))
        if sys.argv[1] == 'aperture' and path.endswith('mag_mem_port.v'):
            match = re.search(pattern, match.group(0).replace("4'h0", "4'h1"))
        return match, source
img = Mutated('v9t6')
print(json.dumps(address_fields(img)))
print(json.dumps(staging_param(img, 'WORDS')))
print(json.dumps(staging_param(img, 'AP_STAGE')))
"""


@pytest.mark.parametrize("site", ("words", "aperture"))
def test_repeated_rtl_sites_must_agree(site):
    good = subprocess.run(
        [sys.executable, "-c", READERS, "none"],
        cwd=SCRIPTS,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert good.returncode == 0, good.stderr
    broken = subprocess.run(
        [sys.executable, "-c", READERS, site],
        cwd=SCRIPTS,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert broken.returncode != 0
    assert "disagrees" in broken.stderr


def generate(*args):
    return subprocess.run(
        [sys.executable, "-m", "machine", "--ver", "v9t6", *args],
        cwd=SCRIPTS,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_fixtures_cannot_enter_production_machines():
    got = generate("--set", "die.staging.entry_words=2", "--check")
    assert got.returncode != 0
    assert "outside machines/" in got.stderr


@pytest.mark.parametrize("settings", VARIANTS)
def test_fixtures_regenerate_byte_equal(settings, tmp_path):
    args = [arg for setting in settings for arg in ("--set", setting)]
    got = generate(*args, "--out", str(tmp_path))
    assert got.returncode == 0, got.stdout + got.stderr
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert files[0].read_bytes() == (FIXTURES / files[0].name).read_bytes()
    doc = json.loads(files[0].read_text(encoding="utf-8"))
    for setting in settings:
        assert doc["sources"][setting.split("=")[0]] == "fixture: --set"


@pytest.mark.parametrize(
    "setting",
    (
        "die.missing=1",
        "die.address.mesh_bits=true",
        'die.address.mesh_bits="1"',
        "die.address.mesh_bits=0",
        "die.address.mesh_bits=3",
        "die.address.special_bit=40",
        "die.address.aperture_bits=5",
        "die.address.aperture_shift=20",
        "die.staging.entry_words=0",
    ),
)
def test_invalid_fixture_is_refused(setting, tmp_path):
    got = generate("--set", setting, "--out", str(tmp_path))
    assert got.returncode != 0
    assert not list(tmp_path.glob("*.json"))
