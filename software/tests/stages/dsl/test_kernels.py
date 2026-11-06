"""DSL to L2: each small kernel on the evaluator against its L2 on the simulator."""

import json
import pathlib
import sys

import numpy as np
import pytest

import kohakutpu.language.dsl as kt
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.application.runtime.offline import HostMemory
from kohakutpu.language.dsl import ops
from kohakutpu.language.examples.dsl.cluster.linear import linear
from kohakutpu.language.examples.dsl.vector.activation import activation
from kohakutpu.language.examples.dsl.vector.glu import glu
from kohakutpu.language.examples.dsl.vector.norm import layernorm
from kohakutpu.language.examples.dsl.vector.softmax import softmax
from kohakutpu.stack import rs
from kohakutpu.stack.buffer import Buffer
from kohakutpu.stack.kernel import dsl_machine
from kohakutpu.stack.rs import _rs

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from evaluate import evaluate

BOARD = "multimesh_v9t8"
PAGE = 1 << 16
#: f16 math stays this near the ideal, of the output's largest value.
PLAIN = 1e-3
MX7 = 0.03


@pytest.fixture(scope="module")
def machine():
    return machine_of(BOARD)


def on_the_simulator(traced, inputs, text):
    """Output tensors of `text`, the bytes through each port's own layout."""
    json_machine = machine_of(BOARD).machine_json
    mem = HostMemory()
    binds, buffers = {}, {}
    at = PAGE
    for name, spec in traced.params:
        b = Buffer.of(spec.text(), json_machine)
        buffers[name] = b
        binds[name] = at
        at += -(-b.nbytes // PAGE) * PAGE
        mem.write_block(binds[name], b.pack(inputs.get(name, np.zeros(spec.shape))))
    _rs.simulate_l2(text, traced.name, binds, mem.read_block, mem.write_block)
    return {
        n: b.unpack(mem.read_block(binds[n], b.nbytes))
        for n, b in buffers.items()
        if traced.roles[n] == "out"
    }


def check(kernel, machine, given, inputs, bound=PLAIN, **consts):
    traced = kernel.trace(given, machine=dsl_machine(machine), **consts)
    graph = json.dumps(traced.graph)
    text = _rs.l2_of(graph, machine.machine_json, rs._MUTANT.get())
    want = evaluate(traced.graph, inputs)
    got = on_the_simulator(traced, inputs, text)
    for name, g in got.items():
        scale = max(float(np.abs(want[name]).max()), 1e-3)
        worst = float(np.abs(g - want[name]).max())
        assert worst <= bound * scale, f"{name}: {worst} of {scale}"


R = np.random.default_rng(0)


@pytest.mark.parametrize("act", ["none", "silu", "gelu", "relu"])
def test_activations(machine, act):
    x = R.normal(size=(16, 64))
    check(activation, machine, {"x": x.astype(np.float16)}, {"x": x}, act=act)


def test_softmax(machine):
    x = R.normal(size=(16, 64))
    check(softmax, machine, {"x": x.astype(np.float16)}, {"x": x})


def test_layernorm(machine):
    x, g, b = R.normal(size=(16, 64)), R.normal(size=64), R.normal(size=64)
    inputs = {"x": x, "g": g, "b": b}
    check(
        layernorm, machine, {k: v.astype(np.float16) for k, v in inputs.items()}, inputs
    )


def test_glu(machine):
    a, g = R.normal(size=(16, 64)), R.normal(size=(16, 64))
    inputs = {"a": a, "g": g}
    check(glu, machine, {k: v.astype(np.float16) for k, v in inputs.items()}, inputs)


def test_padded_heads_check_their_finite_status_once(machine):
    """Heads padded 13 to 16 rows and 144 to 160 columns: each a checked quant
    whose status waits for one check at the end, its bytes the reference packing
    of the zero-padded heads."""
    rows, cols, rp, cp, heads = 13, 144, 16, 160, 3
    args = dict(
        src=dict(kind="full", dtype="f16", offset=0, pitch=cols, rows=heads * rows),
        rows=rows,
        cols=cols,
        rp=rp,
        cp=cp,
        side="a",
        g=4,
        nk=1,
        batches=heads,
        pad_strategy="native",
        schedule=dict(finite_batch=4, pipeline=1, zero_classifier=1, stream_axes=1),
    )
    graph = _rs.generate(
        "matmul.quant_kernel", json.dumps(args), machine.machine_json, rs._MUTANT.get()
    )
    text = _rs.l2_of(graph, machine.machine_json, rs._MUTANT.get())
    assert text.count(" status=") == heads and text.count("quant_check") == 1, text
    ports = json.loads(graph)["ports"]
    name = text.split("fn ", 1)[1].split(".l2(", 1)[0]
    mem, binds, buffers, at = HostMemory(), {}, {}, PAGE
    x = R.normal(size=(heads * rows, cols))
    for p in ports:
        spec = p["spec"]
        layout = {k: v["int"] for k, v in spec.get("layout", [])}
        t = kt.TensorSpec.make(spec["shape"], spec["dtype"], layout)
        buffers[p["name"]] = b = Buffer.of(t.text(), machine.machine_json)
        binds[p["name"]] = at
        at += -(-b.nbytes // PAGE) * PAGE
        value = x if p["name"] == "x" else np.zeros(spec["shape"])
        mem.write_block(binds[p["name"]], b.pack(value))
    _rs.simulate_l2(text, name, binds, mem.read_block, mem.write_block)
    want = np.zeros((heads * rp, cp))
    for h in range(heads):
        want[h * rp : h * rp + rows, :cols] = x[h * rows : (h + 1) * rows]
    q = buffers["q"]
    assert mem.read_block(binds["q"], q.nbytes) == q.pack(want)


def test_rotated_runs_gather_their_source_words_once(machine):
    """Runs of 28 values from odd source offsets into rows 32 apart: each staged row
    gathered once, its next words walked from it, and every value in place."""
    step = dict(
        src_n=300, walk=[[8, 33], [28, 1]], at=3, dwalk=[[8, 32], [28, 1]], dat=0
    )
    step |= dict(m=256, keep=False)
    call = {"pl": {"made_by": {"gen": "relayout.plan", "args": step}}}
    plan = json.loads(
        _rs.relayout_plan(json.dumps(step), machine.machine_json, rs._MUTANT.get())
    )
    graph = _rs.generate(
        "relayout.relayout_kernel",
        json.dumps(call),
        machine.machine_json,
        rs._MUTANT.get(),
    )
    text = _rs.l2_of(graph, machine.machine_json, rs._MUTANT.get())
    assert text.count(" <- src fp index=") == 1, text
    # The next words' walks write whole lines: two walks ending inside one line
    # would both write part of it (L1.VER.017).
    m = json.loads(machine.machine_json)["machine"]
    line, word = m["card"]["dram"]["line_bytes"], m["die"]["word_bytes"]
    body = json.loads(graph)["body"]
    kws = [dict(op["kw"]) for op in body if op.get("op") == "copy"]
    shifted = [k for k in kws if "at" in k and k["at"]["int"] - k["dat"]["int"] == word]
    assert shifted
    for kw in shifted:
        count, stride = kw["dwalk"]["walk"][0]
        assert (count * stride) % line == 0 and kw["dat"]["int"] % line == 0, kw
    rows = [op["dst"]["axes"] for op in body if op.get("op") == "gather"]
    assert rows
    for axes in rows:
        assert axes[1]["run"][0] == 0 and (axes[1]["run"][1] * 2) % line == 0, axes
    name = text.split("fn ", 1)[1].split(".l2(", 1)[0]
    src = R.normal(size=304).astype(np.float16)
    values = {
        "src": src.reshape(-1, 16),
        "index": np.asarray(plan["index"], np.float64),
        "mask": np.asarray(plan["masks"], np.float64),
    }
    mem, binds, buffers, at = HostMemory(), {}, {}, PAGE
    for p in json.loads(graph)["ports"]:
        spec = p["spec"]
        t = kt.TensorSpec.make(spec["shape"], spec["dtype"])
        buffers[p["name"]] = b = Buffer.of(t.text(), machine.machine_json)
        binds[p["name"]] = at
        at += -(-b.nbytes // PAGE) * PAGE
        value = values.get(p["name"], np.full(spec["shape"], np.nan))
        mem.write_block(binds[p["name"]], b.pack(value.reshape(spec["shape"])))
    _rs.simulate_l2(text, name, binds, mem.read_block, mem.write_block)
    got = buffers["dst"].unpack(mem.read_block(binds["dst"], buffers["dst"].nbytes))
    want = np.zeros(256)
    rows = np.arange(8)[:, None]
    want[(rows * 32 + np.arange(28)).ravel()] = src[
        (3 + rows * 33 + np.arange(28)).ravel()
    ]
    assert np.array_equal(got.reshape(-1), want)


def test_contraction(machine):
    x, w = R.normal(size=(32, 64)), R.normal(size=(64, 64))
    bias = R.normal(size=(64,))
    given = {
        "x": kt.TensorSpec.make((32, 64), "mx7a", {"gm": 8, "nk": 2}),
        "w": kt.TensorSpec.make((64, 64), "mx7b", {"gn": 16, "nk": 2}),
        "bias": bias.astype(np.float16),
    }
    check(
        linear,
        machine,
        given,
        {"x": x, "w": w, "bias": bias},
        MX7,
        tn=64,
        tm=32,
        rows=4,
    )


def test_a_whole_row_band_is_taken_only_where_the_lowering_buffers_fit(tmp_path):
    """GPT's row statistic over two [224, 128] operands, a column and a row: one
    128-column band of 16 rows, each looped view double-buffered as the lowering
    lays it, needs more than a core's 512 L1 words, so the kernel keeps two
    column blocks (L2.PASS.010 refused the whole-row band)."""
    full = dict(kind="full", dtype="f16", offset=0, pitch=128, rows=0)
    col = dict(kind="col", dtype="f16", offset=0, pitch=0, rows=0)
    row = dict(kind="row", dtype="f16", offset=0, pitch=0, rows=0)
    nodes = [
        {"load": 0},
        {"load": 1},
        {"load": 3},
        {"op": "mul", "args": [1, 2]},
        {"op": "mul", "args": [0, 3]},
        {"reduce": "sum", "arg": 4},
        {"load": 2},
        {"op": "mul", "args": [5, 6]},
    ]
    e = dict(nodes=nodes, root=7)
    args = dict(e=e, operands=[full, col, row, full], R=224, W=128, out=row)
    json_machine = machine_of("multimesh_v9t6").machine_json
    source = _rs.generate(
        "vector.rows_kernel", json.dumps(args), json_machine, rs._MUTANT.get()
    )
    rs.library_prebuild(tmp_path, source, json_machine)


def test_a_composed_statistic_is_the_shape_its_caller_allocates(machine):
    """GPT's row statistic over a transposed read: the kernel reading it composed
    writes the one-block output `rows_shape` gives the runtime to allocate."""
    walk = [[8, 128], [28, 0], [128, 1]]
    plan = dict(walk=walk, at=0, dwalk=[[8, 3584], [28, 128], [128, 1]], dat=0)
    plan.update(m=28672, src_n=1024, keep=False, z0=0, tree=True)
    nodes = [
        {"load": 0},
        {"load": 1},
        {"op": "mul", "args": [0, 1]},
        {"reduce": "sum", "arg": 2},
    ]
    full = dict(kind="full", dtype="f16", offset=0, pitch=128, rows=0)
    row = dict(kind="row", dtype="f16", offset=0, pitch=0, rows=0)
    e = dict(nodes=nodes, root=3)
    shape = dict(e=e, operands=[full, full], R=224, W=128, out=row)
    made = {"1": [{"made_by": {"gen": "relayout.plan", "args": plan}}]}
    args = dict(shape, made=made, alias={}, composed=[1])
    mutant = rs._MUTANT.get()
    want = json.loads(_rs.rows_shape(json.dumps(shape), machine.machine_json, mutant))
    graph = json.loads(
        _rs.generate(
            "prepared.rows_kernel", json.dumps(args), machine.machine_json, mutant
        )
    )
    out = next(p for p in graph["ports"] if p["name"] == "out")
    assert out["spec"]["shape"] == want["out"]


@kt.kernel
def sliced_reduction(x=kt.In(8, 64), y=kt.Out(4, 32), *, register=False):
    for _core in kt.par(1, on=kt.vc):
        value = kt.load(x[:, :], gt4=True)
        if register:
            value = kt.unpack(value)
        band = value[4:8, 32:64]
        total = kt.rsum(band)
        kt.store(y[:, :], ops.math("add", band, total))


@pytest.mark.parametrize("register", [False, True])
def test_direct_view_reduction_lowers_and_evaluates(machine, register):
    x = np.arange(512, dtype=np.float16).reshape(8, 64) / 512
    traced = sliced_reduction.trace(
        {"x": x}, machine=dsl_machine(machine), register=register
    )
    text = _rs.l2_of(json.dumps(traced.graph), machine.machine_json, rs._MUTANT.get())
    got = on_the_simulator(traced, {"x": x}, text)["y"]
    band = x[4:8, 32:64].astype(np.float64)
    np.testing.assert_allclose(got, band + band.sum(axis=1, keepdims=True), rtol=1e-3)
