"""Select on the card model (L2.OP.006): `o = sel(cmplt(v, exp2 v), v, exp2 v)`
is v for every finite v, since 2^v > v; through the EXP2 domain guard too
(v >= 128, where 2^v is past f32). The full stack lowers the select to a
predicated move pair (`vmov o, e` then `vmov o, v if=m`), which returns a
zero as +0 (ISA.VC.058): y equals x bit for bit except 0x8000 -> 0x0000,
for the frozen e2e golden's L1 and for the L2 text compiled now, over every
f16 subnormal of both signs as well (all exact). The same kernel without the
select (`unpack_pack`) keeps -0, so the f16 -> f32 -> f16 path is exact
everywhere. The arms-swapped and the wrong-arm lowerings give 2^v instead.
"""

import json
from pathlib import Path

import numpy as np
import pytest

from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

ROOT = Path(__file__).resolve().parents[4]
GOLDEN = ROOT / "software/rust/golden/e2e/l2-op-L2.OP.006.vsel.json"
#: The kernel's one trip covers rows 0..3 (`for t in 0..1`, 4 rows a trip).
ROWS = 4
TRIP = ROWS * 128


def probe():
    """Both zeros, small, guard-boundary (127..130), large and very negative
    values, then seeded normal f16 values over the full range."""
    edge = [0.0, -0.0, 0.5, -3.0, 10.0, 15.5, 20.0, 100.0, 127.0, 128.0, 130.0]
    edge += [200.0, 255.0, 256.0, 300.0, 1000.0, -100.0, -130.0, -200.0, -300.0]
    edge += [65504.0, -65504.0]
    rng = np.random.default_rng(6)
    bits = rng.integers(0x0400, 0x7C00, TRIP - len(edge), dtype=np.uint16)
    bits |= rng.integers(0, 2, bits.size, dtype=np.uint16) << 15
    return np.concatenate([np.array(edge, np.float16), bits.view(np.float16)])


def subnormals():
    """Every f16 subnormal, both signs (0x0001..0x03FF, 0x8001..0x83FF)."""
    bits = np.arange(1, 0x0400, dtype=np.uint16)
    return np.concatenate([bits, bits | 0x8000]).view(np.float16)


def through(dev, c, values):
    """`values` through kernel `c`, one launch per trip of TRIP elements."""
    out = []
    for i in range(0, values.size, TRIP):
        x = np.zeros((8, 128), np.float16)
        part = values[i : i + TRIP]
        x.ravel()[: part.size] = part
        xa, ya = dev.alloc(x.nbytes), dev.alloc(x.nbytes)
        try:
            dev.write(xa, x.tobytes())
            dev.write(ya, bytes(x.nbytes))
            dev.run(c, {"x": xa, "y": ya})
            y = np.frombuffer(dev.read(ya, x.nbytes), np.float16)
        finally:
            dev.free(xa)
            dev.free(ya)
        out.append(y[: part.size])
    return np.concatenate(out)


def changed(x, y):
    """{input bits: output bits} of every element not returned bit-exact."""
    a, b = x.view(np.uint16), y.view(np.uint16)
    return {f"{int(p):04x}": f"{int(q):04x}" for p, q in zip(a, b) if p != q}


@pytest.fixture(scope="module")
def case():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def round_trip(l2):
    """`l2` with the exp2 / cmplt / sel lines dropped: y = pack(unpack x)."""
    keep = [
        s for s in l2.splitlines() if not s.lstrip().startswith(("e =", "m =", "o ="))
    ]
    return "\n".join(keep).replace("pack o ", "pack v ")


def compiled(dev, case, form, mutant=None):
    if form == "golden_l1":
        return K.compile(case["l1"], name="k", machine=dev.machine, level="l1")
    text = round_trip(case["l2"]) if form == "unpack_pack" else case["l2"]
    with rs.mutant(mutant):
        return K.compile(text, name="k", machine=dev.machine)


def test_round_trip_text_drops_only_the_select(case):
    text = round_trip(case["l2"])
    assert "pack v " in text and not {"exp2", "cmplt", "sel"} & set(text.split())
    assert len(case["l2"].splitlines()) - len(text.splitlines()) == 3


#: What each form changes (ISA.VC.058): a move returns a zero as +0; the bare
#: unpack / pack changes nothing.
FLUSH = {"golden_l1": {"8000": "0000"}, "lowered_l2": {"8000": "0000"}}


@pytest.mark.parametrize("form", ["golden_l1", "lowered_l2", "unpack_pack"])
@pytest.mark.parametrize("values", [probe, subnormals])
def test_vsel_selects_v(dev, case, form, values):
    x = values()
    got = changed(x, through(dev, compiled(dev, case, form), x))
    print(json.dumps({"form": form, "values": values.__name__, "changed": got}))
    present = {f"{int(b):04x}" for b in x.view(np.uint16)}
    want = {k: v for k, v in FLUSH.get(form, {}).items() if k in present}
    assert got == want, f"{len(got)} of {x.size} changed: {got}"


def test_probe_holds_negative_zero():
    assert np.float16(-0.0).tobytes() in {b.tobytes() for b in probe()}


@pytest.mark.parametrize(
    "mutant", ["l2.vector/sel-arms-swapped", "l2.predicate-select/wrong-arm"]
)
def test_broken_select_returns_exp2(dev, case, mutant):
    x = probe()
    c = compiled(dev, case, "lowered_l2", mutant)
    assert len(changed(x, through(dev, c, x))) > x.size // 2
