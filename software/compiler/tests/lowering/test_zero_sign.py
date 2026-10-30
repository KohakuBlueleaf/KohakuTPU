"""The `zero_sign` knob's plumbing (L2.VC.026), offline: the default is
`_rs.ZERO_SIGN` ("native"); `_rs.compile(zero_sign=)` and `rs.zero_sign`
reach the vector lowering on fetch and node-fed builds and through
`compile_many`; the stage cache, `kernel.compile`'s cache and the prebuilt
library keep the two values apart (a library entry per value, each a hit
that compiles nothing); an unknown value is refused. The emitted forms per op are the
ktpu-l2 unit tests' (`lower/vector/sign/tests.rs`); the values they return
are the card model's (`application/tests/runtime/test_zero_sign_model.py`).
"""

import json
import re

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.compiler import _rs
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

MACHINE = "multimesh_v9t6"
BOARD = "vlt_v9t6s_1n"
SOURCE = """target ktpu.v9

fn k.l2(x: f16[8, 128] @dram, y: f16[8, 128] @dram)
    par c in 0..1 on vc[c]
        for t in 0..1
            xl = load x[t*4 : +4, :]   : f16[4, 128] @l1
            v = unpack xl                      : f32[4, 128] @reg
            n = neg v                          : f32[4, 128] @reg
            o = max n, v                       : f32[4, 128] @reg
            ol = pack o                     : f16[4, 128] @l1
            store y[t*4 : +4, :] <- ol
"""


def machine_json():
    return rs.machine_json(OfflineCard(MACHINE).machine)


def ops(l1):
    return re.findall(r"^\s*(v[a-z0-9]+)\b", l1, re.MULTILINE)


def lowered(zero_sign=None, fetch=True, cache_dir=""):
    got = _rs.compile(
        SOURCE,
        machine_json(),
        cache_dir=cache_dir,
        fetch=fetch,
        zero_sign=zero_sign,
    )
    assert not got["verdict"]["refused"], got["verdict"]
    return got["l1"]


def test_the_default_is_native():
    assert _rs.ZERO_SIGN == "native"
    assert lowered() == lowered("native")


@pytest.mark.parametrize("fetch", [True, False])
def test_preserve_replaces_neg_and_max(fetch):
    native, preserve = ops(lowered("native", fetch)), ops(lowered("preserve", fetch))
    assert {"vneg", "vmax"} <= set(native)
    assert not {"vneg", "vmax"} & set(preserve)
    assert {"vsub", "vcmplt"} <= set(preserve)


def test_compile_many_takes_the_knob():
    many = _rs.compile_many(
        [SOURCE], machine_json(), cache_dir="", zero_sign="preserve"
    )
    assert "vneg" not in ops(many[0]["l1"])


def test_an_unknown_value_is_refused():
    with pytest.raises(ValueError, match="native or preserve"):
        lowered("keep")


def test_the_stage_cache_keeps_the_values_apart(tmp_path):
    cache = str(tmp_path)
    native = lowered("native", cache_dir=cache)
    preserve = lowered("preserve", cache_dir=cache)
    assert lowered("native", cache_dir=cache) == native != preserve


def test_a_device_keys_the_context():
    dev = D.Device("offline", board=BOARD)
    native = dev._kernel_key("g", None, False)
    with rs.zero_sign("preserve"):
        assert dev._kernel_key("g", None, False) != native


def max_graph(dev):
    """A traced graph whose lowering holds a vmax under native."""
    kernel, given = V.flat_kernel(X.op("max", X.Load(0), 0.0), ("f16",), 128, "f16", 2)
    return kernel.trace(given, machine=K.dsl_machine(dev.machine)).graph_json


def test_a_library_holds_each_value_and_a_hit_compiles_nothing(tmp_path):
    dev = D.Device(
        "offline", board=BOARD, library_only=True, library=tmp_path, classes=False
    )
    source = max_graph(dev)
    machine = rs.machine_json(dev.machine)
    with rs.zero_sign("preserve"):
        built = json.loads(rs.library_prebuild(tmp_path, source, machine)["key"])
        preserve = dev.compile_graph(source)
    assert dev.counters["exact_hits"] == 1
    assert dev.counters["compile_calls"] == 0
    assert "vmax" not in ops(preserve["l1"])
    with pytest.raises(RuntimeError, match="library miss"):
        dev.compile_graph(source)
    key = json.loads(rs.library_prebuild(tmp_path, source, machine)["key"])
    native = dev.compile_graph(source)
    assert dev.counters["exact_hits"] == 2
    assert dev.counters["compile_calls"] == 0
    assert "vmax" in ops(native["l1"])
    assert key["class"] == built["class"]
    assert key["machine"] != built["machine"]
    assert key["machine"] == rs.library_machine(machine)
    assert json.loads(_rs.library_key(source, machine, zero_sign="native")) == key
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        [key["machine"], built["machine"]]
    )


def test_a_library_blind_to_the_value_serves_the_wrong_one(tmp_path, monkeypatch):
    for name in ("library_prebuild", "library_get"):
        call = getattr(_rs, name)
        monkeypatch.setattr(_rs, name, lambda *a, zero_sign=None, call=call: call(*a))
    dev = D.Device(
        "offline", board=BOARD, library_only=True, library=tmp_path, classes=False
    )
    source = max_graph(dev)
    with rs.zero_sign("preserve"):
        rs.library_prebuild(tmp_path, source, rs.machine_json(dev.machine))
        served = dev.compile_graph(source)
    assert dev.counters["compile_calls"] == 0
    assert "vmax" in ops(served["l1"])


def test_kernel_compile_keys_the_context():
    machine = OfflineCard(MACHINE).machine
    native = K.compile(SOURCE, name="k", machine=machine)
    with rs.zero_sign("preserve"):
        preserve = K.compile(SOURCE, name="k", machine=machine)
    assert "vneg" in ops(native.l1_text)
    assert "vneg" not in ops(preserve.l1_text)
    assert K.compile(SOURCE, name="k", machine=machine) is native
