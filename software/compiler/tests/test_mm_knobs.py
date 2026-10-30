"""The contraction knobs have one source, the native generator's: Python's
plan is the native plan of the call it would record, its knobs those the
`KTPU_MM_*` overrides resolve natively."""

import json

import pytest

from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.runtime import generated
from kohakutpu.compiler import _rs
from kohakutpu.stack import rs

MACHINE = rs.machine_json("multimesh_v9t6")


@pytest.fixture(autouse=True)
def no_overrides(monkeypatch):
    for env, _ in MM.KNOBS.values():
        monkeypatch.delenv(env, raising=False)


def test_the_knob_table_is_the_native_one():
    assert set(MM.KNOBS) == set(json.loads(_rs.mm_knobs()))
    assert {g for _, g in MM.KNOBS.values()} <= {"plan", "kernel"}
    assert MM.plan_knobs() == {
        "pad_m": False,
        "n_tiles": 1,
        "dense_n": False,
        "pad_k": False,
    }
    assert MM.kernel_knobs() == {"batch_tiles": False}


def test_a_recorded_call_keeps_its_knobs(monkeypatch):
    """`generated.launch` fills each kernel knob the call does not name from
    the overrides, and a knob the call names stays as recorded."""
    seen = []
    monkeypatch.setattr(
        generated, "graph", lambda g, a, m, mutant: seen.append(json.loads(a)) or ""
    )

    class Dev:
        machine = "multimesh_v9t6"

        def kernel(self, source):
            return source

        def run(self, compiled, bindings, kind, info):
            raise RuntimeError("generated only")

    monkeypatch.setattr(generated.rs, "machine_json", lambda m: MACHINE)
    defaults = MM.kernel_knobs()
    recorded = {"e": {}, **{k: away(v) for k, v in defaults.items()}}
    for k, v in defaults.items():
        monkeypatch.setenv(MM.KNOBS[k][0], str(int(v)))
    with pytest.raises(RuntimeError):
        generated.launch(Dev(), "matmul.mm_kernel", recorded, {})
    assert {k: seen[0][k] for k in recorded} == recorded
    monkeypatch.setenv(MM.KNOBS["batch_tiles"][0], "1")
    with pytest.raises(RuntimeError):
        generated.launch(Dev(), "matmul.mm_kernel", {"e": {}}, {})
    assert seen[1]["batch_tiles"] is True


def away(v):
    """A knob value other than `v`."""
    return not v if isinstance(v, bool) else v + 1


def test_an_override_moves_the_plan_through_the_native_knobs(monkeypatch):
    base = MM.plan(1, 1576, 768, 768, machine_json=MACHINE)
    assert (base.tm, base.mp) == (8, 1576)
    monkeypatch.setenv("KTPU_MM_PAD_M", "1")
    assert MM.plan_knobs()["pad_m"] is True
    padded = MM.plan(1, 1576, 768, 768, machine_json=MACHINE)
    assert (padded.tm, padded.mp) == (228, 1596)
    assert MM.plan(1, 1576, 768, 768, machine_json=MACHINE, pad_m=False) == base


def test_the_plan_is_the_recorded_calls_replay(monkeypatch):
    monkeypatch.setenv("KTPU_MM_DENSE_N", "1")
    mm = MM.plan(2, 64, 96, 40, False, 16, machine_json=MACHINE)
    args = {
        "bt": 2,
        "m": 64,
        "n": 96,
        "k": 40,
        "shared": False,
        "quantum": 16,
        **MM.pairing_arg(),
        **MM.plan_knobs(),
    }
    assert vars(mm) == json.loads(_rs.mm_plan(json.dumps(args), MACHINE))
    assert mm.tn == 96


@pytest.mark.parametrize("name", ["multimesh_v9t6", "vlt_v9t6s_1n"])
@pytest.mark.parametrize("rows,rp", [(64, 64), (40, 48)])
def test_a_pad_k_operand_quantises_with_its_tail_padded(monkeypatch, name, rows, rp):
    """KTPU_MM_PAD_K pairs a 96-deep K into 128 where the pairing pairs
    blocks; the native quant of its
    96-column operand packs the 32-column tail as padding and compiles."""
    machine = rs.machine_json(name)
    monkeypatch.setenv("KTPU_MM_PAD_K", "1")
    mm = MM.plan(1, 64, 128, 96, pairing="vector", machine_json=machine)
    assert (mm.kp, mm.nk) == (128, 2)
    args = dict(
        src=dict(kind="full", pitch=96, rows=0, offset=0, dtype="f16"),
        rows=rows,
        cols=96,
        rp=rp,
        cp=mm.kp,
        side="b",
        g=rp // 4,
        nk=mm.nk,
    )
    source = _rs.generate("matmul.quant_kernel", json.dumps(args), machine, None)
    quant = [n for n in json.loads(source)["body"] if n.get("op") == "quant"]
    assert len(quant) == 1 and dict(quant[0]["kw"])["pad"]["int"] == 1
    compiled = _rs.compile(source, machine, cache_dir="", fetch=True)
    assert not compiled["verdict"]["refused"], compiled["verdict"]
    assert pad_fill(compiled["l1"]) == "s4"
    lifted = _rs.compile(
        source, machine, cache_dir="", fetch=True, mutant="l2.packing/unit-scale-zero"
    )
    assert pad_fill(lifted["l1"]) == "k0"


def pad_fill(l1):
    """The register the K tail's padding entries pack from: the zero lift
    (`s4`, RUL.099), so the nk = 2 pair merge never shifts the live block."""
    (fill,) = {
        line.split(",")[1].strip()
        for image in l1.split("\nimage ")
        if "vpack v0 -> l1[128] mx7b" in image
        for line in image.splitlines()
        if line.strip().startswith("vmov v0,")
    }
    return fill


def test_an_n_not_whole_blocks_is_refused():
    with pytest.raises(ValueError, match="API.OP.006"):
        MM.plan(1, 64, 40, 64, machine_json=MACHINE)
