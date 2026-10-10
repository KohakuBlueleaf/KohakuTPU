"""`python -m kohakutpu.ir`: text in and out at every stage, and the packages
it builds are the ones the in-memory compiler builds."""

import pathlib

from kohakutpu.ir import l2, l3
from kohakutpu.ir.__main__ import main
from kohakutpu.ir.l2 import text as l2_text

GOLDEN = pathlib.Path(__file__).with_name("golden")


def test_check_reads_every_level(capsys):
    files = [
        GOLDEN / "words.l1",
        GOLDEN / "schedule.l2",
        *sorted(l3.KERNELS.glob("*.l3")),
    ]
    assert main(["check", *map(str, files)]) == 0
    assert capsys.readouterr().out.count(": ok") == len(files)


def test_a_wrong_text_fails_with_its_position(tmp_path, capsys):
    bad = tmp_path / "bad.l3"
    bad.write_text(
        "level l3\nprogram p(x: f16[4])\n    y = frob x : f16[4]\n", encoding="utf-8"
    )
    assert main(["check", str(bad)]) == 1
    assert "bad.l3:3:5: kohakutpu has no op 'frob'" in capsys.readouterr().err


def test_lower_then_build_is_the_compiler_in_memory(tmp_path):
    l1_path, out = tmp_path / "schedule.l1", tmp_path / "pkg"
    assert main(["lower", str(GOLDEN / "schedule.l2"), "-o", str(l1_path)]) == 0
    assert main(["build", str(l1_path), "-o", str(out)]) == 0
    schedule = l2_text.read((GOLDEN / "schedule.l2").read_text(encoding="utf-8"))
    resident: dict = {}
    want = [p.build(None, resident).build().to_bytes() for p in l2.compile(schedule)]
    got = [(out / f"schedule.p{i}.pkg").read_bytes() for i in range(len(want))]
    assert got == want and len(want) == 2


def test_lower_takes_an_l3_program_down_to_l1(tmp_path, capsys):
    l2_path, l1_path = tmp_path / "ln.l2", tmp_path / "ln.l1"
    rows = str(l3.KERNELS / "rows.l3")
    shape = ["--shape", "x=32x128", "--shape", "g=128", "--shape", "b=128"]
    assert main(["lower", rows, "-p", "layernorm", *shape, "-o", str(l2_path)]) == 0
    assert main(["lower", str(l2_path), "-o", str(l1_path)]) == 0
    assert main(["check", str(l2_path), str(l1_path)]) == 0
    text = l2_path.read_text(encoding="utf-8")
    assert text == l2_text.write(l2_text.read(text))
    assert "vec_stream" in text and '"vp"' in text
    assert main(["lower", rows, "--shape", "x=32x128"]) == 1
    assert "holds 2 programs; name one with -p" in capsys.readouterr().err


def test_fmt_prints_the_canonical_text(capsys):
    assert main(["fmt", str(GOLDEN / "schedule.l2")]) == 0
    text = capsys.readouterr().out
    assert text == l2_text.write(l2_text.read(text))
    assert "buffer p7 at 0x8010_a800 bytes=2K" in text
